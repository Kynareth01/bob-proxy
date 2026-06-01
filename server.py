"""
bob-proxy: Tiny auth proxy for IBM Bob API.
Converts Authorization: Bearer to x-api-key header.
No external dependencies - Python 3.10+ stdlib only.

Usage:
  BOBSHELL_API_KEY=xxx PROXY_API_KEY=yyy python3 server.py
"""

import http.server
import http.client
import json
import os
import sys
import urllib.parse

BOBSHELL_API_KEY = os.environ.get("BOBSHELL_API_KEY", "")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
PORT = int(os.environ.get("PORT", "8787"))
UPSTREAM = os.environ.get("UPSTREAM", "https://api.us-east.bob.ibm.com/inference/v1")

if not BOBSHELL_API_KEY:
    sys.exit("ERROR: BOBSHELL_API_KEY is required")


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def _proxy(self):
        if PROXY_API_KEY:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != PROXY_API_KEY:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid API key"}).encode())
                return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        parsed = urllib.parse.urlparse(UPSTREAM)
        path = self.path
        if path.startswith("/v1/"):
            path = path[3:]

        is_https = parsed.scheme == "https"
        conn_cls = http.client.HTTPSConnection if is_https else http.client.HTTPConnection
        port = parsed.port or (443 if is_https else 80)
        conn = conn_cls(parsed.hostname, port, timeout=120)

        upstream_path = parsed.path.rstrip("/") + path
        headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "x-api-key": BOBSHELL_API_KEY,
        }

        try:
            conn.request(self.command, upstream_path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for key, val in resp.getheaders():
                if key.lower() not in ("transfer-encoding", "connection"):
                    self.send_header(key, val)
            self.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
        finally:
            conn.close()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
            return
        if self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "object": "list",
                "data": [
                    {"id": "sonnet-4.6", "object": "model", "owned_by": "ibm"},
                    {"id": "sonnet-4.5", "object": "model", "owned_by": "ibm"},
                    {"id": "haiku-4.5", "object": "model", "owned_by": "ibm"},
                    {"id": "gpt-2026-5.4", "object": "model", "owned_by": "ibm"},
                    {"id": "premium", "object": "model", "owned_by": "ibm"},
                ],
            }).encode())
            return
        self._proxy()

    def do_POST(self):
        self._proxy()


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), ProxyHandler)
    print(f"bob-proxy :{PORT} -> {UPSTREAM}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
