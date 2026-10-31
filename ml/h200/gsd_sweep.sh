#!/usr/bin/env bash
# Detector GSD sweep (no training). Detached; log: ml/runs/<name>/gsd_log.txt
set -euo pipefail
NAME="${1:-wg-medium}"
IMAGE="${IMAGE:-nvcr.io/nvidia/pytorch:26.04-py3}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN="$REPO/ml/runs/$NAME"
DATA="${DATA:-$HOME/data/weedsgalore_coco}"
case "$REPO" in "$HOME"/*) ;; *) echo "Repo must be inside $HOME"; exit 2;; esac
[ -d "$RUN" ] || { echo "MISSING_RUN $RUN"; exit 2; }
if docker ps --format '{{.Names}}' | grep -qx "fa-gsd-$NAME"; then echo "Started. (already running)"; exit 0; fi
read -r GPU USED UTIL < <(nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader,nounits \
    | tr -d ' ' | awk -F, '{print $1, $2, $3}' | sort -k2,2n | head -1)
if [ "${USED:-999999}" -gt 4000 ] || [ "${UTIL:-100}" -gt 40 ]; then echo "No idle GPU (best GPU $GPU: ${USED} MiB, ${UTIL}%)"; exit 3; fi
rm -f "$RUN/gsd_log.txt"
mkdir -p "$HOME/.fa_home" "$HOME/.fa_pyuser" "$HOME/.fa_cache"
docker run -d --rm --name "fa-gsd-$NAME" --gpus "device=$GPU" --shm-size=8g \
  --user "$(id -u):$(id -g)" -v "$HOME:$HOME" -w "$REPO" \
  -e HOME="$HOME/.fa_home" -e PYTHONUSERBASE="$HOME/.fa_pyuser" -e USER="$(id -un)" -e LOGNAME="$(id -un)" \
  -e XDG_CACHE_HOME="$HOME/.fa_cache" -e PYTHONUNBUFFERED=1 "$IMAGE" bash -c '
    LOG=ml/runs/'"$NAME"'/gsd_log.txt
    export PATH=$PYTHONUSERBASE/bin:$PATH
    { pip install --user "rfdetr==1.10.1" && PYTHONPATH=ml:services/gateway:packages/fa_core:$PYTHONPATH python ml/h200/gsd_sweep.py --run ml/runs/'"$NAME"' --data '"$DATA"' --out ml/results/'"$NAME"'/gsd_sweep.json; } 2>&1 | tee -a "$LOG"
    echo "GSD_DONE rc=${PIPESTATUS[0]}" | tee -a "$LOG"'
echo "Started on GPU $GPU."
