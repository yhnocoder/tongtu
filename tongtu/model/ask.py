from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import anthropic
import openai

from .config import Api, RoleTable, load_config, model_api, provider_key, resolve_role

SCHEMA_NAME = "ask_response"

DETAIL_EXCERPT_CHARS = 2000

MESSAGES_MAX_TOKENS = 32768

THINKING_BUDGET_TOKENS: dict[str, int] = {"low": 1024, "medium": 2048, "high": 4096}

MESSAGES_TIMEOUT_SECONDS = 600.0


class AskStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True)
class AskOutcome:
    status: AskStatus
    text: str = ""
    detail: str = ""


Reply = tuple[AskOutcome, dict[str, object]]


def ask(
    role: str,
    system: str,
    messages: list[tuple[str, str]],
    *,
    schema: dict | None = None,
    log_path: Path,
    model: str | None = None,
    effort: str | None = None,
) -> AskOutcome:
    started = time.monotonic()
    outcome, fields = _request(role, system, messages, schema, model, effort)
    record: dict[str, object] = {
        "provider": None,
        "model": None,
        "effort": None,
        "system": system,
        "messages": [list(item) for item in messages],
        "schema": schema,
        "finish_reason": None,
        "usage": None,
    }
    record.update(fields)
    record.update(
        {
            "status": outcome.status,
            "response": outcome.text,
            "detail": outcome.detail,
            "duration_seconds": time.monotonic() - started,
        }
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        return AskOutcome(
            status=AskStatus.ERROR,
            detail=f"调用日志写不出（{type(error).__name__}： {error}）。 确认 {log_path.parent} 可写。",
        )
    return outcome


def _error(detail: str, fields: dict[str, object] | None = None) -> Reply:
    return AskOutcome(status=AskStatus.ERROR, detail=detail), fields or {}


def _request(
    role: str,
    system: str,
    messages: list[tuple[str, str]],
    schema: dict | None,
    model: str | None,
    effort: str | None,
) -> Reply:
    config, detail = load_config()
    if config is None:
        return _error(detail)
    resolved, detail = resolve_role(config, role, RoleTable.PROVIDER, model, effort)
    if resolved is None:
        return _error(detail)
    name = resolved.provider or ""
    api, detail = model_api(config, name, resolved.model)
    if api is None:
        return _error(detail)
    provider = config.provider[name]
    fields: dict[str, object] = {"provider": name, "model": resolved.model, "effort": resolved.effort}
    api_key, detail = provider_key(name, provider)
    if api_key is None:
        return _error(detail, fields)
    try:
        if api is Api.MESSAGES:
            outcome, extra = _messages(
                provider.base_url, api_key, resolved.model, resolved.effort, system, messages, schema
            )
        else:
            client = openai.OpenAI(base_url=provider.base_url, api_key=api_key)
            caller = _responses if api is Api.RESPONSES else _chat
            outcome, extra = caller(client, resolved.model, resolved.effort, system, messages, schema)
    except (openai.OpenAIError, anthropic.AnthropicError) as error:
        return _error(f"请求失败（{type(error).__name__}： {error}）"[:DETAIL_EXCERPT_CHARS], fields)
    return outcome, fields | extra


def _chat(
    client: openai.OpenAI,
    model: str,
    effort: str,
    system: str,
    messages: list[tuple[str, str]],
    schema: dict | None,
) -> Reply:
    request_kwargs: dict[str, object] = {}
    if schema is not None:
        request_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema},
        }
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}] + [{"role": item[0], "content": item[1]} for item in messages],
        reasoning_effort=effort,
        **request_kwargs,
    )
    choice = response.choices[0] if response.choices else None
    finish_reason = choice.finish_reason if choice is not None else None
    usage = None if response.usage is None else response.usage.model_dump(exclude_none=True)
    extra: dict[str, object] = {"finish_reason": finish_reason, "usage": usage}
    content = choice.message.content if choice is not None else None
    if not content:
        return AskOutcome(
            status=AskStatus.ERROR,
            detail=f"响应里没有正文（choices 为空或 content 为空）， finish_reason={finish_reason or '（无）'}。",
        ), extra
    return AskOutcome(status=AskStatus.OK, text=content), extra


def _responses(
    client: openai.OpenAI,
    model: str,
    effort: str,
    system: str,
    messages: list[tuple[str, str]],
    schema: dict | None,
) -> Reply:
    if schema is not None:
        return _schema_unsupported(Api.RESPONSES, model)
    response = client.responses.create(
        model=model,
        instructions=system,
        input=[{"role": item[0], "content": item[1]} for item in messages],
        reasoning={"effort": effort},
    )
    usage = None if response.usage is None else response.usage.model_dump(exclude_none=True)
    extra: dict[str, object] = {"finish_reason": response.status, "usage": usage}
    if not response.output_text:
        return AskOutcome(
            status=AskStatus.ERROR,
            detail=f"响应里没有正文（output_text 为空）， status={response.status or '（无）'}。",
        ), extra
    return AskOutcome(status=AskStatus.OK, text=response.output_text), extra


def _messages(
    base_url: str,
    api_key: str,
    model: str,
    effort: str,
    system: str,
    messages: list[tuple[str, str]],
    schema: dict | None,
) -> Reply:
    if schema is not None:
        return _schema_unsupported(Api.MESSAGES, model)
    budget = THINKING_BUDGET_TOKENS.get(effort)
    if budget is None:
        return _error(
            f"模型 {model} 走 messages 接口， 该接口按 token 预算给推理强度， "
            f"档位只有 {'、'.join(THINKING_BUDGET_TOKENS)}， 配置里给的是 {effort}。"
        )
    client = anthropic.Anthropic(base_url=base_url.removesuffix("/v1"), api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=MESSAGES_MAX_TOKENS,
        system=system,
        messages=[{"role": item[0], "content": item[1]} for item in messages],
        thinking={"type": "enabled", "budget_tokens": budget},
        timeout=MESSAGES_TIMEOUT_SECONDS,
    )
    usage = None if response.usage is None else response.usage.model_dump(exclude_none=True)
    extra: dict[str, object] = {"finish_reason": response.stop_reason, "usage": usage}
    content = "".join(block.text for block in response.content if block.type == "text")
    if not content:
        return AskOutcome(
            status=AskStatus.ERROR,
            detail=f"响应里没有正文（content 里没有 text 块）， stop_reason={response.stop_reason or '（无）'}。",
        ), extra
    return AskOutcome(status=AskStatus.OK, text=content), extra


def _schema_unsupported(api: Api, model: str) -> Reply:
    return _error(f"模型 {model} 走 {api} 接口， 该接口的输出约束形态未实测， 不接受 schema。")
