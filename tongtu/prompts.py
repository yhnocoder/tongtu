"""prompt 资产装载（PHASE0 §3.4、架构决策 1：SKILL 降级为 prompt 资产）。

v2 的 `skill/SKILL.md` 承担了两件事：**编排**（流程步骤、目录约定）与 **prompt 资产**
（翻译规则、术语与文风、常见坑）。架构决策 1 把编排收回代码，剩下的按**关节**拆开，每个
关节一个标准 Skill 目录（`skill/<name>/SKILL.md`）：

    skill/survey/     关节④ 通读与术语   消费方 tongtu/stages/survey.py::build_prompt
    skill/translate/  关节⑤ 逐块翻译     消费方 tongtu/stages/translate.py::build_prompt
    skill/repair/     关节②/⑥ 编译修复   消费方 tongtu/stages/{baseline,compile}.py → agent.session
    skill/classify/   关节③ 环境分类     消费方 tongtu/stages/mask.py 的 arbiter 回调

## 目录形态

采用 Agent Skills 通用规范（Claude Code / Codex 等运行时都能直接识别的形状）：

    skill/<name>/SKILL.md        YAML frontmatter（name / description / version）+ Markdown 正文
    skill/<name>/references/     可选：长参考材料，正文里用 `@references/xxx.md` 引用
    skill/<name>/scripts/        可选：随技能分发的脚本

**目前四个技能都只有 SKILL.md**，是有意的：通途的三个 `complete` 关节（③④⑤）只把
正文文本递给模型，模型没有读文件的手段；`session` 关节（②/⑥）的工作目录是论文目录而不是
本仓库，同样够不着 `references/`。所以规则本身必须留在 SKILL.md 正文里——拆到
`references/` 下会让规则**静默失传**。等某个关节真的跑在能读文件的运行时里，再拆不迟。

本模块只做三件事：**找到**这些技能、**读出正文**（剥掉 frontmatter）、**读出版本号**。

## 版本号：逐技能，不是全局一个

`prompt_version` 进块级翻译缓存的 key（架构 §4）：规则改了而版本没动，等于拿旧译文当作新
规则的产物。既然规则住在各自的 SKILL.md 里，版本号就写在同一份文件的 frontmatter 里，改
规则与改版本是同一次编辑——这是**单一来源**。

**版本号按技能分开**是本轮评审的结论：从前一个全局 `PROMPT_VERSION` 进 translate 的缓存
key，于是改一句编译修复的措辞就会让全篇译文缓存失效，多付一次全量重翻的成本。现在
:func:`version_of` 逐技能读 frontmatter，translate 的缓存 key 只认 `translate` 的版本号
（见 :func:`tongtu.stages.translate.prompt_version`），改 `repair` 不再牵连它。

:data:`PROMPT_VERSION` 保留为**聚合版本**（各技能 `name@version` 的有序拼接），只用于报告
与诊断——它不进任何缓存 key，因此语义变化不会引发全量重翻。

:data:`STYLE_VERSION` 仍是本模块的手写常量，不来自 frontmatter：它是**用户可覆盖的契约
字段**（`glossary.style.style_version`，架构 §8）的默认值，bump 一次即全量重翻，该由人显式
决定，而不该跟着 translate 正文里任何一句无关措辞的修订一起动。

## 为什么不 format()

skill 正文里全是 `\\section{...}`、`⟦BLK-n⟧` 这类字面量，`str.format` 会把 `{...}` 当字段
解析并报错。故 prompt 组装一律**拼接**，不做模板替换——上下文块由调用方追加在规则之后。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 由下面的模块级 `__getattr__` 惰性提供，见那里的说明。
    #: 全部技能的聚合版本（`classify@1+repair@1+…`）。报告与诊断用，不进缓存 key。
    PROMPT_VERSION: str

__all__ = [
    "CLASSIFY",
    "JOINT_SKILLS",
    "PACKAGED_SKILL",
    "PROMPT_VERSION",
    "REPAIR",
    "SKILL_DIRNAME",
    "SKILL_ENV",
    "SKILL_FILE",
    "STYLE_VERSION",
    "SURVEY",
    "TRANSLATE",
    "PromptError",
    "aggregate_version",
    "available",
    "dir_of",
    "find_skill_dir",
    "joint_prompt",
    "joint_version",
    "load",
    "meta",
    "parse_frontmatter",
    "path_of",
    "version_of",
    "versions",
]

#: 全局文风规则版本号（架构 §4：bump 即全量重翻，是显式有意的行为）。
#: 手写常量而非 frontmatter：见模块文档「版本号」一节。
STYLE_VERSION = "m3"

#: 仓库里 prompt 资产的目录名与它的环境变量覆盖。
SKILL_DIRNAME = "skill"
SKILL_ENV = "TONGTU_SKILL"

#: 每个技能目录里的正文文件名（Agent Skills 通用规范）。
SKILL_FILE = "SKILL.md"

#: wheel 里 prompt 资产的落点（pyproject 的 `force-include`，同 fonts 的做法）。
PACKAGED_SKILL = "data/skill"

# 四个技能的名字（= 目录名 = frontmatter 的 name）——只在这里写死一次。
TRANSLATE = "translate"
REPAIR = "repair"
CLASSIFY = "classify"
SURVEY = "survey"

#: 关节（`tongtu.agent.JOINTS`）→ 技能名。关节①（主文件）暂无独立资产。
JOINT_SKILLS: dict[str, str] = {
    "build_env": REPAIR,
    "env_classify": CLASSIFY,
    "survey": SURVEY,
    "translate": TRANSLATE,
    "fixup": REPAIR,
}

#: 合法技能名——同时挡住 `../` 一类的路径穿越（名字有可能来自配置）。
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

#: frontmatter 的分隔行（首行必须是它，正文从下一条分隔行之后开始）。
_FENCE = "---"

#: frontmatter 支持的键名形状（本解析器只认 `key: value` 这一个子集）。
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: frontmatter 里必须给全的字段。
_REQUIRED_META = ("name", "description", "version")


class PromptError(RuntimeError):
    """prompt 资产不可用（找不到目录、名字非法、文件缺失、读不动或 frontmatter 不合法）。

    结构化到 `kind` + `detail`（同 :class:`tongtu.compiler.AssetError`）：调用方据此决定
    是当环境问题终止，还是记警告降级。
    """

    def __init__(self, message: str, *, kind: str = "prompt", detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail

    def to_json(self) -> dict:
        return {"kind": self.kind, "message": str(self), "detail": self.detail}


# ------------------------------------------------------------------ 定位


def _looks_like_skill_dir(path: Path) -> bool:
    """判据：目录里有 `translate/SKILL.md`——空目录与同名的无关目录都不算数。"""
    return path.is_dir() and (path / TRANSLATE / SKILL_FILE).is_file()


def _packaged_skill_dir() -> Path | None:
    """包内 prompt 资产目录（wheel 安装态）。取不到真实路径（zip 导入等）则 None。"""
    try:
        path = Path(str(files("tongtu").joinpath(PACKAGED_SKILL)))
    except (ModuleNotFoundError, TypeError, OSError):
        return None
    return path.absolute() if _looks_like_skill_dir(path) else None


def find_skill_dir(skill_dir: str | os.PathLike[str] | None = None) -> Path:
    """定位 `skill/`。解析顺序与 :func:`tongtu.compiler.find_fonts` 同构：

    显式参数 → `$TONGTU_SKILL` → **相对本模块逐级向上找**（源码树 / editable 安装：
    `tongtu/prompts.py` → 仓库根 `skill/`）→ **包内 `tongtu/data/skill/`**（wheel 安装态）。

    源码树优先：开发时改仓库 `skill/` 立即生效。全落空时抛 :class:`PromptError`。
    """
    if skill_dir is not None:
        path = Path(skill_dir).expanduser()
        if _looks_like_skill_dir(path):
            return path.absolute()
        raise PromptError(
            f"指定的 prompt 资产目录不可用：{path}（需含 {TRANSLATE}/{SKILL_FILE}）",
            kind="missing_skill",
            detail=str(path),
        )

    env = (os.environ.get(SKILL_ENV) or "").strip()
    if env:
        path = Path(env).expanduser()
        if _looks_like_skill_dir(path):
            return path.absolute()
        raise PromptError(
            f"${SKILL_ENV} 指向的 prompt 资产目录不可用：{path}",
            kind="missing_skill",
            detail=str(path),
        )

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / SKILL_DIRNAME
        if _looks_like_skill_dir(candidate):
            return candidate

    packaged = _packaged_skill_dir()
    if packaged is not None:
        return packaged

    raise PromptError(
        f"找不到 prompt 资产目录 {SKILL_DIRNAME}/——关节⑤/⑥/③ 的规则都住在那里。"
        f"源码树 / editable 安装从仓库根找，wheel 安装态从包内 {PACKAGED_SKILL} 找；"
        f"都没有时用 ${SKILL_ENV} 显式指定",
        kind="missing_skill",
        detail=str(here.parent),
    )


def dir_of(name: str, *, skill_dir: str | os.PathLike[str] | None = None) -> Path:
    """技能自己的目录（`skill/<name>/`）——`references/` 一类的旁挂内容住在这里。"""
    if not _NAME_RE.match(name or ""):
        raise PromptError(
            f"非法的技能名：{name!r}（只允许小写字母 / 数字 / `-` / `_`）",
            kind="bad_name",
            detail=str(name),
        )
    return find_skill_dir(skill_dir) / name


def path_of(name: str, *, skill_dir: str | os.PathLike[str] | None = None) -> Path:
    """技能正文的路径（`skill/<name>/SKILL.md`，不读内容）。

    名字非法或文件不存在时抛 :class:`PromptError`。
    """
    path = dir_of(name, skill_dir=skill_dir) / SKILL_FILE
    if not path.is_file():
        raise PromptError(
            f"没有技能 {name}（找的是 {path}）",
            kind="missing_prompt",
            detail=str(path),
        )
    return path


# ------------------------------------------------------------------ frontmatter


def parse_frontmatter(text: str, *, source: str = "") -> tuple[dict[str, str], str]:
    """拆出 YAML frontmatter 与正文，返回 `({键: 值}, 正文)`。

    **只支持 `key: value` 这一个子集**（零依赖，不引 PyYAML）：一行一个键，值是单行标量，
    可用成对的单 / 双引号包起来；空行与 `#` 开头的整行注释跳过。列表、嵌套映射、块标量
    （`|` / `>`）、跨行值一律**报错而不是猜**——报错里带行号与原文，改的人一眼看得见。

    没有 frontmatter（首行不是 `---`）时返回 `({}, 原文)`：自定义资产目录可以只写正文，
    只是这样就没有版本号（:func:`version_of` 会明确报错）。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return ({}, text)

    where = f"{source}：" if source else ""
    data: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=2):
        stripped = line.strip()
        if stripped == _FENCE:
            body = "\n".join(lines[index:])
            return (data, body)
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            raise PromptError(
                f"{where}第 {index} 行 frontmatter 有缩进——本解析器只支持顶层的 "
                f"`key: value`，不支持嵌套或列表：{line!r}",
                kind="bad_frontmatter",
                detail=str(source),
            )
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or not _KEY_RE.match(key):
            raise PromptError(
                f"{where}第 {index} 行不是合法的 `key: value`：{line!r}",
                kind="bad_frontmatter",
                detail=str(source),
            )
        value = value.strip()
        if value[:1] in ("|", ">", "[", "{", "&", "*"):
            raise PromptError(
                f"{where}第 {index} 行的值用了 YAML 的高级写法（块标量 / 流式集合 / 锚点），"
                f"本解析器只支持单行标量：{line!r}",
                kind="bad_frontmatter",
                detail=str(source),
            )
        if not value:
            raise PromptError(
                f"{where}第 {index} 行 `{key}` 没有值——空值多半是想写多行，本解析器只支持单行标量：{line!r}",
                kind="bad_frontmatter",
                detail=str(source),
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in data:
            raise PromptError(
                f"{where}第 {index} 行的键 `{key}` 重复了",
                kind="bad_frontmatter",
                detail=str(source),
            )
        data[key] = value

    raise PromptError(
        f"{where}frontmatter 没有收尾的 `{_FENCE}` 行",
        kind="bad_frontmatter",
        detail=str(source),
    )


# ------------------------------------------------------------------ 装载


@lru_cache(maxsize=32)
def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:  # 权限 / 编码等
        raise PromptError(
            f"读不动 prompt 资产：{path}（{type(exc).__name__}：{exc}）",
            kind="unreadable",
            detail=path,
        ) from None


@lru_cache(maxsize=32)
def _split(path: str) -> tuple[tuple[tuple[str, str], ...], str]:
    """`(frontmatter 键值对, 正文)`——键值对做成元组，免得缓存里的字典被就地改掉。"""
    data, body = parse_frontmatter(_read(path), source=path)
    body = body.strip()
    if not body:
        raise PromptError(f"prompt 资产的正文是空的：{path}", kind="empty", detail=path)
    if data:  # 写了 frontmatter 就得写全、且 name 与目录名对得上
        missing = [key for key in _REQUIRED_META if not data.get(key)]
        if missing:
            raise PromptError(
                f"{path} 的 frontmatter 缺字段：{'、'.join(missing)}（必填：{'、'.join(_REQUIRED_META)}）",
                kind="missing_field",
                detail=path,
            )
        expected = Path(path).parent.name
        if data["name"] != expected:
            raise PromptError(
                f"{path} 的 frontmatter 写着 name: {data['name']}，与目录名 {expected} 不一致——技能名以目录名为准",
                kind="bad_frontmatter",
                detail=path,
            )
    return (tuple(data.items()), body)


def load(
    name: str,
    *,
    skill_dir: str | os.PathLike[str] | None = None,
    strip_meta: bool = True,
) -> str:
    """读一个技能的正文（默认剥掉 frontmatter，只留送进模型的规则）。

    `strip_meta=False` 时返回文件原文（含 frontmatter），给诊断与测试用。

    结果按**文件路径**缓存；测试里换 `skill_dir` / `$TONGTU_SKILL` 会命中不同的 key，
    互不干扰（同一路径的内容改动需要 :func:`cache_clear`——开发时改文件请重启进程）。
    """
    path = str(path_of(name, skill_dir=skill_dir))
    if not strip_meta:
        return _read(path)
    return _split(path)[1]


def meta(name: str, *, skill_dir: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """一个技能的 frontmatter（`{name, description, version, …}`）。没写则是空字典。"""
    return dict(_split(str(path_of(name, skill_dir=skill_dir)))[0])


def version_of(name: str, *, skill_dir: str | os.PathLike[str] | None = None) -> str:
    """一个技能的版本号（frontmatter 的 `version`）。

    进对应关节的缓存 key，故**缺了就报错**：默认一个「0」会让缓存对改动视而不见。
    """
    fields = meta(name, skill_dir=skill_dir)
    version = (fields.get("version") or "").strip()
    if not version:
        raise PromptError(
            f"技能 {name} 没有 frontmatter，取不到 version——它进缓存 key，不能缺"
            f"（必填字段：{'、'.join(_REQUIRED_META)}）",
            kind="missing_field",
            detail=name,
        )
    return version


def versions(*, skill_dir: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """现有技能的 `{名字: 版本号}`（按名字排序）。"""
    return {name: version_of(name, skill_dir=skill_dir) for name in available(skill_dir=skill_dir)}


def aggregate_version(*, skill_dir: str | os.PathLike[str] | None = None) -> str:
    """全部技能的聚合版本，形如 `classify@1+repair@1+survey@1+translate@1`。

    只用于报告与诊断（「这一跑用的是哪一套规则」），**不进任何缓存 key**——缓存按技能各认
    各的版本号，见模块文档。可读拼接而不是 hash：出问题时人要能一眼看出是谁动了。
    """
    pairs = versions(skill_dir=skill_dir).items()
    return "+".join(f"{name}@{version}" for name, version in pairs)


def joint_prompt(joint: str, **kwargs) -> str:
    """按关节名取正文（`tongtu.agent.JOINTS` → :data:`JOINT_SKILLS`）。"""
    return load(_skill_for(joint), **kwargs)


def joint_version(
    joint: str,
    *,
    skill_dir: str | os.PathLike[str] | None = None,
    default: str = "",
) -> str:
    """按关节名取版本号；关节没登记技能或技能不可用时返回 `default`。

    宽容是有意的：调用方是**记账**（`Intervention.prompt_version`），记不到版本号不该把
    正在进行的修复打断。真正依赖版本号正确性的是缓存 key，那条路走 :func:`version_of`。
    """
    try:
        return version_of(_skill_for(joint), skill_dir=skill_dir)
    except PromptError:
        return default


def _skill_for(joint: str) -> str:
    name = JOINT_SKILLS.get(joint)
    if name is None:
        raise PromptError(
            f"关节 {joint!r} 没有登记技能（已登记：{sorted(JOINT_SKILLS)}）",
            kind="unknown_joint",
            detail=str(joint),
        )
    return name


def available(*, skill_dir: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    """目录里现有的技能名（排序）。并行会话新加的技能也会出现在这里。"""
    root = find_skill_dir(skill_dir)
    return tuple(sorted(path.parent.name for path in root.glob(f"*/{SKILL_FILE}") if _NAME_RE.match(path.parent.name)))


def cache_clear() -> None:
    """清掉装载缓存（改了 `skill/` 又不想重启进程时用；测试里也用它隔离）。"""
    _read.cache_clear()
    _split.cache_clear()


def __getattr__(name: str) -> str:
    """:data:`PROMPT_VERSION` 惰性求值：它要读四份 SKILL.md，不该在 import 时做 IO。

    （PEP 562 的模块级 `__getattr__`；`from tongtu.prompts import PROMPT_VERSION` 一样走这里。）
    """
    if name == "PROMPT_VERSION":
        return aggregate_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
