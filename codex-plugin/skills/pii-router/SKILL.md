---
name: pii-router
description: Set up, check and tune the local PII Router filter that replaces personal data with [PII:TYPE_n] tags before requests reach the cloud. Use when the user asks about the PII filter, [PII:...] tags, what was sent to the cloud, the dictionary of private values, detector pipelines, the LLM filter, or wants to test a text or file.
---

# PII Router

A local proxy (127.0.0.1:11500) sits between Codex (`openai_base_url`) and GPT / Ollama Cloud;
local and network models bypass it unfiltered. It
replaces personal data with tags such as `[PII:PERSON_1]` and puts the real values back
into answers and tool calls. The table of values lives only in the proxy's memory.

Tags in the conversation are expected: use them as they are, never ask the user for the
real value, and never try to undo the filter.

Run the CLI as `pii-router <command>` (install.sh links it into ~/.local/bin; else
`~/Library/Application\ Support/pii-router/venv/bin/pii-router`).

| Task | Command |
| --- | --- |
| Is this session filtered? | `pii-router status` |
| Packages, hardware, local LLM servers | `pii-router doctor` |
| Turn on for Codex | `pii-router codex enable` (then restart the app) |
| Local models in the model picker | `pii-router codex models --local qwen3:8b` |
| Only count, don't replace | `pii-router mode audit` |
| Pipelines (detector sets) | `pii-router pipeline list`, `pipeline use NAME` |
| Own pipeline | `pii-router pipeline add NAME --chat gliner,llm --document gliner` |
| Compare pipelines | `pii-router eval --all` (built-in synthetic corpus) |
| Local LLM filter | `pii-router llm --model NAME [--instruction TEXT] --test TEXT` |
| Private values | `pii-router dictionary add "Value = TYPE"` |
| What the cloud would get | `pii-router test TEXT` or `--file doc.pdf` (sends nothing) |
| What was sent | `pii-router debug on 30`, then `pii-router sent` |

Rules for you:
- Personal data the user types is filtered before it reaches you; don't echo values from
  `pii-router test` or `sent` output back into the chat beyond what the user asks for.
- `pii-router dictionary list` prints private values: run it only when the user asks.
- Never run `codex enable`, `codex models`, `install` or `uninstall` without the user
  asking: they change the user's agent settings.
- Use `pii-router test` (local) rather than sending a sample to a cloud model.
