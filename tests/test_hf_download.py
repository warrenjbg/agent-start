"""Self-check for hf_download.py. Run: .venv\\Scripts\\python.exe tests\\test_hf_download.py

Safe by construction:
  * No request ever goes to the real Hugging Face. Two local http.server instances on 127.0.0.1 with ephemeral
    ports play the hub (API + /resolve/) and the download CDN; hf_download._API_BASE / _RESOLVE_BASE point at the
    hub and _ALLOW_LOCAL_HTTP_FOR_TESTS is on only while those tests run (restored at the end).
  * Redirects to other hosts are refused by the module before any connection is made (that is what is tested).
  * Everything is written under one temp dir outside the repo that is removed at the end. No process is started.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hf_download as hf  # noqa: E402

logging.getLogger("agent_start").addHandler(logging.NullHandler())   # keep expected refusal warnings quiet
from hf_download import HfError, HfFile, RepoListing  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="agentstart-hfdl-test-"))
REPO = "acme/Tiny-Model-GGUF"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
COMMIT2 = "fedcba9876543210fedcba9876543210fedcba98"
PAYLOAD = random.Random(1234).randbytes(300_000)
SHA = hashlib.sha256(PAYLOAD).hexdigest()
NAME = "tiny-Q4_K_M.gguf"
DUMMY_TOKEN = "hf_dummy_token_must_never_be_sent"


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------------------------- local servers

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        srv = self.server
        srv.seen.append((self.path, dict(self.headers)))
        r = srv.routes.get(self.path)
        if r is None:
            self.send_error(404)
            return
        try:
            headers = dict(r.get("headers", {}))
            if "location" in r:
                self.send_response(r.get("status", 302))
                self.send_header("Location", r["location"])
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = r.get("body", b"")
            status = r.get("status", 200)
            rng = self.headers.get("Range")
            if rng and status == 200 and r.get("range", True):
                m = re.fullmatch(r"bytes=(\d+)-", rng)
                if r.get("range_status") == 416 or not m or int(m.group(1)) >= len(body):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(body)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = int(m.group(1)) + r.get("range_shift", 0)
                status = 206
                headers["Content-Range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
                body = body[start:]
            self.send_response(status)
            self.send_header("Content-Type", r.get("ctype", "application/octet-stream"))
            for k, v in headers.items():
                self.send_header(k, v)
            clen = r.get("clen", len(body))
            if clen is not None:
                self.send_header("Content-Length", str(clen))
            self.end_headers()
            if "short" in r:
                body = body[:r["short"]]
            step = r.get("chunk", 0)
            if step:
                for i in range(0, len(body), step):
                    self.wfile.write(body[i:i + step])
                    self.wfile.flush()
                    time.sleep(r.get("chunk_sleep", 0))
            else:
                self.wfile.write(body)
        except (ConnectionError, OSError):
            pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.routes: dict[str, dict] = {}
        self.seen: list[tuple[str, dict]] = []


def start_server() -> tuple[Server, str]:
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


HUB: Server
CDN: Server
HUB_BASE = ""
CDN_BASE = ""


# ----------------------------------------------------------------------------------------- helpers

def expect_err(kind: str, fn, *a, **kw) -> tuple[bool, str]:
    try:
        fn(*a, **kw)
    except HfError as e:
        ok = e.kind == kind and isinstance(e.message, str) and bool(e.message) and "Traceback" not in e.message
        return ok, f"{e.kind}: {e.message}"
    except Exception as e:  # noqa: BLE001
        return False, f"wrong exception {type(e).__name__}: {e}"
    return False, "did not raise"


def no_raise(fn, *a, **kw):
    try:
        return True, fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return False, e


_n = 0


def newdir(tag: str) -> str:
    global _n
    _n += 1
    d = TMP / f"m{_n:02d}-{tag}"
    d.mkdir()
    return str(d)


def only_allowed(case: str, d: str, name: str = NAME) -> None:
    extra = set(os.listdir(d)) - {name, name + ".part", name + ".part.json"}
    check(f"{case}: no other file in dest", not extra, repr(sorted(extra)))


def entries(d: str) -> set[str]:
    return set(os.listdir(d))


def make_file(path: str = NAME, payload: bytes = PAYLOAD, kind: str = "model") -> HfFile:
    return HfFile(path=path, name=path.rsplit("/", 1)[-1], size=len(payload),
                  sha256=hashlib.sha256(payload).hexdigest(), kind=kind, supported=True, note="")


def make_listing(f: HfFile, commit: str = COMMIT, repo: str = REPO) -> RepoListing:
    return RepoListing(repo_id=repo, commit=commit, files=(f,))


def resolve_path(repo: str, commit: str, path: str) -> str:
    return f"/{repo}/resolve/{commit}/" + "/".join(urllib.parse.quote(s, safe="") for s in path.split("/"))


_tok = 0


def serve_file(f: HfFile, payload: bytes = PAYLOAD, commit: str = COMMIT, repo: str = REPO, hub_headers=None,
               **cdn_opts) -> str:
    """Hub resolve route -> 302 to a fresh CDN path that serves payload. Returns the CDN path."""
    global _tok
    _tok += 1
    cdn_path = f"/cdn/xet-bridge/{_tok}?Expires=1&Signature=x"
    h = {"X-Repo-Commit": commit, "X-Linked-Size": str(f.size), "X-Linked-ETag": f'"{f.sha256}"'}
    if hub_headers is not None:
        h = hub_headers
    HUB.routes[resolve_path(repo, commit, f.path)] = {"location": CDN_BASE + cdn_path, "headers": h}
    CDN.routes[cdn_path] = {"body": payload, **cdn_opts}
    return cdn_path


def cdn_requests(cdn_path: str) -> list[dict]:
    return [h for p, h in CDN.seen if p == cdn_path]


def sha_of(p: str) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ----------------------------------------------------------------------------------------- parse_repo

def test_parse_repo():
    section("parse_repo")
    good = {
        "acme/Tiny-Model-GGUF": "acme/Tiny-Model-GGUF",
        "  acme/tiny.model_v2  ": "acme/tiny.model_v2",
        "a/b": "a/b",
        "https://huggingface.co/acme/Tiny-Model-GGUF": "acme/Tiny-Model-GGUF",
        "https://huggingface.co/acme/Tiny-Model-GGUF/": "acme/Tiny-Model-GGUF",
        "https://huggingface.co/acme/Tiny-Model-GGUF?library=gguf": "acme/Tiny-Model-GGUF",
        "https://huggingface.co/acme/Tiny-Model-GGUF#files": "acme/Tiny-Model-GGUF",
        "https://huggingface.co/acme/Tiny-Model-GGUF/tree/main": "acme/Tiny-Model-GGUF",
        "https://huggingface.co/acme/Tiny-Model-GGUF/tree/main/sub/dir": "acme/Tiny-Model-GGUF",
        "https://huggingface.co/acme/Tiny-Model-GGUF/blob/main/x-Q4_K_M.gguf": "acme/Tiny-Model-GGUF",
        "https://huggingface.co/acme/Tiny-Model-GGUF/resolve/main/x.gguf?download=true": "acme/Tiny-Model-GGUF",
        "huggingface.co/acme/Tiny-Model-GGUF": "acme/Tiny-Model-GGUF",
        "https://hf.co/acme/Tiny-Model-GGUF": "acme/Tiny-Model-GGUF",
        "hf.co/acme/Tiny-Model-GGUF/tree/main": "acme/Tiny-Model-GGUF",
        "https://www.huggingface.co/acme/Tiny-Model-GGUF": "acme/Tiny-Model-GGUF",
        "HTTPS://HuggingFace.co/acme/Tiny-Model-GGUF": "acme/Tiny-Model-GGUF",
        "http://huggingface.co/acme/m": "acme/m",
        "acme/x\n": "acme/x",
        "x" * 96 + "/" + "y" * 96: "x" * 96 + "/" + "y" * 96,
    }
    for text, want in good.items():
        ok, r = no_raise(hf.parse_repo, text)
        check(f"accepts {text[:70]!r}", ok and r == want, repr(r))
    bad = [
        "", "   ", "acme", "acme/", "/acme/x", "acme/x/y", "acme//x", "acme/x y", "ac me/x", "acme/'x'", 'acme/"x"',
        "acme/..", "../acme/x", "acme/a..b", "acme/a--b", "acme/x.git", "acme/X.GIT", "acme/-x", "acme/x-",
        "acme/.x", "acme/x.", "acme\\x", "acme/x\\y", "acme/x\ny", "acme/x\x00", "acme/x\u00e9", "acme/x;rm",
        "x" * 97 + "/y", "a/" + "b" * 97, "a/b" + "c" * 5000,
        "datasets/acme/x", "spaces/acme/x", "datasets/x", "spaces/x", "models/x", "api/models",
        "https://huggingface.co/datasets/acme/x", "https://huggingface.co/spaces/acme/x",
        "https://huggingface.co/api/models/acme/x", "https://huggingface.co/acme",
        "https://huggingface.co/", "https://huggingface.co", "https://huggingface.co/acme/x/discussions",
        "https://huggingface.co/acme/x/tree", "https://huggingface.co/acme/x/blob/main",
        "https://huggingface.co/acme/x/resolve/main", "https://user:pw@huggingface.co/acme/x",
        "https://user@huggingface.co/acme/x", "https://huggingface.co:443/acme/x", "https://huggingface.co:8443/acme/x",
        "https://evil.com/acme/x", "https://huggingface.co.evil.com/acme/x", "https://evilhuggingface.co/acme/x",
        "https://hf.co.evil.com/acme/x", "ftp://huggingface.co/acme/x", "file:///C:/acme/x",
        "https://huggingface.co/acme/x%2F..%2Fy", "https://huggingface.co/acme/..", "https://huggingface.co/../x/y",
        "https://huggingface.co\\@evil.com/acme/x", "https://huggingface.co/acme/x y",
        "https://huggingface.co/" + "a" * 3000 + "/b", None, 5, b"acme/x",
    ]
    for text in bad:
        ok, d = expect_err("bad_repo", hf.parse_repo, text)
        check(f"rejects {str(text)[:70]!r}", ok, d)


# ----------------------------------------------------------------------------------------- host policy

def test_host_policy():
    section("url_allowed (host policy, real host names, no network)")
    hf._ALLOW_LOCAL_HTTP_FOR_TESTS = False
    later = [
        ("https://huggingface.co/a/b/resolve/c/x.gguf", True),
        ("https://huggingface.co:443/a", True),
        ("https://HuggingFace.CO/a", True),
        ("https://us.aws.cdn.hf.co/xet-bridge-us/1/2?Expires=1", True),
        ("https://eu-west-3.aws.cdn.hf.co/x", True),
        ("https://cas-bridge.xethub.hf.co/x", True),
        ("https://cdn-lfs.hf.co/x", True),
        ("https://cas-server.xethub.hf.co/v1/reconstructions/x", False),
        ("https://transfer.xethub.hf.co/x", False),
        ("https://transfer.xethub-eu.hf.co/x", False),
        ("https://hf.co/x", False),
        ("https://evilhf.co/x", False),
        ("https://hf.co.evil.com/x", False),
        ("https://huggingface.co.evil.com/x", False),
        ("https://evilhuggingface.co/x", False),
        ("https://huggingface.co./x", False),
        ("https://us..cdn.hf.co/x", False),
        ("https://-x.hf.co/x", False),
        ("https://s3.us-east-1.amazonaws.com/x", False),
        ("http://huggingface.co/x", False),
        ("http://us.aws.cdn.hf.co/x", False),
        ("https://huggingface.co:8443/x", False),
        ("https://us.aws.cdn.hf.co:8443/x", False),
        ("https://huggingface.co:80/x", False),
        ("https://huggingface.co:abc/x", False),
        ("https://user:pw@huggingface.co/x", False),
        ("https://user@us.aws.cdn.hf.co/x", False),
        ("https://huggingface.co\\@evil.com/x", False),
        ("https://xn--hgging-xxa.hf.co/x", True),
        ("https://hüggingface.co/x", False),
        ("ftp://huggingface.co/x", False),
        ("//huggingface.co/x", False),
        ("https://huggingface.co/a b", False),
        ("http://127.0.0.1:5555/x", False),
        ("", False), (None, False),
    ]
    for url, want in later:
        check(f"later hop {url!r} -> {want}", hf.url_allowed(url) is want)
    first = [("https://huggingface.co/api/models/a/b", True), ("https://us.aws.cdn.hf.co/x", False),
             ("https://hf.co/a/b", False), ("https://cas-bridge.xethub.hf.co/x", False)]
    for url, want in first:
        check(f"first hop {url!r} -> {want}", hf.url_allowed(url, first_hop=True) is want)
    hf._ALLOW_LOCAL_HTTP_FOR_TESTS = True
    try:
        check("flag on: http://127.0.0.1:5555 ok", hf.url_allowed("http://127.0.0.1:5555/x", first_hop=True))
        check("flag on: http://localhost refused", not hf.url_allowed("http://localhost:5555/x"))
        check("flag on: http://127.0.0.2 refused", not hf.url_allowed("http://127.0.0.2:5555/x"))
        check("flag on: http://huggingface.co still refused", not hf.url_allowed("http://huggingface.co/x"))
    finally:
        hf._ALLOW_LOCAL_HTTP_FOR_TESTS = False
    check("test hooks default: flag False, bases are huggingface.co",
          hf._ALLOW_LOCAL_HTTP_FOR_TESTS is False and hf._API_BASE == "https://huggingface.co"
          and hf._RESOLVE_BASE == "https://huggingface.co")


# ----------------------------------------------------------------------------------------- list_files

def sib(path, size=1000, sha="ab" * 32, lfs=True, top=True):
    s = {"rfilename": path, "blobId": "0" * 40}
    if top:
        s["size"] = size
    if lfs:
        s["lfs"] = {"sha256": sha, "size": size, "pointerSize": 134}
    return s


def info(repo_id="acme/Tiny-Model-GGUF", siblings=(), sha=COMMIT, gated=False, **extra):
    return {"id": repo_id, "private": False, "gated": gated, "disabled": False, "sha": sha,
            "siblings": list(siblings), **extra}


def api_path(repo: str) -> str:
    return f"/api/models/{repo}?blobs=true"


def serve_api(repo: str, obj=None, **route) -> None:
    if obj is not None:
        route = {"body": json.dumps(obj).encode(), "ctype": "application/json", **route}
    HUB.routes[api_path(repo)] = route


def test_list_files():
    section("list_files (local hub)")
    sib_list = [
        sib(".gitattributes", 1700, lfs=False),
        sib("README.md", 5000, lfs=False),
        sib("tiny-Q8_0.gguf", 2000, "cd" * 32),
        sib("tiny-Q4_K_M.gguf", 1000, "AB" * 32),
        sib("mmproj-tiny-f16.gguf", 3000, "ef" * 32),
        sib("Big/Big-Q2_K-00002-of-00003.gguf", 200, "12" * 32),
        sib("Big/Big-Q2_K-00001-of-00003.gguf", 100, "11" * 32),
        sib("Big/Big-Q2_K-00003-of-00003.gguf", 300, "13" * 32),
        sib("config.json", 100, lfs=False),
        sib("../evil.gguf"), sib("sub/../../evil.gguf"), sib("/abs.gguf"), sib("C:/x.gguf"), sib("C:x.gguf"),
        sib("a\\b.gguf"), sib("sub//x.gguf"), sib("./x.gguf"), sib("con.gguf"), sib("sub/NUL.q4.gguf"),
        sib("x\x00.gguf"), sib("x\n.gguf"), sib("x\u202egguf.gguf"), sib("x /y.gguf"), sib("dot./y.gguf"),
        sib(".gguf"), sib("x?.gguf"), sib("x\".gguf"), sib("a" * 600 + ".gguf"), sib("x.gguf.exe"),
        sib("nosha.gguf", lfs=False),
        sib("badsha.gguf", sha="zz" * 32),
        sib("shortsha.gguf", sha="ab" * 31),
        sib("zero.gguf", size=0),
        sib("huge.gguf", size=hf.MAX_FILE_BYTES + 1),
        {"rfilename": "boolsize.gguf", "size": True, "lfs": {"sha256": "ab" * 32, "size": True}},
        {"rfilename": "strsize.gguf", "size": "1000", "lfs": {"sha256": "ab" * 32, "size": "1000"}},
        {"rfilename": "disagree.gguf", "size": 999, "lfs": {"sha256": "ab" * 32, "size": 1000}},
        {"rfilename": 123, "size": 1, "lfs": {"sha256": "ab" * 32, "size": 1}},
        "not a dict", None, ["x.gguf"],
    ]
    serve_api(REPO, info(siblings=sib_list, sha=COMMIT.upper()))
    hf._ALLOW_LOCAL_HTTP_FOR_TESTS = True
    HUB.seen.clear()
    ok, lst = no_raise(hf.list_files, REPO)
    check("normal repo lists", ok, repr(lst))
    if ok:
        check("repo_id as returned", lst.repo_id == REPO)
        check("commit lower-cased 40 hex", lst.commit == COMMIT, lst.commit)
        names = [f.name for f in lst.files]
        check("only valid gguf entries, supported first then by name",
              names == ["mmproj-tiny-f16.gguf", "tiny-Q4_K_M.gguf", "tiny-Q8_0.gguf", "Big-Q2_K-00001-of-00003.gguf"],
              repr(names))
        by = {f.name: f for f in lst.files}
        q4 = by.get("tiny-Q4_K_M.gguf")
        check("model entry fields", q4 == HfFile("tiny-Q4_K_M.gguf", "tiny-Q4_K_M.gguf", 1000, "ab" * 32, "model",
                                                 True, ""), repr(q4))
        mm = by.get("mmproj-tiny-f16.gguf")
        check("mmproj is kind vision and supported", mm is not None and mm.kind == "vision" and mm.supported)
        sp = by.get("Big-Q2_K-00001-of-00003.gguf")
        check("split grouped into one entry", sp is not None and sum(f.kind == "split" for f in lst.files) == 1)
        if sp is not None:
            check("split: path of part 1, size = sum, unsupported",
                  sp.path == "Big/Big-Q2_K-00001-of-00003.gguf" and sp.size == 600 and not sp.supported
                  and sp.sha256 == "11" * 32, repr(sp))
            check("split note says 3 parts and not supported", "3 parts" in sp.note and "not supported" in sp.note,
                  sp.note)
        check("files is a tuple of HfFile", isinstance(lst.files, tuple)
              and all(isinstance(f, HfFile) for f in lst.files))
    check("exactly one API request", len(HUB.seen) == 1, repr([p for p, _ in HUB.seen]))
    if HUB.seen:
        p, h = HUB.seen[0]
        check("request path is /api/models/<repo>?blobs=true", p == api_path(REPO), p)
        hl = {k.lower(): v for k, v in h.items()}
        check("User-Agent AgentStart/..., no Authorization/Cookie", hl.get("user-agent", "").startswith("AgentStart")
              and "authorization" not in hl and "cookie" not in hl, repr(hl))
        check("HF_TOKEN value never sent", DUMMY_TOKEN not in json.dumps(h))

    # pasted URL works too
    ok, lst = no_raise(hf.list_files, f"https://huggingface.co/{REPO}/blob/main/tiny-Q8_0.gguf")
    check("list_files accepts a pasted /blob/ URL", ok and lst.repo_id == REPO, repr(lst))

    # rename: relative same-host 307
    serve_api("Acme/old-name", location=api_path(REPO), status=307)
    ok, lst = no_raise(hf.list_files, "Acme/old-name")
    check("relative 307 on the hub followed, id as returned", ok and lst.repo_id == REPO, repr(lst))
    # redirect off the API origin is refused
    serve_api("acme/offsite", location=CDN_BASE + api_path(REPO), status=307)
    CDN.seen.clear()
    ok, d = expect_err("refused", hf.list_files, "acme/offsite")
    check("API redirect to another origin refused", ok and not CDN.seen, d)
    serve_api("acme/evil", location="https://evil.example.com/x", status=302)
    ok, d = expect_err("refused", hf.list_files, "acme/evil")
    check("API redirect to another host refused", ok, d)

    serve_api("acme/gated", info(repo_id="acme/gated", gated="manual", siblings=[sib("x.gguf")]))
    ok, d = expect_err("gated", hf.list_files, "acme/gated")
    check("gated 'manual' -> gated", ok and "login" in d and "does not support" in d, d)
    serve_api("acme/gated2", info(repo_id="acme/gated2", gated="auto", siblings=[sib("x.gguf")]))
    check("gated 'auto' -> gated", expect_err("gated", hf.list_files, "acme/gated2")[0])
    serve_api("acme/gated3", status=401, body=b"x", headers={"X-Error-Code": "GatedRepo"})
    check("401 + GatedRepo -> gated", expect_err("gated", hf.list_files, "acme/gated3")[0])
    serve_api("acme/missing", status=401, body=b'{"error":"Invalid username or password."}',
              headers={"WWW-Authenticate": 'Bearer realm="Authentication required"'})
    ok, d = expect_err("not_found", hf.list_files, "acme/missing")
    check("401 plain -> not_found, says does not exist or is private", ok and "does not exist or is private" in d, d)
    serve_api("acme/404", status=404, body=b"x")
    check("404 -> not_found", expect_err("not_found", hf.list_files, "acme/404")[0])
    serve_api("acme/disabled", info(repo_id="acme/disabled", disabled=True, siblings=[sib("x.gguf")]))
    check("disabled -> not_found", expect_err("not_found", hf.list_files, "acme/disabled")[0])
    serve_api("acme/rl", status=429, body=b"slow down", headers={"RateLimit": '"api";r=0;t=41',
                                                               "RateLimit-Policy": '"fixed window";"api";q=500;w=300'})
    try:
        hf.list_files("acme/rl")
        check("429 -> rate_limited", False, "no raise")
    except HfError as e:
        check("429 -> rate_limited with retry_after_s 41", e.kind == "rate_limited" and e.retry_after_s == 41
              and "41 seconds" in e.message, f"{e.kind} {e.retry_after_s} {e.message}")
    serve_api("acme/rl2", status=429, body=b"", headers={"Retry-After": "120"})
    try:
        hf.list_files("acme/rl2")
        check("429 Retry-After", False, "no raise")
    except HfError as e:
        check("429 with Retry-After: 120 -> retry_after_s 120", e.kind == "rate_limited" and e.retry_after_s == 120)
    serve_api("acme/rl3", status=429, body=b"")
    try:
        hf.list_files("acme/rl3")
    except HfError as e:
        check("429 without hints -> retry_after_s None", e.kind == "rate_limited" and e.retry_after_s is None)
    serve_api("acme/500", status=500, body=b"oops")
    check("500 -> network", expect_err("network", hf.list_files, "acme/500")[0])

    big = b'{"id":"acme/big","x":"' + b"a" * (hf.MAX_API_BYTES + 10) + b'"}'
    serve_api("acme/big", body=big, ctype="application/json")
    ok, d = expect_err("network", hf.list_files, "acme/big")
    check("oversized body (Content-Length) refused", ok and "large" in d, d)
    serve_api("acme/big2", body=big, ctype="application/json", clen=None)
    ok, d = expect_err("network", hf.list_files, "acme/big2")
    check("oversized body (streamed, no length) refused", ok and "large" in d, d)
    for i, junk in enumerate([b"{not json", b"\xff\xfe\x00", b"[1,2,3]", b'"str"', b"null",
                              json.dumps({"id": "acme/j", "siblings": []}).encode(),
                              json.dumps(info(repo_id="acme/j", sha="xyz")).encode(),
                              json.dumps(info(repo_id="../etc", siblings=[sib("x.gguf")])).encode(),
                              json.dumps({**info(repo_id="acme/j"), "siblings": {"x.gguf": 1}}).encode(),
                              b"[" * 100000 + b"]" * 100000]):
        serve_api(f"acme/junk{i}", body=junk, ctype="application/json")
        ok, d = expect_err("network", hf.list_files, f"acme/junk{i}")
        check(f"junk JSON #{i} -> network (could not read)", ok, d)
    serve_api("acme/nogguf", info(repo_id="acme/nogguf", siblings=[sib("README.md", lfs=False),
                                                                   sib("model.safetensors")]))
    ok, d = expect_err("no_gguf", hf.list_files, "acme/nogguf")
    check("no gguf -> no_gguf", ok, d)
    serve_api("acme/onlybad", info(repo_id="acme/onlybad", siblings=[sib("../x.gguf"), sib("y.gguf", lfs=False)]))
    check("only unusable gguf entries -> no_gguf", expect_err("no_gguf", hf.list_files, "acme/onlybad")[0])
    HUB.seen.clear()
    ok, d = expect_err("bad_repo", hf.list_files, "datasets/acme/x")
    check("bad input -> bad_repo, no request made", ok and not HUB.seen, d)

    # a request to a dead port: network error, plain message
    saved = hf._API_BASE
    srv = Server(("127.0.0.1", 0), Handler)
    dead = f"http://127.0.0.1:{srv.server_address[1]}"
    srv.server_close()
    hf._API_BASE = dead
    try:
        ok, d = expect_err("network", hf.list_files, REPO, timeout=3)
        check("connection refused -> network", ok, d)
    finally:
        hf._API_BASE = saved


# ----------------------------------------------------------------------------------------- download

def test_download_basic():
    section("download: happy path, refusals")
    f = make_file()
    lst = make_listing(f)
    cdn_path = serve_file(f)
    d = newdir("happy")
    calls: list[tuple[int, int]] = []
    HUB.seen.clear()
    ok, r = no_raise(hf.download, lst, f, d, progress=lambda a, b: calls.append((a, b)))
    check("happy path returns final path", ok and r == os.path.join(os.path.realpath(d), NAME), repr(r))
    check("final file sha256 right", ok and sha_of(r) == SHA)
    check("only the final file remains (part + sidecar gone)", entries(d) == {NAME}, repr(entries(d)))
    check("progress called, ends at (size, size)", bool(calls) and calls[-1] == (f.size, f.size), repr(calls[-3:]))
    check("progress monotonic, total constant", all(b == f.size for _, b in calls)
          and all(calls[i][0] <= calls[i + 1][0] for i in range(len(calls) - 1)))
    hub_paths = [p for p, _ in HUB.seen]
    check("resolve URL pinned to the commit", hub_paths == [resolve_path(REPO, COMMIT, NAME)], repr(hub_paths))
    reqs = cdn_requests(cdn_path)
    check("CDN contacted once without Range", len(reqs) == 1 and "Range" not in reqs[0], repr(reqs))
    check("no token / cookie sent", all(DUMMY_TOKEN not in json.dumps(h) and "Cookie" not in h
                                        and "Authorization" not in h for _, h in HUB.seen + CDN.seen))
    only_allowed("happy", d)

    ok, e = expect_err("exists", hf.download, lst, f, d)
    check("final already exists -> exists", ok, e)
    check("existing file untouched, no part created", entries(d) == {NAME} and sha_of(os.path.join(d, NAME)) == SHA)

    # path in a subfolder, odd characters quoted segment by segment
    f2 = make_file("Sub Dir/tiny #1+[x]%.gguf")
    lst2 = make_listing(f2)
    serve_file(f2)
    d2 = newdir("quoted")
    ok, r = no_raise(hf.download, lst2, f2, d2)
    check("path quoted segment by segment, saved under basename", ok and os.path.basename(r) == "tiny #1+[x]%.gguf"
          and sha_of(r) == SHA, repr(r))
    only_allowed("quoted", d2, f2.name)

    # renamed repo: relative same-host 307 then the 302 with the headers
    f3 = make_file("renamed.gguf")
    lst3 = make_listing(f3, repo="acme/Old-Name")
    serve_file(f3, repo="acme/New-Name")
    HUB.routes[resolve_path("acme/Old-Name", COMMIT, f3.path)] = {
        "status": 307, "location": resolve_path("acme/New-Name", COMMIT, f3.path)}
    d3 = newdir("rename")
    ok, r = no_raise(hf.download, lst3, f3, d3)
    check("relative 307 (renamed repo) then 302 -> ok", ok and sha_of(r) == SHA, repr(r))
    only_allowed("rename", d3, f3.name)

    # vision projector downloads
    fv = make_file("mmproj-tiny-f16.gguf", kind="vision")
    lv = make_listing(fv)
    serve_file(fv)
    dv = newdir("vision")
    ok, r = no_raise(hf.download, lv, fv, dv)
    check("mmproj (vision) downloads", ok and sha_of(r) == SHA, repr(r))

    # refusals before any request
    split = HfFile("Big/Big-00001-of-00002.gguf", "Big-00001-of-00002.gguf", 10, "ab" * 32, "split", False,
                   "Split into 2 parts; split models are not supported.")
    ds = newdir("split")
    ok, e = expect_err("refused", hf.download, RepoListing(REPO, COMMIT, (split,)), split, ds)
    check("split file refused", ok and "not supported" in e, e)
    other = make_file("other.gguf")
    ok, e = expect_err("refused", hf.download, lst, other, ds)
    check("file not in the listing refused", ok, e)
    forged = HfFile("../x.gguf", "x.gguf", 10, "ab" * 32, "model", True, "")
    ok, e = expect_err("refused", hf.download, RepoListing(REPO, COMMIT, (forged,)), forged, ds)
    check("forged unsafe path refused", ok, e)
    bad_commit = RepoListing(REPO, "main", (f,))
    check("non-hash commit refused", expect_err("refused", hf.download, bad_commit, f, ds)[0])
    check("nothing written by refusals", entries(ds) == set(), repr(entries(ds)))
    missing = str(TMP / "does-not-exist")
    ok, e = expect_err("disk", hf.download, lst, f, missing)
    check("dest_dir that does not exist -> disk", ok and not os.path.exists(missing), e)
    for unc in ("\\\\server\\share\\models", "//server/share/models", "\\\\?\\C:\\models", "relative\\models", ""):
        ok, e = expect_err("refused", hf.download, lst, f, unc)
        check(f"dest_dir {unc!r} refused", ok, e)
    check("a file as dest_dir -> disk", expect_err("disk", hf.download, lst, f, os.path.join(d, NAME))[0])

    saved = hf.free_space
    hf.free_space = lambda _d: 10
    try:
        dsp = newdir("space")
        ok, e = expect_err("disk", hf.download, lst, f, dsp)
        check("not enough free space -> disk, nothing written", ok and "free space" in e and entries(dsp) == set(), e)
    finally:
        hf.free_space = saved
    check("space_ok: tiny size ok", hf.space_ok(d, 10))
    check("space_ok: absurd size refused", not hf.space_ok(d, 10 ** 18))
    check("space_ok: bool / negative refused", not hf.space_ok(d, True) and not hf.space_ok(d, -1))
    check("free_space on missing dir -> None", hf.free_space(missing) is None)
    fs = hf.free_space(d)
    check("free_space int > 0", isinstance(fs, int) and fs > 0, repr(fs))


def test_download_policy():
    section("download: redirect and header policy")
    f = make_file()
    lst = make_listing(f)
    good_h = {"X-Repo-Commit": COMMIT, "X-Linked-Size": str(f.size), "X-Linked-ETag": f'"{SHA}"'}
    rp = resolve_path(REPO, COMMIT, NAME)
    for case, loc in (("other host", "https://evil.example.com/x"), ("localhost", CDN_BASE.replace("127.0.0.1",
                                                                                                   "localhost") + "/x"),
                      ("cas-server", "https://cas-server.xethub.hf.co/v1/x"),
                      ("userinfo", CDN_BASE.replace("http://", "http://u:p@") + "/x"),
                      ("https on test host", CDN_BASE.replace("http://", "https://") + "/x")):
        HUB.routes[rp] = {"location": loc, "headers": good_h}
        CDN.seen.clear()
        d = newdir("redir")
        ok, e = expect_err("refused", hf.download, lst, f, d)
        check(f"redirect to {case} refused", ok and not CDN.seen and entries(d) == set(), e)

    # hop counting: 5 redirects ok, 6 refused
    def chain(n: int, tag: str) -> str:
        """Exactly n redirects: resolve -> /hop/1 -> ... -> /hop/(n-1) -> CDN."""
        cdn_path = f"/cdn/{tag}"
        CDN.routes[cdn_path] = {"body": PAYLOAD}
        HUB.routes[rp] = {"location": f"/hop/{tag}/1", "headers": good_h}
        for i in range(1, n - 1):
            HUB.routes[f"/hop/{tag}/{i}"] = {"location": f"/hop/{tag}/{i + 1}", "headers": good_h}
        HUB.routes[f"/hop/{tag}/{n - 1}"] = {"location": CDN_BASE + cdn_path, "headers": good_h}
        return cdn_path
    chain(5, "five")
    d = newdir("hops5")
    ok, r = no_raise(hf.download, lst, f, d)
    check("5 redirects followed", ok and sha_of(r) == SHA, repr(r))
    cp = chain(6, "six")
    CDN.seen.clear()
    d = newdir("hops6")
    ok, e = expect_err("refused", hf.download, lst, f, d)
    check("6 redirects refused", ok and not cdn_requests(cp) and entries(d) == set(), e)

    for case, h in (("commit", {**good_h, "X-Repo-Commit": COMMIT2}),
                    ("no commit header", {k: v for k, v in good_h.items() if k != "X-Repo-Commit"}),
                    ("size", {**good_h, "X-Linked-Size": str(f.size + 1)}),
                    ("size junk", {**good_h, "X-Linked-Size": "12x"}),
                    ("etag", {**good_h, "X-Linked-ETag": '"' + "0" * 64 + '"'})):
        cp = serve_file(f, hub_headers=h)
        CDN.seen.clear()
        d = newdir("hdr")
        ok, e = expect_err("mismatch", hf.download, lst, f, d)
        check(f"header mismatch ({case}) refused before the CDN and before any byte",
              ok and not cdn_requests(cp) and entries(d) == set(), e)
    cp = serve_file(f, hub_headers={"X-Repo-Commit": COMMIT})
    d = newdir("hdrmin")
    ok, r = no_raise(hf.download, lst, f, d)
    check("only X-Repo-Commit present (linked headers absent) still ok", ok and sha_of(r) == SHA, repr(r))
    serve_file(f, clen=f.size + 5)
    d = newdir("clen")
    ok, e = expect_err("mismatch", hf.download, lst, f, d)
    check("CDN Content-Length != size refused before writing", ok and entries(d) == set(), e)

    HUB.routes[rp] = {"status": 401, "body": b"x", "headers": {"X-Error-Code": "GatedRepo"}}
    check("resolve 401 GatedRepo -> gated", expect_err("gated", hf.download, lst, f, newdir("gated"))[0])
    HUB.routes[rp] = {"status": 429, "body": b"x", "headers": {"RateLimit": '"resolvers";r=0;t=7'}}
    try:
        hf.download(lst, f, newdir("rl"))
        check("resolve 429 -> rate_limited", False, "no raise")
    except HfError as e:
        check("resolve 429 -> rate_limited, retry 7", e.kind == "rate_limited" and e.retry_after_s == 7, e.message)
    HUB.routes[rp] = {"status": 404, "body": b"Entry not found", "headers": {"X-Error-Code": "EntryNotFound"}}
    check("resolve 404 -> not_found", expect_err("not_found", hf.download, lst, f, newdir("404"))[0])
    cp = serve_file(f)
    CDN.routes[cp] = {"status": 403, "body": b"Auth failed: credentials expired"}
    d = newdir("403")
    ok, e = expect_err("network", hf.download, lst, f, d)
    check("CDN 403 -> network, nothing written", ok and entries(d) == set(), e)


def test_download_data():
    section("download: bad data, short body, cancel, deadline")
    f = make_file()
    lst = make_listing(f)
    wrong = bytearray(PAYLOAD)
    wrong[1000] ^= 0xFF
    serve_file(f, payload=bytes(wrong))
    d = newdir("shabad")
    ok, e = expect_err("mismatch", hf.download, lst, f, d)
    check("sha256 mismatch -> mismatch, part + sidecar deleted", ok and entries(d) == set(), e)
    serve_file(f, payload=PAYLOAD + b"extra", clen=None)
    d = newdir("toolong")
    ok, e = expect_err("mismatch", hf.download, lst, f, d)
    check("more bytes than size -> mismatch, deleted", ok and entries(d) == set(), e)

    serve_file(f, short=120_000)
    d = newdir("short")
    ok, e = expect_err("network", hf.download, lst, f, d)
    part = os.path.join(d, NAME + ".part")
    check("short body -> network, part + sidecar kept", ok and entries(d) == {NAME + ".part", NAME + ".part.json"}
          and os.path.getsize(part) == 120_000, f"{e} {entries(d)}")
    check("pending_part reports the kept bytes", hf.pending_part(d, f, lst) == 120_000)
    check("pending_part for another commit is 0", hf.pending_part(d, f, make_listing(f, COMMIT2)) == 0)
    side = json.loads(Path(part + ".json").read_text(encoding="utf-8"))
    check("sidecar holds the app mark, repo, commit, path, size, sha256",
          side == {"app": "agent-start", "repo": REPO, "commit": COMMIT, "path": NAME, "size": f.size,
                   "sha256": SHA}, repr(side))
    only_allowed("short", d)
    ok, _ = no_raise(hf.discard_part, d, f)
    check("discard_part removes part + sidecar", ok and entries(d) == set(), repr(entries(d)))
    check("pending_part with nothing kept is 0", hf.pending_part(d, f, lst) == 0)
    check("pending_part never raises on junk", hf.pending_part("\\\\srv\\x", f, lst) == 0
          and hf.pending_part(d, None, None) == 0)

    # cancel keeps part + sidecar
    serve_file(f, chunk=8192, chunk_sleep=0.01)
    d = newdir("cancel")
    stop = threading.Event()

    def prog(done, total):
        if done >= 40_000:
            stop.set()
    ok, e = expect_err("cancelled", hf.download, lst, f, d, progress=prog, stop_evt=stop)
    part = os.path.join(d, NAME + ".part")
    n = os.path.getsize(part) if os.path.exists(part) else -1
    check("cancel -> cancelled, part + sidecar kept", ok and entries(d) == {NAME + ".part", NAME + ".part.json"}
          and 0 < n < f.size, f"{e} {entries(d)} {n}")
    only_allowed("cancel", d)
    stop2 = threading.Event()
    stop2.set()
    check("stop already set -> cancelled, part untouched",
          expect_err("cancelled", hf.download, lst, f, d, stop_evt=stop2)[0] and os.path.getsize(part) == n)

    serve_file(f, chunk=8192, chunk_sleep=0.05)
    d2 = newdir("deadline")
    pcalls: list[float] = []
    t0 = time.monotonic()
    ok, e = expect_err("timeout", hf.download, lst, f, d2, deadline_s=0.4, progress=lambda a, b: pcalls.append(a))
    el = time.monotonic() - t0
    check("deadline -> timeout, part kept", ok and NAME + ".part" in entries(d2), f"{e} {entries(d2)}")
    check("progress throttled to about 10 per second", 1 <= len(pcalls) <= el * 10 + 2, f"{len(pcalls)} in {el:.2f}s")
    only_allowed("deadline", d2)


def make_part(tag: str, f: HfFile, lst: RepoListing, payload: bytes = PAYLOAD) -> tuple[str, int]:
    """A kept .part made the real way: start a slow download and cancel it."""
    serve_file(f, payload=payload, commit=lst.commit, chunk=8192, chunk_sleep=0.01)
    d = newdir(tag)
    stop = threading.Event()

    def prog(done, total):
        if done >= 60_000:
            stop.set()
    try:
        hf.download(lst, f, d, progress=prog, stop_evt=stop)
    except HfError:
        pass
    return d, hf.pending_part(d, f, lst)


def test_resume():
    section("download: resume")
    f = make_file()
    lst = make_listing(f)
    d, n = make_part("resume", f, lst)
    check("resume: a kept part exists", 0 < n < f.size, str(n))
    cp = serve_file(f)
    calls: list[tuple[int, int]] = []
    ok, r = no_raise(hf.download, lst, f, d, progress=lambda a, b: calls.append((a, b)))
    check("resume completes, final sha256 right", ok and sha_of(r) == SHA, repr(r))
    reqs = cdn_requests(cp)
    check("resume asked for Range bytes=N-", len(reqs) == 1 and reqs[0].get("Range") == f"bytes={n}-", repr(reqs))
    check("resume progress starts with the re-hash (<= N) and ends at size",
          bool(calls) and calls[0][0] <= n and calls[-1] == (f.size, f.size), repr(calls[:2]))
    check("resume: only the final file remains", entries(d) == {NAME}, repr(entries(d)))

    d, n = make_part("ignore", f, lst)
    cp = serve_file(f, range=False)
    ok, r = no_raise(hf.download, lst, f, d)
    reqs = cdn_requests(cp)
    check("server ignores Range (200) -> restart from zero, ok", ok and sha_of(r) == SHA and len(reqs) == 1
          and reqs[0].get("Range") == f"bytes={n}-", f"{r!r} {reqs}")
    only_allowed("ignore", d)

    d, n = make_part("badrange", f, lst)
    cp = serve_file(f, range_shift=1)
    ok, r = no_raise(hf.download, lst, f, d)
    reqs = cdn_requests(cp)
    check("wrong Content-Range -> fresh resolve without Range, ok",
          ok and sha_of(r) == SHA and len(reqs) == 2 and "Range" in reqs[0] and "Range" not in reqs[1],
          f"{r!r} {[h.get('Range') for h in reqs]}")

    d, n = make_part("416", f, lst)
    cp = serve_file(f, range_status=416)
    ok, r = no_raise(hf.download, lst, f, d)
    check("416 on resume -> restart from zero, ok", ok and sha_of(r) == SHA and len(cdn_requests(cp)) == 2, repr(r))

    # sidecar for a different commit: discarded, no Range
    d, n = make_part("commit", f, lst)
    lst2 = make_listing(f, COMMIT2)
    check("pending_part is 0 for the other commit", hf.pending_part(d, f, lst2) == 0)
    cp = serve_file(f, commit=COMMIT2)
    ok, r = no_raise(hf.download, lst2, f, d)
    reqs = cdn_requests(cp)
    check("sidecar for another commit discarded, fresh download ok",
          ok and sha_of(r) == SHA and len(reqs) == 1 and "Range" not in reqs[0], f"{r!r} {reqs}")
    only_allowed("commit", d)

    # this app's own part that cannot be resumed (larger than the file): discarded, fresh download
    d = newdir("odd")
    part = os.path.join(d, NAME + ".part")
    Path(part).write_bytes(PAYLOAD + b"xx")
    side = {"app": "agent-start", "repo": REPO, "commit": COMMIT, "path": NAME, "size": f.size, "sha256": SHA}
    Path(part + ".json").write_text(json.dumps(side), encoding="utf-8")
    check("too large: pending_part 0", hf.pending_part(d, f, lst) == 0)
    cp = serve_file(f)
    ok, r = no_raise(hf.download, lst, f, d)
    reqs = cdn_requests(cp)
    check("too large: discarded, fresh download ok", ok and sha_of(r) == SHA and len(reqs) == 1
          and "Range" not in reqs[0] and entries(d) == {NAME}, f"{r!r} {entries(d)}")

    # a NAME.part this app did NOT make (a browser's unfinished download, a note without the app mark, junk, a
    # note alone): never deleted, never written to; the download and discard_part refuse and nothing is requested
    foreign = PAYLOAD[:50_000]
    old_note = json.dumps({"repo": REPO, "commit": COMMIT, "path": NAME, "size": f.size, "sha256": SHA})
    for case, note in (("no sidecar", None), ("junk sidecar", "{junk"), ("sidecar without the app mark", old_note),
                       ("sidecar of another app", json.dumps({"app": "other"})), ("sidecar is a list", "[1]")):
        d = newdir("foreign")
        part = os.path.join(d, NAME + ".part")
        Path(part).write_bytes(foreign)
        if note is not None:
            Path(part + ".json").write_text(note, encoding="utf-8")
        before = {n: Path(d, n).read_bytes() for n in entries(d)}
        check(f"{case}: pending_part 0", hf.pending_part(d, f, lst) == 0)
        cp = serve_file(f)
        ok, e = expect_err("exists", hf.download, lst, f, d)
        check(f"{case}: download refused (exists), nothing requested", ok and not cdn_requests(cp), e)
        ok2, e2 = expect_err("exists", hf.discard_part, d, f)
        after = {n: Path(d, n).read_bytes() for n in entries(d)}
        check(f"{case}: discard_part refused too; every file is byte for byte what it was",
              ok2 and after == before, f"{e2} {sorted(after)}")
    d = newdir("foreign")
    Path(d, NAME + ".part.json").write_text("someone else's notes", encoding="utf-8")
    ok, e = expect_err("exists", hf.download, lst, f, d)
    check("a foreign NAME.part.json alone: refused, left alone",
          ok and Path(d, NAME + ".part.json").read_text(encoding="utf-8") == "someone else's notes", e)
    d = newdir("foreign")
    Path(d, NAME + ".part.json").write_text(json.dumps(side), encoding="utf-8")
    serve_file(f)
    ok, r = no_raise(hf.download, lst, f, d)
    check("this app's note alone (stopped before the first byte): cleared, download ok",
          ok and sha_of(r) == SHA and entries(d) == {NAME}, f"{r!r} {entries(d)}")
    d = newdir("foreign")
    ok, _ = no_raise(hf.discard_part, d, f)
    check("discard_part with nothing there: nothing happens", ok and entries(d) == set())

    # kept prefix that is corrupt: resume completes the bytes, whole-file hash catches it, everything deleted
    d, n = make_part("corrupt", f, lst)
    part = os.path.join(d, NAME + ".part")
    with open(part, "r+b") as fh:
        fh.seek(10)
        b = fh.read(1)
        fh.seek(10)
        fh.write(bytes([b[0] ^ 0xFF]))
    serve_file(f)
    ok, e = expect_err("mismatch", hf.download, lst, f, d)
    check("corrupt kept prefix -> whole-file hash mismatch, part deleted", ok and entries(d) == set(), e)

    # cancel during the re-hash keeps the part
    d, n = make_part("rehashcancel", f, lst)
    stop = threading.Event()
    stop.set()
    ok, e = expect_err("cancelled", hf.download, lst, f, d, stop_evt=stop)
    check("cancel before/while re-hashing keeps part", ok and hf.pending_part(d, f, lst) == n, e)


def test_busy():
    section("download: one at a time")
    f = make_file()
    lst = make_listing(f)
    serve_file(f, chunk=8192, chunk_sleep=0.02)
    d = newdir("busy1")
    stop = threading.Event()
    started = threading.Event()
    out: dict = {}

    def run():
        try:
            out["r"] = hf.download(lst, f, d, progress=lambda a, b: started.set() if a > 0 else None, stop_evt=stop)
        except HfError as e:
            out["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    check("first download started", started.wait(10))
    f2 = make_file("other.gguf")
    serve_file(f2)
    d2 = newdir("busy2")
    ok, e = expect_err("busy", hf.download, make_listing(f2), f2, d2)
    check("second concurrent download -> busy", ok and entries(d2) == set(), e)
    ok, e = expect_err("busy", hf.discard_part, d, f)
    check("discard_part while downloading -> busy", ok, e)
    stop.set()
    t.join(15)
    check("first download cancelled cleanly", not t.is_alive() and getattr(out.get("e"), "kind", "") == "cancelled",
          repr(out))
    ok, r = no_raise(hf.download, make_listing(f2), f2, d2)
    check("after the first ends, a new download runs", ok and sha_of(r) == SHA, repr(r))
    only_allowed("busy", d)


# ----------------------------------------------------------------------------------------- main

def main() -> int:
    global HUB, CDN, HUB_BASE, CDN_BASE
    HUB, HUB_BASE = start_server()
    CDN, CDN_BASE = start_server()
    saved = (hf._API_BASE, hf._RESOLVE_BASE, hf._ALLOW_LOCAL_HTTP_FOR_TESTS)
    saved_token = os.environ.get("HF_TOKEN")
    os.environ["HF_TOKEN"] = DUMMY_TOKEN          # must never be read or sent
    try:
        for fn in (test_parse_repo, test_host_policy):
            try:
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
        hf._API_BASE = HUB_BASE
        hf._RESOLVE_BASE = HUB_BASE
        hf._ALLOW_LOCAL_HTTP_FOR_TESTS = True
        for fn in (test_list_files, test_download_basic, test_download_policy, test_download_data, test_resume,
                   test_busy):
            try:
                hf._ALLOW_LOCAL_HTTP_FOR_TESTS = True
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
        hf._ALLOW_LOCAL_HTTP_FOR_TESTS = False
        ok, e = expect_err("refused", hf.list_files, REPO)
        check("flag off: http test base refused (no request)", ok, e)
    finally:
        hf._API_BASE, hf._RESOLVE_BASE, hf._ALLOW_LOCAL_HTTP_FOR_TESTS = saved
        if saved_token is None:
            os.environ.pop("HF_TOKEN", None)
        else:
            os.environ["HF_TOKEN"] = saved_token
        for srv in (HUB, CDN):
            srv.shutdown()
            srv.server_close()
        shutil.rmtree(TMP, ignore_errors=True)
        check("temp dir removed", not TMP.exists(), str(TMP))
    check("test hooks restored", hf._ALLOW_LOCAL_HTTP_FOR_TESTS is False and hf._API_BASE == "https://huggingface.co"
          and hf._RESOLVE_BASE == "https://huggingface.co")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
