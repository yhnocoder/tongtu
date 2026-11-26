from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..config import config_path

RUNTIME_NAMES: tuple[str, ...] = ("codex", "claude-code", "pi")


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


class PdfConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crop_margins: bool = Field(default=False, strict=True)


DEFAULT_TIMEOUT_SECONDS = 3600


class ModeName(StrEnum):
    FAST = "fast"
    SMART = "smart"


def parse_model(text: str) -> tuple[str, str]:
    runtime, _separator, model = text.strip().partition("/")
    if runtime not in RUNTIME_NAMES:
        raise ValueError(
            f"a model must be written as runtime/model, where runtime is {', '.join(RUNTIME_NAMES)}; got {text!r}"
        )
    return runtime, model


class ModeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    effort: str | None = None
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)

    @field_validator("model")
    @classmethod
    def runtime_model(cls, value: str) -> str:
        parse_model(value)
        return value.strip()


class ModesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: ModeName = ModeName.SMART
    fast: ModeConfig
    smart: ModeConfig


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modes: ModesConfig
    fonts: FontsConfig = FontsConfig()
    pdf: PdfConfig = PdfConfig()


@dataclass(frozen=True)
class Target:
    runtime: str
    model: str
    effort: str | None
    timeout_seconds: float

    def __str__(self) -> str:
        return f"{self.runtime}/{self.model}" if self.model else self.runtime


def mode_target(config: ModelsConfig, mode: str) -> tuple[Target | None, str]:
    if mode not in tuple(ModeName):
        return None, f"mode {mode} does not exist; the modes are {', '.join(ModeName)}."
    entry: ModeConfig = getattr(config.modes, mode)
    runtime, model = parse_model(entry.model)
    return Target(runtime, model, entry.effort, entry.timeout_seconds), ""


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


CONFIG_TEMPLATE = """\
# 模式：一次运行里的全部 agent 会话（翻译会话与两种修复会话）用同一个模式
# fast：初步查看译文，费用最低；smart：准备用中文 PDF 代替原文来读
# 命令行的 --mode 优先；不给时用 default
# model 写成「运行时/模型」，只在第一个 / 处切分；运行时是 agentrun 支持的 codex、claude-code、pi
# 只写运行时名时用运行时自己的默认模型；effort 不写就不传给运行时
# timeout_seconds 是每次会话的时限，不写时为 3600 秒
# 改了这一段之后，要用新的设置重译已经译完的论文，执行 tongtu run PAPER --from translate
[modes]
default = "smart"

[modes.fast]
model  = "claude-code/claude-sonnet-5-5"
effort = "low"

[modes.smart]
model  = "claude-code/claude-opus-5-5"
effort = "medium"

# 中文字体：main 正文、sans 无衬线、mono 等宽（不写则用 main 的常规字体）
# main 与 sans 写一族字体：只写一个文件表示这一族只有常规字形，粗体由 xeCJK 伪粗体生成；写成 { regular = …, bold = … } 同时给出粗体
# 文件写仓库 fonts/ 里的文件名，或绝对路径、以 ~ 开头的路径；编译时由 TTFONTS / OPENTYPEFONTS 把这些目录交给 XeTeX
# 不接受系统字体名：zh.tex 只写文件名，论文目录才能在不同机器之间搬动
# 一族里有文件找不到时，这一族整体改用自带的字体，并在 precompile 的 manifest 里给出警告
# 改了这一段之后从 precompile 重跑：tongtu run PAPER --from precompile
[fonts]
main = { regular = "LXGWWenKai-Light.ttf", bold = "LXGWWenKai-Medium.ttf" }
sans = { regular = "SourceHanSansSC-Regular.otf", bold = "SourceHanSansSC-Bold.otf" }

# 中文 PDF：crop_margins = true 时 compile 把 out/ 下 PDF 的页边白边裁掉（只改 CropBox，排版与链接不变，需要 Ghostscript）
# 改了这一段之后从 compile 重跑：tongtu run PAPER --from compile
[pdf]
crop_margins = false
"""
