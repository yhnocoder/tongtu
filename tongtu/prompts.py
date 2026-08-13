"""prompt 资产装载（PHASE0 §3.4、架构决策 1：SKILL 降级为 prompt 资产）。

v2 的 `skill/SKILL.md` 一个人干了两件事：**编排**（流程步骤、目录约定）与 **prompt 资产**
（翻译规则、术语与文风、常见坑）。架构决策 1 把编排收回代码，剩下的按**关节**拆成
`skill/*.md`：

    skill/survey.md      关节④ 通读与术语     消费方 tongtu/stages/survey.py
    skill/translate.md   关节⑤ 逐块翻译     消费方 tongtu/stages/translate.py
    skill/repair.md      关节②/⑥ 编译修复   消费方 tongtu/stages/{baseline,compile}.py → agent.session
    skill/classify.md    关节③ 环境分类      消费方 tongtu/stages/mask.py 的 arbiter 回调

本模块只做两件事：**找到**这些文件、**读出来**（顺带剥掉给人看的 `<!-- -->` 头），外加
一个版本常量。

## 为什么版本号在这里而不在各阶段

`prompt_version` 进块级翻译缓存的 key（架构 §4）：prompt 改了而版本没动 = 拿旧译文冒充新
规则的产物。既然规则本身住在 `skill/` 里，版本号就该跟规则住在一起，成为**单一来源**——
`tongtu.stages.translate` 与将来的 survey 一律从这里 import，不各自定义。

**改动 `skill/` 下任何文件都要 bump :data:`PROMPT_VERSION`**（改的是文风规则则同时 bump
:data:`STYLE_VERSION`，后者一 bump 就是全量重翻，是显式有意的行为）。

## 为什么不 format()

skill 文件里全是 `\\section{...}`、`⟦BLK-n⟧` 这类字面量，`str.format` 会把 `{...}` 当字段
炸掉。故 prompt 组装一律**拼接**，不做模板替换——上下文块由调用方追加在规则之后。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

__all__ = [
    "CLASSIFY",
    "JOINT_SKILLS",
    "PACKAGED_SKILL",
    "PROMPT_VERSION",
    "REPAIR",
    "SKILL_DIRNAME",
    "SKILL_ENV",
    "STYLE_VERSION",
    "SURVEY",
    "TRANSLATE",
    "PromptError",
    "available",
    "find_skill_dir",
    "joint_prompt",
    "load",
    "path_of",
]

#: prompt 资产版本号（**单一来源**，进块级翻译缓存 key，架构 §4）。改 `skill/` 即 bump。
PROMPT_VERSION = "m3"

#: 全局文风规则版本号（架构 §4：bump 即全量重翻，是显式有意的行为）。
#: 与 `PROMPT_VERSION` 分开是因为二者失效范围不同：文风改动影响每一块，纪律措辞改动不影响
#: 已经通过校验的译文，但仍要让缓存诚实——故两个号各自 bump。
STYLE_VERSION = "m3"

#: 仓库里 prompt 资产的目录名与它的环境变量覆盖。
SKILL_DIRNAME = "skill"
SKILL_ENV = "TONGTU_SKILL"

#: wheel 里 prompt 资产的落点（pyproject 的 `force-include`，同 fonts 的做法）。
PACKAGED_SKILL = "data/skill"

# 三份资产的名字（不含扩展名）——只在这里写死一次。
TRANSLATE = "translate"
REPAIR = "repair"
CLASSIFY = "classify"
SURVEY = "survey"

#: 关节（`tongtu.agent.JOINTS`）→ prompt 资产名。关节①（主文件）暂无独立资产。
JOINT_SKILLS: dict[str, str] = {
    "build_env": REPAIR,
    "env_classify": CLASSIFY,
    "survey": SURVEY,
    "translate": TRANSLATE,
    "fixup": REPAIR,
}

_SUFFIX = ".md"

#: 合法资产名——顺手挡住 `../` 一类的路径穿越（名字有可能来自配置）。
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

#: 给人看的头（用途 / 消费方 / 版本约定）。送进模型的是规则本身，故装载时剥掉。
_META_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class PromptError(RuntimeError):
    """prompt 资产不可用（找不到目录、名字非法、文件缺失或读不动）。

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
    """判据：目录里有 `translate.md`——空目录与同名的无关目录都不算数。"""
    return path.is_dir() and (path / f"{TRANSLATE}{_SUFFIX}").is_file()


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
            f"指定的 prompt 资产目录不可用：{path}（需含 {TRANSLATE}{_SUFFIX}）",
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


def path_of(name: str, *, skill_dir: str | os.PathLike[str] | None = None) -> Path:
    """资产文件路径（不读内容）。名字非法或文件不存在时抛 :class:`PromptError`。"""
    if not _NAME_RE.match(name or ""):
        raise PromptError(
            f"非法的 prompt 资产名：{name!r}（只允许小写字母 / 数字 / `-` / `_`）",
            kind="bad_name",
            detail=str(name),
        )
    path = find_skill_dir(skill_dir) / f"{name}{_SUFFIX}"
    if not path.is_file():
        raise PromptError(
            f"没有 prompt 资产 {name}{_SUFFIX}（找的是 {path}）",
            kind="missing_prompt",
            detail=str(path),
        )
    return path


# ------------------------------------------------------------------ 装载


@lru_cache(maxsize=32)
def _read(path: str, strip_meta: bool) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:  # 权限 / 编码等
        raise PromptError(
            f"读不动 prompt 资产：{path}（{type(exc).__name__}：{exc}）",
            kind="unreadable",
            detail=path,
        ) from None
    if strip_meta:
        text = _META_RE.sub("", text)
    text = text.strip()
    if not text:
        raise PromptError(f"prompt 资产是空的：{path}", kind="empty", detail=path)
    return text


def load(
    name: str,
    *,
    skill_dir: str | os.PathLike[str] | None = None,
    strip_meta: bool = True,
) -> str:
    """读一份 prompt 资产（默认剥掉 `<!-- -->` 头注释，只留送进模型的规则正文）。

    结果按**文件路径**缓存；测试里换 `skill_dir` / `$TONGTU_SKILL` 会命中不同的 key，
    不会串味（同一路径的内容改动需要 `load.cache_clear()`——开发时改文件请重启进程）。
    """
    return _read(str(path_of(name, skill_dir=skill_dir)), strip_meta)


def joint_prompt(joint: str, **kwargs) -> str:
    """按关节名取资产（`tongtu.agent.JOINTS` → :data:`JOINT_SKILLS`）。"""
    name = JOINT_SKILLS.get(joint)
    if name is None:
        raise PromptError(
            f"关节 {joint!r} 没有登记 prompt 资产（已登记：{sorted(JOINT_SKILLS)}）",
            kind="unknown_joint",
            detail=str(joint),
        )
    return load(name, **kwargs)


def available(*, skill_dir: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    """目录里现有的资产名（排序）。并行会话新加的资产（如 survey）也会出现在这里。"""
    return tuple(sorted(p.stem for p in find_skill_dir(skill_dir).glob(f"*{_SUFFIX}")))


def cache_clear() -> None:
    """清掉装载缓存（改了 `skill/` 又不想重启进程时用；测试里也用它隔离）。"""
    _read.cache_clear()
