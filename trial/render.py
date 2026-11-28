import re
from tongtu.reconstruct import table

FENCE_RE = re.compile(r"^```[^\n]*\n?|\n?```\s*$")
LANG_RE = re.compile(r"^\s*<_[^>]*_>")
MATH_RE = re.compile(r"(\$\$.*?\$\$|\$.*?\$|\\\(.*?\\\)|\\\[.*?\\\])")
LINE_RE = re.compile(r"^(\s*)(\d+:)?(\s*)(.*)$")
SPECIAL_RE = re.compile(r"(?<!\\)([#%&_])")
SPACE_EM = 0.35
NUMBER_EM = 2.5

PREAMBLE = (
    table.PREAMBLE
    + r"\usepackage{fancyvrb}\setmonofont{lmmono10-regular.otf}\setlength{\parindent}{0pt}"
)


def clean(text: str) -> str:
    text = LANG_RE.sub("", text.strip())
    return FENCE_RE.sub("", text.strip("\n")).expandtabs(4)


def escape_text(segment: str) -> str:
    segment = SPECIAL_RE.sub(r"\\\1", segment)
    return segment.replace("~", r"\textasciitilde{}").replace("^", r"\textasciicircum{}")


def algorithm_line(line: str) -> str:
    lead, number, gap, rest = LINE_RE.match(line).groups()
    rest = "".join(part if MATH_RE.fullmatch(part) else escape_text(part) for part in MATH_RE.split(rest))
    if number:
        indent = NUMBER_EM + max(0, len(gap) - 1) * SPACE_EM
        head = rf"\llap{{\makebox[{indent:.2f}em][l]{{\makebox[{NUMBER_EM - 0.5}em][r]{{{number}}}}}}}"
    elif lead:
        indent = NUMBER_EM + len(lead) * SPACE_EM
        head = ""
    else:
        return rf"\leftskip=0pt\hangindent={NUMBER_EM}em\hangafter=1\noindent {rest}\par"
    return rf"\leftskip={indent:.2f}em\noindent {head}{rest}\par"


def document(kind: str, text: str, width: float, size: float) -> str:
    body = clean(text)
    cjk = table.CJK_PACKAGE if table.CJK_RE.search(body) else ""
    font = rf"\fontsize{{{size:.1f}bp}}{{{size * 1.25:.1f}bp}}\selectfont"
    if kind == "listing":
        content = font + "\n\\begin{Verbatim}\n" + body + "\n\\end{Verbatim}\n"
    else:
        lines = "\n".join(algorithm_line(line) for line in body.splitlines() if line.strip())
        content = rf"\begin{{minipage}}{{{width:.1f}bp}}{font}" + "\n" + lines + "\n\\end{minipage}"
    return PREAMBLE + cjk + r"\begin{document}" + content + r"\end{document}"
