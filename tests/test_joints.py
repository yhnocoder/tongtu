"""六个 agent 关节的接线（架构 §3 的介入点、§9 两原语、决策 1）。

关节的正确性判据只有三条，本文件逐条钉死：

1. **该问的时候才问**（未知环境、主文件真歧义、编译失败……），问的时候现场信息与
   prompt 资产都到位；
2. **裁决权不移交**——`session` 说 done、`complete` 说「判为 prose」，都不是结论：
   编译回环、往返自检、validate 才是；agent 不可用 / 乱答一律保守降级，不损坏；
3. **每次拉起都记账**（`PipelineResult.interventions`，形状对齐 `report.schema.json` 的
   `agent_interventions`）——促升规则要靠这份统计判断什么该固化成确定性代码。

用可编程的 `FakeAgent` 而不是 MockAgent：MockAgent 是恒等的，问什么答什么，恰恰测不出
「答案真的被采纳了」还是「本来就那样」。
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from tongtu.agent import JOINTS, SessionOutcome
from tongtu.agent.mock import CompleteCall, MockAgent
from tongtu.compiler import CompileRunResult
from tongtu.pipeline import Events, Pipeline, run_pipeline
from tongtu.schema_check import load_schema, validate_schema
from tongtu.stages.translate import PROMPT_TAIL
from tongtu.workdir import Workdir

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / "tests" / "fixtures" / "papers" / "article"

#: 假编译器的「炸弹」：出现在 tex 里就编不过（同 tests/test_compile.py 的手法）。
BOMB = "BADSEGMENT"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@dataclass
class FakeAgent:
    """可编程的 agent 运行时：两原语的行为由调用方给的函数决定，现场全部记下来。"""

    answer: Callable[[str, str], str] = lambda prompt, text: ""
    on_session: Callable[[object], None] | None = None
    model: str = "fake-model"
    prompts: list[tuple[str, str]] = field(default_factory=list)
    sessions: list[object] = field(default_factory=list)

    def complete(self, prompt: str, text: str, model=None) -> str:
        self.prompts.append((prompt, text))
        return self.answer(prompt, text)

    def as_session_fn(self):
        def run(request):
            self.sessions.append(request)
            if self.on_session is not None:
                self.on_session(request)
            # done=True 是**自述**，按架构 §9 不构成任何「修好了」的断言
            return SessionOutcome(
                done=True,
                transcript_path=Path("logs/fake-session.log"),
                message="我改完了（自述无效力）",
            )

        return run


def pipeline_for(tmp_path, agent=None, **kwargs) -> Pipeline:
    workdir = Workdir(path=tmp_path / "work", arxiv_id="2401.00001").create()
    return Pipeline(
        workdir,
        agent=agent,
        events=Events(io.StringIO(), json_mode=True, arxiv_id="2401.00001"),
        **kwargs,
    )


def seed_src(pipeline: Pipeline, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = pipeline.workdir.src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def seed_flat(pipeline: Pipeline, text: str) -> None:
    (pipeline.workdir.build / "flat.tex").write_text(text, encoding="utf-8")


def compiler(predicate=lambda text: BOMB in text, *, error="! Undefined control sequence.", line=999):
    """假编译器：`predicate(tex)` 为真即编不过；成功时把 tex 写进「PDF」。"""
    calls: list[str] = []

    def run(tex: Path, build_dir: Path) -> CompileRunResult:
        text = tex.read_text(encoding="utf-8")
        calls.append(text)
        pdf = build_dir / f"{tex.stem}.pdf"
        if predicate(text):
            if pdf.exists():
                pdf.unlink()
            return CompileRunResult(
                ok=False, log=f"{error}\nl.{line} \\foo\n", returncode=12, engine="xelatex"
            )
        pdf.write_text(text, encoding="utf-8")
        return CompileRunResult(
            ok=True, pdf=pdf, log="Output written on zh.pdf\n", returncode=0, engine="xelatex"
        )

    run.calls = calls  # type: ignore[attr-defined]
    return run


def only(interventions, joint: str):
    return [i for i in interventions if i.joint == joint]


# --------------------------------------------------------------------------- #
# ① 主文件（flatten，complete，提示词内联）
# --------------------------------------------------------------------------- #

TWO_MAINS = {
    "alpha.tex": "\\documentclass{article}\n\\begin{document}\nAlpha.\n\\end{document}\n",
    "beta.tex": "\\documentclass{article}\n\\begin{document}\nBeta.\n\\end{document}\n",
}


def test_main_file_joint_is_asked_only_on_a_real_tie(tools, tmp_path):
    agent = FakeAgent(answer=lambda prompt, text: "beta.tex")
    pipeline = pipeline_for(tmp_path, agent=agent)
    seed_src(pipeline, TWO_MAINS)

    outcome = pipeline.run_one("flatten")

    assert outcome.status == "ok"
    prompt = agent.prompts[0][0]
    assert "alpha.tex" in prompt and "beta.tex" in prompt, "候选与打分明细要到达关节①"
    assert "\\documentclass" in prompt
    assert outcome.detail["main"]["main"] == "beta.tex"
    assert outcome.detail["main"]["arbitrated"] is True
    assert "Beta." in (pipeline.workdir.build / "flat.tex").read_text(encoding="utf-8")

    entry = only(pipeline.interventions, "main_file")[0]
    assert entry.primitive == "complete" and entry.outcome == "resolved"
    assert entry.stage == "flatten" and "beta.tex" in entry.action
    assert entry.model_id == "fake-model" and entry.prompt_version


def test_a_single_candidate_never_reaches_the_joint(tools, tmp_path):
    """唯一候选不是判断题——不许为它花一次调用。"""
    agent = FakeAgent(answer=lambda prompt, text: "alpha.tex")
    pipeline = pipeline_for(tmp_path, agent=agent)
    seed_src(pipeline, {"alpha.tex": TWO_MAINS["alpha.tex"]})

    assert pipeline.run_one("flatten").status == "ok"

    assert agent.prompts == [] and pipeline.interventions == []


def test_an_unusable_answer_falls_back_to_the_score(tools, tmp_path):
    """乱答 / 不知道 → 按分数取第一个，由 baseline 编译裁决（架构 §3 flatten 行）。"""
    agent = FakeAgent(answer=lambda prompt, text: "gamma.tex（我猜的）")
    pipeline = pipeline_for(tmp_path, agent=agent)
    seed_src(pipeline, TWO_MAINS)

    outcome = pipeline.run_one("flatten")

    assert outcome.status == "ok"
    assert outcome.detail["main"]["ambiguous"] is True
    assert outcome.detail["main"]["arbitrated"] is False
    assert only(pipeline.interventions, "main_file")[0].outcome == "unresolved"


# --------------------------------------------------------------------------- #
# ③ 环境分类（mask，complete + skill/classify.md）
# --------------------------------------------------------------------------- #

UNKNOWN_ENV = """\
\\documentclass{article}
\\begin{document}
\\section{S}
An ordinary paragraph.

