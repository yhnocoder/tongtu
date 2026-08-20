from __future__ import annotations

from pathlib import Path

import pytest

from tongtu.model.ask import AskStatus, ask

pytestmark = pytest.mark.llm

TABLE = """
[provider.opencode]
base_url = "https://opencode.ai/zen/go/v1"
api_key_env = "OPENCODE_API_KEY"

[provider.opencode.models]
"deepseek-v4-flash" = "chat"
"gpt-5.6-luna" = "responses"
"qwen3.7-plus" = "messages"

[roles]
chat_role = { provider = "opencode", model = "deepseek-v4-flash", effort = "low" }
responses_role = { provider = "opencode", model = "gpt-5.6-luna", effort = "low" }
messages_role = { provider = "opencode", model = "qwen3.7-plus", effort = "low" }
"""


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "tongtu" / "models.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TABLE, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("role", ["chat_role", "responses_role", "messages_role"])
def test_ask_returns_translation(configured: Path, role: str) -> None:
    log_path = configured / "logs" / f"{role}.json"
    outcome = ask(
        role,
        "你是一名中英翻译，只输出译文。",
        [("user", "把下面这句话译成中文：Hello, world.")],
        log_path=log_path,
    )
    assert outcome.status == AskStatus.OK, outcome.detail
    assert outcome.text.strip()
    assert log_path.is_file()
    print(f"{role} 日志：{log_path}")
    print(f"{role} 正文：{outcome.text.strip()}")
