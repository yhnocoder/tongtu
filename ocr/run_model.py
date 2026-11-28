import json, sys, time, torch
from pathlib import Path
from progress import bar
from PIL import Image
from transformers import AutoProcessor, AutoModelForImageTextToText
import os
DEVICE = os.environ.get("DEVICE") or ("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
if DEVICE == "cpu": torch.set_num_threads(os.cpu_count())
MAX_PIXELS = 1280 * 28 * 28
BATCH = int(os.environ.get("BATCH") or (1 if DEVICE == "cpu" else 8))
MAX_TIME = float(os.environ.get("MAX_TIME", "600"))
M = Path(os.environ.get("MODELS_DIR", "/home/user/models"))
PROMPTS = {
 "paddle": ("PaddleOCR-VL-1.6", "Formula Recognition:", "Table Recognition:"),
 "glm": ("GLM-OCR", "Formula Recognition:", "Table Recognition:"),
 "mineru": ("MinerU2.5-Pro-2605-1.2B", "\nFormula Recognition:", "\nTable Recognition:"),
 "minicpm": ("MiniCPM-OCR-1.0", "Formula Recognition:", "Table Recognition:"),
 "ovis": ("OvisOCR3", "prompts/formula_recognition.txt", "prompts/document_markdown.txt"),
 "minicpm2": ("MiniCPM-OCR-1.0", "Formula Recognition:", "Table Recognition:"),
 "tele": ("TeleOCR", "\nPlease write out the expression of the formula in the image using LaTeX format.", "\nThis is the image of a table. Please output the table in OTSL format."),
}
name, paper_dirs = sys.argv[1], sys.argv[2:]
limit = None
d, fp, tp = PROMPTS[name]
if name == "ovis":
    fp, tp = (M / d / fp).read_text(), (M / d / tp).read_text()
t0 = time.time()
kw = {"trust_remote_code": True} if name in ("tele",) else {}
proc = AutoProcessor.from_pretrained(M/d, **kw)
model = AutoModelForImageTextToText.from_pretrained(M/d, dtype=torch.bfloat16, **kw).to(DEVICE).eval()
tok = getattr(proc, "tokenizer", proc)
tok.padding_side = "left"
print("load", round(time.time()-t0,1), DEVICE, flush=True)
print("load", round(time.time()-t0,1), DEVICE, file=sys.stderr, flush=True)
def load(pd, it):
    img = Image.open(pd/os.environ.get("IMG_DIR", "images")/f"{it['key']}.png").convert("RGB")
    scale = min(1.0, (MAX_PIXELS / (img.width * img.height)) ** 0.5)
    if scale < 1: img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img

def prompt_for(it, hints):
    prompt = fp if it["type"] == "formula" else tp
    if name == "minicpm2" and it.get("kind") == "inline":
        prompt = "Text Recognition:"
    if hints:
        prompt = prompt + "\nThe PDF text layer of this image, as font#colour:text pieces in drawing order: " + " | ".join(hints[it["key"]]) + "\nUse exactly these characters. Fonts whose name contains Bold, Medi, BX or MIB are bold (write \\bm{} or \\mathbf{}); a colour other than 000000 is written as {\\color[HTML]{RRGGBB} ...}."
    return prompt

def generate(pd, group, hints):
    msgs = [[{"role":"user","content":[{"type":"image","image":load(pd, it)},{"type":"text","text":prompt_for(it, hints)}]}] for it in group]
    extra = {}
    if name == "paddle":
        extra = {"images_kwargs":{"size":{"shortest_edge":getattr(proc.image_processor,"min_pixels",None) or proc.image_processor.size["shortest_edge"],"longest_edge":1280*28*28}}}
    inputs = proc.apply_chat_template(msgs, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors="pt", padding=len(group) > 1, **extra)
    inputs.pop("token_type_ids", None)
    inputs = inputs.to(DEVICE)
    table = group[0]["type"] == "table"
    with torch.inference_mode():
        o = model.generate(**inputs, max_new_tokens=2048 if table else 768, do_sample=False, max_time=MAX_TIME, pad_token_id=tok.pad_token_id or tok.eos_token_id)
    n = inputs["input_ids"].shape[-1]
    result = []
    for row in o:
        gen = row[n:]
        gen = gen[gen != (tok.pad_token_id if tok.pad_token_id is not None else -1)]
        result.append((proc.decode(gen, skip_special_tokens=True), proc.decode(gen, skip_special_tokens=False), int(gen.shape[-1])))
    return result

HINTS = os.environ.get("HINTS") == "1"
jobs = []
for pd in map(Path, paper_dirs):
    items = json.loads((pd/("items_h.json" if HINTS else "items.json")).read_text())
    hints = json.loads((pd/"hints.json").read_text()) if HINTS else {}
    out = pd/(f"out_{name}_h.json" if HINTS else f"out_{name}.json")
    res = json.loads(out.read_text()) if out.exists() and os.environ.get("RERUN") != "1" else {}
    todo = [it for it in items if it["key"] not in res and not (os.environ.get("ONLY") and it["type"] != os.environ["ONLY"])]
    jobs.append((pd, hints, out, res, todo))

with bar() as progress:
    tasks = [progress.add_task(f"{name} {pd.name}", total=len(todo)) for pd, _, _, _, todo in jobs]
    for task, (pd, hints, out, res, todo) in zip(tasks, jobs):
        formulas = sorted((it for it in todo if it["type"] == "formula"), key=lambda it: (it.get("kind", ""), Image.open(pd/os.environ.get("IMG_DIR", "images")/f"{it['key']}.png").size[0]))
        groups = [formulas[i:i+BATCH] for i in range(0, len(formulas), BATCH)] + [[it] for it in todo if it["type"] != "formula"]
        for group in groups:
            t = time.time()
            try:
                outputs = generate(pd, group, hints)
            except Exception as error:
                if len(group) == 1: raise
                print(pd.name, "batch failed, falling back to one at a time:", error, file=sys.stderr, flush=True)
                outputs = [generate(pd, [it], hints)[0] for it in group]
            seconds = round((time.time()-t) / len(group), 2)
            for it, (text, raw, tokens) in zip(group, outputs):
                res[it["key"]] = {"text": text, "raw": raw, "seconds": seconds, "wall": seconds, "tokens": tokens}
                print(pd.name, it["key"], seconds, repr(text[:120]), file=sys.stderr, flush=True)
            out.write_text(json.dumps(res, ensure_ascii=False, indent=1))
            progress.advance(task, len(group))
