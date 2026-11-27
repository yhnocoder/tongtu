from __future__ import annotations

import collections
import re
import statistics
import unicodedata
from pathlib import Path

import pymupdf

from .. import processes
from ..artifacts.reconstruct import RegionKind
from .crops import Cropper, crop_path
from .layout import PROSE_RE, Char, Item, Layout, Line, family

INLINE_CROP_MACRO = r"\newcommand\im[3]{\raisebox{-#2}{\includegraphics{crops/#1}}}"

FOOTNOTE_OPEN = "\\footnote{"

ESCAPES: dict[str, str] = {
    "∆": r"\ensuremath{\Delta}",
    "∇": r"\ensuremath{\nabla}",
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
}

CJK_FONT = "FandolSong-Regular.otf"

FALLBACK_FONT = "DejaVuSans.ttf"

MATH_PACKAGES: tuple[tuple[str, str], ...] = (
    (r"XCharter", "[xcharter]{newtxmath}"),
    (r"Libertin", "[libertine]{newtxmath}"),
    (r"^(ntx|txmi|rtxmi|NewTXMI|txsy)", "{newtxmath}"),
    (r"^(pxmi|zplm|NewPXMI|pxsy)", "{newpxmath}"),
)

FLOAT_PLACEMENT = (
    "\\renewcommand\\topfraction{0.95}\\renewcommand\\bottomfraction{0.8}\\renewcommand\\textfraction{0.05}"
    "\\renewcommand\\floatpagefraction{0.85}\\renewcommand\\dbltopfraction{0.95}"
    "\\renewcommand\\dblfloatpagefraction{0.85}\\setcounter{topnumber}{4}\\setcounter{totalnumber}{6}"
    "\\setcounter{dbltopnumber}{4}"
)

GYRE_SHAPES = "[Extension=.otf,UprightFont=*-regular,BoldFont=*-bold,ItalicFont=*-italic,BoldItalicFont=*-bolditalic]"

FLOATING_KINDS = ("caption", "caption+", "footnote", "header", "footer", "title")

TABLE_CAPTION_RE = re.compile(r"^(Table|TABLE|Tab\.)")

TWOCOLUMN_FIGURE_RE = re.compile(r"\\begin\{figure\*\}\[htbp\]\n\\centering(.*)\n\\end\{figure\*\}")

Piece = str | list[Char] | None

TOOL_TIMEOUT_SECONDS = 30


def is_cjk(code: int) -> bool:
    return 0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF or 0xFF00 <= code <= 0xFFEF


class ToolError(Exception):
    pass


def tool_output(command: list[str]) -> str:
    try:
        outcome = processes.run_in_process_group(command, Path.cwd(), TOOL_TIMEOUT_SECONDS)
    except FileNotFoundError as error:
        raise ToolError(f"the command {command[0]} was not found") from error
    if outcome.timed_out:
        raise ToolError(f"{' '.join(command)} did not finish within {TOOL_TIMEOUT_SECONDS} seconds")
    return outcome.stdout.decode("utf-8", errors="replace")


def font_list(code: int) -> list[str]:
    listed = tool_output(["fc-list", f":charset={code:x}", "file"]).split()
    return sorted(listed, key=lambda path: "unifont" in path)


def kpsewhich(name: str) -> str:
    return tool_output(["kpsewhich", name]).strip()


def is_mono(font: str) -> bool:
    return "Mon" in font or "CMTT" in font


def is_math(piece: Piece) -> bool:
    return isinstance(piece, Char) and piece.math


def is_script(piece: Piece, reference: Char) -> bool:
    return (
        isinstance(piece, Char)
        and not piece.c.isspace()
        and piece.size < reference.size * 0.85
        and piece.line is reference.line
    )


def small(char: Char) -> bool:
    return char.size < char.line.size * 0.8


def region_kind(item: Item, equation: bool) -> tuple[str, RegionKind]:
    if equation:
        return "e", RegionKind.DISPLAY
    if item.kind == "title":
        return "t", RegionKind.AUTHORS
    if item.kind == "refs":
        return "b", RegionKind.REFERENCES
    if item.caption and TABLE_CAPTION_RE.match(item.caption[0].text):
        return "r", RegionKind.TABLE
    if item.caption and item.caption[0].text.startswith("Algorithm"):
        return "r", RegionKind.ALGORITHM
    return "r", RegionKind.FIGURE