\\begin{sidebar}
A boxed remark that is really just prose.
\\end{sidebar}

Another ordinary paragraph.
\\end{document}
"""


def masked_stream(pipeline: Pipeline) -> str:
    return (pipeline.workdir.build / "masked.tex").read_text(encoding="utf-8")


def environments(pipeline: Pipeline) -> dict:
    data = json.loads((pipeline.workdir.build / "blocks.json").read_text(encoding="utf-8"))
    return {e["name"]: e for e in data["environments"]}


def test_env_classify_joint_gets_the_rules_and_the_sample(tools, tmp_path):
    agent = FakeAgent(answer=lambda prompt, text: "prose")
    pipeline = pipeline_for(tmp_path, agent=agent)
    seed_flat(pipeline, UNKNOWN_ENV)

    outcome = pipeline.run_one("mask")

    assert outcome.status == "ok" and outcome.detail["roundtrip_ok"] is True
    prompt, sample = agent.prompts[0]
    assert "环境分类" in prompt, "skill/classify.md 的规则要到达关节③"
    assert "sidebar" in prompt and "全文出现次数：1" in prompt
    assert "\\begin{sidebar}" in sample, "首次出现处的源码片段是判断的依据"
    assert len(agent.prompts) == 1, "一个未知环境只问一次（article / document 是已知的）"


def test_prose_verdict_keeps_the_environment_in_the_stream(tools, tmp_path):
    """判成散文 → 环境体留在翻译流里（`\\begin`/`\\end` 包裹照旧），并记 decided_by=agent。"""
    agent = FakeAgent(answer=lambda prompt, text: "prose\n")
    pipeline = pipeline_for(tmp_path, agent=agent)
    seed_flat(pipeline, UNKNOWN_ENV)

    assert pipeline.run_one("mask").status == "ok"

    assert "A boxed remark that is really just prose." in masked_stream(pipeline)
    info = environments(pipeline)["sidebar"]
    assert info["classification"] == "prose" and info["decided_by"] == "agent"
    entry = only(pipeline.interventions, "env_classify")[0]
    assert entry.outcome == "resolved" and entry.promotable is True
    assert "sidebar" in entry.trigger and entry.action == "判为 prose"


def test_heavy_verdict_masks_the_block(tools, tmp_path):
    agent = FakeAgent(answer=lambda prompt, text: "heavy")
    pipeline = pipeline_for(tmp_path, agent=agent)
    seed_flat(pipeline, UNKNOWN_ENV)

    assert pipeline.run_one("mask").status == "ok"

    assert "A boxed remark" not in masked_stream(pipeline)
    assert environments(pipeline)["sidebar"]["decided_by"] == "agent"
    assert only(pipeline.interventions, "env_classify")[0].action == "判为 heavy"


@pytest.mark.parametrize("answer", ["unknown", "我也说不好", "", "prose 还是 heavy？"])
def test_an_unusable_verdict_falls_back_to_conservative_masking(answer, tools, tmp_path):
    """「不知道」是有用的答案：保守整块掩码，只降覆盖率、绝不损坏（架构 §3.1 第 2 条）。"""
    agent = FakeAgent(answer=lambda prompt, text: answer)
    pipeline = pipeline_for(tmp_path, agent=agent)
    seed_flat(pipeline, UNKNOWN_ENV)

    assert pipeline.run_one("mask").status == "ok"

    assert "A boxed remark" not in masked_stream(pipeline)
    info = environments(pipeline)["sidebar"]
    assert info["decided_by"] == "default" and info["category"] == "unknown"
    entry = only(pipeline.interventions, "env_classify")[0]
    assert entry.outcome == "unresolved" and entry.promotable is None


def test_a_joint_that_raises_does_not_break_the_stage(tools, tmp_path):
    def boom(prompt, text):
        raise RuntimeError("运行时挂了")

    pipeline = pipeline_for(tmp_path, agent=FakeAgent(answer=boom))
    seed_flat(pipeline, UNKNOWN_ENV)

    outcome = pipeline.run_one("mask")

    assert outcome.status == "ok" and outcome.detail["roundtrip_ok"] is True
    assert environments(pipeline)["sidebar"]["decided_by"] == "default"
    assert only(pipeline.interventions, "env_classify")[0].outcome == "unresolved"


def test_known_environments_never_reach_the_joint(tools, tmp_path):
    """分类表与文档自带声明都是**确定性知识**，轮不到 agent（也就不该花钱）。"""
    agent = FakeAgent(answer=lambda prompt, text: "prose")
    pipeline = pipeline_for(tmp_path, agent=agent)
    seed_flat(
        pipeline,
        "\\documentclass{article}\n\\newtheorem{claimlike}{Claim}\n\\begin{document}\n"
        "\\begin{equation}x\\end{equation}\n\n\\begin{claimlike}Text.\\end{claimlike}\n"
        "\\end{document}\n",
    )

    assert pipeline.run_one("mask").status == "ok"

    assert agent.prompts == []
    assert only(pipeline.interventions, "env_classify") == []


# --------------------------------------------------------------------------- #
# ② 构建环境（baseline，session）
# --------------------------------------------------------------------------- #

FLAT = "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n"


def test_build_env_joint_gets_the_scene_and_the_verdict_is_the_recompile(tools, tmp_path):
    """会话改好了 → 由**重新编译**说了算，不是由 `done=True` 说了算（架构 §9）。"""
    state = {"broken": True}

    def fixed(request):
        state["broken"] = False  # 会话「修好了环境」

    agent = FakeAgent(on_session=fixed)
    pipeline = pipeline_for(
        tmp_path, agent=agent, compiler=compiler(lambda text: state["broken"])
    )
    seed_flat(pipeline, FLAT)

    outcome = pipeline.run_one("baseline")

    assert outcome.status == "ok" and outcome.detail["passes"] == 2
    request = agent.sessions[0]
    assert request.joint == "build_env"
    assert "编译目录" in request.prompt and "第一个错误" in request.prompt
    assert request.workdir is pipeline.workdir, "会话的可写范围是工作目录"

    entry = only(pipeline.interventions, "build_env")[0]
    assert entry.primitive == "session" and entry.outcome == "resolved"
    assert entry.transcript_path.endswith("fake-session.log")


def test_a_session_that_changes_nothing_still_fails_the_stage(tools, tmp_path):
    """agent 自述「我改完了」没有效力——编不过就是编不过，流水线到此终止。"""
    agent = FakeAgent()  # on_session=None：什么也不改
    pipeline = pipeline_for(tmp_path, agent=agent, compiler=compiler(lambda text: True))
    seed_flat(pipeline, FLAT)

    outcome = pipeline.run_one("baseline")

    assert outcome.status == "failed" and "env_failed" in json.dumps(outcome.detail)
    assert len(agent.sessions) == 1, "关节②只拉起一次，不无限重试"
    assert only(pipeline.interventions, "build_env")[0].outcome == "unresolved"


def test_baseline_that_passes_never_wakes_the_joint(tools, tmp_path):
    agent = FakeAgent()
    pipeline = pipeline_for(tmp_path, agent=agent, compiler=compiler(lambda text: False))
    seed_flat(pipeline, FLAT)

    assert pipeline.run_one("baseline").status == "ok"

    assert agent.sessions == [] and pipeline.interventions == []


# --------------------------------------------------------------------------- #
# ⑤ 坏段重译 / ⑥ 适配与修复（compile）——整条流水线上验
# --------------------------------------------------------------------------- #


class BombAgent(MockAgent):
    """第一块的译文里塞一个会让假编译器炸掉的记号；被要求重译时给干净译文。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.bombed = False

    def complete(self, prompt: str, text: str, model=None) -> str:
        self.completions.append(CompleteCall(prompt=prompt, text=text, model=model))
        if PROMPT_TAIL not in prompt:
            return text  # survey：恒等输出 → 走确定性骨架
        if "编译失败" in prompt:
            return text  # 关节⑤复用：重译一次，这次给干净的
        if not self.bombed:
            self.bombed = True
            return f"{text} {BOMB}"
        return text


