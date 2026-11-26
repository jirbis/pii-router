"""Compare pipelines on a labeled corpus: recall, precision, misses, false alarms, speed.

Corpus: JSON lines {"id", "lang", "context": "chat"|"document", "text",
"pii": [[value, TYPE], ...]}. The built-in one (data/corpus.jsonl) is synthetic
RU/DE/EN chat, forms, invoices, secrets, and code that must stay untouched.
Your own corpus stays on your Mac: pii-router eval --corpus my.jsonl.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config
from .filter import PiiFilter

CORPUS = Path(__file__).parent / "data/corpus.jsonl"
GROUPS = {"ADDRESS": "PLACE", "LOCATION": "PLACE", "ID_NUMBER": "NUMBER", "TAX_ID": "NUMBER",
          "ACCOUNT": "NUMBER", "IBAN": "NUMBER", "CARD": "NUMBER"}


def load_corpus(path=None) -> list:
    rows = []
    for line in Path(path or CORPUS).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            rows.append(json.loads(line))
    return rows


def covered(spans, start: int, end: int) -> tuple:
    """Fraction of [start, end) covered by spans, and the type of the biggest overlap."""
    best, kind, chars = 0, None, set()
    for s, e, k in spans:
        o = min(e, end) - max(s, start)
        if o > 0:
            chars.update(range(max(s, start), min(e, end)))
            if o > best:
                best, kind = o, k
    return len(chars) / max(1, end - start), kind


def score(flt: PiiFilter, rows: list) -> dict:
    tp = fn = fp = typed = 0
    per_type: dict = {}
    misses, alarms = [], []
    started = time.perf_counter()
    for row in rows:
        text, ctx = row["text"], row.get("context", "chat")
        spans = flt.scan(text, ctx)
        expected = []
        for value, kind in row.get("pii", []):
            pos = text.find(value)
            if pos >= 0:
                expected.append((pos, pos + len(value), kind, value))
        for s, e, kind, value in expected:
            frac, got = covered(spans, s, e)
            t = per_type.setdefault(kind, [0, 0])
            t[1] += 1
            if frac >= 0.6:
                tp += 1
                t[0] += 1
                typed += GROUPS.get(got, got) == GROUPS.get(kind, kind)
            else:
                fn += 1
                misses.append(f"{row['id']}: {kind} {value!r}" + (f" (only {frac:.0%})" if frac else ""))
        for s, e, kind in spans:
            if not any(min(e, xe) > max(s, xs) for xs, xe, _, _ in expected):
                fp += 1
                alarms.append(f"{row['id']}: {kind} {text[s:e]!r}")
    ms = (time.perf_counter() - started) * 1000 / max(1, len(rows))
    return {"recall": tp / max(1, tp + fn), "precision": tp / max(1, tp + fp), "type_match": typed / max(1, tp),
            "found": tp, "expected": tp + fn, "false_alarms": fp, "ms_per_text": ms,
            "per_type": {k: f"{a}/{b}" for k, (a, b) in sorted(per_type.items())},
            "misses": misses, "alarms": alarms}


def run(names: list, corpus=None, cfg: dict | None = None) -> dict:
    cfg = cfg or config.load()
    rows = load_corpus(corpus)
    results = {}
    for name in names:
        flt = PiiFilter(cfg, pipeline=name, dictionary_file="")      # never your dictionary in a test
        flt.mode = "audit"
        t0 = time.perf_counter()
        flt.wait()
        load_s = time.perf_counter() - t0
        flt.scan("warm up", "chat")
        r = score(flt, rows)
        r["load_s"] = load_s
        r["detectors"] = {n: d.state() for n, d in flt.detectors.items()}
        results[name] = r
    return results


def table(results: dict) -> str:
    head = f"{'pipeline':<12} {'recall':>7} {'precision':>9} {'found':>7} {'false+':>6} {'ms/text':>8} {'load s':>7}  detectors"
    lines = [head, "-" * len(head)]
    for name, r in results.items():
        det = ", ".join(f"{n}: {s}" for n, s in r["detectors"].items()) or "rules only"
        lines.append(f"{name:<12} {r['recall']:>7.0%} {r['precision']:>9.0%} {r['found']:>3}/{r['expected']:<3} "
                     f"{r['false_alarms']:>6} {r['ms_per_text']:>8.0f} {r['load_s']:>7.1f}  {det}")
    return "\n".join(lines)
