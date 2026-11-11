"""Fake Strata server for tests (copied to <tempdir>\\serve\\server.py so its cmdline matches Strata identity).

Mirrors the bits of Strata 0.2.10 serve/server.py that EndpointManager relies on:
GET /health, GET /v1/models, POST /unload[?wait=s], POST /load; POSTs need Content-Type application/json and
no Origin header. Exits immediately if the port is taken.
"""
import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--model", default="fake-model")
ap.add_argument("--load-delay", type=float, default=0.0, help="seconds before loaded=true (or /load duration)")
ap.add_argument("--exit-early", action="store_true", help="exit with code 3 before binding")
ap.add_argument("--busy-unload", action="store_true", help="a turn is always running: /unload -> 409")
ap.add_argument("--busy-wait-cap", type=float, default=1e9, help="cap on honoured ?wait seconds when busy")
ap.add_argument("--unload-delay", type=float, default=0.0)
ap.add_argument("--never-ready", action="store_true", help="loaded stays false; /load -> 503")
ap.add_argument("--lazy", action="store_true", help="loaded stays false until POST /load")
ap.add_argument("--record", default="", help="append one line per request to this file")
args, _unknown = ap.parse_known_args()

if args.exit_early:
    print("fake strata: exiting early", flush=True)
    sys.exit(3)

state = {"loaded": False}
lock = threading.Lock()


def record(line: str) -> None:
    if args.record:
        with lock, open(args.record, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def eager_load() -> None:
    time.sleep(args.load_delay)
    state["loaded"] = True
    print("fake strata: model loaded", flush=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        print("fake strata:", fmt % a, flush=True)

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        record(f"GET {self.path}")
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"status": "ok", "loaded": state["loaded"], "model": args.model,
                             "service": "strata"})
        elif path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": args.model, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        record(f"POST {self.path}")
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        if self.headers.get("Origin"):
            return self._send(403, {"error": "origin not allowed"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._send(415, {"error": "content-type must be application/json"})
        u = urlparse(self.path)
        if u.path == "/unload":
            if args.busy_unload:
                try:
                    wait = float(parse_qs(u.query).get("wait", ["0"])[0])
                except ValueError:
                    wait = 0.0
                time.sleep(max(0.0, min(wait, args.busy_wait_cap)))
                return self._send(409, {"status": "busy"})
            time.sleep(args.unload_delay)
            state["loaded"] = False
            return self._send(200, {"status": "unloaded"})
        if u.path == "/load":
            if args.never_ready:
                return self._send(503, {"status": "error"})
            time.sleep(args.load_delay)
            state["loaded"] = True
            return self._send(200, {"status": "loaded"})
        return self._send(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    allow_reuse_address = False   # on Windows SO_REUSEADDR would let us steal a busy port
    daemon_threads = True


try:
    srv = Server(("127.0.0.1", args.port), H)
except OSError as e:
    print(f"fake strata: port {args.port} in use: {e}", flush=True)
    sys.exit(2)
print(f"fake strata listening on {args.port}", flush=True)
if not (args.lazy or args.never_ready):
    threading.Thread(target=eager_load, daemon=True).start()
srv.serve_forever()
