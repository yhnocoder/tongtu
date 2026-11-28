import html, json, sys
from pathlib import Path

work, papers = Path(sys.argv[1]), sys.argv[2].split()
labels = sorted({f.stem[len("verdict_"):] for p in papers for f in (work / p).glob("verdict_*.json")})
if not labels:
    sys.exit("没有可汇总的结果，看 work/log_<名称>.txt")
data = {}
for p in papers:
    items = json.loads((work / p / "items.json").read_text())
    data[p] = {"items": items, "out": {}, "verdict": {}}
    for m in labels:
        for kind in ("out", "verdict"):
            f = work / p / f"{kind}_{m}.json"
            data[p][kind][m] = json.loads(f.read_text()) if f.exists() else {}

columns = ["模型", "类别", "通过/总数", "文字一致", "字符相似度", "上下标字号一致", "缩进一致", "逐行一致", "粗体不一致字符", "识别 s/条", "比对 s/条", "合计 s"]
rows = []
for kind in ("algorithm", "listing"):
    for m in labels:
        vs, outs = [], []
        for p in papers:
            for it in data[p]["items"]:
                v = data[p]["verdict"][m].get(it["key"])
                if it["kind"] == kind and v:
                    vs.append(v)
                    outs.append(data[p]["out"][m][it["key"]])
        if not vs:
            continue
        n = len(vs)
        count = lambda field: f"{sum(bool(v.get(field)) for v in vs)}/{n}"
        recog = sum(o.get("wall", o["seconds"]) for o in outs)
        check = sum(v.get("verify_wall", 0) for v in vs)
        rows.append([m, kind, count("passed"), count("text_ok"), f"{sum(v.get('chars', 0) for v in vs) / n:.3f}",
                     count("script_ok"), count("indent_ok"), count("lines_ok"), str(sum(v.get("bold_diff", 0) for v in vs)),
                     f"{recog / n:.2f}", f"{check / n:.2f}", f"{recog + check:.0f}"])
print("\t".join(columns))
for row in rows:
    print("\t".join(row))

e = html.escape
parts = ["<!doctype html><meta charset=utf-8><title>算法与代码块识别试跑</title><style>",
         "body{font:14px/1.5 -apple-system,sans-serif;margin:24px;max-width:1500px}table{border-collapse:collapse}",
         "td,th{border:1px solid #ccc;padding:4px 8px;vertical-align:top}img{max-width:100%;border:1px solid #ddd}",
         ".ok{color:#17803d}.bad{color:#b42318}pre{white-space:pre-wrap;font-size:12px;background:#f6f6f6;padding:6px;max-width:680px}",
         "h3{margin-top:32px}</style><h1>算法与代码块识别试跑</h1><table><tr>",
         "".join(f"<th>{e(c)}</th>" for c in columns), "</tr>"]
for row in rows:
    parts.append("<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>")
parts.append("</table>")
for p in papers:
    for it in data[p]["items"]:
        ran = [m for m in labels if it["key"] in data[p]["verdict"][m]]
        parts.append(f"<h3>{e(p)} {e(it['key'])} · {e(it['caption'])}</h3><table><tr><th>原文截图</th>")
        parts.append("".join(f"<th>{e(m)}</th>" for m in ran) + "</tr><tr>")
        parts.append(f"<td><img src='{e(p)}/images/{e(it['key'])}.png'></td>")
        for m in ran:
            v = data[p]["verdict"][m][it["key"]]
            o = data[p]["out"][m][it["key"]]
            state = "<b class=ok>通过</b>" if v["passed"] else "<b class=bad>未通过</b>"
            rendered = work / p / f"render_{m}" / f"{it['key']}.png"
            image = f"<br><img src='{e(p)}/render_{e(m)}/{e(it['key'])}.png'>" if rendered.exists() else ""
            problems = "".join(f"<br>{e(x)}" for x in v.get("problems", []))
            parts.append(f"<td>{state} 字符相似度 {v.get('chars', 0):.3f}，粗体不一致 {v.get('bold_diff', 0)}，"
                         f"识别 {o['seconds']} s{problems}{image}<details><summary>模型原始输出</summary><pre>{e(o['text'])}</pre></details></td>")
        parts.append("</tr></table>")
(work / "report.html").write_text("".join(parts))
