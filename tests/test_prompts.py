"""prompt 资产（`skill/<name>/SKILL.md`）的装载与版本语义（PHASE0 §3.4、架构决策 1）。

这一层不测「prompt 写得好不好」（那是 LLM 层的事，架构 §12 层 3），只测**机械**部分：
技能目录找得到、frontmatter 解析得对（含超出子集时的报错）、正文剥得干净、名字挡得住
路径穿越、版本号逐技能来自 frontmatter、translate 的提示词确实由 `skill/translate/`
组装而不是硬编码在代码里。
"""

import pytest

from tongtu import prompts
from tongtu.stages import translate as tr

#: 四个关节资产。
CORE = (prompts.TRANSLATE, prompts.REPAIR, prompts.CLASSIFY, prompts.SURVEY)


@pytest.fixture(autouse=True)
def _isolate_cache():
    prompts.cache_clear()
    yield
    prompts.cache_clear()


def write_skill(root, name, *, version="1", body="规则正文", description="说明"):
    """在 `root` 下按标准形态造一个技能，返回它的 `SKILL.md` 路径。"""
    path = root / name / prompts.SKILL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: {version}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# 定位与装载
# --------------------------------------------------------------------------- #


def test_repo_skill_dir_holds_the_joint_assets():
    found = prompts.available()

    assert set(CORE) <= set(found), f"缺 prompt 资产：{set(CORE) - set(found)}"
    assert prompts.find_skill_dir().name == prompts.SKILL_DIRNAME


@pytest.mark.parametrize("name", CORE)
def test_each_skill_is_a_folder_with_a_skill_md(name):
    """标准 Skill 形态：`skill/<name>/SKILL.md`，别的运行时也认得这个形状。"""
    path = prompts.path_of(name)

    assert path.name == prompts.SKILL_FILE
    assert path.parent == prompts.dir_of(name) == prompts.find_skill_dir() / name


@pytest.mark.parametrize("name", CORE)
def test_load_strips_the_frontmatter(name):
    text = prompts.load(name)

    assert text and not text.startswith("---")
    assert "description:" not in text, "frontmatter 是给人与运行时看的，不该进提示词"
    assert prompts.load(name, strip_meta=False).startswith("---"), "原文含 frontmatter"


@pytest.mark.parametrize("name", CORE)
def test_frontmatter_carries_name_description_version(name):
    fields = prompts.meta(name)

    assert fields["name"] == name
    assert fields["description"].strip()
    assert prompts.version_of(name) == fields["version"].strip()


def test_translate_asset_states_the_placeholder_discipline():
    """占位符纪律是四层机械校验的第一层，规则里必须写死。"""
    text = prompts.load(prompts.TRANSLATE)

    assert "⟦BLK-n⟧" in text and "⟦CAP-n⟧" in text
    assert "段落" in text and "术语" in text


def test_repair_asset_points_at_the_adaptation_table():
    """关节⑥的成功适配要沉淀成数据条目（架构 §2 原则 3 的促升规则）。"""
    text = prompts.load(prompts.REPAIR)

    assert "documentclass.json" in text and "adaptations" in text
    assert "重新编译" in text, "裁决权在编译，不在 agent 自述"


def test_classify_asset_keeps_the_conservative_default():
    text = prompts.load(prompts.CLASSIFY)

    assert "prose" in text and "heavy" in text and "unknown" in text


# --------------------------------------------------------------------------- #
# frontmatter 解析（零依赖的 `key: value` 子集）
# --------------------------------------------------------------------------- #


def test_parses_the_supported_subset():
    fields, body = prompts.parse_frontmatter(
        "---\n"
        "# 注释行\n"
        "name: translate\n"
        'description: "带引号的说明：冒号也不怕"\n'
        "version: 2\n"
        "\n"
        "---\n"
        "\n正文第一行\n"
    )

    assert fields == {
        "name": "translate",
        "description": "带引号的说明：冒号也不怕",
        "version": "2",
    }
    assert body.strip() == "正文第一行"


def test_no_frontmatter_means_all_body():
    fields, body = prompts.parse_frontmatter("# 标题\n正文")

    assert fields == {} and body == "# 标题\n正文"


@pytest.mark.parametrize(
    "text",
    [
        "---\nname: translate\ntags:\n  - a\n---\n正文\n",  # 嵌套 / 列表
        "---\ndescription: |\n  块标量\n---\n正文\n",  # 块标量
        "---\nname translate\n---\n正文\n",  # 没有冒号
        "---\nname: translate\n正文\n",  # 没有收尾围栏
        "---\nname: a\nname: b\n---\n正文\n",  # 键重复
    ],
)
def test_beyond_the_subset_is_a_clear_error(text):
    with pytest.raises(prompts.PromptError) as excinfo:
        prompts.parse_frontmatter(text, source="skill/x/SKILL.md")

    assert excinfo.value.kind == "bad_frontmatter"
    assert "skill/x/SKILL.md" in str(excinfo.value) or "SKILL.md" in excinfo.value.detail


