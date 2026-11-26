from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from .config import AbstractConfig

ABSTRACT_TIMEOUT_SECONDS = 60

ABSTRACT_TEMPERATURE = 0.2

ABSTRACT_PROMPT = (
    "你会收到一篇论文的英文摘要。把它译成简体中文。\n"
    "要求：\n"
    "1. 按中文的习惯组织句子，不照搬英文语序。\n"
    "2. 论文新提出的方法名、模型名、数据集名、指标名，通用的英文算法名、术语缩写，"
    "以及其他用英文更容易理解的专有名词，保留英文原文，不加括号注释。\n"
    "3. 公式、符号、数字原样保留。\n"
    "4. 只输出译文，不加说明、标题或引号。"
)


class ChatError(Exception):
    pass


class _Message(BaseModel):
    content: str | None = None


class _Choice(BaseModel):
    message: _Message


class _Completion(BaseModel):
    choices: list[_Choice]


def _sendable_in_header(value: str) -> bool:
    return value.isascii() and all(char.isprintable() and not char.isspace() for char in value)


def _host(url: str) -> str:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    return host or "abstract.base_url"


def translate_abstract(config: AbstractConfig, text: str) -> str:
    if not config.api_key:
        raise ChatError("abstract.api_key is not set; set it with tongtu config set abstract.api_key KEY")
    if not _sendable_in_header(config.api_key):
        raise ChatError("abstract.api_key has characters that cannot be sent in a request header")
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    host = _host(url)
    body = {
        "model": config.model,
        "messages": [{"role": "system", "content": ABSTRACT_PROMPT}, {"role": "user", "content": text}],
        "temperature": ABSTRACT_TEMPERATURE,
        "thinking": {"type": "disabled"},
    }
    try:
        response = httpx.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=ABSTRACT_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        raise ChatError(f"request to {host} failed: {type(error).__name__}") from error
    if not response.is_success:
        raise ChatError(f"{host} answered HTTP {response.status_code}")
    try:
        completion = _Completion.model_validate_json(response.content)
    except ValidationError as error:
        raise ChatError(f"the answer from {host} is not a chat completion") from error
    reply = (completion.choices[0].message.content or "").strip() if completion.choices else ""
    if not reply:
        raise ChatError(f"the answer from {host} has no text")
    return reply
