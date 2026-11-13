"""Self-check for updater.py. Run: .venv\\Scripts\\python.exe tests\\test_updater.py

Safe by construction:
  * All HTTP goes to a local http.server on 127.0.0.1 (random port), except ONE read-only check('0.0.1')
    against api.github.com (skipped when AGENTSTART_TEST_OFFLINE=1; a NOTE, not a FAIL, when it errors).
  * launch_installer never runs the real Agent Start installer: updater._popen is a recorder everywhere
    except test_real_inno, which compiles its OWN throwaway Inno Setup installer (Uninstallable=no,
    per-user, installs into this test's temp dir) and runs it. Skipped when ISCC is missing or
    AGENTSTART_TEST_NO_INNO=1.
  * Everything is written under a temp dir that is removed at the end; LOCALAPPDATA is pointed at it for
    the default_download_dir() tests.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater  # noqa: E402
from updater import UpdateError, UpdateInfo  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="agentstart-updater-test-"))
NAME = "AgentStart-Setup-9.9.0.exe"
CANON = f"/{updater.REPO}/releases/download/v9.9.0/{NAME}"
SHA_AB = "ab" * 32
_LAD = os.environ.get("LOCALAPPDATA", "")
ISCC_CANDIDATES = [Path(_LAD) / "Programs" / "Inno Setup 6" / "ISCC.exe",
                   Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe")]


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------------------------- local server

ROUTES: dict[str, dict] = {}
SEEN_HEADERS: list[dict] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        SEEN_HEADERS.append(dict(self.headers))
        r = ROUTES.get(self.path)
        if r is None:
            self.send_error(404)
            return
        if r.get("delay"):
            time.sleep(r["delay"])
        try:
            if "raw" in r:                       # malformed HTTP on purpose
                self.wfile.write(r["raw"])
                return
            if "location" in r:
                self.send_response(r.get("status", 302))
                self.send_header("Location", r["location"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = r.get("body", b"")
            self.send_response(r.get("status", 200))
            self.send_header("Content-Type", r.get("ctype", "application/octet-stream"))
            clen = r.get("clen", len(body))
            if clen is not None:                 # None: no Content-Length, body ends at close (HTTP/1.0)
                self.send_header("Content-Length", str(clen))
            self.end_headers()
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


def start_server():
    srv = Server(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ----------------------------------------------------------------------------------------- helpers

_OMIT = object()


def canon_url(base: str, tag="v9.9.0", name=None) -> str:
    return f"{base}/{updater.REPO}/releases/download/{tag}/{name or 'AgentStart-Setup-' + tag.lstrip('vV') + '.exe'}"


def release(base: str, tag="v9.9.0", asset_name=None, size=1234, digest=f"sha256:{SHA_AB}", draft=False,
            prerelease=False, asset_url=None, extra_assets=()) -> dict:
    ver = tag.lstrip("vV")
    name = asset_name or f"AgentStart-Setup-{ver}.exe"
    a = {"name": name, "size": size, "state": "uploaded",
         "browser_download_url": asset_url or f"{base}/{updater.REPO}/releases/download/{tag}/{name}"}
    if digest is not _OMIT:
        a["digest"] = digest
    return {"tag_name": tag, "name": f"Agent Start {ver}", "draft": draft, "prerelease": prerelease,
            "html_url": f"https://github.com/{updater.REPO}/releases/tag/{tag}",
            "assets": [*extra_assets, a]}


def serve_json(path: str, obj, **extra) -> None:
    ROUTES[path] = {"body": json.dumps(obj).encode(), "ctype": "application/json", **extra}


def no_raise(fn, *a, **kw):
    try:
        return True, fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return False, e


def raises_update_error(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except UpdateError as e:
        return True, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"wrong exception {type(e).__name__}: {e}"
    return False, "did not raise"


def info_for(base: str, body: bytes, name=NAME, sha=True, size=None, url=None, tag="v9.9.0"):
    return UpdateInfo(version="9.9.0", tag=tag, asset_name=name, asset_url=url or canon_url(base, tag, name),
                      size=len(body) if size is None else size,
                      sha256=hashlib.sha256(body).hexdigest() if sha is True else sha,
                      page_url="https://github.com/x")


def sha_of(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def make_recorder(calls, during=None):
    def recorder(argv, **kw):
        calls.append((argv, kw))
        if during is not None:
            during(argv)

        class P:
            pid = 0
        return P()
    return recorder


# ----------------------------------------------------------------------------------------- tests

def test_versions():
    section("parse_version / is_newer")
    check("parse 'v1.10.0' == (1,10,0)", updater.parse_version("v1.10.0") == (1, 10, 0))
    check("parse '1.6.0' == (1,6,0)", updater.parse_version("1.6.0") == (1, 6, 0))
    check("parse 'V2' == (2,)", updater.parse_version("V2") == (2,))
    for junk in ("", "abc", "1..2", "1.2.x", "v", "1.2.3-beta", "../1.2", "1.2.3.4.5", None, 5):
        ok, _ = raises_any_value_error(junk)
        check(f"parse rejects {junk!r}", ok)
    check("1.10.0 newer than 1.9.0", updater.is_newer("1.10.0", "1.9.0"))
    check("1.9.0 not newer than 1.10.0", not updater.is_newer("1.9.0", "1.10.0"))
    check("v1.6.1 newer than 1.6.0 (v-prefix)", updater.is_newer("v1.6.1", "1.6.0"))
    check("1.6.0 not newer than v1.6.0", not updater.is_newer("1.6.0", "v1.6.0"))
    check("1.6 == 1.6.0 (not newer either way)", not updater.is_newer("1.6", "1.6.0")
          and not updater.is_newer("1.6.0", "1.6"))
    check("1.6.0.1 newer than 1.6", updater.is_newer("1.6.0.1", "1.6"))
    check("2.0.0 newer than 1.99.99", updater.is_newer("2.0.0", "1.99.99"))
    check("junk a -> False", updater.is_newer("banana", "1.0") is False)
    check("junk b -> False", updater.is_newer("9.0", "banana") is False)
    check("None -> False", updater.is_newer(None, "1.0") is False)


def raises_any_value_error(s):
    try:
        updater.parse_version(s)
    except ValueError:
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, repr(e)
    return False, "no raise"


def test_due():
    section("due()")
    now = 1_000_000_000.0
    check("never checked -> due", updater.due(None, now))
    check("just checked -> not due", not updater.due(now - 10, now))
    check("23h ago -> not due", not updater.due(now - 23 * 3600, now))
    check("24h ago -> due", updater.due(now - 24 * 3600, now))
    check("clock went backwards -> due", updater.due(now + 3600, now))
    check("custom interval", updater.due(now - 61, now, interval_s=60) and not updater.due(now - 59, now, 60))
    check("int last works", not updater.due(int(now) - 10, now))
    for bad in (float("nan"), float("inf"), float("-inf"), "123", "nan", True, False, [1], object()):
        ok, r = no_raise(updater.due, bad, now)
        check(f"last={bad!r} -> due, no raise", ok and r is True, repr(r))
    ok, r = no_raise(updater.due, now - 10, float("nan"))
    check("now=nan -> due", ok and r is True, repr(r))
    ok, r = no_raise(updater.due, now - 10, now, interval_s="x")
    check("junk interval -> due, no raise", ok and r is True, repr(r))
    check("CHECK_INTERVAL_S == 86400", updater.CHECK_INTERVAL_S == 86400)
    check("RETRY_AFTER_ERROR_S == 3600", updater.RETRY_AFTER_ERROR_S == 3600)
    check("REPO", updater.REPO == "warrenjbg/agent-start")
    check("CHECK error retry: due(last, now, RETRY_AFTER_ERROR_S) after 61 min",
          updater.due(now - 3660, now, updater.RETRY_AFTER_ERROR_S)
          and not updater.due(now - 600, now, updater.RETRY_AFTER_ERROR_S))


def test_url_ok():
    section("_url_ok (scheme / host / port policy)")
    gh = {"github.com"}
    updater._ALLOW_LOCAL_HTTP_FOR_TESTS = False
    for url, want in (("https://github.com/a", True), ("https://github.com:443/a", True),
                      ("https://GitHub.com/a", True),
                      ("https://github.com:8443/a", False), ("https://github.com:80/a", False),
                      ("https://github.com:99999/a", False), ("https://github.com:abc/a", False),
                      ("http://github.com/a", False), ("ftp://github.com/a", False),
                      ("https://user:pw@github.com/a", False), ("https://evil.com/a", False),
                      ("https://github.com.evil.com/a", False), ("http://127.0.0.1:5555/a", False),
                      ("", False), (None, False)):
        check(f"{url!r} -> {want}", updater._url_ok(url, gh) is want)
    updater._ALLOW_LOCAL_HTTP_FOR_TESTS = True
    loc = {"127.0.0.1"}
    check("flag on: http://127.0.0.1:5555 ok (any port)", updater._url_ok("http://127.0.0.1:5555/a", loc))
    check("flag on: https://127.0.0.1:5555 refused (https must be 443)",
          not updater._url_ok("https://127.0.0.1:5555/a", loc))
    check("flag on: http://localhost refused", not updater._url_ok("http://localhost:5555/a", {"localhost"}))
    check("flag on: http://github.com still refused", not updater._url_ok("http://github.com/a", gh))


def test_check(base: str):
    section("check / check_latest (local server)")
    updater._ALLOW_LOCAL_HTTP_FOR_TESTS = True

    serve_json("/newer", release(base))
    SEEN_HEADERS.clear()
    ok, r = no_raise(updater.check, "1.6.0", api_url=f"{base}/newer")
    check("newer -> ('newer', UpdateInfo)", ok and isinstance(r, tuple) and r[0] == "newer"
          and isinstance(r[1], UpdateInfo), repr(r))
    info = r[1] if ok and isinstance(r, tuple) else None
    if isinstance(info, UpdateInfo):
        check("fields", (info.version, info.tag, info.asset_name, info.size, info.sha256)
              == ("9.9.0", "v9.9.0", NAME, 1234, SHA_AB), repr(info))
        check("asset_url is the canonical release URL", info.asset_url == f"{base}{CANON}", info.asset_url)
        check("page_url", info.page_url == f"https://github.com/{updater.REPO}/releases/tag/v9.9.0", info.page_url)
    hdr = SEEN_HEADERS[-1] if SEEN_HEADERS else {}
    check("User-Agent AgentStart/<current>", hdr.get("User-Agent") == "AgentStart/1.6.0", repr(hdr))
    check("Accept github json", hdr.get("Accept") == "application/vnd.github+json", repr(hdr))
    r = updater.check_latest("1.6.0", api_url=f"{base}/newer")
    check("check_latest wrapper -> UpdateInfo on 'newer'", isinstance(r, UpdateInfo), repr(r))

    serve_json("/upper", release(base, digest=f"SHA256:{SHA_AB.upper()}"))
    st, r = updater.check("1.6.0", api_url=f"{base}/upper")
    check("uppercase digest normalised", st == "newer" and r.sha256 == SHA_AB, repr((st, r)))
    serve_json("/minor10", release(base, tag="v1.10.0"))
    st, r = updater.check("1.9.0", api_url=f"{base}/minor10")
    check("1.10.0 offered over 1.9.0", st == "newer" and r.version == "1.10.0", repr((st, r)))
    serve_json("/extra", release(base, extra_assets=[{"name": "AgentStart-Setup-9.9.0.zip", "size": 5,
                                                      "browser_download_url": f"{base}/x"}]))
    st, r = updater.check("1.6.0", api_url=f"{base}/extra")
    check("picks the .exe among several assets", st == "newer" and r.asset_name == NAME, repr((st, r)))

    for label, path, obj, cur in (("same version", "/same", release(base, tag="v1.6.0"), "1.6.0"),
                                  ("older version", "/older", release(base, tag="v1.5.9"), "1.6.0"),
                                  ("same version, no asset at all", "/same-noasset",
                                   {**release(base, tag="v1.6.0"), "assets": []}, "1.6")):
        serve_json(path, obj)
        ok, r = no_raise(updater.check, cur, api_url=f"{base}{path}")
        check(f"{label} -> ('current', None)", ok and r == ("current", None), repr(r))
        check(f"{label}: check_latest -> None", updater.check_latest(cur, api_url=f"{base}{path}") is None)

    foreign_repo = f"{base}/evil/agent-start/releases/download/v9.9.0/{NAME}"
    cases = {
        "no digest (mandatory)": ("/nodigest", release(base, digest=_OMIT), "1.6.0"),
        "digest null": ("/nulldigest", release(base, digest=None), "1.6.0"),
        "md5 digest": ("/md5", release(base, digest="md5:abcd"), "1.6.0"),
        "malformed sha256": ("/badsha", release(base, digest="sha256:zz"), "1.6.0"),
        "sha256 too short": ("/shortsha", release(base, digest="sha256:" + "ab" * 31), "1.6.0"),
        "prerelease": ("/pre", release(base, prerelease=True), "1.6.0"),
        "draft": ("/draft", release(base, draft=True), "1.6.0"),
        "missing asset": ("/noasset", {**release(base), "assets": []}, "1.6.0"),
        "assets not a list": ("/assetsdict", {**release(base), "assets": {"x": 1}}, "1.6.0"),
        "wrong asset name": ("/wrongname", release(base, asset_name="AgentStart-Setup-9.8.0.exe"), "1.6.0"),
        "asset name with path": ("/pathname", release(base, asset_name="../AgentStart-Setup-9.9.0.exe"), "1.6.0"),
        "junk tag": ("/junktag", release(base, tag="latest"), "1.6.0"),
        "tag with whitespace": ("/wstag", {**release(base), "tag_name": " v9.9.0"}, "1.6.0"),
        "zero size": ("/zerosize", release(base, size=0), "1.6.0"),
        "string size": ("/strsize", release(base, size="1234"), "1.6.0"),
        "too big": ("/toobig", release(base, size=updater.MAX_INSTALLER_BYTES + 1), "1.6.0"),
        "state not uploaded": ("/state", {**release(base), "assets": [{**release(base)["assets"][0],
                                                                         "state": "new"}]}, "1.6.0"),
        "asset URL on foreign host": ("/foreign", release(base, asset_url="https://evil.example.com/a.exe"), "1.6.0"),
        "asset URL file://": ("/fileurl", release(base, asset_url="file:///C:/Windows/notepad.exe"), "1.6.0"),
        "asset URL of a foreign repo (same host)": ("/foreignrepo", release(base, asset_url=foreign_repo), "1.6.0"),
        "asset URL for another tag": ("/othertag", release(base, asset_url=canon_url(base, "v9.9.1", NAME)), "1.6.0"),
        "asset URL with query": ("/query", release(base, asset_url=f"{base}{CANON}?x=1"), "1.6.0"),
        "asset URL with fragment": ("/frag", release(base, asset_url=f"{base}{CANON}#x"), "1.6.0"),
        "asset URL case-changed": ("/case", release(base, asset_url=f"{base}{CANON.replace('agent-start', 'Agent-Start')}"), "1.6.0"),
        "JSON is a list": ("/list", [release(base)], "1.6.0"),
        "junk current version": ("/newer", None, "banana"),
        "current None": ("/newer", None, None),
    }
    for label, (path, obj, cur) in cases.items():
        if obj is not None:
            serve_json(path, obj)
        ok, r = no_raise(updater.check, cur, api_url=f"{base}{path}")
        check(f"{label} -> ('error', None)", ok and r == ("error", None), repr(r))
        ok, r = no_raise(updater.check_latest, cur, api_url=f"{base}{path}")
        check(f"{label}: check_latest -> None", ok and r is None, repr(r))

    # the production rule: _DOWNLOAD_BASE = https://github.com, exact URL only
    saved = updater._DOWNLOAD_BASE
    updater._DOWNLOAD_BASE = "https://github.com"
    try:
        gh = f"https://github.com/{updater.REPO}/releases/download/v9.9.0/{NAME}"
        serve_json("/prod-ok", release(base, asset_url=gh))
        st, r = updater.check("1.6.0", api_url=f"{base}/prod-ok")
        check("production: exact github.com URL -> newer", st == "newer" and r.asset_url == gh, repr((st, r)))
        for label, u in (("foreign repo on github.com", f"https://github.com/evil/agent-start/releases/download/v9.9.0/{NAME}"),
                         ("foreign owner, same repo name", f"https://github.com/warrenjbg2/agent-start/releases/download/v9.9.0/{NAME}"),
                         ("explicit :443", gh.replace("github.com/", "github.com:443/", 1)),
                         ("port 8443", gh.replace("github.com/", "github.com:8443/", 1)),
                         ("plain http", gh.replace("https://", "http://", 1)),
                         ("objects host", gh.replace("github.com", "objects.githubusercontent.com", 1)),
                         ("dot-dot path", f"https://github.com/{updater.REPO}/releases/download/v9.9.0/../v9.9.0/{NAME}")):
            serve_json("/prod-bad", release(base, asset_url=u))
            ok, r = no_raise(updater.check, "1.6.0", api_url=f"{base}/prod-bad")
            check(f"production: {label} -> error", ok and r == ("error", None), repr(r))
    finally:
        updater._DOWNLOAD_BASE = saved

    ROUTES["/500"] = {"status": 500, "body": b"oops"}
    ROUTES["/garbage"] = {"body": b"{not json at all", "ctype": "application/json"}
    ROUTES["/binary"] = {"body": b"\xff\xfe\x00garbage", "ctype": "application/json"}
    ROUTES["/slow"] = {"delay": 3, "body": json.dumps(release(base)).encode()}
    ROUTES["/redir-foreign"] = {"location": "https://evil.example.com/latest"}
    ROUTES["/redir-http-gh"] = {"location": "http://github.com/latest"}
    ROUTES["/badstatus"] = {"raw": b"HTTX/9 garbage\r\n\r\n"}
    for label, path in (("HTTP 500", "/500"), ("HTTP 404", "/missing"), ("garbage JSON", "/garbage"),
                        ("non-UTF8 body", "/binary"), ("redirect to foreign host", "/redir-foreign"),
                        ("redirect https->http (github.com over http)", "/redir-http-gh"),
                        ("BadStatusLine (http.client.HTTPException)", "/badstatus")):
        ok, r = no_raise(updater.check, "1.6.0", api_url=f"{base}{path}")
        check(f"{label} -> error, no raise", ok and r == ("error", None), repr(r))

    # MAX_API_BYTES, with and without a Content-Length header
    saved_max = updater.MAX_API_BYTES
    try:
        obj = release(base)
        small = json.dumps(obj).encode()
        updater.MAX_API_BYTES = len(small)
        serve_json("/at-limit", obj)
        check("body exactly MAX_API_BYTES -> newer", updater.check("1.6.0", api_url=f"{base}/at-limit")[0] == "newer")
        big = json.dumps({**obj, "body": "x" * 5000}).encode()
        ROUTES["/over-limit"] = {"body": big, "ctype": "application/json"}
        ROUTES["/over-limit-nocl"] = {"body": big, "ctype": "application/json", "clen": None}
        for p in ("/over-limit", "/over-limit-nocl"):
            ok, r = no_raise(updater.check, "1.6.0", api_url=f"{base}{p}")
            check(f"body over MAX_API_BYTES ({p}) -> error", ok and r == ("error", None), repr(r))
    finally:
        updater.MAX_API_BYTES = saved_max

    t0 = time.monotonic()
    ok, r = no_raise(updater.check, "1.6.0", timeout=0.5, api_url=f"{base}/slow")
    dt = time.monotonic() - t0
    check("per-op timeout -> error, no raise, fast", ok and r == ("error", None) and dt < 2.5, f"{r!r} in {dt:.2f}s")

    saved_dl = updater.CHECK_DEADLINE_S
    updater.CHECK_DEADLINE_S = 1.0
    try:
        serve_json("/trickle", {**release(base), "pad": "x" * 3000}, chunk=1, chunk_sleep=0.02)
        t0 = time.monotonic()
        ok, r = no_raise(updater.check, "1.6.0", timeout=5, api_url=f"{base}/trickle")
        dt = time.monotonic() - t0
        check("trickling API body hits the overall deadline -> error (< 5 s for a 1 s deadline)",
              ok and r == ("error", None) and dt < 5, f"{r!r} in {dt:.2f}s")
    finally:
        updater.CHECK_DEADLINE_S = saved_dl

    ok, r = no_raise(updater.check, "1.6.0", timeout=1, api_url="http://127.0.0.1:1/x")
    check("connection refused -> error", ok and r == ("error", None), repr(r))
    ok, r = no_raise(updater.check, "1.6.0", api_url="not a url")
    check("bad api_url -> error", ok and r == ("error", None), repr(r))

    orig = updater._opener

    class Boom:
        def open(self, *a, **k):
            raise RuntimeError("not an OSError")
    updater._opener = lambda hosts: Boom()
    try:
        ok, r = no_raise(updater.check, "1.6.0", api_url=f"{base}/newer")
        check("non-OSError (RuntimeError) inside check -> error, no raise", ok and r == ("error", None), repr(r))
    finally:
        updater._opener = orig

    updater._ALLOW_LOCAL_HTTP_FOR_TESTS = False
    ok, r = no_raise(updater.check, "1.6.0", api_url=f"{base}/newer")
    check("plain http refused when flag off", ok and r == ("error", None), repr(r))
    updater._ALLOW_LOCAL_HTTP_FOR_TESTS = True


def test_download(base: str):
    section("download (local server)")
    updater._ALLOW_LOCAL_HTTP_FOR_TESTS = True
    hosts = {"127.0.0.1"}
    body = os.urandom(300_000)
    ROUTES[CANON] = {"body": body}
    dest = TMP / "dl"

    def listing():
        return sorted(p.name for p in dest.iterdir()) if dest.exists() else []

    def serve(route):
        ROUTES[CANON] = route

    calls = []
    info = info_for(base, body)
    ok, r = no_raise(updater.download, info, str(dest), progress=lambda d, t: calls.append((d, t)),
                     allowed_hosts=hosts)
    check("success returns final path", ok and r == str((dest / NAME).resolve()), repr(r))
    check("file content correct", (dest / NAME).exists() and (dest / NAME).read_bytes() == body)
    check("no .part left", listing() == [NAME], repr(listing()))
    check("progress monotonic, ends at total",
          len(calls) >= 2 and calls[-1] == (len(body), len(body))
          and all(a[0] < b[0] for a, b in zip(calls, calls[1:])) and all(t == len(body) for _, t in calls),
          repr(calls[:3]))
    check("lock released after success", not updater._download_lock.locked())
    (dest / NAME).unlink()

    ok, r = no_raise(updater.download, info, str(dest), progress=lambda d, t: 1 / 0, allowed_hosts=hosts)
    check("raising progress callback does not abort", ok and Path(r).exists(), repr(r))
    (dest / NAME).unlink()

    ROUTES["/dl/real"] = {"body": body}
    serve({"location": f"{base}/dl/real"})
    ok, r = no_raise(updater.download, info, str(dest), allowed_hosts=hosts)
    check("redirect to allowed host followed", ok and Path(r).read_bytes() == body, repr(r))
    (dest / NAME).unlink()
    serve({"body": body})

    def refused(label, inf, **kw):
        kw.setdefault("allowed_hosts", hosts)
        ok, msg = raises_update_error(updater.download, inf, str(dest), **kw)
        check(f"{label} -> UpdateError", ok, msg)
        check(f"{label}: nothing left in dest", listing() == [], repr(listing()))
        check(f"{label}: lock released", not updater._download_lock.locked())
        return msg

    m = refused("sha256 None (mandatory)", info_for(base, body, sha=None))
    check("sha256 None message", "sha256" in m, m)
    refused("sha256 empty", info_for(base, body, sha=""))
    refused("sha256 malformed", info_for(base, body, sha="zz" * 32))
    refused("asset URL not the canonical path", info_for(base, body, url=f"{base}/dl/real"))
    refused("asset URL of a foreign repo", info_for(base, body, url=f"{base}/evil/x/releases/download/v9.9.0/{NAME}"))
    refused("asset URL for another tag", info_for(base, body, url=canon_url(base, "v9.9.1", NAME)))
    refused("junk tag", info_for(base, body, tag="v9.9.0/../x"))
    saved = updater._DOWNLOAD_BASE
    updater._DOWNLOAD_BASE = "https://github.com"
    try:
        gh = f"https://github.com/{updater.REPO}/releases/download/v9.9.0/{NAME}"
        refused("production: github.com URL with :8443", info_for(base, body, url=gh.replace(".com/", ".com:8443/", 1)),
                allowed_hosts=None)
        refused("production: http:// github", info_for(base, body, url=gh.replace("https", "http", 1)),
                allowed_hosts=None)
    finally:
        updater._DOWNLOAD_BASE = saved

    refused("wrong size (expected bigger)", info_for(base, body, size=len(body) + 10))
    refused("wrong size (expected smaller)", info_for(base, body, size=len(body) - 10))
    serve({"body": body[:1000], "clen": len(body)})  # server lies: promises more than it sends
    refused("truncated body", info, timeout=3)
    serve({"body": body + b"EXTRA" * 100, "clen": None})
    m = refused("over-length body without Content-Length", info)
    check("over-length message says size mismatch", "size mismatch" in m, m)
    serve({"body": body})
    refused("wrong sha256", info_for(base, body, sha="00" * 32))
    ROUTES.pop(CANON)
    refused("HTTP 404", info)
    serve({"raw": b"HTTX/9 garbage\r\n\r\n"})
    m = refused("BadStatusLine (http.client.HTTPException, not OSError)", info)
    check("BadStatusLine wrapped (not raw)", "download failed" in m, m)

    orig_op = updater._opener

    class Boom:
        def open(self, *a, **k):
            raise RuntimeError("not an OSError")
    updater._opener = lambda h: Boom()
    try:
        refused("RuntimeError from the opener", info)
    finally:
        updater._opener = orig_op

    serve({"body": body})
    orig_fsync = os.fsync

    def disk_full(fd):
        raise OSError(28, "No space left on device")
    os.fsync = disk_full
    try:
        m = refused("disk full at fsync (OSError)", info)
        check("disk-full message", "No space" in m or "download failed" in m, m)
    finally:
        os.fsync = orig_fsync

    afile = TMP / "dest-is-a-file"
    afile.write_bytes(b"x")
    ok, msg = raises_update_error(updater.download, info, str(afile), allowed_hosts=hosts)
    check("dest_dir is a file (makedirs FileExistsError) -> UpdateError", ok, msg)
    ok, msg = raises_update_error(updater.download, info, str(afile / "sub"), allowed_hosts=hosts)
    check("dest_dir under a file -> UpdateError", ok, msg)
    afile.unlink()

    slowbig = os.urandom(4_000_000)
    big = info_for(base, slowbig)
    serve({"body": slowbig, "chunk": 64 * 1024, "chunk_sleep": 0.01})
    evt = threading.Event()
    seen = []

    def prog(d, t):
        seen.append(d)
        if len(seen) == 3:
            evt.set()

    msg = refused("cancel via stop_evt mid-stream", big, progress=prog, stop_evt=evt)
    check("cancel message is 'cancelled'", msg == "cancelled", msg)
    check("cancel happened mid-stream", 3 <= len(seen) < 10 and seen[-1] < big.size, repr(seen[-3:]))
    evt2 = threading.Event()
    evt2.set()
    m = refused("stop_evt already set", info, stop_evt=evt2)
    check("pre-set cancel message is 'cancelled'", m == "cancelled", m)

    # overall deadline vs a trickling server (each recv succeeds, so the per-op timeout never fires)
    trickle = os.urandom(20_000)
    serve({"body": trickle, "chunk": 16, "chunk_sleep": 0.02})
    t0 = time.monotonic()
    m = refused("trickling server past deadline_s", info_for(base, trickle), timeout=10, deadline_s=1.0)
    dt = time.monotonic() - t0
    check("deadline message is exactly 'timed out'", m == "timed out", m)
    check("deadline honoured (< 5 s for a 1 s deadline)", dt < 5, f"{dt:.2f}s")
    # a server that sends a little, then stalls far longer than the deadline (but shorter than timeout):
    # the socket timeout must be shrunk to the remaining deadline, not left at `timeout`
    stall = os.urandom(300)
    serve({"body": stall, "chunk": 100, "chunk_sleep": 15})
    t0 = time.monotonic()
    m = refused("stalling server, deadline < per-op timeout", info_for(base, stall), timeout=30, deadline_s=1.0)
    dt = time.monotonic() - t0
    check("stall: message is exactly 'timed out'", m == "timed out", m)
    check("stall: ended near the deadline (< 5 s, per-op timeout was 30 s)", dt < 5, f"{dt:.2f}s")

    # one download at a time
    serve({"body": slowbig, "chunk": 64 * 1024, "chunk_sleep": 0.01})
    stop_a = threading.Event()
    started = threading.Event()
    res_a = {}

    def run_a():
        try:
            updater.download(big, str(dest), progress=lambda d, t: started.set(), stop_evt=stop_a,
                             allowed_hosts=hosts)
            res_a["r"] = "finished"
        except UpdateError as e:
            res_a["r"] = str(e)

    ta = threading.Thread(target=run_a, daemon=True)
    ta.start()
    started.wait(10)
    part_before = listing()
    ok, msg = raises_update_error(updater.download, info, str(dest), allowed_hosts=hosts)
    check("second concurrent download -> 'another download is running'",
          ok and msg == "another download is running", msg)
    check("first download's .part untouched by the refused one", listing() == part_before == [NAME + ".part"],
          f"{part_before} -> {listing()}")
    stop_a.set()
    ta.join(15)
    check("first download then cancelled cleanly", res_a.get("r") == "cancelled" and listing() == [],
          f"{res_a} {listing()}")
    check("lock free again", not updater._download_lock.locked())

    # .part exclusively opened: a .part held by someone else is neither deleted nor shared
    serve({"body": body})
    dest.mkdir(parents=True, exist_ok=True)
    held = dest / (NAME + ".part")
    with open(held, "wb") as hf:
        hf.write(b"someone else's data")
        hf.flush()
        ok, msg = raises_update_error(updater.download, info, str(dest), allowed_hosts=hosts)
        check(".part held open elsewhere -> UpdateError", ok, msg)
    check("held .part content untouched", held.read_bytes() == b"someone else's data", repr(held.read_bytes()[:30]))
    held.unlink()

    serve({"location": "https://evil.example.com/AgentStart-Setup-9.9.0.exe"})
    m = refused("redirect to disallowed host", info)
    check("redirect refusal names it", "redirect" in m, m)
    serve({"location": f"http://localhost:{base.rsplit(':', 1)[1]}/dl/real"})
    refused("redirect to non-allowlisted 'localhost'", info)
    serve({"location": "file:///C:/Windows/win.ini"})
    refused("redirect to file://", info)
    serve({"location": f"http://github.com/{updater.REPO}/releases/download/v9.9.0/{NAME}"})
    refused("redirect https->http downgrade (http://github.com)", info, allowed_hosts=hosts | {"github.com"})
    serve({"location": f"https://127.0.0.1:{base.rsplit(':', 1)[1]}/dl/real"})
    refused("redirect to https on a non-443 port", info)
    serve({"body": body})
    refused("127.0.0.1 not in default allowed hosts", info, allowed_hosts=None)

    updater._ALLOW_LOCAL_HTTP_FOR_TESTS = False
    refused("plain http with flag off", info)
    updater._ALLOW_LOCAL_HTTP_FOR_TESTS = True

    for bad in ("../AgentStart-Setup-9.9.0.exe", "..\\AgentStart-Setup-9.9.0.exe", "sub/AgentStart-Setup-9.9.0.exe",
                "AgentStart-Setup-9.9.0.exe/..", "C:AgentStart-Setup-9.9.0.exe", "evil.exe",
                "AgentStart-Setup-9..9.exe", "AgentStart-Setup-9.9.0.exe\n"):
        refused(f"asset name {bad!r}", info_for(base, body, name=bad, url=f"{base}{CANON}"))
    check("nothing written outside dest", not (TMP / NAME).exists() and not (TMP / "sub").exists())

    stale = dest / (NAME + ".part")
    stale.write_bytes(b"stale junk")
    ok, r = no_raise(updater.download, info, str(dest), allowed_hosts=hosts)
    check("stale .part replaced, download ok", ok and Path(r).read_bytes() == body and listing() == [NAME],
          repr(r))
    shutil.rmtree(dest, ignore_errors=True)


# --------------------------------------------------------------------------- signed releases
# A test-only Ed25519 SIGNER (RFC 8032) built on update_sig's own curve arithmetic, and the OpenSSH SSHSIG
# wrapping, so the updater's signature path is tested without ssh-keygen. The keys are throwaway seeds written
# here in the open; the verifier under test is checked independently in test_update_sig.py (RFC vectors and
# signatures made by the real ssh-keygen).

def _ed_compress(p) -> bytes:
    import update_sig as S
    x, y, z, _t = p
    zi = pow(z, -1, S._P)
    x, y = x * zi % S._P, y * zi % S._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _ed_key(seed: bytes) -> tuple[int, bytes, bytes]:
    import update_sig as S
    h = hashlib.sha512(seed).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    return a, h[32:], _ed_compress(S._mul(a, S._G))


def _ed_sign(seed: bytes, msg: bytes) -> bytes:
    import update_sig as S
    a, prefix, pub = _ed_key(seed)
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little") % S._L
    big_r = _ed_compress(S._mul(r, S._G))
    k = int.from_bytes(hashlib.sha512(big_r + pub + msg).digest(), "little") % S._L
    return big_r + ((r + k * a) % S._L).to_bytes(32, "little")


def _sshsig(seed: bytes, message: bytes, namespace: str | None = None, alg: str = "sha512") -> str:
    import base64

    import update_sig as S
    ns = S.NAMESPACE if namespace is None else namespace
    pub = _ed_key(seed)[2]
    st = S._string
    sig = _ed_sign(seed, S.signed_blob(ns, alg, message))
    blob = (b"SSHSIG" + (1).to_bytes(4, "big") + st(st(b"ssh-ed25519") + st(pub)) + st(ns.encode()) + st(b"")
            + st(alg.encode()) + st(st(b"ssh-ed25519") + st(sig)))
    b64 = base64.b64encode(blob).decode()
    return ("-----BEGIN SSH SIGNATURE-----\n" + "\n".join(b64[i:i + 70] for i in range(0, len(b64), 70))
            + "\n-----END SSH SIGNATURE-----\n")


def test_signature(base: str):
    section("signed releases (local server, throwaway keys)")
    import update_sig as S
    sys.path.insert(0, str(ROOT / "tools"))
    import check_release_keys
    updater._ALLOW_LOCAL_HTTP_FOR_TESTS = True
    hosts = {"127.0.0.1"}
    seed, stranger = bytes(range(32)), bytes(range(100, 132))
    pub = _ed_key(seed)[2]
    vec_seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    check("the test signer reproduces RFC 8032 test vector 1 (key and signature)",
          _ed_key(vec_seed)[2].hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
          and _ed_sign(vec_seed, b"").hex() ==
          "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
          "d25bf5f0595bbe24655141438e7a100b")
    body = os.urandom(60_000)
    sha = hashlib.sha256(body).hexdigest()
    sig_route = CANON + S.SIG_SUFFIX
    dest = TMP / "signed"

    def sig_text(by=seed, repo=updater.REPO, version="9.9.0", name=NAME, size=None, sha256=sha, ns=None) -> str:
        return _sshsig(by, S.manifest(repo, version, name, len(body) if size is None else size, sha256), ns)

    def serve(sig, **asset) -> None:
        """The release (installer of `body`) with an optional .sig asset; asset fields can be overridden."""
        extra = []
        ROUTES.pop(sig_route, None)
        if sig is not None:
            raw = sig if isinstance(sig, bytes) else sig.encode()
            a = {"name": NAME + S.SIG_SUFFIX, "size": len(raw), "state": "uploaded",
                 "browser_download_url": f"{base}{sig_route}"}
            a.update(asset)
            extra.append(a)
            ROUTES[sig_route] = {"body": raw}
        serve_json("/signed", release(base, size=len(body), digest=f"sha256:{sha}", extra_assets=extra))
        ROUTES[CANON] = {"body": body}

    def run():
        ok, r = no_raise(updater.check, "1.6.0", api_url=f"{base}/signed")
        return r if ok and isinstance(r, tuple) else ("raised", r)

    saved_keys = S.PUBLIC_KEYS
    try:
        # ---- a build without keys: exactly as before the feature
        S.PUBLIC_KEYS = ()
        serve(None)
        st, info = run()
        check("no keys in the build, release without a signature: 'newer' as before", st == "newer"
              and isinstance(info, UpdateInfo) and info.signature == "", repr((st, info)))
        ok, p = no_raise(updater.download, info, str(dest), allowed_hosts=hosts)
        check("...and it downloads by SHA-256 as before", ok and sha_of(p) == sha, repr(p))
        Path(p).unlink()
        serve("junk that is not a signature")
        check("no keys: a junk .sig asset is not even looked at", run()[0] == "newer")
        check("build guard: no keys -> the build fails", check_release_keys.main(env={}) == 1)
        check("build guard: no keys but the test-build override -> goes on",
              check_release_keys.main(env={check_release_keys.OVERRIDE: "1"}) == 0
              and check_release_keys.main(env={check_release_keys.OVERRIDE: "yes"}) == 1)
        check("build guard: with a key -> fine", check_release_keys.main(env={}, keys=(pub,)) == 0)

        # ---- a build with keys: only a release signed by one of them is offered
        S.PUBLIC_KEYS = (_ed_key(bytes(range(50, 82)))[2], pub)        # primary + backup; signed by the second
        good = sig_text()
        serve(good)
        st, info = run()
        check("valid signature: 'newer', the signature rides with the info", st == "newer"
              and isinstance(info, UpdateInfo) and info.signature == good, repr(st))
        ok, p = no_raise(updater.download, info, str(dest), allowed_hosts=hosts)
        check("...and it downloads", ok and sha_of(p) == sha, repr(p))
        if ok:
            Path(p).unlink()
        refused = [
            ("no .sig asset in the release", lambda: serve(None)),
            ("signed by a key the app does not trust", lambda: serve(sig_text(by=stranger))),
            ("a signature of another version (replay / downgrade)", lambda: serve(sig_text(version="9.8.0",
                                                                           name="AgentStart-Setup-9.8.0.exe"))),
            ("a signature of another size", lambda: serve(sig_text(size=len(body) + 1))),
            ("a signature of another sha256 (the installer was swapped)", lambda: serve(sig_text(sha256="cd" * 32))),
            ("a signature made for another repository", lambda: serve(sig_text(repo="someone/else"))),
            ("a signature in another namespace", lambda: serve(sig_text(ns="file"))),
            ("a tampered signature", lambda: serve(good.replace(good[60], "A" if good[60] != "A" else "B", 1))),
            ("junk instead of a signature", lambda: serve("hello")),
            ("non-ASCII bytes", lambda: serve(b"\xff\xfe" * 40)),
            (".sig URL on another path", lambda: serve(good, browser_download_url=f"{base}/elsewhere.sig")),
            (".sig URL on another host", lambda: serve(good, browser_download_url=f"https://example.com{sig_route}")),
            (".sig not uploaded yet", lambda: serve(good, state="new")),
            (".sig listed larger than allowed", lambda: serve(good, size=S.MAX_SIG_BYTES + 1)),
            (".sig listed with size 0", lambda: serve(good, size=0)),
            (".sig listed with a size that is not a number", lambda: serve(good, size=True)),
            (".sig body longer than listed", lambda: serve(good, size=len(good) - 5)),
            (".sig body shorter than listed", lambda: serve(good, size=len(good) + 5)),
        ]
        ROUTES["/elsewhere.sig"] = {"body": good.encode()}       # a valid signature, but not where it must be
        for why, arrange in refused:
            arrange()
            st, info = run()
            check(f"{why}: 'unsigned', with the release page", st == "unsigned" and isinstance(info, UpdateInfo)
                  and info.signature == "" and info.page_url.startswith(f"https://github.com/{updater.REPO}/"),
                  repr(st))
            if isinstance(info, UpdateInfo):
                ROUTES.pop(CANON, None)          # the installer is not even asked for: a 404 would say so
                ok, e = raises_update_error(updater.download, info, str(dest), allowed_hosts=hosts)
                check(f"{why}: download refuses before any request", ok and "signature" in e, e)
        serve(good)
        ROUTES[sig_route] = {"status": 404, "body": b""}
        check(".sig answers 404: 'unsigned'", run()[0] == "unsigned")
        ROUTES[sig_route] = {"location": "https://example.com/x.sig"}
        check(".sig redirects to a host that is not allowed: 'unsigned'", run()[0] == "unsigned")
        check("check_latest() offers nothing for an unsigned release",
              updater.check_latest("1.6.0", api_url=f"{base}/signed") is None)

        # ---- the info is checked again at download time, on the values the download will use
        serve(good)
        st, info = run()
        for field, value in (("size", len(body) + 1), ("sha256", "ef" * 32), ("version", "9.9.1"),
                             ("signature", ""), ("signature", sig_text(by=stranger))):
            bad = UpdateInfo(**{**info.__dict__, field: value})
            ROUTES.pop(CANON, None)
            ok, e = raises_update_error(updater.download, bad, str(dest), allowed_hosts=hosts)
            check(f"info with another {field} than was signed: download refuses before any request",
                  ok and "signature" in e, e)
        serve(good)
        serve_json("/signed-old", release(base, tag="v1.0.0"))
        check("not newer: 'current' (no signature needed for that)",
              updater.check("1.6.0", api_url=f"{base}/signed-old") == ("current", None))
        check("nothing but finished files is left in the download folder",
              not dest.exists() or not list(dest.iterdir()), repr(list(dest.iterdir()) if dest.exists() else []))
    finally:
        S.PUBLIC_KEYS = saved_keys
        ROUTES.pop(sig_route, None)
        ROUTES.pop("/elsewhere.sig", None)
    check("the shipped key list is restored", S.PUBLIC_KEYS is saved_keys)


def test_cleanup():
    section("cleanup_old")
    d = TMP / "cleanup"
    d.mkdir()
    outside = TMP / "outside-AgentStart-Setup-1.0.0.exe"
    outside.write_bytes(b"keep me")
    names = ["AgentStart-Setup-1.5.0.exe", "AgentStart-Setup-1.6.0.exe", "AgentStart-Setup-1.6.exe",
             "AgentStart-Setup-1.7.0.exe", "AgentStart-Setup-1.6.0.exe.part", "AgentStart-Setup-2.0.0.exe.part",
             "evil.exe", "random.part", "AgentStart-Setup-1.0.0.exe.bak", "agentstart-setup-1.0.0.exe.txt",
             "AgentStart-Setup-1.0.0.exe.part.exe"]
    for n in names:
        (d / n).write_bytes(b"x")
    sub = d / "AgentStart-Setup-1.0.0.exe"          # a DIRECTORY with an asset-like name
    sub.mkdir()
    (sub / "AgentStart-Setup-1.0.0.exe").write_bytes(b"nested")
    link_made = False
    try:
        os.symlink(outside, d / "AgentStart-Setup-1.1.0.exe")
        link_made = True
    except OSError:
        print("  NOTE: cannot create symlinks here (no Developer Mode/admin); symlink case skipped")
    n = updater.cleanup_old(str(d), "1.6.0")
    left = sorted(p.name for p in d.iterdir())
    want_left = sorted(["AgentStart-Setup-1.7.0.exe", "evil.exe", "random.part", "AgentStart-Setup-1.0.0.exe.bak",
                        "agentstart-setup-1.0.0.exe.txt", "AgentStart-Setup-1.0.0.exe.part.exe",
                        "AgentStart-Setup-1.0.0.exe"] + (["AgentStart-Setup-1.1.0.exe"] if link_made else []))
    check("removes <= current + our .part files only", left == want_left, f"{left}")
    check("returns count (5)", n == 5, repr(n))
    check("did not recurse", (sub / "AgentStart-Setup-1.0.0.exe").exists())
    check("link target outside untouched", outside.read_bytes() == b"keep me")

    (d / "AgentStart-Setup-1.0.0.exe.part").write_bytes(b"x")
    (d / "AgentStart-Setup-1.5.0.exe").write_bytes(b"x")
    updater._download_lock.acquire()
    try:
        n = updater.cleanup_old(str(d), "1.6.0")
        check("while a download runs: .part kept, old exe removed",
              (d / "AgentStart-Setup-1.0.0.exe.part").exists() and not (d / "AgentStart-Setup-1.5.0.exe").exists()
              and n == 1, repr(n))
    finally:
        updater._download_lock.release()
    n = updater.cleanup_old(str(d), "banana")
    check("junk current version: exes kept, .part removed",
          n == 1 and (d / "AgentStart-Setup-1.7.0.exe").exists()
          and not (d / "AgentStart-Setup-1.0.0.exe.part").exists(), repr(n))
    with open(d / "AgentStart-Setup-1.7.0.exe", "rb") as held:      # a file in use: cannot delete, no raise
        held.read(1)
        ok, n = no_raise(updater.cleanup_old, str(d), "9.0")
        check("file in use -> skipped, no raise", ok and n == 0 and (d / "AgentStart-Setup-1.7.0.exe").exists(),
              repr(n))
    for label, args in (("missing dir", (str(TMP / "nope"), "1.0")), ("None dir", (None, "1.0")),
                        ("dir is a file", (str(d / "evil.exe"), "1.0")), ("None version", (str(d), None))):
        ok, r = no_raise(updater.cleanup_old, *args)
        check(f"{label} -> int, no raise", ok and isinstance(r, int), repr(r))
    shutil.rmtree(d, ignore_errors=True)
    outside.unlink()


def test_default_dir():
    section("default_download_dir")
    old = os.environ.get("LOCALAPPDATA")
    fake = TMP / "lad"
    os.environ["LOCALAPPDATA"] = str(fake)
    try:
        d = updater.default_download_dir()
        check("under %LOCALAPPDATA%/AgentStart/updates", Path(d) == fake / "AgentStart" / "updates", d)
        check("created", Path(d).is_dir())
    finally:
        if old is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old


def test_launch():
    section("launch_installer (recorder, nothing runs)")
    calls = []
    orig = updater._popen
    updater._popen = make_recorder(calls)
    try:
        dest = TMP / "launch"
        dest.mkdir(parents=True, exist_ok=True)
        good = dest / "AgentStart-Setup-9.9.0.exe"
        good.write_bytes(b"MZ" + os.urandom(3_000_000))
        sha = sha_of(good)
        real = os.path.realpath(good)
        updater.launch_installer(str(good), sha, str(dest))
        base_argv = [real, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"]
        check("_popen called once", len(calls) == 1, repr(calls))
        if calls:
            argv, kw = calls[0]
            check("exact argv (no extras)", argv == base_argv, repr(argv))
            check("argv is a list (no shell string)", isinstance(argv, list))
            check("no shell=True", not kw.get("shell"), repr(kw))
            check("close_fds=True", kw.get("close_fds") is True, repr(kw))
            check("DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP", kw.get("creationflags") == 0x8 | 0x200, repr(kw))
        idir = r"C:\Users\Some One\AppData\Local\Programs\Agent Start"
        lpath = r"C:\Users\Some One\AppData\Local\AgentStart\updates\setup log.txt"
        calls.clear()
        updater.launch_installer(str(good), sha.upper(), str(dest), install_dir=idir + "\\", log_path=lpath,
                                 wait_pid=4242)
        full = base_argv + ["/WAITPID=4242", f"/DIR={idir}", f"/LOG={lpath}"]
        check("full argv order: RELAUNCH, WAITPID, DIR, LOG (trailing sep stripped)",
              calls and calls[-1][0] == full, repr(calls[-1][0] if calls else None))
        cl = subprocess.list2cmdline(full)
        check("command line quotes the spaced /DIR and /LOG as whole args",
              f'"/DIR={idir}"' in cl and f'"/LOG={lpath}"' in cl, cl)
        calls.clear()
        updater.launch_installer(str(good), sha, str(dest), wait_pid=77)
        check("wait_pid only", calls and calls[-1][0] == base_argv + ["/WAITPID=77"], repr(calls))

        # handle is held (deny write/delete) while _popen runs
        probe = {}

        def during(argv):
            p = argv[0]
            for label, fn in (("open r+b", lambda: open(p, "r+b").close()),
                              ("open ab", lambda: open(p, "ab").close()),
                              ("remove", lambda: os.remove(p)),
                              ("rename", lambda: os.rename(p, p + ".moved"))):
                try:
                    fn()
                    probe[label] = "ALLOWED"
                except OSError as e:
                    probe[label] = type(e).__name__
            try:
                with open(p, "rb") as fr:
                    probe["read"] = hashlib.sha256(fr.read()).hexdigest() == sha
            except OSError as e:
                probe["read"] = type(e).__name__
        updater._popen = make_recorder(calls, during)
        updater.launch_installer(str(good), sha, str(dest))
        check("while held: write-open denied", probe.get("open r+b") == "PermissionError"
              and probe.get("open ab") == "PermissionError", repr(probe))
        check("while held: delete and rename denied", probe.get("remove") == "PermissionError"
              and probe.get("rename") == "PermissionError", repr(probe))
        check("while held: read-open still allowed (installer reads itself)", probe.get("read") is True, repr(probe))
        ok, _ = no_raise(lambda: open(good, "r+b").close())
        check("after return: handle released (write-open works)", ok)
        updater._popen = make_recorder(calls)

        n0 = len(calls)
        for label, s in (("sha256 None", None), ("sha256 empty", ""), ("sha256 malformed", "xyz"),
                         ("sha256 mismatch", "00" * 32), ("sha256 of other content", hashlib.sha256(b"MZ").hexdigest())):
            ok, msg = raises_update_error(updater.launch_installer, str(good), s, str(dest))
            check(f"refuses {label}", ok, msg)
        check("_popen not called on hash refusals", len(calls) == n0, repr(calls[n0:]))
        ok, _ = no_raise(lambda: open(good, "r+b").close())
        check("handle released after a mismatch", ok)

        for label, kw in (("wait_pid 0", {"wait_pid": 0}), ("wait_pid -1", {"wait_pid": -1}),
                          ("wait_pid True", {"wait_pid": True}), ("wait_pid '12'", {"wait_pid": "12"}),
                          ("relative install_dir", {"install_dir": "Programs\\Agent Start"}),
                          ("install_dir with a quote", {"install_dir": 'C:\\a" /LOG=C:\\x'}),
                          ("install_dir with newline", {"install_dir": "C:\\a\nb"}),
                          ("empty log_path", {"log_path": ""})):
            ok, msg = raises_update_error(updater.launch_installer, str(good), sha, str(dest), **kw)
            check(f"refuses {label}", ok, msg)
        check("_popen not called for arg refusals", len(calls) == n0, repr(calls[n0:]))

        outside = TMP / "AgentStart-Setup-9.9.0.exe"
        outside.write_bytes(b"MZ")
        wrong = dest / "evil.exe"
        wrong.write_bytes(b"MZ")
        nested = dest / "sub"
        nested.mkdir(exist_ok=True)
        (nested / "AgentStart-Setup-9.9.0.exe").write_bytes(b"MZ")
        ms = hashlib.sha256(b"MZ").hexdigest()
        for label, p in (("outside dest_dir", outside), ("wrong name", wrong),
                         ("sub-folder of dest_dir", nested / "AgentStart-Setup-9.9.0.exe"),
                         ("missing file", dest / "AgentStart-Setup-9.9.1.exe"),
                         ("dot-dot escape", dest / ".." / "AgentStart-Setup-9.9.0.exe"),
                         (".part file", dest / "AgentStart-Setup-9.9.0.exe.part")):
            ok, msg = raises_update_error(updater.launch_installer, str(p), ms, str(dest))
            check(f"refuses {label}", ok, msg)
        check("_popen not called for refusals", len(calls) == n0, repr(calls[n0:]))

        old = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(TMP / "lad2")
        try:
            ok, msg = raises_update_error(updater.launch_installer, str(good), sha)
            check("default dest_dir = default_download_dir(); file elsewhere refused", ok, msg)
            dd = Path(updater.default_download_dir())
            inst = dd / "AgentStart-Setup-9.9.0.exe"
            inst.write_bytes(b"MZ")
            ok, r = no_raise(updater.launch_installer, str(inst), ms)
            check("file in default dir accepted", ok and calls[-1][0][0] == os.path.realpath(inst), repr(r))
        finally:
            if old is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old

        for exc in (OSError("nope"), RuntimeError("weird")):
            def boom(argv, _e=exc, **kw):
                raise _e
            updater._popen = boom
            ok, msg = raises_update_error(updater.launch_installer, str(good), sha, str(dest))
            check(f"Popen {type(exc).__name__} -> UpdateError", ok, msg)
        ok, _ = no_raise(lambda: open(good, "r+b").close())
        check("handle released after a Popen failure", ok)
    finally:
        updater._popen = orig
    check("_popen restored", updater._popen is orig)


ISS = r"""
[Setup]
AppId={appid}
AppName=AgentStartUpdaterSelfTest
AppVersion=9.9.1
DefaultDirName={default_dir}
PrivilegesRequired=lowest
Uninstallable=no
CreateAppDir=yes
UsePreviousAppDir=no
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableWelcomePage=yes
DisableFinishedPage=yes
CloseApplications=no
RestartApplications=no
OutputDir={out_dir}
OutputBaseFilename=AgentStart-Setup-9.9.1
Compression=lzma2/fast

