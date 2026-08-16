"""stage manifest 的读写与异常格式化：各阶段驱动器共用的落盘约定。

manifest 的字段级定义在 `tongtu/artifacts/` 的各 model，本模块只管它们与磁盘之间的两个
动作：装载（读不到或不合 schema 一律返回 None，由调用方转对应状态）与写出（缺目录先建，
JSON 缩进两格、末尾带换行）。`describe_error` 是异常记进 manifest 的 `message` 时的统一
格式，各阶段的失败说明由此保持同一形状。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError


def load_manifest[ManifestT: BaseModel](path: Path, model_cls: type[ManifestT]) -> ManifestT | None:
    """读一份 manifest 并按 `model_cls` 解析；文件读不到或内容不合 schema 返回 None。

    驱动器装载上游结论与自己上次的结论都经此处：两种失败对调用方的含义相同（没有可用的
    已有结论），故不区分，也不向上抛。
    """
    try:
        return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


def write_manifest(path: Path, manifest: BaseModel) -> None:
    """把 manifest 写到 `path`：缺失的上级目录先建出，JSON 缩进两格并以换行结尾。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")


def describe_error(error: Exception) -> str:
    """异常统一格式化成「类型名：信息」，记入 manifest 的 message。"""
    return f"{type(error).__name__}：{error}"
