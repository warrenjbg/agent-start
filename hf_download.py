"""Hugging Face GGUF lister and downloader: list a public model repo's .gguf files, download one into a local
models folder with its SHA-256 verified.

Stdlib only. Call list_files() / download() / discard_part() from a worker thread (they block on the network or
the disk); parse_repo(), url_allowed(), pending_part(), free_space() and space_ok() are cheap. Policy:
  * Repo ids come only through parse_repo() (the Hub's owner/name grammar). File paths come only from the API
    listing and are validated before they are offered (.gguf, no backslash / colon / control or format
    characters, no empty, '.' or '..' segment, no leading slash, no Windows device name, length caps).
  * HTTPS only, default port only, no userinfo. The first request of every call goes to exactly huggingface.co.
    Redirects are never followed by urllib: this module walks them hop by hop, at most MAX_HOPS, and checks every
    hop (url_allowed): huggingface.co, or a dot-anchored *.hf.co subdomain that does not start with cas-server. or
    transfer. (Xet protocol APIs). Listing redirects must stay on the API origin. Plain http is allowed solely for
    host 127.0.0.1 (any port) and only while _ALLOW_LOCAL_HTTP_FOR_TESTS is True (tests; False in the app).
  * Anonymous only: no token, no cookies, no Authorization header; the HF_TOKEN environment variable is never read.
    Gated and private repos are refused with a plain message.
  * Downloads are pinned to the commit the listing returned (/resolve/<commit>/<path>). Before anything is written,
    the huggingface.co response must carry X-Repo-Commit == that commit, and X-Linked-Size / X-Linked-ETag (when
    present) must equal the listed size / sha256.
  * Files are written only inside dest_dir (an existing folder on a local fixed drive) as <name>.part plus a small
    <name>.part.json sidecar, hashed while streaming, and renamed to <name> only after size and sha256 match the
    listing. An existing <name> is never overwritten. One download at a time per process. An overall deadline and
    a stop event are honoured between chunks.
  * Resume: a kept .part whose sidecar matches (repo, commit, path, size, sha256) is re-hashed, then the rest is
    requested with Range after a fresh resolve; only a 206 whose Content-Range starts exactly at the offset and
    names the full size is accepted. The whole file is hashed before it is accepted.
  * Nothing downloaded is executed or opened by this module beyond writing and hashing it.

Every refusal or failure raises HfError (kind + a message written for the user).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import ssl
import stat
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from launcher_bat import is_local_fixed_path

log = logging.getLogger("agent_start")

HUB_HOST = "huggingface.co"
MAX_HOPS = 5                              # redirects followed per request (every hop checked)
MAX_API_BYTES = 4 * 1024 * 1024           # model-info body cap
MAX_FILE_BYTES = 500 * 1024 ** 3          # sanity bound for one file (not a model-size policy)
SPACE_MARGIN_BYTES = 1024 ** 3            # free space kept on top of the remaining bytes
LIST_DEADLINE_S = 60.0                    # overall cap for one listing (connect + body)
NET_CHUNK = 256 * 1024
HASH_CHUNK = 1024 * 1024
PROGRESS_INTERVAL_S = 0.1                 # progress callback at most ~10 times a second
MAX_INPUT_CHARS = 2048                    # parse_repo input
MAX_PATH_CHARS = 512                      # repo-relative file path
MAX_NAME_CHARS = 200                      # one path segment (leaves room for '.part.json' under 255)
MAX_SIDECAR_BYTES = 4096
MAX_SIBLINGS = 50000
SIDECAR_APP = "agent-start"               # marks <name>.part.json (and so <name>.part) as made by this app

# Test hooks only. Never changed by the app.
_ALLOW_LOCAL_HTTP_FOR_TESTS = False
_API_BASE = "https://huggingface.co"      # listing URL = f"{_API_BASE}/api/models/<owner>/<name>?blobs=true"
_RESOLVE_BASE = "https://huggingface.co"  # download URL = f"{_RESOLVE_BASE}/<repo>/resolve/<commit>/<path>"

KINDS = frozenset({"bad_repo", "not_found", "gated", "no_gguf", "network", "rate_limited", "refused", "disk",
                   "exists", "cancelled", "timeout", "mismatch", "busy"})

_DEFAULT_MSG = {
    "bad_repo": "That does not look like a Hugging Face model. Paste owner/name or a huggingface.co link to a model.",
    "not_found": "This repo does not exist or is private. Private repos need a Hugging Face login, which Agent "
                 "Start does not support.",
    "gated": "This repo is gated: it needs a Hugging Face login, which Agent Start does not support.",
    "no_gguf": "This repo has no .gguf model files that Agent Start can download.",
    "network": "Could not reach Hugging Face. Check the internet connection and try again.",
    "rate_limited": "Hugging Face is limiting requests from this connection. Try again in a few minutes.",
    "refused": "The request was stopped for safety.",
    "disk": "Could not write the file to the models folder.",
    "exists": "A file with this name already exists in the models folder. Agent Start never overwrites a file.",
    "cancelled": "Download cancelled. The partial file is kept so the download can resume later.",
    "timeout": "The download took too long and was stopped. The partial file is kept so it can resume later.",
    "mismatch": "The file on Hugging Face does not match the list. Refresh the list and try again.",
    "busy": "Another model download is already running. Wait for it to finish or cancel it first.",
}

_KEPT = " The partial file is kept; start the download again to resume."

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_INT_RE = re.compile(r"[0-9]{1,20}")
_REPO_PART = r"[A-Za-z0-9_](?:[A-Za-z0-9_.\-]{0,94}[A-Za-z0-9_])?"
_REPO_RE = re.compile(rf"({_REPO_PART})/({_REPO_PART})")
_SPLIT_RE = re.compile(r"(?P<stem>.+)-(?P<i>[0-9]{5})-of-(?P<n>[0-9]{5})\.gguf", re.IGNORECASE)
_CONTENT_RANGE_RE = re.compile(r"bytes\s+([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})", re.IGNORECASE)
_RATELIMIT_T_RE = re.compile(r"(?:^|;)\s*t=([0-9]{1,9})(?:\s*(?:;|,|$))")
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?")
# Windows device names (same rule as launcher_bat's safe-mode paths)
_RESERVED_RE = re.compile(r"(?:con|prn|aux|nul|clock\$|conin\$|conout\$|com[0-9\u00b9\u00b2\u00b3]|"
                          r"lpt[0-9\u00b9\u00b2\u00b3])", re.IGNORECASE)
_BAD_PATH_CHARS = frozenset('\\:<>"|?*')
_URL_HOSTS = frozenset({"huggingface.co", "www.huggingface.co", "hf.co"})
_RESERVED_FIRST = frozenset({"datasets", "spaces", "api", "models", "docs", "collections", "papers", "organizations",
                             "settings", "blog", "join", "login", "logout", "new", "pricing", "search", "tasks",
                             "learn", "chat", "enterprise", "posts", "notifications", "billing", "storage"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_download_lock = threading.Lock()


class HfError(Exception):
    """A refusal or failure. `kind` is one of KINDS; str(exc) / `message` is a plain sentence for the user.
    For kind 'rate_limited', `retry_after_s` is the server's hint in seconds (or None)."""

    def __init__(self, kind: str, message: str | None = None, *, retry_after_s: int | None = None) -> None:
        if kind not in KINDS:
            kind = "network"
        msg = message or _DEFAULT_MSG[kind]
        super().__init__(msg)
        self.kind = kind
        self.message = msg
        self.retry_after_s = retry_after_s


