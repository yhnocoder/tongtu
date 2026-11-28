import base64, json, os, struct, sys, threading, time, urllib.request
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

def image(pd, it):
    return (pd / os.environ.get("IMG_DIR", "images") / f"{it['key']}.png").read_bytes()


def cost(pd, it):
    width, height = struct.unpack(">II", image(pd, it)[16:24])
    return width * height * (20 if it["type"] == "table" else 1)


def run(job, it):
    pd, out, res, todo, task = job
    k = it["key"]
    formula = it["type"] == "formula"
    t = time.time()
    try:
        text, tokens = ask(image(pd, it), formula_prompt if formula else table_prompt, 768 if formula else 4096)
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
    jobs.append([pd, out, res, [it for it in items if it["key"] not in res and not (os.environ.get("ONLY") and it["type"] != os.environ["ONLY"])]])

with bar() as progress:
    for job in jobs:
        job.append(progress.add_task(f"{label} {job[0].name}", total=len(job[3])))
    for job in jobs:
        queue = sorted(job[3], key=lambda it: -cost(job[0], it))
        start = time.time()
        with ThreadPoolExecutor(concurrency) as pool:
            list(pool.map(lambda it: run(job, it), queue))
        wall = time.time() - start
        res = job[2]
        scale = wall / (sum(res[it["key"]]["seconds"] for it in queue) or 1)
        for it in queue:
            res[it["key"]]["wall"] = round(res[it["key"]]["seconds"] * scale, 3)
        job[1].write_text(json.dumps(res, ensure_ascii=False, indent=1))
        print(job[0].name, len(queue), "items", round(wall, 1), "s wall", file=sys.stderr, flush=True)
