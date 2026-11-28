import base64, json, os, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from progress import bar

PROMPTS = {
    "mineru": ("\nFormula Recognition:", "\nTable Recognition:", "You are a helpful assistant."),
    "tele": ("\nPlease write out the expression of the formula in the image using LaTeX format.",
             "\nThis is the image of a table. Please output the table in OTSL format.", "You are a helpful assistant."),
    "paddle": ("Formula Recognition:", "Table Recognition:", None),
    "glm": ("Formula Recognition:", "Table Recognition:", None),
    "minicpm": ("Formula Recognition:", "Table Recognition:", None),
}
label, family, papers = sys.argv[1], sys.argv[2], sys.argv[3:]
base = os.environ.get("API_BASE", "http://127.0.0.1:8000/v1").rstrip("/")
model = os.environ.get("API_MODEL", "")
key = os.environ.get("API_KEY", "none")
max_time = float(os.environ.get("MAX_TIME", "600"))
formula_prompt, table_prompt, system = PROMPTS[family]
concurrency = int(os.environ.get("CONCURRENCY", "4"))
lock = threading.Lock()

def ask(png, prompt, max_tokens):
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": prompt}]})
    body = {"messages": messages, "temperature": 0, "max_tokens": max_tokens}
    if model:
        body["model"] = model
    request = urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(request, timeout=max_time) as response:
        reply = json.load(response)
    return reply["choices"][0]["message"]["content"] or "", reply.get("usage", {}).get("completion_tokens", 0)

def run(pd, it, res, out):
    k = it["key"]
    png = (pd / os.environ.get("IMG_DIR", "images") / f"{k}.png").read_bytes()
    formula = it["type"] == "formula"
    t = time.time()
    try:
        text, tokens = ask(png, formula_prompt if formula else table_prompt, 768 if formula else 4096)
    except Exception as error:
        text, tokens = "", 0
        print(pd.name, k, "error", error, file=sys.stderr, flush=True)
    with lock:
        res[k] = {"text": text, "raw": text, "seconds": round(time.time() - t, 2), "tokens": tokens}
        print(pd.name, k, res[k]["seconds"], repr(text[:100]), file=sys.stderr, flush=True)
        progress.advance(task)
        out.write_text(json.dumps(res, ensure_ascii=False, indent=1))

jobs = []
for pd in map(Path, papers):
    items = json.loads((pd / "items.json").read_text())
    out = pd / f"out_{label}.json"
    res = json.loads(out.read_text()) if out.exists() and os.environ.get("RERUN") != "1" else {}
    jobs.append((pd, out, res, [it for it in items if it["key"] not in res and not (os.environ.get("ONLY") and it["type"] != os.environ["ONLY"])]))

with bar() as progress:
    for pd, out, res, todo in jobs:
        task = progress.add_task(f"{label} {pd.name}", total=len(todo))
        groups = {}
        for it in todo:
            groups.setdefault(it.get("kind") or it["type"], []).append(it)
        for group in groups.values():
            start = time.time()
            with ThreadPoolExecutor(concurrency) as pool:
                list(pool.map(lambda it: run(pd, it, res, out), group))
            share = round((time.time() - start) / len(group), 3)
            with lock:
                for it in group:
                    res[it["key"]]["wall"] = share
                out.write_text(json.dumps(res, ensure_ascii=False, indent=1))
            print(pd.name, it.get("kind") or it["type"], len(group), "items", round(time.time() - start, 1), "s wall", file=sys.stderr, flush=True)
