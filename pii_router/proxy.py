"""The local proxy on 127.0.0.1:<port>.

  /v1/messages, /v1/messages/count_tokens   Claude Code (ANTHROPIC_BASE_URL)
                                             -> Anthropic, filtered
  /v1/responses, /v1/chat/completions, ...   Codex / ChatGPT app (openai_base_url)
      local models        -> the local server (Mac Intel Local AI, Ollama, llama.cpp), unfiltered
      network models      -> your LLM gateway, unfiltered (it's yours)
      GPT, :cloud models  -> OpenAI / Ollama Cloud, filtered
  /_router/status, /_router/sent, /_router/debug/<minutes>   local status and debug view
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import anthropic_api, config, openai_api
from .filter import PiiFilter, now_ms, summary

MARK = "PII Router"
ACTIVE = Path.home() / "Library/Application Support/Mac Intel Local AI/active.json"   # optional
ZSTD = next((p for p in ("/usr/local/bin/zstd", "/opt/homebrew/bin/zstd", "/usr/bin/zstd") if os.path.exists(p)), None)
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
FILTER: PiiFilter | None = None


def decode_body(body: bytes, encoding: str | None) -> bytes | None:
    """The request body before compression (Codex uses zstd), or None if unreadable."""
    enc = (encoding or "").strip().lower()
    if not enc or enc == "identity":
        return body
    if enc == "gzip":
        import gzip
        return gzip.decompress(body)
    if enc == "deflate":
        import zlib
        return zlib.decompress(body)
    if enc == "br":
        try:
            import brotli
            return brotli.decompress(body)
        except ImportError:
            return None
    if enc == "zstd" and ZSTD:
        r = subprocess.run([ZSTD, "-d", "-c", "-q"], input=body, capture_output=True)
        return r.stdout if r.returncode == 0 else None
    return None


def tls_context():
    """Verify HTTPS like macOS does (Keychain roots, e.g. a home CA); else Python's CA list."""
    import ssl
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


TLS = tls_context()

# ---------------------------------------------------------------- debug view (memory only)

DEBUG = {"until": 0.0, "sent": []}
DEBUG_LOCK = threading.Lock()


def debug_on() -> bool:
    return time.time() < DEBUG["until"]


def texts_of(payload: dict) -> list:
    """(item, text, note) of a request as sent, for the debug view."""
    out = []

    def add(label, content, role=None):
        texts, other = [], []
        for c in content if isinstance(content, list) else [content]:
            if isinstance(c, str):
                texts.append(c)
            elif isinstance(c, dict):
                if isinstance(c.get("text"), str):
                    texts.append(c["text"])
                elif c.get("type") == "tool_result":
                    add(label + "/tool_result", c.get("content"))
                elif c.get("type") == "tool_use":
                    texts.append(json.dumps(c.get("input"), ensure_ascii=False))
                elif c.get("type") in ("thinking", "redacted_thinking"):
                    continue
                else:
                    other.append(f"{c.get('type', '?')} (NOT FILTERED)")
        text = "\n".join(texts)
        if role in ("developer", "system"):
            text = text[:200] + (f" ... [{len(text)} chars of instructions]" if len(text) > 200 else "")
        elif len(text) > 8000:
            text = text[:8000] + f" ... [{len(text) - 8000} more chars]"
        if text or other:
            out.append({"item": label, "text": text, **({"attachments": other} if other else {})})

    if isinstance(payload.get("messages"), list) and "input" not in payload:
        for m in payload["messages"]:
            if isinstance(m, dict):
                add(f"message/{m.get('role')}", m.get("content"), m.get("role"))
        return out
    inp = payload.get("input")
    for item in inp if isinstance(inp, list) else [{"role": "user", "content": inp}]:
        if isinstance(item, dict):
            kind, role = item.get("type", "message"), item.get("role", "-")
            content = item.get("content") if "content" in item else item.get("output", item.get("arguments"))
            add(f"{kind}/{role}", content, role)
    return out


