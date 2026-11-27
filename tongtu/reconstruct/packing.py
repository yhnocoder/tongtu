from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from .crops import CROPS_DIRNAME, crop_path, open_crop
from .latex import INLINE_CROP_MACRO
from .rebuild import MAIN_FILENAME

PACKED_CROPS_FILENAME = "crops.pdf"

CROP_REFERENCE_RE = re.compile(
    r"\\includegraphics(?:\[(?P<options>[^\]]*)\])?\{" + CROPS_DIRNAME + r"/(?P<graphic>[^}#]+?)(?:\.pdf)?\}"
    r"|\\im\{(?P<inline>[^}]+)\}"
)

PACKED_INLINE_MACRO = (
    r"\newcommand\im[3]{\raisebox{-#2}{\includegraphics[page=\csname crop@#1\endcsname]{"
    + PACKED_CROPS_FILENAME
    + "}}}"
)

BEGIN_DOCUMENT = r"\begin{document}"


def referenced_crops(tex: str) -> list[str]:
    names: dict[str, None] = {}
    for match in CROP_REFERENCE_RE.finditer(tex):
        names.setdefault(match.group("graphic") or match.group("inline"), None)
    return list(names)


def packed_source(tex: str, pages: dict[str, int]) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group("inline") is not None:
            return match.group(0)
        options = ",".join(part for part in (f"page={pages[match.group('graphic')]}", match.group("options")) if part)
        return f"{{\\includegraphics[{options}]{{{PACKED_CROPS_FILENAME}}}}}"

    tex = CROP_REFERENCE_RE.sub(replace, tex)
    tex = tex.replace(INLINE_CROP_MACRO, PACKED_INLINE_MACRO)
    definitions = "\n".join(
        f"\\expandafter\\def\\csname crop@{name}\\endcsname{{{page}}}" for name, page in pages.items()
    )
    return tex.replace(BEGIN_DOCUMENT, definitions + "\n" + BEGIN_DOCUMENT, 1)


def missing_crops(tree: Path) -> list[str]:
    tex = (tree / MAIN_FILENAME).read_text(encoding="utf-8")
    return [name for name in referenced_crops(tex) if not (tree / crop_path(name)).is_file()]


def pack(tree: Path, out: Path) -> None:
    tex = (tree / MAIN_FILENAME).read_text(encoding="utf-8")
    names = referenced_crops(tex)
    out.mkdir(parents=True, exist_ok=True)
    if names:
        packed = pymupdf.open()
        for name in names:
            packed.insert_pdf(open_crop(tree / crop_path(name)))
        packed.save(out / PACKED_CROPS_FILENAME, garbage=4, deflate=True, deflate_images=True)
    pages = {name: index for index, name in enumerate(names, start=1)}
    (out / MAIN_FILENAME).write_text(packed_source(tex, pages), encoding="utf-8")
