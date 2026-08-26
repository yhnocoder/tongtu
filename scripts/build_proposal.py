import re
from pathlib import Path

PROPOSAL = Path(__file__).resolve().parent.parent / "docs" / "proposal"
OUT = PROPOSAL / "proposal.html"
PAGES = [
    ("pipeline-v0.html", "流水线"),
    ("model.html", "模型调用层"),
    ("cli.html", "命令行"),
]

TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>通途提案</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<link rel="stylesheet" href="style.css">
<style>
html,body{height:100%;overflow:hidden}
body{display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;gap:4px;padding:0 16px;height:46px;flex:none;
  border-bottom:1px solid var(--line);background:var(--card)}
.topbar .brand{font-weight:700;font-size:14px;margin-right:16px;color:var(--muted)}
.tab{appearance:none;border:0;background:none;color:var(--muted);font:inherit;font-size:14px;
  padding:0 14px;height:100%;cursor:pointer;position:relative}
.tab:hover{color:var(--fg)}
.tab.active{color:var(--accent);font-weight:700}
.tab.active::after{content:"";position:absolute;left:10px;right:10px;bottom:-1px;height:2px;background:var(--accent)}
.scroll{flex:1;display:flex;gap:24px;overflow-x:auto;overflow-y:hidden;
  scroll-snap-type:x mandatory;overscroll-behavior-x:contain;
  padding:18px calc((100vw - min(80vw,1160px)) / 2) 22px;scrollbar-width:thin}
.panel{flex:0 0 min(80vw,1160px);min-width:0;height:100%;overflow-y:auto;scroll-behavior:smooth;
  scroll-snap-align:center;scroll-snap-stop:always;
  background:var(--card);border:1px solid var(--line);border-radius:12px;
  box-shadow:0 2px 12px rgba(0,0,0,.06);
  opacity:.45;transition:opacity .35s}
.panel.focus{opacity:1}
.panel .wrap{padding:32px 40px 64px}
.panel .card{background:var(--bg)}
</style>
</head>
<body>
<nav class="topbar"><span class="brand">通途提案</span>@TABS@</nav>
<main class="scroll" id="scroll">
@PANELS@
</main>
<script>
const scroller = document.getElementById("scroll");
const tabs = [...document.querySelectorAll(".tab")];
const panels = [...document.querySelectorAll(".panel")];
tabs.forEach(t => t.addEventListener("click", () => {
  panels[t.dataset.i].scrollIntoView({inline: "center", behavior: "smooth"});
}));
const io = new IntersectionObserver(es => es.forEach(e => {
  if (e.intersectionRatio > .6) {
    const i = panels.indexOf(e.target);
    tabs.forEach((t, j) => t.classList.toggle("active", i === j));
    panels.forEach((p, j) => p.classList.toggle("focus", i === j));
  }
}), {root: scroller, threshold: .6});
panels.forEach(p => io.observe(p));
</script>
</body>
</html>
"""


def page_body(name: str) -> str:
    text = (PROPOSAL / name).read_text()
    if "<style" in text:
        raise SystemExit(f"{name}: page-level <style> is not allowed; put rules in style.css")
    body = re.search(r"<body>\s*(.*?)\s*</body>", text, re.S).group(1)
    for page, _ in PAGES:
        stem = page.removesuffix(".html")
        body = body.replace(f'href="{page}#', 'href="#')
        body = body.replace(f'href="{page}"', f'href="#page-{stem}"')
    return body


def main() -> None:
    tabs = "".join(f'<button class="tab" data-i="{i}">{title}</button>' for i, (_, title) in enumerate(PAGES))
    panels = "\n".join(
        f'<section class="panel" id="page-{name.removesuffix(".html")}">\n{page_body(name)}\n</section>'
        for name, _ in PAGES
    )
    OUT.write_text(TEMPLATE.replace("@TABS@", tabs).replace("@PANELS@", panels))
    print(OUT)


if __name__ == "__main__":
    main()
