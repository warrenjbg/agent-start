"""In-app updater: check GitHub Releases for a newer Agent Start, download the installer safely, launch it.

Stdlib only. Policy:
  * HTTPS only, default port only. Plain http is allowed solely for host 127.0.0.1 (any port) and only
    while the module flag _ALLOW_LOCAL_HTTP_FOR_TESTS is True (the self-test flips it; False in the app).
  * The release asset URL must be EXACTLY https://github.com/<REPO>/releases/download/<tag>/<asset_name>,
    and the release must publish a sha256 digest for it. No digest -> no update is offered.
  * Downloads (and every redirect hop) must stay on an allow-listed host, finish within an overall
    deadline, and are only written inside dest_dir as <asset_name>.part (opened exclusively), then renamed
    after size + sha256 have been verified. One download at a time per process.
  * launch_installer() opens the installer with a share mode that denies writes/deletes, re-hashes it
    through that handle and starts it while the handle is still held (no verify -> swap -> run window).

check() / check_latest() / cleanup_old() never raise; download() / launch_installer() raise UpdateError on
any refusal or failure.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import math
import os
import re
import ssl
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("agent_start")

REPO = "warrenjbg/agent-start"
CHECK_INTERVAL_S = 24 * 3600
RETRY_AFTER_ERROR_S = 3600
LATEST_API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DEFAULT_ALLOWED_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                                   "release-assets.githubusercontent.com"})
API_HOSTS = frozenset({"api.github.com"})
CHUNK = 64 * 1024
MAX_API_BYTES = 2 * 1024 * 1024
MAX_INSTALLER_BYTES = 500 * 1024 * 1024
CHECK_DEADLINE_S = 60.0          # overall cap for one API check (connect + body), on top of per-op timeout

# Test hooks only. Never changed by the app.
_ALLOW_LOCAL_HTTP_FOR_TESTS = False
_DOWNLOAD_BASE = "https://github.com"      # expected asset URL = f"{_DOWNLOAD_BASE}/{REPO}/releases/download/..."

# Indirection so tests can replace process creation with a recorder.
_popen = subprocess.Popen

_VERSION_RE = re.compile(r"^\s*[vV]?(\d{1,6}(?:\.\d{1,6}){0,3})\s*$")
_TAG_RE = re.compile(r"^[vV]?\d{1,6}(?:\.\d{1,6}){0,3}\Z")
_ASSET_RE = re.compile(r"^AgentStart-Setup-(\d{1,6}(?:\.\d{1,6}){0,3})\.exe\Z")
_PART_RE = re.compile(r"^AgentStart-Setup-\d{1,6}(?:\.\d{1,6}){0,3}\.exe\.part\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{64}\Z")

DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
INSTALLER_ARGS = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"]

_download_lock = threading.Lock()


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str
    tag: str
    asset_name: str
    asset_url: str
    size: int
    sha256: str          # lower-case hex; required (download/launch refuse anything else)
    page_url: str


# ------------------------------------------------------------------------------------------- versions

def parse_version(s: str) -> tuple[int, ...]:
    """'v1.10.0' -> (1, 10, 0). Raises ValueError for anything that is not 1-4 dot-separated integers."""
    m = _VERSION_RE.match(s if isinstance(s, str) else "")
    if not m:
        raise ValueError(f"not a version: {s!r}")
    return tuple(int(p) for p in m.group(1).split("."))


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def is_newer(a: str, b: str) -> bool:
    """True when version a is strictly newer than b. Unparseable input on either side -> False."""
    try:
        return _cmp(parse_version(a), parse_version(b)) > 0
    except (ValueError, TypeError):
        return False


def _finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def due(last_check_epoch: float | None, now: float, interval_s: float = CHECK_INTERVAL_S) -> bool:
    """True when a check is due: never checked, last is not a finite number, clock went backwards, or
    interval_s has elapsed."""
    if not _finite_number(last_check_epoch) or not _finite_number(now):
        return True
    if last_check_epoch > now:
        return True
    try:
        return now - last_check_epoch >= float(interval_s)
    except (TypeError, ValueError):
        return True


# ------------------------------------------------------------------------------------------- url policy

def _url_ok(url: str, allowed_hosts) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
        port = u.port
    except (ValueError, TypeError, AttributeError):
        return False
    if not host or u.username or u.password:
        return False
    if u.scheme == "https":
        if port not in (None, 443):
            return False
    elif u.scheme == "http" and host == "127.0.0.1" and _ALLOW_LOCAL_HTTP_FOR_TESTS:
        pass
    else:
        return False
    return host in {h.lower() for h in allowed_hosts}


def _expected_asset_url(tag, asset_name) -> str | None:
    if not (isinstance(tag, str) and _TAG_RE.match(tag) and isinstance(asset_name, str)
            and _ASSET_RE.match(asset_name)):
        return None
    return f"{_DOWNLOAD_BASE}/{REPO}/releases/download/{tag}/{asset_name}"


class _RedirectRefused(UpdateError):
    pass


def _opener(allowed_hosts) -> urllib.request.OpenerDirector:
    hosts = frozenset(allowed_hosts)

    class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not _url_ok(newurl, hosts):
                raise _RedirectRefused(f"redirect to disallowed URL refused: {urllib.parse.urlsplit(newurl).netloc}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    ctx = ssl.create_default_context()
    return urllib.request.build_opener(_GuardedRedirect(), urllib.request.HTTPSHandler(context=ctx))


def _headers(current: str, accept: str) -> dict:
    return {"User-Agent": f"AgentStart/{current}", "Accept": accept}


def _set_sock_timeout(resp, t: float) -> None:
    """Best effort: shrink the socket timeout so a single recv cannot outlive the overall deadline."""
    try:
        resp.fp.raw._sock.settimeout(max(0.05, t))
    except Exception:  # noqa: BLE001
        pass


def _read_some(resp, n: int) -> bytes:
    rd = getattr(resp, "read1", None)
    return rd(n) if rd is not None else resp.read(n)


# ------------------------------------------------------------------------------------------- check

def check(current: str, timeout: float = 8.0, api_url: str | None = None) -> tuple[str, UpdateInfo | None]:
    """One GitHub check. Returns (status, info):
      ('newer', UpdateInfo)  a newer release with a valid, digest-carrying installer asset exists;
      ('current', None)      the latest release is not newer than `current`;
      ('error', None)        anything else (network/HTTP/JSON trouble, odd release or asset, no digest...).
    Never raises."""
    try:
        return _check(current, timeout, api_url)
    except Exception as e:  # noqa: BLE001  never raises
        log.debug("update check failed: %s", e, exc_info=not isinstance(e, (UpdateError, urllib.error.URLError,
                                                                            OSError, ValueError)))
        return "error", None


def check_latest(current: str, timeout: float = 8.0, api_url: str | None = None) -> UpdateInfo | None:
    """Thin wrapper over check(): the UpdateInfo when status is 'newer', else None. Never raises."""
    status, info = check(current, timeout, api_url)
    return info if status == "newer" else None


def _err(msg: str, *a) -> tuple[str, None]:
    log.debug("update check: " + msg, *a)
    return "error", None


def _check(current: str, timeout: float, api_url: str | None) -> tuple[str, UpdateInfo | None]:
    try:
        cur_t = parse_version(current)
    except ValueError:
        return _err("unparseable current version %r", current)
    deadline = time.monotonic() + CHECK_DEADLINE_S
    url = api_url or LATEST_API_URL
    api_host = (urllib.parse.urlsplit(url).hostname or "").lower() if isinstance(url, str) else ""
    hosts = API_HOSTS | ({api_host} if api_url else set())
    if not _url_ok(url, hosts):
        return _err("refusing API URL %r", url)
    req = urllib.request.Request(url, headers={**_headers(current, "application/vnd.github+json"),
                                               "X-GitHub-Api-Version": "2022-11-28"})
    with _opener(hosts).open(req, timeout=timeout) as resp:
        if resp.status != 200:
            return _err("HTTP %s", resp.status)
        buf = bytearray()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return _err("timed out reading the response")
            _set_sock_timeout(resp, min(timeout, left))
            chunk = _read_some(resp, CHUNK)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_API_BYTES:
                return _err("response too large")
    data = json.loads(bytes(buf).decode("utf-8"))
    if not isinstance(data, dict):
        return _err("response is not an object")
    if data.get("draft") or data.get("prerelease"):
        return _err("latest is draft/prerelease")
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not _TAG_RE.match(tag):
        return _err("unparseable tag %r", tag)
    ver_t = parse_version(tag)
    version = ".".join(str(p) for p in ver_t)
    if _cmp(ver_t, cur_t) <= 0:
        log.debug("update check: %s is not newer than %s", tag, current)
        return "current", None
    want = f"AgentStart-Setup-{version}.exe"
    asset = None
    assets = data.get("assets")
    for a in assets if isinstance(assets, list) else []:
        if isinstance(a, dict) and a.get("name") == want:
            asset = a
            break
    if asset is None:
        return _err("release %s has no asset %s", tag, want)
    asset_url = asset.get("browser_download_url")
    expected = _expected_asset_url(tag, want)
    if not isinstance(asset_url, str) or expected is None or asset_url != expected:
        return _err("asset URL %r is not the expected %r", asset_url, expected)
    if not _url_ok(asset_url, DEFAULT_ALLOWED_HOSTS | ({"127.0.0.1"} if _ALLOW_LOCAL_HTTP_FOR_TESTS else set())):
        return _err("asset URL not allowed: %r", asset_url)
    size = asset.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_INSTALLER_BYTES:
        return _err("bad asset size %r", size)
    if asset.get("state", "uploaded") != "uploaded":
        return _err("asset state %r", asset.get("state"))
    digest = asset.get("digest")
    if not (isinstance(digest, str) and digest[:7].lower() == "sha256:"):
        return _err("release %s publishes no sha256 digest (%r)", tag, digest)
    sha = digest[7:].strip().lower()
    if not _SHA_RE.match(sha):
        return _err("malformed sha256 digest %r", digest)
    page = data.get("html_url")
    if not (isinstance(page, str) and page.startswith(f"https://github.com/{REPO}/")):
        page = f"https://github.com/{REPO}/releases/tag/{urllib.parse.quote(tag)}"
    return "newer", UpdateInfo(version=version, tag=tag, asset_name=want, asset_url=asset_url, size=size,
                               sha256=sha, page_url=page)


# ------------------------------------------------------------------------------------------- download

def default_download_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    d = os.path.join(base, "AgentStart", "updates")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_name(name) -> bool:
    return (isinstance(name, str) and "/" not in name and "\\" not in name and ".." not in name
            and ":" not in name and "\x00" not in name and bool(_ASSET_RE.match(name)))


def _unlink(p: str) -> None:
    try:
        os.remove(p)
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("could not remove %s", p, exc_info=True)


def download(info: UpdateInfo, dest_dir: str, progress=None, stop_evt=None, timeout: float = 30.0,
             allowed_hosts=None, deadline_s: float = 900.0) -> str:
    """Stream info.asset_url to <dest_dir>/<asset_name>.part, verify size and sha256 (both required),
    rename to <dest_dir>/<asset_name> and return that path. Every failure raises UpdateError: 'cancelled'
    (exactly) for stop_evt, 'timed out' (exactly) when deadline_s elapses, 'another download is running'
    when a download is already in progress in this process."""
    if not _download_lock.acquire(blocking=False):
        raise UpdateError("another download is running")
    try:
        return _download(info, dest_dir, progress, stop_evt, timeout, allowed_hosts, deadline_s)
    except UpdateError:
        raise
    except Exception as e:  # noqa: BLE001  OSError, http.client.HTTPException, anything else
        raise UpdateError(f"download failed: {type(e).__name__}: {e}") from None
    finally:
        _download_lock.release()


def _download(info, dest_dir, progress, stop_evt, timeout, allowed_hosts, deadline_s) -> str:
    deadline = time.monotonic() + float(deadline_s)
    hosts = frozenset(h.lower() for h in (allowed_hosts if allowed_hosts is not None else DEFAULT_ALLOWED_HOSTS))
    name = getattr(info, "asset_name", None)
    if not _safe_name(name):
        raise UpdateError(f"refusing unsafe asset name: {name!r}")
    size = getattr(info, "size", None)
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_INSTALLER_BYTES:
        raise UpdateError(f"bad expected size: {size!r}")
    want_sha = info.sha256.lower() if isinstance(info.sha256, str) else None
    if want_sha is None:
        raise UpdateError("refusing download without a published sha256")
    if not _SHA_RE.match(want_sha):
        raise UpdateError("malformed sha256")
    expected = _expected_asset_url(info.tag, name)
    if expected is None or info.asset_url != expected:
        raise UpdateError(f"refusing unexpected download URL: {info.asset_url!r}")
    if not _url_ok(info.asset_url, hosts):
        raise UpdateError(f"refusing download URL: {info.asset_url}")
    if stop_evt is not None and stop_evt.is_set():
        raise UpdateError("cancelled")

    dest_real = os.path.realpath(dest_dir)
    os.makedirs(dest_real, exist_ok=True)
    final = os.path.join(dest_real, name)
    part = final + ".part"
    if os.path.dirname(os.path.realpath(final)) != dest_real:
        raise UpdateError("refusing to write outside dest_dir")

    _unlink(part)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    try:
        fd = os.open(part, flags, 0o600)
    except FileExistsError:
        raise UpdateError("download file is in use (another download is running?)") from None
    ok = False
    try:
        with os.fdopen(fd, "wb") as f:
            done, digest = _stream(info, hosts, timeout, deadline, f, progress, stop_evt)
            f.flush()
            os.fsync(f.fileno())
        if done != size:
            raise UpdateError(f"size mismatch: got {done}, expected {size}")
        if digest != want_sha:
            raise UpdateError("sha256 mismatch")
        try:
            os.replace(part, final)
        except OSError as e:
            raise UpdateError(f"could not finalise download: {e}") from None
        ok = True
        log.info("update downloaded: %s (%d bytes, sha256 verified)", final, done)
        return final
    finally:
        if not ok:
            _unlink(part)


def _stream(info, hosts, timeout, deadline, f, progress, stop_evt) -> tuple[int, str]:
    def left() -> float:
        r = deadline - time.monotonic()
        if r <= 0:
            raise UpdateError("timed out")
        return r

    req = urllib.request.Request(info.asset_url, headers=_headers(_current_version(), "application/octet-stream"))
    try:
        resp = _opener(hosts).open(req, timeout=min(timeout, left()))
    except UpdateError:
        raise
    except urllib.error.HTTPError as e:
        raise UpdateError(f"download failed: HTTP {e.code}") from None
    except Exception as e:  # noqa: BLE001
        left()
        raise UpdateError(f"download failed: {type(e).__name__}: {e}") from None
    with resp:
        if resp.status != 200:
            raise UpdateError(f"download failed: HTTP {resp.status}")
        if not _url_ok(resp.geturl(), hosts):
            raise UpdateError("download ended on a disallowed URL")
        cl = resp.headers.get("Content-Length")
        if cl is not None and cl.strip().isdigit() and int(cl) != info.size:
            raise UpdateError(f"size mismatch: server reports {cl}, expected {info.size}")
        h = hashlib.sha256()
        done = 0
        while True:
            if stop_evt is not None and stop_evt.is_set():
                raise UpdateError("cancelled")
            _set_sock_timeout(resp, min(timeout, left()))
            try:
                chunk = _read_some(resp, CHUNK)
            except Exception as e:  # noqa: BLE001  socket timeout, IncompleteRead, reset...
                left()
                raise UpdateError(f"download failed: {type(e).__name__}: {e}") from None
            if not chunk:
                break
            done += len(chunk)
            if done > info.size:
                raise UpdateError(f"size mismatch: more than {info.size} bytes")
            f.write(chunk)
            h.update(chunk)
            if progress is not None:
                try:
                    progress(done, info.size)
                except Exception:  # noqa: BLE001
                    log.debug("update progress callback raised", exc_info=True)
        if stop_evt is not None and stop_evt.is_set():
            raise UpdateError("cancelled")
        left()
    return done, h.hexdigest()


def _current_version() -> str:
    try:
        from version import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return "unknown"


# ------------------------------------------------------------------------------------------- cleanup

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def cleanup_old(dest_dir: str, current_version: str) -> int:
    """Delete AgentStart-Setup-<v>.exe with v <= current_version and AgentStart-Setup-<v>.exe.part files
    directly inside dest_dir (no recursion, links/reparse points skipped, .part left alone while a download
    runs). Returns how many files were removed. Never raises."""
    removed = 0
    try:
        try:
            cur = parse_version(current_version)
        except (ValueError, TypeError):
            cur = None
        if os.path.islink(dest_dir) or (hasattr(os.path, "isjunction") and os.path.isjunction(dest_dir)):
            return 0
        busy = _download_lock.locked()
        with os.scandir(dest_dir) as it:
            entries = list(it)
        for e in entries:
            try:
                m = _ASSET_RE.match(e.name)
                if m:
                    if cur is None or _cmp(parse_version(m.group(1)), cur) > 0:
                        continue
                elif _PART_RE.match(e.name):
                    if busy:
                        continue
                else:
                    continue
                st = e.stat(follow_symlinks=False)
                if (not stat.S_ISREG(st.st_mode) or e.is_symlink()
                        or getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT):
                    continue
                os.remove(os.path.join(dest_dir, e.name))
                removed += 1
            except Exception:  # noqa: BLE001
                log.debug("cleanup_old: could not remove %s", e.name, exc_info=True)
    except Exception:  # noqa: BLE001
        log.debug("cleanup_old failed", exc_info=True)
    return removed


# ------------------------------------------------------------------------------------------- launch

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _k32():
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.CreateFileW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    return k


def _final_path(k, h) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    n = k.GetFinalPathNameByHandleW(h, buf, len(buf), 0)
    if n == 0 or n >= len(buf):
        raise UpdateError("could not resolve the installer path")
    s = buf.value
    if s.startswith("\\\\?\\UNC\\"):
        s = "\\\\" + s[8:]
    elif s.startswith("\\\\?\\"):
        s = s[4:]
    return s


def _check_cmd_path(label: str, v) -> str:
    if not isinstance(v, str) or not v or not os.path.isabs(v) or '"' in v or any(ord(c) < 32 for c in v):
        raise UpdateError(f"refusing {label}: {v!r}")
    v = os.path.normpath(v)
    if v.endswith(("\\", "/")) and " " in v:
        raise UpdateError(f"refusing {label}: {v!r}")
    return v


def launch_installer(path: str, sha256: str, dest_dir: str | None = None, install_dir: str | None = None,
                     log_path: str | None = None, *, wait_pid: int | None = None) -> None:
    """Start the downloaded installer (Inno Setup; /SILENT so progress and errors stay visible).
    Refuses anything that is not <dest_dir>/AgentStart-Setup-<ver>.exe, then opens it denying writes and
    deletes, re-hashes it through that handle against `sha256` (required), and starts it while still
    holding the handle. argv:
      [exe, /SILENT, /SUPPRESSMSGBOXES, /NORESTART, /RELAUNCH=1, (/WAITPID=n), (/DIR=install_dir), (/LOG=log_path)]"""
    want = sha256.lower() if isinstance(sha256, str) else None
    if want is None or not _SHA_RE.match(want):
        raise UpdateError("launch_installer needs the expected sha256")
    if wait_pid is not None and (isinstance(wait_pid, bool) or not isinstance(wait_pid, int) or wait_pid <= 0):
        raise UpdateError(f"bad wait_pid: {wait_pid!r}")
    extra = []
    if wait_pid is not None:
        extra.append(f"/WAITPID={wait_pid}")
    if install_dir is not None:
        extra.append(f"/DIR={_check_cmd_path('install_dir', install_dir)}")
    if log_path is not None:
        extra.append(f"/LOG={_check_cmd_path('log_path', log_path)}")
    try:
        d = os.path.realpath(dest_dir if dest_dir is not None else default_download_dir())
        p = os.path.realpath(path)
    except (OSError, ValueError, TypeError) as e:
        raise UpdateError(f"bad installer path: {e}") from None
    if os.path.dirname(p) != d:
        raise UpdateError(f"refusing installer outside {d}: {path}")
    if not _safe_name(os.path.basename(p)):
        raise UpdateError(f"refusing unexpected installer name: {os.path.basename(p)}")
    if not os.path.isfile(p):
        raise UpdateError(f"installer not found: {p}")

    k = _k32()
    h = k.CreateFileW(p, _GENERIC_READ, _FILE_SHARE_READ, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
    if h is None or h == _INVALID_HANDLE:
        raise UpdateError(f"could not open installer exclusively (error {ctypes.get_last_error()})")
    f = None
    try:
        if os.path.normcase(_final_path(k, h)) != os.path.normcase(p):
            raise UpdateError("installer path changed while opening it")
        import msvcrt
        fd = msvcrt.open_osfhandle(h, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        f = os.fdopen(fd, "rb", buffering=0)       # f now owns the handle
        hs = hashlib.sha256()
        buf = bytearray(1024 * 1024)
        mv = memoryview(buf)
        while True:
            n = f.readinto(buf)
            if not n:
                break
            hs.update(mv[:n])
        if hs.hexdigest() != want:
            raise UpdateError("installer sha256 mismatch; refusing to run it")
        argv = [p, *INSTALLER_ARGS, *extra]
        log.info("launching installer: %s", argv)
        try:
            _popen(argv, close_fds=True, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        except Exception as e:  # noqa: BLE001
            raise UpdateError(f"could not start installer: {e}") from None
    except UpdateError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"could not verify installer: {type(e).__name__}: {e}") from None
    finally:
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
        else:
            k.CloseHandle(h)
