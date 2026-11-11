"""Fake Strata server for tests (copied to <tempdir>\\serve\\server.py so its cmdline matches Strata identity).

Mirrors the bits of Strata 0.2.10 serve/server.py that EndpointManager relies on:
GET /health, GET /v1/models, POST /unload[?wait=s], POST /load; POSTs need Content-Type application/json and
no Origin header. Exits immediately if the port is taken.

GET /metrics mirrors Strata's Monitor document (engine, live, requests newest-first - only the last 12 unless
?requests=all -, totals, hardware, time) and, like Strata, answers Prometheus text instead when the Accept header
mentions text/plain or openmetrics or the query has format=prometheus. Test hooks (not in Strata):
GET /_fake/add?n=3 finishes n more requests, GET /_fake/live?state=generating|reading|idle sets the live block.
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
ap.add_argument("--metrics-requests", type=int, default=0, help="finished requests /metrics starts with")
ap.add_argument("--metrics-live", default="idle", help="live.state: idle | generating | reading")
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


GPU_TOTAL = 16 * 1024 ** 3
history: list[dict] = []          # finished requests, oldest first (as Strata keeps them)
totals = {"requests": 0, "prompt_tokens": 0, "reused": 0, "output_tokens": 0, "prompt_ms": 0.0, "decode_ms": 0.0}
live_state = {"state": args.metrics_live, "read": 0}


def finish_request() -> dict:
    """Append one finished request with Strata 0.2.10's per-request fields (deterministic, varied)."""
    i = len(history)
    cache_hit = i % 4 == 3                       # every 4th request re-uses the whole prompt (tiny read)
    prompt_total = 1800 + 350 * (i % 7)
    prompt_read = 12 if cache_hit else prompt_total - 200 * (i % 3)
    prompt_ms = 9.0 if cache_hit else round(prompt_read / (3400.0 + 60 * (i % 5)) * 1000, 1)
    generated = 0 if i % 9 == 8 else 120 + 40 * (i % 6)      # an occasional answer with no decode figure
    decode_tok_s = round(92.0 + 3.5 * ((i * 7) % 6), 1) if generated >= 2 else None
    decode_ms = round(generated / decode_tok_s * 1000, 1) if decode_tok_s else None
    rec = {"id": f"req{i:09d}", "projection": None, "time": time.time() - 1.0, "duration_s": 2.4,
           "finish": "stop", "prompt_tokens": prompt_total, "reused": prompt_total - prompt_read,
           "output_tokens": generated, "first_engine_token_s": 0.4, "prompt_total": prompt_total,
           "prompt_read": prompt_read, "reasoning_recoveries": 0, "reasoning_repeat_coverage": 0.0,
           "engine_generated": generated, "prompt_ms": prompt_ms, "decode_ms": decode_ms,
           "decode_tok_s": decode_tok_s, "hit_rate": 0.93, "pcie_share": 0.5, "ram_blobs": 0, "file_blobs": 0,
           "file_mb": 0.0, "drafts_offered": None, "drafts_accepted": None}
    history.append(rec)
    totals["requests"] += 1
    totals["prompt_tokens"] += prompt_total
    totals["reused"] += prompt_total - prompt_read
    totals["output_tokens"] += generated
    totals["prompt_ms"] += prompt_ms
    totals["decode_ms"] += decode_ms or 0.0
    return rec


def metrics_doc(all_requests: bool) -> dict:
    st = live_state["state"] if state["loaded"] else "unloaded"
    busy = st in ("generating", "reading")
    live = {"state": st, "queued": 0, "phase": None, "prompt_tokens": 2400 if busy else None,
            "prompt_read": None, "prompt_total": None, "generated": 57 if st == "generating" else None,
            "max_tokens": 4096 if busy else None, "elapsed_s": 1.2 if busy else None,
            "tok_s": 101.3 if st == "generating" else None, "tok_s_mean": 99.8 if st == "generating" else None,
            "prefill_tok_s_mean": 3650.0 if busy else None, "tok_s_window_s": 2.0 if st == "generating" else None}
    if st == "reading":
        live_state["read"] = min(2400, live_state["read"] + 900)
        live["prompt_read"], live["prompt_total"] = live_state["read"], 2400
    gpu_used = (11 if state["loaded"] else 2) * 1024 ** 3 + 300 * 1024 ** 2
    with lock:
        hist = list(history)
        tot = dict(totals)
    return {"engine": {"model": args.model, "max_context": 262144, "images": True},
            "live": live, "requests": hist[::-1][:None if all_requests else 12],
            "conversation_cache": {}, "requests_kept": len(hist), "totals": tot,
            "hardware": {"gpu_util": 71, "gpu_mem_used": gpu_used, "gpu_mem_total": GPU_TOTAL, "gpu_temp": 61,
                         "gpu_power": 212.0, "cpu": 23.5, "ram_used": 41 * 1024 ** 3, "ram_total": 64 * 1024 ** 3},
            "hardware_static": {"gpu_name": "Fake RTX", "gpu_count": 1},
            "history": {}, "time": time.time()}


def wants_prometheus(accept: str, query: str) -> bool:
    return "format=prometheus" in (query or "") or "openmetrics" in (accept or "") or "text/plain" in (accept or "")


for _ in range(max(0, args.metrics_requests)):
    finish_request()


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
        elif path == "/metrics":
            query = urlparse(self.path).query
            if wants_prometheus(self.headers.get("Accept", ""), query):
                body = f"# TYPE vllm:request_success_total counter\nvllm:request_success_total {len(history)}\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(200, metrics_doc("requests=all" in self.path))
        elif path == "/_fake/add":
            try:
                n = int(parse_qs(urlparse(self.path).query).get("n", ["1"])[0])
            except ValueError:
                n = 1
            with lock:
                for _ in range(max(0, min(n, 200))):
                    finish_request()
            self._send(200, {"requests_kept": len(history)})
        elif path == "/_fake/live":
            live_state["state"] = parse_qs(urlparse(self.path).query).get("state", ["idle"])[0]
            live_state["read"] = 0
            self._send(200, {"state": live_state["state"]})
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
