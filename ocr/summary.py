import json, re, sys
from pathlib import Path
work = Path(sys.argv[1]); papers = ["transformer", "kimi", "kimi15"]
labels = sorted({f.stem[len("verdict_"):] for f in (work / papers[0]).glob("verdict_*.json")})
ms = [m for m in labels if all((work / p / f"verdict_{m}.json").exists() for p in papers)]
V = {p: {m: json.loads((work / p / f"verdict_{m}.json").read_text()) for m in ms} for p in papers}
def ref(p, k): return sum(bool(re.search(r"http|arxiv|url|\.org", V[p][m][k]["latex"], re.I)) for m in ms) >= min(3, len(ms))
print("model\tgroup\tpassed/total\tavg s\tmax s")
for g in ["inline", "display", "table"]:
    for m in ms + ["any"]:
        n = ok = 0; secs = []
        for p in papers:
            keys = [k for k, v in V[p][ms[0]].items() if v["group"] == g and not (g == "display" and ref(p, k))]
            for k in keys:
                if m == "any":
                    ok += any(V[p][x].get(k, {}).get("passed", False) for x in ms); n += 1
                elif k in V[p][m]:
                    ok += V[p][m][k]["passed"]; n += 1; secs.append(V[p][m][k]["seconds"])
        if n:
            print(f"{m}\t{g}\t{ok}/{n}\t" + (f"{sum(secs)/len(secs):.1f}\t{max(secs):.0f}" if secs else "-\t-"))
