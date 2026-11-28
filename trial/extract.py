import json, re, shutil, sys
from pathlib import Path
import pymupdf
from tongtu.reconstruct.crops import crop_path, open_crop
from tongtu.reconstruct.rebuild import MAIN_FILENAME, rebuild

FIGURE_RE = re.compile(r"\\begin\{(figure\*?|wrapfigure)\}.*?\\end\{\1\}", re.S)
CAPTION_RE = re.compile(r"\\caption\[\]\{(.*)")
CROP_RE = re.compile(r"crops/(r-\d+)\.pdf")

pdf, out = Path(sys.argv[1]), Path(sys.argv[2])
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
print(out.name, len(items), "blocks:", ", ".join(f"{it['key']} {it['caption'][:30]}" for it in items))
