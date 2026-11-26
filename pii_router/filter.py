"""The PII filter: finds personal data, replaces each value with a tag such as
[PII:EMAIL_1] and puts the real values back into the answer.

The value <-> tag table lives only in this process's memory. One value always
gets the same tag, so repeated requests (whole chat histories, prompt caches)
stay identical. Modes: "redact" (replace), "audit" (only count), "off".
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import config, detectors
from .rules import (LONG_NUMBER, SERVICE_BLOCKS, TAG_RE, dictionary_entries, looks_like_prose, merge,
                    rule_spans, value_pattern)

CONTEXTS = ("chat", "document", "service")


class PiiFilter:
    def __init__(self, cfg: dict | None = None, pipeline: str | None = None, dictionary_file: str | None = None,
                 background: bool = True):
        self.full = cfg or config.load()
        self.cfg = self.full["filter"]
        self.mode = self.cfg["mode"]
        self.pipeline_name = pipeline or self.cfg["pipeline"]
        all_pipes = config.pipelines(self.full)
        if self.pipeline_name not in all_pipes:
            raise ValueError(f"unknown pipeline {self.pipeline_name!r}; known: {', '.join(all_pipes)}")
        self.pipeline = all_pipes[self.pipeline_name]
        self.lock = threading.Lock()
        self.by_value: dict = {}           # (TYPE, value) -> tag
        self.by_tag: dict = {}             # tag -> value
        self.counters: dict = {}
        self.cache: OrderedDict = OrderedDict()
        self.known_rx = None               # every value tagged so far, as one pattern
        self.known_version = -1
        self.dictionary_file = str(config.DICTIONARY) if dictionary_file is None else dictionary_file
        deny = self.cfg.get("deny_list") or {}
        self.config_deny = list(deny.items() if isinstance(deny, dict) else ((v, "PRIVATE") for v in deny))
        self.dict_mtime = None
        self.dict_loaded = False
        self.dict_size = 0
        self.deny: list = []
        self._load_dictionary()
        self.allow = {a.lower() for a in self.cfg["allow_list"]}
        names = []
        for ctx in ("chat", "document"):
            names += [n for n in self.pipeline.get(ctx, []) if n not in names]
        self.detectors = {} if self.mode == "off" else \
            {n: detectors.make(n, config.detector_cfg(self.full, n), background) for n in names}

    def wait(self, timeout: float = 300) -> None:
        for d in self.detectors.values():
            d.ready.wait(timeout)

    # -- dictionary
    def _load_dictionary(self):
        """(Re)build the deny patterns from the config and the dictionary file when it changed."""
        path = self.dictionary_file
        try:
            mtime = Path(path).stat().st_mtime if path else None
        except OSError:
            mtime = None
        if self.dict_loaded and mtime == self.dict_mtime:
            return
        entries = self.config_deny + (dictionary_entries(path) if mtime else [])
        entries.sort(key=lambda e: -len(e[0]))          # "Jane Doe" wins over "Jane"
        deny = [(value_pattern(v), t.upper()) for v, t in entries if v.strip()]
        with self.lock:
            self.deny, self.dict_mtime, self.dict_size, self.dict_loaded = deny, mtime, len(entries), True
            self.cache.clear()

    # -- tags
    def _tag(self, kind: str, value: str) -> str:
        key = (kind, value)
        with self.lock:
            tag = self.by_value.get(key)
            if tag is None:
                n = self.counters[kind] = self.counters.get(kind, 0) + 1
                tag = f"[PII:{kind}_{n}]"
                self.by_value[key] = tag
                self.by_tag[tag] = value
            return tag

    @staticmethod
    def propagates(value: str) -> bool:
        """Long enough to hide everywhere once found (not "52" from a house-number field)."""
        v = value.strip()
        return len(v) >= 4 and any(c.isalpha() for c in v) or sum(c.isdigit() for c in v) >= 6

    def known_spans(self, text: str) -> list:
        """Values already tagged in this session, wherever they appear again: a name a model
        found in chat must not leave later inside tool arguments, where only rules run."""
        with self.lock:
            if self.known_version != len(self.by_value):
                values = sorted({(v, k) for (k, v) in self.by_value if self.propagates(v)},
                                key=lambda e: -len(e[0]))
                self.known_kinds = {v: k for v, k in values}
                self.known_rx = re.compile("|".join(r"(?<!\w)" + re.escape(v) + r"(?!\w)" for v, _ in values)) \
                    if values else None
                self.known_version = len(self.by_value)
            rx, kinds = self.known_rx, self.known_kinds if self.known_rx else {}
        return [(m.start(), m.end(), kinds[m.group(0)]) for m in rx.finditer(text)] if rx else []

    def restore(self, text: str, json_string: bool = False) -> str:
        """Values back in place of tags; json_string: escape them for the inside of a JSON string."""
        if "[PII:" not in text:
            return text

        def value(m):
            v = self.by_tag.get(m.group(0))
            if v is None:
                return m.group(0)
            return json.dumps(v, ensure_ascii=False)[1:-1] if json_string else v
        return TAG_RE.sub(value, text)

    def restore_json(self, value):
        if isinstance(value, str):
            return self.restore(value)
        if isinstance(value, list):
            return [self.restore_json(v) for v in value]
        if isinstance(value, dict):
            return {k: self.restore_json(v) for k, v in value.items()}
        return value

    # -- one string
    def context(self, text: str, ctx: str) -> str:
        if ctx != "service" and text.lstrip().startswith(SERVICE_BLOCKS):
            return "service"
        if ctx == "document" and not looks_like_prose(text):
            return "service"            # code, JSON, logs: rules only
        return ctx

    def scan(self, text: str, ctx: str = "chat") -> list:
        """(start, end, TYPE) spans for a text seen in a context (chat | document | service)."""
        ctx = self.context(text, ctx)
        spans = merge(rule_spans(text, self.cfg, self.deny))
        if ctx == "service":
            names = []
        else:
            names = self.pipeline.get(ctx, [])
            if self.cfg.get("long_numbers", True):
                spans = merge(spans + [(m.start(), m.end(), "ID_NUMBER") for m in LONG_NUMBER.finditer(text)])
        if names and any(c.isalpha() for c in text):
            found = []
            for n in names:
                d = self.detectors.get(n)
                if d is not None:
                    found += d.spans(text)
            # Exact rules (dictionary, tax numbers, companies) win over the models.
            spans += [m for m in found if not any(m[0] < r[1] and r[0] < m[1] for r in spans)]
        tags = [(m.start(), m.end()) for m in TAG_RE.finditer(text)]
        return [s for s in merge(spans) if text[s[0]:s[1]].strip().lower() not in self.allow
                and any(c.isalnum() for c in text[s[0]:s[1]])
                and not any(s[0] < b and a < s[1] for a, b in tags)]

    def redact_text(self, text: str, ctx: str, stats: dict) -> str:
        if not text or self.mode == "off":
            return text
        key = (hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest(), ctx)
        with self.lock:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache.move_to_end(key)
        if hit is None:
            spans = self.scan(text, ctx)
            hit = (spans, [s[2] for s in spans])
            with self.lock:
                self.cache[key] = hit
                while len(self.cache) > 5000:
                    self.cache.popitem(last=False)
        spans, kinds = hit
        for k in kinds:
            stats[k] = stats.get(k, 0) + 1
        if self.mode == "redact" and self.by_value:
            extra = [k for k in self.known_spans(text) if not any(k[0] < s[1] and s[0] < k[1] for s in spans)]
            if extra:
                tags = [(m.start(), m.end()) for m in TAG_RE.finditer(text)]
                extra = [k for k in extra if not any(k[0] < b and a < k[1] for a, b in tags)]
                spans = sorted(spans + extra)
                stats["_known"] = stats.get("_known", 0) + len(extra)
        if self.mode != "redact" or not spans:
            return text
        out, pos = [], 0
        for start, end, kind in spans:
            out.append(text[pos:start])
            out.append(self._tag(kind, text[start:end]))
            pos = end
        out.append(text[pos:])
        return "".join(out)

    def redact_value(self, value, ctx: str, stats: dict):
        """Every string inside a JSON value (tool arguments)."""
        if isinstance(value, str):
            return self.redact_text(value, ctx, stats)
        if isinstance(value, list):
            return [self.redact_value(v, ctx, stats) for v in value]
        if isinstance(value, dict):
            return {k: self.redact_value(v, ctx, stats) for k, v in value.items()}
        return value

    def begin(self) -> dict:
        """Start a request: re-read the dictionary if it changed; returns the stats dict."""
        self._load_dictionary()
        return {}

    def status(self) -> dict:
        return {"mode": self.mode, "pipeline": self.pipeline_name,
                "detectors": {n: d.state() for n, d in self.detectors.items()},
                "dictionary_entries": self.dict_size, "known_values": len(self.by_tag)}


def summary(stats: dict, ms: float | None = None) -> str:
    found = " ".join(f"{k}x{v}" for k, v in sorted(stats.items()) if not k.startswith("_")) or "nothing"
    return found if ms is None else f"{found} in {ms:.0f} ms"


def now_ms() -> float:
    return time.perf_counter() * 1000
