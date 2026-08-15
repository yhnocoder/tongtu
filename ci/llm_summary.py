#!/usr/bin/env python3
"""LLM 质量层（架构 §12 层 3）试跑结果 → GitHub job summary 的 Markdown 表格。

`.github/workflows/llm-quality.yml` 的「汇总」步骤调它：

    uv run python ci/llm_summary.py >> "$GITHUB_STEP_SUMMARY"

参数默认从 workflow 已经导出的环境变量取（`PAPERS` / `QUALITY_DIR` / `TONGTU_HOME` /
`AGENT` / `MODEL` / `IMAGE`），故 workflow 里不必再重复一遍；本地要复现某次试跑的表格时，
用命令行参数指到下载下来的存档目录即可：

    python3 ci/llm_summary.py --help
    python3 ci/llm_summary.py --papers 2401.00001 --quality-dir ./llm-quality-123

**本文件不属于 tongtu 包**：它是 CI 的胶水，不是引擎代码，也不进 wheel。因此只用标准库、
不 import tongtu，可以脱离项目环境单跑。

## 两条取数路径

1. **产物包里的 `out/report.json`**（架构 §7 的权威统计）——优先；
2. **`--json` 事件流**降级——export 阶段尚未落地（PHASE0 §3.2，report.json 属 M4）或某篇
   跑挂在半路时只有它。能算多少算多少，算不出的字段写 `—` 而不是编一个 0。

「跑挂了」在这一层不是错误：退出码照实进表，作业照常绿（监控不门禁）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from dataclasses import dataclass

#: 表格里「这个数算不出来」的写法——与 0 区分开，免得把缺数据读成好成绩。
MISSING = "—"

COLUMNS = (
    "论文",
    "结果",
    "退出码",
    "块数",
    "回退块",
    "校验重试",
    "agent 干预",
    "编译警告",
    "编译",
    "耗时",
)


def safe_name(paper: str) -> str:
    """arXiv id → 文件名（旧式 id 带 `/`，如 `math/0601001`）。与 workflow 里同一规则。"""
    return paper.replace("/", "_")


def load_json(path: pathlib.Path) -> dict | None:
    """读一份 JSON；读不到或不是 JSON 都返回 None（缺数据不是异常）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_events(path: pathlib.Path) -> list[dict]:
    """读 `--json` 事件流（JSONL）。非 JSON 行是人类可读输出，跳过。"""
    events: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events


def cell(value) -> str:
    return MISSING if value is None else str(value)


@dataclass(frozen=True)
class Row:
    """表格的一行：一篇论文的一次试跑。字段值 None = 算不出来。"""

    paper: str
    status: str | None = None
    exit_code: str = MISSING
    chunks: int | None = None
    fallback: int | None = None
    retries: int | None = None
    interventions: int | None = None
    warnings: int | None = None
    compiled: bool | None = None
    duration_ms: int | None = None
    has_report: bool = False

    def to_markdown(self) -> str:
        compiled = MISSING if self.compiled is None else ("通过" if self.compiled else "未通过")
        duration = MISSING if self.duration_ms is None else f"{self.duration_ms / 1000:.0f}s"
        return f"| `{self.paper}` | {cell(self.status)} | {self.exit_code} | {cell(self.chunks)} | {cell(self.fallback)} | {cell(self.retries)} | {cell(self.interventions)} | {cell(self.warnings)} | {compiled} | {duration} |"


