from __future__ import annotations

import json
import types
from pathlib import Path

import anthropic
import httpx
import openai
import pytest

from tongtu.model.ask import ASK_TIMEOUT_SECONDS, THINKING_BUDGET_TOKENS, AskStatus, ask

TABLE = """
[provider.demo]
base_url = "https://demo.example"
api_key_env = "DEMO_KEY"

[provider.demo.models]
"chat-model" = "chat"
"responses-model" = "responses"
"messages-model" = "messages"

[provider.odd]
base_url = "https://odd.example"
api_key_env = "ODD_KEY"
api = "grpc"

[provider.inline]
base_url = "https://inline.example"
api_key = "written-key"
api = "chat"

[roles]
chat_role = { provider = "demo", model = "chat-model", effort = "low" }
responses_role = { provider = "demo", model = "responses-model", effort = "high" }
messages_role = { provider = "demo", model = "messages-model", effort = "medium" }
messages_xhigh_role = { provider = "demo", model = "messages-model", effort = "xhigh" }
unknown_model_role = { provider = "demo", model = "other-model", effort = "low" }
ghost_role = { provider = "ghost", model = "chat-model", effort = "low" }
odd_role = { provider = "odd", model = "any-model", effort = "low" }
inline_role = { provider = "inline", model = "chat-model", effort = "low" }
work_role = { runtime = "claude_code", model = "m", effort = "low", max_turns = 4, timeout_seconds = 60 }
"""

MESSAGES = [("user", "把下面这句话译成中文：Hello, world.")]


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "tongtu" / "models.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TABLE, encoding="utf-8")
    monkeypatch.setenv("DEMO_KEY", "demo-key")
    monkeypatch.setenv("ODD_KEY", "odd-key")
    return tmp_path


def usage_stub() -> types.SimpleNamespace:
    return types.SimpleNamespace(model_dump=lambda **_: {"input_tokens": 7})


def openai_stub(recorded: dict, response: object) -> object:
    def create(**kwargs: object) -> object:
        recorded.update(kwargs)
        if isinstance(response, Exception):
            raise response
        return response

    def factory(**kwargs: object) -> object:
        recorded["client"] = kwargs
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
            responses=types.SimpleNamespace(create=create),
        )

    return factory


def anthropic_stub(recorded: dict, response: object) -> object:
    def create(**kwargs: object) -> object:
        recorded.update(kwargs)
        if isinstance(response, Exception):
            raise response
        return response

    def factory(**kwargs: object) -> object:
        recorded["client"] = kwargs
        return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))

    return factory


def chat_response(content: str | None, finish_reason: str = "stop") -> types.SimpleNamespace:
    choice = types.SimpleNamespace(message=types.SimpleNamespace(content=content), finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], usage=usage_stub())


def responses_response(text: str, status: str = "completed") -> types.SimpleNamespace:
    return types.SimpleNamespace(output_text=text, status=status, usage=usage_stub())


def messages_response(text: str, stop_reason: str = "end_turn") -> types.SimpleNamespace:
    blocks = [
        types.SimpleNamespace(type="thinking", thinking="想一想"),
        types.SimpleNamespace(type="text", text=text),
    ]
    return types.SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage_stub())