class _Restart(Exception):
    """Internal: the kept .part cannot be resumed; discard it and start from zero."""


class _BadData(HfError):
    """Internal: the bytes received are wrong (size or sha256); the .part and sidecar are deleted."""


@dataclass(frozen=True)
class HfFile:
    path: str            # repo-relative, forward slashes
    name: str            # basename (the local file name)
    size: int            # bytes (for a split set: the sum of its parts)
    sha256: str          # 64 lower-case hex (for a split set: the first listed part's)
    kind: str            # 'model' | 'vision' | 'split'
    supported: bool
    note: str            # why it is not supported, or ''


@dataclass(frozen=True)
class RepoListing:
    repo_id: str                 # as the API returned it (canonical case / after a rename)
    commit: str                  # 40 hex; every download is pinned to it
    files: tuple[HfFile, ...]    # .gguf only, supported first, then by name


# ------------------------------------------------------------------------------------------- small helpers

def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _valid_repo_id(s: object) -> bool:
    if not isinstance(s, str) or len(s) > 193:
        return False
    m = _REPO_RE.fullmatch(s)
    if not m:
        return False
    for part in m.groups():
        if "--" in part or ".." in part or part.lower().endswith(".git"):
            return False
    return True


def _odd_char(c: str) -> bool:
    return ord(c) < 0x20 or unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def _valid_gguf_path(p: object) -> bool:
    """A repo-relative path from the API that may be offered and saved under its basename."""
    if not isinstance(p, str) or not p or len(p) > MAX_PATH_CHARS:
        return False
    if not p.lower().endswith(".gguf") or p.startswith("/"):
        return False
    if any(c in _BAD_PATH_CHARS or _odd_char(c) for c in p):
        return False
    for seg in p.split("/"):
        if seg in ("", ".", "..") or len(seg) > MAX_NAME_CHARS:
            return False
        if seg != seg.strip() or seg.endswith("."):       # Windows silently drops trailing dots / spaces
            return False
    name = p.rsplit("/", 1)[-1]
    if len(name) <= len(".gguf"):
        return False
    if _RESERVED_RE.fullmatch(name.split(".")[0].rstrip(" ")):
        return False
    return True


def _quote_path(path: str) -> str:
    return "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))


def _user_agent() -> str:
    try:
        from version import __version__
        return f"AgentStart/{__version__}"
    except Exception:  # noqa: BLE001
        return "AgentStart"


def _headers(accept: str) -> dict[str, str]:
    return {"User-Agent": _user_agent(), "Accept": accept, "Accept-Encoding": "identity"}


def _origin(url: str) -> tuple[str, str, int]:
    u = urllib.parse.urlsplit(url)
    scheme = u.scheme.lower()
    return scheme, (u.hostname or "").lower(), u.port or (443 if scheme == "https" else 80)


