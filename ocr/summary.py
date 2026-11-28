import json, re, sys
from pathlib import Path
work = Path(sys.argv[1]); papers = ["transformer", "kimi", "kimi15"]
labels = sorted({f.stem[len("verdict_"):] for f in (work / papers[0]).glob("verdict_*.json")})
ms = [m for m in labels if all((work / p / f"verdict_{m}.json").exists() for p in papers)]
if not ms:
    sys.exit("no results to summarize; see work/log_<model>.txt")
V = {p: {m: json.loads((work / p / f"verdict_{m}.json").read_text()) for m in ms} for p in papers}
def ref(p, k): return sum(bool(re.search(r"http|arxiv|url|\.org", V[p][m][k]["latex"], re.I)) for m in ms) >= min(3, len(ms))
print("model\tgroup\tpassed/total\trecognize s/item\tverify s/item\tboth s/item\tboth total s")
for g in ["inline", "display", "table"]:
    for m in ms + ["any"]:
        n = ok = 0; secs = []; walls = []; checks = []
        for p in papers:
            keys = [k for k, v in V[p][ms[0]].items() if v["group"] == g and not (g == "display" and ref(p, k))]
            for k in keys:
                if m == "any":
                    ok += any(V[p][x].get(k, {}).get("passed", False) for x in ms); n += 1
                elif k in V[p][m]:
                    ok += V[p][m][k]["passed"]; n += 1; secs.append(V[p][m][k]["seconds"]); walls.append(V[p][m][k].get("wall", V[p][m][k]["seconds"])); checks.append(V[p][m][k].get("verify_wall", 0))
        if n:
            print(f"{m}\t{g}\t{ok}/{n}\t" + (f"{sum(walls)/n:.2f}\t{sum(checks)/n:.2f}\t{(sum(walls)+sum(checks))/n:.2f}\t{sum(walls)+sum(checks):.0f}" if secs else "-\t-\t-\t-"))