def read_log(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_chat_request_shape_and_log(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(openai, "OpenAI", openai_stub(recorded, chat_response("你好，世界。")))
    log_path = configured / "logs" / "chat.json"
    outcome = ask("chat_role", "你是译者", MESSAGES, log_path=log_path)
    assert outcome.status == AskStatus.OK
    assert outcome.text == "你好，世界。"
    assert recorded["client"] == {
        "base_url": "https://demo.example/v1",
        "api_key": "demo-key",
        "timeout": ASK_TIMEOUT_SECONDS,
        "max_retries": 1,
    }
    assert recorded["model"] == "chat-model"
    assert recorded["reasoning_effort"] == "low"
    assert recorded["messages"] == [
        {"role": "system", "content": "你是译者"},
        {"role": "user", "content": MESSAGES[0][1]},
    ]
    assert "response_format" not in recorded
    record = read_log(log_path)
    assert record["provider"] == "demo"
    assert record["model"] == "chat-model"
    assert record["effort"] == "low"
    assert record["system"] == "你是译者"
    assert record["messages"] == [["user", MESSAGES[0][1]]]
    assert record["schema"] is None
    assert record["status"] == "ok"
    assert record["response"] == "你好，世界。"
    assert record["detail"] == ""
    assert record["finish_reason"] == "stop"
    assert record["usage"] == {"input_tokens": 7}
    assert record["duration_seconds"] >= 0


def test_chat_passes_schema(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(openai, "OpenAI", openai_stub(recorded, chat_response('{"a": 1}')))
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    outcome = ask("chat_role", "", MESSAGES, schema=schema, log_path=configured / "log.json")
    assert outcome.status == AskStatus.OK
    assert recorded["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "ask_response", "strict": True, "schema": schema},
    }
    assert read_log(configured / "log.json")["schema"] == schema


def test_chat_multi_turn_messages(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(openai, "OpenAI", openai_stub(recorded, chat_response("好")))
    ask(
        "chat_role",
        "系统",
        [("user", "一"), ("assistant", "二"), ("user", "三")],
        log_path=configured / "log.json",
    )
    assert [item["role"] for item in recorded["messages"]] == ["system", "user", "assistant", "user"]


def test_responses_request_shape(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(openai, "OpenAI", openai_stub(recorded, responses_response("你好")))
    outcome = ask("responses_role", "你是译者", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.OK
    assert outcome.text == "你好"
    assert recorded["instructions"] == "你是译者"
    assert recorded["input"] == [{"role": "user", "content": MESSAGES[0][1]}]
    assert recorded["client"] == {
        "base_url": "https://demo.example/v1",
        "api_key": "demo-key",
        "timeout": ASK_TIMEOUT_SECONDS,
        "max_retries": 1,
    }
    assert recorded["reasoning"] == {"effort": "high"}
    assert read_log(configured / "log.json")["finish_reason"] == "completed"


def test_messages_request_shape(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(anthropic, "Anthropic", anthropic_stub(recorded, messages_response("你好")))
    outcome = ask("messages_role", "你是译者", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.OK
    assert outcome.text == "你好"
    assert recorded["client"] == {
        "base_url": "https://demo.example",
        "api_key": "demo-key",
        "timeout": ASK_TIMEOUT_SECONDS,
        "max_retries": 1,
    }
    assert recorded["system"] == "你是译者"
    assert recorded["max_tokens"] == 32768
    assert recorded["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert recorded["messages"] == [{"role": "user", "content": MESSAGES[0][1]}]
    assert read_log(configured / "log.json")["finish_reason"] == "end_turn"


def test_model_and_effort_overrides_are_applied(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(anthropic, "Anthropic", anthropic_stub(recorded, messages_response("你好")))
    outcome = ask(
        "chat_role",
        "系统",
        MESSAGES,
        log_path=configured / "log.json",
        model="demo/messages-model",
        effort="low",
    )
    assert outcome.status == AskStatus.OK
    assert recorded["model"] == "messages-model"
    assert recorded["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    record = read_log(configured / "log.json")
    assert record["provider"] == "demo"
    assert record["model"] == "messages-model"
    assert record["effort"] == "low"


def test_model_override_without_slash_is_error(configured: Path) -> None:
    outcome = ask("chat_role", "", MESSAGES, log_path=configured / "log.json", model="chat-model")
    assert outcome.status == AskStatus.ERROR
    assert "provider/model" in outcome.detail


def test_model_override_with_unknown_provider_is_error(configured: Path) -> None:
    outcome = ask("chat_role", "", MESSAGES, log_path=configured / "log.json", model="ghost/chat-model")
    assert outcome.status == AskStatus.ERROR
    assert "ghost" in outcome.detail


def test_missing_config_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    outcome = ask("chat_role", "", MESSAGES, log_path=tmp_path / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "tongtu setup" in outcome.detail
    assert read_log(tmp_path / "log.json")["status"] == "error"


def test_unknown_role_is_error(configured: Path) -> None:
    outcome = ask("nobody", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "nobody" in outcome.detail


def test_role_without_provider_is_error(configured: Path) -> None:
    outcome = ask("work_role", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "provider" in outcome.detail


def test_unknown_provider_is_error(configured: Path) -> None:
    outcome = ask("ghost_role", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "ghost" in outcome.detail


def test_model_without_api_is_error(configured: Path) -> None:
    outcome = ask("unknown_model_role", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "other-model" in outcome.detail


def test_unknown_api_value_is_error(configured: Path) -> None:
    outcome = ask("odd_role", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "grpc" in outcome.detail


def test_api_key_written_in_config_is_used(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(openai, "OpenAI", openai_stub(recorded, chat_response("你好")))
    outcome = ask("inline_role", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.OK
    assert recorded["client"] == {
        "base_url": "https://inline.example/v1",
        "api_key": "written-key",
        "timeout": ASK_TIMEOUT_SECONDS,
        "max_retries": 1,
    }


def test_missing_api_key_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_KEY", "")
    outcome = ask("chat_role", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "DEMO_KEY" in outcome.detail
    record = read_log(configured / "log.json")
    assert record["provider"] == "demo"
    assert record["model"] == "chat-model"


def test_schema_unsupported_on_responses(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(openai, "OpenAI", openai_stub(recorded, responses_response("你好")))
    outcome = ask("responses_role", "", MESSAGES, schema={"type": "object"}, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "schema" in outcome.detail


def test_schema_unsupported_on_messages(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(anthropic, "Anthropic", anthropic_stub(recorded, messages_response("你好")))
    outcome = ask("messages_role", "", MESSAGES, schema={"type": "object"}, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "schema" in outcome.detail


def test_effort_outside_thinking_table_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(anthropic, "Anthropic", anthropic_stub(recorded, messages_response("你好")))
    outcome = ask("messages_xhigh_role", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "xhigh" in outcome.detail
    assert recorded == {}


def test_empty_content_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(openai, "OpenAI", openai_stub(recorded, chat_response("", finish_reason="length")))
    outcome = ask("chat_role", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "length" in outcome.detail
    assert read_log(configured / "log.json")["finish_reason"] == "length"


def test_openai_error_is_reported(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(openai, "OpenAI", openai_stub(recorded, openai.OpenAIError("服务端拒绝")))
    outcome = ask("chat_role", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "服务端拒绝" in outcome.detail


def test_anthropic_error_is_reported(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(anthropic, "Anthropic", anthropic_stub(recorded, anthropic.AnthropicError("服务端拒绝")))
    outcome = ask("messages_role", "", MESSAGES, log_path=configured / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "服务端拒绝" in outcome.detail


def test_unwritable_log_is_error(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}
    monkeypatch.setattr(openai, "OpenAI", openai_stub(recorded, chat_response("你好")))
    blocker = configured / "blocker"
    blocker.write_text("", encoding="utf-8")
    outcome = ask("chat_role", "", MESSAGES, log_path=blocker / "log.json")
    assert outcome.status == AskStatus.ERROR
    assert "call log" in outcome.detail


def test_thinking_budget_table_matches_efforts() -> None:
    assert THINKING_BUDGET_TOKENS == {"low": 1024, "medium": 2048, "high": 4096}


def openai_sequence(calls: list[dict], responses: list[object]) -> object:
    def create(**kwargs: object) -> object:
        calls.append(kwargs)
        reply = responses[len(calls) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def factory(**kwargs: object) -> object:
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
            responses=types.SimpleNamespace(create=create),
        )

    return factory


def bad_request(message: str) -> openai.BadRequestError:
    request = httpx.Request("POST", "https://demo.example/v1/chat/completions")
    response = httpx.Response(400, request=request, json={"error": {"message": message}})
    return openai.BadRequestError(message, response=response, body=None)


def test_chat_falls_back_to_json_object_when_the_schema_format_is_refused(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    refusal = bad_request("This response_format type is unavailable now")
    monkeypatch.setattr(openai, "OpenAI", openai_sequence(calls, [refusal, chat_response('{"a": 1}')]))
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    log_path = configured / "log.json"
    outcome = ask("chat_role", "你是术语员", MESSAGES, schema=schema, log_path=log_path)
    assert outcome.status == AskStatus.OK
    assert outcome.text == '{"a": 1}'
    assert len(calls) == 2
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[1]["response_format"] == {"type": "json_object"}
    assert calls[0]["messages"][0]["content"] == "你是术语员"
    instructed = calls[1]["messages"][0]["content"]
    assert instructed.startswith("你是术语员")
    assert json.dumps(schema, ensure_ascii=False) in instructed
    assert calls[1]["messages"][1:] == calls[0]["messages"][1:]
    record = read_log(log_path)
    assert record["json_object_fallback"] is True
    assert record["schema"] == schema
    assert record["status"] == "ok"


def test_chat_with_a_working_schema_sends_one_request(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(openai, "OpenAI", openai_sequence(calls, [chat_response('{"a": 1}')]))
    log_path = configured / "log.json"
    outcome = ask("chat_role", "", MESSAGES, schema={"type": "object"}, log_path=log_path)
    assert outcome.status == AskStatus.OK
    assert len(calls) == 1
    assert "json_object_fallback" not in read_log(log_path)


def test_chat_does_not_retry_a_bad_request_about_something_else(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(openai, "OpenAI", openai_sequence(calls, [bad_request("Model not found")]))
    log_path = configured / "log.json"
    outcome = ask("chat_role", "", MESSAGES, schema={"type": "object"}, log_path=log_path)
    assert outcome.status == AskStatus.ERROR
    assert "Model not found" in outcome.detail
    assert len(calls) == 1
    assert "json_object_fallback" not in read_log(log_path)