def test_frontmatter_must_be_complete_and_match_the_folder(tmp_path):
    (tmp_path / "translate").mkdir()
    (tmp_path / "translate" / prompts.SKILL_FILE).write_text(
        "---\nname: translate\nversion: 1\n---\n\n正文\n", encoding="utf-8"
    )
    with pytest.raises(prompts.PromptError) as excinfo:
        prompts.load(prompts.TRANSLATE, skill_dir=tmp_path)
    assert excinfo.value.kind == "missing_field"

    prompts.cache_clear()
    write_skill(tmp_path, "translate")
    (tmp_path / "repair" / prompts.SKILL_FILE).parent.mkdir()
    (tmp_path / "repair" / prompts.SKILL_FILE).write_text(
        "---\nname: repare\ndescription: 拼错了\nversion: 1\n---\n\n正文\n",
        encoding="utf-8",
    )
    with pytest.raises(prompts.PromptError) as excinfo:
        prompts.load(prompts.REPAIR, skill_dir=tmp_path)
    assert excinfo.value.kind == "bad_frontmatter"


# --------------------------------------------------------------------------- #
# 错误路径
# --------------------------------------------------------------------------- #


def test_explicit_dir_wins_and_must_be_real(tmp_path):
    write_skill(tmp_path, "translate", body="自定义规则")

    assert prompts.load(prompts.TRANSLATE, skill_dir=tmp_path) == "自定义规则"

    with pytest.raises(prompts.PromptError) as excinfo:
        prompts.find_skill_dir(tmp_path / "nope")
    assert excinfo.value.kind == "missing_skill"


def test_env_var_overrides_the_repo(tmp_path, monkeypatch):
    write_skill(tmp_path, "translate", body="环境变量规则")
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
    write_skill(tmp_path, "translate", body="")

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


def test_joint_version_is_lenient_because_it_only_feeds_bookkeeping():
    assert prompts.joint_version("fixup") == prompts.version_of(prompts.REPAIR)
    assert prompts.joint_version("main_file") == "", "关节①没有专属技能"


# --------------------------------------------------------------------------- #
# 版本语义与 translate 的接线
# --------------------------------------------------------------------------- #


def test_versions_come_from_each_skills_frontmatter(tmp_path):
    write_skill(tmp_path, "translate", version="7")
    write_skill(tmp_path, "repair", version="3")

    assert prompts.versions(skill_dir=tmp_path) == {"repair": "3", "translate": "7"}
    assert prompts.aggregate_version(skill_dir=tmp_path) == "repair@3+translate@7"


def test_a_skill_without_frontmatter_has_no_version(tmp_path):
    """自定义目录可以只写正文；但那样就没有版本号，缓存 key 不许拿默认值糊弄。"""
    path = tmp_path / "translate" / prompts.SKILL_FILE
    path.parent.mkdir(parents=True)
    path.write_text("光有正文\n", encoding="utf-8")

    assert prompts.load(prompts.TRANSLATE, skill_dir=tmp_path) == "光有正文"
    with pytest.raises(prompts.PromptError) as excinfo:
        prompts.version_of(prompts.TRANSLATE, skill_dir=tmp_path)
    assert excinfo.value.kind == "missing_field"


def test_translate_takes_its_own_skill_version_not_the_aggregate():
    """评审结论：改 repair 不该失效 translate 的译文缓存。"""
    assert tr.prompt_version() == prompts.version_of(prompts.TRANSLATE)
    assert tr.prompt_version() != prompts.PROMPT_VERSION, "聚合版本不进块级缓存 key"
    assert prompts.PROMPT_VERSION == prompts.aggregate_version()


def test_style_version_stays_a_hand_bumped_constant():
    """`style_version` 是用户可覆盖的契约字段（架构 §8），默认值由人显式决定。"""
    assert prompts.STYLE_VERSION and tr.STYLE_VERSION is prompts.STYLE_VERSION


def test_prompt_version_participates_in_the_cache_key():
    base = tr.cache_key("正文")

    assert base != tr.cache_key("正文", prompt_version="other")
    assert base == tr.cache_key("正文", prompt_version=tr.prompt_version())


def test_only_the_translate_skill_moves_the_cache_key(tmp_path, monkeypatch):
    """同一段正文：改 repair 的版本 key 不动，改 translate 的版本 key 才动。"""
    for name in CORE:
        write_skill(tmp_path, name, version="1")
    monkeypatch.setenv(prompts.SKILL_ENV, str(tmp_path))
    base = tr.cache_key("正文")

    write_skill(tmp_path, "repair", version="2")
    prompts.cache_clear()
    assert tr.cache_key("正文") == base, "改编译修复的规则不该让译文全量重翻"

    write_skill(tmp_path, "translate", version="2")
    prompts.cache_clear()
    assert tr.cache_key("正文") != base, "改翻译规则必须让译文缓存失效"


def test_build_prompt_consumes_the_skill_asset():
    prompt = tr.build_prompt(tr.Context(terms=(("tensor", "张量"),)))

    assert prompts.load(prompts.TRANSLATE) in prompt, "规则来自 skill/，不是硬编码"
    assert "tensor → 张量" in prompt
    assert prompt.rstrip().endswith(tr.PROMPT_TAIL), "待翻译正文接在提示词之后"