class LatexWriter:
    def __init__(self, layout: Layout, cropper: Cropper) -> None:
        self.layout = layout
        self.cropper = cropper
        self.fonts: dict[str, pymupdf.Font] = {}
        self.fallback_fonts: list[str] = []
        self.body_font: pymupdf.Font | None = None
        self.cjk = False
        self.cjk_fallback: str | None = None
        self.uses_strip = False
        self.math_package_name: str | None = None
        self.heading_styles: dict[str, list[Line]] = collections.defaultdict(list)
        self.footnotes: list[list[Line]] = []
        self.footnote_by_mark: dict[tuple[int, str], int] = {}
        self.footnotes_used: set[int] = set()

    def document(self) -> str:
        layout = self.layout
        body = self.emit()
        if layout.twocol:
            body = self.twocolumn_heads(body)
        return (
            self.preamble()
            + "\n\\begin{document}\n"
            + "\n\n".join(part for part in body if part)
            + "\n\\end{document}\n"
        )

    def floating(self) -> list[tuple[int, pymupdf.Rect, str]]:
        return [(line.page, line.bbox, line.kind) for line in self.layout.lines if line.kind in FLOATING_KINDS]

    def twocolumn_heads(self, body: list[str]) -> list[str]:
        layout = self.layout
        items = layout.items

        def flatten(head: str) -> str:
            head = re.sub(r"^\\twocolumn\n*", "", head)
            return TWOCOLUMN_FIGURE_RE.sub(r"\\begin{center}\1\\end{center}", head)

        starts: dict[int, int] = {}
        for index, item in enumerate(items):
            starts.setdefault(item.page, index)
        new_body = list(body)
        for page_number, first in sorted(starts.items(), reverse=True):
            end = first
            while (
                end < len(items)
                and items[end].page == page_number
                and items[end].col == 2
                and not items[end].caption
                and items[end].kind != "caption"
            ):
                end += 1
            if end == first or page_number in layout.onecol_pages:
                continue
            if page_number > 0 and not any(items[index].kind in ("heading", "title") for index in range(first, end)):
                continue
            head = [flatten(part) for part in new_body[first:end]]
            joined = "\n\n".join(part for part in head if part)
            new_body[first:end] = [f"\\twocolumn[{joined}\n\\vspace{{{layout.leading:.1f}bp}}]"] + [""] * (
                end - first - 1
            )
        return new_body

    def esc(self, text: str) -> str:
        return "".join(ESCAPES.get(char) or self.glyph(char) for char in text if char >= " " or char == "\n")

    def main_font(self) -> pymupdf.Font:
        if self.body_font is None:
            body_family = self.layout.body_family
            if "Nimbus" in body_family or "Times" in body_family:
                name = "texgyretermes-regular.otf"
            elif re.search(r"Palladio|Pagella|Palatino", body_family):
                name = "texgyrepagella-regular.otf"
            elif "Libertine" in body_family:
                name = "LinLibertine_R.otf"
            else:
                name = "lmroman10-regular.otf"
            self.body_font = pymupdf.Font(fontfile=kpsewhich(name))
        return self.body_font

    def glyph(self, char: str) -> str:
        code = ord(char)
        if char in " \n" or code < 128:
            return char
        font = self.main_font()
        if is_cjk(code):
            self.cjk = True
            if self.font_file(CJK_FONT).has_glyph(code):
                return char
            for path in font_list(code):
                self.cjk_fallback = self.cjk_fallback or path.rstrip(":")
                break
            return char
        if font.has_glyph(code):
            return char
        name = unicodedata.name(char, "")
        if name.startswith("MATHEMATICAL") or char == "ℎ":
            base = unicodedata.normalize("NFKC", char)
            if base.isascii() and base.isalnum():
                return self.math_letter(name, base)
        return self.fallback_glyph(char)

    def math_letter(self, name: str, base: str) -> str:
        if "DOUBLE-STRUCK" in name:
            return f"\\ensuremath{{\\mathbb{{{base}}}}}"
        if "SCRIPT" in name:
            return f"\\ensuremath{{\\mathcal{{{base}}}}}"
        if "BOLD" in name and "ITALIC" in name:
            return f"\\ensuremath{{\\bm{{{base}}}}}"
        if "BOLD" in name:
            return f"\\textbf{{{base}}}"
        if "ITALIC" in name:
            return f"\\ensuremath{{{base}}}" if base.isalpha() else base
        if "MONOSPACE" in name:
            return f"\\texttt{{{base}}}"
        if "SANS-SERIF" in name:
            return f"\\textsf{{{base}}}"
        return base

    def font_file(self, name: str) -> pymupdf.Font:
        if name not in self.fonts:
            path = name if name.startswith("/") else kpsewhich(name)
            self.fonts[name] = pymupdf.Font(fontfile=path)
        return self.fonts[name]

    def fallback_glyph(self, char: str) -> str:
        code = ord(char)
        for index, path in enumerate(self.fallback_fonts):
            if self.font_file(path).has_glyph(code):
                return f"{{\\fallback{chr(65 + index)} {char}}}"
        for candidate in [FALLBACK_FONT, *font_list(code)]:
            path = candidate.rstrip(":")
            try:
                if self.font_file(path).has_glyph(code):
                    self.fallback_fonts.append(path)
                    return f"{{\\fallback{chr(64 + len(self.fallback_fonts))} {char}}}"
            except Exception:
                continue
        return char

    def joined_chars(self, lines: list[Line]) -> list[Piece]:
        chars: list[Piece] = []
        for line in lines:
            own = list(line.chars)
            while own and own[-1].c.isspace():
                own.pop()
            while own and own[0].c.isspace():
                own.pop(0)
            if chars:
                tail = "".join(
                    piece if isinstance(piece, str) else ("" if piece is None else piece.c) for piece in chars[-3:]
                )
                hyphenated = tail.endswith("-") and own and len(tail) > 1 and tail[-2].isalpha()
                if hyphenated and own[0].c.islower():
                    chars.pop()
                    chars.append(None)
                elif hyphenated and own[0].c.isupper():
                    chars.append(None)
                else:
                    chars.append(" ")
            for char in own:
                char.line = line
            chars += own
        return chars

    def inline(self, lines: list[Line], allow_math: bool = True) -> str:
        layout = self.layout
        chars = self.joined_chars(lines)
        count = len(chars)
        out: list[str | list[Char]] = []

        def glyph_at(index: int) -> Char | None:
            piece = chars[index] if 0 <= index < count else None
            return piece if isinstance(piece, Char) else None

        for index in range(1, count):
            current, previous, before = glyph_at(index), glyph_at(index - 1), glyph_at(index - 2)
            if (
                current is not None
                and previous is not None
                and current.math
                and not previous.math
                and previous.bold
                and previous.c.isalpha()
                and previous.line is current.line
                and current.bbox[0] - previous.bbox[2] < 0.6
                and not (before is not None and before.c.isalnum())
            ):
                previous.math = True
        position = 0
        while position < count:
            piece = chars[position]
            if piece is None:
                position += 1
                continue
            if isinstance(piece, str):
                out.append(piece)
                position += 1
                continue
            if allow_math and is_math(piece):
                position = self.math_run(chars, position, out)
                continue
            if small(piece) and piece.origin[1] < layout.baseline(piece.line) - 1.5 and not piece.c.isspace():
                end = position
                mark = ""
                while end < count:
                    char = glyph_at(end)
                    if char is None or not small(char) or char.c.isspace():
                        break
                    mark += char.c
                    end += 1
                footnote = self.footnote_by_mark.get((piece.line.page, mark))
                if footnote is not None and footnote not in self.footnotes_used:
                    self.footnotes_used.add(footnote)
                    out.append(FOOTNOTE_OPEN + self.render_text(self.footnotes[footnote], strip_mark=True) + "}")
                else:
                    out.append("\\textsuperscript{" + self.esc(mark) + "}")
                position = end
                continue
            style = (piece.bold, piece.italic, is_mono(piece.font))
            end = position
            buffer = ""
            while end < count:
                char = glyph_at(end)
                if (
                    char is None
                    or (allow_math and is_math(char))
                    or not (char.c.isspace() or (char.bold, char.italic, is_mono(char.font)) == style)
                    or (small(char) and not char.c.isspace())
                ):
                    break
                buffer += char.c
                end += 1
            text = self.esc(buffer)
            if style[2]:
                text = "\\texttt{" + text + "}"
            elif style[0] and style[1]:
                text = "\\textbf{\\textit{" + text + "}}"
            elif style[0]:
                text = "\\textbf{" + text + "}"
            elif style[1]:
                text = "\\textit{" + text + "}"
            if end > position:
                out.append(text)
                position = end
            else:
                out.append(self.esc(piece.c))
                position += 1
        rendered = "".join(self.math_crop(part) if isinstance(part, list) else part for part in out)
        rendered = re.sub(r"\\textit\{( *)\}", r"\1", rendered)
        rendered = re.sub(r" +", " ", rendered)
        return rendered.strip()

    def math_run(self, chars: list[Piece], position: int, out: list[str | list[Char]]) -> int:
        layout = self.layout
        count = len(chars)
        end = position
        run: list[Char] = []
        while end < count:
            piece = chars[end]
            if is_math(piece):
                run.append(piece)
                end += 1
                continue
            if not isinstance(piece, Char) or not run:
                break
            following = chars[end + 1] if end + 1 < count else None
            if (is_script(piece, max(run, key=lambda char: char.size)) and piece.bbox[0] - run[-1].bbox[2] < 1.5) or (
                piece.c.isspace() and end + 1 < count and is_math(following) and following.line is piece.line
            ):
                if not piece.c.isspace():
                    run.append(piece)
                end += 1
                continue
            if not piece.c.isspace() and family(piece.font) not in layout.text_families and piece.line is run[-1].line:
                run.append(piece)
                end += 1
                continue
            upcoming = next(
                (
                    chars[k]
                    for k in range(end + 1, min(count, end + 4))
                    if isinstance(chars[k], Char) and not chars[k].c.isspace()
                ),
                None,
            )
            if (
                not piece.c.isalpha()
                and piece.line is run[-1].line
                and upcoming is not None
                and is_math(upcoming)
                and upcoming.line is piece.line
                and upcoming.bbox[0] - run[-1].bbox[2] < 1.2 * layout.body_size
                and piece.c not in ",;:"
            ):
                run.append(piece)
                end += 1
                continue
            break
        if run and all(
            re.match(r"CMR|LMRoman", char.font) and char.c in "0123456789.,%" and abs(char.size - run[0].size) < 0.3
            for char in run
        ):
            out.append("".join(char.c for char in run).replace("%", "\\%"))
            return end
        last = len(out) - 1
        while last >= 0 and isinstance(out[last], str) and out[last].strip() == "":
            last -= 1
        if last >= 0 and isinstance(out[last], list) and self.continues_run(out[last], run):
            out[last] += run
            del out[last + 1 :]
        else:
            out.append(run)
        return end

    def continues_run(self, earlier: list[Char], run: list[Char]) -> bool:
        line = run[0].line
        return (
            earlier[-1].line.page == line.page
            and min(char.bbox[0] for char in earlier) <= run[0].bbox[0]
            and run[0].bbox[0] - max(char.bbox[2] for char in earlier) < 0.3 * line.size
            and abs((run[0].bbox[1] + run[0].bbox[3]) - (earlier[-1].bbox[1] + earlier[-1].bbox[3])) < 2 * line.size
        )

    def math_crop(self, run: list[Char]) -> str:
        layout = self.layout
        line = run[0].line
        page_number = line.page
        rect = pymupdf.Rect(run[0].bbox)
        for char in run:
            rect |= pymupdf.Rect(char.bbox)
        base = layout.baseline(line)
        rect = pymupdf.Rect(
            rect.x0 - 0.3,
            min(rect.y0, base - line.size * 0.75) - 0.8,
            rect.x1 + 0.3,
            max(rect.y1, base + line.size * 0.25) + 0.5,
        )
        overlapping = [
            layout.baseline(other)
            for other in layout.lines
            if other.page == page_number and other is not line and other.bbox.x1 > rect.x0 and other.bbox.x0 < rect.x1
        ]
        above = [value for value in overlapping if value < base - 2]
        below = [value for value in overlapping if value > base + 2]
        if above:
            rect.y0 = max(rect.y0, max(above) + 0.23 * line.size)
        if below:
            rect.y1 = min(rect.y1, min(below) - 0.72 * line.size)
        hint = "".join(char.c for char in run)
        name = self.cropper.crop(page_number, rect, "m", RegionKind.INLINE, hint)
        depth = rect.y1 - base
        return f"\\im{{{name}}}{{{depth:.2f}bp}}{{{self.esc(hint).replace(' ', '')}}}"

    def render_text(self, lines: list[Line], strip_mark: bool = False) -> str:
        if strip_mark and lines:
            first = lines[0]
            index = 0
            while (
                index < len(first.chars)
                and (first.chars[index].size < first.size * 0.85 or first.chars[index].c.isspace())
                and not first.chars[index].c.isalpha()
                or index < len(first.chars)
                and first.chars[index].size < first.size * 0.85
            ):
                index += 1
            stripped = Line(
                first.page, first.bbox, first.chars[index:], first.kind, first.col, first.text, first.size, first.font
            )
            lines = [stripped] + lines[1:]
        return self.inline(lines)

    def split_footnotes(self, text: str) -> tuple[str, list[str]]:
        notes = []
        while True:
            start = text.find(FOOTNOTE_OPEN)
            if start < 0:
                return text, notes
            end = start + len(FOOTNOTE_OPEN)
            depth = 1
            while end < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[end], 0) if text[end - 1] != "\\" else 0
                end += 1
            notes.append(text[start + len(FOOTNOTE_OPEN) : end - 1])
            text = text[:start] + "\\footnotemark{}" + text[end:]

    def prepare_footnotes(self) -> None:
        layout = self.layout
        for page_number in range(len(layout.doc)):
            page_lines = [line for line in layout.lines if line.page == page_number and line.kind == "footnote"]
            current = None
            for line in sorted(page_lines, key=lambda line: (line.col, line.bbox.y0)):
                first = next((char for char in line.chars if not char.c.isspace()), None)
                if first is not None and first.size < line.size * 0.85:
                    mark = ""
                    for char in line.chars:
                        if char.c.isspace():
                            continue
                        if char.size < line.size * 0.85:
                            mark += char.c
                        else:
                            break
                    current = len(self.footnotes)
                    self.footnotes.append([line])
                    self.footnote_by_mark[(page_number, mark)] = current
                elif current is not None:
                    self.footnotes[current].append(line)
                else:
                    current = len(self.footnotes)
                    self.footnotes.append([line])

    def emit(self) -> list[str]:
        layout = self.layout
        out: list[str] = []
        self.prepare_footnotes()
        one_column = False
        marks: list[int] = []
        for item in layout.items:
            if marks:
                out[marks[-1] :] = ["\n\n".join(part for part in out[marks[-1] :] if part)]
            marks.append(len(out))
            if layout.twocol and (item.page in layout.onecol_pages) != one_column:
                one_column = not one_column
                out.append("\\onecolumn" if one_column else "\\twocolumn")
            if item.kind == "heading":
                self.emit_heading(item, out)
            elif item.kind == "body" and self.is_toc(item):
                self.emit_toc(item, out)
            elif item.kind == "body":
                out.append(self.paragraph(item))
            elif item.kind in ("region", "title", "refs"):
                self.emit_region(item, out)
            elif item.kind == "caption":
                out.append("\\par\\noindent " + self.inline(item.lines))
            else:
                out.append("")
        if marks:
            out[marks[-1] :] = ["\n\n".join(part for part in out[marks[-1] :] if part)]
        self.page_breaks(out)
        self.leftover_footnotes(out)
        return out

    def emit_heading(self, item: Item, out: list[str]) -> None:
        layout = self.layout
        text = " ".join(line.text for line in item.lines)
        big = max(line.size for line in item.lines)
        if big > layout.body_size + 1.5 or re.match(r"^\d+\s", text) or text.isupper():
            command = "section"
        elif re.match(r"^\d+\.\d+\s", text) or big > layout.body_size + 0.5:
            command = "subsection"
        else:
            command = "subsubsection"
        if re.match(r"^\d+\.\d+\.\d+", text):
            command = "subsubsection"
        elif re.match(r"^\d+\.\d+", text) or re.match(r"^[A-Z]\.\d+", text):
            command = "subsection"
        low, high = layout.col_bounds(item.col)
        if re.match(r"^abstract$", text, re.I) and abs((item.rect.x0 + item.rect.x1) / 2 - (low + high) / 2) < 10:
            out.append("\\begin{center}\\textbf{\\large " + self.esc(text) + "}\\end{center}")
            return
        self.heading_styles[command].append(item.lines[0])
        out.append(f"\\{command}*{{{self.esc(text)}}}")

    def emit_toc(self, item: Item, out: list[str]) -> None:
        low, high = self.layout.col_bounds(item.col)
        for line in item.lines:
            text = self.inline([line]).strip()
            previous = next((index for index in range(len(out) - 1, -1, -1) if out[index]), None)
            heading = (
                re.fullmatch(r"\\(sub)*section\*\{(.*)\}", out[previous])
                if previous is not None and re.fullmatch(r"\d+", line.text)
                else None
            )
            if heading:
                out[previous] = f"\\par\\medskip\\noindent\\textbf{{{heading.group(2)}}}\\hfill {text}\\par"
                continue
            number = re.search(r"\s*((?:\\textbf\{)?\d+\}?)$", text)
            if not number or line.bbox.x1 < high - 6:
                out.append(f"\\noindent\\hspace*{{{line.bbox.x0 - low:.1f}bp}}{text}\\par")
                continue
            body = text[: number.start()]
            leader = bool(re.search(r"(?:\s*\.){3,}\s*$", body))
            body = re.sub(r"(?:\s*\.){3,}\s*$", "", body)
            fill = "\\dotfill " if leader else "\\hfill "
            out.append(f"\\noindent\\hspace*{{{line.bbox.x0 - low:.1f}bp}}{body}" + fill + number.group(1) + "\\par")

    def paragraph(self, item: Item) -> str:
        layout = self.layout
        low, high = layout.col_bounds(item.col)
        indent = item.lines[0].bbox.x0 - low
        text = self.inline(item.lines)
        prefix = "\\noindent " if indent < 3 else ""
        if item.lines[0].bbox.x0 - low > 4 * layout.body_size:
            prefix = f"\\noindent\\hspace*{{{indent:.1f}bp}}"
        text = re.sub(r" +\\footnote", r"\\footnote", text)
        size = statistics.median(line.size for line in item.lines)
        lefts = [line.bbox.x0 for line in item.lines[1:]] or [item.lines[0].bbox.x0]
        rights = [line.bbox.x1 for line in item.lines[:-1]] or [item.lines[-1].bbox.x1]
        left_skip = min(lefts) - low
        right_skip = high - max(rights)
        setup = ""
        if abs(size - layout.body_size) > 0.4:
            setup += f"\\fontsize{{{size:.2f}bp}}{{{size * layout.leading / layout.body_size:.2f}bp}}\\selectfont "
        hang = (
            len(item.lines) > 1
            and item.lines[0].bbox.x0 < min(lefts) - 3
            and all(abs(x - min(lefts)) < 3 for x in lefts)
        )
        if left_skip > 6 and (right_skip > 6 or hang) and len(item.lines) > 1:
            setup += f"\\leftskip={left_skip:.1f}bp\\rightskip={max(right_skip, 0) if right_skip > 6 else 0:.1f}bp "
            first_offset = item.lines[0].bbox.x0 - min(lefts)
            if not prefix and first_offset > 3:
                prefix = ""
            elif not prefix:
                prefix = "\\noindent "
            if prefix.startswith("\\noindent") and first_offset > 3:
                prefix = ""
            if hang:
                prefix = f"\\noindent\\hspace*{{{first_offset:.1f}bp}}"
        if setup:
            return setup + prefix + text + "\\par\\normalsize\\leftskip=0pt\\rightskip=0pt\\relax"
        return prefix + text

    def emit_region(self, item: Item, out: list[str]) -> None:
        layout = self.layout
        rect = item.rect + (-1.5, -1.5, 1.5, 1.5)
        low, high = layout.col_bounds(item.col)
        near = [
            line
            for line in layout.lines
            if line.page == item.page
            and line.kind not in ("header", "footer")
            and line.bbox.x1 > rect.x0
            and line.bbox.x0 < rect.x1
        ]
        above = [
            layout.baseline(line) + 0.25 * line.size
            for line in near
            if layout.baseline(line) < item.rect.y0 and layout.baseline(line) + 0.25 * line.size > rect.y0
        ]
        below = [
            layout.baseline(line) - 0.75 * line.size
            for line in near
            if layout.baseline(line) > item.rect.y1 and layout.baseline(line) - 0.75 * line.size < rect.y1
        ]
        if above:
            rect.y0 = max(above)
        if below:
            rect.y1 = min(below)
        equation = (
            item.kind == "region"
            and not item.caption
            and item.lines
            and all(line.kind == "display" or not PROSE_RE.search(line.text) for line in item.lines)
            and any(line.kind == "display" for line in item.lines)
            and not any(
                (box & item.rect).get_area() > 0.3 * item.rect.get_area()
                for box in layout.page_boxes.get(item.page, [])
            )
        )
        prefix, kind = region_kind(item, bool(equation))
        graphic = crop_path(self.cropper.crop(item.page, rect, prefix, kind))
        if item.caption:
            out.append(self.figure(item, graphic, out))
            return
        offset = max(0, rect.x0 - (low if not item.wide else layout.left))
        if item.kind == "refs":
            previous = next(
                (
                    other
                    for other in reversed(layout.items[: len(out)])
                    if other.kind == "refs" and other.page == item.page and other.col == item.col
                ),
                None,
            )
            gap = max(0.0, item.rect.y0 - previous.rect.y1 - 4) if previous else layout.leading * 0.5
            out.append(
                f"{{\\parskip={gap:.1f}bp\\par\\noindent\\hspace*{{{offset:.1f}bp}}\\includegraphics{{{graphic}}}\\par}}"
            )
        elif item.wide and layout.twocol and equation:
            out.append(f"\\begin{{strip}}\\centering\\includegraphics{{{graphic}}}\\end{{strip}}")
            self.uses_strip = True
        elif item.wide and layout.twocol:
            out.append(f"\\begin{{figure*}}[htbp]\n\\centering\\includegraphics{{{graphic}}}\n\\end{{figure*}}")
        elif abs((rect.x0 + rect.x1) / 2 - (low + high) / 2) < 8 and rect.width < high - low:
            out.append(f"\\par\\smallskip\\noindent\\hfil\\includegraphics{{{graphic}}}\\hfil\\par\\smallskip")
        else:
            out.append(
                f"\\par\\smallskip\\noindent\\hspace*{{{offset:.1f}bp}}\\includegraphics{{{graphic}}}\\par\\smallskip"
            )

    def figure(self, item: Item, graphic: str, out: list[str]) -> str:
        caption, notes = self.split_footnotes(self.inline(item.caption))
        environment = "figure*" if item.wide else "figure"
        include = f"\\centering\\includegraphics{{{graphic}}}"
        caption_command = f"\\caption[]{{{caption}}}"
        body = f"{caption_command}\n{include}" if item.caption_above else f"{include}\n{caption_command}"
        if item.caption_above and item.caption[0].text.startswith("Algorithm"):
            body = "\\hrule height 0.8pt\\smallskip\n" + body
        if item.wrap:
            if out and re.match(r"\\(sub)*section\*", out[-1]):
                out.append("\\leavevmode\\vspace*{-\\baselineskip}\\par")
            return f"\\begin{{wrapfigure}}{{{item.wrap[0]}}}{{{item.wrap[1]:.1f}bp}}\n{body}\n\\end{{wrapfigure}}"
        footnotes = "".join(f"\\footnotetext{{{note}}}" for note in notes)
        return f"\\begin{{{environment}}}[htbp]\n{body}\n\\end{{{environment}}}" + footnotes

    def page_breaks(self, out: list[str]) -> None:
        layout = self.layout
        items = layout.items
        last_page = max(item.page for item in items) if items else 0
        for page_number in range(last_page):
            indices = [index for index, item in enumerate(items) if item.page == page_number]
            if not indices:
                continue
            low = max(items[index].rect.y1 for index in indices)
            last = indices[-1]
            if low < layout.bottom - 0.25 * (layout.bottom - layout.top) and not items[last].caption:
                out[last] = out[last] + "\n\\clearpage"

    def leftover_footnotes(self, out: list[str]) -> None:
        layout = self.layout
        leftover = [index for index in range(len(self.footnotes)) if index not in self.footnotes_used]
        for index in reversed(leftover):
            page_number = self.footnotes[index][0].page
            position = next(
                (
                    k
                    for k, item in enumerate(layout.items)
                    if item.page == page_number and item.kind == "body" and not (layout.twocol and item.col == 2)
                ),
                None,
            )
            first = self.footnotes[index][0]
            mark = ""
            for char in first.chars:
                if char.c.isspace() or char.size >= first.size * 0.85:
                    break
                mark += char.c
            text = (
                "{\\renewcommand\\thefootnote{"
                + self.esc(mark)
                + "}\\footnotetext{"
                + self.render_text(self.footnotes[index], strip_mark=True)
                + "}}"
            )
            if position is None:
                out.append(text)
            else:
                out[position] = out[position] + "\n" + text

    def preamble(self) -> str:
        layout = self.layout
        body_family = layout.body_family
        times = "Nimbus" in body_family or "Times" in body_family
        options = (
            f"paperwidth={layout.page_w:.2f}bp,paperheight={layout.page_h:.2f}bp,left={layout.left:.2f}bp,"
            f"top={layout.top:.2f}bp,textwidth={layout.right - layout.left:.2f}bp,"
            f"textheight={layout.bottom - layout.top:.2f}bp,headsep=12bp,footskip=24bp"
        )
        if layout.twocol:
            options += f",columnsep={layout.col2_left - layout.col_right:.2f}bp"
        math_package = self.math_package()
        lines = [
            "\\documentclass[" + ("twocolumn" if layout.twocol else "") + "]{article}",
            f"\\usepackage[{options}]{{geometry}}",
            "\\usepackage{graphicx}\\usepackage{wrapfig}" + ("\\usepackage{cuted}" if self.uses_strip else ""),
            "\\usepackage[labelformat=empty]{caption}" + self.caption_font(),
            "\\usepackage{amsmath}"
            + (f"\\usepackage{math_package}" if math_package else "\\usepackage{amssymb}")
            + "\\usepackage{bm}\\usepackage{fontspec}",
            f"\\usepackage{{xeCJK}}\\setCJKmainfont{{{CJK_FONT}}}" if self.cjk else "",
            self.cjk_fallback_setup(),
            "".join(
                f"\\newfontfamily\\fallback{chr(65 + index)}{{{Path(path).name}}}"
                + (f"[Path={Path(path).parent}/]" if path.startswith("/") else "")
                for index, path in enumerate(self.fallback_fonts)
            ),
            self.heading_tex(),
            f"\\setmainfont{{texgyretermes}}{GYRE_SHAPES}" if times else "",
            f"\\setmainfont{{texgyrepagella}}{GYRE_SHAPES}"
            if re.search(r"Palladio|Pagella|Palatino", body_family)
            else "",
            "\\setmainfont{LinLibertine}[Extension=.otf,UprightFont=*_R,BoldFont=*_RB,ItalicFont=*_RI,BoldItalicFont=*_RBI]"
            if "Libertine" in body_family
            else "",
            f"\\setmonofont{{texgyrecursor}}{GYRE_SHAPES}" if times else "",
            f"\\renewcommand\\normalsize{{\\fontsize{{{layout.body_size:.2f}bp}}"
            f"{{{layout.leading:.2f}bp}}\\selectfont}}",
            INLINE_CROP_MACRO,
            FLOAT_PLACEMENT,
            "\\setlength\\parindent{0pt}"
            if not layout.indented
            else f"\\setlength\\parindent{{{layout.parindent:.1f}bp}}",
            "\\setlength\\parskip{" + (f"{layout.parskip:.1f}bp" if layout.parskip > 1 else "0pt") + "}",
            self.header_tex(),
        ]
        return "\n".join(line for line in lines if line)

    def cjk_fallback_setup(self) -> str:
        if self.cjk_fallback is None:
            return ""
        path = Path(self.cjk_fallback)
        return f"\\xeCJKsetup{{AutoFallBack=true}}\\setCJKfallbackfamilyfont{{rm}}[Path={path.parent}/]{{{path.name}}}"

    def math_package(self) -> str:
        if self.math_package_name is None:
            counts = collections.Counter(
                family(char.font) for line in self.layout.lines for char in line.chars if char.math and char.c.isalpha()
            )
            math_family = counts.most_common(1)[0][0] if counts else ""
            self.math_package_name = next(
                (package for pattern, package in MATH_PACKAGES if re.search(pattern, math_family, re.I)), ""
            )
        return self.math_package_name

    def is_toc(self, item: Item) -> bool:
        layout = self.layout
        _low, high = layout.col_bounds(item.col)
        toc = [
            line
            for line in item.lines
            if line.bbox.x1 > high - 6
            and re.search(r"\d+$", line.text)
            and (
                re.search(r"(\. ?){4,}\d*$", line.text)
                or layout.max_gap(line) > 2 * layout.body_size
                or re.fullmatch(r"\d+", line.text)
            )
        ]
        return (
            len(item.lines) >= 2
            and len(toc) >= 0.6 * len(item.lines)
            and any(re.search(r"(\. ?){4,}", line.text) for line in item.lines)
        )

    def caption_font(self) -> str:
        layout = self.layout
        captions = [line for line in layout.lines if line.kind in ("caption", "caption+")]
        if not captions:
            return "\\captionsetup{font=small}"
        size = statistics.median(line.size for line in captions)
        return (
            f"\\DeclareCaptionFont{{measured}}{{\\fontsize{{{size:.2f}bp}}{{{size * layout.leading / layout.body_size:.2f}bp}}"
            "\\selectfont}\\captionsetup{font=measured}"
        )

    def heading_tex(self) -> str:
        layout = self.layout
        out = ["\\usepackage{titlesec}"]
        for command, lines in self.heading_styles.items():
            if not lines:
                continue
            size = statistics.median(line.size for line in lines)
            bold = sum(all(char.bold for char in line.chars if char.c.isalpha()) for line in lines) > len(lines) / 2
            centered = (
                sum(
                    abs((line.bbox.x0 + line.bbox.x1) / 2 - sum(layout.col_bounds(line.col)) / 2) < 10
                    and line.bbox.x0 - layout.col_bounds(line.col)[0] > 15
                    for line in lines
                )
                > len(lines) / 2
            )
            style = (
                ("\\centering" if centered else "")
                + f"\\fontsize{{{size:.1f}bp}}{{{size * 1.2:.1f}bp}}\\selectfont"
                + ("\\bfseries" if bold else "")
            )
            out.append(f"\\titleformat{{\\{command}}}{{{style}}}{{}}{{0pt}}{{}}")
            out.append(
                f"\\titlespacing*{{\\{command}}}{{0pt}}{{{1.2 * layout.leading:.1f}bp}}{{{0.6 * layout.leading:.1f}bp}}"
            )
        return "\n".join(out)

    def header_tex(self) -> str:
        layout = self.layout
        slots: dict[str, collections.Counter[str]] = {
            "L": collections.Counter(),
            "C": collections.Counter(),
            "R": collections.Counter(),
        }
        for line in layout.lines:
            if (
                id(line) not in layout.running
                or line.bbox.y0 >= layout.page_h / 2
                or re.fullmatch(r"[\divxlc]+", line.text)
            ):
                continue
            middle = (line.bbox.x0 + line.bbox.x1) / 2
            if abs(line.bbox.x0 - layout.left) < 10:
                slot = "L"
            elif abs(line.bbox.x1 - layout.right) < 10:
                slot = "R"
            elif abs(middle - layout.page_w / 2) < 15:
                slot = "C"
            else:
                slot = "L" if middle < layout.page_w / 2 else "R"
            slots[slot][line.text] += 1
        if not any(slots.values()):
            return "\\pagestyle{plain}"
        parts = "".join(
            f"\\fancyhead[{key}]{{{self.esc(counter.most_common(1)[0][0])}}}"
            for key, counter in slots.items()
            if counter
        )
        return (
            "\\usepackage{fancyhdr}\\pagestyle{fancy}\\fancyhf{}"
            + parts
            + "\\fancyfoot[C]{\\thepage}\\renewcommand\\headrulewidth{0.4pt}"
        )
