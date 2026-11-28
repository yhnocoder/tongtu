import json, os, sys, collections, re
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalize
from progress import bar
from tongtu.reconstruct import formula, table
from tongtu.reconstruct.crops import crop_path
from tongtu.artifacts.reconstruct import RegionKind

def category(problem):
    p = problem or ""
    for key, label in (("Glyph sequence", "chars"), ("Characters in the original", "chars"), ("Words differing", "chars"), ("bold", "bold"), ("Bold", "bold"),
                       ("colour", "color"), ("Colour", "color"), ("vertical placement", "position"), ("Layout", "layout"),
                       ("failed", "compile"), ("not allowed", "forbidden"), ("forbidden", "forbidden"), ("prose", "prose"), ("environments", "env")):
        if key in p: return label
    return "other:" + p[:40]

def judge(pd, it, r):
    k = it["key"]
    if it["type"] == "formula":
        base = normalize.formula(r["raw"] if "raw" in r else r["text"])
        cands = [base]
        alt = re.sub(r"\\(left|right|bigl|bigr|Bigl|Bigr|big|Big)\s*(?=[()\[\]|.]|\\[{}|])", "", base)
        alt = re.sub(r"\\(left|right)\s*\.", "", alt)
        if alt != base: cands.append(alt)
        for latex in cands:
            v = formula.verify(pd / "tree" / crop_path(k), latex, RegionKind(it["kind"]))
            if v.passed: break
        latex = cands[0] if not v.passed else latex
        group = it["kind"]
    else:
        base = normalize.table(r.get("raw", r["text"])) or "SKIP"
        cands = [base]
        m = re.match(r"\\begin\{tabular\}\{(l+)\}", base)
        if m:
            n = len(m.group(1))
            for spec in ("c" * n, "l" + "c" * (n - 1), "r" * n, "l" + "r" * (n - 1)):
                cands.append(base.replace("{" + m.group(1) + "}", "{" + spec + "}", 1))
        for latex in cands:
            v = table.verify(pd / "tree" / crop_path(k), latex)
            if v.passed: break
        if not v.passed and "Bold differs for:" in (v.problem or ""):
            fixed = latex
            for _ in range(2):
                m2 = re.search(r"Bold differs for: (.*?)(?:\. |$|Colour)", v.problem or "")
                if not m2: break
                fixed = normalize.toggle_bold(fixed, [w.strip() for w in m2.group(1).split(", ")])
                v = table.verify(pd / "tree" / crop_path(k), fixed)
                if v.passed or "Bold differs" not in (v.problem or ""): break
            if v.passed: latex = fixed
        if not v.passed:
            latex = base; v = table.verify(pd / "tree" / crop_path(k), base) if len(cands) > 1 else v
        group = "table"
    return k, {"group": group, "passed": v.passed, "category": "" if v.passed else category(v.problem),
               "problem": v.problem[:300], "latex": latex, "seconds": r["seconds"]}


if __name__ == "__main__":
    model = sys.argv[1]
    summary = {}
    progress = bar(stderr=True)
    progress.start()
    with ProcessPoolExecutor(int(os.environ.get("VERIFY_JOBS") or os.cpu_count())) as pool:
        for pd in map(Path, sys.argv[2:]):
            out = pd / f"out_{model}.json"
            if not out.exists(): continue
            res = json.loads(out.read_text())
            items = [it for it in json.loads((pd / "items.json").read_text()) if it["key"] in res]
            task = progress.add_task(f"verify {model} {pd.name}", total=len(items))
            futures = [pool.submit(judge, pd, it, res[it["key"]]) for it in items]
            verdicts = {}
            for f in as_completed(futures):
                k, v = f.result()
                verdicts[k] = v
                progress.advance(task)
            verdicts = {it["key"]: verdicts[it["key"]] for it in items}
            (pd / f"verdict_{model}.json").write_text(json.dumps(verdicts, ensure_ascii=False, indent=1))
            for k, v in verdicts.items():
                s = summary.setdefault((pd.name, v["group"]), {"n": 0, "pass": 0, "sec": 0.0, "cats": collections.Counter()})
                s["n"] += 1; s["pass"] += v["passed"]; s["sec"] += v["seconds"]
                if not v["passed"]: s["cats"][v["category"]] += 1
    progress.stop()
    for (p, g), s in sorted(summary.items()):
        print(f"{model}\t{p}\t{g}\t{s['pass']}/{s['n']}\tavg {s['sec']/s['n']:.1f}s\t{dict(s['cats'])}")
