"""OpenCode Go 这个服务商的定义：API 前缀、模型到端点家族的对应表、三级密钥来源。

传输、日志与错误归一都在 `api` 模块，本模块只提供数据与 OpenCode 独有的那一级密钥来源
（本机 opencode 登录态）。换服务商就是再写一份这样的 `Provider`，不改传输层。

密钥按「越显式越优先」的顺序解析：环境变量 `OPENCODE_API_KEY` → 配置目录
credentials.json 里录入的密钥 → 本机 opencode 登录凭证里 Go 订阅条目的密钥。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .. import config
from .api import (
    FAMILY_CHAT,
    FAMILY_MESSAGES,
    FAMILY_RESPONSES,
    KEY_SOURCE_ENV,
    KEY_SOURCE_LOGIN,
    KEY_SOURCE_STORED,
    Provider,
    ResolvedKey,
)
from .api import ask as _ask
from .base import ASK_STATUS_ERROR, ASK_STATUS_OK, AskOutcome

#: 调用方按服务商模块取这些名字，不必再认 `api` 与 `base` 的分工。
__all__ = [
    "API_KEY_ENV",
    "ASK_STATUS_ERROR",
    "ASK_STATUS_OK",
    "BASE_URL",
    "KEY_SOURCE_ENV",
    "KEY_SOURCE_LOGIN",
    "KEY_SOURCE_STORED",
    "MODEL_FAMILIES",
    "OPENCODE_AUTH_PATH",
    "PROVIDER",
    "AskOutcome",
    "ResolvedKey",
    "ask",
    "resolve_api_key",
]

#: 密钥的环境变量名。OpenCode 官方没有为密钥定环境变量名，这个名字是通途自己的约定。
API_KEY_ENV = "OPENCODE_API_KEY"

#: OpenCode Go 的 API 前缀。模型清单可从其下的 /models 查询；三个端点家族都挂在这个前缀下。
BASE_URL = "https://opencode.ai/zen/go/v1"

#: 本机 opencode 的登录凭证文件（其 `/connect` 的产物）与其中 Go 订阅的条目名。这是
#: opencode 的内部存储而非文档化契约，形状变了这一路就解析不到，由前两级来源顶上。
OPENCODE_AUTH_PATH = Path("~/.local/share/opencode/auth.json")
OPENCODE_AUTH_PROVIDER = "opencode-go"

# See (https://opencode.ai/docs/zh-cn/go/)。表里没有的模型直接报失败。

MODEL_FAMILIES: dict[str, str] = {
    "deepseek-v4-pro": FAMILY_CHAT,
    "deepseek-v4-flash": FAMILY_CHAT,
    "glm-5.3": FAMILY_CHAT,
    "glm-5.2": FAMILY_CHAT,
    "glm-5.1": FAMILY_CHAT,
    "kimi-k3": FAMILY_CHAT,
    "kimi-k2.7-code": FAMILY_CHAT,
    "kimi-k2.6": FAMILY_CHAT,
    "mimo-v2.5": FAMILY_CHAT,
    "mimo-v2.5-pro": FAMILY_CHAT,
    "hy3": FAMILY_CHAT,
    "grok-4.5": FAMILY_RESPONSES,
    "gpt-5.6-luna": FAMILY_RESPONSES,
    "muse-spark-1.2-contributor": FAMILY_RESPONSES,
    "minimax-m3": FAMILY_MESSAGES,
    "minimax-m2.7": FAMILY_MESSAGES,
    "minimax-m2.5": FAMILY_MESSAGES,
    "qwen3.8-max": FAMILY_MESSAGES,
    "qwen3.7-max": FAMILY_MESSAGES,
    "qwen3.7-plus": FAMILY_MESSAGES,
    "qwen3.6-plus": FAMILY_MESSAGES,
}


def _login_key() -> str:
    """读本机 opencode 登录凭证里 Go 订阅条目的密钥；文件缺失、不可解析或形状不符都返回空串。"""
    try:
        data = json.loads(OPENCODE_AUTH_PATH.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    entry = data.get(OPENCODE_AUTH_PROVIDER) if isinstance(data, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    return key.strip() if isinstance(key, str) else ""


PROVIDER = Provider(
    name="opencode",
    base_url=BASE_URL,
    api_key_env=API_KEY_ENV,
    key_hint=(
        f"没有可用的 OpenCode 密钥。查找了三处：环境变量 {API_KEY_ENV}、"
        f"{config.credentials_path()}、{OPENCODE_AUTH_PATH}（opencode 里 /connect 登录 "
        f"Go 订阅后产生）。任设其一即可，密钥在 OpenCode 的 Zen 控制台创建。"
    ),
    families=MODEL_FAMILIES,
    stored_key=lambda env: config.load_credentials(env).opencode_api_key,
    login_key=_login_key,
)


def resolve_api_key(env: Mapping[str, str] | None = None) -> ResolvedKey | None:
    """OpenCode 的密钥解析，三处都没有返回 None。`env` 供测试注入环境变量。"""
    return PROVIDER.resolve_api_key(env)


def ask(
    prompt: str,
    text: str,
    model: str,
    schema: dict | None,
    log_path: Path,
    effort: str | None = None,
) -> AskOutcome:
    """向 OpenCode Go 发一次单次问答；参数与返回值的含义见 `api.ask`。"""
    return _ask(PROVIDER, prompt, text, model, schema, log_path, effort)
