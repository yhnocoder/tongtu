from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ..config import config_dir

MODELS_FILENAME = "models.toml"

DEFAULT_ASK_MODEL = {"opencode": "deepseek-v4-pro", "deepseek": "deepseek-v4-pro", "anthropic": "claude-sonnet-5"}


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
    api_key: str | None = None
    api_key_env: str | None = None
    api: str | None = None
    models: dict[str, str] = {}


class RuntimeConfig(BaseModel):
    skill_path: str
    command: list[str]
    settings: dict | None = None
    provider: str | None = None
    env: dict[str, str] | None = None


class FontsConfig(BaseModel):
    main: str | list[str] = "LXGWWenKai-Light.ttf"
    bold: str | None = "LXGWWenKai-Medium.ttf"
    sans: str | list[str] | None = None
    mono: str | list[str] | None = None


class RoleConfig(BaseModel):
    model: str
    effort: str
    provider: str | None = None
    runtime: str | None = None
    max_turns: int | None = None
    timeout_seconds: float | None = None


class ModelsConfig(BaseModel):
    provider: dict[str, ProviderConfig] = {}
    runtime: dict[str, RuntimeConfig] = {}
    fonts: FontsConfig = FontsConfig()
    roles: dict[str, RoleConfig] = {}


def models_path() -> Path:
    return config_dir() / MODELS_FILENAME


def load_config() -> tuple[ModelsConfig | None, str]:
    path = models_path()
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
        return ModelsConfig.model_validate(data), ""
    except ValidationError as error:
        return None, f"config file {path} has invalid fields ({error})."


def provider_key(name: str, provider: ProviderConfig) -> tuple[str | None, str]:
    written = (provider.api_key or "").strip()
    if written:
        return written, "api_key in models.toml"
    variable = (provider.api_key_env or "").strip()
    if variable:
        value = (os.environ.get(variable) or "").strip()
        if value:
            return value, f"environment variable {variable}"
    if variable:
        return None, (
            f"no key for provider {name}. Write api_key under [provider.{name}] in {models_path()}, "
            f"or set the environment variable {variable}."
        )
    return None, (
        f"no key for provider {name}. Write api_key under [provider.{name}] in {models_path()}, "
        f"or write api_key_env naming the environment variable that holds the key."
    )


