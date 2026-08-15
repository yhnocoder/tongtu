"""anchors.json —— 交互地图（export 阶段合成，架构 §7）。

公式 / 图 / 表 / 章节在 zh.pdf 中的页码与矩形区域，供 inspection page 画热区与文枢
read path 定位。三来源（synctex / blocks 源码位置 / PDF 文本扫描）叠加，逐条记 source
与置信度。草案级：热区容差与叠加次序拿真实论文的 synctex 数据实测后校准（架构附录 B
第 4 条）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .base import CONTRACT_VERSION, ArtifactModel, ContractVersion

#: 锚点类型，决定 inspection page 热区样式与点击行为。
AnchorType = Literal["section", "equation", "figure", "table", "algorithm", "block", "citation"]

#: 锚点的定位来源；merged 表示多来源叠加。
AnchorSource = Literal["synctex", "blocks", "pdf-scan", "merged"]


class PageSize(ArtifactModel):
    """单页尺寸（pt），前端换算缩放比例用。"""

    page: Annotated[int, Field(ge=1)]
    width: Annotated[float, Field(gt=0)]
    height: Annotated[float, Field(gt=0)]


class PdfInfo(ArtifactModel):
    """被标注的 PDF。"""

    path: str = Field(description="artifact package 内相对路径，通常为 zh.pdf。")
    page_count: Annotated[int, Field(ge=1)]
    pages: list[PageSize] = Field(
        min_length=1,
        description="逐页尺寸，条数与 page_count 一致；inspection page 换算缩放比例的依据（pdf-scan 时零成本取得）。",
    )

    @model_validator(mode="after")
    def _check_page_count(self) -> PdfInfo:
        if len(self.pages) != self.page_count:
            raise ValueError(f"pages 条数（{len(self.pages)}）与 page_count（{self.page_count}）不一致")
        return self


class CoordinateSystem(ArtifactModel):
    """矩形坐标约定，避免前端猜。"""

    origin: Literal["top-left", "bottom-left"] = Field(
        default="top-left", description="原点位置；默认 top-left（与 PDF.js 视口一致）。"
    )
    unit: Literal["pt"] = Field(default="pt", description="长度单位，恒为 PDF 点（1/72 inch）。")


class Rect(ArtifactModel):
    """页内矩形（含容差外扩后的最终热区）。"""

    x: float
    y: float
    w: Annotated[float, Field(ge=0)]
    h: Annotated[float, Field(ge=0)]


class Anchor(ArtifactModel):
    """一个锚点。"""

    id: str = Field(description="锚点唯一 id。")
    type: AnchorType
    label: str | None = Field(default=None, description="LaTeX \\label 值（如 eq:loss），交叉引用键。")
    number: str | None = Field(default=None, description="排版后的编号（如 3.2、Figure 4），从 PDF 或 aux 提取。")
    title: str | None = Field(default=None, description="章节标题或 caption 首句，侧栏展示用。")
    block_id: str | None = Field(default=None, description="对应 blocks.json 的 block id；点击热区即取其原始 TeX。")
    chunk_id: str | None = Field(default=None, description="所属翻译 chunk id，用于「这段是回退原文」的标注。")
    page: Annotated[int, Field(ge=1)] = Field(description="1-based 页码。")
    rects: list[Rect] = Field(min_length=1, description="热区矩形，跨行/跨栏对象可有多个。")
    source: AnchorSource | None = None
    confidence: Annotated[float, Field(ge=0, le=1)] | None = Field(
        default=None, description="定位置信度，供 inspection page 区分实线/虚线热区。"
    )


class AnchorsArtifact(ArtifactModel):
    """anchors.json 全文件。"""

    contract_version: ContractVersion = CONTRACT_VERSION
    pdf: PdfInfo
    coordinate_system: CoordinateSystem
    anchors: list[Anchor] = Field(default_factory=list, description="锚点列表，按页码与纵坐标排序。")
