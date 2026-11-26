"""pii-router: command line."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import __version__, config, integrations, service

DETECTOR_NAMES = ("spacy", "gliner", "hf_ner", "llm")


def say(obj) -> None:
    print(obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False))


def restart_note() -> None:
    if service.running():
        service.restart()
        print("proxy restarted with the new settings")


# ---------------------------------------------------------------- commands

def cmd_serve(a):
    from . import proxy
    return proxy.serve(a.port)


def cmd_install(a):
    for note in service.start():
        print(note)
    cfg = config.load()
    if a.codex:
        print(integrations.codex_enable(cfg))
    if a.claude:
        print(integrations.claude_enable(cfg))
    if not (a.codex or a.claude):
        print("Point an agent at it: pii-router codex enable / pii-router claude enable")
    s = service.wait_ready(cfg, 30)
    say({"proxy": s})
    return 0


def cmd_uninstall(a):
    cfg = config.load()
    print(integrations.codex_disable(cfg))
    print(integrations.claude_disable(cfg))
    service.stop()
    print(f"proxy stopped; settings, dictionary and venv stay in {config.SUPPORT}")
    return 0


def cmd_status(a):
    cfg = config.load()
    say({"version": __version__, "proxy": f"http://127.0.0.1:{cfg['port']}", "agent_running": service.running(),
         "live": service.live_status(cfg), "codex": integrations.codex_state(cfg),
         "claude": integrations.claude_state(cfg), "settings": str(config.CONFIG),
         "dictionary": str(config.DICTIONARY), "log": str(config.LOG)})
    return 0


def cmd_codex(a):
    cfg = config.load()
    if a.action == "enable":
        config.update({"codex": {"enabled": True}})
        print(integrations.codex_enable(config.load()))
    elif a.action == "disable":
        print(integrations.codex_disable(cfg))
    elif a.action == "models":
        change = {}
        if a.local is not None:
            change["local_models"] = [m for m in a.local.split(",") if m]
        if a.network is not None:
            change["network_models"] = [m for m in a.network.split(",") if m]
        config.update({"codex": change})
        print(integrations.codex_enable(config.load()))
    elif a.action == "network":
        n = dict(cfg["codex"]["network"])
        if a.name:
            n["name"] = a.name
        if a.url:
            n["base_url"] = a.url
        if a.account:
            n["keychain_account"] = a.account
        config.update({"codex": {"network": n}})
        say(n)
    elif a.action == "key":
        n = cfg["codex"]["network"]
        print(f"Paste the API key of {n['name']} (stored in the Keychain, service "
              f"{n.get('keychain_service', config.KEYCHAIN_SERVICE)}, account {n['keychain_account']}):")
        return subprocess.call(["/usr/bin/security", "add-generic-password", "-U", "-s",
                                n.get("keychain_service", config.KEYCHAIN_SERVICE), "-a", n["keychain_account"], "-w"])
    restart_note() if a.action in ("models", "network") else None
    return 0


def cmd_claude(a):
    cfg = config.load()
    if a.action == "enable":
        print(integrations.claude_enable(cfg))
    elif a.action == "disable":
        print(integrations.claude_disable(cfg))
    elif a.action == "upstream":
        config.update({"claude": {"upstream": a.url.rstrip("/")}})
        print(f"Claude Code requests go on to {a.url}")
        restart_note()
    return 0


def cmd_mode(a):
    config.update({"filter": {"mode": a.mode}})
    print(f"filter mode: {a.mode}")
    restart_note()
    return 0


def cmd_pipeline(a):
    cfg = config.load()
    pipes = config.pipelines(cfg)
    if a.action in (None, "list"):
        for name, p in pipes.items():
            mark = "*" if name == cfg["filter"]["pipeline"] else " "
            print(f"{mark} {name:<10} chat: {', '.join(p.get('chat') or []) or 'rules'};"
                  f" documents: {', '.join(p.get('document') or []) or 'rules'}   {p.get('about', '')}")
        print("\n(* = active; rules and the dictionary always run first)")
        return 0
    if a.action == "use":
        if a.name not in pipes:
            raise SystemExit(f"unknown pipeline {a.name!r}; see: pii-router pipeline list")
        config.update({"filter": {"pipeline": a.name}})
        print(f"active pipeline: {a.name}")
        restart_note()
        return 0
    if a.action == "add":
        chat = [d for d in (a.chat or "").split(",") if d]
        doc = [d for d in (a.document or "").split(",") if d]
        bad = [d for d in chat + doc if d not in DETECTOR_NAMES]
        if bad:
            raise SystemExit(f"unknown detector(s) {bad}; known: {', '.join(DETECTOR_NAMES)}")
        config.update({"pipelines": {a.name: {"chat": chat, "document": doc, "about": a.about or "your pipeline"}}})
        print(f"pipeline {a.name} saved; try it: pii-router eval --pipeline {a.name}")
        return 0
    if a.action == "remove":
        s = config.saved()
        (s.get("pipelines") or {}).pop(a.name, None)
        config.CONFIG.write_text(json.dumps(s, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        print(f"pipeline {a.name} removed")
        return 0
    return 2


def parse_value(v: str):
    try:
        return json.loads(v)
    except ValueError:
        return v


def cmd_detector(a):
    cfg = config.load()
    if a.action in (None, "list"):
        for n in DETECTOR_NAMES:
            print(f"--- {n}")
            say(config.detector_cfg(cfg, n))
        return 0
    if a.action == "set":
        change = {}
        for kv in a.values:
            k, _, v = kv.partition("=")
            change[k.strip()] = parse_value(v)
        config.update({"detectors": {a.name: change}})
        say(config.detector_cfg(config.load(), a.name))
        restart_note()
        return 0
    return 2


def cmd_llm(a):
    change = {}
    for field in ("api", "model", "timeout", "max_chars"):
        if getattr(a, field) is not None:
            change[field] = getattr(a, field)
    if a.url is not None:
        change["base_url"] = a.url
    if a.key_account is not None:
        change["keychain_account"] = a.key_account
    if a.allow_remote:
        change["allow_remote"] = True
    if a.local_only:
        change["allow_remote"] = False
    if a.instruction is not None:
        change["instruction"] = a.instruction
    if a.instruction_file:
        change["instruction"] = Path(a.instruction_file).expanduser().read_text(encoding="utf-8")
    if change:
        config.update({"detectors": {"llm": change}})
    llm_cfg = config.detector_cfg(config.load(), "llm")
    if a.test:
        from . import detectors
        d = detectors.make("llm", llm_cfg, background=False)
        if not d.ok:
            raise SystemExit(f"LLM unavailable: {d.error}")
        print(f"{d.detail()}:")
        for s, e, k in d.find(a.test):
            print(f"  {k:<14} {a.test[s:e]!r}")
        return 0
    say({k: (v if k != "instruction" else (v[:200] + "…" if len(v) > 200 else v)) for k, v in llm_cfg.items()})
    if change:
        restart_note()
    print("\nUse it in a pipeline: pii-router pipeline use accurate (or add your own with llm)")
    return 0


def cmd_dictionary(a):
    path = config.DICTIONARY
    if not path.exists():
        config.SUPPORT.mkdir(parents=True, exist_ok=True)
        path.write_text(service.DICTIONARY_TEMPLATE, encoding="utf-8")
        path.chmod(0o600)
    if a.action == "edit":
        return subprocess.call([os.environ.get("EDITOR") or "open", str(path)] if os.environ.get("EDITOR")
                               else ["open", "-t", str(path)])
    if a.action == "add":
        with path.open("a", encoding="utf-8") as f:
            for entry in a.entries:
                f.write(entry.strip() + "\n")
        print(f"{len(a.entries)} entr{'y' if len(a.entries) == 1 else 'ies'} added to {path}")
        return 0
    if a.action == "path":
        print(path)
        return 0
    from .rules import dictionary_entries
    entries = dictionary_entries(str(path))
    print(f"{path}: {len(entries)} entries (only on this Mac)")
    if a.action == "list":
        for v, k in entries:
            print(f"  {k:<12} {v}")
    return 0


def cmd_test(a):
    from .filter import PiiFilter, summary
    if a.file:
        text = "\n".join(read_document(f) for f in a.file)
        ctx = a.context or "document"
    else:
        text = " ".join(a.text) if a.text else sys.stdin.read()
        ctx = a.context or "chat"
    cfg = config.load()
    cfg["filter"]["mode"] = "redact"
    flt = PiiFilter(cfg, pipeline=a.pipeline)
    flt.wait()
    stats: dict = {}
    print(flt.redact_text(text, ctx, stats))
    print(f"\n[{flt.pipeline_name}, {ctx}] {summary(stats)} | " +
          ", ".join(f"{n}: {d.state()}" for n, d in flt.detectors.items()), file=sys.stderr)
    return 0


def read_document(path: str) -> str:
    p = Path(path).expanduser()
    if p.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise SystemExit("reading PDFs needs pypdf: pip install pypdf (in the pii-router venv)")
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(p)).pages)
    return p.read_text(encoding="utf-8", errors="replace")


def cmd_eval(a):
    from . import evaluate
    cfg = config.load()
    names = list(config.pipelines(cfg)) if a.all else (a.pipeline or [cfg["filter"]["pipeline"], "rules"])
    results = evaluate.run(list(dict.fromkeys(names)), a.corpus, cfg)
    if a.json:
        say(results)
        return 0
    print(evaluate.table(results))
    for name, r in results.items():
        print(f"\n{name}: per type {r['per_type']}")
        if a.verbose:
            for m in r["misses"]:
                print(f"  missed  {m}")
            for m in r["alarms"]:
                print(f"  extra   {m}")
    if not a.verbose:
        print("\n(--verbose lists every miss and false alarm)")
    return 0


def cmd_debug(a):
    cfg = config.load()
    minutes = (a.minutes if a.minutes is not None else 30) if a.state == "on" else 0
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg['port']}/_router/debug/{minutes:g}", timeout=5):
            pass
    except OSError as exc:
        raise SystemExit(f"proxy not reachable: {exc}")
    if minutes:
        print(f"Debug mode on for {minutes:g} min. The proxy keeps the last 3 cloud requests exactly as sent\n"
              "(personal data as [PII:...] tags), in memory only, never on disk or in the log. They still hold\n"
              "the rest of your text. `pii-router sent` shows them; `pii-router debug off` clears them, and so\n"
              "do the end of the time and a restart.")
    else:
        print("Debug mode off, kept requests cleared.")
    return 0


def cmd_sent(a):
    from .filter import summary
    cfg = config.load()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg['port']}/_router/sent", timeout=5) as r:
            info = json.loads(r.read())
    except OSError as exc:
        raise SystemExit(f"proxy not reachable: {exc}")
    if not info.get("debug_minutes_left"):
        print("Debug mode is off, nothing kept. `pii-router debug on` keeps the next cloud requests for 30 min.")
        return 0
    if not info["sent"]:
        print(f"Nothing sent to the cloud yet (debug on, {info['debug_minutes_left']} min left).")
    for req in info["sent"]:
        print(f"===== {req['time']} {req['api']} {req['model']}  found: {summary(req['found'])}")
        print(f"      kinds: {req['input']}")
        for it in req["items"]:
            print(f"--- {it['item']}" + (f"  attachments: {', '.join(it['attachments'])}" if it.get("attachments") else ""))
            if it["text"]:
                print(it["text"])
    print(f"\n(debug on, {info['debug_minutes_left']} min left; `pii-router debug off` clears)")
    return 0


def cmd_doctor(a):
    import importlib.util
    machine = platform.machine()
    info = {"python": sys.version.split()[0], "executable": sys.executable,
            "mac": f"{platform.mac_ver()[0]} {machine}",
            "hardware": "Apple Silicon" if machine == "arm64" else "Intel"}
    pk = {}
    for mod in ("truststore", "presidio_analyzer", "spacy", "gliner", "transformers", "torch", "pypdf",
                "en_core_web_md", "de_core_news_md", "ru_core_news_md"):
        spec = importlib.util.find_spec(mod)
        if spec is None:
            pk[mod] = "missing"
            continue
        try:
            from importlib.metadata import version
            pk[mod] = version(mod.replace("_", "-")) if mod not in ("en_core_web_md", "de_core_news_md",
                                                                    "ru_core_news_md") else "installed"
        except Exception:
            pk[mod] = "installed"
    info["packages"] = pk
    if pk.get("torch") not in (None, "missing"):
        try:
            import torch
            info["torch_mps"] = bool(torch.backends.mps.is_available())
        except Exception as exc:
            info["torch_mps"] = f"error: {exc}"
    from . import detectors
    base, model = detectors.active_llm()
    info["mac_intel_local_ai"] = {"base_url": base, "model": model} if base else "not installed"
    for name, url in (("ollama", "http://127.0.0.1:11434/api/version"),):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                info[name] = json.loads(r.read()).get("version")
        except (OSError, ValueError):
            info[name] = "not running"
    info["advice"] = advice(info)
    say(info)
    return 0


def advice(info: dict) -> list:
    out = []
    pk = info["packages"]
    if info["hardware"] == "Intel" and pk.get("torch") not in ("missing", "2.2.2"):
        out.append("Intel Mac: PyTorch stops at 2.2.2; use ./install.sh --profile intel")
    if pk.get("gliner") == "missing":
        out.append("no GLiNER: the default pipeline runs spaCy only (./install.sh --profile intel|apple-silicon)")
    if pk.get("presidio_analyzer") == "missing":
        out.append("no Presidio/spaCy: chat names rely on GLiNER or the LLM")
    if info["hardware"] == "Apple Silicon" and info.get("torch_mps") is True:
        out.append("Apple Silicon with MPS: GLiNER uses the GPU when it matches the CPU (gliner device auto)")
    return out


def cmd_hook(a):
    from . import hooks
    return hooks.run(a.event)


# ---------------------------------------------------------------- parser

def build() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pii-router", description="Filter personal data out of Codex and Claude Code "
                                "requests before they reach a cloud model.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the proxy in the foreground (the LaunchAgent does this)")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("install", help="start the proxy as a LaunchAgent; --codex / --claude point the agents at it")
    s.add_argument("--codex", action="store_true")
    s.add_argument("--claude", action="store_true")
    s.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="agents back as they were, proxy stopped").set_defaults(fn=cmd_uninstall)
    sub.add_parser("status", help="proxy, filter, Codex and Claude Code state").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="Python, packages, hardware (Intel / Apple Silicon), local LLM servers"
                   ).set_defaults(fn=cmd_doctor)

    s = sub.add_parser("codex", help="Codex / ChatGPT app: enable | disable | models | network | key")
    s.add_argument("action", choices=["enable", "disable", "models", "network", "key"])
    s.add_argument("--local", help="local models, comma-separated (unfiltered, never leave the Mac)")
    s.add_argument("--network", help="models of your network LLM gateway, comma-separated")
    s.add_argument("--name")
    s.add_argument("--url")
    s.add_argument("--account", help="Keychain account of the gateway key")
    s.set_defaults(fn=cmd_codex)

    s = sub.add_parser("claude", help="Claude Code: enable | disable | upstream URL")
    s.add_argument("action", choices=["enable", "disable", "upstream"])
    s.add_argument("url", nargs="?")
    s.set_defaults(fn=cmd_claude)

    s = sub.add_parser("mode", help="redact (replace) | audit (only count) | off")
    s.add_argument("mode", choices=["redact", "audit", "off"])
    s.set_defaults(fn=cmd_mode)

    s = sub.add_parser("pipeline", help="list | use NAME | add NAME --chat a,b --document a | remove NAME")
    s.add_argument("action", nargs="?", choices=["list", "use", "add", "remove"])
    s.add_argument("name", nargs="?")
    s.add_argument("--chat", help=f"detectors for chat messages: {', '.join(DETECTOR_NAMES)}")
    s.add_argument("--document", help="detectors for documents and tool output")
    s.add_argument("--about")
    s.set_defaults(fn=cmd_pipeline)

    s = sub.add_parser("detector", help="list | set NAME key=value ... (model, score, device, labels...)")
    s.add_argument("action", nargs="?", choices=["list", "set"])
    s.add_argument("name", nargs="?", choices=DETECTOR_NAMES)
    s.add_argument("values", nargs="*")
    s.set_defaults(fn=cmd_detector)

    s = sub.add_parser("llm", help="the LLM detector: server, model, your extra instruction; --test TEXT")
    s.add_argument("--api", choices=["openai", "ollama"])
    s.add_argument("--url", help="base URL; empty = Mac Intel Local AI's server, else Ollama on :11434")
    s.add_argument("--model")
    s.add_argument("--key-account", help="Keychain account (service pii-router) for a gateway key")
    s.add_argument("--allow-remote", action="store_true", help="allow a server that isn't on this Mac (your LAN)")
    s.add_argument("--local-only", action="store_true")
    s.add_argument("--instruction", help="your addition to the instruction ('' clears it)")
    s.add_argument("--instruction-file")
    s.add_argument("--timeout", type=float)
    s.add_argument("--max-chars", type=int, dest="max_chars")
    s.add_argument("--test", metavar="TEXT", help="ask the LLM now and show what it finds")
    s.set_defaults(fn=cmd_llm)

    s = sub.add_parser("dictionary", help="show | list | add 'Value = TYPE' ... | edit | path")
    s.add_argument("action", nargs="?", choices=["show", "list", "add", "edit", "path"], default="show")
    s.add_argument("entries", nargs="*")
    s.set_defaults(fn=cmd_dictionary)

    s = sub.add_parser("test", help="what the cloud would get for a text or --file (PDF, txt); sends nothing")
    s.add_argument("text", nargs="*")
    s.add_argument("--file", nargs="+")
    s.add_argument("--pipeline")
    s.add_argument("--context", choices=["chat", "document", "service"])
    s.set_defaults(fn=cmd_test)

    s = sub.add_parser("eval", help="compare pipelines on a labeled corpus (built-in or --corpus)")
    s.add_argument("--pipeline", action="append")
    s.add_argument("--all", action="store_true")
    s.add_argument("--corpus")
    s.add_argument("--verbose", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_eval)

    s = sub.add_parser("debug", help="on [MINUTES] | off: keep the last cloud requests as sent, in memory")
    s.add_argument("state", choices=["on", "off"])
    s.add_argument("minutes", nargs="?", type=float)
    s.set_defaults(fn=cmd_debug)
    sub.add_parser("sent", help="the last cloud requests as sent (debug mode)").set_defaults(fn=cmd_sent)

    s = sub.add_parser("hook", help=argparse.SUPPRESS)
    s.add_argument("event", choices=["session-start", "pre-tool-use"])
    s.set_defaults(fn=cmd_hook)
    return p


def main(argv=None) -> int:
    args = build().parse_args(argv)
    return args.fn(args) or 0