def _host_for_msg(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    host = "".join(c for c in host if c.isascii() and (c.isalnum() or c in ".-"))[:80]
    return host or "an unknown server"


def _set_sock_timeout(resp: Any, t: float) -> None:
    """Best effort: shrink the socket timeout so a single recv cannot outlive the overall deadline."""
    try:
        resp.fp.raw._sock.settimeout(max(0.05, t))
    except Exception:  # noqa: BLE001
        pass


def _read_some(resp: Any, n: int) -> bytes:
    rd = getattr(resp, "read1", None)
    return rd(n) if rd is not None else resp.read(n)


def _stop_check(stop_evt: threading.Event | None) -> None:
    if stop_evt is not None and stop_evt.is_set():
        raise HfError("cancelled")


def _deadline_fn(deadline: float, message: str) -> Callable[[], float]:
    def left() -> float:
        r = deadline - time.monotonic()
        if r <= 0:
            raise HfError("timeout", message)
        return r
    return left


def _retry_after(h: Any) -> int | None:
    if h is None:
        return None
    rl = h.get("RateLimit")
    if isinstance(rl, str):
        m = _RATELIMIT_T_RE.search(rl)
        if m:
            return min(int(m.group(1)), 86400)
    ra = h.get("Retry-After")
    if isinstance(ra, str) and _INT_RE.fullmatch(ra.strip()):
        return min(int(ra.strip()), 86400)
    return None


def _http_error(code: int, h: Any, what: str) -> HfError:
    err_code = (h.get("X-Error-Code") or "").strip() if h is not None else ""
    if code == 429:
        t = _retry_after(h)
        msg = (f"Hugging Face is limiting requests from this connection. Try again in {t} seconds."
               if t is not None else _DEFAULT_MSG["rate_limited"])
        return HfError("rate_limited", msg, retry_after_s=t)
    if err_code == "GatedRepo" and code in (401, 403):
        return HfError("gated")
    if code == 401:
        return HfError("not_found")
    if code == 404:
        if what == "file":
            return HfError("not_found", "This file is no longer available at the listed version. Refresh the list "
                                        "and try again.")
        return HfError("not_found", "This repo does not exist on Hugging Face. Check the name.")
    if code == 403:
        return HfError("network", "Hugging Face refused the request (HTTP 403). Try again later.")
    if 500 <= code <= 599:
        return HfError("network", f"Hugging Face had a server problem (HTTP {code}). Try again later.")
    return HfError("network", f"Hugging Face answered with an unexpected status (HTTP {code}).")


def _net_error(e: BaseException, what: str) -> HfError:
    log.debug("hf %s: network error: %s: %s", what, type(e).__name__, e)
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return HfError("network", "The secure connection to Hugging Face could not be verified.")
    if isinstance(reason, TimeoutError):
        if what == "file":
            return HfError("network", "The download server stopped answering." + _KEPT)
        return HfError("timeout", "Hugging Face did not answer in time. Try again later.")
    if what == "file":
        return HfError("network", "The connection to Hugging Face was lost." + _KEPT)
    return HfError("network")


# ------------------------------------------------------------------------------------------- repo input

def parse_repo(text: str) -> str:
    """'owner/name' or a pasted huggingface.co / hf.co model link -> canonical 'owner/name'.
    Raises HfError('bad_repo') for anything else (datasets, spaces, other hosts, userinfo, ports, odd ids)."""
    if not isinstance(text, str):
        raise HfError("bad_repo")
    s = text.strip()
    if not s or len(s) > MAX_INPUT_CHARS or any(not (0x21 <= ord(c) <= 0x7E) for c in s):
        raise HfError("bad_repo")
    low = s.lower()
    if "://" not in s and not low.startswith(tuple(f"{h}/" for h in _URL_HOSTS)):
        owner = s.split("/", 1)[0]
        if owner.lower() in _RESERVED_FIRST or not _valid_repo_id(s):
            raise HfError("bad_repo")
        return s
    if "://" not in s:
        s = "https://" + s
    try:
        u = urllib.parse.urlsplit(s)
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        raise HfError("bad_repo") from None
    if (u.scheme.lower() not in ("http", "https") or host not in _URL_HOSTS or port is not None
            or u.username is not None or u.password is not None or "@" in u.netloc
            or u.netloc.lower() != host):
        raise HfError("bad_repo")
    segs = u.path.split("/")
    if len(segs) < 3 or segs[0] != "":
        raise HfError("bad_repo")
    segs = segs[1:]
    if segs[-1] == "":
        segs = segs[:-1]
    if len(segs) < 2 or segs[0].lower() in _RESERVED_FIRST:
        raise HfError("bad_repo")
    rest = segs[2:]
    if rest:
        if rest[0] == "tree":
            ok = len(rest) >= 2
        elif rest[0] in ("blob", "resolve"):
            ok = len(rest) >= 3
        else:
            ok = False
        if not ok or not all(rest):
            raise HfError("bad_repo")
    repo = f"{segs[0]}/{segs[1]}"
    if not _valid_repo_id(repo):
        raise HfError("bad_repo")
    return repo


# ------------------------------------------------------------------------------------------- host policy

def url_allowed(url: str, *, first_hop: bool = False) -> bool:
    """The host policy for one request. HTTPS, default port, no userinfo, ASCII only. first_hop: the host must be
    exactly huggingface.co. Later hops: huggingface.co, or a dot-anchored *.hf.co subdomain that does not start
    with cas-server. or transfer. Plain http to 127.0.0.1 only while _ALLOW_LOCAL_HTTP_FOR_TESTS is True."""
    if not isinstance(url, str) or not url or len(url) > 16384:
        return False
    if any(not (0x21 <= ord(c) <= 0x7E) for c in url):
        return False
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return False
    if not host or u.username is not None or u.password is not None or "@" in u.netloc:
        return False
    scheme = u.scheme.lower()
    if scheme == "http" and host == "127.0.0.1" and _ALLOW_LOCAL_HTTP_FOR_TESTS:
        return True
    if scheme != "https" or port not in (None, 443):
        return False
    if host == HUB_HOST:
        return True
    if first_hop:
        return False
    labels = host.split(".")
    if len(labels) < 3 or labels[-2:] != ["hf", "co"]:
        return False
    if not all(_LABEL_RE.fullmatch(lab) for lab in labels):
        return False
    return not host.startswith(("cas-server.", "transfer."))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx responses surface as HTTPError so _walk() can check every hop itself."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))


def _walk(url: str, headers: dict[str, str], timeout: float, left: Callable[[], float], *, what: str,
          same_origin: bool, ranged: bool = False,
          check_hub: Callable[[Any], None] | None = None) -> tuple[Any, Any]:
    """GET url, following redirects by hand under url_allowed(). Returns (open response, headers of the last
    response from the first hop's origin). check_hub(headers) runs on those headers before the first request that
    leaves that origin (or on the final response when it never leaves). Raises HfError (or _Restart for a 416 on
    a ranged request)."""
    hub = _origin(url)
    hub_headers: Any = None
    hub_checked = False
    opener = _opener()
    for hop in range(MAX_HOPS + 1):
        if not url_allowed(url, first_hop=(hop == 0)) or (same_origin and hop and _origin(url) != hub):
            log.warning("hf %s: refused request to %s (hop %d)", what, _host_for_msg(url), hop)
            raise HfError("refused", f"Hugging Face redirected to an unexpected server ({_host_for_msg(url)}). "
                                     "The request was stopped for safety.")
        if check_hub is not None and not hub_checked and hop and _origin(url) != hub:
            check_hub(hub_headers)
            hub_checked = True
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            resp = opener.open(req, timeout=max(0.05, min(timeout, left())))
        except urllib.error.HTTPError as e:
            try:
                if _origin(url) == hub:
                    hub_headers = e.headers
                if e.code in _REDIRECT_CODES:
                    loc = (e.headers.get("Location") or "").strip()
                    if not loc:
                        raise HfError("network", "Hugging Face sent a redirect without a target.") from None
                    url = urllib.parse.urljoin(url, loc)
                    continue
                if e.code == 416 and ranged:
                    raise _Restart() from None
                raise _http_error(e.code, e.headers, what) from None
            finally:
                e.close()
        if _origin(url) == hub:
            hub_headers = resp.headers
        if check_hub is not None and not hub_checked:
            try:
                check_hub(hub_headers)
            except BaseException:
                resp.close()
                raise
        return resp, hub_headers
    log.warning("hf %s: more than %d redirects", what, MAX_HOPS)
    raise HfError("refused", "Hugging Face redirected too many times. The request was stopped for safety.")


# ------------------------------------------------------------------------------------------- listing

def list_files(repo: str, *, timeout: float = 15.0) -> RepoListing:
    """One GET of /api/models/<owner>/<name>?blobs=true; returns the validated .gguf files pinned to the commit
    the API reported. Blocking: call from a worker thread. Raises HfError."""
    repo_id = parse_repo(repo)
    t = _timeout_value(timeout)
    left = _deadline_fn(time.monotonic() + LIST_DEADLINE_S, "Hugging Face did not answer in time. Try again later.")
    owner, name = repo_id.split("/")
    url = f"{_API_BASE}/api/models/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}" \
          "?blobs=true"
    try:
        resp, _ = _walk(url, _headers("application/json"), t, left, what="repo", same_origin=True)
        with resp:
            if resp.status != 200:
                raise HfError("network", f"Hugging Face answered with an unexpected status (HTTP {resp.status}).")
            cl = resp.headers.get("Content-Length")
            if cl is not None and _INT_RE.fullmatch(cl.strip()) and int(cl) > MAX_API_BYTES:
                raise HfError("network", "Hugging Face sent an unexpectedly large answer.")
            buf = bytearray()
            while True:
                _set_sock_timeout(resp, min(t, left()))
                chunk = _read_some(resp, 64 * 1024)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_API_BYTES:
                    raise HfError("network", "Hugging Face sent an unexpectedly large answer.")
    except HfError:
        raise
    except Exception as e:  # noqa: BLE001  URLError, OSError, http.client.HTTPException, ...
        left()
        raise _net_error(e, "repo") from None
    try:
        data = json.loads(bytes(buf).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HfError("network", "Hugging Face sent an answer Agent Start could not read.") from None
    listing = _parse_listing(data)
    log.info("hf: listed %s at %s: %d .gguf entries", listing.repo_id, listing.commit[:12], len(listing.files))
    return listing


def _timeout_value(timeout: object) -> float:
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) \
            or timeout <= 0:
        raise HfError("refused", "Invalid timeout.")
    return float(timeout)


def _parse_listing(data: object) -> RepoListing:
    bad = HfError("network", "Hugging Face sent an answer Agent Start could not read.")
    if not isinstance(data, dict):
        raise bad
    gated = data.get("gated", False)
    if gated is not False and gated is not None:
        raise HfError("gated")
    if data.get("private") is True:
        raise HfError("not_found")
    if data.get("disabled") is True:
        raise HfError("not_found", "This repo has been disabled on Hugging Face.")
    rid = data.get("id")
    commit = data.get("sha")
    siblings = data.get("siblings")
    if not isinstance(rid, str) or not _valid_repo_id(rid) or not isinstance(commit, str) \
            or not isinstance(siblings, list):
        raise bad
    commit = commit.lower()
    if not _COMMIT_RE.fullmatch(commit):
        raise bad
    singles: dict[str, HfFile] = {}
    groups: dict[tuple[str, str, int], dict[int, tuple[str, str, int, str]]] = {}
    for s in siblings[:MAX_SIBLINGS]:
        entry = _sibling(s)
        if entry is None:
            continue
        path, size, sha = entry
        name = path.rsplit("/", 1)[-1]
        m = _SPLIT_RE.fullmatch(name)
        if m:
            folder = path[:-len(name)]
            key = (folder.lower(), m.group("stem").lower(), int(m.group("n")))
            groups.setdefault(key, {}).setdefault(int(m.group("i")), (path, name, size, sha))
            continue
        if path not in singles:
            kind = "vision" if "mmproj" in name.lower() else "model"
            singles[path] = HfFile(path=path, name=name, size=size, sha256=sha, kind=kind, supported=True, note="")
    files = list(singles.values())
    for (_, _, n), parts in groups.items():
        first = parts[min(parts)]
        total = sum(p[2] for p in parts.values())
        note = f"Split into {n} parts ({len(parts)} listed); split models are not supported."
        files.append(HfFile(path=first[0], name=first[1], size=total, sha256=first[3], kind="split",
                            supported=False, note=note))
    if not files:
        raise HfError("no_gguf")
    files.sort(key=lambda f: (not f.supported, f.name.casefold(), f.path))
    return RepoListing(repo_id=rid, commit=commit, files=tuple(files))


def _sibling(s: object) -> tuple[str, int, str] | None:
    """(path, size, sha256) for a usable .gguf sibling, else None."""
    if not isinstance(s, dict):
        return None
    path = s.get("rfilename")
    if not _valid_gguf_path(path):
        if isinstance(path, str) and path.lower().endswith(".gguf"):
            log.debug("hf: skipped unsafe path %r", path[:120])
        return None
    lfs = s.get("lfs")
    if not isinstance(lfs, dict):
        return None
    sha = lfs.get("sha256")
    size = lfs.get("size")
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha.lower()):
        return None
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_FILE_BYTES:
        return None
    top = s.get("size")
    if top is not None and (isinstance(top, bool) or not isinstance(top, int) or top != size):
        return None
    if not isinstance(path, str):
        return None
    return path, size, sha.lower()


