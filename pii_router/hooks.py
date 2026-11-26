"""Claude Code hooks (from the plugin). Fast, standard library only.

session-start   Tells you (and Claude) whether this session's requests go through the
                filter; starts the proxy if its LaunchAgent is installed but stopped.
pre-tool-use    The proxy only filters what goes to the model. A tool call runs on your
                Mac with the real values put back, so `curl -d ... https://...` or a web
                search could still send them out. When such a call contains personal data
                the filter knows, Claude Code asks you first.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

from . import config

NETWORK = re.compile(r"(?<![\w-])(curl|wget|http|https|xh|nc|ncat|telnet|scp|sftp|rsync|ssh|ftp|"
                     r"gh\s+(issue|pr|gist|api|release)|git\s+push|aws|gcloud|az|sendmail|mail|osascript)(?![\w-])")


def out(obj: dict) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return 0


def proxy(path: str, body: dict | None = None, timeout: float = 2):
    cfg = config.load()
    url = f"http://127.0.0.1:{cfg['port']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def session_start() -> int:
    cfg = config.load()
    want = f"http://127.0.0.1:{cfg['port']}"
    current = (os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    status = proxy("/_router/status")
    if status is None and config.AGENT.exists():
        from . import service
        service.restart() if service.running() else service.launchctl("bootstrap", service.domain(),
                                                                        str(config.AGENT))
        status = proxy("/_router/status", timeout=5)
    if current != want:
        msg = ("PII Router: this session talks to Anthropic directly, nothing is filtered. "
               "Turn it on with `pii-router claude enable` (new sessions).")
    elif status is None:
        msg = (f"PII Router: ANTHROPIC_BASE_URL points at {want}, but the proxy isn't answering, so "
               "requests will fail. Start it: `pii-router install`.")
    else:
        msg = f"PII Router: filter {status.get('mode')}, pipeline {status.get('pipeline')}."
    context = ("Personal data in this conversation may appear as tags like [PII:PERSON_1]. They are "
               "placeholders a local filter puts in place of real values and turns back into the values "
               "in your answers and tool calls. Use the tags as they are; don't ask for the real values."
               if current == want and status else "")
    obj = {"systemMessage": msg}
    if context:
        obj["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": context}
    return out(obj)


def pre_tool_use() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = event.get("tool_name") or ""
    args = event.get("tool_input") or {}
    if tool == "Bash":
        text = str(args.get("command") or "")
        if not NETWORK.search(text):
            return 0
    elif tool in ("WebFetch", "WebSearch"):
        text = " ".join(str(args.get(k) or "") for k in ("url", "query", "prompt"))
    else:
        return 0
    found = proxy("/_router/check", {"text": text})
    kinds = (found or {}).get("found") or {}
    if not kinds:
        return 0
    what = ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items()))
    return out({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "ask",
        "permissionDecisionReason": f"PII Router: this {tool} call would send personal data off this Mac "
                                    f"({what}). Allow only if that's intended."}})


def run(event: str) -> int:
    try:
        return session_start() if event == "session-start" else pre_tool_use()
    except Exception as exc:          # a hook must never break Claude Code
        print(f"pii-router hook: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
