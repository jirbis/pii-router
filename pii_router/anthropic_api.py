"""Anthropic Messages API (Claude Code): what to filter in a request, and putting
values back into the answer stream.

Never touched: thinking / redacted_thinking blocks. They are signed, and Claude
Code sends them back unchanged; restoring values in them would break the
signature, so tags in thinking stay visible as tags.
"""

from __future__ import annotations

import json

from .filter import PiiFilter
from .rules import TAG_PREFIX_RE

UNREADABLE = ("image", "document", "search_result", "container_upload")


def redact_request(flt: PiiFilter, payload: dict) -> dict:
    """Redact a /v1/messages (or count_tokens) request in place; returns {TYPE: count}."""
    stats = flt.begin()

    def blocks(content, ctx):
        if isinstance(content, str):
            return flt.redact_text(content, ctx, stats)
        if not isinstance(content, list):
            return content
        for b in content:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "text" and isinstance(b.get("text"), str):
                b["text"] = flt.redact_text(b["text"], ctx, stats)
            elif kind == "tool_use" and "input" in b:
                b["input"] = flt.redact_value(b["input"], "service", stats)
            elif kind == "tool_result":
                b["content"] = blocks(b.get("content"), "document")
            elif kind in UNREADABLE:
                src = b.get("source") or {}
                if src.get("type") == "text" and isinstance(src.get("data"), str):   # plain-text documents
                    src["data"] = flt.redact_text(src["data"], "document", stats)
                else:
                    stats["_unfiltered"] = stats.get("_unfiltered", 0) + 1
            # thinking, redacted_thinking, server tool blocks: unchanged
        return content

    if "system" in payload:
        payload["system"] = blocks(payload["system"], "service")
    for msg in payload.get("messages") or []:
        if isinstance(msg, dict) and "content" in msg:
            msg["content"] = blocks(msg["content"], "chat")
    return stats


def shape(payload: dict) -> str:
    """Block kinds and sizes of a request, never content."""
    parts: dict = {}
    for msg in payload.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        for b in content if isinstance(content, list) else [{"type": "text", "text": content or ""}]:
            if isinstance(b, dict):
                key = f"{msg.get('role')}/{b.get('type')}"
                n, c = parts.get(key, (0, 0))
                parts[key] = (n + 1, c + len(json.dumps(b, ensure_ascii=False)))
    return " ".join(f"{k}={n}x{c}" for k, (n, c) in sorted(parts.items()))


def restore_message(flt: PiiFilter, msg: dict) -> dict:
    """A whole (non-streamed) message: text and tool_use input, not thinking."""
    for b in msg.get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            b["text"] = flt.restore(b["text"])
        elif b.get("type") == "tool_use" and "input" in b:
            b["input"] = flt.restore_json(b["input"])
    return msg


class SseRestorer:
    """Rewrites a Messages SSE stream: text_delta and input_json_delta get the real values
    (JSON-escaped inside tool input); a possible tag prefix is held back until the next
    delta of the same block or its content_block_stop."""

    def __init__(self, flt: PiiFilter):
        self.flt = flt
        self.buf = b""
        self.pending: dict = {}           # block index -> (delta type, held text)

    @staticmethod
    def _emit(name: str | None, ev: dict) -> bytes:
        head = f"event: {name}\n" if name else ""
        return (head + "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode()

    def _delta_event(self, index, kind: str, text: str) -> bytes:
        field = "text" if kind == "text_delta" else "partial_json"
        value = self.flt.restore(text, json_string=kind == "input_json_delta")
        return self._emit("content_block_delta", {"type": "content_block_delta", "index": index,
                                                  "delta": {"type": kind, field: value}})

    def _flush(self, index=None) -> bytes:
        out = b""
        for i in [index] if index is not None else list(self.pending):
            kind, held = self.pending.pop(i, (None, ""))
            if held:
                out += self._delta_event(i, kind, held)
        return out

    def _event(self, block: bytes) -> bytes:
        text = block.decode("utf-8", "replace")
        name, data_lines = None, []
        for line in text.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        try:
            ev = json.loads("\n".join(data_lines)) if data_lines else None
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            return block + b"\n\n"
        kind = ev.get("type")
        if kind == "content_block_delta":
            delta = ev.get("delta") or {}
            dkind = delta.get("type")
            field = {"text_delta": "text", "input_json_delta": "partial_json"}.get(dkind)
            if field and isinstance(delta.get(field), str):
                index = ev.get("index")
                held = self.pending.get(index, (dkind, ""))[1] + delta[field]
                m = TAG_PREFIX_RE.search(held)
                safe, tail = (held[:m.start()], held[m.start():]) if m else (held, "")
                self.pending[index] = (dkind, tail)
                return self._delta_event(index, dkind, safe) if safe else b""
            return block + b"\n\n"            # thinking_delta, signature_delta, citations: as they are
        if kind == "content_block_stop":
            return self._flush(ev.get("index")) + block + b"\n\n"
        if kind == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "text" and cb.get("text"):
                cb["text"] = self.flt.restore(cb["text"])
                return self._emit(name, ev)
            if cb.get("type") == "tool_use" and cb.get("input"):
                cb["input"] = self.flt.restore_json(cb["input"])
                return self._emit(name, ev)
            return block + b"\n\n"
        if kind == "message_start" and isinstance(ev.get("message"), dict):
            restore_message(self.flt, ev["message"])
            return self._emit(name, ev)
        return self._flush() + block + b"\n\n" if kind in ("message_delta", "message_stop") else block + b"\n\n"

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
        return out + self._flush()
