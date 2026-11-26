"""OpenAI Responses / Chat Completions (Codex): what to filter in a request, and
putting values back into the answer stream."""

from __future__ import annotations

import json

from .filter import PiiFilter
from .rules import TAG_PREFIX_RE


def redact_json_string(flt: PiiFilter, text: str, stats: dict) -> str:
    """Tool arguments are a JSON string: redact the values inside it, so a tag stands for the
    real value (not its escaped form) and can be put back JSON-escaped."""
    try:
        value = json.loads(text)
    except ValueError:
        return flt.redact_text(text, "service", stats)
    before = sum(v for k, v in stats.items() if not k.startswith("_"))
    redacted = flt.redact_value(value, "service", stats)
    if sum(v for k, v in stats.items() if not k.startswith("_")) == before or flt.mode != "redact":
        return text                     # nothing found: keep the exact string
    return json.dumps(redacted, ensure_ascii=False)


def redact_request(flt: PiiFilter, payload: dict) -> dict:
    """Redact a Responses or Chat Completions request in place; returns {TYPE: count}."""
    stats = flt.begin()

    def parts(content, ctx):
        if isinstance(content, str):
            return flt.redact_text(content, ctx, stats)
        if isinstance(content, list):
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    p["text"] = flt.redact_text(p["text"], ctx, stats)
                elif isinstance(p, dict) and p.get("type") in ("input_image", "input_file", "image_url", "file"):
                    stats["_unfiltered"] = stats.get("_unfiltered", 0) + 1
        return content

    if isinstance(payload.get("instructions"), str):
        payload["instructions"] = flt.redact_text(payload["instructions"], "service", stats)
    items = payload.get("input")
    if isinstance(items, str):
        payload["input"] = flt.redact_text(items, "chat", stats)
        items = []
    for item in (items or []) + (payload.get("messages") or []):
        if not isinstance(item, dict):
            continue
        kind = item.get("type", "message")
        if kind == "message" or "role" in item:
            ctx = "chat" if item.get("role") in ("user", "assistant") else \
                "document" if item.get("role") == "tool" else "service"
            if "content" in item:
                item["content"] = parts(item["content"], ctx)
            for call in item.get("tool_calls") or []:        # Chat Completions
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    fn["arguments"] = redact_json_string(flt, fn["arguments"], stats)
        elif kind in ("function_call", "custom_tool_call"):
            if isinstance(item.get("arguments"), str):
                item["arguments"] = redact_json_string(flt, item["arguments"], stats)
            if isinstance(item.get("input"), str):       # custom tools: free text (patches, commands)
                item["input"] = flt.redact_text(item["input"], "service", stats)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            item["output"] = parts(item.get("output"), "document")
        elif kind == "reasoning":
            for s in item.get("summary") or []:
                if isinstance(s, dict) and isinstance(s.get("text"), str):
                    s["text"] = flt.redact_text(s["text"], "service", stats)
    return stats


def input_shape(payload: dict) -> str:
    """Which kinds of input a request carries (types and sizes only, never content)."""
    parts: dict = {}

    def add(key, chars):
        n, c = parts.get(key, (0, 0))
        parts[key] = (n + 1, c + chars)
    inp = payload.get("input")
    for item in inp if isinstance(inp, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type", "message")
        content = item.get("content") if "content" in item else item.get("output")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    extra = "+file_id" if c.get("file_id") else "+data" if c.get("file_data") or \
                        str(c.get("image_url", "")).startswith("data:") else ""
                    add(f"{kind}/{item.get('role', '-')}/{c.get('type', '?')}{extra}", len(json.dumps(c)))
        else:
            add(f"{kind}/{item.get('role', '-')}", len(json.dumps(content, ensure_ascii=False)))
    return " ".join(f"{k}={n}x{c}" for k, (n, c) in sorted(parts.items()))


class SseRestorer:
    """Rewrites an SSE stream of Responses events, putting real values back.

    Deltas are joined per stream (item, content part) so that a tag split across
    two deltas ("[PII:EMA" + "IL_1]") is restored; a possible tag prefix at the end
    of a delta is held back until the next delta or the next other event. Inside
    function-call arguments (JSON) the values are JSON-escaped.
    """

    def __init__(self, flt: PiiFilter):
        self.flt = flt
        self.buf = b""
        self.pending: dict = {}           # stream key -> (event dict, held text)

    @staticmethod
    def _key(ev: dict):
        return (ev.get("type"), ev.get("item_id"), ev.get("output_index"), ev.get("content_index"))

    @staticmethod
    def _emit(ev: dict, event_name: str | None) -> bytes:
        head = f"event: {event_name}\n" if event_name else ""
        return (head + "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode()

    def _restore_delta(self, ev: dict, text: str) -> str:
        return self.flt.restore(text, json_string="arguments" in str(ev.get("type", "")))

    def _flush_pending(self) -> bytes:
        out = b""
        for ev, held in self.pending.values():
            if held:
                out += self._emit(dict(ev, delta=self._restore_delta(ev, held)), ev.get("type"))
        self.pending.clear()
        return out

    def _event(self, block: bytes) -> bytes:
        text = block.decode("utf-8", "replace")
        name, data_lines = None, []
        for line in text.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        data = "\n".join(data_lines)
        try:
            ev = json.loads(data) if data else None
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            return self._flush_pending() + block + b"\n\n"
        if isinstance(ev.get("delta"), str) and str(ev.get("type", "")).endswith(".delta"):
            key = self._key(ev)
            held = self.pending.get(key, (ev, ""))[1] + ev["delta"]
            m = TAG_PREFIX_RE.search(held)
            safe, tail = (held[:m.start()], held[m.start():]) if m else (held, "")
            self.pending[key] = (ev, tail)
            return self._emit(dict(ev, delta=self._restore_delta(ev, safe)), name) if safe else b""
        return self._flush_pending() + self._emit(restore_event(self.flt, ev), name)

    def feed(self, chunk: bytes) -> bytes:
        self.buf += chunk.replace(b"\r\n", b"\n")
        out = b""
        while b"\n\n" in self.buf:
            block, self.buf = self.buf.split(b"\n\n", 1)
            if block.strip():
                out += self._event(block)
        return out

    def close(self) -> bytes:
        out = self._event(self.buf) if self.buf.strip() else b""
        return out + self._flush_pending()


def restore_event(flt: PiiFilter, value, key: str = ""):
    """Values back into an event or response object; "arguments" strings are JSON: escape."""
    if isinstance(value, str):
        return flt.restore(value, json_string=key == "arguments")
    if isinstance(value, list):
        return [restore_event(flt, v, key) for v in value]
    if isinstance(value, dict):
        return {k: restore_event(flt, v, k) for k, v in value.items()}
    return value
