import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from pii_router import detectors


def test_parse_answer():
    assert detectors.parse_llm_answer('```json\n{"pii": [{"text": "Masha", "type": "person"}]}\n```') == [("Masha", "PERSON")]
    assert detectors.parse_llm_answer("no json") == []
    assert detectors.parse_llm_answer('{"pii": ["x1"]}') == [("x1", "PRIVATE")]


def test_remote_needs_permission():
    d = detectors.make("llm", {"base_url": "http://192.168.1.5:4000/v1", "model": "m"}, background=False)
    assert not d.ok and "allow-remote" in d.error


def test_llm_detector_with_instruction():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["system"] = body["messages"][0]["content"]
            answer = {"pii": [{"text": "Masha", "type": "PERSON"}, {"text": "Falcon", "type": "PROJECT"}]}
            data = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = detectors.make("llm", {"base_url": f"http://127.0.0.1:{srv.server_port}/v1", "model": "m",
                                   "instruction": "Project code names are PII."}, background=False)
        assert d.ok, d.error
        text = "My wife Masha works on Falcon. Masha!"
        spans = d.find(text)
        assert [text[s:e] for s, e, _ in spans].count("Masha") == 2
        assert ("PRIVATE" in {k for *_, k in spans})            # PROJECT isn't an allowed type
        assert "Project code names are PII." in seen["system"]
    finally:
        srv.shutdown()


def test_missing_package_is_unavailable():
    d = detectors.make("gliner", {}, background=False)
    assert d.ok or d.error                                       # either installed or a clear reason
