import os
import sys
import tempfile
from pathlib import Path

# Never touch the real settings, dictionary, Codex or Claude Code config in tests.
_tmp = tempfile.mkdtemp(prefix="pii-router-test-")
os.environ["PII_ROUTER_HOME"] = _tmp
os.environ["CODEX_HOME"] = str(Path(_tmp) / "codex")
os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_tmp) / "claude")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture
def flt():
    from pii_router import config
    from pii_router.filter import PiiFilter
    cfg = config.load()
    cfg["filter"]["deny_list"] = {"Jane Doe": "PERSON", "Projekt Falke": "PROJECT"}
    return PiiFilter(cfg, pipeline="rules", dictionary_file="")
