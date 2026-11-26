"""Rules: regular expressions with checks (IBAN mod-97, Luhn), form fields, the user's dictionary.

Pure Python, always available; every pipeline starts with them."""

from __future__ import annotations

import re
from pathlib import Path

TAG_RE = re.compile(r"\[PII:([A-Z_]+)_(\d+)\]")
# A stream fragment ending in what may become a tag: hold it back until complete.
TAG_PREFIX_RE = re.compile(r"\[(?:P(?:I(?:I(?::[A-Z_]*(?:\d+)?)?)?)?)?$")

# Blocks the agents add to every request (environment, AGENTS.md / CLAUDE.md,
# reminders): no models there, only rules and the dictionary.
SERVICE_BLOCKS = ("<environment_context", "<user_instructions", "# AGENTS.md", "<permissions",
                  "<INSTRUCTIONS>", "<skills_instructions", "<system-reminder>", "<command-name>",
                  "<local-command-stdout>")

EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
# International (+49 ..., +7 ...), German national (0171 ..., 030/...), Russian 8 (9xx) ...
PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[\s.-]?(?:\(\d{1,5}\)[\s.-]?)?\d{1,5}(?:[\s.-]?\d{2,5}){1,4}"
    r"|0\d{2,5}[\s/.-]\d{3,}(?:[\s.-]?\d{2,})*"
    r"|8[\s-]?\(\d{3}\)[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2})(?![\w])")
IBAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?(?![A-Za-z0-9])")
CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
SECRETS = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?<![\w-])sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}",
    r"(?<!\w)gh[pousr]_[A-Za-z0-9]{36,}",
    r"(?<!\w)github_pat_[A-Za-z0-9_]{22,}",
    r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])",
    r"(?<!\w)xox[abprs]-[A-Za-z0-9-]{10,}",
    r"(?<!\w)AIza[0-9A-Za-z_-]{35}",
    r"(?<!\w)hf_[A-Za-z0-9]{30,}",
    r"(?<![\w-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
)]
# Tax and company register numbers. After a keyword (ИНН, Steuernummer, VAT ID,
# HRB, ...) any number; without one only formats that are hard to mistake.
TAX_KEYWORD = re.compile(
    r"(?i)(?<!\w)(?:ИНН|КПП|ОГРНИП|ОГРН|ОКПО|ОКВЭД|БИК|СНИЛС|р/с|к/с|"
    r"Steuer-?ID|Steuer-?Nr\.?|Steuernummer|St\.?-?Nr\.?|USt\.?-?Id(?:Nr)?\.?|Ust-?IdNr\.?|"
    r"Umsatzsteuer-?(?:Identifikations)?(?:nummer|-?ID)|Identifikationsnummer|W-?IdNr\.?|"
    r"Handelsregister(?:nummer)?|HRB|HRA|Amtsgericht\s+\w+\s+HRB|"
    r"Tax\s*(?:ID|No\.?|number)|TIN|EIN|VAT(?:\s*(?:ID|Reg\.?|No\.?|number))?|UID(?:-?Nr\.?)?|Company\s+(?:No\.?|number))"
    r"(?![\w-])\s*(?:[:№#]|Nr\.?|No\.?)?\s*([A-Z]{0,4}[ -]?\d[\d \-/.]{2,24}\d)(?![\d])")
VAT_ID = re.compile(
    r"(?<![A-Z0-9])(?:ATU\d{8}|BE0?\d{9}|DE\d{9}|DK\d{8}|ES[A-Z0-9]\d{7}[A-Z0-9]|FI\d{8}|FR[A-Z0-9]{2}\d{9}|"
    r"IT\d{11}|LU\d{8}|NL\d{9}B\d{2}|PL\d{10}|SE\d{12}|CHE-?\d{3}\.?\d{3}\.?\d{3}(?:\s?(?:MWST|TVA|IVA))?)"
    r"(?![A-Z0-9])")
STEUERNUMMER = re.compile(r"(?<![\d/])\d{2,3}/\d{3,4}/\d{4,5}(?![\d/])")
COMPANY = re.compile(
    r"(?:(?:ООО|ОАО|ЗАО|ПАО|АО|НКО|ИП)\s+[«\"„][^»\"“\n]{1,60}[»\"“]"
    r"|(?:[A-ZÄÖÜ0-9][\w&.'-]*\s){1,4}(?:GmbH(?:\s&\sCo\.\s?KG)?|gGmbH|AG|KGaA|KG|OHG|UG(?:\s\(haftungsbeschränkt\))?|"
    r"GbR|e\.\s?K\.|e\.\s?V\.|SE|Ltd\.?|LLC|Inc\.?|Corp\.?|S\.A\.|S\.r\.l\.|B\.V\.|N\.V\.)(?![\w]))")
