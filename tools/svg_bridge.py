#!/usr/bin/env python3
"""Serve a directory and accept rendered images back from the page.

The SVG rig is posed and rasterized in a browser canvas (the only renderer
guaranteed to be present), then POSTed here as a data URL so the build can
write real files. Loopback only.

  python svg_bridge.py <serve-dir> [port]
  POST /save?name=atlas.png   body: data:image/png;base64,...
"""
import base64
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8763


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/save"):
            self.send_error(404)
            return
        name = "out.png"
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if kv.startswith("name="):
                    name = os.path.basename(kv[5:]) or name
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", "replace")
        if "," in body:
            body = body.split(",", 1)[1]
        try:
            data = base64.b64decode(body)
            with open(os.path.join(ROOT, name), "wb") as f:
                f.write(data)
            msg = f"wrote {name} ({len(data)} bytes)"
        except Exception as e:
            msg = f"error: {e}"
        print(msg, flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"svg_bridge serving {ROOT} on 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
