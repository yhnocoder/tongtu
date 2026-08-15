"""artifact model 的公共定义：基类、公共标量类型与契约版本号。

架构 §5 artifact contract 节（决策 A.22）：各 JSON artifact 以本包下的 pydantic model
为字段级权威定义，字段、类型与默认值只在 model 一处定义，artifact 的读写都经过
model 校验。语言中立的 JSON Schema 由 model 生成（``model_json_schema()``），不提交进仓库。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

#: 产物契约版本号。0.2 是 docs/schemas/ 手写草案的版本；artifact model 成为权威定义时
#: 契约字段有变（survey_restore 语义、title 槽位移除、figures 一图多文件、原语改名等，
#: 见 docs/BACKLOG.md 设计变更清单），故 bump 到 0.3。``tongtu/__init__.py`` 的同名常量
#: 服务旧实现，流水线切换到 artifact model 时移除。
CONTRACT_VERSION = "0.3"

ContractVersion = Annotated[
    str,
    Field(
        pattern=r"^[0-9]+\.[0-9]+(\.[0-9]+)?$",
        description="产物契约版本号。契约变更 = 改 model 并 bump 此值（架构 §5 artifact contract 节）。",
    ),
]

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="小写十六进制 SHA-256。")]

#: 回退原因，chunks.json 与 report.json 共用同一取值集合。
FallbackReason = Literal["validate_failed", "compile_failed", "agent_error", "agent_unavailable", "other"]


class ArtifactModel(BaseModel):
    """全部 artifact model 的基类。

    - 未声明的字段一律拒绝（``extra="forbid"``），字段集合以 model 定义为准；
    - 写出统一 UTF-8、缩进 2、非 ASCII 不转义、末尾换行；值为 ``None`` 的可选字段不写出；
    - 读入即校验，不合契约抛 ``pydantic.ValidationError``。
    """

    model_config = ConfigDict(extra="forbid")

    def to_json_bytes(self) -> bytes:
        data = self.model_dump(mode="json", exclude_none=True)
        return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def write_json(self, path: Path) -> None:
        path.write_bytes(self.to_json_bytes())

    @classmethod
    def read_json(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_bytes())
