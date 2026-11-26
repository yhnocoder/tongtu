from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import tomli_w
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
    fast: ModeConfig = ModeConfig(model="claude-code/claude-sonnet-5-5", effort="low")
    smart: ModeConfig = ModeConfig(model="claude-code/claude-opus-5-5", effort="medium")


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modes: ModesConfig = ModesConfig()
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
    except FileNotFoundError:
        return ModelsConfig(), ""
    except OSError as error:
        return None, f"cannot read config file {path} ({type(error).__name__}: {error})."
    except tomllib.TOMLDecodeError as error:
        return None, f"config file {path} is not valid TOML ({error})."
    try:
        config = ModelsConfig.model_validate(data)
    except ValidationError as error:
        return None, f"config file {path} has invalid fields ({error})."
    return config, ""


def _leaves(prefix: str, value: object) -> list[tuple[str, object]]:
    if isinstance(value, dict):
        return [entry for name, item in value.items() for entry in _leaves(f"{prefix}{name}.", item)]
    return [(prefix.removesuffix("."), value)]


def config_entries(config: ModelsConfig) -> list[tuple[str, object]]:
    return _leaves("", config.model_dump(mode="python"))


def updated_config(config: ModelsConfig, key: str, value: object) -> ModelsConfig:
    if key not in dict(config_entries(config)):
        raise KeyError(key)
    data = config.model_dump(mode="python")
    *parents, leaf = key.split(".")
    table = data
    for name in parents:
        table = table[name]
    table[leaf] = value
    return ModelsConfig.model_validate(data)


def write_config(config: ModelsConfig) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(config.model_dump(mode="json", exclude_none=True)), encoding="utf-8")
    path.chmod(0o600)
    return path


def ensure_config_file() -> tuple[ModelsConfig | None, str]:
    if not config_path().exists():
        write_config(ModelsConfig())
    return load_config()
