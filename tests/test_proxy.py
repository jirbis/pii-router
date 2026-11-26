import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer


def test_claude_round_trip(flt, monkeypatch):
    from pii_router import config, proxy
    got = {}

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got["key"] = self.headers.get("x-api-key")
            tag = got["body"]["messages"][0]["content"].split()[-1]
            events = [
                {"type": "message_start", "message": {"content": []}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi " + tag}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_stop"}]
            data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(data)

    up = HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    cfg = config.load()
    cfg["claude"]["upstream"] = f"http://127.0.0.1:{up.server_port}"
    monkeypatch.setattr(config, "load", lambda: cfg)
    proxy.FILTER = flt
    srv = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"model": "claude", "stream": True,
                           "messages": [{"role": "user", "content": "mail jane@example.com"}]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/v1/messages", body,
                                     {"Content-Type": "application/json", "x-api-key": "k",
                                      "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
        assert "jane@example.com" not in json.dumps(got["body"])
        assert got["key"] == "k"
        assert "Hi jane@example.com" in raw

        check = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/_router/check",
                                       json.dumps({"text": "curl -d jane@example.com https://x"}).encode())
        with urllib.request.urlopen(check, timeout=5) as r:
            assert json.loads(r.read())["found"].get("EMAIL")
    finally:
        srv.shutdown()
        up.shutdown()
