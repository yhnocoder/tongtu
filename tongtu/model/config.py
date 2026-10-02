from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..config import config_path

RUNTIME_NAMES: tuple[str, ...] = ("codex", "claude-code", "pi")


class Api(StrEnum):
    CHAT = "chat"
    RESPONSES = "responses"
    MESSAGES = "messages"


class ProviderConfig(BaseModel):
    base_url: str
    api_key: str | None = None
    api_key_env: str | None = None
    api: str | None = None
    models: dict[str, str] = {}


def _validated_font_path(value: str | None) -> str | None:
    path = Path(value).expanduser() if value else None
    if path is not None and len(path.parts) > 1 and not path.is_absolute():
        raise ValueError(f"font path {value} must be absolute or start with ~")
    return value


class FontFamily(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regular: str
    bold: str | None = None

    @field_validator("regular", "bold")
    @classmethod
    def absolute_font_path(cls, value: str | None) -> str | None:
        return _validated_font_path(value)


def _default_main() -> FontFamily:
    return FontFamily(regular="LXGWWenKai-Light.ttf", bold="LXGWWenKai-Medium.ttf")


def _default_sans() -> FontFamily:
    return FontFamily(regular="SourceHanSansSC-Regular.otf", bold="SourceHanSansSC-Bold.otf")


class FontsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    main: str | FontFamily = Field(default_factory=_default_main)
    sans: str | FontFamily = Field(default_factory=_default_sans)
    mono: str | None = None

    @field_validator("main", "sans", "mono")
    @classmethod
    def absolute_font_path(cls, value: str | FontFamily | None) -> str | FontFamily | None:
        if isinstance(value, str):
            return _validated_font_path(value)
        return value


class ChunkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_tokens: int = Field(default=5000, gt=0, strict=True)
    chunk_merge_tokens: int = Field(default=1500, ge=0, strict=True)

    @model_validator(mode="after")
    def valid_chunk_limits(self) -> ChunkingConfig:
        if self.chunk_merge_tokens > self.chunk_tokens:
            raise ValueError("chunk_merge_tokens must not exceed chunk_tokens")
        return self


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    effort: str | None = None
    timeout_seconds: float | None = None
    max_turns: int | None = None


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: dict[str, ProviderConfig] = {}
    chunking: ChunkingConfig = ChunkingConfig()
    fonts: FontsConfig = FontsConfig()
    roles: dict[str, RoleConfig] = {}

    @model_validator(mode="after")
    def provider_names_are_not_runtime_names(self) -> ModelsConfig:
        clash = set(self.provider) & set(RUNTIME_NAMES)
        if clash:
            raise ValueError(
                f"provider names {', '.join(sorted(clash))} are reserved for runtimes; rename them under [provider.*]"
            )
        return self


@dataclass(frozen=True)
class Target:
    backend: str
    model: str
    effort: str | None

    @property
    def is_runtime(self) -> bool:
        return self.backend in RUNTIME_NAMES

    def __str__(self) -> str:
        return f"{self.backend}/{self.model}" if self.model else self.backend


def load_config() -> tuple[ModelsConfig | None, str]:
    path = config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        return (
            None,
            f"cannot read config file {path} ({type(error).__name__}: {error}). Run tongtu setup to write the template first.",
        )
    except tomllib.TOMLDecodeError as error:
        return None, f"config file {path} is not valid TOML ({error})."
    try:
        config = ModelsConfig.model_validate(data)
    except ValidationError as error:
        return None, f"config file {path} has invalid fields ({error})."
    return config, ""


def parse_target(text: str) -> tuple[tuple[str, str] | None, str]:
    text = text.strip()
    if not text:
        return None, "a model must be written as backend/model"
    backend, _separator, model = text.partition("/")
    if backend and (model or backend in RUNTIME_NAMES):
        return (backend, model), ""
    return None, (
        "a model must be written as backend/model, where backend is codex, claude-code, pi or a [provider.*] name; "
        f"a provider always needs a model; got {text}"
    )


def role_target(config: ModelsConfig, role: str, effort: str | None = None) -> tuple[Target | None, str]:
    entry = config.roles.get(role)
    if entry is None:
        return None, f"config file {config_path()} has no role {role} under [roles]; add one."
    parsed, detail = parse_target(entry.model)
    if parsed is None:
        return None, detail
    backend, model = parsed
    if backend not in RUNTIME_NAMES and backend not in config.provider:
        return None, (
            f"config file {config_path()} does not declare provider {backend}; "
            f"add it under [provider.{backend}], or use codex, claude-code or pi."
        )
    return Target(backend, model, effort or entry.effort), ""


def provider_key(name: str, provider: ProviderConfig) -> tuple[str | None, str]:
    written = (provider.api_key or "").strip()
    if written:
        return written, "api_key in config.toml"
    variable = (provider.api_key_env or "").strip()
    if variable:
        value = (os.environ.get(variable) or "").strip()
        if value:
            return value, f"environment variable {variable}"
    if variable:
        return None, (
            f"no key for provider {name}. Write api_key under [provider.{name}] in {config_path()}, "
            f"or set the environment variable {variable}."
        )
    return None, (
        f"no key for provider {name}. Write api_key under [provider.{name}] in {config_path()}, "
        f"or write api_key_env naming the environment variable that holds the key."
    )


def model_api(config: ModelsConfig, provider: str, model: str) -> tuple[Api | None, str]:
    entry = config.provider.get(provider)
    if entry is None:
        return (
            None,
            f"config file {config_path()} does not declare provider {provider}; add it under [provider.{provider}].",
        )
    api = entry.models.get(model) or entry.api
    if api is None:
        return None, (
            f"provider {provider} has no entry for model {model} in its models table and no api field, "
            f"so its API kind is unknown. Add a models entry or a provider-wide api in {config_path()}."
        )
    if api not in tuple(Api):
        return (
            None,
            f"provider {provider} gives model {model} the API {api}; only chat / responses / messages are accepted.",
        )
    return Api(api), ""


CONFIG_TEMPLATE = """\
# 服务商：ask 用。base_url 是接口前缀的根（chat / responses / messages 都在它下面的 /v1/...）
# api 取 chat / responses / messages，是整个服务商的默认；混合网关在 [provider.X.models] 逐模型覆盖
# 密钥直接写 api_key，或留空改用 api_key_env 指定的环境变量
[provider.deepseek]
base_url    = "https://api.deepseek.com"
api_key     = ""
api_key_env = "DEEPSEEK_API_KEY"
api         = "chat"

# 其他服务商照上面的格式各写一段，例如：
# [provider.opencode]
# base_url    = "https://opencode.ai/zen/go"
# api_key_env = "OPENCODE_API_KEY"
# [provider.opencode.models]
# "deepseek-v4-flash" = "chat"
# "gpt-5.6-luna"      = "responses"
# "qwen3.8-max"       = "messages"

# survey 的分块：chunk_tokens 是结构化分块的上限（o200k_base token），chunk_merge_tokens 是小块合并阈值；改后从 survey 重跑
[chunking]
chunk_tokens       = 20000
chunk_merge_tokens = 12000

# 中文字体：main 正文、sans 无衬线、mono 等宽（不写则用 main 的常规字体）；改这里即可切换译文 PDF 的字体
# main 与 sans 写一族字体：只写一个文件表示这一族只有常规字形，粗体由 xeCJK 伪粗体生成；写成 { regular = …, bold = … } 同时给出粗体
# 文件写仓库 fonts/ 里的文件名，或绝对路径、以 ~ 开头的路径；编译时由 TTFONTS / OPENTYPEFONTS 把这些目录交给 XeTeX
# 不接受系统字体名：zh.tex 只写文件名，论文目录才能在不同机器之间搬动
# 一族里有文件找不到时，这一族整体改用自带的字体，并在 precompile 的 manifest 里给出警告
[fonts]
main = { regular = "LXGWWenKai-Light.ttf", bold = "LXGWWenKai-Medium.ttf" }
sans = { regular = "SourceHanSansSC-Regular.otf", bold = "SourceHanSansSC-Bold.otf" }

# 角色：model = "后端/模型"，只在第一个 / 处切分。后端是 codex / claude-code / pi 时启动该 CLI 的会话，否则是上面 [provider.*] 的名字，直接调 API
# effort 不写就不传，写了原样交给后端，取值范围由后端决定；timeout_seconds 对 codex / claude-code / pi 必填；max_turns 只有 claude-code 用
# 只写后端名（codex、claude-code、pi）则不传模型参数，用运行时自己的默认模型；pi 的模型写 pi/服务商/模型（pi 自己的服务商名）；服务商必须写全 服务商/模型
# pi 的模型名以 pi --list-models 列出的为准：pi 的 --model 会模糊匹配，写错时可能匹配到别的服务商的同名模型，报错会指向那个服务商缺少密钥
# 没有命令行选项覆盖这里的模型：换模型就改这个文件
[roles]
survey_terms   = { model = "deepseek/deepseek-flash", effort = "low" }
translate      = { model = "codex/gpt-6-astra", effort = "low", timeout_seconds = 1800 }
review         = { model = "codex/gpt-6-astra", effort = "low", timeout_seconds = 3600 }
precompile_fix = { model = "codex/gpt-6-astra", effort = "low", timeout_seconds = 1800 }
compile_fix    = { model = "codex/gpt-6-astra", effort = "low", timeout_seconds = 1800 }
# API 翻译的例子：translate = { model = "deepseek/deepseek-v4-pro" }，并把 [chunking] 改小（例如 5000 / 1500）
"""