FORM_LABELS = {
    "ADDRESS": r"Straße|Strasse|Str\.|Hausnummer|Hausnr\.?|Postleitzahl|PLZ|Wohnort|Ort|Stadt|Город|City|Anschrift|Adresse|"
               r"Улица|Дом|Квартира|Индекс|Адрес|Street|Address|ZIP|Postcode|Postal\s+code",
    "PERSON": r"Vorname|Nachname|Familienname|Geburtsname|Name\s+des\s+\w+|Kontoinhaber|Inhaber|"
              r"ФИО|Фамилия|Имя|Отчество|Full\s+name|First\s+name|Last\s+name|Account\s+holder",
    "DATE_OF_BIRTH": r"Geburtsdatum|geboren\s+am|Дата\s+рождения|Date\s+of\s+birth|DOB",
    "ID_NUMBER": r"Kundennummer|Kunden-?Nr\.?|Vertragsnummer|Vertrags-?Nr\.?|Mandatsreferenz|Gläubiger-?ID|"
                 r"Versichertennummer|Versicherungsnummer|Mitgliedsnummer|Personalausweis(?:nummer)?|Reisepass(?:nummer)?|"
                 r"Kontonummer|Konto-?Nr\.?|Номер\s+(?:договора|счёта|счета|паспорта)|Паспорт|Customer\s+(?:No\.?|number|ID)|"
                 r"Contract\s+(?:No\.?|number)|Account\s+(?:No\.?|number)|Passport(?:\s+No\.?)?",
}
# Label, then ":" or a column gap (2+ spaces or a tab), then the value up to the line end.
# Case-sensitive: form labels are capitalized ("Straße", "ФИО"); code ("street: str") isn't.
FORM_FIELD = re.compile(r"(?m)(?<![\w-])(?:" + "|".join(f"(?P<{k}>{v})" for k, v in FORM_LABELS.items()) +
                        r")(?:\s*:[ \t]*|[ \t]{2,}|\t)(?P<value>[^\s:][^\n]{0,70}?)[ \t]*(?=\n|\\n|$)")
LONG_NUMBER = re.compile(r"(?<![\w.,/-])\d{8,}(?![\w.,/-])")
LOOPBACK = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    s = value.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1
    except ValueError:
        return False


def _private_ip(ip: str) -> bool:
    a, b = (int(x) for x in ip.split(".")[:2])
    return a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)


def rule_spans(text: str, cfg: dict, deny: list) -> list:
    """(start, end, TYPE) found by the rules."""
    spans = []
    for rx in SECRETS:
        spans += [(m.start(), m.end(), "SECRET") for m in rx.finditer(text)]
    spans += [(m.start(), m.end(), "EMAIL") for m in EMAIL.finditer(text)]
    spans += [(m.start(), m.end(), "IBAN") for m in IBAN.finditer(text) if _iban_ok(m.group())]
    for m in CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits) and len(set(digits)) > 1:
            spans.append((m.start(), m.end(), "CARD"))
    for m in PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 9 <= len(digits) <= 15:
            spans.append((m.start(), m.end(), "PHONE"))
    for m in IPV4.finditer(text):
        ip = m.group()
        if ip not in LOOPBACK and (cfg.get("private_ips", True) or not _private_ip(ip)):
            spans.append((m.start(), m.end(), "IP"))
    if cfg.get("tax_ids", True):
        spans += [(m.start(1), m.end(1), "TAX_ID") for m in TAX_KEYWORD.finditer(text)]
        spans += [(m.start(), m.end(), "TAX_ID") for m in VAT_ID.finditer(text)]
        spans += [(m.start(), m.end(), "TAX_ID") for m in STEUERNUMMER.finditer(text)]
    if cfg.get("form_fields", True):
        for m in FORM_FIELD.finditer(text):
            kind = next(k for k in FORM_LABELS if m.group(k))
            if any(c.isalnum() for c in m.group("value")) and not m.group("value").startswith("[PII:"):
                spans.append((m.start("value"), m.end("value"), kind))
    if cfg.get("companies", True):
        spans += [(m.start(), m.end(), "COMPANY") for m in COMPANY.finditer(text)]
    for rx, kind in deny:
        spans += [(m.start(), m.end(), kind) for m in rx.finditer(text)]
    return spans


def looks_like_prose(text: str) -> bool:
    """Code punctuation is rare and letters and spaces are most of it: a document (also an
    invoice or statement full of numbers), not source code, JSON or a log."""
    if len(text) < 40:
        return False
    code = sum(text.count(c) for c in "{}();=<>[]_$\\|`")
    letters = sum(c.isalpha() or c.isspace() for c in text)
    return code / len(text) < 0.015 and letters / len(text) > 0.6


def merge(spans: list) -> list:
    """Drop overlaps: earlier start wins, then the longer span."""
    out = []
    for s in sorted(spans, key=lambda x: (x[0], -(x[1] - x[0]))):
        if out and s[0] < out[-1][1]:
            continue
        out.append(s)
    return out


def language(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    if sum("Ѐ" <= c <= "ӿ" for c in letters) / len(letters) > 0.3:
        return "ru"
    low = f" {text.lower()} "
    if any(c in text for c in "äöüßÄÖÜ") or sum(f" {w} " in low for w in ("ich", "und", "nicht", "ist", "der", "die", "das", "mit")) >= 2:
        return "de"
    return "en"



def dictionary_entries(path: str) -> list:
    """Lines "value = TYPE" (TYPE optional, default PRIVATE); "#" starts a comment."""
    entries = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        value, _, kind = line.rpartition("=") if "=" in line else (line, "", "")
        value, kind = value.strip(), (kind.strip().upper() or "PRIVATE")
        if value and re.fullmatch(r"[A-Z_]+", kind):
            entries.append((value, kind))
    return entries


def value_pattern(value: str):
    """Case-insensitive whole-word pattern; "*" = any word ending; spaces match any whitespace."""
    words = [re.escape(w.rstrip("*")) + (r"[^\W\d_]*" if w.endswith("*") else "") for w in value.split()]
    # A letter must not touch the value; "_", "-", digits may (file names: Bank_Inna_08.pdf).
    return re.compile(r"(?<![^\W\d_])" + r"[\s_-]+".join(words) + r"(?![^\W\d_])", re.I)