# ------------------------------------------------------------------------------------------- free space

def free_space(dest_dir: str) -> int | None:
    """Free bytes on the drive holding dest_dir, or None when it cannot be read."""
    try:
        return int(shutil.disk_usage(dest_dir).free)
    except (OSError, ValueError, TypeError):
        return None


def space_ok(dest_dir: str, size: int, margin_bytes: int = SPACE_MARGIN_BYTES) -> bool:
    """True when dest_dir's drive has room for `size` bytes plus the margin. Unknown free space -> False."""
    if not _is_int(size) or size < 0 or not _is_int(margin_bytes) or margin_bytes < 0:
        return False
    free = free_space(dest_dir)
    return free is not None and free >= size + margin_bytes


# ------------------------------------------------------------------------------------------- files on disk

def _check_file(file: object) -> HfFile:
    if not isinstance(file, HfFile):
        raise HfError("refused", "Invalid file selection.")
    if not _valid_gguf_path(file.path) or file.name != file.path.rsplit("/", 1)[-1]:
        raise HfError("refused", "This file name is not safe to save.")
    if not isinstance(file.sha256, str) or not _SHA_RE.fullmatch(file.sha256):
        raise HfError("refused", "This file has no valid checksum.")
    if not _is_int(file.size) or file.size <= 0:
        raise HfError("refused", "This file has an invalid size.")
    return file