[Files]
Source: "{payload}"; DestDir: "{{app}}"

[Code]
const OutFile = '{out_file}';

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  SaveStringToFile(OutFile, 'WAITPID=' + ExpandConstant('{{param:WAITPID|0}}') + #13#10, True);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SaveStringToFile(OutFile, 'APP=' + ExpandConstant('{{app}}') + #13#10 +
                     'RELAUNCH=' + ExpandConstant('{{param:RELAUNCH|missing}}') + #13#10, True);
end;
"""


def test_real_inno():
    section("REAL tiny Inno Setup installer via launch_installer (/DIR + /LOG with spaces, handle held)")
    if os.environ.get("AGENTSTART_TEST_NO_INNO") == "1":
        print("  NOTE: skipped (AGENTSTART_TEST_NO_INNO=1)")
        return
    iscc = next((p for p in ISCC_CANDIDATES if p.is_file()), None)
    if iscc is None:
        print("  NOTE: ISCC.exe not found; skipped")
        return
    import winreg
    root = TMP / "inno test"
    out_dir = root / "dl dir"
    install_dir = root / "Programs" / "Agent Start Test"
    log_dir = root / "log dir"
    for p in (out_dir, log_dir):
        p.mkdir(parents=True)
    payload = root / "payload.txt"
    payload.write_text("hello from the tiny installer\n")
    out_file = root / "out.txt"
    appid = f"AgentStartUpdaterSelfTest{uuid.uuid4().hex[:12]}"
    iss = root / "tiny.iss"
    iss.write_text(ISS.format(appid=appid, default_dir=str(root / "default app"), out_dir=str(out_dir),
                              payload=str(payload), out_file=str(out_file)), encoding="utf-8")
    cp = subprocess.run([str(iscc), "/Q", str(iss)], capture_output=True, text=True, timeout=120,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    exe = out_dir / "AgentStart-Setup-9.9.1.exe"
    if not check("tiny installer compiled", cp.returncode == 0 and exe.is_file(), cp.stdout[-500:] + cp.stderr[-500:]):
        return
    sha = sha_of(exe)
    log_path = log_dir / "setup log.txt"
    seen = {}

    def real_popen_and_wait(argv, **kw):
        seen["argv"] = list(argv)
        seen["cmdline"] = subprocess.list2cmdline(argv)
        proc = subprocess.Popen(argv, **kw)          # our own throwaway installer
        time.sleep(0.5)
        try:
            open(argv[0], "r+b").close()
            seen["write_while_held"] = "ALLOWED"
        except OSError as e:
            seen["write_while_held"] = type(e).__name__
        try:
            seen["rc"] = proc.wait(120)               # launch_installer still holds the handle meanwhile
        except subprocess.TimeoutExpired:
            proc.kill()
            seen["rc"] = "timeout (killed)"
        return proc

    orig = updater._popen
    updater._popen = real_popen_and_wait
    t0 = time.monotonic()
    try:
        ok, r = no_raise(updater.launch_installer, str(exe), sha, str(out_dir), install_dir=str(install_dir),
                         log_path=str(log_path), wait_pid=os.getpid())
    finally:
        updater._popen = orig
    dt = time.monotonic() - t0
    print(f"  argv: {seen.get('argv')}")
    print(f"  cmdline: {seen.get('cmdline')}")
    print(f"  installer exit code {seen.get('rc')!r} after {dt:.1f}s; write-open while held: "
          f"{seen.get('write_while_held')}")
    check("launch_installer returned without error", ok, repr(r))
    check("installer exited 0 while our deny-write handle was held (it could read itself)", seen.get("rc") == 0,
          repr(seen.get("rc")))
    check("write-open of the exe denied while held", seen.get("write_while_held") == "PermissionError",
          repr(seen.get("write_while_held")))
    check("argv order", seen.get("argv") == [os.path.realpath(exe), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                             "/RELAUNCH=1", f"/WAITPID={os.getpid()}", f"/DIR={install_dir}",
                                             f"/LOG={log_path}"], repr(seen.get("argv")))
    check("installed into the /DIR path with spaces", (install_dir / "payload.txt").is_file(),
          repr(list(root.rglob("payload.txt"))))
    check("default dir NOT used", not (root / "default app").exists())
    check("/LOG file with spaces written", log_path.is_file() and log_path.stat().st_size > 0)
    out = out_file.read_text(errors="replace") if out_file.is_file() else ""
    print("  out.txt: " + out.replace("\r\n", " | ").strip())
    check("{app} == /DIR value", f"APP={install_dir}" in out.splitlines(), out)
    check("{param:RELAUNCH} == 1", "RELAUNCH=1" in out.splitlines(), out)
    check("{param:WAITPID} arrived intact in PrepareToInstall", f"WAITPID={os.getpid()}" in out.splitlines(), out)
    if log_path.is_file():
        lt = log_path.read_text(errors="replace")
        check("log records the /DIR install", str(install_dir) in lt, lt[-300:])
    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{appid}_is1"
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, key))
        present = True
    except FileNotFoundError:
        present = False
    check("no HKCU Uninstall key left for the test AppId (Uninstallable=no)", not present, key)
    leftovers = []
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall") as k:
        i = 0
        while True:
            try:
                n = winreg.EnumKey(k, i)
            except OSError:
                break
            if "AgentStartUpdaterSelfTest" in n:
                leftovers.append(n)
            i += 1
    check("no AgentStartUpdaterSelfTest* Uninstall keys at all", not leftovers, repr(leftovers))
    shutil.rmtree(root, ignore_errors=True)
    check("inno temp dir removed", not root.exists())


def test_real_github():
    section("real GitHub (one read-only GET)")
    updater._ALLOW_LOCAL_HTTP_FOR_TESTS = False
    if os.environ.get("AGENTSTART_TEST_OFFLINE") == "1":
        print("  NOTE: skipped (AGENTSTART_TEST_OFFLINE=1)")
        return
    ok, r = no_raise(updater.check, "0.0.1")
    check("check('0.0.1') does not raise", ok, repr(r))
    print(f"  result: {r!r}")
    if not ok:
        return
    st, info = r
    check("status is one of newer/current/error", st in ("newer", "current", "error"), st)
    if st != "newer":
        print("  NOTE: not 'newer' (offline, rate-limited, no release, or the release has no sha256 digest)"
              " - not counted as a failure")
        return
    check("real asset name matches version", info.asset_name == f"AgentStart-Setup-{info.version}.exe")
    check("real asset URL is the exact canonical URL",
          info.asset_url == f"https://github.com/{updater.REPO}/releases/download/{info.tag}/{info.asset_name}",
          info.asset_url)
    check("real size plausible (> 1 MB)", info.size > 1_000_000, str(info.size))
    check("real sha256 present", bool(info.sha256) and len(info.sha256) == 64, repr(info.sha256))


def main() -> int:
    import update_sig
    shipped = update_sig.PUBLIC_KEYS
    check("the shipped release keys: a tuple of valid 32-byte keys (none, or primary + backup)",
          isinstance(shipped, tuple) and len(shipped) in (0, 2) and len(set(shipped)) == len(shipped)
          and all(update_sig.enabled((k,)) for k in shipped), repr(len(shipped)))
    srv, base = start_server()
    saved_base = updater._DOWNLOAD_BASE
    updater._DOWNLOAD_BASE = base
    update_sig.PUBLIC_KEYS = ()                  # everything below except test_signature: a build without keys
    try:
        for fn in (test_versions, test_due, test_url_ok, lambda: test_check(base), lambda: test_download(base),
                   lambda: test_signature(base), test_cleanup, test_default_dir, test_launch, test_real_inno):
            try:
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{getattr(fn, '__name__', 'test')} raised", False, "exception")
        updater._DOWNLOAD_BASE = saved_base
        check("_DOWNLOAD_BASE restored to https://github.com", updater._DOWNLOAD_BASE == "https://github.com")
        try:
            test_real_github()
        except Exception:
            traceback.print_exc()
            check("test_real_github raised", False, "exception")
    finally:
        update_sig.PUBLIC_KEYS = shipped
        updater._DOWNLOAD_BASE = saved_base
        updater._ALLOW_LOCAL_HTTP_FOR_TESTS = False
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(TMP, ignore_errors=True)
        check("temp dir removed", not TMP.exists(), str(TMP))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