def test_compile_retranslates_a_bad_segment_and_records_it(tools, tmp_path):
    """坏段重译一次救活（架构 §3 compile 行）：判据是**重新编译**，不是重译本身。"""
    agent = BombAgent()
    result = run_pipeline(
        PAPER,
        workdir=tmp_path / "work",
        agent=agent,
        compiler=compiler(),
        out=io.StringIO(),
    )

    assert result.exit_code == 0 and result.status == "ok"
    detail = result.stage("compile").detail
    assert detail["retranslated"], "坏段该被重译救活"
    assert detail["fallbacks"] == [], "救活了就不该再回退原文"
    assert BOMB not in (tmp_path / "work" / "build" / "zh" / "zh.tex").read_text("utf-8")

    entry = only(result.interventions, "translate")[0]
    assert entry.stage == "compile" and entry.primitive == "complete"
    assert entry.outcome == "resolved" and "坏段" in entry.trigger
    prompt = next(c.prompt for c in agent.completions if "编译失败" in c.prompt)
    assert "Undefined control sequence" in prompt, "编译器给的线索要到达关节⑤"
    assert "占位符" in prompt, "重译仍走 skill/translate.md 的规则"


def test_a_bad_segment_that_stays_broken_falls_back(tools, tmp_path):
    """重译不成 → 回退原文段落，照样出 PDF（退出码 0），干预记 fallback。"""

    class Stubborn(BombAgent):
        def complete(self, prompt: str, text: str, model=None) -> str:
            if PROMPT_TAIL in prompt and "编译失败" in prompt:
                self.completions.append(CompleteCall(prompt=prompt, text=text, model=model))
                return f"{text} {BOMB}"  # 重译还是带炸弹
            return super().complete(prompt, text, model)

    result = run_pipeline(
        PAPER,
        workdir=tmp_path / "work",
        agent=Stubborn(),
        compiler=compiler(),
        out=io.StringIO(),
    )

    assert result.exit_code == 0 and result.status == "ok_with_fallback"
    assert result.stage("compile").detail["fallbacks"]
    assert only(result.interventions, "translate")[0].outcome == "fallback"


