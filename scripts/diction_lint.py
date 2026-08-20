#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DENYLIST_PATH = REPO / "scripts" / "diction_denylist.toml"
BASELINE_PATH = REPO / "scripts" / "diction_baseline.json"

EXCLUDE_PREFIXES = (
    "tongtu/data/report_page/vendor/",
    "tests/fixtures/",
    "fonts/",
)
EXCLUDE_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "scripts/diction_denylist.toml",
    "scripts/diction_baseline.json",
    "uv.lock",
}
TEXT_EXTS = {
    ".py",
    ".md",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".txt",
    ".tex",
    ".js",
    ".css",
    ".html",
    ".sh",
    ".cls",
    ".sty",
    ".bib",
}


def load_rules() -> list[tuple[re.Pattern, str, str]]:
    try:
        data = tomllib.loads(DENYLIST_PATH.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"denylist 不是合法 TOML：{e}") from e
    rules = []
    for i, rule in enumerate(data.get("rules", []), 1):
        pattern, hint = rule.get("pattern"), rule.get("hint")
        if not isinstance(pattern, str) or not isinstance(hint, str):
            raise SystemExit(f"denylist 第 {i} 条规则缺 pattern 或 hint 字符串：{rule!r}")
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise SystemExit(f"denylist 第 {i} 条规则正则非法（{e}）：{pattern!r}") from e
        rules.append((compiled, pattern, hint))
    if not rules:
        raise SystemExit("denylist 没有任何 [[rules]] 条目")
    return rules


def lintable(rel: str) -> bool:
    if rel in EXCLUDE_FILES or rel.startswith(EXCLUDE_PREFIXES):
        return False
    return Path(rel).suffix in TEXT_EXTS


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [f for f in out.split("\0") if f and lintable(f)]


def scan(rel_paths: list[str], rules) -> dict[tuple[str, str], list[tuple[int, str, str]]]:
    """返回 {(路径, 正则): [(行号, 命中文本, 提示)]}。"""
    found: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    for rel in rel_paths:
        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for compiled, pattern, hint in rules:
                for m in compiled.finditer(line):
                    found.setdefault((rel, pattern), []).append((lineno, m.group(0), hint))
    return found


def load_baseline() -> dict[tuple[str, str], int]:
    if not BASELINE_PATH.exists():
        return {}
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"diction_baseline.json 不是合法 JSON：{e}") from e
    allowed: dict[tuple[str, str], int] = {}
    for rel, patterns in data.get("counts", {}).items():
        for pattern, count in patterns.items():
            allowed[(rel, pattern)] = int(count)
    return allowed


def write_baseline(found) -> None:
    counts: dict[str, dict[str, int]] = {}
    for (rel, pattern), matches in sorted(found.items()):
        counts.setdefault(rel, {})[pattern] = len(matches)
    data = {
        "note": "diction_lint 基线：存量违例的 {路径: {正则: 数量}}，数量只许减少；"
        "清理后跑 scripts/diction_lint.py --update-baseline 重新生成 diction_baseline.json。不要手工更改这个文件来放宽限制。",
        "counts": counts,
    }
    BASELINE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def new_violations(found, allowed):
    over = []
    for key, matches in sorted(found.items()):
        limit = allowed.get(key, 0)
        if len(matches) > limit:
            over.append((key, matches, limit))
    return over


def format_violations(over) -> list[str]:
    out = []
    for (rel, _pattern), matches, limit in over:
        note = f"（基线允许 {limit} 处，现 {len(matches)} 处）" if limit else ""
        _, _, hint = matches[0]
        out.append(f"{rel}: 「{matches[0][1]}」×{len(matches)} — {hint}{note}")
        for lineno, text, _ in matches[:5]:
            out.append(f"  {rel}:{lineno}: {text}")
    return out


def run_scan(paths: list[str], use_baseline: bool) -> int:
    rules = load_rules()
    if paths:
        rels = []
        for p in paths:
            try:
                rel = str(Path(p).resolve().relative_to(REPO))
            except ValueError as e:
                raise SystemExit(f"不在仓库内：{p}") from e
            if lintable(rel):
                rels.append(rel)
    else:
        rels = tracked_files()
    found = scan(rels, rules)
    allowed = load_baseline() if use_baseline else {}
    over = new_violations(found, allowed)
    if over:
        kind = "新增" if use_baseline else ""
        print(f"措辞{kind}违例（规则见 CLAUDE.md「行文与命名规则」）：")
        print("\n".join(format_violations(over)))
        return 1
    print("diction_lint：无" + ("新增违例" if use_baseline else "违例"))
    return 0


def run_hook() -> int:
    try:
        event = json.load(sys.stdin)
        file_path = (event.get("tool_input") or {}).get("file_path")
        if not file_path:
            return 0
        resolved = Path(file_path).resolve()
        try:
            rel = str(resolved.relative_to(REPO))
        except ValueError:
            return 0  # 仓库之外的文件不归本检查管
        if not lintable(rel) or not resolved.exists():
            return 0
        rules = load_rules()
        found = scan([rel], rules)
        over = new_violations(found, load_baseline())
        if not over:
            return 0
        print("diction_lint 检出措辞新增违例，请改写后重新写入：", file=sys.stderr)
        print("\n".join(format_violations(over)), file=sys.stderr)
        return 2
    except SystemExit as e:  # denylist / 基线本身的格式错误也反馈给模型修
        print(f"diction_lint 配置错误：{e}", file=sys.stderr)
        return 2
    except Exception as e:  # hook 自身故障不应拦住写入
        print(f"diction_lint hook 内部错误（已放行）：{e}", file=sys.stderr)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="只检查这些文件；缺省为全仓 git ls-files")
    parser.add_argument("--all", action="store_true", help="忽略基线，报告全部存量违例")
    parser.add_argument("--update-baseline", action="store_true", help="以当前扫描结果重写基线")
    parser.add_argument("--hook", action="store_true", help="PostToolUse hook 入口（stdin 读事件 JSON）")
    args = parser.parse_args()

    if args.hook:
        return run_hook()
    if args.update_baseline:
        found = scan(tracked_files(), load_rules())
        write_baseline(found)
        print(f"基线已重写：{len(found)} 个（文件×模式）条目 → {BASELINE_PATH.relative_to(REPO)}")
        return 0
    return run_scan(args.files, use_baseline=not args.all)


if __name__ == "__main__":
    sys.exit(main())
