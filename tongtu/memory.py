"""翻译记忆（块级缓存）的存取与失效（架构 §4 块级缓存、§7 `chunks.json`、决策 3）。

架构决策 3 把「可丢弃缓存」与「增量重翻所需状态」分了开：后者随产物包走，于是
**权威翻译记忆是产物包里的 `out/chunks.json`**，`build/` 整体删除不丢失任何昂贵成果。
本模块就是这条决策的实现：

    装载： out/chunks.json（权威，上一轮产物） + build/zh-chunks/chunks.json（本轮工作副本）
             → {cache_key: 译文正文}
    写回： translate 阶段照 `chunks.schema.json` 落 build/zh-chunks/chunks.json
             （export（M4）负责搬进 out/；本期只保证 build 侧完整、且 build 删了能从 out 恢复）
    失效： retranslate 按块 id / 术语 / 全量删条目——**删的是缓存条目，不是控制流回跳**
             （架构 §2 原则 2：返工 = 失效 + 重算受影响子图）

## key 是谁算的

不是本模块。cache key 的公式住在 `tongtu.stages.translate.cache_key`（架构 §4 逐项照
搬），本模块只负责**存取与命中统计**：改术语只失效命中块、bump `style_version` /
`prompt_version` / 换模型全失效、brief 变化全失效——这些语义全都已经编码在 key 里，
装载方不需要（也不应该）再判一次。

## 为什么记忆里存的是「正文」而不是块文本

`chunks.json` 的 `translation` 含块首尾空白（拼接恒等于掩码流是 compile 的前提），而
translate 的块循环把首尾空白**由驱动器保管**（`lead + complete(...) + trail`），缓存里
只该有中间那截正文。故装载时按 :func:`tongtu.stages.translate.split_affixes` 剥一次——
两处用同一把尺子，命中之后重新拼回去仍是同一个形状。

## 只有 translated 进记忆

`status="fallback"` 的条目存的是**原文**（重试用尽的保底）。把它当缓存命中等于把一次
失败永久冻结起来——下一轮连重试的机会都没有了。故装载时一律跳过，让它重新翻一次。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, MutableMapping

from .glossary import hit_terms
from .stages.translate import FALLBACK, split_affixes

__all__ = [
    "CHUNKS_NAME",
    "Memory",
    "ZH_CHUNKS_DIRNAME",
    "chunk_ids",
    "drop_entries",
    "entries",
    "keys_for_chunks",
    "keys_for_term",
    "load",
    "load_file",
    "memory_paths",
    "read_chunks",
    "write_chunks",
]

#: 翻译记忆的文件名（产物契约 `out/chunks.json`，build 侧同名同形，export 直接搬）。
CHUNKS_NAME = "chunks.json"

#: build 区里译块与工作副本所在的目录名。
ZH_CHUNKS_DIRNAME = "zh-chunks"


# --------------------------------------------------------------------- 记忆对象


@dataclass
class Memory(MutableMapping[str, str]):
    """`{cache_key: 译文正文}` 的可变映射，外加「从哪儿装来的」这点账。

    形状刻意就是 `translate(cache=...)` 要的那个 `MutableMapping`——阶段驱动器不认识
    工作目录，也不该认识；它只知道有个字典可以查、可以写。
    """

    entries: dict[str, str] = field(default_factory=dict)
    sources: tuple[str, ...] = ()
    """装载来源（按装载顺序，靠后的覆盖靠前的）；进 manifest / 事件里的人话说明。"""

    def __getitem__(self, key: str) -> str:
        return self.entries[key]

    def __setitem__(self, key: str, value: str) -> None:
        self.entries[key] = value

    def __delitem__(self, key: str) -> None:
        del self.entries[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def forget(self, keys: Iterable[str]) -> int:
        """删掉若干条目，返回真正删掉的条数（retranslate 的「失效」就是这一下）。"""
        dropped = 0
        for key in set(keys):
            if self.entries.pop(key, None) is not None:
                dropped += 1
        return dropped

    def to_json(self) -> dict:
        return {"entries": len(self.entries), "sources": list(self.sources)}


# --------------------------------------------------------------------- 读文件


def read_chunks(path: str | Path) -> dict | None:
    """读一份 `chunks.json`；不存在 / 不是 JSON 对象 → None（不是错误）。

    翻译记忆坏了不该让流水线停摆——最坏的后果只是**全部块重翻一次**（贵，但不损坏）。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def entries(data: Mapping | None) -> tuple[dict, ...]:
    """`chunks.json` 里的块条目（形状不对一律当空）。"""
    if not isinstance(data, Mapping):
        return ()
    raw = data.get("chunks")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def load_file(path: str | Path) -> dict[str, str]:
    """一份 `chunks.json` → `{cache_key: 译文正文}`（跳过回退块与残缺条目）。"""
    found: dict[str, str] = {}
    for entry in entries(read_chunks(path)):
        key = entry.get("cache_key")
        translation = entry.get("translation")
        if not isinstance(key, str) or not key or not isinstance(translation, str):
            continue
        if entry.get("status") == FALLBACK:
            continue  # 回退块存的是原文，命中它等于把一次失败冻结成永久结论
        body = split_affixes(translation)[1]
        if body:
            found[key] = body
    return found