def capture(api: str, model, stats: dict, payload: dict, shape_text: str) -> None:
    with DEBUG_LOCK:
        DEBUG["sent"].append({"time": time.strftime("%H:%M:%S"), "api": api, "model": model,
                              "found": {k: v for k, v in stats.items() if not k.startswith("_")},
                              "input": shape_text, "items": texts_of(payload)})
        del DEBUG["sent"][:-3]


# ---------------------------------------------------------------- Codex routing

def network(cfg: dict) -> dict:
    return cfg["codex"]["network"]


def network_prefix(cfg: dict) -> str:
    c = cfg["codex"]
    return c.get("network_prefix") or network(cfg)["name"].lower().replace(" ", "-") + "/"


def active_server(cfg: dict) -> tuple:
    """(OpenAI base, key) of the local server: Mac Intel Local AI's selection, else local_base_url."""
    try:
        a = json.loads(ACTIVE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg["codex"]["local_base_url"], None
    key = config.keychain_key(a["keychain_account"], a.get("keychain_service", "mac-intel-local-ai")) \
        if a.get("keychain_account") else None
    return a.get("openai_base_url"), key


_codex_api: dict = {}


def local_codex_api(cfg: dict) -> bool:
    """Whether the Ollama behind `fallback` has /api/codex/v1 (checked at most every 30 s)."""
    now = time.time()
    if now - _codex_api.get("at", 0) < 30:
        return _codex_api["ok"]
    target = urllib.parse.urlsplit(cfg["codex"]["fallback"].rstrip("/") + "/responses")
    try:
        c = http.client.HTTPConnection(target.netloc, timeout=2)
        c.request("POST", target.path, body=b"{}", headers={"Content-Type": "application/json"})
        ok = c.getresponse().status != 404
        c.close()
    except OSError:
        ok = False
    _codex_api.update(at=now, ok=ok)
    return ok


def route(cfg: dict, model):
    """(base_url, model to send, key or None or "" for missing, keep client auth / "ollama-cloud")."""
    c = cfg["codex"]
    if model and model in c["local_models"]:
        base, key = active_server(cfg)
        return base, model, key, False
    prefix = network_prefix(cfg)
    if model and c["network_models"] and model.startswith(prefix):
        n = network(cfg)
        if not n["base_url"]:
            return None, model, None, False
        key = config.keychain_key(n["keychain_account"], n.get("keychain_service", config.KEYCHAIN_SERVICE))
        return n["base_url"], model[len(prefix):], key or "", False
    if model and model.endswith(":cloud"):
        if "/api/codex/" in c["fallback"] and not local_codex_api(cfg):
            key = config.keychain_key(c["ollama_cloud_keychain_account"])
            if key:
                return c["ollama_cloud_base_url"], model[:-len(":cloud")], key, "ollama-cloud"
        return c["fallback"], model, None, True
    if c.get("gpt_direct", True) and (model or not local_codex_api(cfg)):
        return c["gpt_base_url"], model, None, True
    return c["fallback"], model, None, True


def is_anthropic(path: str, headers) -> bool:
    p = urllib.parse.urlsplit(path).path
    return p.startswith("/v1/messages") or bool(headers.get("anthropic-version"))


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"     # close-delimited: streams SSE without re-chunking
    server_version = "pii-router"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self):
        if self.path.startswith("/_router/debug/"):
            try:
                minutes = max(0.0, min(float(self.path.rsplit("/", 1)[1]), 24 * 60))
            except ValueError:
                minutes = 0.0
            with DEBUG_LOCK:
                DEBUG["until"] = time.time() + minutes * 60 if minutes else 0.0
                if not minutes:
                    DEBUG["sent"].clear()
            self.path = "/_router/sent"
        if self.path in ("/_router/status", "/_router/sent"):
            info = FILTER.status() if FILTER else {"mode": "off"}
            if self.path == "/_router/sent":
                with DEBUG_LOCK:
                    if not debug_on():
                        DEBUG["sent"].clear()
                    left = max(0, int((DEBUG["until"] - time.time()) / 60 + 0.99))
                    info = {"debug_minutes_left": left, "sent": list(DEBUG["sent"])}
            return self.reply(200, info)
        # Codex tries a WebSocket first and falls back to POST on 426.
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            return self.fail(426, "WebSocket not supported; use HTTP")
        self.dispatch(None)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/_router/check":
            return self.check(body)
        self.dispatch(body)

    def check(self, body):
        """For the Claude Code tool guard: which personal data a text holds (types only)."""
        try:
            text = str(json.loads(body).get("text") or "")
        except (ValueError, AttributeError):
            return self.fail(400, "expected {\"text\": ...}")
        stats: dict = {}
        if FILTER and FILTER.mode != "off":
            FILTER.begin()
            spans = FILTER.scan(text, "service") + FILTER.known_spans(text)
            for _, _, kind in spans:
                stats[kind] = stats.get(kind, 0) + 1
        return self.reply(200, {"found": stats})

    def do_DELETE(self):
        self.dispatch(None)

    def dispatch(self, body):
        cfg = config.load()
        if is_anthropic(self.path, self.headers):
            return self.forward_anthropic(cfg, body)
        if not self.path.startswith("/v1"):
            return self.fail(404, "unknown path")
        if not cfg["codex"].get("enabled", True):
            return self.fail(503, "the Codex route is off: pii-router codex enable")
        return self.forward_codex(cfg, body)

    # -- shared
    def read_payload(self, body):
        encoding = self.headers.get("Content-Encoding")
        if not body:
            return None, None, encoding, True
        plain = decode_body(body, encoding)
        if plain is None:
            self.fail(415, f"can't read a {encoding} request body"
                           + (" (install zstd: brew install zstd)" if (encoding or "").lower() == "zstd" else ""))
            return None, None, encoding, False
        try:
            payload = json.loads(plain)
        except ValueError:
            return None, None, encoding, True
        return payload, (payload.get("model") if isinstance(payload, dict) else None), encoding, True

    def filter_payload(self, api: str, model, payload: dict):
        """Redact in place; returns (stats, restore needed)."""
        t0 = now_ms()
        stats = (anthropic_api if api == "anthropic" else openai_api).redact_request(FILTER, payload)
        shape_text = anthropic_api.shape(payload) if api == "anthropic" else openai_api.input_shape(payload)
        unfiltered = stats.get("_unfiltered", 0)
        self.log_message("pii %s [%s, %s]: %s%s", FILTER.mode, api, FILTER.pipeline_name,
                         summary(stats, now_ms() - t0),
                         f"; {unfiltered} attachment(s) NOT filtered" if unfiltered else "")
        self.log_message("cloud input: %s", shape_text)
        if debug_on():
            capture(api, model, stats, payload, shape_text)
        return stats, FILTER.mode == "redact"

    def upstream(self, method, base: str, suffix: str, body, headers: dict):
        target = urllib.parse.urlsplit(base.rstrip("/") + suffix)
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        path = target.path + (("?" + target.query) if target.query else "")
        conn = conn_cls(target.netloc, timeout=600, context=TLS) if target.scheme == "https" else \
            conn_cls(target.netloc, timeout=600)
        conn.request(method, path, body=body, headers=headers)
        return conn, conn.getresponse(), target

    def relay(self, conn, resp, restorer_cls, restore_whole, rewrite: bool):
        ctype = (resp.getheader("Content-Type") or "").lower()
        rewrite = rewrite and not resp.getheader("Content-Encoding")
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in HOP and not (rewrite and k.lower() == "content-length"):
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        sse = restorer_cls(FILTER) if rewrite and "event-stream" in ctype else None
        try:
            if rewrite and sse is None:
                raw = resp.read()
                try:
                    raw = json.dumps(restore_whole(json.loads(raw)), ensure_ascii=False).encode()
                except ValueError:
                    raw = FILTER.restore(raw.decode("utf-8", "replace")).encode()
                self.wfile.write(raw)
                return
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(sse.feed(chunk) if sse else chunk)
                self.wfile.flush()
            if sse:
                self.wfile.write(sse.close())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    # -- Claude Code
    def forward_anthropic(self, cfg, body):
        payload, model, encoding, ok = self.read_payload(body)
        if not ok:
            return
        restore = False
        if FILTER and FILTER.mode != "off" and isinstance(payload, dict) and \
                urllib.parse.urlsplit(self.path).path.startswith("/v1/messages"):
            stats, restore = self.filter_payload("anthropic", model, payload)
            if stats.get("_unfiltered") and cfg["filter"].get("attachments") == "block":
                return self.fail(422, "the request has an image or PDF the filter can't read "
                                      "(filter.attachments = block)", anthropic=True)
            if restore:
                body, encoding = json.dumps(payload, ensure_ascii=False).encode("utf-8"), None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers = {k: v for k, v in headers.items() if k.lower() != "content-encoding"}
        if restore:
            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
        if encoding and body is not None:
            headers["Content-Encoding"] = encoding
        if body is not None:
            headers["Content-Length"] = str(len(body))
        upstream = cfg["claude"]["upstream"]
        self.log_message("%s %s model=%s len=%s -> %s", self.command, self.path, model,
                         len(body) if body is not None else None, upstream)
        try:
            conn, resp, _ = self.upstream(self.command, upstream, self.path, body, headers)
        except OSError as exc:
            return self.fail(502, f"upstream {upstream} unreachable: {exc}", anthropic=True)
        self.relay(conn, resp, anthropic_api.SseRestorer,
                   lambda m: anthropic_api.restore_message(FILTER, m) if isinstance(m, dict) else m, restore)

    # -- Codex
    def forward_codex(self, cfg, body):
        c = cfg["codex"]
        suffix = self.path[len("/v1"):]
        payload, model, encoding, ok = self.read_payload(body)
        if not ok:
            return
        base, send_model, key, keep_auth = route(cfg, model)
        if not keep_auth and isinstance(payload, dict):
            if c.get("functions_only", True) and isinstance(payload.get("tools"), list):
                payload["tools"] = [t for t in payload["tools"] if t.get("type") == "function"]
            payload.pop("parallel_tool_calls", None)      # LiteLLM rejects it for Ollama models
        if not keep_auth and payload is not None:
            body, encoding = json.dumps(payload).encode(), None
        if base is None:
            return self.fail(503, "no network LLM set: pii-router codex network --url ...")
        if key == "":
            return self.fail(401, "no key for the network LLM in the Keychain (account %s): "
                                  "pii-router codex key" % network(cfg)["keychain_account"])
        if payload is not None and send_model != model:
            payload["model"] = send_model
            body, encoding = json.dumps(payload).encode(), None
        restore = False
        cloud = bool(keep_auth)
        if keep_auth == "ollama-cloud":
            keep_auth = False             # Ollama Cloud gets its own key, not the ChatGPT token
        if cloud and FILTER and FILTER.mode != "off" and isinstance(payload, dict):
            stats, restore = self.filter_payload("codex", model, payload)
            if stats.get("_unfiltered") and cfg["filter"].get("attachments") == "block":
                return self.fail(422, "the request has an image or file the filter can't read "
                                      "(filter.attachments = block)")
            if restore:
                body, encoding = json.dumps(payload, ensure_ascii=False).encode("utf-8"), None
        # Streamed through LiteLLM/Ollama, tool calls arrive as plain text; unstreamed they
        # are proper function_call items. Ask without streaming and replay it as SSE.
        replay = (not cloud and suffix.startswith("/responses") and isinstance(payload, dict)
                  and payload.get("stream") and c.get("buffer_stream", True))
        if replay:
            payload["stream"] = False
            body, encoding = json.dumps(payload).encode(), None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        if not keep_auth:
            # Never hand the ChatGPT/OpenAI token to a local or network server.
            headers = {k: v for k, v in headers.items()
                       if k.lower() not in ("authorization", "chatgpt-account-id", "openai-organization", "cookie")
                       and not k.lower().startswith("x-codex") and not k.lower().startswith("openai-")}
            if key:
                headers["Authorization"] = "Bearer " + key
        headers = {k: v for k, v in headers.items() if k.lower() != "content-encoding"}
        if restore:
            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
        if encoding and body is not None:
            headers["Content-Encoding"] = encoding
        if body is not None:
            headers["Content-Length"] = str(len(body))
        self.log_message("%s %s model=%s enc=%s len=%s -> %s", self.command, self.path, model,
                         self.headers.get("Content-Encoding"), len(body) if body is not None else None,
                         base.rstrip("/") + suffix)
        try:
            conn, resp, _ = self.upstream(self.command, base, suffix, body, headers)
        except OSError as exc:
            return self.fail(502, f"upstream {urllib.parse.urlsplit(base).netloc} unreachable: {exc}")
        if replay:
            try:
                return self.replay_sse(resp)
            finally:
                conn.close()
        if keep_auth and resp.status == 404 and base == c["fallback"] and "/api/codex/" in c["fallback"]:
            resp.read()
            conn.close()
            return self.fail(404, "the Ollama on :11434 has no /api/codex/v1, so Ollama Cloud models can't pass "
                                  "through it. Run Ollama 0.40+, or store an Ollama API key: "
                                  "security add-generic-password -s pii-router -a ollama-cloud -w")
        self.relay(conn, resp, openai_api.SseRestorer, lambda v: openai_api.restore_event(FILTER, v), restore)

    def replay_sse(self, resp):
        """Send a finished Responses object to Codex as the SSE events it expects."""
        raw = resp.read()
        try:
            done = json.loads(raw)
        except ValueError:
            done = None
        if resp.status != 200 or not isinstance(done, dict) or "output" not in done:
            self.send_response(resp.status if resp.status != 200 else 502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        seq = 0

        def event(kind: str, **fields):
            nonlocal seq
            data = {"type": kind, "sequence_number": seq, **fields}
            seq += 1
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())

        try:
            start = dict(done, status="in_progress", output=[])
            event("response.created", response=start)
            event("response.in_progress", response=start)
            for i, item in enumerate(done["output"]):
                kind = item.get("type")
                if kind == "message":
                    event("response.output_item.added", output_index=i, item=dict(item, content=[], status="in_progress"))
                    for j, part in enumerate(item.get("content") or []):
                        if part.get("type") == "output_text":
                            text = part.get("text", "")
                            ids = dict(item_id=item.get("id"), output_index=i, content_index=j)
                            event("response.content_part.added", **ids, part=dict(part, text=""))
                            event("response.output_text.delta", **ids, delta=text)
                            event("response.output_text.done", **ids, text=text)
                            event("response.content_part.done", **ids, part=part)
                elif kind == "function_call":
                    event("response.output_item.added", output_index=i, item=dict(item, arguments="", status="in_progress"))
                    event("response.function_call_arguments.delta", item_id=item.get("id"), output_index=i,
                          delta=item.get("arguments", ""))
                    event("response.function_call_arguments.done", item_id=item.get("id"), output_index=i,
                          arguments=item.get("arguments", ""))
                else:
                    event("response.output_item.added", output_index=i, item=item)
                event("response.output_item.done", output_index=i, item=item)
            event("response.completed", response=dict(done, status=done.get("status") or "completed"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def reply(self, status: int, info):
        data = json.dumps(info, ensure_ascii=False, indent=1).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def fail(self, status: int, message: str, anthropic: bool = False):
        if anthropic:
            info = {"type": "error", "error": {"type": "api_error", "message": f"[{MARK}] {message}"}}
        else:
            info = {"error": {"message": f"[{MARK}] {message}", "type": "router_error"}}
        self.reply(status, info)


def serve(port: int | None = None) -> int:
    global FILTER
    cfg = config.load()
    FILTER = PiiFilter(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", int(port or cfg["port"])), Handler)
    server.daemon_threads = True
    print(f"pii-router on http://127.0.0.1:{server.server_port}; filter {FILTER.mode}, "
          f"pipeline {FILTER.pipeline_name}", file=sys.stderr, flush=True)
    server.serve_forever()
    return 0
