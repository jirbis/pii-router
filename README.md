# PII Router

A local proxy for macOS that keeps personal data out of cloud model requests.
Codex (ChatGPT app) and Claude Code send their requests to `127.0.0.1:11500`; the
proxy replaces names, addresses, emails, IBANs, tax numbers, secrets and your own
private values with tags such as `[PII:PERSON_1]`, forwards the request, and puts the
real values back into the answer and into tool calls. The table of values lives only
in the proxy's memory.

```
Claude Code ──/v1/messages──┐                      ┌── Anthropic           (filtered)
                            ├─ 127.0.0.1:11500 ────┼── GPT, Ollama Cloud    (filtered)
Codex app ───/v1/responses──┘   rules, dictionary, ├── local models         (unfiltered: never leave the Mac)
                                models, local LLM  └── your LAN gateway     (unfiltered: yours)
```

What you get in this repo:

| Part | Where | What it does |
| --- | --- | --- |
| Filter + proxy | `pii_router/` | detectors, pipelines, the OpenAI Responses and Anthropic Messages APIs, SSE restore |
| CLI | `pii-router` | install, settings, pipelines, LLM filter, tests and evaluation, debug view |
| Claude Code plugin | `claude-plugin/` | session check, a guard for tool calls that would send known personal data out, a skill |
| Codex plugin | `codex-plugin/` | a skill that teaches Codex the tags and the CLI |

## Install

```bash
git clone https://github.com/jirbis/pii-router && cd pii-router
./install.sh                    # profile picked from the hardware, proxy started as a LaunchAgent
pii-router doctor               # packages, hardware, local LLM servers
pii-router codex enable         # Codex / ChatGPT app (restart the app)
pii-router claude enable        # Claude Code (new sessions)
```

Profiles (`./install.sh --profile NAME`):

| Profile | For | Detectors | Size |
| --- | --- | --- | --- |
| `minimal` | any Mac | rules, dictionary, LLM (no Python ML packages) | ~30 MB |
| `intel` | Intel Mac | + Presidio/spaCy, GLiNER on PyTorch 2.2.2 (the last for x86_64), CPU | ~2 GB |
| `apple-silicon` | M1 and later | + Presidio/spaCy, GLiNER on current PyTorch, GPU (MPS) when it matches the CPU | ~2 GB |

`zstd` is needed for the Codex app (it compresses requests): `brew install zstd`.

Start in audit mode if you want to see what it would replace first:
`pii-router mode audit`, then look at `~/Library/Logs/pii-router.log` (types and counts only).

## Detectors and pipelines

Rules and your dictionary always run first and win over any model:
email, phone, IBAN (checksum), card (Luhn), IP, API keys and tokens, tax and VAT
numbers, companies with a legal form (`ООО «…»`, `… GmbH`), values after form labels
(`Straße  Main St.`, `ФИО: …`), long numbers.

Model detectors, each optional:

| Name | Package | Good at | Notes |
| --- | --- | --- | --- |
| `spacy` | Presidio + spaCy (en, de, ru) | names and places in chat | tags German form headings as names, so not used on documents by default |
| `gliner` | GLiNER `urchade/gliner_multi_pii-v1` | names, addresses, birth dates, IDs, companies; multilingual | ~100 ms per message on CPU |
| `hf_ner` | any Hugging Face token classifier, default Piiranha | 17 PII types in 6 languages | Piiranha's license is CC BY-NC-ND |
| `llm` | a local LLM server | context: "my wife Masha", project names, things no rule knows | slowest; your extra instruction |

A pipeline says which detectors run on chat messages and which on documents (tool
output: files, PDFs, command output; models only run on it when it reads like prose,
not code):

```bash
pii-router pipeline list
pii-router pipeline use accurate
pii-router pipeline add mine --chat gliner,llm --document gliner --about "GLiNER + LLM"
pii-router detector set gliner score=0.4 device=cpu
pii-router detector set hf_ner model=your/model
```

Built-in: `rules`, `default` (GLiNER), `broad` (GLiNER + spaCy in chat), `spacy`, `hf`,
`accurate` (models + LLM), `llm`.

Built-in corpus on an Intel MacBook Pro 2019 (CPU; LLM = qwen2.5:3b in Ollama on the Radeon GPU):

| pipeline | recall | precision | false alarms | ms / text |
| --- | --- | --- | --- | --- |
| rules | 64% | 97% | 1 | 0 |
| default (GLiNER) | 98% | 94% | 3 | ~200 |
| broad (GLiNER + spaCy) | 98% | 86% | 7 | ~280 |
| spacy | 76% | 87% | 5 | 5 |
| hf (Piiranha) | 78% | 92% | 3 | ~180 |
| llm (qwen2.5:3b) | 76% | 97% | 1 | ~14 000 |

