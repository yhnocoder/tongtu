import base64, json, os, struct, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from progress import bar

SYSTEM = "You are a helpful assistant."
PROMPTS = {
    "tele": ("\nThe image contains a code snippet, please output the parsing result.", SYSTEM),
    "tele_text": ("\nPlease output the text content from the image.", SYSTEM),
    "mineru": ("\nText Recognition:", SYSTEM),
    "paddle": ("OCR:", None),
    "glm": ("Text Recognition:", None),
    "minicpm": ("Text Recognition:", None),
}
label, family, papers = sys.argv[1], sys.argv[2], sys.argv[3:]
base = os.environ.get("API_BASE", "http://127.0.0.1:11000/v1").rstrip("/")
model = os.environ.get("API_MODEL", "")
key = os.environ.get("API_KEY", "none")
max_time = float(os.environ.get("MAX_TIME", "600"))
max_tokens = int(os.environ.get("MAX_TOKENS", "4096"))
concurrency = int(os.environ.get("CONCURRENCY", "4"))
lock = threading.Lock()


def ask(png):
    prompt, system = PROMPTS[family]
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


def text_layer(paper, item):
    import glyphs
    from tongtu.reconstruct.crops import crop_path, open_crop
    return "\n".join(glyphs.text_lines(open_crop(paper / "tree" / crop_path(item["key"]))[0])), 0


def image(paper, item):
    return (paper / "images" / f"{item['key']}.png").read_bytes()


def cost(paper, item):
    width, height = struct.unpack(">II", image(paper, item)[16:24])
    return width * height


def run(job, item):
    paper, out, results, todo, task = job
    start = time.time()
    try:
        text, tokens = text_layer(paper, item) if family == "textlayer" else ask(image(paper, item))
    except Exception as error:
        with lock:
            print(paper.name, item["key"], "error", error, file=sys.stderr, flush=True)
            progress.advance(task)
        return
    with lock:
        results[item["key"]] = {"text": text, "seconds": round(time.time() - start, 2), "tokens": tokens}
        print(paper.name, item["key"], results[item["key"]]["seconds"], repr(text[:100]), file=sys.stderr, flush=True)
        progress.advance(task)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1))


jobs = []
for paper in map(Path, papers):
    items = json.loads((paper / "items.json").read_text())
    out = paper / f"out_{label}.json"
    results = json.loads(out.read_text()) if out.exists() and os.environ.get("RERUN") != "1" else {}
    todo = [it for it in items if it["key"] not in results and not (family == "textlayer" and it["kind"] != "listing")]
    jobs.append([paper, out, results, todo])

with bar() as progress:
    for job in jobs:
        job.append(progress.add_task(f"识别 {label} {job[0].name}", total=len(job[3]), start=False))
    for job in jobs:
        queue = sorted(job[3], key=lambda it: -cost(job[0], it))
        progress.start_task(job[4])
        start = time.time()
        with ThreadPoolExecutor(1 if family == "textlayer" else concurrency) as pool:
            list(pool.map(lambda it: run(job, it), queue))
        wall = time.time() - start
        results = job[2]
        done = [it for it in queue if it["key"] in results]
        scale = wall / (sum(results[it["key"]]["seconds"] for it in done) or 1)
        for it in done:
            results[it["key"]]["wall"] = round(results[it["key"]]["seconds"] * scale, 3)
        job[1].write_text(json.dumps(results, ensure_ascii=False, indent=1))
