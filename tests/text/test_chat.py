from __future__ import annotations

import socket

import httpx
import pytest

from tongtu.model import chat
from tongtu.model.config import AbstractConfig

KEY = "sk-secret-key"

CONFIG = AbstractConfig(base_url="https://chat.example.com/", model="deepseek-flash", api_key=KEY)


def completion(content: object) -> dict:
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}


def wire_chat(monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception) -> list[dict]:
    calls: list[dict] = []

    def post(url: str, **kwargs: object) -> httpx.Response:
        calls.append({"url": url, **kwargs})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(chat.httpx, "post", post)
    return calls


def answer(status_code: int, body: object) -> httpx.Response:
    request = httpx.Request("POST", "https://chat.example.com/chat/completions")
    if isinstance(body, str):
        return httpx.Response(status_code, text=body, request=request)
    return httpx.Response(status_code, json=body, request=request)


def test_translate_abstract_sends_one_chat_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_chat(monkeypatch, answer(200, completion("  我们提出 Adam。\n")))
    assert chat.translate_abstract(CONFIG, "We introduce Adam.") == "我们提出 Adam。"
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://chat.example.com/chat/completions"
    assert call["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert call["timeout"] == chat.ABSTRACT_TIMEOUT_SECONDS == 60
    assert call["json"] == {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": chat.ABSTRACT_PROMPT},
            {"role": "user", "content": "We introduce Adam."},
        ],
        "temperature": chat.ABSTRACT_TEMPERATURE,
        "thinking": {"type": "disabled"},
    }
    assert chat.ABSTRACT_TEMPERATURE == 0.2


def test_translate_abstract_without_a_key_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_chat(monkeypatch, answer(200, completion("译文")))
    with pytest.raises(chat.ChatError, match="abstract.api_key"):
        chat.translate_abstract(AbstractConfig(), "We introduce Adam.")
    assert calls == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        answer(401, {"error": {"message": f"bad key {KEY}"}}),
        answer(500, "server error"),
        answer(200, "not json"),
        answer(200, {"choices": []}),
        answer(200, completion(None)),
        answer(200, completion("   ")),
        answer(200, {"result": "译文"}),
    ],
)
def test_translate_abstract_reports_an_expected_failure(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception
) -> None:
    wire_chat(monkeypatch, response)
    with pytest.raises(chat.ChatError) as raised:
        chat.translate_abstract(CONFIG, "We introduce Adam.")
    message = str(raised.value)
    assert KEY not in message
    assert len(message.splitlines()) == 1


def test_translate_abstract_reports_a_malformed_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CONFIG.model_copy(update={"base_url": "not a url"})
    with pytest.raises(chat.ChatError) as raised:
        chat.translate_abstract(config, "We introduce Adam.")
    assert KEY not in str(raised.value)


def unreachable_base_url(userinfo: str = "") -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://{userinfo}127.0.0.1:{port}/v1?token=FAKE-QUERY-TOKEN"


def test_a_real_network_error_names_only_its_type_and_host() -> None:
    config = AbstractConfig(base_url=unreachable_base_url("FAKE-USER:FAKE-PASS@"), api_key="sk-FAKE-SECRET-1")
    with pytest.raises(chat.ChatError) as raised:
        chat.translate_abstract(config, "We introduce Adam.")
    message = str(raised.value)
    assert message == "request to 127.0.0.1 failed: ConnectError"
    assert "sk-FAKE-SECRET-1" not in message
    assert "FAKE-USER" not in message
    assert "FAKE-PASS" not in message
    assert "FAKE-QUERY-TOKEN" not in message


@pytest.mark.parametrize("key", ["sk-FAKE\nSECRET-1", "sk-FAKE SECRET-1", "sk-FAKE\x00SECRET-1", "sk-FAKE-SECRET-é"])
def test_a_key_that_cannot_go_into_a_header_sends_nothing(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    calls = wire_chat(monkeypatch, answer(200, completion("译文")))
    with pytest.raises(chat.ChatError) as raised:
        chat.translate_abstract(CONFIG.model_copy(update={"api_key": key}), "We introduce Adam.")
    assert str(raised.value) == "abstract.api_key has characters that cannot be sent in a request header"
    assert calls == []


def test_a_key_with_a_trailing_newline_is_sent_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire_chat(monkeypatch, answer(200, completion("译文")))
    config = AbstractConfig(base_url="https://chat.example.com", api_key="  sk-FAKE-SECRET-1\n")
    assert chat.translate_abstract(config, "We introduce Adam.") == "译文"
    assert calls[0]["headers"] == {"Authorization": "Bearer sk-FAKE-SECRET-1"}


@pytest.mark.parametrize("response", [httpx.ConnectError("refused"), answer(500, "server error"), answer(200, "x")])
def test_a_failure_does_not_name_the_userinfo_of_the_base_url(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception
) -> None:
    wire_chat(monkeypatch, response)
    config = CONFIG.model_copy(update={"base_url": "https://FAKE-USER:FAKE-PASS@chat.example.com/v1"})
    with pytest.raises(chat.ChatError) as raised:
        chat.translate_abstract(config, "We introduce Adam.")
    assert "chat.example.com" in str(raised.value)
    assert "FAKE-USER" not in str(raised.value)
    assert "FAKE-PASS" not in str(raised.value)


def test_abstract_prompt_states_the_rules() -> None:
    prompt = chat.ABSTRACT_PROMPT
    assert "简体中文" in prompt
    assert "保留英文原文" in prompt
    assert "只输出译文" in prompt
