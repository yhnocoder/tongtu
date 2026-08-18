"""survey 阶段三份产物的字段级权威定义：stage manifest 与 `build/glossary.json`、`build/brief.json`。

`SurveyManifest` 是 `build/manifests/survey.json`：`status` 是唯一分流依据；`masked_sha256`、
`blocks_sha256` 与 `glossary_input_sha256` 是本阶段的三个输入 hash；`glossary_sha256` 与
`brief_sha256` 是输出 hash，下游判定「输入未变不重算」时引用它们，后者即架构 §4 缓存 key 里
的 `brief_hash`；`filtered` 是合并后未在全文命中、因而不进 resolved 的条目清单。

`GlossaryFile` 是 `build/glossary.json`（resolved glossary），`BriefFile` 是 `build/brief.json`，
两者都是 artifact contract 的一员，消费方是 translate。词条来源层的取值词表由
`tongtu/glossary.py` 定义（合并层产出它们），本模块按那份词表给字段定类型。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from ..glossary import GlossaryLayer


class SurveyStatus(StrEnum):
    """survey 的结果状态，JSON 序列化为小写字符串值。

    `OK` 之外都不是异常，调用方按状态分流：`MASK_MISSING` 与 `MASK_NOT_OK` 是前置条件不满足
    （上游 mask 没跑、产物有缺，或它的结论不是 ok）；`GLOSSARY_INVALID` 是某份 input glossary
    不可解析或不符合形状，具体文件与首个错误由 message 给出。
    """

    OK = "ok"
    MASK_MISSING = "mask_missing"
    MASK_NOT_OK = "mask_not_ok"
    GLOSSARY_INVALID = "glossary_invalid"


class AbstractSource(StrEnum):
    """摘要照录的来路：blocks.json 的前导区槽位、masked.tex 里的 abstract 环境，或两条都落空。"""

    PREAMBLE_SLOT = "preamble_slot"
    BODY_ENVIRONMENT = "body_environment"
    ABSENT = "absent"


class FilteredTerm(BaseModel):
    """一条合并后未在全文命中、因而不进 resolved glossary 的词条。"""

    word: str = Field(description="词的原写法，取胜出层的写法")
    decided_by: GlossaryLayer = Field(description="胜出层，即这条词条来自哪一层输入")


class GlossaryInputRecord(BaseModel):
    """一个合并单元的来路与是否存在，排查「某份表为何没生效」时看这里。"""

    layer: GlossaryLayer = Field(description="所属层：全局配置目录、论文工作目录或命令行")
    path: str = Field(default="", description="文件路径；命令行未给出 --glossary 时为空")
    present: bool = Field(default=False, description="该文件是否存在并被读入")


class SurveyManifest(BaseModel):
    """survey 的结构化结果，同时是它的 stage manifest。"""

    stage: Literal["survey"] = "survey"
    status: SurveyStatus
    masked_sha256: str = Field(
        default="", description="build/masked.tex 的 sha256，本阶段的输入 hash 之一，从 mask manifest 转录"
    )
    blocks_sha256: str = Field(
        default="", description="build/blocks.json 的 sha256，本阶段的输入 hash 之二，从 mask manifest 转录"
    )
    glossary_input_sha256: str = Field(
        default="", description="三层 input glossary 按层序规范化序列后的 sha256，本阶段的输入 hash 之三"
    )
    glossary_sha256: str = Field(default="", description="build/glossary.json 的 sha256，本阶段的输出 hash 之一")
    brief_sha256: str = Field(
        default="",
        description="build/brief.json 的 sha256，本阶段的输出 hash 之二，即架构 §4 缓存 key 里的 brief_hash",
    )
    terms_total: int = Field(default=0, description="resolved glossary 里给出译法的词条数")
    do_not_translate_total: int = Field(default=0, description="resolved glossary 里保留原文的词条数")
    filtered: list[FilteredTerm] = Field(
        default_factory=list, description="合并后未在全文命中、被过滤掉的条目清单，配置不静默消失"
    )
    glossary_inputs: list[GlossaryInputRecord] = Field(
        default_factory=list, description="本次看到的各合并单元及其存在与否，按层序排列，排查用"
    )
    abstract_source: AbstractSource = Field(default=AbstractSource.ABSENT, description="摘要照录的来路")
    abstract_chars: int = Field(default=0, description="照录的摘要字符数，观察值")
    mask_status: str = Field(default="", description="本次读到的 mask manifest 的状态，排查用")
    fetch_status: str = Field(default="", description="mask manifest 记录的 fetch 状态，退出码映射与排查用")
    warnings: list[str] = Field(default_factory=list)
    message: str = ""


class TermEntry(BaseModel):
    """resolved glossary 里一条给出译法的词条。"""

    word: str = Field(description="词的原写法，取胜出层的写法")
    translation: str = Field(description="该词在本篇的译法，translate 按它约束用词")
    decided_by: GlossaryLayer = Field(description="胜出层，即这条词条来自哪一层输入")


class DoNotTranslateEntry(BaseModel):
    """resolved glossary 里一条保留原文的词条。"""

    word: str = Field(description="词的原写法，取胜出层的写法")
    decided_by: GlossaryLayer = Field(description="胜出层，即这条词条来自哪一层输入")


class GlossaryFile(BaseModel):
    """`build/glossary.json` 的内容：本篇实际生效的词条与 style rules。

    两个列表只含在 `masked.tex` 中命中的词条，未命中的记在 manifest 的 `filtered` 里。`style`
    是用户写的一段额外要求，survey 不产 style，三层合并的结果原样转录到这里，由 translate 放
    进提示词。
    """

    terms: list[TermEntry] = Field(default_factory=list)
    do_not_translate: list[DoNotTranslateEntry] = Field(default_factory=list)
    style: str | None = Field(
        default=None,
        description="写给译者的额外要求，原样进 translate 的提示词；三层都没写这一段时为 null，survey 不造默认值",
    )


class BriefFile(BaseModel):
    """`build/brief.json` 的内容：逐 chunk 翻译共享的全局语境。

    零期只有 `abstract` 一个字段，是论文原文摘要的照录，由程序提取、不经模型；提取不到时为
    null，不是失败。将来的扩展字段落在这个文件里，`brief_sha256` 的语义不变。
    """

    abstract: str | None = Field(default=None, description="论文原文摘要的照录；提取不到为 null")
