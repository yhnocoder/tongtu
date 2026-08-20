from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ..config import config_dir

MODELS_FILENAME = "models.toml"


class Api(StrEnum):
    CHAT = "chat"
    RESPONSES = "responses"
    MESSAGES = "messages"


class RoleTable(StrEnum):
    PROVIDER = "provider"
    RUNTIME = "runtime"


@dataclass(frozen=True)
class ResolvedRole:
    provider: str | None
    runtime: str | None
    model: str
    effort: str


class ProviderConfig(BaseModel):
    base_url: str
    api_key_env: str
    api: str | None = None
    models: dict[str, str] = {}


class RuntimeConfig(BaseModel):
    skill_path: str
    command: list[str]


class RoleConfig(BaseModel):
    model: str
    effort: str
    provider: str | None = None
    runtime: str | None = None
    max_turns: int | None = None
    timeout_seconds: float | None = None
    bash: list[str] | None = None


class ModelsConfig(BaseModel):
    provider: dict[str, ProviderConfig] = {}
    runtime: dict[str, RuntimeConfig] = {}
    roles: dict[str, RoleConfig] = {}


def models_path() -> Path:
    return config_dir() / MODELS_FILENAME


def load_config() -> tuple[ModelsConfig | None, str]:
    path = models_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        return None, f"读不到配置文件 {path}（{type(error).__name__}：{error}）。先运行 tongtu setup 写出模板。"
    except tomllib.TOMLDecodeError as error:
        return None, f"配置文件 {path} 不是合法的 TOML（{error}）。"
    try:
        return ModelsConfig.model_validate(data), ""
    except ValidationError as error:
        return None, f"配置文件 {path} 的字段不符合要求（{error}）。"


def role_config(config: ModelsConfig, role: str) -> tuple[RoleConfig | None, str]:
    found = config.roles.get(role)
    if found is None:
        return None, f"配置文件 {models_path()} 的 [roles] 里没有角色 {role}，补一条。"
    return found, ""


def resolve_role(
    config: ModelsConfig,
    role: str,
    table: RoleTable,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[ResolvedRole | None, str]:
    entry, detail = role_config(config, role)
    if entry is None:
        return None, detail
    declared = config.provider if table is RoleTable.PROVIDER else config.runtime
    name = entry.provider if table is RoleTable.PROVIDER else entry.runtime
    chosen = entry.model
    if model is not None:
        prefix, separator, tail = model.partition("/")
        if not separator or not prefix or not tail:
            return None, (f"覆盖的模型要写成 {table}/模型名，{table} 是 [{table}.*] 里的名字，给的是 {model}。")
        name, chosen = prefix, tail
    if name is None:
        return None, f"角色 {role} 没有 {table} 字段，在 {models_path()} 的 [roles] 里补上。"
    if name not in declared:
        return None, (
            f"配置文件 {models_path()} 里没有声明 {table} {name}，在 [{table}.{name}] 下补上；"
            f"覆盖的模型前缀也要是 [{table}.*] 里的名字。"
        )
    resolved = ResolvedRole(
        provider=name if table is RoleTable.PROVIDER else None,
        runtime=name if table is RoleTable.RUNTIME else None,
        model=chosen,
        effort=effort or entry.effort,
    )
    return resolved, ""


def model_api(config: ModelsConfig, provider: str, model: str) -> tuple[Api | None, str]:
    entry = config.provider.get(provider)
    if entry is None:
        return None, f"配置文件 {models_path()} 里没有声明服务商 {provider}，在 [provider.{provider}] 下补上。"
    api = entry.models.get(model) or entry.api
    if api is None:
        return None, (
            f"服务商 {provider} 的 models 表里没有模型 {model}，服务商也没有 api 字段，"
            f"不知道它走哪种接口。在 {models_path()} 里补 models 表的一条或整个服务商的 api。"
        )
    if api not in tuple(Api):
        return None, f"服务商 {provider} 给模型 {model} 的接口是 {api}，取值只能是 chat / responses / messages。"
    return Api(api), ""


MODELS_TEMPLATE = """\
# 服务商：一个 API 前缀 + 一个密钥变量名 + 哪个模型走哪种接口（api 取 chat / responses / messages）
[provider.opencode]
base_url    = "https://opencode.ai/zen/go/v1"
api_key_env = "OPENCODE_API_KEY"

[provider.opencode.models]
"deepseek-v4-pro" = "chat"
"deepseek-v4-flash" = "chat"
"glm-5.3" = "chat"
"glm-5.2" = "chat"
"glm-5.1" = "chat"
"kimi-k3" = "chat"
"kimi-k2.7-code" = "chat"
"kimi-k2.6" = "chat"
"mimo-v2.5" = "chat"
"mimo-v2.5-pro" = "chat"
"hy3" = "chat"
"grok-4.5" = "responses"
"gpt-5.6-luna" = "responses"
"muse-spark-1.2-contributor" = "responses"
"minimax-m3" = "messages"
"minimax-m2.7" = "messages"
"minimax-m2.5" = "messages"
"qwen3.8-max" = "messages"
"qwen3.7-max" = "messages"
"qwen3.7-plus" = "messages"
"qwen3.6-plus" = "messages"

[provider.anthropic]
base_url    = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
api         = "messages"          # 整个服务商一种接口时不必逐模型列

# 会话运行时：一条命令模板，{model} {effort} {max_turns} 由 work 填入
[runtime.claude_code]
skill_path = ".claude/skills/{role}"     # skill 目录拷到现场的哪里
command = ["claude", "-p", "--model", "{model}", "--effort", "{effort}", "--max-turns", "{max_turns}",
           "--output-format", "stream-json", "--verbose",
           "--allowedTools", "Read,Edit,Write,Glob,Grep,{bash_allow}", "--permission-mode", "acceptEdits"]
# {bash_allow} 由角色给出，形如 "Bash(python3 validate.py:*)"

# 角色：流水线里每一处调模型的地方一个名字，这里定它默认用什么
[roles]
survey_terms   = { provider = "opencode", model = "deepseek-v4-flash", effort = "low" }   # 可选；不配就不提议
translate      = { provider = "opencode", model = "deepseek-v4-flash", effort = "low" }
review         = { runtime = "claude_code", model = "claude-sonnet-5", effort = "high", max_turns = 80, timeout_seconds = 3600, bash = ["python3 validate.py"] }
precompile_fix = { runtime = "claude_code", model = "claude-sonnet-5", effort = "xhigh", max_turns = 40, timeout_seconds = 1800, bash = ["latexmk", "xelatex", "kpsewhich"] }
compile_fix    = { runtime = "claude_code", model = "claude-sonnet-5", effort = "xhigh", max_turns = 40, timeout_seconds = 1800, bash = ["latexmk", "xelatex", "kpsewhich"] }
"""
