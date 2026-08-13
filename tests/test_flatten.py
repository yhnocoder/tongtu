"""flatten 阶段：主文件识别（关节①）+ latexpand 展开（架构 §3 flatten 行）。

latexpand 本机不一定装（CI 用参考镜像才保证有），所以分两路测：

* **假 latexpand**：往 `tmp_path/bin/` 放一个可执行脚本，注入 PATH——覆盖参数拼装、
  stdout 落盘、退出码分诊等整条调用路径，不依赖任何外部工具；
* **真 latexpand**：`skipif` 保护，装了才跑，验证参数确实被真程序接受。
"""

import os
import shutil
import stat
from pathlib import Path

import pytest

from tongtu.stages import flatten as fl
from tongtu.workdir import Workdir

PREAMBLE = "\\documentclass{article}\n\\usepackage{amsmath}\n"
BODY = "\\begin{document}\nHello \\emph{world}.\n\\end{document}\n"


@pytest.fixture
def paper(tmp_path) -> Workdir:
    return Workdir(path=tmp_path / "work" / "2401.01234", arxiv_id="2401.01234").create()


def write(paper: Workdir, name: str, text: str) -> Path:
    path = paper.src / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 主文件识别
# --------------------------------------------------------------------------- #


def test_single_candidate(paper):
    write(paper, "whatever.tex", PREAMBLE + BODY)
    write(paper, "macros.tex", "\\newcommand{\\R}{\\mathbb{R}}\n")

    result = fl.find_main_tex(paper)

    assert result.ok and result.main == paper.src / "whatever.tex"
    assert result.ambiguous is False
    assert [c.relpath for c in result.candidates] == ["whatever.tex"]


def test_accepts_plain_directory_too(paper):
    write(paper, "main.tex", PREAMBLE + BODY)
    assert fl.find_main_tex(paper.src).main == paper.src / "main.tex"


def test_document_env_beats_bare_documentclass(paper):
    write(paper, "aaa.tex", PREAMBLE + BODY)  # 排序上在前，但靠 \begin{document} 取胜
    write(paper, "zzz.tex", PREAMBLE)
    write(paper, "template.tex", PREAMBLE)

    result = fl.find_main_tex(paper)

    assert result.main.name == "aaa.tex"
    assert result.ambiguous is False
    assert result.candidates[0].has_document is True


def test_preferred_filename_breaks_the_tie(paper):
    write(paper, "aaa.tex", PREAMBLE + BODY)
    write(paper, "main.tex", PREAMBLE + BODY)

    result = fl.find_main_tex(paper)

    assert result.main.name == "main.tex"
    assert result.ambiguous is False
    assert any("文件名 main" in r for r in result.candidates[0].reasons)


def test_toplevel_beats_nested(paper):
    write(paper, "supplement/arxiv.tex", PREAMBLE + BODY)
    write(paper, "arxiv.tex", PREAMBLE + BODY)

    result = fl.find_main_tex(paper)

    assert result.main == paper.src / "arxiv.tex"
    assert result.ambiguous is False
    assert any("顶层" in r for r in result.candidates[0].reasons)


def test_preferred_name_outweighs_toplevel(paper):
    """名字分（main = 45）压过顶层分（10）——社区习惯比目录深度更可靠。"""
    write(paper, "sub/main.tex", PREAMBLE + BODY)
    write(paper, "arxiv.tex", PREAMBLE + BODY)

    assert fl.find_main_tex(paper).main == paper.src / "sub" / "main.tex"


def test_included_file_is_penalised(paper):
    write(paper, "arxiv.tex", PREAMBLE + "\\input{parts/body}\n" + BODY)
    write(paper, "parts/body.tex", PREAMBLE + BODY)

    result = fl.find_main_tex(paper)

    assert result.main.name == "arxiv.tex"
    assert result.ambiguous is False
    penalised = [c for c in result.candidates if c.relpath == "parts/body.tex"][0]
    assert penalised.included_by == ("arxiv.tex",)
    assert penalised.score < result.candidates[0].score


def test_bare_input_without_braces_counts(paper):
    write(paper, "arxiv.tex", PREAMBLE + "\\input parts/body.tex\n" + BODY)
    write(paper, "parts/body.tex", PREAMBLE + BODY)

    result = fl.find_main_tex(paper)
    assert result.main.name == "arxiv.tex"


def test_commented_documentclass_is_not_a_candidate(paper):
    """v2 的缺陷：`"\\documentclass" in text` 会把注释掉的那一行也算数。"""
    write(paper, "old.tex", "% \\documentclass{article}\n% 旧版导言区，已废弃\n")
    write(paper, "guide.tex", "\\begin{lstlisting}\n\\documentclass{article}\n\\end{lstlisting}\n")

    result = fl.find_main_tex(paper)

    assert result.status == fl.NO_MAIN and result.main is None
    assert sorted(result.commented_out) == ["guide.tex", "old.tex"]
    assert "注释" in result.message


