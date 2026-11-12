"""ANOTHER project's serve\\server.py (copied OUTSIDE strata_dir): an HTTP server whose cmdline matches the Strata
marker but whose /health says service "other". Records every request line to --record (if given), so a test can
prove no POST /unload reached it.  Usage: python <dir>\\serve\\server.py --port N [--record FILE]
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--record", default="")
args, _ = ap.parse_known_args()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _send(self):
        if args.record:
            with open(args.record, "a", encoding="utf-8") as f:
                f.write(f"{self.command} {self.path}\n")
        body = json.dumps({"status": "ok", "service": "other-project", "loaded": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _send

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self._send()


class Server(HTTPServer):
    allow_reuse_address = False


Server(("127.0.0.1", args.port), H).serve_forever()
