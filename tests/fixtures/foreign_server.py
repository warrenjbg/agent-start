"""A non-Strata HTTP listener used to occupy a test port (cmdline does NOT contain the Strata marker)."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _send(self):
        body = json.dumps({"service": "other"}).encode()
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


Server(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