def test_commented_candidate_does_not_shadow_a_real_one(paper):
    write(paper, "old.tex", "% \\documentclass{article}\n")
    write(paper, "real.tex", PREAMBLE + BODY)

    result = fl.find_main_tex(paper)

    assert result.main.name == "real.tex"
    assert result.commented_out == ("old.tex",)


def test_no_candidates_at_all(paper):
    write(paper, "macros.tex", "\\newcommand{\\R}{\\mathbb{R}}\n")

    result = fl.find_main_tex(paper)

    assert result.status == fl.NO_MAIN
    assert result.commented_out == () and result.candidates == ()


def test_missing_source_dir(tmp_path):
    result = fl.find_main_tex(tmp_path / "nope")
    assert result.status == fl.NO_MAIN


# --------------------------------------------------------------------------- #
# 关节①：真歧义
# --------------------------------------------------------------------------- #


@pytest.fixture
def tied(paper) -> Workdir:
    """两个各方面等价的候选——打分并列，只能由关节①或编译裁决。"""
    write(paper, "alpha.tex", PREAMBLE + BODY)
    write(paper, "beta.tex", PREAMBLE + BODY)
    return paper


def test_tie_without_arbiter_is_flagged_ambiguous(tied):
    result = fl.find_main_tex(tied)

    assert result.ok  # 仍然给出一个主文件，交给 baseline 编译裁决
    assert result.main.name == "alpha.tex"
    assert result.ambiguous is True and result.arbitrated is False
    assert "同分" in result.message
    assert result.to_json()["ambiguous"] is True


def test_arbiter_decides(tied):
    seen = []

    def arbiter(query: fl.MainQuery):
        seen.append(query)
        return "beta.tex"

    result = fl.find_main_tex(tied, arbiter=arbiter)

    assert result.main.name == "beta.tex"
    assert result.arbitrated is True and result.ambiguous is False
    assert [c.relpath for c in seen[0].tied] == ["alpha.tex", "beta.tex"]
    assert seen[0].root == tied.src


def test_arbiter_accepts_absolute_path(tied):
    result = fl.find_main_tex(tied, arbiter=lambda q: q.tied[-1].path)
    assert result.main.name == "beta.tex"


def test_arbiter_saying_dont_know_falls_back(tied):
    result = fl.find_main_tex(tied, arbiter=lambda q: None)
    assert result.main.name == "alpha.tex"
    assert result.ambiguous is True and result.arbitrated is False
    assert result.warnings == ()


def test_arbiter_answer_outside_candidates_is_ignored(tied):
    result = fl.find_main_tex(tied, arbiter=lambda q: "gamma.tex")
    assert result.main.name == "alpha.tex"
    assert result.ambiguous is True
    assert result.warnings and "不在候选里" in result.warnings[0]


def test_arbiter_not_called_when_unambiguous(paper):
    write(paper, "main.tex", PREAMBLE + BODY)

    def arbiter(query):  # pragma: no cover - 不该被调用
        raise AssertionError("没有歧义时不该拉起关节①")

    assert fl.find_main_tex(paper, arbiter=arbiter).main.name == "main.tex"


# --------------------------------------------------------------------------- #
# latexpand 调用（假 latexpand）
# --------------------------------------------------------------------------- #

FAKE = r'''#!/usr/bin/env python3
"""最小 latexpand 替身：拼接 \input 并把结果回显到 stdout。"""
import re, sys
from pathlib import Path

argv = sys.argv[1:]
main, bbl = None, None
i = 0
while i < len(argv):
    if argv[i] == "--expand-bbl":
        bbl = argv[i + 1]
        i += 2
        continue
    if argv[i].startswith("--"):
        i += 1
        continue
    main = argv[i]
    i += 1

text = Path(main).read_text(encoding="utf-8")
text = re.sub(
    r"\\input\{([^}]*)\}",
    lambda m: Path(m.group(1) + ("" if m.group(1).endswith(".tex") else ".tex")).read_text(
        encoding="utf-8"
    ),
    text,
)
if bbl:
    text = text.replace("\\bibliography{refs}", Path(bbl).read_text(encoding="utf-8"))
sys.stdout.write("%% fake-latexpand " + " ".join(argv) + "\n" + text)
'''

FAKE_FAIL = """#!/usr/bin/env python3
import sys
sys.stderr.write("latexpand: can't find file 'missing.tex'\\n")
sys.exit(1)
"""

