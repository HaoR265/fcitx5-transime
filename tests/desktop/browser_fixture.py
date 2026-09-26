#!/usr/bin/python3
"""Serve a local empty textarea with input-event observations. No browser fill."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("Prepared only; --execute starts a loopback-only empty browser test page.")
        return
    token = secrets.token_hex(16)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    title = json.dumps(args.marker)
    page = ("<!doctype html><meta charset=utf-8><title></title>"
            "<style>body{font:20px sans-serif;margin:40px}textarea{width:90%;height:220px}</style>"
            "<p>TransIME dedicated input test. This page does not send messages.</p>"
            "<textarea id=e autofocus></textarea><script>"
            f"document.title={title};const e=document.getElementById('e');"
            "let compositionEvents=0,inputEvents=0;"
            "e.addEventListener('compositionstart',()=>compositionEvents++);"
            "e.addEventListener('input',()=>inputEvents++);"
            "setInterval(()=>fetch(location.pathname,{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({text:e.value,active:document.hasFocus()&&document.activeElement===e,"
            "composition_events:compositionEvents,input_events:inputEvents})}),100);</script>").encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path != "/" + token:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if self.path != "/" + token or not 0 < size <= 16384:
                self.send_error(400)
                return
            try:
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict) or not isinstance(value.get("text"), str) or len(value["text"]) > 4096:
                    raise ValueError("invalid synthetic state")
            except (ValueError, UnicodeError):
                self.send_error(400)
                return
            value.update(marker=args.marker, kind="browser", monotonic=time.monotonic())
            temporary = args.state.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            temporary.replace(args.state)
            self.send_response(204)
            self.end_headers()

    # Serialize reports so overlapping browser fetches cannot race on .tmp.
    server = HTTPServer(("127.0.0.1", 0), Handler)
    metadata = {"server_pid": os.getpid(), "url": f"http://127.0.0.1:{server.server_port}/{token}",
                "marker": args.marker}
    args.state.with_suffix(".server.json").write_text(json.dumps(metadata), encoding="utf-8")
    print(metadata["url"], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
