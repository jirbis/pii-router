"""Model-based detectors. Each one is optional: missing packages make it "unavailable".

A detector finds (start, end, TYPE) spans in a text. The pipeline (filter.py)
decides which detectors run for chat messages and which for documents (tool
output: files and PDFs the agent read). Rules and the dictionary always run
first and win over any model.

  spacy    Microsoft Presidio + spaCy NER (PERSON, LOCATION; ru/de/en models)
  gliner   GLiNER, a small model trained for PII (urchade/gliner_multi_pii-v1)
  hf_ner   any Hugging Face token-classification model (e.g. Piiranha)
  llm      a local or network LLM asked to list the personal data in a text
"""

from __future__ import annotations

import importlib.util
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from .rules import language

DEFAULTS = {
    "spacy": {
        "models": {"en": "en_core_web_md", "de": "de_core_news_md", "ru": "ru_core_news_md"},
        "entities": ["PERSON", "LOCATION"],
        "score": 0.6,
    },
    "gliner": {
        "model": "urchade/gliner_multi_pii-v1",
        "score": 0.5,
        # auto: the GPU (MPS) on Apple Silicon, the CPU on Intel Macs, where PyTorch
        # 2.2's MPS on AMD Radeon is slower and misses entities. The GPU is used only
        # if it finds the same entities as the CPU on a check sentence.
        "device": "auto",
        "labels": {"person": "PERSON", "street address": "ADDRESS", "date of birth": "DATE_OF_BIRTH",
                   "passport number": "ID_NUMBER", "bank account number": "ACCOUNT",
                   "company": "COMPANY", "tax identification number": "TAX_ID"},
    },
    "hf_ner": {
        # Piiranha: DeBERTa-v3 fine-tuned for 17 PII types in en/de/es/fr/it/nl (CC BY-NC-ND 4.0).
        "model": "iiiorg/piiranha-v1-detect-personal-information",
        "score": 0.6,
        "device": "cpu",
        "labels": {"GIVENNAME": "PERSON", "SURNAME": "PERSON", "USERNAME": "USERNAME", "EMAIL": "EMAIL",
                   "TELEPHONENUM": "PHONE", "STREET": "ADDRESS", "BUILDINGNUM": "ADDRESS", "ZIPCODE": "ADDRESS",
                   "CITY": "LOCATION", "DATEOFBIRTH": "DATE_OF_BIRTH", "IDCARDNUM": "ID_NUMBER",
                   "DRIVERLICENSENUM": "ID_NUMBER", "SOCIALNUM": "ID_NUMBER", "TAXNUM": "TAX_ID",
                   "ACCOUNTNUM": "ACCOUNT", "CREDITCARDNUMBER": "CARD", "PASSWORD": "SECRET"},
    },
    "llm": {
        # api: "openai" (any /v1/chat/completions: llama.cpp, LM Studio, MLX, LiteLLM, vLLM)
        #      or "ollama" (/api/chat).
        "api": "openai",
        # Empty: the server selected in Mac Intel Local AI (active.json), else Ollama on :11434.
        "base_url": "",
        "model": "",
        # API key from the Keychain (service pii-router, this account), for a network gateway.
        "keychain_account": "",
        # The LLM sees the raw text, so a server that isn't on this Mac must be allowed
        # explicitly (your own LAN gateway, never a public cloud).
        "allow_remote": False,
        # Your additions to the instruction, e.g. "Also treat project code names as PII."
        "instruction": "",
        "timeout": 60,
        "max_chars": 3000,
        "types": ["PERSON", "ADDRESS", "LOCATION", "COMPANY", "ID_NUMBER", "DATE_OF_BIRTH", "ACCOUNT",
                  "USERNAME", "PRIVATE"],
    },
}

ACTIVE = "~/Library/Application Support/Mac Intel Local AI/active.json"


def chunks(text: str, size: int):
    """(offset, piece) of at most `size` characters, cut at a newline or space."""
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + size)
        if end < len(text):
            cut = max(text.rfind("\n", pos, end), text.rfind(" ", pos, end))
            end = cut + 1 if cut > pos else end
        yield pos, text[pos:end]
        pos = end


class Detector:
    """Loads in the background; spans() waits for it and returns [] when unavailable."""
    name = "?"
    requires: tuple = ()

    def __init__(self, cfg: dict, background: bool = True):
        self.cfg = cfg
        self.ready = threading.Event()
        self.error = ""
        self.lock = threading.Lock()
        self.ok = False
        if not all(importlib.util.find_spec(m) for m in self.requires):
            self.error = "not installed: pip install " + " ".join(self.requires)
            self.ready.set()
        elif background:
            threading.Thread(target=self._start, daemon=True).start()
        else:
            self._start()

    def _start(self):
        try:
            import warnings
            warnings.filterwarnings("ignore")
            self.load()
            self.ok = True
        except Exception as exc:  # a missing model, no network on first download...
            self.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            self.ready.set()

    def load(self):
        pass

    def find(self, text: str) -> list:
        raise NotImplementedError

    def spans(self, text: str, wait: float = 180) -> list:
        if not self.ready.wait(wait) or not self.ok:
            return []
        with self.lock:
            return self.find(text)

    def state(self) -> str:
        if not self.ready.is_set():
            return "loading"
        return ("ready" + (f" ({self.detail()})" if self.detail() else "")) if self.ok else \
            f"unavailable ({self.error})"

    def detail(self) -> str:
        return ""


