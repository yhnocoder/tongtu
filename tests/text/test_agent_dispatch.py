"""按端点家族分派与解析的文本层用例：不发真实请求，只看分派、请求形状与正文解析。

三个家族的请求形态互不兼容（见 tongtu/agent/api.py 模块文档），分派错了的表现是请求
直接被服务端拒绝，本地看不出来，故在这里把它们钉住。入口取 opencode 这个服务商：它的
模型表同时覆盖三个家族。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tongtu.agent import api, opencode
from tongtu.agent.base import ASK_STATUS_ERROR, ASK_STATUS_OK


@pytest.fixture(autouse=True)
def _stub_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥走环境变量这一级，免得本机有没有登录态影响结果。"""
    monkeypatch.setenv(opencode.API_KEY_ENV, "k")


class _FakeOpenAI:
    """记下 openai SDK 这一路收到的调用：家族名与关键字。"""

    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, **_: object) -> None:
        self.chat = self
        self.completions = self
        self.responses = self

    def create(self, **kwargs: Any) -> Any:
        if "instructions" in kwargs:
            _FakeOpenAI.calls.append(("responses", kwargs))
            return _Responses()
        _FakeOpenAI.calls.append(("chat", kwargs))
        return _Chat()


class _Responses:
    output_text = "responses 的正文"
    status = "completed"
    usage = None


class _Chat:
    choices = [
        type("Choice", (), {"finish_reason": "stop", "message": type("Message", (), {"content": "chat 的正文"})()})()
    ]
    usage = None


@pytest.fixture(autouse=True)
def _stub_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeOpenAI.calls = []
    monkeypatch.setattr(api.openai, "OpenAI", _FakeOpenAI)


class _FakeResponse:
    """假响应：默认 200、无重发头，只把 payload 交回去。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("deepseek-v4-flash", "chat"),
        ("glm-5.3", "chat"),
        ("muse-spark-1.2-contributor", "responses"),
        ("grok-4.5", "responses"),
        ("gpt-5.6-luna", "responses"),
    ],
)
def test_openai_sdk_models_dispatch_by_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str, family: str
) -> None:
    outcome = opencode.ask(prompt="p", text="t", model=model, schema=None, log_path=tmp_path / "log.json", effort="low")
    assert outcome.status == ASK_STATUS_OK
    assert [name for name, _ in _FakeOpenAI.calls] == [family]
    kwargs = _FakeOpenAI.calls[0][1]
    if family == "responses":
        assert kwargs["reasoning"] == {"effort": "low"}
        assert "reasoning_effort" not in kwargs
    else:
        assert kwargs["reasoning_effort"] == "low"
        assert "reasoning" not in kwargs


@pytest.mark.parametrize("model", ["minimax-m3", "qwen3.8-max", "qwen3.7-plus"])
def test_messages_models_go_through_httpx_with_the_anthropic_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str
) -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        seen.update(url=url, **kwargs)
        return _FakeResponse(
            {
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 4},
                "content": [
                    {"type": "thinking", "thinking": "思考不进正文"},
                    {"type": "text", "text": "messages 的正文"},
                ],
            }
        )

    monkeypatch.setattr(api.httpx, "post", fake_post)
    outcome = opencode.ask(prompt="p", text="t", model=model, schema=None, log_path=tmp_path / "log.json", effort="low")
    assert outcome.status == ASK_STATUS_OK
    assert outcome.text == "messages 的正文"
    assert not _FakeOpenAI.calls
    assert seen["url"].endswith("/messages")
    assert seen["headers"]["x-api-key"] == "k"
    assert "Authorization" not in seen["headers"]
    assert seen["json"]["system"] == "p"
    assert seen["json"]["messages"] == [{"role": "user", "content": "t"}]
    assert seen["json"]["max_tokens"] == api.MESSAGES_MAX_TOKENS
    assert seen["json"]["thinking"] == {"type": "enabled", "budget_tokens": api.THINKING_BUDGET_TOKENS["low"]}
    record = json.loads((tmp_path / "log.json").read_text(encoding="utf-8"))
    assert record["usage"] == {"input_tokens": 3, "output_tokens": 4}
    assert record["finish_reason"] == "end_turn"


def test_a_messages_response_without_a_text_block_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        api.httpx,
        "post",
        lambda url, **kwargs: _FakeResponse({"stop_reason": "max_tokens", "content": [{"type": "thinking"}]}),
    )
    outcome = opencode.ask(
        prompt="p", text="t", model="minimax-m3", schema=None, log_path=tmp_path / "log.json", effort=None
    )
    assert outcome.status == ASK_STATUS_ERROR
    assert "max_tokens" in outcome.detail


def test_the_messages_family_refuses_a_schema(tmp_path: Path) -> None:
    outcome = opencode.ask(
        prompt="p", text="t", model="qwen3.7-max", schema={"type": "object"}, log_path=tmp_path / "log.json"
    )
    assert outcome.status == ASK_STATUS_ERROR
    assert "messages 端点" in outcome.detail


class _StatusResponse(_FakeResponse):
    """带状态码与头的假响应：用于验证退避重发只针对可重试的状态码。"""

    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        super().__init__(payload or {"stop_reason": "end_turn", "content": [{"type": "text", "text": "正文"}]})
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


def _record_posts(monkeypatch: pytest.MonkeyPatch, statuses: list[int]) -> list[str]:
    seen: list[str] = []
    remaining = list(statuses)

    def fake_post(url: str, **kwargs: Any) -> _StatusResponse:
        seen.append(url)
        return _StatusResponse(remaining.pop(0) if remaining else 200)

    monkeypatch.setattr(api.httpx, "post", fake_post)
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)
    return seen


def test_a_rate_limited_messages_request_is_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _record_posts(monkeypatch, [429, 503, 200])
    outcome = opencode.ask(prompt="p", text="t", model="qwen3.7-max", schema=None, log_path=tmp_path / "log.json")
    assert outcome.status == ASK_STATUS_OK
    assert len(seen) == 3


def test_retries_stop_at_the_attempt_ceiling(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _record_posts(monkeypatch, [429, 429, 429, 429])
    outcome = opencode.ask(prompt="p", text="t", model="qwen3.7-max", schema=None, log_path=tmp_path / "log.json")
    assert outcome.status == ASK_STATUS_ERROR
    assert len(seen) == api.MESSAGES_RETRY_ATTEMPTS


def test_a_non_retriable_status_is_not_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _record_posts(monkeypatch, [400])
    outcome = opencode.ask(prompt="p", text="t", model="qwen3.7-max", schema=None, log_path=tmp_path / "log.json")
    assert outcome.status == ASK_STATUS_ERROR
    assert len(seen) == 1


def test_a_model_outside_the_family_table_is_refused(tmp_path: Path) -> None:
    """表里没有的模型不猜家族：本地直接报失败，不发请求。"""
    outcome = opencode.ask(prompt="p", text="t", model="没有这个模型", schema=None, log_path=tmp_path / "log.json")
    assert outcome.status == ASK_STATUS_ERROR
    assert "模型清单里没有" in outcome.detail
    assert not _FakeOpenAI.calls