FAKE_EMPTY = """#!/usr/bin/env python3
"""


@pytest.fixture
def fake_bin(tmp_path, monkeypatch) -> Path:
    """把三个假 latexpand 放进 PATH 最前面（真的那个即使装了也不参与）。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (
        ("latexpand", FAKE),
        ("latexpand-fail", FAKE_FAIL),
        ("latexpand-empty", FAKE_EMPTY),
    ):
        script = bindir / name
        script.write_text(body, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return bindir


def test_flatten_expands_inputs(paper, fake_bin):
    write(paper, "main.tex", PREAMBLE + "\\input{parts/body}\n" + BODY)
    write(paper, "parts/body.tex", "% 一条注释\nIncluded body.\n")

    result = fl.flatten(paper, "main.tex")

    assert result.ok and result.status == fl.OK
    assert result.flat == paper.build / "flat.tex"
    flat = result.flat.read_text(encoding="utf-8")
    assert "Included body." in flat
    assert "% 一条注释" in flat  # --keep-comments：注释必须留着（M1 的 mask 无损处理注释）
    assert "--keep-comments" in flat.splitlines()[0]
    assert result.chars == len(flat)
    assert result.bbl_expanded is False
    assert "--expand-bbl" not in result.command


def test_flatten_accepts_absolute_main(paper, fake_bin):
    main = write(paper, "main.tex", PREAMBLE + BODY)
    assert fl.flatten(paper, main).ok


def test_flatten_expands_bbl_when_present(paper, fake_bin):
    write(paper, "main.tex", PREAMBLE + "\\bibliography{refs}\n" + BODY)
    write(paper, "main.bbl", "\\begin{thebibliography}{1}\n\\end{thebibliography}\n")

    result = fl.flatten(paper, "main.tex")

    assert result.bbl_expanded is True
    assert list(result.command[-3:]) == ["--expand-bbl", "main.bbl", "main.tex"]
    assert "thebibliography" in result.flat.read_text(encoding="utf-8")


def test_flatten_runs_in_the_main_files_directory(paper, fake_bin):
    """cwd = 主文件所在目录：`\\input` 的相对路径才解析得对。"""
    write(paper, "sub/main.tex", PREAMBLE + "\\input{body}\n" + BODY)
    write(paper, "sub/body.tex", "Nested body.\n")

    result = fl.flatten(paper, "sub/main.tex")

    assert result.ok
    assert "Nested body." in result.flat.read_text(encoding="utf-8")
    assert result.command[-1] == "main.tex"  # 传的是文件名而非路径


def test_flatten_missing_tool_is_structured(paper, tmp_path, monkeypatch):
    write(paper, "main.tex", PREAMBLE + BODY)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    result = fl.flatten(paper, "main.tex")

    assert result.status == fl.MISSING_TOOL and result.ok is False
    assert "latexpand" in result.message and "doctor" in result.message
    assert not (paper.build / "flat.tex").exists()


def test_flatten_reports_nonzero_exit(paper, fake_bin):
    write(paper, "main.tex", PREAMBLE + BODY)

    result = fl.flatten(paper, "main.tex", latexpand="latexpand-fail")

    assert result.status == fl.FAILED
    assert "退出码 1" in result.message
    assert "missing.tex" in result.stderr
    assert not (paper.build / "flat.tex").exists()


def test_flatten_reports_empty_output(paper, fake_bin):
    write(paper, "main.tex", PREAMBLE + BODY)
    result = fl.flatten(paper, "main.tex", latexpand="latexpand-empty")
    assert result.status == fl.EMPTY


def test_flatten_missing_main(paper, fake_bin):
    result = fl.flatten(paper, "nope.tex")
    assert result.status == fl.MISSING_MAIN
    assert "不存在" in result.message


def test_flatten_to_json(paper, fake_bin):
    write(paper, "main.tex", PREAMBLE + BODY)
    data = fl.flatten(paper, "main.tex").to_json()
    assert data["status"] == fl.OK and data["main"] == "main.tex"
    assert data["command"][1] == "--keep-comments"


# --------------------------------------------------------------------------- #
# 真 latexpand（装了才跑）
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("latexpand") is None, reason="本机没有 latexpand")
def test_real_latexpand(paper):
    write(paper, "main.tex", PREAMBLE + "\\input{parts/body}\n" + BODY)
    write(paper, "parts/body.tex", "% 一条注释\nIncluded body.\n")

    result = fl.flatten(paper, "main.tex")

    assert result.ok, result.message
    flat = result.flat.read_text(encoding="utf-8")
    assert "Included body." in flat
    assert "% 一条注释" in flat
