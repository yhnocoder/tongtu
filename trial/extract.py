import json, re, shutil, sys, urllib.request
from pathlib import Path
import pymupdf
from progress import bar
from tongtu.reconstruct.crops import crop_path, open_crop
from tongtu.reconstruct.rebuild import MAIN_FILENAME, rebuild

FIGURE_RE = re.compile(r"\\begin\{(figure\*?|wrapfigure)\}.*?\\end\{\1\}", re.S)
CAPTION_RE = re.compile(r"\\caption\[\]\{(.*)")
CROP_RE = re.compile(r"crops/(r-\d+)\.pdf")


def complete(pdf):
    if not pdf.exists():
        return False
    try:
        return pdf.read_bytes().rstrip().endswith(b"%%EOF") and len(pymupdf.open(pdf)) > 0
    except Exception:
        return False


def download(arxiv_id, pdf):
    if complete(pdf):
        return
    request = urllib.request.Request(f"https://arxiv.org/pdf/{arxiv_id}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=300) as response:
        data = response.read()
    partial = pdf.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(pdf)
    if not complete(pdf):
        raise SystemExit(f"{arxiv_id}: the download is not a complete PDF ({len(data)} bytes, starts with {data[:20]!r})")


def extract(pdf, out):
    tree = out / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdf, tree / "original.pdf")
    rebuild(tree / "original.pdf", tree)
    tex = (tree / MAIN_FILENAME).read_text()
    images = out / "images"
    images.mkdir(exist_ok=True)
    items = []
    for figure in FIGURE_RE.finditer(tex):
        caption = CAPTION_RE.search(figure.group(0))
        if not caption:
            continue
        text = re.sub(r"\\[a-zA-Z]+|[{}]", "", caption.group(1)).strip()
        kind = re.match(r"(Algorithm|Listing)", text)
        if not kind:
            continue
        for key in CROP_RE.findall(figure.group(0)):
            page = open_crop(tree / crop_path(key))[0]
            zoom = max(2, min(4, 1400 / page.rect.width))
            page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(images / f"{key}.png")
            items.append({"key": key, "kind": kind.group(1).lower(), "caption": text[:80], "words": len(page.get_text().split())})
    (out / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))
    return items


work, papers = Path(sys.argv[1]), sys.argv[2:]
todo = [p for p in papers if not (work / p.split(":")[0] / "items.json").exists()]
(work / "pdfs").mkdir(parents=True, exist_ok=True)
with bar() as progress:
    task = progress.add_task("下载并重建论文", total=len(todo))
    for paper in todo:
        name, arxiv_id = paper.split(":", 1)
        pdf = work / "pdfs" / f"{name}.pdf"
        progress.update(task, description=f"下载 {name}")
        download(arxiv_id, pdf)
        progress.update(task, description=f"重建 {name}（{len(pymupdf.open(pdf))} 页）")
        items = extract(pdf, work / name)
        progress.console.print(f"{name}: {sum(it['kind'] == 'algorithm' for it in items)} 个算法块，"
                               f"{sum(it['kind'] == 'listing' for it in items)} 个代码块")
        progress.advance(task)
    progress.update(task, description="下载并重建论文")