def role_config(config: ModelsConfig, role: str) -> tuple[RoleConfig | None, str]:
    found = config.roles.get(role)
    if found is None:
        return None, f"config file {models_path()} has no role {role} under [roles]; add one."
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
            return None, (
                f"a model override must be written as {table}/model, where {table} is a name under [{table}.*]; got {model}."
            )
        name, chosen = prefix, tail
    if name is None:
        return None, f"role {role} has no {table} field; add it under [roles] in {models_path()}."
    if name not in declared:
        return None, (
            f"config file {models_path()} does not declare {table} {name}; add it under [{table}.{name}]. "
            f"A model override prefix must also be a name under [{table}.*]."
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
        return (
            None,
            f"config file {models_path()} does not declare provider {provider}; add it under [provider.{provider}].",
        )
    api = entry.models.get(model) or entry.api
    if api is None:
        return None, (
            f"provider {provider} has no entry for model {model} in its models table and no api field, "
            f"so its API kind is unknown. Add a models entry or a provider-wide api in {models_path()}."
        )
    if api not in tuple(Api):
        return (
            None,
            f"provider {provider} gives model {model} the API {api}; only chat / responses / messages are accepted.",
        )
    return Api(api), ""


MODELS_TEMPLATE = """\
# 服务商：一个 API 前缀 + 一个密钥 + 哪个模型走哪种接口（api 取 chat / responses / messages）
[provider.opencode]
base_url    = "https://opencode.ai/zen/go"   # 接口前缀的根，chat / responses / messages 都在它下面的 /v1/...
api_key     = ""                  # 直接写密钥，或留空改用下面的环境变量
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

[provider.deepseek]
base_url    = "https://api.deepseek.com"
api_key     = ""                  # 直接写密钥，或留空改用下面的环境变量
api_key_env = "DEEPSEEK_API_KEY"
api         = "chat"              # OpenAI 兼容；模型 deepseek-v4-flash / deepseek-v4-pro

[provider.anthropic]
base_url    = "https://api.anthropic.com"
api_key     = ""                  # 直接写密钥，或留空改用下面的环境变量
api_key_env = "ANTHROPIC_API_KEY"
api         = "messages"          # 整个服务商一种接口时不必逐模型列

# 会话运行时：一条命令模板，{model} {effort} {max_turns} {tmp_dir} 由 work 填入；{tmp_dir} 是 work 为本次会话建的临时目录，会话结束即删
[runtime.claude_code]
skill_path = ".claude/skills/{role}"     # skill 目录拷到现场的哪里
command = ["claude", "-p", "--model", "{model}", "--effort", "{effort}", "--max-turns", "{max_turns}",
           "--output-format", "stream-json", "--verbose",
           "--setting-sources", "", "--strict-mcp-config",                       # 不加载用户 hooks / MCP / 插件；订阅登录照常（--bare 只认 API key，不用）
           "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash", "--permission-mode", "acceptEdits",
           "--disallowedTools", "Edit(.claude/skills/**)",                       # agent 改不了 skill 目录（含 validate.py），重定向覆盖也按这条拦
           "--settings", "{settings}"]                                           # 下面 settings 表序列化成 JSON 填入
settings = { sandbox = { enabled = true, autoAllowBashIfSandboxed = true, allowUnsandboxedCommands = false, failIfUnavailable = true, network = { allowedDomains = [] } } }
env = { ANTHROPIC_API_KEY = "", ANTHROPIC_AUTH_TOKEN = "", ANTHROPIC_BASE_URL = "" }   # 空串 = 未设：钉死订阅登录；-p 模式下 shell 里导出的这些变量会不问直接压过登录
# 沙箱：macOS 零安装（Seatbelt），Linux 镜像装 bubblewrap 与 socat；写范围 = 会话 cwd，断网
# 嵌套容器（如云端会话）里设环境变量 TONGTU_NESTED_SANDBOX=1，work 会在有 sandbox 表的运行时合入 enableWeakerNestedSandbox = true
# Bash 在沙箱内自由使用：写范围 = 现场，断网；agent 做得对不对由驱动器在现场外重跑校验终判
# work 拉起子进程时环境加 TONGTU_DISABLE=1、PATH 收成固定清单

# 同一个 Claude Code，模型换成 opencode 上的：一个运行时条目 = 一个「工具 × 服务商」组合
[runtime.claude_code_opencode]
provider = "opencode"                    # {base_url} 与 {api_key} 由 work 从 [provider.opencode] 填入
skill_path = ".claude/skills/{role}"     # skill 目录拷到现场的哪里
command = ["claude", "-p", "--model", "{model}", "--effort", "{effort}", "--max-turns", "{max_turns}",
           "--output-format", "stream-json", "--verbose",
           "--setting-sources", "", "--strict-mcp-config",                       # 不加载用户 hooks / MCP / 插件；订阅登录照常（--bare 只认 API key，不用）
           "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash", "--permission-mode", "acceptEdits",
           "--disallowedTools", "Edit(.claude/skills/**)",                       # agent 改不了 skill 目录（含 validate.py），重定向覆盖也按这条拦
           "--settings", "{settings}"]                                           # 下面 settings 表序列化成 JSON 填入
settings = { sandbox = { enabled = true, autoAllowBashIfSandboxed = true, allowUnsandboxedCommands = false, failIfUnavailable = true, network = { allowedDomains = [] } } }
env = { ANTHROPIC_BASE_URL = "{base_url}", ANTHROPIC_API_KEY = "{api_key}", ANTHROPIC_DEFAULT_HAIKU_MODEL = "{model}", ANTHROPIC_DEFAULT_SONNET_MODEL = "{model}", ANTHROPIC_DEFAULT_OPUS_MODEL = "{model}", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1", DISABLE_TELEMETRY = "1" }
# opencode 的 messages 端点只认 x-api-key，所以密钥写进 ANTHROPIC_API_KEY；ANTHROPIC_AUTH_TOKEN 走 Bearer，会 401
# Claude Code 把 ANTHROPIC_BASE_URL 当根，自己拼 /v1/messages，所以这里填的是不带 /v1 的 base_url
# 三个 DEFAULT_*_MODEL 都指同一个模型，防止后台的小调用拿 claude 系列的名字去打 opencode
# 密钥经进程环境传入而不是写进命令行参数：命令行参数在 ps 里可见

# Codex + opencode：只靠 Codex 自带的沙箱（-s workspace-write + approval never），不写 .codex/rules——它的 allow 是「沙箱外执行」
[runtime.codex_opencode]
provider   = "opencode"
skill_path = ".codex/skills/{role}"
command = ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
           "-s", "workspace-write", "-c", 'approval_policy="never"',
           "-m", "{model}", "-c", 'model_reasoning_effort="{effort}"',
           "-c", 'model_provider="opencode"', "-c", 'model_providers.opencode.name="opencode"',
           "-c", 'model_providers.opencode.base_url="{base_url}/v1"',
           "-c", 'model_providers.opencode.env_key="OPENCODE_API_KEY"',
           "-c", 'model_providers.opencode.wire_api="responses"']
env = { OPENCODE_API_KEY = "{api_key}", CODEX_HOME = "{tmp_dir}" }
# prompt 不作为位置参数：codex exec 没给 prompt 参数时从 stdin 读，work 正是经 stdin 喂 PROMPT；cwd 由 work 设为现场，不加 -C
# CODEX_HOME 指向 work 为本次会话建的临时目录，会话结束整个删掉；~/.codex/ 不被读也不被写
# max_turns 对 Codex 不生效：沙箱只靠 -s workspace-write 与 approval_policy="never"，写范围 = 现场 + 临时目录，默认断网
# wire_api 只接受 responses，所以这个条目只能跑 opencode 的 responses 端点支持的模型：gpt-5.6-luna、grok-4.5、muse-spark-1.2-contributor、deepseek-v4-flash
# codex 须是原生二进制（brew cask 装的）：npm 装的是 node 启动脚本，work 把子进程 PATH 收成 TeX + 系统 bin 后找不到 node，退出码 127

# 中文字体：main 正文、bold 粗体、sans 无衬线、mono 等宽；改这里即可切换译文 PDF 的字体
# 取值两类：字体文件（.ttf / .otf / .ttc——仓库 fonts/ 里的文件名，或一个文件路径，可用 ~）、系统字体名（如 "Noto Serif CJK SC"）
# main / sans / mono 还可写成列表按序回退（同 CSS 的 font-family）：系统字体存在就用，不存在试下一个；
# 文件候选必然可用，它之后的候选不会被用到；候选全是系统字体名时，末尾自动补默认的霞鹜文楷兜底
# 例：main = ["Source Han Serif SC", "Noto Serif CJK SC", "LXGWWenKai-Light.ttf"]
# bold 要与候选同类（同为文件或同为字体名）才配对生效，默认的 bold 只与默认的 main 配对
# sans 不写则按平台探测（Hiragino Sans GB → Noto Sans CJK SC → 霞鹜文楷）；mono 不写则用 main
[fonts]
main = "LXGWWenKai-Light.ttf"
bold = "LXGWWenKai-Medium.ttf"

# 角色：流水线里每一处调模型的地方一个名字，这里定它默认用什么
[roles]
survey_terms   = { provider = "opencode", model = "deepseek-v4-flash", effort = "low" }   # 可选；不配就不提议
translate      = { provider = "opencode", model = "deepseek-v4-pro", effort = "none" }
review         = { runtime = "claude_code", model = "claude-opus-5", effort = "medium", max_turns = 300, timeout_seconds = 3600 }
precompile_fix = { runtime = "claude_code", model = "claude-sonnet-5", effort = "xhigh", max_turns = 40, timeout_seconds = 1800 }
compile_fix    = { runtime = "claude_code", model = "claude-sonnet-5", effort = "xhigh", max_turns = 40, timeout_seconds = 1800 }
"""
