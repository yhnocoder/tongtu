import re, html
from html.parser import HTMLParser

END_TOKENS = re.compile(r"<\|im_end\|>|<\|endoftext\|>|</s>|<\|end\|>|<\|user\|>|<eos>|<\|im_start\|>.*", re.S)

def formula(text):
    t = END_TOKENS.sub("", text).strip()
    t = re.sub(r"^```(?:latex)?\s*|\s*```$", "", t).strip()
    for a, b in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
        if t.startswith(a) and t.endswith(b) and len(t) >= len(a) + len(b):
            t = t[len(a):len(t) - len(b)].strip()
            break
    t = re.sub(r"\$\$\s*\n?\s*\$\$", r" \\\\ ", t)
    t = re.sub(r"\\tag\*?\s*\{\s*\(?([^{}()]+?)\)?\s*\}", r" \\quad (\1)", t)
    t = t.replace(r"\begin{array}{c}", r"\begin{array}{c}")
    t = re.sub(r"\\(begin|end)\{(align\*?|equation\*?|eqnarray\*?)\}", lambda m: "" if m.group(2).startswith("equation") else ("\\" + m.group(1) + "{aligned}"), t)
    return t.strip()

def cell_text(s):
    s = END_TOKENS.sub("", s)
    out = []
    pos = 0
    for m in re.finditer(r"\\\((.+?)\\\)|\$(.+?)\$", s, re.S):
        out.append(esc(s[pos:m.start()]))
        out.append("$" + (m.group(1) or m.group(2)).strip() + "$")
        pos = m.end()
    out.append(esc(s[pos:]))
    return "".join(out).strip()

def esc(s):
    s = html.unescape(s)
    s = re.sub(r"<br\s*/?>", r"\\\\", s)
    s = re.sub(r"<(b|strong)>(.*?)</\1>", lambda m: "\x01" + m.group(2) + "\x02", s, flags=re.S)
    s = re.sub(r"<(i|em)>(.*?)</\1>", lambda m: "\x03" + m.group(2) + "\x02", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("#", r"\#"), ("_", r"\_"), ("^", r"\^{}"), ("~", r"\~{}"), ("{", r"\{"), ("}", r"\}")):
        if a == "\\":
            s = re.sub(r"\\(?!\\)", lambda m: b, s) if "\\\\" not in s else s
        else:
            s = s.replace(a, b)
    s = s.replace("\x01", r"\textbf{").replace("\x03", r"\textit{").replace("\x02", "}")
    return s

def otsl_grid(t):
    rows = [r for r in re.split(r"<nl>", t) if r.strip() or "<" in r]
    grid = []
    for r in rows:
        toks = re.findall(r"<(fcel|ecel|lcel|ucel|xcel|ched|rhed|srow)>([^<]*)", r)
        if toks:
            grid.append([(k, v) for k, v in toks])
    return grid

def from_otsl(t):
    grid = otsl_grid(t)
    if not grid:
        return None
    n = max(len(r) for r in grid)
    for r in grid:
        r += [("ecel", "")] * (n - len(r))
    cells = []
    for i, r in enumerate(grid):
        for j, (k, v) in enumerate(r):
            if k in ("fcel", "ecel", "ched", "rhed", "srow"):
                cs = 1
                while j + cs < n and grid[i][j + cs][0] == "lcel":
                    cs += 1
                rs = 1
                while i + rs < len(grid) and grid[i + rs][j][0] in ("ucel", "xcel"):
                    rs += 1
                cells.append((i, j, rs, cs, v))
    return emit(len(grid), n, cells)

class _P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False); self.rows = []; self.cur = None; self.buf = None
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr": self.rows.append([])
        elif tag in ("td", "th"):
            self.cur = [int(a.get("rowspan", 1) or 1), int(a.get("colspan", 1) or 1)]; self.buf = []
        elif self.buf is not None:
            self.buf.append(self.get_starttag_text())
    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.buf is not None:
            if not self.rows: self.rows.append([])
            self.rows[-1].append((self.cur[0], self.cur[1], "".join(self.buf))); self.buf = None
        elif self.buf is not None: self.buf.append(f"</{tag}>")
    def handle_data(self, d):
        if self.buf is not None: self.buf.append(d)
    def handle_entityref(self, n):
        if self.buf is not None: self.buf.append(f"&{n};")
    def handle_charref(self, n):
        if self.buf is not None: self.buf.append(f"&#{n};")

def from_html(t):
    p = _P(); p.feed(t)
    rows = [r for r in p.rows if r]
    if not rows: return None
    occ = {}; cells = []; ncols = 0
    for i, r in enumerate(rows):
        j = 0
        for rs, cs, v in r:
            while (i, j) in occ: j += 1
            cells.append((i, j, rs, cs, v))
            for a in range(rs):
                for b in range(cs): occ[(i + a, j + b)] = 1
            j += cs; ncols = max(ncols, j)
    nrows = max(i + rs for i, j, rs, cs, v in cells)
    return emit(nrows, ncols, cells)

def emit(nrows, ncols, cells):
    at = {(i, j): (rs, cs, v) for i, j, rs, cs, v in cells}
    covered = set()
    for i, j, rs, cs, v in cells:
        for a in range(rs):
            for b in range(cs):
                if (a, b) != (0, 0): covered.add((i + a, j + b))
    lines = []
    for i in range(nrows):
        parts = []; j = 0
        while j < ncols:
            if (i, j) in at:
                rs, cs, v = at[(i, j)]
                c = cell_text(v)
                if "\\\\" in c: c = r"\makecell{" + c + "}"
                if rs > 1: c = rf"\multirow{{{rs}}}{{*}}{{{c}}}"
                if cs > 1: c = rf"\multicolumn{{{cs}}}{{c}}{{{c}}}"
                parts.append(c); j += cs
            else:
                cs = 1
                while (i, j + cs) in covered and (i, j + cs) not in at and False: cs += 1
                parts.append(""); j += 1
        lines.append(" & ".join(parts) + r" \\")
        if i == 0: lines.append(r"\midrule")
    return "\\begin{tabular}{" + "l" * ncols + "}\n\\toprule\n" + "\n".join(lines) + "\n\\bottomrule\n\\end{tabular}"

def table(raw):
    t = END_TOKENS.sub("", raw)
    if "<fcel>" in t or "<ecel>" in t or "<nl>" in t:
        return from_otsl(t)
    if "<table" in t or "<tr" in t:
        m = re.search(r"<table.*?</table>", t, re.S)
        return from_html(m.group(0) if m else t)
    return None


def toggle_bold(latex, words):
    words = {w.casefold() for w in words if w}
    lines = latex.split("\n")
    out = []
    for line in lines:
        if not line.rstrip().endswith("\\\\"):
            out.append(line); continue
        body = line.rstrip()[:-2]
        cells = body.split(" & ")
        new = []
        for c in cells:
            plain = re.sub(r"\\[a-zA-Z]+|[{}$]", " ", c).casefold().split()
            if c.strip() and plain and (all(tok in words for tok in plain) or "".join(plain) in words):
                inner = c.strip()
                if inner.startswith("\\textbf{\\boldmath ") and inner.endswith("}"):
                    c = inner[18:-1]
                elif inner.startswith("\\textbf{") and inner.endswith("}"):
                    c = inner[8:-1]
                else:
                    c = ("\\textbf{" + inner + "}") if "$" not in inner else ("\\textbf{\\boldmath " + inner + "}")
            new.append(c)
        out.append(" & ".join(new) + " \\\\")
    return "\n".join(out)
