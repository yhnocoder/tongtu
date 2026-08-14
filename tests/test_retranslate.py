"""块级增量：翻译记忆在流水线里的行为，与 `tongtu retranslate`（架构 §4/§6、决策 3）。

`tests/test_memory.py` 覆盖的是存取与命中语义（纯函数层），这里覆盖的是**整条流水线上**的
账：首跑全 miss、记忆写回产物形态的 `chunks.json`、`build/` 删掉还能从 `out/chunks.json`
恢复、以及三种失效范围各自只重翻该重翻的那些块。

判据一律是**关节⑤被拉起了几次**（`PROMPT_TAIL` 在提示词里出现的次数），不是「跑通了」：
缓存与 retranslate 的全部价值就是少调几次模型。假 latexpand / 假 latexmk 来自 conftest 的
`tools` 夹具，于是除这两个外部程序外全部代码路径都是真的。
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from tongtu import memory as mem
from tongtu.agent.mock import MockAgent
from tongtu.pipeline import retranslate, run_pipeline
from tongtu.schema_check import load_schema, validate_schema
from tongtu.stages.translate import PROMPT_TAIL
from tongtu.workdir import Workdir

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / "tests" / "fixtures" / "papers" / "article"


class Counting(MockAgent):
    """恒等 MockAgent + 关节⑤的调用计数（survey 的那次通读不算在内）。"""

    @property
    def translate_calls(self) -> int:
        return sum(1 for call in self.completions if PROMPT_TAIL in call.prompt)


def run(workdir: Path, agent: Counting, **kwargs):
    return run_pipeline(PAPER, workdir=workdir, agent=agent, out=io.StringIO(), **kwargs)


def again(workdir: Path, agent: Counting, **kwargs):
    """再跑一次，但先抹掉 translate 的 manifest——否则整个阶段被 manifest 跳过，
    根本轮不到块级缓存说话（两层增量各测各的）。"""
    (workdir / "build" / "manifests" / "translate.json").unlink()
    return run(workdir, agent, **kwargs)


def memory_file(workdir: Path) -> dict:
    return json.loads((workdir / "build" / "zh-chunks" / "chunks.json").read_text("utf-8"))


def redo(workdir: Path, agent: Counting, **kwargs):
    return retranslate("article", workdir=workdir, agent=agent, out=io.StringIO(), **kwargs)


# --------------------------------------------------------------------------- #
# 记忆
# --------------------------------------------------------------------------- #


def test_first_run_is_all_miss_and_writes_the_memory(tools, tmp_path):
    workdir = tmp_path / "work"
    agent = Counting()

    result = run(workdir, agent)

    assert result.exit_code == 0
    assert result.cache_hits == 0 and result.cache_misses == result.chunks_total
    assert agent.translate_calls == result.chunks_total
    record = memory_file(workdir)
    assert validate_schema(record, load_schema("chunks")) == []
    assert len(record["chunks"]) == result.chunks_total
    assert all(len(c["cache_key"]) == 64 and c["src"] for c in record["chunks"])


def test_a_repeated_translate_stage_hits_the_memory(tools, tmp_path):
    """阶段 manifest 没了也不该重新花钱：块级缓存是第二道闸（架构 §4）。"""
    workdir = tmp_path / "work"
    agent = Counting()
    first = run(workdir, agent)
    spent = agent.translate_calls

    second = again(workdir, agent)

    assert second.exit_code == 0
    assert {s.stage: s.status for s in second.stages}["translate"] == "ok"
    assert second.cache_hits == first.chunks_total and second.cache_misses == 0
    assert agent.translate_calls == spent, "全部命中，一次也不该拉起关节⑤"


def test_memory_survives_deleting_the_whole_build_directory(tools, tmp_path):
    """`build/` 可整体删除（架构 §5）：权威记忆在 `out/chunks.json`，重建时全量命中。"""
    workdir = tmp_path / "work"
    agent = Counting()
    first = run(workdir, agent)
    spent = agent.translate_calls
    out_path, _ = mem.memory_paths(Workdir(path=workdir, arxiv_id="article"))
    assert out_path.is_file(), "export 该把权威记忆写进 out/"

    shutil.rmtree(workdir / "build")
    rebuilt = run(workdir, agent)

    assert rebuilt.exit_code == 0
    assert {s.status for s in rebuilt.stages} <= {"ok", "skipped"}, "build 没了就全得重算"
    assert rebuilt.cache_hits == first.chunks_total and rebuilt.cache_misses == 0
    assert agent.translate_calls == spent, "昂贵成果一条都不该丢"
    assert memory_file(workdir)["chunks"], "重建之后 build 侧的记忆也要写回来"


def test_a_fallback_chunk_is_not_remembered(tools, tmp_path):
    """回退块存的是原文——下一轮必须重新试，而不是把失败冻结成结论。"""

    class Stubborn(Counting):
        def complete(self, prompt: str, text: str, model=None) -> str:
            out = super().complete(prompt, text, model)
            return "彻底不守规矩的译文" if PROMPT_TAIL in prompt else out

    workdir = tmp_path / "work"
    agent = Stubborn()
    result = run(workdir, agent, max_retries=0)

    assert result.exit_code == 0 and result.fallback_chunks == result.chunks_total
    record = memory_file(workdir)
    assert {c["status"] for c in record["chunks"]} == {"fallback"}
    assert mem.load_file(workdir / "build" / "zh-chunks" / "chunks.json") == {}


# --------------------------------------------------------------------------- #
# retranslate
# --------------------------------------------------------------------------- #


def test_retranslate_chunks_only_redoes_the_named_ones(tools, tmp_path):
    workdir = tmp_path / "work"
    agent = Counting()
    first = run(workdir, agent)
    spent = agent.translate_calls
    ids = [c["id"] for c in memory_file(workdir)["chunks"]]

    result = redo(workdir, agent, chunks=[ids[1]])

    assert result.exit_code == 0
    assert agent.translate_calls == spent + 1, "只重翻点名的那一块"
    assert result.cache_hits == first.chunks_total - 1 and result.cache_misses == 1
    statuses = {s.stage: s.status for s in result.stages}
    assert statuses["mask"] == "cached" and statuses["chunk"] == "cached", "上游只装载"
    assert statuses["translate"] == "ok"
    # 恒等 mock 重翻出来的译文与原来一模一样 → 下游 manifest 判定不必重编译
    assert statuses["compile"] == "cached"
    assert len(memory_file(workdir)["chunks"]) == first.chunks_total, (
        "重翻一块也要写回**整份**记忆，否则没被重翻的块下一轮就没得命中了"
    )


def test_retranslate_term_only_redoes_the_chunks_that_hit_it(tools, tmp_path):
    """编辑某术语条目 → 命中该术语的块（架构 §4 返工触发表第 3 行）。"""
    workdir = tmp_path / "work"
    agent = Counting()
    run(workdir, agent)
    spent = agent.translate_calls
    record = memory_file(workdir)
    hits = [c["id"] for c in record["chunks"] if "tolerance" in c["src"].lower()]
    assert 0 < len(hits) < len(record["chunks"]), "夹具指望这个词只出现在一部分块里"

    result = redo(workdir, agent, term="Tolerance")

    assert result.exit_code == 0
    assert agent.translate_calls == spent + len(hits)
    assert result.cache_misses == len(hits)


def test_retranslate_all_redoes_everything(tools, tmp_path):
    """改文风 / 换模型时的显式全量重翻（架构 §4 返工触发表第 4 行）。"""
    workdir = tmp_path / "work"
    agent = Counting()
    first = run(workdir, agent)
    spent = agent.translate_calls

    result = redo(workdir, agent, all_chunks=True)

    assert result.exit_code == 0
    assert agent.translate_calls == spent + first.chunks_total
    assert result.cache_hits == 0 and result.cache_misses == first.chunks_total


def test_retranslate_also_forgets_the_authoritative_memory(tools, tmp_path):
    """光删内存里的那份不算数：`out/chunks.json` 不抹，下一次 run 会把它原样装回来。

    M4 起 export 会在重翻之后把权威记忆**重新写回** `out/`，故事后去看那份文件必然是齐
    的——判据因此落在过程上：失效那一步确实抹到了 `out/chunks.json`（进度行如实记账），
    且失效的那一块确实重新翻了一遍（关节⑤被拉起恰好一次）。
    """
    workdir = tmp_path / "work"
    agent = Counting()
    run(workdir, agent)
    spent = agent.translate_calls
    out_path, _ = mem.memory_paths(Workdir(path=workdir, arxiv_id="article"))
    ids = [c["id"] for c in memory_file(workdir)["chunks"]]
    assert mem.chunk_ids(mem.read_chunks(out_path)) == tuple(ids)

    stream = io.StringIO()
    result = retranslate(
        "article", workdir=workdir, agent=agent, out=stream, chunks=[ids[0]]
    )

    assert result.exit_code == 0
    assert f"来自 {out_path.name}" in stream.getvalue(), "权威记忆没被抹到"
    assert agent.translate_calls == spent + 1
    assert mem.chunk_ids(mem.read_chunks(out_path)) == tuple(ids), "重翻后权威记忆写回齐了"


def test_retranslate_rejects_an_unknown_chunk_id(tools, tmp_path):
    workdir = tmp_path / "work"
    agent = Counting()
    run(workdir, agent)

    with pytest.raises(ValueError) as exc:
        redo(workdir, agent, chunks=["c999"])

    assert "c999" in str(exc.value)


def test_retranslate_without_any_memory_fails_structurally(tools, tmp_path):
    empty = tmp_path / "work"
    empty.mkdir()

    result = retranslate("article", workdir=empty, all_chunks=True, out=io.StringIO())

    assert result.exit_code == 1 and result.status == "failed"
    assert "tongtu run" in result.message


def test_retranslate_on_a_missing_workdir_says_so(tmp_path):
    result = retranslate(
        "article", workdir=tmp_path / "没有这个目录", all_chunks=True, out=io.StringIO()
    )

    assert result.exit_code == 1 and "工作目录不存在" in result.message


def test_retranslate_term_that_hits_nothing_is_a_structured_noop(tools, tmp_path):
    workdir = tmp_path / "work"
    agent = Counting()
    run(workdir, agent)
    spent = agent.translate_calls

    result = redo(workdir, agent, term="根本没人提过的词")

    assert result.exit_code == 1 and "无需重翻" in result.message
    assert agent.translate_calls == spent, "什么也没失效，就不该重翻"


def test_retranslate_emits_a_valid_event_stream(tools, tmp_path):
    workdir = tmp_path / "work"
    agent = Counting()
    run(workdir, agent)
    stream = io.StringIO()

    result = retranslate(
        "article", workdir=workdir, agent=agent, all_chunks=True, json_events=True, out=stream
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    schema = load_schema("events")
    assert events and all(validate_schema(e, schema) == [] for e in events)
    assert events[-1]["event"] == "result" and events[-1]["exit_code"] == result.exit_code
    # 阶段图对所有论文不变（架构 §3）：retranslate 也把每个阶段的账走一遍，上游记 cached
    ends = {e["stage"]: e["status"] for e in events if e["event"] == "stage_end"}
    assert ends["mask"] == "cached" and ends["survey"] == "cached"
    assert ends["translate"] == "ok"
    assert {e["status"] for e in events if e["event"] == "chunk_progress"} == {
        "started", "translated",
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_retranslate_roundtrip(tools, tmp_path, capsys):
    from tongtu.cli import main

    workdir = tmp_path / "work"
    assert run(workdir, Counting()).exit_code == 0
    capsys.readouterr()

    code = main(["retranslate", "article", "--workdir", str(workdir), "--all", "--json"])

    assert code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[-1]["event"] == "result" and lines[-1]["exit_code"] == 0


def test_cli_retranslate_reports_a_bad_chunk_id(tools, tmp_path, capsys):
    from tongtu.cli import main

    workdir = tmp_path / "work"
    assert run(workdir, Counting()).exit_code == 0

    assert main(["retranslate", "article", "--workdir", str(workdir), "--chunks", "c999"]) == 2
    assert "c999" in capsys.readouterr().err
    assert main(["retranslate", "article", "--workdir", str(workdir), "--chunks", " ,"]) == 2
