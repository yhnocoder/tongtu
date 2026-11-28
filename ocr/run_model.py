import json, sys, time, torch
from pathlib import Path
from PIL import Image
from transformers import AutoProcessor, AutoModelForImageTextToText
import os
DEVICE = os.environ.get("DEVICE") or ("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
if DEVICE == "cpu": torch.set_num_threads(os.cpu_count())
MAX_PIXELS = 1280 * 28 * 28
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
print("load", round(time.time()-t0,1), DEVICE, flush=True)
for pd in map(Path, paper_dirs):
    HINTS = os.environ.get("HINTS") == "1"
    items = json.loads((pd/("items_h.json" if HINTS else "items.json")).read_text())
    hints = json.loads((pd/"hints.json").read_text()) if HINTS else {}
    out = pd/(f"out_{name}_h.json" if HINTS else f"out_{name}.json")
    res = json.loads(out.read_text()) if out.exists() else {}
    for it in items:
        k = it["key"]
        if k in res: continue
        if os.environ.get("ONLY") and it["type"] != os.environ["ONLY"]: continue
        img = Image.open(pd/os.environ.get("IMG_DIR", "images")/f"{k}.png").convert("RGB")
        scale = min(1.0, (MAX_PIXELS / (img.width * img.height)) ** 0.5)
        if scale < 1: img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        prompt = fp if it["type"] == "formula" else tp
        if name == "minicpm2" and it.get("kind") == "inline":
            prompt = "Text Recognition:"
        if HINTS:
            prompt = prompt + "\nThe PDF text layer of this image, as font#colour:text pieces in drawing order: " + " | ".join(hints[k]) + "\nUse exactly these characters. Fonts whose name contains Bold, Medi, BX or MIB are bold (write \\bm{} or \\mathbf{}); a colour other than 000000 is written as {\\color[HTML]{RRGGBB} ...}."
        msgs = [{"role":"user","content":[{"type":"image","image":img},{"type":"text","text":prompt}]}]
        extra = {}
        if name == "paddle":
            extra = {"images_kwargs":{"size":{"shortest_edge":getattr(proc.image_processor,"min_pixels",None) or proc.image_processor.size["shortest_edge"],"longest_edge":1280*28*28}}}
        inputs = proc.apply_chat_template(msgs, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors="pt", **extra)
        inputs.pop("token_type_ids", None)
        inputs = inputs.to(DEVICE)
        t = time.time()
        with torch.inference_mode():
            o = model.generate(**inputs, max_new_tokens=2048 if it["type"]=="table" else 768, do_sample=False, max_time=MAX_TIME)
        gen = o[0][inputs["input_ids"].shape[-1]:]
        text = proc.decode(gen, skip_special_tokens=True)
        raw = proc.decode(gen, skip_special_tokens=False)
        res[k] = {"text": text, "raw": raw, "seconds": round(time.time()-t, 2), "tokens": int(o.shape[-1]-inputs["input_ids"].shape[-1])}
        print(pd.name, k, res[k]["seconds"], repr(text[:120]), flush=True)
        out.write_text(json.dumps(res, ensure_ascii=False, indent=1))
