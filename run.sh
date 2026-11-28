#!/bin/sh
set -e
cd "$(dirname "$0")"
HERE=$(pwd)
MODELS=${*:-mineru tele}
export MODELS_DIR="$HERE/models"
WORK="$HERE/work"
PAPERS="$WORK/transformer $WORK/kimi $WORK/kimi15"
mkdir -p "$WORK/pdfs" "$MODELS_DIR"

uv sync -q
case " $MODELS " in *" tele "*) (cd tele && uv sync -q) ;; esac

for m in $MODELS; do
  case $m in
    *:*) continue ;;
    paddle) r=PaddlePaddle/PaddleOCR-VL-1.6 ;;
    mineru) r=opendatalab/MinerU2.5-Pro-2605-1.2B ;;
    glm) r=zai-org/GLM-OCR ;;
    minicpm) r=openbmb/MiniCPM-OCR-1.0 ;;
    ovis) r=ATH-MaaS/OvisOCR3 ;;
    tele) r=XingChen-AGI/TeleOCR ;;
    *) echo "unknown model $m"; exit 1 ;;
  esac
  [ -d "$MODELS_DIR/${r#*/}" ] || .venv/bin/hf download "$r" --local-dir "$MODELS_DIR/${r#*/}" > /dev/null
done

for p in transformer:1706.03762v7 kimi:2507.20534 kimi15:2501.12599; do
  n=${p%%:*}
  [ -f "$WORK/pdfs/$n.pdf" ] || curl -sL -o "$WORK/pdfs/$n.pdf" "https://arxiv.org/pdf/${p#*:}"
  [ -f "$WORK/$n/items.json" ] || .venv/bin/python ocr/extract.py "$WORK/pdfs/$n.pdf" "$WORK/$n"
done

for m in $MODELS; do
  case $m in
    *:*) .venv/bin/python ocr/run_api.py "${m%%:*}" "${m#*:}" $PAPERS ;;
    tele) tele/.venv/bin/python ocr/run_model.py tele $PAPERS ;;
    *) .venv/bin/python ocr/run_model.py "$m" $PAPERS ;;
  esac 2> "$WORK/log_${m%%:*}.txt" || { echo "$m failed, last lines of $WORK/log_${m%%:*}.txt:"; tail -25 "$WORK/log_${m%%:*}.txt"; continue; }
  .venv/bin/python ocr/verify.py "${m%%:*}" $PAPERS > /dev/null
done
.venv/bin/python ocr/summary.py "$WORK" | tee "$WORK/summary.tsv"
