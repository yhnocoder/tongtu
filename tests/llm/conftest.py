from __future__ import annotations

import os

import pytest

from tongtu.model.config import load_config, provider_key


def opencode_key() -> str | None:
    value = os.environ.get("OPENCODE_API_KEY")
    if value:
        return value
    config, _ = load_config()
    if config is None or "opencode" not in config.provider:
        return None
    key, _ = provider_key("opencode", config.provider["opencode"])
    return key


@pytest.fixture
def opencode_env(monkeypatch: pytest.MonkeyPatch) -> str:
    key = opencode_key()
    if not key:
        pytest.skip("没有 OPENCODE_API_KEY，models.toml 里也没写 opencode 的密钥")
    monkeypatch.setenv("OPENCODE_API_KEY", key)
    return key