class SpacyDetector(Detector):
    name = "spacy"
    requires = ("presidio_analyzer", "spacy")

    def load(self):
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_analyzer.predefined_recognizers import SpacyRecognizer
        models = [{"lang_code": lang, "model_name": name}
                  for lang, name in self.cfg["models"].items() if importlib.util.find_spec(name)]
        if not models:
            raise RuntimeError("no spaCy model installed (python -m spacy download en_core_web_md ...)")
        nlp = NlpEngineProvider(nlp_configuration={"nlp_engine_name": "spacy", "models": models}).create_engine()
        self.languages = [m["lang_code"] for m in models]
        registry = RecognizerRegistry(supported_languages=self.languages)
        for lang in self.languages:
            registry.add_recognizer(SpacyRecognizer(supported_language=lang,
                                                    supported_entities=list(self.cfg["entities"])))
        self.engine = AnalyzerEngine(nlp_engine=nlp, registry=registry, supported_languages=self.languages)

    def find(self, text):
        lang = language(text)
        if lang not in self.languages:
            lang = "en" if "en" in self.languages else self.languages[0]
        found = self.engine.analyze(text=text, language=lang, entities=list(self.cfg["entities"]),
                                    score_threshold=float(self.cfg["score"]))
        return [(r.start, r.end, r.entity_type) for r in found]

    def detail(self):
        return ",".join(getattr(self, "languages", []))


def wanted_device(setting: str) -> str:
    if setting in ("cpu", "mps", "cuda"):
        return setting
    import platform
    return "mps" if platform.system() == "Darwin" and platform.machine() == "arm64" else "cpu"


class GlinerDetector(Detector):
    name = "gliner"
    requires = ("gliner",)
    CHUNK = 1200
    CHECK = ("Меня зовут Иван Петров, живу на улице Ленина 12 в Москве, моя жена Маша родилась 3 марта 1985. "
             "Ich heiße Anna Schmidt, Hauptstraße 5, 10115 Berlin. Ask Jane Doe, passport X1234567.")
    device = "cpu"
    note = ""

    def load(self):
        from gliner import GLiNER
        self.model = GLiNER.from_pretrained(self.cfg["model"])
        if wanted_device(str(self.cfg.get("device", "auto"))) == "mps":
            self._try_mps()
        self.model.predict_entities("warm up", list(self.cfg["labels"]))

    def _found(self, text):
        return {(e["start"], e["end"], e["label"]) for e in
                self.model.predict_entities(text, list(self.cfg["labels"]), threshold=float(self.cfg["score"]))}

    def _try_mps(self):
        """Move to the GPU only if it finds what the CPU finds on the check sentence."""
        try:
            import torch
            if not torch.backends.mps.is_available():
                self.note = "MPS not available, using the CPU"
                return
            expected = self._found(self.CHECK)
            self.model = self.model.to("mps")
            if self._found(self.CHECK) == expected:
                self.device = "mps"
                return
            self.note = "MPS results differ from the CPU, using the CPU"
        except Exception as exc:
            self.note = f"MPS failed ({type(exc).__name__}), using the CPU"
        self.model = self.model.to("cpu")
        self.device = "cpu"

    def find(self, text):
        labels = dict(self.cfg["labels"])
        out = []
        for off, piece in chunks(text, self.CHUNK):
            if any(c.isalpha() for c in piece):
                for e in self.model.predict_entities(piece, list(labels), threshold=float(self.cfg["score"])):
                    out.append((off + e["start"], off + e["end"], labels.get(e["label"], "PRIVATE")))
        return out

    def detail(self):
        return self.device + (f"; {self.note}" if self.note else "")


class HfNerDetector(Detector):
    name = "hf_ner"
    requires = ("transformers", "torch")
    CHUNK = 1500

    def load(self):
        from transformers import pipeline
        dev = self.cfg.get("device", "cpu")
        self.pipe = pipeline("token-classification", model=self.cfg["model"], aggregation_strategy="simple",
                             device=wanted_device(dev) if dev != "cpu" else -1)
        self.pipe("warm up")

    def find(self, text):
        labels = self.cfg["labels"]
        out = []
        for off, piece in chunks(text, self.CHUNK):
            for e in self.pipe(piece):
                group = str(e.get("entity_group") or e.get("entity") or "").split("-")[-1]
                if float(e.get("score", 0)) >= float(self.cfg["score"]) and group in labels:
                    start, end = off + int(e["start"]), off + int(e["end"])
                    while start < end and text[start].isspace():
                        start += 1
                    if end > start:
                        out.append((start, end, labels[group]))
        return merge_adjacent(out, text)

    def detail(self):
        return self.cfg["model"].split("/")[-1]


