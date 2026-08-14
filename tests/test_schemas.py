"""产物契约 schemas 冒烟测（docs/schemas/，架构 §7）。

零期不引第三方校验库（运行时零依赖，dev 也先不加）：只查每份 schema 是合法 JSON、
draft 2020-12、带 $id / title / description，且 JSON 产物都约定了 contract_version。
真正的实例校验从 M2 的恒等翻译 e2e 起在 CI 里做。
"""

import json
from pathlib import Path

import pytest

from tongtu import CONTRACT_VERSION

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "docs" / "schemas"

#: 契约要求存在的 schema 文件（缺一个即 CI 红）。
EXPECTED = {
    "blocks",
    "anchors",
    "chunks",
    "brief",
    "glossary",
    "report",
    "figures",
    "events",
}

DRAFT = "https://json-schema.org/draft/2020-12/schema"


def schema_files():
    return sorted(SCHEMA_DIR.glob("*.schema.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_expected_schemas_present():
    found = {p.name.removesuffix(".schema.json") for p in schema_files()}
    assert found == EXPECTED


@pytest.mark.parametrize("path", schema_files(), ids=lambda p: p.name)
def test_schema_is_wellformed(path):
    schema = load(path)
    assert schema["$schema"] == DRAFT, "必须是 JSON Schema draft 2020-12"
    assert schema["$id"].endswith(path.name), "$id 与文件名不一致"
    assert schema["title"]
    assert schema["description"], "顶层需要描述性中文 description"
    assert schema["type"] == "object"
    assert isinstance(schema.get("$defs", {}), dict)


@pytest.mark.parametrize("path", schema_files(), ids=lambda p: p.name)
def test_contract_version_is_declared(path):
    """所有 JSON 产物（含事件流的每个事件）都携带 contract_version。"""
    schema = load(path)
    if "properties" in schema:
        assert "contract_version" in schema["properties"]
        assert "contract_version" in schema.get("required", [])
    else:  # events：分支式 schema，逐分支检查
        branches = schema["oneOf"]
        assert branches
        for branch in branches:
            target = branch["$ref"].removeprefix("#/$defs/")
            variant = schema["$defs"][target]
            assert "contract_version" in variant["properties"]
            assert "contract_version" in variant["required"]


@pytest.mark.parametrize("path", schema_files(), ids=lambda p: p.name)
def test_local_refs_resolve(path):
    """所有 $ref 都是本文件内的 #/$defs/... 且真实存在（草案期不做跨文件引用）。"""
    schema = load(path)
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref is not None:
                assert ref.startswith("#/$defs/"), f"{path.name}: 非本地引用 {ref}"
                assert ref.removeprefix("#/$defs/") in defs, f"{path.name}: 悬空引用 {ref}"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)


def test_current_contract_version_matches_pattern():
    """包里声明的 CONTRACT_VERSION 要能通过 schema 的 pattern。"""
    import re

    pattern = load(SCHEMA_DIR / "report.schema.json")["$defs"]["contract_version"]["pattern"]
    assert re.fullmatch(pattern, CONTRACT_VERSION)
