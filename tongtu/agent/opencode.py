"""OpenCode Go 的 `ask` 适配器：单次问答走 API 直调。

请求经 openai SDK 打向 OpenCode Go 的 OpenAI 兼容端点（chat/completions），超时与重试
沿用 SDK 默认值。`prompt` 作 system message，`text` 作 user message，一次请求一次响应：
不提供工具、不保留状态，返回正文只由入参决定（模型自身的随机性除外）。

`schema`（JSON Schema 字典）给出时映射为请求的 response_format（json_schema、strict）：
输出约束由服务端执行，返回正文即符合该 schema 的 JSON 字符串。响应里模型的思考过程在
独立字段，不混入正文，本模块只取正文。是否采信正文由调用方自己校验，本模块不解析内容。

每次调用写一个 JSON 日志文件到 `log_path`（路径由调用方拼好）：请求要素、返回正文、
usage、finish_reason 与耗时。usage 是服务端返回的原样结构，其中
prompt_tokens_details.cached_tokens 是本次请求命中前缀缓存的 token 数：缓存由服务端自动维护，
请求侧没有可设的参数，命中与否取决于两次请求的前缀是否逐字节相同。失败的调用同样落日志。
用量不进返回值，事后统计从日志汇总。

密钥按「越显式越优先」的顺序解析（`resolve_api_key`）：环境变量 `OPENCODE_API_KEY` →
配置目录 credentials.json 里录入的密钥 → 本机 opencode 登录凭证里 Go 订阅条目的密钥。
三处都没有直接返回 `error`，不发请求。全部可预期失败（密钥缺失、请求失败、响应里解析
不出正文、日志写不出）都以 `AskOutcome` 的 `error` 状态返回，不向调用方抛异常。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import openai

from .. import config
from .base import ASK_STATUS_ERROR, ASK_STATUS_OK, AskOutcome

#: OpenCode Go 的 API 前缀。模型清单可从其下的 /models 查询；本模块只用 chat/completions
#: 一个端点，走不同端点家族的模型（如 Qwen、MiniMax）当前不可用。
BASE_URL = "https://opencode.ai/zen/go/v1"

#: `model` 参数为 None 时使用的模型标识（请求体里就用这样的裸标识，无供应商前缀）。
DEFAULT_MODEL = "deepseek-v4-flash"

#: 密钥的环境变量名。OpenCode 官方没有为密钥定环境变量名，这个名字是通途自己的约定。
API_KEY_ENV = "OPENCODE_API_KEY"

#: 密钥来源标识：环境变量、配置目录里录入的密钥、本机 opencode 登录态。来源进 doctor
#: 报告与 CLI 提示，不进请求。
KEY_SOURCE_ENV = "env"
KEY_SOURCE_STORED = "stored"
KEY_SOURCE_OPENCODE_LOGIN = "opencode_login"

#: 本机 opencode 的登录凭证文件（其 `/connect` 的产物）与其中 Go 订阅的条目名。这是
#: opencode 的内部存储而非文档化契约，形状变了这一路就解析不到，由前两级来源顶上。
OPENCODE_AUTH_PATH = Path("~/.local/share/opencode/auth.json")
OPENCODE_AUTH_PROVIDER = "opencode-go"

#: response_format 里 json_schema 的 name 字段，协议要求非空，取值不影响输出。
SCHEMA_NAME = "ask_response"

#: 摘进 `AskOutcome.detail` 的错误描述长度上限。
DETAIL_EXCERPT_CHARS = 2000


@dataclass(frozen=True)
class ResolvedKey:
    """解析到的密钥与它的来源（`KEY_SOURCE_*` 之一）。"""

    key: str
    source: str


def resolve_api_key(env: Mapping[str, str] | None = None) -> ResolvedKey | None:
    """按「越显式越优先」的顺序找密钥，三处都没有返回 None。

    顺序：环境变量 `OPENCODE_API_KEY`（本次调用的明示，也是容器与 CI 的传入路径）→
    配置目录 credentials.json 里录入的密钥 → 本机 opencode 登录凭证里 Go 订阅条目的
    密钥。`env` 供测试注入环境变量，为 None 时读进程环境。
    """
    environ = os.environ if env is None else env
    from_env = (environ.get(API_KEY_ENV) or "").strip()
    if from_env:
        return ResolvedKey(key=from_env, source=KEY_SOURCE_ENV)
    stored = config.load_credentials(env).opencode_api_key.strip()
    if stored:
        return ResolvedKey(key=stored, source=KEY_SOURCE_STORED)
    login = _login_key()
    if login:
        return ResolvedKey(key=login, source=KEY_SOURCE_OPENCODE_LOGIN)
    return None


def _login_key() -> str:
    """读本机 opencode 登录凭证里 Go 订阅条目的密钥；文件缺失、不可解析或形状不符都返回空串。"""
    try:
        data = json.loads(OPENCODE_AUTH_PATH.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    entry = data.get(OPENCODE_AUTH_PROVIDER) if isinstance(data, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    return key.strip() if isinstance(key, str) else ""


def ask(
    prompt: str,
    text: str,
    model: str | None,
    schema: dict | None,
    log_path: Path,
) -> AskOutcome:
    """向 OpenCode Go 发一次单次问答，日志写 `log_path`，返回正文或失败现场。

    `model` 为 None 时用 `DEFAULT_MODEL`。`schema` 为 None 时不设输出约束，返回普通文本。
    返回的 `status` 是 `ok` 只表示拿到了非空正文，内容是否可用由调用方解析与校验。
    """
    resolved_model = model or DEFAULT_MODEL
    started = time.monotonic()
    outcome, finish_reason, usage = _request(prompt, text, resolved_model, schema)
    record = {
        "model": resolved_model,
        "prompt": prompt,
        "text": text,
        "schema": schema,
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


def _request(
    prompt: str, text: str, model: str, schema: dict | None
) -> tuple[AskOutcome, str | None, dict[str, object] | None]:
    """发出请求并从响应里取正文；返回结局、finish_reason 与 usage（后两者只进日志）。"""
    resolved = resolve_api_key()
    if resolved is None:
        return (
            AskOutcome(
                status=ASK_STATUS_ERROR,
                detail=(
                    f"没有可用的 OpenCode 密钥。查找了三处：环境变量 {API_KEY_ENV}、"
                    f"{config.credentials_path()}、{OPENCODE_AUTH_PATH}（opencode 里 /connect 登录 "
                    f"Go 订阅后产生）。任设其一即可，密钥在 OpenCode 的 Zen 控制台创建。"
                ),
            ),
            None,
            None,
        )

    request_kwargs: dict[str, object] = {}
    if schema is not None:
        request_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema},
        }
    try:
        client = openai.OpenAI(base_url=BASE_URL, api_key=resolved.key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            **request_kwargs,
        )
    except openai.OpenAIError as error:
        detail = f"请求失败（{type(error).__name__}：{error}）"[:DETAIL_EXCERPT_CHARS]
        return AskOutcome(status=ASK_STATUS_ERROR, detail=detail), None, None

    choice = response.choices[0] if response.choices else None
    finish_reason = choice.finish_reason if choice is not None else None
    # usage 整份落日志而不摘字段：缓存命中量在嵌套的 prompt_tokens_details.cached_tokens 里，
    # 且 responses 端点家族的字段名与此处不同（input_tokens_details.cached_tokens），
    # 摘字段就得跟着端点分两套。exclude_none 滤掉该模型不适用的项（audio_tokens 等）。
    usage = None if response.usage is None else response.usage.model_dump(exclude_none=True)
    content = choice.message.content if choice is not None else None
    if not content:
        return (
            AskOutcome(
                status=ASK_STATUS_ERROR,
                detail=f"响应里没有正文（choices 为空或 content 为空），finish_reason={finish_reason or '（无）'}。",
            ),
            finish_reason,
            usage,
        )
    return AskOutcome(status=ASK_STATUS_OK, text=content), finish_reason, usage
