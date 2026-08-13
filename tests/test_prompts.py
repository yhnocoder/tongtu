"""prompt 资产（`skill/`）的装载与版本常量（PHASE0 §3.4、架构决策 1）。

这一层不测「prompt 写得好不好」（那是 LLM 层的事，架构 §12 层 3），只测**机械**部分：
资产找得到、头注释剥得掉、名字挡得住路径穿越、版本常量单一来源、translate 的提示词确实
由 `skill/translate.md` 组装而不是硬编码在代码里。
"""

import pytest

from tongtu import prompts
from tongtu.stages import translate as tr

#: 本会话交付的三份资产（survey 由并行会话落地，此处不硬性要求）。
CORE = (prompts.TRANSLATE, prompts.REPAIR, prompts.CLASSIFY)


@pytest.fixture(autouse=True)
def _isolate_cache():
    prompts.cache_clear()
    yield
    prompts.cache_clear()


# --------------------------------------------------------------------------- #
# 定位与装载
# --------------------------------------------------------------------------- #


def test_repo_skill_dir_holds_the_joint_assets():
    found = prompts.available()

    assert set(CORE) <= set(found), f"缺 prompt 资产：{set(CORE) - set(found)}"
    assert prompts.find_skill_dir().name == prompts.SKILL_DIRNAME


@pytest.mark.parametrize("name", CORE)
def test_load_strips_the_human_header(name):
    text = prompts.load(name)

    assert text and not text.startswith("<!--")
    assert "消费方" not in text, "用途 / 消费方是给人看的头，不该进提示词"
    assert prompts.load(name, strip_meta=False).lstrip().startswith("<!--")


def test_translate_asset_states_the_placeholder_discipline():
    """占位符纪律是四层机械校验的第一层，规则里必须写死。"""
    text = prompts.load(prompts.TRANSLATE)

    assert "⟦BLK-n⟧" in text and "⟦CAP-n⟧" in text
    assert "段落" in text and "术语" in text


def test_repair_asset_points_at_the_adaptation_table():
    """关节⑥ 的成功适配要沉淀成数据条目（架构 §2 原则 3 的促升规则）。"""
    text = prompts.load(prompts.REPAIR)

    assert "documentclass.json" in text and "adaptations" in text
    assert "重新编译" in text, "裁决权在编译，不在 agent 自述"


def test_classify_asset_keeps_the_conservative_default():
    text = prompts.load(prompts.CLASSIFY)

    assert "prose" in text and "heavy" in text and "unknown" in text


# --------------------------------------------------------------------------- #
# 错误路径
# --------------------------------------------------------------------------- #


def test_explicit_dir_wins_and_must_be_real(tmp_path):
    (tmp_path / "translate.md").write_text("自定义规则", encoding="utf-8")

    assert prompts.load(prompts.TRANSLATE, skill_dir=tmp_path) == "自定义规则"

    with pytest.raises(prompts.PromptError) as excinfo:
        prompts.find_skill_dir(tmp_path / "nope")
    assert excinfo.value.kind == "missing_skill"


def test_env_var_overrides_the_repo(tmp_path, monkeypatch):
    (tmp_path / "translate.md").write_text("环境变量规则", encoding="utf-8")
    monkeypatch.setenv(prompts.SKILL_ENV, str(tmp_path))

    assert prompts.load(prompts.TRANSLATE) == "环境变量规则"


def test_bad_names_and_missing_files_are_structured():
    for bad in ("../secrets", "Translate", "", "a b"):
        with pytest.raises(prompts.PromptError) as excinfo:
            prompts.path_of(bad)
        assert excinfo.value.kind == "bad_name"

    with pytest.raises(prompts.PromptError) as excinfo:
        prompts.load("no-such-asset")
    assert excinfo.value.kind == "missing_prompt"


def test_empty_asset_is_an_error(tmp_path):
    (tmp_path / "translate.md").write_text("<!-- 只有头 -->\n", encoding="utf-8")

    with pytest.raises(prompts.PromptError) as excinfo:
        prompts.load(prompts.TRANSLATE, skill_dir=tmp_path)
    assert excinfo.value.kind == "empty"


def test_joint_prompt_maps_the_joints():
    from tongtu.agent import JOINTS

    assert prompts.joint_prompt("fixup") == prompts.load(prompts.REPAIR)
    assert prompts.joint_prompt("build_env") == prompts.load(prompts.REPAIR)
    assert prompts.joint_prompt("env_classify") == prompts.load(prompts.CLASSIFY)
    assert set(prompts.JOINT_SKILLS) <= set(JOINTS), "关节名以 agent.JOINTS 为准"

    with pytest.raises(prompts.PromptError) as excinfo:
        prompts.joint_prompt("main_file")
    assert excinfo.value.kind == "unknown_joint"


# --------------------------------------------------------------------------- #
# 版本常量与 translate 的接线
# --------------------------------------------------------------------------- #


def test_version_constants_have_a_single_source():
    assert prompts.PROMPT_VERSION and prompts.STYLE_VERSION
    assert tr.PROMPT_VERSION is prompts.PROMPT_VERSION
    assert tr.STYLE_VERSION is prompts.STYLE_VERSION


def test_prompt_version_participates_in_the_cache_key():
    base = tr.cache_key("正文")

    assert base != tr.cache_key("正文", prompt_version="other")
    assert base == tr.cache_key("正文", prompt_version=prompts.PROMPT_VERSION)


def test_build_prompt_consumes_the_skill_asset():
    prompt = tr.build_prompt(tr.Context(terms=(("tensor", "张量"),)))

    assert prompts.load(prompts.TRANSLATE) in prompt, "规则来自 skill/，不是硬编码"
    assert "tensor → 张量" in prompt
    assert prompt.rstrip().endswith(tr.PROMPT_TAIL), "待翻译正文接在提示词之后"
