"""Loopback-only canvas fixture server with an independent append-only receipt.

Usage: python tools/serve_complex_web018.py --port 8768
No browser, GUI input, configuration changes, or outbound network requests.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit


REPOSITORY = Path(__file__).resolve().parents[1]
PAGE = REPOSITORY / "fixtures" / "ComplexWeb018.html"
MAX_BODY = 65536


def create_server(port: int, receipt: Path) -> ThreadingHTTPServer:
    receipt = receipt.absolute()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    page = PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalCanvasFixture/1"

        def log_message(self, *_):
            pass

        def send(self, status, data, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def local_request(self):
            expected = "127.0.0.1:" + str(self.server.server_port)
            return self.headers.get("Host") == expected and self.client_address[0] == "127.0.0.1"

        def do_GET(self):
            if not self.local_request():
                self.send(403, b'{"error":"loopback_host_required"}')
                return
            route = urlsplit(self.path).path
            if route in {"/", "/ComplexWeb018.html"}:
                self.send(200, page, "text/html; charset=utf-8")
            elif route == "/health":
                self.send(200, b'{"ok":true,"fixture":"complex-canvas-018","audit":"local-jsonl"}')
            else:
                self.send(404, b'{"error":"not_found"}')

        def do_POST(self):
            origin = "http://127.0.0.1:" + str(self.server.server_port)
            if not self.local_request() or self.headers.get("Origin") != origin:
                self.send(403, b'{"error":"same_origin_required"}')
                return
            if urlsplit(self.path).path != "/audit":
                self.send(404, b'{"error":"not_found"}')
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY or self.headers.get_content_type() != "application/json":
                    raise ValueError("invalid body")
                event = json.loads(self.rfile.read(size))
                if (not isinstance(event, dict) or not isinstance(event.get("session"), str)
                        or len(event["session"]) > 80 or type(event.get("sequence")) is not int
                        or event["sequence"] < 1 or not isinstance(event.get("event"), str)
                        or len(event["event"]) > 80 or type(event.get("effect")) is not bool
                        or not isinstance(event.get("field_values"), dict)):
                    raise ValueError("invalid receipt")
                envelope = {"received_at": datetime.now(timezone.utc).isoformat(), "source": "canvas-user-event", "data": event}
                line = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n"
                with lock:
                    with receipt.open("a", encoding="utf-8", newline="\n") as output:
                        output.write(line)
                        output.flush()
                self.send(200, b'{"ok":true}')
            except (ValueError, UnicodeError, json.JSONDecodeError):
                self.send(400, b'{"error":"invalid_receipt"}')
            except OSError:
                self.send(500, b'{"error":"receipt_write_failed"}')

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--receipt", type=Path, default=REPOSITORY / ".data" / "complex-web018" / "receipts.jsonl")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    server = create_server(args.port, args.receipt)
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/", "receipt": str(args.receipt.absolute())}), flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
