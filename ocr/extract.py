import json, sys, shutil
from pathlib import Path
import pymupdf
from tongtu.reconstruct.rebuild import rebuild, MAIN_FILENAME
from tongtu.reconstruct import formula, table
from tongtu.reconstruct.crops import crop_path, open_crop

pdf, out = Path(sys.argv[1]), Path(sys.argv[2])
tree = out / "tree"; tree.mkdir(parents=True, exist_ok=True)
orig = tree / "original.pdf"; shutil.copyfile(pdf, orig)
regions = rebuild(orig, tree)
tex = (tree / MAIN_FILENAME).read_text()
fk = formula.recognizable(tex, regions)
tk = table.recognizable(tex, regions)
img = out / "images"; img.mkdir(exist_ok=True)
items = []
for key, kind in fk.items():
    page = open_crop(tree / crop_path(key))[0]
    zoom = max(3, min(8, 120 / max(1, page.rect.height)))
    page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(img / f"{key}.png")
    items.append({"key": key, "type": "formula", "kind": kind.value})
for key in tk:
    page = open_crop(tree / crop_path(key))[0]
    page.get_pixmap(matrix=pymupdf.Matrix(3, 3)).save(img / f"{key}.png")
    items.append({"key": key, "type": "table"})
(out / "items.json").write_text(json.dumps(items, indent=1))
from collections import Counter
print(Counter((i["type"], i.get("kind")) for i in items))