def memory_paths(workdir) -> tuple[Path, Path]:
    """`(out/chunks.json, build/zh-chunks/chunks.json)`——权威记忆在前，工作副本在后。"""
    root = Path(getattr(workdir, "path", workdir))
    out = getattr(workdir, "out", None)
    build = getattr(workdir, "build", None)
    out_dir = Path(out) if out is not None else root / "out"
    build_dir = Path(build) if build is not None else root / "build"
    return out_dir / CHUNKS_NAME, build_dir / ZH_CHUNKS_DIRNAME / CHUNKS_NAME


def load(workdir) -> Memory:
    """装载这篇论文的翻译记忆（`out/` 权威 → `build/` 工作副本，靠后的覆盖靠前的）。

    覆盖顺序的依据：两份都在时 `build/` 是本轮刚写的、更新，而 `out/` 是上一轮产物；
    同一个 cache_key 在两边理应是同一段译文，真不一致时以更新的那份为准。
    """
    memory = Memory()
    for path in memory_paths(workdir):
        found = load_file(path)
        if not found:
            continue
        memory.entries.update(found)
        memory.sources = (*memory.sources, str(path))
    return memory


# --------------------------------------------------------------------- 写文件


def write_chunks(path: str | Path, payload: Mapping) -> Path:
    """落一份 `chunks.json`（UTF-8、缩进 2、末尾换行——与其余产物同一风格）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def drop_entries(path: str | Path, keys: Iterable[str]) -> int:
    """从盘上的 `chunks.json` 里抹掉若干 cache_key，返回抹掉的条数。

    retranslate 失效 `out/chunks.json`（权威记忆）时用：只从内存里删是不够的——下一次
    `tongtu run` 会把它原样再装回来，用户会觉得「重翻根本没生效」。
    """
    keys = set(keys)
    if not keys:
        return 0
    data = read_chunks(path)
    items = entries(data)
    if data is None or not items:
        return 0
    kept = [item for item in items if item.get("cache_key") not in keys]
    dropped = len(items) - len(kept)
    if dropped:
        write_chunks(path, {**data, "chunks": kept})
    return dropped


# --------------------------------------------------------------------- 失效选择


def chunk_ids(data: Mapping | None) -> tuple[str, ...]:
    """记忆里有哪些块 id（按文档顺序）——`--chunks` 报「没有这个块」时用它给提示。"""
    return tuple(str(e["id"]) for e in entries(data) if isinstance(e.get("id"), str))


def keys_for_chunks(data: Mapping | None, ids: Iterable[str]) -> tuple[set[str], tuple[str, ...]]:
    """`(命中的 cache_key 集合, 记忆里没有的块 id)`（`retranslate --chunks`）。"""
    wanted = [str(i).strip() for i in ids if str(i).strip()]
    by_id: dict[str, set[str]] = {}
    for entry in entries(data):
        key, chunk_id = entry.get("cache_key"), entry.get("id")
        if isinstance(chunk_id, str) and isinstance(key, str) and key:
            by_id.setdefault(chunk_id, set()).add(key)
    keys: set[str] = set()
    missing: list[str] = []
    for chunk_id in wanted:
        found = by_id.get(chunk_id)
        if found:
            keys |= found
        else:
            missing.append(chunk_id)
    return keys, tuple(missing)


def keys_for_term(data: Mapping | None, term: str) -> set[str]:
    """命中某术语的块的 cache_key（`retranslate --term WORD`）。

    命中判定用 :func:`tongtu.glossary.hit_terms`——与 translate 组装上下文、算 cache key
    时**同一份实现**（大小写不敏感的子串命中）。两处各写一遍迟早会漂，而漂了就意味着
    「我说它命中」与「缓存 key 里算它命中」不是一回事。

    两条线索取并集：块源码里出现过这个词，或者块的**术语快照**里记着它（术语可能是靠
    `aliases` 命中的，源码里未必出现这个词形）。
    """
    needle = (term or "").strip()
    if not needle:
        return set()
    lowered = needle.lower()
    keys: set[str] = set()
    for entry in entries(data):
        key = entry.get("cache_key")
        if not isinstance(key, str) or not key:
            continue
        if hit_terms(str(entry.get("src") or ""), {needle: needle}):
            keys.add(key)
            continue
        snapshot = entry.get("terms")
        if isinstance(snapshot, (list, tuple)) and any(
            isinstance(t, Mapping) and str(t.get("term", "")).strip().lower() == lowered
            for t in snapshot
        ):
            keys.add(key)
    return keys
