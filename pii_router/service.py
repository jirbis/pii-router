"""The proxy as a macOS LaunchAgent (starts at login, restarts if it stops)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import config

DICTIONARY_TEMPLATE = """# PII Router: your own values to hide from the cloud.
# One per line: value = TYPE (TYPE optional; PERSON, ADDRESS, COMPANY, PROJECT, ... or PRIVATE).
# Case doesn't matter. "*" matches word endings: "Petrov*" also hides Petrova, Petrovu.
# Entries also match inside file names (Bank_Inna_08.pdf). The proxy re-reads this
# file when it changes; no restart needed. It never leaves this Mac.
#
# Jane Doe = PERSON
# Petrov* = PERSON
# Main Street 1 = ADDRESS
"""

OLD = [  # earlier routers on this Mac: stopped and replaced
    ("com.github.jirbis.codex-pii-router", Path.home() / "Library/Application Support/codex-pii-router"),
    ("com.github.jirbis.mac-intel-local-ai.codex-router", None),
]


def python() -> str:
    return str(config.VENV_PYTHON) if config.VENV_PYTHON.exists() else sys.executable


def agent_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- PII Router: local proxy that filters personal data out of Codex and Claude Code requests. -->
<plist version="1.0">
<dict>
    <key>Label</key><string>{config.LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python()}</string><string>-I</string><string>-m</string><string>pii_router</string><string>serve</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>{config.LOG}</string>
    <key>StandardErrorPath</key><string>{config.LOG}</string>
</dict>
</plist>
"""


def launchctl(*args: str) -> int:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True).returncode


def domain() -> str:
    return f"gui/{os.getuid()}"


def running() -> bool:
    return launchctl("print", f"{domain()}/{config.LABEL}") == 0


def live_status(cfg: dict, timeout: float = 2) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg['port']}/_router/status", timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def migrate() -> list:
    """Take over settings and the dictionary of the earlier codex-pii-router, then stop it."""
    notes = []
    for label, support in OLD:
        if support and support.exists():
            old_cfg = support / "config.json"
            if old_cfg.exists() and not config.CONFIG.exists():
                old = json.loads(old_cfg.read_text(encoding="utf-8"))
                p = old.pop("pii", {}) or {}
                codex = {k: v for k, v in old.items() if k in config.DEFAULTS["codex"]}
                filt = {k: v for k, v in p.items() if k in config.DEFAULTS["filter"]}
                config.update({"codex": codex, "filter": filt, "port": old.get("port", 11500)})
                notes.append(f"settings taken over from {old_cfg}")
            old_dict = support / "dictionary.txt"
            if old_dict.exists() and not config.DICTIONARY.exists():
                config.SUPPORT.mkdir(parents=True, exist_ok=True)
                config.DICTIONARY.write_text(old_dict.read_text(encoding="utf-8"), encoding="utf-8")
                config.DICTIONARY.chmod(0o600)
                notes.append(f"dictionary taken over from {old_dict}")
        plist = Path.home() / f"Library/LaunchAgents/{label}.plist"
        if launchctl("print", f"{domain()}/{label}") == 0 or plist.exists():
            launchctl("bootout", f"{domain()}/{label}")
            plist.unlink(missing_ok=True)
            notes.append(f"old router {label} stopped")
    return notes


def start() -> list:
    notes = migrate()
    config.SUPPORT.mkdir(parents=True, exist_ok=True)
    config.LOG.parent.mkdir(parents=True, exist_ok=True)
    if not config.DICTIONARY.exists():
        config.DICTIONARY.write_text(DICTIONARY_TEMPLATE, encoding="utf-8")
        config.DICTIONARY.chmod(0o600)
    config.AGENT.write_text(agent_xml(), encoding="utf-8")
    launchctl("bootout", f"{domain()}/{config.LABEL}")
    for _ in range(40):
        if not running():
            break
        time.sleep(0.25)
    if launchctl("bootstrap", domain(), str(config.AGENT)) != 0:
        raise SystemExit(f"launchctl bootstrap {config.AGENT} failed")
    notes.append(f"proxy started: LaunchAgent {config.LABEL}, log {config.LOG}")
    return notes


def restart() -> None:
    if running():
        launchctl("kickstart", "-k", f"{domain()}/{config.LABEL}")


def stop() -> None:
    launchctl("bootout", f"{domain()}/{config.LABEL}")
    config.AGENT.unlink(missing_ok=True)


def wait_ready(cfg: dict, seconds: float = 120) -> dict | None:
    end = time.time() + seconds
    while time.time() < end:
        s = live_status(cfg)
        if s and all(not v.startswith("loading") for v in (s.get("detectors") or {}).values()):
            return s
        time.sleep(1)
    return live_status(cfg)