def _check_inputs(listing: object, file: object) -> tuple[RepoListing, HfFile]:
    f = _check_file(file)
    if not isinstance(listing, RepoListing) or not _valid_repo_id(listing.repo_id) \
            or not isinstance(listing.commit, str) or not _COMMIT_RE.fullmatch(listing.commit) \
            or not isinstance(listing.files, tuple) or f not in listing.files:
        raise HfError("refused", "Invalid file selection. Refresh the list and try again.")
    if f.kind == "split" or not f.supported:
        raise HfError("refused", f.note or "This file cannot be downloaded by Agent Start.")
    if f.size > MAX_FILE_BYTES:
        raise HfError("refused", "This file has an invalid size.")
    return listing, f


def _targets(dest_dir: object, file: HfFile) -> tuple[str, str, str, str]:
    """(dest_real, final, part, sidecar) - all directly inside dest_dir after realpath."""
    if not isinstance(dest_dir, str) or not dest_dir or not is_local_fixed_path(dest_dir):
        raise HfError("refused", "Models can only be saved to a folder on a local drive of this PC.")
    try:
        dest_real = os.path.realpath(dest_dir)
    except (OSError, ValueError):
        raise HfError("disk", "The models folder could not be found.") from None
    if not is_local_fixed_path(dest_real):
        raise HfError("refused", "Models can only be saved to a folder on a local drive of this PC.")
    if not os.path.isdir(dest_real):
        raise HfError("disk", "The models folder does not exist.")
    final = os.path.join(dest_real, file.name)
    part = final + ".part"
    side = part + ".json"
    want = os.path.normcase(dest_real)
    for p in (final, part, side):
        try:
            inside = os.path.normcase(os.path.dirname(os.path.realpath(p))) == want
        except (OSError, ValueError):
            inside = False
        if not inside:
            raise HfError("refused", "Refusing to write outside the models folder.")
    return dest_real, final, part, side


