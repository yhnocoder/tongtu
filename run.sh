#!/bin/sh
set -e
cd "$(dirname "$0")"
WORK="$(pwd)/work"
RUNS=${*:-textlayer:textlayer tele_mlx:tele}
PAPERS=${PAPERS:-"flashattn:2205.14135 adam:1412.6980 ddpm:2006.11239 ppo:1707.06347 grpo:2402.03300 specdec:2211.17192 sglang:2312.07104 outlines:2307.09702 pytorch:1912.01703 drb:2308.07505 awkward:2303.02205 autosp:2604.27089 pyprompt:2408.09134 pastalean:2610.34205"}
export API_BASE=${API_BASE:-http://127.0.0.1:11000/v1}
export API_MODEL=${API_MODEL:-TeleOCR-oQ8-MLX}
mkdir -p "$WORK/pdfs"
uv sync -q

NAMES=""
for p in $PAPERS; do
  n=${p%%:*}
  NAMES="$NAMES $n"
  [ -f "$WORK/pdfs/$n.pdf" ] || curl -sfL -o "$WORK/pdfs/$n.pdf" "https://arxiv.org/pdf/${p#*:}"
  [ -f "$WORK/$n/items.json" ] || .venv/bin/python trial/extract.py "$WORK/pdfs/$n.pdf" "$WORK/$n" 2> "$WORK/log_extract_$n.txt"
done
DIRS=""
for n in $NAMES; do DIRS="$DIRS $WORK/$n"; done

START=$(date +%s)
for r in $RUNS; do
  label=${r%%:*}
  family=${r#*:}
  if .venv/bin/python trial/recognize.py "$label" "$family" $DIRS 2> "$WORK/log_$label.txt"; then
    .venv/bin/python trial/judge.py "$label" $DIRS 2>> "$WORK/log_$label.txt"
  else
    echo "$label failed, last lines of $WORK/log_$label.txt:"
    tail -25 "$WORK/log_$label.txt"
  fi
done
echo "识别加比对共 $(( $(date +%s) - START )) 秒"
.venv/bin/python trial/report.py "$WORK" "$NAMES" | tee "$WORK/summary.tsv"
echo "逐条对照: $WORK/report.html"
