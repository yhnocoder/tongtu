from __future__ import annotations

import json
from pathlib import Path

import pytest

from tongtu.artifacts.survey import BriefFile, SurveyStatus
from tongtu.stages import mask, survey
from tongtu.workdir import Workdir

from ...conftest import paper_dir

pytestmark = pytest.mark.llm

TABLE = """
[provider.opencode]
base_url = "https://opencode.ai/zen/go/v1"
api_key_env = "OPENCODE_API_KEY"

[provider.opencode.models]
"deepseek-v4-flash" = "chat"

[roles]
survey_terms = { provider = "opencode", model = "deepseek-v4-flash", effort = "low" }
"""

PAPER = "revtex"


def test_survey_proposes_terms_with_a_real_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    models_path = tmp_path / "config" / "tongtu" / "models.toml"
    models_path.parent.mkdir(parents=True, exist_ok=True)
    models_path.write_text(TABLE, encoding="utf-8")
    workdir = Workdir(tmp_path / PAPER)
    workdir.create()
    (workdir.build / mask.PRECOMPILE_FILENAME).write_text(
        (paper_dir(PAPER) / "main.tex").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert mask.run(workdir).status.value == "ok"
    manifest = survey.run(workdir)
    assert manifest.status is SurveyStatus.OK, manifest.message
    log_path = workdir.logs / survey.TERMS_LOG_FILENAME
    assert log_path.is_file()
    record = json.loads(log_path.read_text(encoding="utf-8"))
    brief = BriefFile.model_validate_json((workdir.build / survey.BRIEF_FILENAME).read_text(encoding="utf-8"))
    print(f"provider {record['provider']} model {record['model']} duration {record['duration_seconds']:.1f}s")
    print(f"warnings {manifest.warnings}")
    print(f"terms {[(entry.word, entry.translation) for entry in brief.terms]}")
    print(f"do_not_translate {[entry.word for entry in brief.do_not_translate]}")
    print(f"filtered {[entry.word for entry in manifest.filtered]}")