def _plain_size(p: str) -> int | None:
    """Size of a regular file at p, None when absent; HfError('disk') when something else is in the way."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None
    except OSError:
        raise HfError("disk", f"Could not read {os.path.basename(p)} in the models folder.") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) \
            or getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise HfError("disk", f"{os.path.basename(p)} in the models folder is not a plain file; remove it first.")
    return st.st_size


def _unlink(p: str) -> None:
    try:
        if _plain_size(p) is None:
            return
        os.remove(p)
    except FileNotFoundError:
        pass
    except (OSError, HfError):
        log.debug("hf: could not remove %s", os.path.basename(p), exc_info=True)


def _discard(part: str, side: str) -> None:
    _unlink(part)
    _unlink(side)


def _sidecar_expected(listing: RepoListing, file: HfFile) -> dict[str, object]:
    return {"app": SIDECAR_APP, "repo": listing.repo_id, "commit": listing.commit, "path": file.path,
            "size": file.size, "sha256": file.sha256}


def _ours(side: str) -> bool:
    """True when <name>.part.json is a note this app wrote. Only then may <name>.part be deleted or replaced:
    other programs (browsers) also name their unfinished downloads NAME.part."""
    try:
        n = _plain_size(side)
        if n is None or n > MAX_SIDECAR_BYTES:
            return False
        with open(side, "rb") as fh:
            d = json.loads(fh.read(MAX_SIDECAR_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError, HfError):
        return False
    return isinstance(d, dict) and d.get("app") == SIDECAR_APP


def _clear_stale(part: str, side: str) -> None:
    """Make room for a fresh <name>.part: an old part of this app's is deleted; anything else by that name is left
    alone and the download refused (HfError 'exists')."""
    if not os.path.lexists(part) and not os.path.lexists(side):
        return
    if not _ours(side):
        which = part if os.path.lexists(part) else side
        raise HfError("exists", f"{os.path.basename(which)} is already in the models folder and was not made by "
                                "Agent Start (another program's download?). It was left alone; move or delete "
                                "it first.")
    _discard(part, side)
    if os.path.lexists(part) or os.path.lexists(side):
        raise HfError("disk", "An old partial download could not be deleted.")


def _sidecar_matches(side: str, listing: RepoListing, file: HfFile) -> bool:
    try:
        n = _plain_size(side)
        if n is None or n > MAX_SIDECAR_BYTES:
            return False
        with open(side, "rb") as fh:
            d = json.loads(fh.read(MAX_SIDECAR_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError, HfError):
        return False
    if not isinstance(d, dict) or not _is_int(d.get("size")):
        return False
    return d == _sidecar_expected(listing, file)


def _resumable(part: str, side: str, listing: RepoListing, file: HfFile) -> int:
    n = _plain_size(part)
    if n is None or not 0 < n < file.size or not _sidecar_matches(side, listing, file):
        return 0
    return n


def pending_part(dest_dir: str, file: HfFile, listing: RepoListing) -> int:
    """Bytes of a kept, resumable <name>.part for this exact file and commit (0 when none). Never raises."""
    try:
        listing, f = _check_inputs(listing, file)
        _, _, part, side = _targets(dest_dir, f)
        return _resumable(part, side, listing, f)
    except Exception:  # noqa: BLE001
        return 0


def discard_part(dest_dir: str, file: HfFile) -> None:
    """Remove this app's <name>.part and <name>.part.json from dest_dir (regular files only; nothing when there
    is none). Raises HfError('busy') while a download runs, HfError('exists') for a NAME.part this app did not
    make (left alone), HfError('refused'/'disk') for an unusable folder."""
    f = _check_file(file)
    if not _download_lock.acquire(blocking=False):
        raise HfError("busy")
    try:
        _, _, part, side = _targets(dest_dir, f)
        _clear_stale(part, side)
    finally:
        _download_lock.release()


# ------------------------------------------------------------------------------------------- download

class _Progress:
    def __init__(self, cb: Callable[[int, int], None] | None, total: int) -> None:
        self.cb = cb
        self.total = total
        self.last = -1e9

    def __call__(self, done: int, force: bool = False) -> None:
        if self.cb is None:
            return
        now = time.monotonic()
        if not force and now - self.last < PROGRESS_INTERVAL_S:
            return
        self.last = now
        try:
            self.cb(done, self.total)
        except Exception:  # noqa: BLE001
            log.debug("hf: progress callback raised", exc_info=True)


def _disk_error(what: str, e: OSError) -> HfError:
    log.debug("hf: disk error (%s): %s", what, e)
    if getattr(e, "errno", None) == 28 or getattr(e, "winerror", None) in (39, 112):
        return HfError("disk", "The drive is full." + _KEPT)
    return HfError("disk", f"Could not {what} in the models folder ({e.strerror or type(e).__name__}).")


def download(listing: RepoListing, file: HfFile, dest_dir: str, *,
             progress: Callable[[int, int], None] | None = None, stop_evt: threading.Event | None = None,
             timeout: float = 30.0, deadline_s: float = 6 * 3600.0) -> str:
    """Download `file` of `listing` (pinned to listing.commit) into dest_dir and return the final path.
    Blocking: call from a worker thread. progress(done_bytes, total_bytes) is called from that thread at most ~10
    times a second (also while a kept .part is re-hashed). Setting stop_evt cancels between chunks. Raises HfError;
    on 'cancelled', 'timeout' and network errors the .part and its sidecar are kept for a resume, on a size or
    sha256 mismatch they are deleted."""
    if not _download_lock.acquire(blocking=False):
        raise HfError("busy")
    try:
        return _download(listing, file, dest_dir, progress, stop_evt, timeout, deadline_s)
    except HfError as e:
        log.info("hf download stopped (%s): %s", e.kind, e.message)
        raise
    except Exception:  # noqa: BLE001
        log.warning("hf download failed unexpectedly", exc_info=True)
        raise HfError("network", "The download failed unexpectedly." + _KEPT) from None
    finally:
        _download_lock.release()


def _download(listing: RepoListing, file: HfFile, dest_dir: str, progress: Callable[[int, int], None] | None,
              stop_evt: threading.Event | None, timeout: float, deadline_s: float) -> str:
    listing, file = _check_inputs(listing, file)
    t = _timeout_value(timeout)
    d = _timeout_value(deadline_s)
    left = _deadline_fn(time.monotonic() + d, "The download took too long and was stopped." + _KEPT)
    dest_real, final, part, side = _targets(dest_dir, file)
    if os.path.lexists(final):
        raise HfError("exists", f"{file.name} already exists in the models folder. Agent Start never overwrites "
                                "a file; delete or rename it first.")
    _stop_check(stop_evt)
    offset = _resumable(part, side, listing, file)
    if offset == 0:
        _clear_stale(part, side)
    if not space_ok(dest_real, file.size - offset):
        free = free_space(dest_real)
        if free is None:
            raise HfError("disk", "Could not check the free space on the models drive.")
        need = file.size - offset + SPACE_MARGIN_BYTES
        raise HfError("disk", f"Not enough free space for this model. It needs {_gib(need)} free (including a "
                              f"1 GB margin), the drive has {_gib(free)}.")
    owner, name = listing.repo_id.split("/")
    url = (f"{_RESOLVE_BASE}/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
           f"/resolve/{listing.commit}/{_quote_path(file.path)}")
    log.info("hf download: %s/%s @ %s -> %s (%d bytes, resume at %d)", listing.repo_id, file.path,
             listing.commit[:12], dest_real, file.size, offset)
    job = _Job(listing, file, part, side, url, t, left, stop_evt, _Progress(progress, file.size))
    done, digest = job.run(offset)
    if done != file.size:
        _discard(part, side)
        raise HfError("mismatch", "The downloaded file has the wrong size and was deleted. Try again.")
    if digest != file.sha256:
        _discard(part, side)
        log.warning("hf download: sha256 mismatch for %s", file.name)
        raise HfError("mismatch", "The downloaded file did not match the checksum published on Hugging Face and "
                                  "was deleted. Try again.")
    if os.path.lexists(final):
        raise HfError("exists", f"{file.name} appeared in the models folder during the download; it was not "
                                "overwritten.")
    try:
        os.rename(part, final)       # never replaces an existing file on Windows
    except FileExistsError:
        raise HfError("exists", f"{file.name} already exists in the models folder; it was not overwritten.") \
            from None
    except OSError as e:
        raise _disk_error("finish the file", e) from None
    _unlink(side)
    job.rep(done, force=True)
    log.info("hf download: %s saved (%d bytes, sha256 verified)", final, done)
    return final


def _gib(n: int | None) -> str:
    return "?" if n is None else f"{n / 1024 ** 3:.1f} GB"


class _Job:
    """One download attempt sequence (resume, or from zero). run() returns (bytes, sha256 hex)."""

    def __init__(self, listing: RepoListing, file: HfFile, part: str, side: str, url: str, timeout: float,
                 left: Callable[[], float], stop_evt: threading.Event | None, rep: _Progress) -> None:
        self.listing, self.file, self.part, self.side, self.url = listing, file, part, side, url
        self.timeout, self.left, self.stop_evt, self.rep = timeout, left, stop_evt, rep
        self.fd: int | None = None

    def _close(self) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except OSError:
                log.debug("hf: close failed", exc_info=True)

    def _restart(self) -> None:
        self._close()
        _discard(self.part, self.side)
        if _plain_size(self.part) is not None:
            raise HfError("disk", "An old partial download could not be deleted.")

    def run(self, offset: int) -> tuple[int, str]:
        try:
            return self._run(offset)
        except _BadData:
            self._close()
            _discard(self.part, self.side)
            raise
        finally:
            self._close()

    def _run(self, offset: int) -> tuple[int, str]:
        flags = getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        hasher: Any = hashlib.sha256()
        if offset:
            try:
                self.fd = os.open(self.part, os.O_RDWR | flags)
            except OSError as e:
                raise _disk_error("open the partial download", e) from None
            try:
                hasher = self._rehash(offset)
            except _Restart:
                self._restart()
                offset, hasher = 0, hashlib.sha256()
        resp = None
        for _attempt in range(2):
            try:
                resp = self._request(offset)
                break
            except _Restart:
                log.info("hf download: resume not accepted by the server; starting from zero")
                self._restart()
                offset, hasher = 0, hashlib.sha256()
        if resp is None:
            raise HfError("network", "The download could not be started.")
        with resp:
            if offset and resp.status == 200:
                log.info("hf download: server ignored the range request; starting from zero")
                self._restart()
                offset, hasher = 0, hashlib.sha256()
            if self.fd is None:
                self._create()
            else:
                try:
                    os.lseek(self.fd, offset, os.SEEK_SET)
                    os.ftruncate(self.fd, offset)
                except OSError as e:
                    raise _disk_error("continue the partial download", e) from None
            done = self._stream(resp, offset, hasher)
        assert self.fd is not None
        try:
            os.fsync(self.fd)
        except OSError as e:
            raise _disk_error("write the file", e) from None
        self._close()
        return done, hasher.hexdigest()

    def _rehash(self, length: int) -> Any:
        assert self.fd is not None
        h = hashlib.sha256()
        os.lseek(self.fd, 0, os.SEEK_SET)
        done = 0
        while done < length:
            _stop_check(self.stop_evt)
            self.left()
            try:
                b = os.read(self.fd, min(HASH_CHUNK, length - done))
            except OSError as e:
                raise _disk_error("read the partial download", e) from None
            if not b:
                raise _Restart()
            h.update(b)
            done += len(b)
            self.rep(done)
        return h

    def _request(self, offset: int) -> Any:
        headers = _headers("*/*")
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            resp, _ = _walk(self.url, headers, self.timeout, self.left, what="file", same_origin=False,
                            ranged=bool(offset), check_hub=self._check_hub)
        except (HfError, _Restart):
            raise
        except Exception as e:  # noqa: BLE001  URLError, OSError, http.client.HTTPException, ...
            self.left()
            raise _net_error(e, "file") from None
        try:
            self._check_response(resp, offset)
        except BaseException:
            resp.close()
            raise
        return resp

    def _check_hub(self, hub: Any) -> None:
        """Headers of the huggingface.co response, checked before the download server is contacted."""
        f, size = self.file, self.file.size
        stale = HfError("mismatch", "Hugging Face reports a different version of this file than the list shows. "
                                    "Refresh the list and try again.")
        if hub is None:
            raise stale
        commit = (hub.get("X-Repo-Commit") or "").strip().lower()
        if commit != self.listing.commit:
            log.warning("hf download: X-Repo-Commit %r != listing %s", commit[:64], self.listing.commit)
            raise stale
        ls = hub.get("X-Linked-Size")
        if ls is not None and (not _INT_RE.fullmatch(ls.strip()) or int(ls.strip()) != size):
            log.warning("hf download: X-Linked-Size %r != %d", ls[:32], size)
            raise stale
        le = hub.get("X-Linked-ETag")
        if le is not None and le.strip().strip('"').lower() != f.sha256:
            log.warning("hf download: X-Linked-ETag %r != listed sha256", le[:80])
            raise stale

    def _check_response(self, resp: Any, offset: int) -> None:
        size = self.file.size
        status = resp.status
        if offset and status == 206:
            m = _CONTENT_RANGE_RE.fullmatch((resp.headers.get("Content-Range") or "").strip())
            if not m or (int(m.group(1)), int(m.group(2)), int(m.group(3))) != (offset, size - 1, size):
                raise _Restart()
            expect = size - offset
        elif status == 200:
            expect = size
        else:
            raise HfError("network", f"The download server answered with an unexpected status (HTTP {status}).")
        cl = resp.headers.get("Content-Length")
        if cl is not None and (not _INT_RE.fullmatch(cl.strip()) or int(cl.strip()) != expect):
            log.warning("hf download: Content-Length %r != expected %d", cl[:32], expect)
            raise HfError("mismatch", "The download server reports a different file size than the list shows. "
                                      "Refresh the list and try again.")

    def _create(self) -> None:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOINHERIT", 0))
        try:                                     # the note first: a part never exists without it
            with open(self.side, "x", encoding="utf-8") as fh:
                json.dump(_sidecar_expected(self.listing, self.file), fh)
        except FileExistsError:
            raise HfError("disk", "The partial download file is in use.") from None
        except OSError as e:
            raise _disk_error("write the download notes", e) from None
        try:
            self.fd = os.open(self.part, flags, 0o600)
        except FileExistsError:                  # not ours: it appeared after the check; only the note goes
            _unlink(self.side)
            raise HfError("disk", "The partial download file is in use.") from None
        except OSError as e:
            _unlink(self.side)
            raise _disk_error("create the file", e) from None

    def _stream(self, resp: Any, offset: int, hasher: Any) -> int:
        assert self.fd is not None
        size = self.file.size
        done = offset
        self.rep(done)
        while True:
            _stop_check(self.stop_evt)
            _set_sock_timeout(resp, min(self.timeout, self.left()))
            try:
                chunk = _read_some(resp, NET_CHUNK)
            except Exception as e:  # noqa: BLE001  socket timeout, IncompleteRead, reset...
                self.left()
                raise _net_error(e, "file") from None
            if not chunk:
                break
            if done + len(chunk) > size:
                raise _BadData("mismatch", "The download server sent more data than the file size; the partial "
                                           "file was deleted. Try again.")
            mv = memoryview(chunk)
            try:
                while mv:
                    n = os.write(self.fd, mv)
                    mv = mv[n:]
            except OSError as e:
                raise _disk_error("write the file", e) from None
            hasher.update(chunk)
            done += len(chunk)
            self.rep(done)
        _stop_check(self.stop_evt)
        self.left()
        if done < size:
            raise HfError("network", "The connection ended before the whole file arrived." + _KEPT)
        return done
