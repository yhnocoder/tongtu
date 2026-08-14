"""零第三方依赖的 JSON Schema 校验（够用子集）——产物契约的机械裁决者。

架构 §13 要求运行时零第三方依赖，而架构 §7 要求「CI 对产物做 schema 校验」、§3 的
survey 行把「`brief.json` 与结构化术语表通过 schema 校验」写成阶段出口判据。两条约束
的交集就是本模块：一个**只够用**的校验器，覆盖 `docs/schemas/*.schema.json` 实际用到的
关键字——type / required / properties / additionalProperties / oneOf / $ref / const /
enum / pattern / items / minimum，不追求完备，也不打算长成 jsonschema。

它原先住在 `tests/test_e2e_identity.py` 里（只给 e2e 用）；survey 阶段要在运行时校验自
己的产物，于是抽到运行时包内，e2e 改为引用同一份实现——测试与生产用同一份实现校验，
「测试里过了、生产里没查」这类偏差不复存在。

schema 文件的定位与 `fonts/` 同法（见 `tongtu.compiler.find_fonts`）：源码树 /
editable 安装态从仓库根 `docs/schemas/` 找，wheel 安装态从包内 `tongtu/data/schemas/`
找（pyproject 的 `force-include`）。找不到时抛 :class:`SchemaError`，由调用方决定是
记警告继续还是判失败——本模块不替谁做这个决定。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

__all__ = [
    "PACKAGED_SCHEMAS",
    "SCHEMA_DIRNAME",
    "SchemaError",
    "check",
    "find_schema_dir",
    "load_schema",
    "validate_schema",
]


class SchemaError(LookupError):
    """schema 文件找不到或读不出来（不是「实例不合规」——那是返回值的事）。"""


#: 源码树里 schema 的位置（仓库根之下）。
SCHEMA_DIRNAME = "docs/schemas"

#: wheel 里 schema 的落点（pyproject 的 `[tool.hatch.build.targets.wheel.force-include]`）。
PACKAGED_SCHEMAS = "data/schemas"


_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value, name: str) -> bool:
    expected = _TYPES[name]
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def validate_schema(
    instance, schema: dict, root: dict | None = None, path: str = "$"
) -> list[str]:
    """返回不合规之处（空列表 = 通过）。支持自家 schema 用到的那些关键字。"""
    root = schema if root is None else root
    errors: list[str] = []

    ref = schema.get("$ref")
    if ref is not None:
        target = root
        for part in ref.removeprefix("#/").split("/"):
            target = target[part]
        return validate_schema(instance, target, root, path)

    if "type" in schema:
        names = schema["type"]
        names = [names] if isinstance(names, str) else names
        if not any(_type_ok(instance, name) for name in names):
            return [f"{path}: 类型应为 {names}，实际 {type(instance).__name__}"]

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: 应为常量 {schema['const']!r}，实际 {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: 应属枚举 {schema['enum']}，实际 {instance!r}")
    if "pattern" in schema and isinstance(instance, str):
        if re.search(schema["pattern"], instance) is None:
            errors.append(f"{path}: {instance!r} 不匹配 {schema['pattern']}")
    if "minimum" in schema and isinstance(instance, (int, float)) and instance < schema["minimum"]:
        errors.append(f"{path}: {instance} 小于下界 {schema['minimum']}")

    if isinstance(instance, dict):
        for key in schema.get("required", ()):
            if key not in instance:
                errors.append(f"{path}: 缺必填字段 {key!r}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate_schema(value, properties[key], root, f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: 不认识的字段 {key!r}")

    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors.extend(validate_schema(item, schema["items"], root, f"{path}[{i}]"))

    if "oneOf" in schema:
        passed = [
            branch
            for branch in schema["oneOf"]
            if not validate_schema(instance, branch, root, path)
        ]
        if len(passed) != 1:
            errors.append(f"{path}: oneOf 命中 {len(passed)} 个分支（应恰为 1）")

    return errors


def _looks_like_schema_dir(path: Path) -> bool:
    return path.is_dir() and (path / "brief.schema.json").is_file()


def find_schema_dir() -> Path:
    """定位 `docs/schemas/`：源码树逐级向上找 → 包内 `tongtu/data/schemas/`。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / SCHEMA_DIRNAME
        if _looks_like_schema_dir(candidate):
            return candidate
    try:
        packaged = Path(str(files("tongtu").joinpath(PACKAGED_SCHEMAS)))
    except (ModuleNotFoundError, TypeError, OSError):
        packaged = None
    if packaged is not None and _looks_like_schema_dir(packaged):
        return packaged
    raise SchemaError(
        f"找不到产物契约 schema 目录（源码树 {SCHEMA_DIRNAME}/ 或包内 {PACKAGED_SCHEMAS}/）"
    )


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    """读一份产物 schema（`"brief"` → `docs/schemas/brief.schema.json`）。"""
    path = find_schema_dir() / f"{name}.schema.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(f"读不出 schema {path}：{exc}") from exc


def check(instance, name: str) -> list[str]:
    """按名字取 schema 并校验，返回不合规之处（空列表 = 通过）。"""
    return validate_schema(instance, load_schema(name))