def merge_adjacent(spans: list, text: str) -> list:
    """Token models split "Anna Schmidt" into GIVENNAME + SURNAME: join same-type neighbours."""
    out = []
    for s in sorted(spans):
        if out and out[-1][2] == s[2] and text[out[-1][1]:s[0]].strip(" ,") == "":
            out[-1] = (out[-1][0], s[1], s[2])
        else:
            out.append(s)
    return out


# ---------------------------------------------------------------- LLM

LLM_INSTRUCTION = """You find personal and confidential data in a text for a privacy filter.
List every piece of it exactly as written in the text: names of people (also first names alone),
street addresses, cities of residence, birth dates, ID, passport, tax, customer, contract and account
numbers, usernames, company names of private persons' employers or clients, and anything else that
identifies a person. Do not list: programming terms, product or brand names of software, public
places mentioned as general facts, dates that are not birth dates, amounts of money, text already
replaced by a tag like [PII:PERSON_1].
Answer with JSON only: {"pii": [{"text": "<exact substring>", "type": "<TYPE>"}]}
with TYPE one of: %s. Answer {"pii": []} if there is none."""


def local_host(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def active_llm() -> tuple[str, str]:
    """(base_url, model) of the server selected in Mac Intel Local AI, if it's installed."""
    from pathlib import Path
    try:
        a = json.loads(Path(ACTIVE).expanduser().read_text(encoding="utf-8"))
        return a.get("openai_base_url") or "", a.get("model") or ""
    except (OSError, ValueError):
        return "", ""


class LlmDetector(Detector):
    name = "llm"

    def load(self):
        base, model = self.cfg.get("base_url") or "", self.cfg.get("model") or ""
        if not base:
            act_base, act_model = active_llm()
            base = act_base or ("http://127.0.0.1:11434" if self.cfg["api"] == "ollama"
                                else "http://127.0.0.1:11434/v1")
            model = model or act_model
        if not model:
            raise RuntimeError("no model set: pii-router llm --model NAME")
        if not local_host(base) and not self.cfg.get("allow_remote"):
            raise RuntimeError(f"{base} isn't on this Mac; allow it with pii-router llm --allow-remote "
                               "(only your own server: the LLM sees the raw text)")
        self.base, self.model = base.rstrip("/"), model
        self.key = None
        if self.cfg.get("keychain_account"):
            from .config import keychain_key
            self.key = keychain_key(self.cfg["keychain_account"])

    def detail(self):
        return f"{self.model} @ {self.base}"

    def ask(self, text: str) -> list:
        system = LLM_INSTRUCTION % ", ".join(self.cfg["types"])
        if self.cfg.get("instruction"):
            system += "\n\nAdditional instruction from the user:\n" + self.cfg["instruction"].strip()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
        if self.cfg["api"] == "ollama":
            url, body = self.base + "/api/chat", {"model": self.model, "messages": messages, "stream": False,
                                                 "format": "json", "options": {"temperature": 0}}
        else:
            url, body = self.base + "/chat/completions", {"model": self.model, "messages": messages,
                                                         "temperature": 0, "stream": False,
                                                         "response_format": {"type": "json_object"}}
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(url, json.dumps(body).encode(), headers)
        with urllib.request.urlopen(req, timeout=float(self.cfg["timeout"])) as r:
            data = json.loads(r.read())
        content = data["message"]["content"] if self.cfg["api"] == "ollama" else \
            data["choices"][0]["message"]["content"]
        return parse_llm_answer(content)

    def find(self, text):
        out = []
        for off, piece in chunks(text, int(self.cfg["max_chars"])):
            if not any(c.isalpha() or c.isdigit() for c in piece):
                continue
            try:
                items = self.ask(piece)
            except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
                self.error = f"last call failed: {type(exc).__name__}: {exc}"[:200]
                continue
            allowed = set(self.cfg["types"])
            for value, kind in items:
                kind = kind if kind in allowed else "PRIVATE"
                if len(value.strip()) < 2:
                    continue
                for m in re.finditer(re.escape(value), piece):
                    out.append((off + m.start(), off + m.end(), kind))
        return out


def parse_llm_answer(content: str) -> list:
    """[(text, TYPE)] from the model's JSON, tolerating code fences and extra prose."""
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    items = data.get("pii") if isinstance(data, dict) else None
    out = []
    for it in items or []:
        if isinstance(it, dict) and isinstance(it.get("text"), str):
            out.append((it["text"], str(it.get("type") or "PRIVATE").upper().replace(" ", "_")))
        elif isinstance(it, str):
            out.append((it, "PRIVATE"))
    return out


KINDS = {"spacy": SpacyDetector, "gliner": GlinerDetector, "hf_ner": HfNerDetector, "llm": LlmDetector}


def make(name: str, cfg: dict, background: bool = True) -> Detector:
    merged = dict(DEFAULTS.get(name, {}))
    merged.update(cfg or {})
    return KINDS[name](merged, background=background)
