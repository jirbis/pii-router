"""Paths and settings: ~/Library/Application Support/pii-router/config.json."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
from pathlib import Path

from . import detectors

HOME = Path.home()
SUPPORT = Path(os.environ.get("PII_ROUTER_HOME") or HOME / "Library/Application Support/pii-router")
CONFIG = SUPPORT / "config.json"
DICTIONARY = SUPPORT / "dictionary.txt"     # your own values to hide, one per line
VENV = SUPPORT / "venv"
VENV_PYTHON = VENV / "bin/python"
LOG = HOME / "Library/Logs/pii-router.log"
LABEL = "com.github.jirbis.pii-router"
AGENT = HOME / f"Library/LaunchAgents/{LABEL}.plist"
KEYCHAIN_SERVICE = "pii-router"

# Pipelines: which detectors run where. Rules and the dictionary always run first.
#   chat      user and assistant messages
#   document  tool output: files, PDFs and command output the agent read (models
#             run on it only when it reads like prose, not code)
#   service   blocks the agents add (environment, AGENTS.md/CLAUDE.md): rules only
PIPELINES = {
    "rules": {"chat": [], "document": [],
              "about": "rules and dictionary only: fast, no models"},
    "default": {"chat": ["gliner"], "document": ["gliner"],
                "about": "GLiNER: best recall and precision in pii-router eval"},
    "broad": {"chat": ["gliner", "spacy"], "document": ["gliner"],
              "about": "GLiNER + spaCy in chat: a few more places, more false names in German"},
    "spacy": {"chat": ["spacy"], "document": [], "about": "Presidio + spaCy only"},
    "hf": {"chat": ["hf_ner"], "document": ["hf_ner"],
           "about": "a Hugging Face token-classification model (Piiranha by default)"},
    "accurate": {"chat": ["gliner", "spacy", "llm"], "document": ["gliner", "llm"],
                 "about": "models plus a local LLM: slowest, catches context ('my wife Masha')"},
    "llm": {"chat": ["llm"], "document": ["llm"], "about": "local LLM only"},
}

DEFAULTS = {
    "port": 11500,
    "filter": {
        "mode": "redact",                   # redact | audit | off
        "pipeline": "default",
        "private_ips": True,                # also replace 10.x, 192.168.x, 172.16-31.x
        "tax_ids": True,                    # ИНН/КПП/ОГРН, Steuernummer, USt-IdNr/VAT, HRB, EIN...
        "companies": True,                  # names with a legal form: ООО «…», … GmbH, … Ltd
        "form_fields": True,                # "Straße   Main St.", "ФИО: ...": the value after a label
        "long_numbers": True,               # in prose: numbers of 8+ digits (customer, contract numbers)
        "deny_list": {},                    # extra dictionary entries {"value": "TYPE"}
        "allow_list": ["Codex", "ChatGPT", "OpenAI", "Ollama", "Claude", "Anthropic", "GitHub", "Python",
                       "Swift", "macOS", "Linux", "Docker", "Mac"],
        "attachments": "pass",              # images/PDF blocks the filter can't read: pass | block
    },
    "pipelines": {},                        # your own pipelines, same shape as PIPELINES
    "detectors": {},                        # overrides of detectors.DEFAULTS
    "codex": {
        "enabled": True,
        "fallback": "http://127.0.0.1:11434/api/codex/v1",
        "local_models": [],
        "local_context_window": 32768,
        "local_base_url": "http://127.0.0.1:11434/v1",
        "network": {"name": "network", "base_url": "", "keychain_account": "network",
                    "keychain_service": "mac-intel-local-ai"},
        "network_models": [],
        "network_prefix": "",
        "network_context_window": 32768,
        "functions_only": True,
        "gpt_direct": True,
        "gpt_base_url": "https://chatgpt.com/backend-api/codex",
        "ollama_cloud_base_url": "https://ollama.com/v1",
        "ollama_cloud_keychain_account": "ollama-cloud",
        "buffer_stream": True,
    },
    "claude": {
        "upstream": "https://api.anthropic.com",
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("deny_list", "labels", "models"):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load() -> dict:
    try:
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return deep_merge(DEFAULTS, saved)


def saved() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update(change: dict) -> dict:
    """Merge a change into the saved settings (only what differs from the defaults is kept)."""
    data = deep_merge(saved(), change)
    SUPPORT.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    CONFIG.chmod(0o600)
    return load()


def pipelines(cfg: dict) -> dict:
    p = copy.deepcopy(PIPELINES)
    p.update(cfg.get("pipelines") or {})
    return p


def detector_cfg(cfg: dict, name: str) -> dict:
    return deep_merge(detectors.DEFAULTS.get(name, {}), (cfg.get("detectors") or {}).get(name) or {})


_keys: dict = {}
_keys_lock = threading.Lock()


def keychain_key(account: str, service: str = KEYCHAIN_SERVICE) -> str | None:
    with _keys_lock:
        if (service, account) in _keys:
            return _keys[(service, account)]
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
                           capture_output=True, text=True)
        key = r.stdout.strip() if r.returncode == 0 else None
        if key:
            _keys[(service, account)] = key
        return key
