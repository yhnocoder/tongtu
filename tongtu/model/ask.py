from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

import anthropic
import openai

from .config import Api, ModelsConfig, Target, load_config, model_api, provider_key, role_target

SCHEMA_NAME = "ask_response"

SCHEMA_INSTRUCTION = "\n\n只输出符合以下 JSON Schema 的 JSON：\n{schema}"

RESPONSE_FORMAT_FIELD = "response_format"

JSON_OBJECT_FORMAT: dict[str, object] = {"type": "json_object"}

FALLBACK_FIELD = "json_object_fallback"

DETAIL_EXCERPT_CHARS = 2000

MESSAGES_MAX_TOKENS = 32768

THINKING_BUDGET_TOKENS: dict[str, int] = {"low": 1024, "medium": 2048, "high": 4096}

ASK_TIMEOUT_SECONDS = 300


class AskStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True)
class AskOutcome:
    status: AskStatus
    text: str = ""
    detail: str = ""
    model: str = ""


Reply = tuple[AskOutcome, dict[str, object]]


def ask(
    role: str,
    system: str,
    messages: list[tuple[str, str]],
    *,
    schema: dict | None = None,
    log_path: Path,
    effort: str | None = None,
) -> AskOutcome:
    started = time.monotonic()
    outcome, fields = _request(role, system, messages, schema, effort)
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
            detail=f"cannot write the call log ({type(error).__name__}: {error}). Check that {log_path.parent} is writable.",
            model=outcome.model,
        )
    return outcome


def _error(detail: str, fields: dict[str, object] | None = None) -> Reply:
    return AskOutcome(status=AskStatus.ERROR, detail=detail), fields or {}


def _request(
    role: str,
    system: str,
    messages: list[tuple[str, str]],
    schema: dict | None,
    effort: str | None,
) -> Reply:
    config, detail = load_config()
    if config is None:
        return _error(detail)
    target, detail = role_target(config, role, effort)
    if target is None:
        return _error(detail)
    if target.is_runtime:
        return _error(f"role {role} points at runtime {target.backend}; ask needs a [provider.*] name.")
    outcome, fields = _resolved_request(config, target, system, messages, schema)
    return replace(outcome, model=str(target)), fields


def _resolved_request(
    config: ModelsConfig,
    target: Target,
    system: str,
    messages: list[tuple[str, str]],
    schema: dict | None,
) -> Reply:
    api, detail = model_api(config, target.backend, target.model)
    if api is None:
        return _error(detail)
    provider = config.provider[target.backend]
    fields: dict[str, object] = {"provider": target.backend, "model": target.model, "effort": target.effort}
    api_key, detail = provider_key(target.backend, provider)
    if api_key is None:
        return _error(detail, fields)
    try:
        if api is Api.MESSAGES:
            outcome, extra = _messages(
                provider.base_url, api_key, target.model, target.effort, system, messages, schema
            )
        else:
            client = openai.OpenAI(
                base_url=f"{provider.base_url}/v1", api_key=api_key, timeout=ASK_TIMEOUT_SECONDS, max_retries=1
            )
            caller = _responses if api is Api.RESPONSES else _chat
            outcome, extra = caller(client, target.model, target.effort, system, messages, schema)
    except (openai.OpenAIError, anthropic.AnthropicError) as error:
        return _error(f"request failed ({type(error).__name__}: {error})"[:DETAIL_EXCERPT_CHARS], fields)
    return outcome, fields | extra


def _chat(
    client: openai.OpenAI,
    model: str,
    effort: str | None,
    system: str,
    messages: list[tuple[str, str]],
    schema: dict | None,
) -> Reply:
    if schema is None:
        return _completion(client, model, effort, system, messages, {})
    strict: dict[str, object] = {
        RESPONSE_FORMAT_FIELD: {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema},
        }
    }
    try:
        return _completion(client, model, effort, system, messages, strict)
    except openai.BadRequestError as error:
        if RESPONSE_FORMAT_FIELD not in str(error):
            raise
    instructed = system + SCHEMA_INSTRUCTION.format(schema=json.dumps(schema, ensure_ascii=False))
    outcome, extra = _completion(
        client, model, effort, instructed, messages, {RESPONSE_FORMAT_FIELD: JSON_OBJECT_FORMAT}
    )
    return outcome, extra | {FALLBACK_FIELD: True}


def _completion(
    client: openai.OpenAI,
    model: str,
    effort: str | None,
    system: str,
    messages: list[tuple[str, str]],
    request_kwargs: dict[str, object],
) -> Reply:
    if effort is not None:
        request_kwargs = request_kwargs | {"reasoning_effort": effort}
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}] + [{"role": item[0], "content": item[1]} for item in messages],
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
            detail=f"response has no body (choices or content is empty), finish_reason={finish_reason or '(none)'}.",
        ), extra
    return AskOutcome(status=AskStatus.OK, text=content), extra


def _responses(
    client: openai.OpenAI,
    model: str,
    effort: str | None,
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
        **({} if effort is None else {"reasoning": {"effort": effort}}),
    )
    usage = None if response.usage is None else response.usage.model_dump(exclude_none=True)
    extra: dict[str, object] = {"finish_reason": response.status, "usage": usage}
    if not response.output_text:
        return AskOutcome(
            status=AskStatus.ERROR,
            detail=f"response has no body (output_text is empty), status={response.status or '(none)'}.",
        ), extra
    return AskOutcome(status=AskStatus.OK, text=response.output_text), extra


def _messages(
    base_url: str,
    api_key: str,
    model: str,
    effort: str | None,
    system: str,
    messages: list[tuple[str, str]],
    schema: dict | None,
) -> Reply:
    if schema is not None:
        return _schema_unsupported(Api.MESSAGES, model)
    budget = None if effort is None else THINKING_BUDGET_TOKENS.get(effort)
    if effort is not None and budget is None:
        return _error(
            f"model {model} uses the messages API, which maps reasoning effort to a token budget; "
            f"the only levels are {', '.join(THINKING_BUDGET_TOKENS)}, but the config gives {effort}."
        )
    client = anthropic.Anthropic(base_url=base_url, api_key=api_key, timeout=ASK_TIMEOUT_SECONDS, max_retries=1)
    response = client.messages.create(
        model=model,
        max_tokens=MESSAGES_MAX_TOKENS,
        system=system,
        messages=[{"role": item[0], "content": item[1]} for item in messages],
        **({} if budget is None else {"thinking": {"type": "enabled", "budget_tokens": budget}}),
    )
    usage = None if response.usage is None else response.usage.model_dump(exclude_none=True)
    extra: dict[str, object] = {"finish_reason": response.stop_reason, "usage": usage}
    content = "".join(block.text for block in response.content if block.type == "text")
    if not content:
        return AskOutcome(
            status=AskStatus.ERROR,
            detail=f"response has no body (no text block in content), stop_reason={response.stop_reason or '(none)'}.",
        ), extra
    return AskOutcome(status=AskStatus.OK, text=content), extra


def _schema_unsupported(api: Api, model: str) -> Reply:
    return _error(
        f"model {model} uses the {api} API, whose structured output support is untested; schema is not accepted."
    )