A 3B LLM alone misses plain names; its use is the context in your instruction, on top of
GLiNER. A larger local model does better and is slower.

### Testing pipelines

```bash
pii-router test "Меня зовут Сергей Петров, почта s.petrov@example.ru"   # sends nothing
pii-router test --file statement.pdf --pipeline accurate
pii-router eval --all                     # recall, precision, false alarms, speed per pipeline
pii-router eval --pipeline mine --verbose # every miss and false alarm
pii-router eval --corpus my.jsonl         # your own labeled texts (they stay on your Mac)
```

The built-in corpus (`pii_router/data/corpus.jsonl`) is synthetic RU/DE/EN chat, forms,
invoices, secrets, and code that must not be touched. A corpus line:
`{"id": "x", "context": "chat", "text": "...", "pii": [["value", "TYPE"], ...]}`.

### The LLM filter

Optional. The LLM sees the raw text, so it must run on this Mac, or on your own server
in the LAN if you allow it explicitly; never a public cloud.

| Where | Setting |
| --- | --- |
| Intel Mac, CPU or Radeon GPU | Ollama / llama.cpp, e.g. via [Mac Intel Local AI](https://github.com/jirbis/mac-intel-local-ai): `pii-router llm --model qwen2.5:3b` (empty URL = the server selected there, else Ollama on :11434) |
| Apple Silicon | Ollama, LM Studio or `mlx_lm.server`: `pii-router llm --url http://127.0.0.1:8080/v1 --model mlx-community/Qwen2.5-3B-Instruct-4bit` |
| LAN gateway (LiteLLM, vLLM) | `pii-router llm --url http://gateway.lan:4000/v1 --model qwen3 --allow-remote --key-account gateway` and store the key: `security add-generic-password -s pii-router -a gateway -w` |

```bash
pii-router llm --api ollama --model qwen2.5:3b
pii-router llm --instruction "Also treat our project code names (Falcon, Heron) as PII."
pii-router llm --instruction-file my-instruction.txt
pii-router llm --test "My wife Masha works on Falcon"
pii-router pipeline use accurate
```

## Your dictionary

Values no model can know: relatives' names, your street, client and project names.

```bash
pii-router dictionary add "Petrov* = PERSON" "Falcon = PROJECT"
pii-router dictionary edit
```

`~/Library/Application Support/pii-router/dictionary.txt` (mode 600), one entry per line,
`*` matches word endings; re-read on change. A value found once (by any detector) is
hidden everywhere in the session after that, also inside tool arguments.

## What was sent

```bash
pii-router debug on 30     # keep the last 3 cloud requests as sent, in memory, for 30 min
pii-router sent
pii-router debug off
```

## Codex details

- GPT goes to `chatgpt.com/backend-api/codex` with the app's own login; `:cloud` models
  to Ollama (`/api/codex/v1`) or `ollama.com` with a key from the Keychain.
- Local models (`pii-router codex models --local qwen3:8b`) and a network gateway
  (`pii-router codex network --name Asgard --url ...`, `codex models --network m1,m2`)
  are added to Codex's model picker and are not filtered.

## Claude Code details

- `pii-router claude enable` sets `env.ANTHROPIC_BASE_URL` in `~/.claude/settings.json`
  (backed up once). A gateway you already used becomes the upstream.
- Filtered: system prompt, messages, tool calls and tool results. Not touched: thinking
  blocks (they are signed), so tags in thinking stay tags.
- Images and PDFs sent as binary aren't filtered (logged, and `filter.attachments: block`
  rejects such requests).

### Plugin

```
/plugin marketplace add jirbis/pii-router
/plugin install pii-router@pii-router
```

- **SessionStart**: says whether the session is filtered; starts a stopped proxy.
- **PreToolUse** (Bash with curl/ssh/gh/..., WebFetch, WebSearch): tool calls run with the
  real values, so a command could still send them off the Mac. If it contains personal
  data the filter knows, Claude Code asks you first.
- **Skill** `pii-router`: setup, tests and tuning from the chat.

A plugin can't set `ANTHROPIC_BASE_URL`, so `pii-router claude enable` is still needed.

### Codex plugin

Copy or link the skill: `ln -s "$PWD/codex-plugin/skills/pii-router" ~/.codex/skills/pii-router`,
or install `codex-plugin/` through a Codex plugin marketplace.

## Limits

- It finds what the detectors find. Amounts, merchant names and free-form descriptions
  usually pass; check with `pii-router test --file` and `debug`.
- Images, scans and binary attachments aren't read.
- The model sees tags, not values, so answers that depend on the value (spelling,
  gender from a name, a postcode's city) can be worse.

## Uninstall

```bash
pii-router uninstall    # agents back to what they used before, proxy stopped
rm -rf ~/Library/Application\ Support/pii-router ~/.local/bin/pii-router
```

## License

MIT
