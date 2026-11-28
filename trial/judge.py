import collections, difflib, functools, json, os, statistics, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pymupdf
from progress import bar
from tongtu.reconstruct import recognition, table
from tongtu.reconstruct.crops import crop_path, open_crop
import glyphs, render

SCRIPT_TOLERANCE = 0.25
SIZE_TOLERANCE = 0.15
INDENT_TOLERANCE = 0.4


def levels(lines):
    found = []
    for line in sorted(lines, key=lambda line: line.x):
        if not found or line.x - found[-1] > INDENT_TOLERANCE * line.size:
            found.append(line.x)
    return lambda line: min(range(len(found)), key=lambda i: abs(found[i] - line.x))


def pairs(original, rendered):
    matcher = difflib.SequenceMatcher(None, [line.text for line in original], [line.text for line in rendered], autojunk=False)
    found = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op in ("equal", "replace"):
            found += list(zip(range(i1, i2), range(j1, j2)))
    return found


def misplaced(a, b):
    left = list(b.chars)
    wrong = ""
    for char in a.chars:
        same = [other for other in left if other.char == char.char]
        if not same:
            continue
        best = min(same, key=lambda other: abs(other.dy - char.dy) + abs(other.size - char.size))
        left.remove(best)
        if abs(best.dy - char.dy) > SCRIPT_TOLERANCE or abs(best.size - char.size) > SIZE_TOLERANCE:
            wrong += char.char
    return wrong


def bold_differences(a, b):
    left = list(b.chars)
    count = 0
    for char in a.chars:
        same = [other for other in left if other.char == char.char]
        if same:
            best = next((other for other in same if other.bold == char.bold), same[0])
            left.remove(best)
            count += best.bold != char.bold
    return count


def compare(kind, original, rendered):
    a = collections.Counter(char.char for line in original for char in line.chars)
    b = collections.Counter(char.char for line in rendered for char in line.chars)
    common = sum((a & b).values())
    result = {"chars": round(2 * common / max(1, sum(a.values()) + sum(b.values())), 4), "text_ok": a == b, "problems": []}
    if a != b:
        missing, extra = "".join((a - b).elements()), "".join((b - a).elements())
        result["problems"].append(f"原文有而结果没有: {missing[:60]!r}；结果多出: {extra[:60]!r}")
    matched = pairs(original, rendered)
    scripts = "" if kind == "listing" else "".join(misplaced(original[i], rendered[j]) for i, j in matched)
    result["script_ok"] = not scripts
    if scripts:
        result["problems"].append("上下标或字号不同: " + scripts[:40])
    result["bold_diff"] = sum(bold_differences(original[i], rendered[j]) for i, j in matched)
    level_a, level_b = levels(original), levels(rendered)
    wrong = [original[i].text[:14] for i, j in matched if level_a(original[i]) != level_b(rendered[j])]
    result["indent_ok"] = not wrong
    if wrong:
        result["problems"].append("缩进层级不同的行: " + " | ".join(wrong[:6]))
    same = sum(original[i].text == rendered[j].text for i, j in matched)
    result["lines"] = f"{same}/{len(original)}"
    result["lines_ok"] = same == len(original) == len(rendered)
    if not result["lines_ok"]:
        result["problems"].append(f"逐行一致 {result['lines']}，结果共 {len(rendered)} 行")
    result["passed"] = result["text_ok"] and result["script_ok"] and result["indent_ok"] and result["lines_ok"]
    return result


def read_rendered(kind, page):
    rect = pymupdf.Rect()
    for block in page.get_text("dict")["blocks"]:
        rect |= block["bbox"]
    for drawing in page.get_drawings():
        rect |= drawing["rect"]
    clip = (rect + (-6, -6, 6, 6)) & page.rect
    zoom = max(2, min(4, 1400 / max(1, clip.width)))
    png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip).tobytes("png")
    return glyphs.lines(page, kind), png


def judge(paper, item, text):
    start = time.time()
    crop = open_crop(paper / "tree" / crop_path(item["key"]))[0]
    original = glyphs.lines(crop, item["kind"])
    if not original:
        verdict = {"passed": False, "problems": ["截图没有文字层，无法比对"], "chars": 0}
    elif not text.strip():
        verdict = {"passed": False, "problems": ["没有输出"], "chars": 0}
    else:
        size = statistics.median(glyph.size for glyph in table.page_glyphs(crop))
        source = render.document(item["kind"], text, crop.rect.width, size)
        outcome = recognition.compile_snippet(source, table.COMPILE_COMMAND, functools.partial(read_rendered, item["kind"]))
        if isinstance(outcome, str):
            verdict = {"passed": False, "problems": ["编译失败: " + outcome[:300]], "chars": 0}
        else:
            rendered, png = outcome
            verdict = compare(item["kind"], original, rendered)
            verdict["png"] = png.hex()
    verdict["verify_seconds"] = time.time() - start
    return item["key"], verdict


if __name__ == "__main__":
    label = sys.argv[1]
    progress = bar(stderr=True)
    progress.start()
    with ProcessPoolExecutor(int(os.environ.get("VERIFY_JOBS") or os.cpu_count())) as pool:
        for paper in map(Path, sys.argv[2:]):
            out = paper / f"out_{label}.json"
            if not out.exists():
                continue
            results = json.loads(out.read_text())
            items = [it for it in json.loads((paper / "items.json").read_text()) if it["key"] in results]
            task = progress.add_task(f"比对 {label} {paper.name}", total=len(items))
            start = time.time()
            futures = [pool.submit(judge, paper, it, results[it["key"]]["text"]) for it in items]
            verdicts = {}
            for future in as_completed(futures):
                key, verdict = future.result()
                verdicts[key] = verdict
                progress.advance(task)
            scale = (time.time() - start) / (sum(v["verify_seconds"] for v in verdicts.values()) or 1)
            render_dir = paper / f"render_{label}"
            render_dir.mkdir(exist_ok=True)
            for key, verdict in verdicts.items():
                verdict["verify_wall"] = round(verdict["verify_seconds"] * scale, 3)
                png = verdict.pop("png", None)
                if png:
                    (render_dir / f"{key}.png").write_bytes(bytes.fromhex(png))
            ordered = {it["key"]: verdicts[it["key"]] for it in items}
            (paper / f"verdict_{label}.json").write_text(json.dumps(ordered, ensure_ascii=False, indent=1))
    progress.stop()
