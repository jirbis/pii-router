"""Pointing the agents at the proxy, and back.

Codex / ChatGPT app: openai_base_url in ~/.codex/config.toml, plus local and
network models in its model catalog. Claude Code: env.ANTHROPIC_BASE_URL in
~/.claude/settings.json. Both files are backed up once before the first change.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from . import config

CODEX_CONFIG = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
CLAUDE_SETTINGS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
MARK = "PII Router"
OLD_MARKS = {MARK, "Codex PII Router", "Mac Intel Local AI"}
BASE_URL_RE = re.compile(r'^openai_base_url\s*=\s*"([^"]*)"\s*$', re.M)
CATALOG_RE = re.compile(r'^model_catalog_json\s*=\s*"([^"]*)"\s*$', re.M)


def proxy_url(cfg: dict, v1: bool = True) -> str:
    return f"http://127.0.0.1:{cfg['port']}" + ("/v1" if v1 else "")


def backup_once(path: Path) -> None:
    bak = path.with_name(path.name + ".bak-pii-router")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)


# ---------------------------------------------------------------- Codex

def codex_toml() -> str:
    try:
        return CODEX_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return ""


def catalog_path(toml: str):
    m = CATALOG_RE.search(toml)
    return Path(m.group(1)).expanduser() if m else None


def catalog_entry(template: dict, slug: str, name: str, context: int) -> dict:
    e = json.loads(json.dumps(template))
    e.update({"slug": slug, "display_name": name, "description": MARK,
              "context_window": context, "max_context_window": context,
              "supports_reasoning_summaries": False, "supports_reasoning_summary_parameter": False,
              "supports_search_tool": False, "input_modalities": ["text"], "priority": 50, "visibility": "list"})
    for k in ("default_service_tier", "upgrade", "availability_nux"):
        if k in e:
            e[k] = None
    return e


def codex_enable(cfg: dict) -> str:
    toml = codex_toml()
    if not toml:
        raise SystemExit(f"{CODEX_CONFIG} not found: run the Codex / ChatGPT app once first")
    url = proxy_url(cfg)
    m = BASE_URL_RE.search(toml)
    if m and m.group(1) != url:
        config.update({"codex": {"fallback": m.group(1)}})      # what Codex used before stays the fallback
        cfg = config.load()
    backup_once(CODEX_CONFIG)
    line = f'openai_base_url = "{url}"'
    toml = BASE_URL_RE.sub(line, toml, count=1) if m else line + "\n" + toml
    CODEX_CONFIG.write_text(toml, encoding="utf-8")
    c = cfg["codex"]
    path = catalog_path(toml)
    if path is None or not path.exists():
        return f"Codex -> {url}; no model_catalog_json, so local models can only be typed by name"
    backup_once(path)
    catalog = json.loads(path.read_text(encoding="utf-8"))
    models = [x for x in catalog.get("models", []) if x.get("description") not in OLD_MARKS]
    template = next((x for x in models if str(x.get("slug", "")).endswith(":cloud")), models[0] if models else None)
    added = []
    if template is not None:
        added = [catalog_entry(template, m, f"{m} (local)", int(c["local_context_window"])) for m in c["local_models"]]
        prefix = c.get("network_prefix") or c["network"]["name"].lower().replace(" ", "-") + "/"
        added += [catalog_entry(template, prefix + m, f"{m} ({c['network']['name']})",
                                int(c["network_context_window"])) for m in c["network_models"]]
    catalog["models"] = added + models
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return (f"Codex -> {url} (fallback {c['fallback']}); added models: "
            f"{', '.join(x['slug'] for x in added) or 'none'}. Restart the ChatGPT/Codex app.")


def codex_disable(cfg: dict) -> str:
    toml = codex_toml()
    fallback = cfg["codex"]["fallback"]
    m = BASE_URL_RE.search(toml)
    if m and m.group(1) == proxy_url(cfg):
        toml = BASE_URL_RE.sub(f'openai_base_url = "{fallback}"', toml, count=1)
        CODEX_CONFIG.write_text(toml, encoding="utf-8")
    path = catalog_path(toml)
    if path and path.exists():
        catalog = json.loads(path.read_text(encoding="utf-8"))
        catalog["models"] = [x for x in catalog.get("models", []) if x.get("description") not in OLD_MARKS]
        path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return f"Codex -> {fallback}; added models removed. Restart the ChatGPT/Codex app."


def codex_state(cfg: dict) -> dict:
    m = BASE_URL_RE.search(codex_toml())
    current = m.group(1) if m else None
    return {"config": str(CODEX_CONFIG), "base_url": current, "via_proxy": current == proxy_url(cfg)}


# ---------------------------------------------------------------- Claude Code

def claude_settings() -> dict:
    try:
        return json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def claude_enable(cfg: dict) -> str:
    s = claude_settings()
    env = s.setdefault("env", {})
    url = proxy_url(cfg, v1=False)
    previous = env.get("ANTHROPIC_BASE_URL")
    if previous and previous != url:
        config.update({"claude": {"upstream": previous.rstrip("/")}})     # a gateway already in use
    backup_once(CLAUDE_SETTINGS)
    env["ANTHROPIC_BASE_URL"] = url
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return (f"Claude Code -> {url} (upstream {config.load()['claude']['upstream']}). "
            "New Claude Code sessions use it; running ones keep their connection.")


def claude_disable(cfg: dict) -> str:
    s = claude_settings()
    env = s.get("env") or {}
    if env.get("ANTHROPIC_BASE_URL") == proxy_url(cfg, v1=False):
        upstream = cfg["claude"]["upstream"]
        if upstream.rstrip("/") != "https://api.anthropic.com":
            env["ANTHROPIC_BASE_URL"] = upstream
        else:
            env.pop("ANTHROPIC_BASE_URL")
        if not env:
            s.pop("env", None)
        CLAUDE_SETTINGS.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "Claude Code no longer uses the proxy (new sessions)."


def claude_state(cfg: dict) -> dict:
    url = (claude_settings().get("env") or {}).get("ANTHROPIC_BASE_URL")
    return {"settings": str(CLAUDE_SETTINGS), "base_url": url, "via_proxy": url == proxy_url(cfg, v1=False),
            "this_shell": os.environ.get("ANTHROPIC_BASE_URL")}
