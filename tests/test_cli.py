import json

from pii_router import cli, config, evaluate, integrations


def test_eval_rules_pipeline():
    r = evaluate.run(["rules"])["rules"]
    assert r["expected"] > 20
    assert not [a for a in r["alarms"] if a.startswith(("code-", "tech-", "tz-"))]
    assert r["recall"] > 0.4


def test_pipeline_add_and_use(capsys):
    assert cli.main(["pipeline", "add", "mine", "--chat", "llm", "--document", ""]) == 0
    assert cli.main(["pipeline", "use", "mine"]) == 0
    assert config.load()["filter"]["pipeline"] == "mine"
    assert cli.main(["pipeline", "use", "rules"]) == 0


def test_llm_instruction_saved(capsys):
    cli.main(["llm", "--model", "qwen3:4b", "--instruction", "Also hide pet names."])
    llm = config.detector_cfg(config.load(), "llm")
    assert llm["model"] == "qwen3:4b" and llm["instruction"] == "Also hide pet names."
    assert llm["allow_remote"] is False


def test_test_command(capsys):
    cli.main(["test", "--pipeline", "rules", "write", "to", "jane@example.com"])
    out = capsys.readouterr().out
    assert "jane@example.com" not in out and "[PII:EMAIL_1]" in out


def test_claude_enable_disable():
    s = integrations.CLAUDE_SETTINGS
    s.parent.mkdir(parents=True, exist_ok=True)
    s.write_text(json.dumps({"model": "opus"}))
    integrations.claude_enable(config.load())
    assert json.loads(s.read_text())["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11500"
    integrations.claude_disable(config.load())
    assert json.loads(s.read_text()) == {"model": "opus"}


def test_codex_enable_disable():
    t = integrations.CODEX_CONFIG
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text('model = "gpt-5"\nopenai_base_url = "http://127.0.0.1:11434/api/codex/v1"\n')
    integrations.codex_enable(config.load())
    assert 'openai_base_url = "http://127.0.0.1:11500/v1"' in t.read_text()
    integrations.codex_disable(config.load())
    assert 'openai_base_url = "http://127.0.0.1:11434/api/codex/v1"' in t.read_text()