def collect(paper: str, quality_dir: pathlib.Path, tongtu_home: pathlib.Path) -> Row:
    """一篇论文的一行数据：report.json 优先，缺什么就退到事件流补什么。"""
    safe = safe_name(paper)
    report_path = quality_dir / f"{safe}.report.json"
    report = load_json(report_path) or load_json(tongtu_home / safe / "out" / "report.json")
    events = load_events(quality_dir / f"{safe}.events.jsonl")
    result = next((e for e in reversed(events) if e.get("event") == "result"), {})
    progress = [e for e in events if e.get("event") == "chunk_progress"]

    try:
        exit_code = (quality_dir / f"{safe}.exit").read_text(encoding="utf-8").strip()
    except OSError:
        exit_code = MISSING

    validation = (report or {}).get("validation", {})
    compile_info = (report or {}).get("compile", {})

    fallback = validation.get("fallback")
    if fallback is None and report is not None:
        fallback = len(report.get("fallbacks", []))
    if fallback is None:
        fallback = result.get("fallback_chunks")
    if fallback is None and progress:
        fallback = sum(1 for e in progress if e.get("status") == "fallback")

    retries = validation.get("retries")
    if retries is None and progress:
        retries = sum(1 for e in progress if e.get("status") == "retry")

    warnings = None
    if compile_info:
        warnings = sum(w.get("count", 1) for w in compile_info.get("warnings", []))

    return Row(
        paper=paper,
        status=(report or {}).get("status") or result.get("status"),
        exit_code=exit_code or MISSING,
        chunks=validation.get("chunks_total", result.get("chunks_total")),
        fallback=fallback,
        retries=retries,
        interventions=(len(report.get("agent_interventions", [])) if report is not None else None),
        warnings=warnings,
        compiled=compile_info.get("passed"),
        duration_ms=result.get("duration_ms"),
        has_report=report_path.is_file(),
    )


def render(rows: list[Row], *, agent: str, model: str, image: str) -> str:
    """整张 job summary（Markdown）。"""
    lines = [
        "## LLM 质量层试跑",
        "",
        f"- 运行时：`{agent}` ／ 模型：`{model or '(运行时默认)'}` ／ 镜像：`{image}`",
        "- **监控不门禁**：单篇失败不代表代码坏了，看趋势而不是看红绿。",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
    ]
    lines.extend(row.to_markdown() for row in rows)
    lines.append("")

    missing = [row.paper for row in rows if not row.has_report]
    if missing:
        lines.append(
            "> 这些论文没有 `out/report.json`，表内数据来自 `--json` 事件流（export "
            "阶段属 M4，见 PHASE0 §3.2）：" + "、".join(f"`{p}`" for p in missing)
        )
    return "\n".join(lines) + "\n"


def parse_papers(values: list[str] | None) -> list[str]:
    """论文清单：命令行给的若干项，或 `$PAPERS` 里空格 / 逗号分隔的一串。"""
    raw = values if values else [os.environ.get("PAPERS", "")]
    papers: list[str] = []
    for item in raw:
        papers.extend(part for part in item.replace(",", " ").split() if part)
    return papers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm_summary.py",
        description="把 LLM 质量层的试跑结果汇总成 GitHub job summary 的 Markdown 表格。",
    )
    parser.add_argument(
        "--papers",
        nargs="*",
        metavar="ARXIV_ID",
        help="论文 id（可给多个）。缺省取 $PAPERS（空格或逗号分隔）。",
    )
    parser.add_argument(
        "--quality-dir",
        default=os.environ.get("QUALITY_DIR", ".llm-quality"),
        help="试跑存档目录（`<id>.events.jsonl` / `<id>.exit` / `<id>.report.json`）。",
    )
    parser.add_argument(
        "--tongtu-home",
        default=os.environ.get("TONGTU_HOME", ".tongtu-home"),
        help="论文工作目录的根（读 `<id>/out/report.json` 兜底）。",
    )
    parser.add_argument("--agent", default=os.environ.get("AGENT", ""), help="agent 运行时名")
    parser.add_argument("--model", default=os.environ.get("MODEL", ""), help="模型标识")
    parser.add_argument("--image", default=os.environ.get("IMAGE", ""), help="镜像标识")
    parser.add_argument("-o", "--output", default="-", help="输出文件，`-` 即标准输出（默认）。")
    args = parser.parse_args(argv)

    quality_dir = pathlib.Path(args.quality_dir)
    tongtu_home = pathlib.Path(args.tongtu_home)
    rows = [collect(p, quality_dir, tongtu_home) for p in parse_papers(args.papers)]
    text = render(rows, agent=args.agent, model=args.model, image=args.image)

    if args.output == "-":
        sys.stdout.write(text)
    else:
        pathlib.Path(args.output).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
