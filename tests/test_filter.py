import json

from pii_router.filter import PiiFilter
from pii_router import config


def test_rules_tags_and_restore(flt):
    text = "Mail jane@example.com, IBAN DE89 3704 0044 0532 0130 00, card 4111 1111 1111 1111, call +49 30 1234567"
    stats = {}
    out = flt.redact_text(text, "chat", stats)
    for v in ("jane@example.com", "DE89 3704 0044 0532 0130 00", "4111 1111 1111 1111", "+49 30 1234567"):
        assert v not in out
    assert {"EMAIL", "IBAN", "CARD", "PHONE"} <= set(stats)
    assert flt.restore(out) == text


def test_same_value_same_tag(flt):
    a = flt.redact_text("write to jane@example.com", "chat", {})
    b = flt.redact_text("again jane@example.com!", "chat", {})
    assert a.split()[-1] == b.split()[-1].rstrip("!")


def test_dictionary_and_known_values_spread(flt):
    out = flt.redact_text("Notes on Projekt Falke for Jane Doe", "chat", {})
    assert "Falke" not in out and "Jane" not in out
    # Found once, the value is hidden in any context, also where only rules run.
    flt2_text = flt.redact_text('{"query": "jane doe"}', "service", {})
    assert "jane doe" not in flt2_text.lower() or "[PII:" in flt2_text


def test_no_tag_inside_tag(flt):
    out = flt.redact_text("[PII:PERSON_1] wrote 12345678901", "document", {})
    assert out.startswith("[PII:PERSON_1]")


def test_code_untouched(flt):
    code = "def f(street: str):\n    return street  # ts 1700000000\n"
    assert flt.redact_text(code, "document", {}) == code


def test_audit_mode_counts_only():
    cfg = config.load()
    cfg["filter"]["mode"] = "audit"
    f = PiiFilter(cfg, pipeline="rules", dictionary_file="")
    stats = {}
    text = "mail jane@example.com"
    assert f.redact_text(text, "chat", stats) == text
    assert stats == {"EMAIL": 1}


def test_restore_json_escapes(flt):
    tag = flt._tag("PERSON", 'Anna "Ann" O\\Brien')
    restored = flt.restore('{"to": "%s"}' % tag, json_string=True)
    assert json.loads(restored)["to"] == 'Anna "Ann" O\\Brien'


def test_unknown_pipeline():
    import pytest
    with pytest.raises(ValueError):
        PiiFilter(config.load(), pipeline="nope", dictionary_file="")


def test_dictionary_file(tmp_path):
    d = tmp_path / "dictionary.txt"
    d.write_text("# comment\nPetrov* = PERSON\nAcme Secret Lab\n", encoding="utf-8")
    f = PiiFilter(config.load(), pipeline="rules", dictionary_file=str(d))
    out = f.redact_text("Petrova from Acme Secret Lab, file Bank_Petrova_08.pdf", "chat", {})
    assert "Petrov" not in out and "Acme Secret Lab" not in out
