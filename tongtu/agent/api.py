"""按服务商与端点家族发单次问答的通用实现：`ask` 原语的传输层。

一个**服务商**（`Provider`）= 一个 API 前缀 + 一份密钥 + 一张「哪个模型走哪个端点家族」的
表。表里必须逐个列全：清单里没有的模型直接报错，不按前缀猜、也没有兜底家族——端点家族是
服务端的属性，猜错的表现是服务端拒绝请求，比一次本地的明确失败难查得多。三个**端点家族**
覆盖了目前见到的全部形态，新服务商多数情况下只是加一条 `Provider` 数据，不必写代码：

- `chat`（OpenAI chat/completions）：`prompt` 作 system message，正文取
  `choices[0].message.content`，推理强度写 `reasoning_effort=`，usage 的字段名是
  `prompt_tokens` / `completion_tokens`。schema 走 `response_format` 的 json_schema。
- `responses`（OpenAI responses）：`prompt` 作 `instructions`，正文取 `output_text`，
  没有 `choices` 与 `finish_reason`，推理强度要写成 `reasoning={"effort": …}`
  （写 `reasoning_effort=` 直接 TypeError），usage 的字段名是 `input_tokens` /
  `output_tokens`。
- `messages`（Anthropic Messages）：openai SDK 打不了，本模块用 httpx 直接发。认证走
  `x-api-key` 头而非 `Authorization`（用 Bearer 会拿到 401），`max_tokens` 是必填项，
  正文在 `content` 数组里按块给出、思考过程是独立的 thinking 块，推理强度写成
  `thinking={"type": "enabled", "budget_tokens": …}`（`reasoning_effort` 在这个家族里
  被静默忽略）。

差异的实测记录见 docs/models.md。

`effort` 给出时映射为该端点家族的推理强度参数；为 None 时不传，由服务端取默认档。翻译类
调用一律取 low（medium 档实测会吞空行且输出 token 翻倍，见 docs/models.md）。

`schema`（JSON Schema 字典）只有 chat 家族支持：另两个家族的输出约束形态未实测，给出
schema 时直接报失败而不是静默发一个无约束的请求——调用方拿到不符合 schema 的正文比一次
明确的失败更难排查。

每次调用写一个 JSON 日志文件到 `log_path`（路径由调用方拼好）：请求要素、返回正文、
usage、finish_reason 与耗时。usage 是服务端返回的原样结构，其中嵌套的 cached_tokens 是本次
请求命中前缀缓存的 token 数：缓存由服务端自动维护，请求侧没有可设的参数，命中与否取决于
两次请求的前缀是否逐字节相同。失败的调用同样落日志。用量不进返回值，事后统计从日志汇总。

全部可预期失败（密钥缺失、请求失败、响应里解析不出正文、日志写不出）都以 `AskOutcome`
的 `error` 状态返回，不向调用方抛异常。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import openai

from .base import ASK_STATUS_ERROR, ASK_STATUS_OK, AskOutcome

#: 三个端点家族的标识，`Provider.families` 与 `Provider.default_family` 的取值。
FAMILY_CHAT = "chat"
FAMILY_RESPONSES = "responses"
FAMILY_MESSAGES = "messages"

#: 密钥来源标识：环境变量、配置目录里录入的密钥、服务商自己的登录态。来源进 doctor 报告
#: 与 CLI 提示，不进请求。
KEY_SOURCE_ENV = "env"
KEY_SOURCE_STORED = "stored"
KEY_SOURCE_LOGIN = "login"

#: response_format 里 json_schema 的 name 字段，协议要求非空，取值不影响输出。
SCHEMA_NAME = "ask_response"

#: 摘进 `AskOutcome.detail` 的错误描述长度上限。
DETAIL_EXCERPT_CHARS = 2000

#: messages 家族的 `max_tokens`：该家族把它列为必填项，另两个家族没有对应的必填项。取值
#: 只是上限而非预算，按最长的输入加上思考块留足即可，实测服务端接受到 64000。
MESSAGES_MAX_TOKENS = 32768

#: messages 家族的推理强度：该家族按 token 预算而非档位描述，这里把档位映射成预算。
#: 1024 是协议允许的下限。
THINKING_BUDGET_TOKENS: dict[str, int] = {"low": 1024, "medium": 4096, "high": 16384}

#: messages 家族请求的超时秒数。另两个家族沿用 openai SDK 的默认值，这一路自己发请求，
#: 故显式给一个同量级的值。
MESSAGES_TIMEOUT_SECONDS = 600.0

#: messages 家族要求的协议版本头。
ANTHROPIC_VERSION = "2023-06-01"

#: messages 家族上遇到限流或服务端错误时的重发次数与退避基数（秒，逐次翻倍）。另两个家族
#: 由 openai SDK 自带重试，这一路自己发请求，故自己退避；服务端给了 Retry-After 就按它等。
MESSAGES_RETRY_ATTEMPTS = 3
MESSAGES_RETRY_BACKOFF_SECONDS = 2.0

#: 触发重发的状态码：429 是限流，5xx 是服务端侧的临时故障。
RETRIABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

#: 一次传输的产出：结局、finish_reason 与 usage（后两者只进日志）。
Reply = tuple[AskOutcome, str | None, dict[str, object] | None]


@dataclass(frozen=True)
class ResolvedKey:
    """解析到的密钥与它的来源（`KEY_SOURCE_*` 之一）。"""

    key: str
    source: str


@dataclass(frozen=True)
class Provider:
    """一个服务商：API 前缀、密钥来源、默认模型，以及模型到端点家族的对应表。

    `families` 要把该服务商的模型逐个列全，取值是 `FAMILY_*` 之一。没有兜底家族：表里
    没有的模型在 `_request` 里直接报失败，用户看到的是「清单里没有它，先补一条」而不是
    一个来自服务端的费解错误。

    `stored_key` 与 `login_key` 是环境变量之外的两级密钥来源，各返回空串表示这一级没有。
    `key_hint` 是三处都没有时给用户的提示，各服务商的录入方式不同，故由服务商自己给。
    """

    name: str
    base_url: str
    api_key_env: str
    key_hint: str
    families: Mapping[str, str]
    stored_key: Callable[[Mapping[str, str] | None], str] | None = None
    login_key: Callable[[], str] | None = None

    def resolve_api_key(self, env: Mapping[str, str] | None = None) -> ResolvedKey | None:
        """按「越显式越优先」的顺序找密钥，三级都没有返回 None。

        顺序：环境变量（本次调用的明示，也是容器与 CI 的传入路径）→ 配置目录里录入的
        密钥 → 服务商自己的登录态。`env` 供测试注入环境变量，为 None 时读进程环境。
        """
        environ = os.environ if env is None else env
        from_env = (environ.get(self.api_key_env) or "").strip()
        if from_env:
            return ResolvedKey(key=from_env, source=KEY_SOURCE_ENV)
        stored = self.stored_key(env).strip() if self.stored_key is not None else ""
        if stored:
            return ResolvedKey(key=stored, source=KEY_SOURCE_STORED)
        login = self.login_key().strip() if self.login_key is not None else ""
        if login:
            return ResolvedKey(key=login, source=KEY_SOURCE_LOGIN)
        return None


def ask(
    provider: Provider,
    prompt: str,
    text: str,
    model: str,
    schema: dict | None,
    log_path: Path,
    effort: str | None = None,
) -> AskOutcome:
    """向 `provider` 发一次单次问答，日志写 `log_path`，返回正文或失败现场。

    端点家族按 `model` 分派；模型由调用方按用途选定，没有服务商级的默认值。`schema` 为 None
    时不设输出约束，返回普通文本。`effort` 是推理强度，为 None 时不传该参数、由服务端取默认档。
    返回的 `status` 是 `ok` 只表示拿到了非空正文，内容是否可用由调用方解析与校验。
    """
    started = time.monotonic()
    outcome, finish_reason, usage = _request(provider, prompt, text, model, schema, effort)
    record = {
        "provider": provider.name,
        "model": model,
        "prompt": prompt,
        "text": text,
        "schema": schema,
        "effort": effort,
        "status": outcome.status,
        "response": outcome.text,
        "detail": outcome.detail,
        "finish_reason": finish_reason,
        "usage": usage,
        "duration_seconds": time.monotonic() - started,
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        return AskOutcome(
            status=ASK_STATUS_ERROR,
            detail=f"调用日志写不出（{type(error).__name__}：{error}）。确认 {log_path.parent} 可写。",
        )
    return outcome


def _request(provider: Provider, prompt: str, text: str, model: str, schema: dict | None, effort: str | None) -> Reply:
    """解析密钥、按端点家族发请求并归一错误；各家族的请求形态在 `_chat` / `_responses` / `_messages`。"""
    resolved = provider.resolve_api_key()
    if resolved is None:
        return AskOutcome(status=ASK_STATUS_ERROR, detail=provider.key_hint), None, None

    family = provider.families.get(model)
    if family is None:
        return (
            AskOutcome(
                status=ASK_STATUS_ERROR,
                detail=(
                    f"服务商 {provider.name} 的模型清单里没有 {model}，不知道它走哪个端点家族。"
                    f"确认模型标识没写错；确实有这个模型就往该服务商的 families 表里补一条，"
                    f"取值是 {FAMILY_CHAT} / {FAMILY_RESPONSES} / {FAMILY_MESSAGES} 之一。"
                ),
            ),
            None,
            None,
        )
    try:
        if family == FAMILY_MESSAGES:
            return _messages(provider.base_url, resolved.key, prompt, text, model, schema, effort)
        client = openai.OpenAI(base_url=provider.base_url, api_key=resolved.key)
        caller = _responses if family == FAMILY_RESPONSES else _chat
        return caller(client, prompt, text, model, schema, effort)
    except (openai.OpenAIError, httpx.HTTPError) as error:
        detail = f"请求失败（{type(error).__name__}：{error}）"[:DETAIL_EXCERPT_CHARS]
        return AskOutcome(status=ASK_STATUS_ERROR, detail=detail), None, None


def _no_content(detail: str, finish_reason: str | None, usage: dict[str, object] | None) -> Reply:
    """响应解析不出正文时的统一结局。"""
    return AskOutcome(status=ASK_STATUS_ERROR, detail=detail), finish_reason, usage


def _schema_unsupported(family: str, model: str) -> Reply:
    """chat 之外的家族不接受 schema 时的统一结局。"""
    return _no_content(f"模型 {model} 走 {family} 端点，该端点的输出约束形态未实测，不接受 schema。", None, None)


def _chat(client: openai.OpenAI, prompt: str, text: str, model: str, schema: dict | None, effort: str | None) -> Reply:
    """chat/completions：`prompt` 作 system message，`text` 作 user message。"""
    request_kwargs: dict[str, object] = {}
    if schema is not None:
        request_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema},
        }
    if effort is not None:
        request_kwargs["reasoning_effort"] = effort
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": text}],
        **request_kwargs,
    )
    choice = response.choices[0] if response.choices else None
    finish_reason = choice.finish_reason if choice is not None else None
    # usage 整份落日志而不摘字段：缓存命中量在嵌套的 details 里，且各家族的字段名不同，
    # 摘字段就得跟着家族分三套。exclude_none 滤掉该模型不适用的项（audio_tokens 等）。
    usage = None if response.usage is None else response.usage.model_dump(exclude_none=True)
    content = choice.message.content if choice is not None else None
    if not content:
        return _no_content(
            f"响应里没有正文（choices 为空或 content 为空），finish_reason={finish_reason or '（无）'}。",
            finish_reason,
            usage,
        )
    return AskOutcome(status=ASK_STATUS_OK, text=content), finish_reason, usage


def _responses(
    client: openai.OpenAI, prompt: str, text: str, model: str, schema: dict | None, effort: str | None
) -> Reply:
    """responses：`prompt` 作 instructions，`text` 作 input，推理强度写在 reasoning 里。"""
    if schema is not None:
        return _schema_unsupported(FAMILY_RESPONSES, model)
    request_kwargs: dict[str, object] = {}
    if effort is not None:
        request_kwargs["reasoning"] = {"effort": effort}
    response = client.responses.create(model=model, instructions=prompt, input=text, **request_kwargs)
    usage = None if response.usage is None else response.usage.model_dump(exclude_none=True)
    if not response.output_text:
        return _no_content(
            f"响应里没有正文（output_text 为空），status={response.status or '（无）'}。", response.status, usage
        )
    return AskOutcome(status=ASK_STATUS_OK, text=response.output_text), response.status, usage


def _messages(
    base_url: str, api_key: str, prompt: str, text: str, model: str, schema: dict | None, effort: str | None
) -> Reply:
    """messages：`prompt` 作 system，`text` 作单条 user message，正文从 content 的 text 块里取。"""
    if schema is not None:
        return _schema_unsupported(FAMILY_MESSAGES, model)
    body: dict[str, object] = {
        "model": model,
        "max_tokens": MESSAGES_MAX_TOKENS,
        "system": prompt,
        "messages": [{"role": "user", "content": text}],
    }
    budget = None if effort is None else THINKING_BUDGET_TOKENS.get(effort)
    if budget is not None:
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    response = _post_with_retry(
        f"{base_url}/messages", body, {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as error:
        return _no_content(f"响应体不是 JSON（{error}）：{response.text[:DETAIL_EXCERPT_CHARS]}", None, None)
    if not isinstance(payload, dict):
        return _no_content(f"响应体不是 JSON 对象：{response.text[:DETAIL_EXCERPT_CHARS]}", None, None)
    stop_reason = payload.get("stop_reason")
    usage = payload.get("usage")
    blocks = payload.get("content") or []
    content = "".join(
        block.get("text") or "" for block in blocks if isinstance(block, dict) and block.get("type") == "text"
    )
    if not content:
        return _no_content(
            f"响应里没有正文（content 里没有 text 块），stop_reason={stop_reason or '（无）'}。", stop_reason, usage
        )
    return AskOutcome(status=ASK_STATUS_OK, text=content), stop_reason, usage


def _post_with_retry(url: str, body: dict[str, object], headers: dict[str, str]) -> httpx.Response:
    """发 POST，遇到限流或服务端临时故障时退避重发，返回最后一次的响应（不判状态码）。

    重发只针对 `RETRIABLE_STATUS_CODES`：其余状态码（含 4xx 的请求错误）重发也是同样结果，
    交给调用方的 `raise_for_status` 报出去。退避时长取 Retry-After 头与逐次翻倍的退避基数
    里的较大者。
    """
    # ponytail: 429 一律当瞬时限流退避。订阅额度耗尽（OpenCode 的 GoUsageLimitError）也是
    # 429，退避再多次也不会通过，白等三轮；要区分就得按响应体里的错误类型分流，等哪次
    # 真的被它拖住了再做。
    response = httpx.post(url, json=body, headers=headers, timeout=MESSAGES_TIMEOUT_SECONDS)
    for attempt in range(1, MESSAGES_RETRY_ATTEMPTS):
        if response.status_code not in RETRIABLE_STATUS_CODES:
            return response
        backoff = MESSAGES_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
        time.sleep(max(backoff, _retry_after_seconds(response)))
        response = httpx.post(url, json=body, headers=headers, timeout=MESSAGES_TIMEOUT_SECONDS)
    return response


def _retry_after_seconds(response: httpx.Response) -> float:
    """Retry-After 头里的秒数；头缺失或不是个数（HTTP 日期形态）时返回 0。"""
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return 0.0
