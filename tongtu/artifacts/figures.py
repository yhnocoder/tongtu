"""figures.json —— 图源元数据（figures 阶段产出，架构 §3 figures 节，决策 A.9/A.19）。

两条规则：位图源（png/jpg/webp）原样带走，不转码不缩放，元数据记原始尺寸；矢量源
（pdf/eps）转一份位图（固定 DPI，起步 150）并保留矢量原件。一图因此可有多个文件，
files 列表逐个记格式与来历。不在生产侧执行消费者的约束——视觉 API 的长边上限是那一个
消费者的数字，需要缩的一方自己缩。本阶段只读 src/ 与 blocks.json，逐图以源文件 sha256
缓存，翻译侧返工不触发重渲染；caption 译文由 export 从 compile backfill 落出的中间
artifact 并入。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .base import CONTRACT_VERSION, ArtifactModel, ContractVersion, Sha256

#: 源图格式。pdf 对 xelatex 是原生输入，eps 走 epstopdf 链，编译侧零处理——本阶段的
#: 产出只服务 AI 读图、inspection page 与 markdown / typst 等下游渲染。
FigureSourceFormat = Literal["pdf", "eps", "ps", "png", "jpg", "webp", "gif", "svg", "other"]

#: artifact package 内图文件的格式：位图源原样带走（png/jpg/webp），矢量原件保留
#: （pdf/eps），矢量源另转一份 png。
FigureFileFormat = Literal["png", "jpg", "webp", "pdf", "eps"]

#: 文件来历：original = 从 src/ 原样复制（位图源本体或矢量原件）；render = 矢量源按
#: 固定 DPI 转出的位图。
FigureFileKind = Literal["original", "render"]


class FigureCaption(ArtifactModel):
    """caption 原文与译文。"""

    source: str = Field(description="caption 原文，取自 blocks.json 的 CAP 槽位。")
    translation: str | None = Field(
        default=None,
        description="caption 译文，由 export 从 compile backfill 落出的中间 artifact 并入；未翻译（槽位未改动或整段回退）为空。",
    )


class FigureSource(ArtifactModel):
    """源图文件（src/ 内，只读）。"""

    path: str = Field(description="相对 src/ 的路径。")
    format: FigureSourceFormat
    sha256: Sha256 = Field(description="逐图缓存 key（架构 §4）。")
    width_pt: Annotated[float, Field(gt=0)] | None = Field(default=None, description="矢量源的原始宽度（pt）。")
    height_pt: Annotated[float, Field(gt=0)] | None = Field(default=None, description="矢量源的原始高度（pt）。")
    width_px: Annotated[int, Field(ge=1)] | None = Field(default=None, description="位图源的原始宽度（px）。")
    height_px: Annotated[int, Field(ge=1)] | None = Field(default=None, description="位图源的原始高度（px）。")


class FigureFile(ArtifactModel):
    """artifact package 内本图的一个文件。"""

    path: str = Field(description="artifact package 内相对路径，如 figures/fig-003.png。")
    format: FigureFileFormat
    kind: FigureFileKind
    width_px: Annotated[int, Field(ge=1)] | None = Field(default=None, description="位图文件的像素宽度。")
    height_px: Annotated[int, Field(ge=1)] | None = Field(default=None, description="位图文件的像素高度。")
    dpi: Annotated[float, Field(gt=0)] | None = Field(
        default=None, description="render 专有：矢量转位图所用的固定 DPI（起步 150）。"
    )
    bytes: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _check_kind_format(self) -> FigureFile:
        if self.format in ("png", "jpg", "webp") and (self.width_px is None or self.height_px is None):
            raise ValueError(f"位图文件（{self.format}）必须记录像素尺寸 width_px / height_px")
        if self.format in ("pdf", "eps") and self.kind != "original":
            raise ValueError("矢量格式只作为原件带走（kind=original）；render 的产出是位图")
        if self.kind == "render" and self.dpi is None:
            raise ValueError("render 文件必须记录转换所用的 DPI")
        return self


class FigureReference(ArtifactModel):
    """正文引用该图的位置（\\ref 家族命中），供 inspection page 与索引跳转。"""

    chunk_id: str | None = None
    paragraph: Annotated[int, Field(ge=0)] | None = None
    section: str | None = None
    text: str | None = Field(default=None, description="引用所在段落原文片段。")


class Figure(ArtifactModel):
    """一条图记录。\\includegraphics 的每一次出现算一条（subfigure 因此产出多条，共享 block_id）。"""

    id: str = Field(description="图 id，如 fig-003。")
    label: str | None = Field(default=None, description="LaTeX \\label 值，anchors 交叉引用键。")
    number: str | None = Field(default=None, description="排版后编号，如 Figure 3。")
    block_id: str | None = Field(default=None, description="所属 figure 环境在 blocks.json 中的 block id。")
    caption: FigureCaption | None = None
    source: FigureSource
    files: list[FigureFile] = Field(
        min_length=1,
        description="本图在 artifact package 内的全部文件：位图源为一个 original；矢量源为一个 original 加一个 render。",
    )
    referenced_in: list[FigureReference] = Field(default_factory=list)


class FiguresArtifact(ArtifactModel):
    """figures.json 全文件。"""

    contract_version: ContractVersion = CONTRACT_VERSION
    figures: list[Figure] = Field(default_factory=list, description="逐图元数据，按源码中出现顺序排列。")