def test_a_global_problem_goes_to_the_fixup_joint(tools, tmp_path):
    """全局问题（前导区错误）→ 关节⑥，改完**原地重编译**裁决（决策 13）。"""
    state = {"broken": True}

    def patch(request):
        state["broken"] = False

    agent = FakeAgent(answer=lambda prompt, text: text, on_session=patch)
    result = run_pipeline(
        PAPER,
        workdir=tmp_path / "work",
        agent=agent,
        compiler=compiler(
            lambda text: state["broken"] and "xeCJK" in text,
            error="! LaTeX Error: File `nope.sty' not found.",
            line=3,
        ),
        out=io.StringIO(),
    )

    assert result.exit_code == 0
    assert result.stage("compile").detail["session_used"] == 1
    joints = {r.joint for r in result.interventions}
    assert "fixup" in joints
    entry = only(result.interventions, "fixup")[0]
    assert entry.primitive == "session" and entry.outcome == "resolved"
    assert agent.sessions[-1].joint == "fixup"


def test_a_fixup_session_that_fixes_nothing_ends_in_failure(tools, tmp_path):
    agent = FakeAgent(answer=lambda prompt, text: text)  # 会话不改任何东西
    result = run_pipeline(
        PAPER,
        workdir=tmp_path / "work",
        agent=agent,
        compiler=compiler(
            lambda text: "xeCJK" in text,
            error="! LaTeX Error: File `nope.sty' not found.",
            line=3,
        ),
        out=io.StringIO(),
    )

    assert result.exit_code == 1 and result.status == "failed"
    assert only(result.interventions, "fixup")[0].outcome == "unresolved"


# --------------------------------------------------------------------------- #
# 干预记录的形状
# --------------------------------------------------------------------------- #


def test_interventions_match_the_report_contract(tools, tmp_path):
    """记录形状即 `report.schema.json` 的 `agent_interventions[]`（落盘属 M4）。"""
    schema = load_schema("report")
    item = schema["properties"]["agent_interventions"]["items"]
    agent = FakeAgent(answer=lambda prompt, text: "prose")
    pipeline = pipeline_for(tmp_path, agent=agent)
    seed_flat(pipeline, UNKNOWN_ENV)
    assert pipeline.run_one("mask").status == "ok"

    entries = [i.to_json() for i in pipeline.interventions]

    assert entries
    for entry in entries:
        assert validate_schema(entry, item, schema) == [], entry
        assert entry["joint"] in JOINTS
