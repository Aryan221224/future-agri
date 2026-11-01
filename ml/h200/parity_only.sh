#!/usr/bin/env bash
# Parity + latency benchmark (matched-detection parity, ml/h200/export_parity.py = new ml/export.py) of the EXISTING ONNX export (no export, no training). Detached; log: ml/runs/<name>/parity_log.txt
set -euo pipefail
NAME="${1:-wg-medium}"
IMAGE="${IMAGE:-nvcr.io/nvidia/pytorch:26.04-py3}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN="$REPO/ml/runs/$NAME"
case "$REPO" in "$HOME"/*) ;; *) echo "Repo must be inside $HOME"; exit 2;; esac
[ -d "$RUN" ] || { echo "MISSING_RUN $RUN"; exit 2; }
ls "$RUN"/*.pth >/dev/null 2>&1 || { echo "MISSING_CHECKPOINT in $RUN"; exit 2; }
ls "$RUN"/export/*.onnx >/dev/null 2>&1 || { echo "MISSING_ONNX in $RUN/export"; exit 2; }
if docker ps --format '{{.Names}}' | grep -qx "fa-parity-$NAME"; then echo "Started. (already running)"; exit 0; fi
read -r GPU USED UTIL < <(nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader,nounits \
    | tr -d ' ' | awk -F, '{print $1, $2, $3}' | sort -k2,2n | head -1)
# the GPUs are shared and partly reserved: take one under 40 % load with at most 12 GB in use
if [ "${USED:-999999}" -gt 12000 ] || [ "${UTIL:-100}" -gt 40 ]; then echo "No idle GPU (best GPU $GPU: ${USED} MiB, ${UTIL}%)"; exit 3; fi
rm -f "$RUN/parity_log.txt"
mkdir -p "$HOME/.fa_home" "$HOME/.fa_pyuser" "$HOME/.fa_cache"
docker run -d --rm --name "fa-parity-$NAME" --gpus "device=$GPU" --shm-size=8g \
  --user "$(id -u):$(id -g)" -v "$HOME:$HOME" -w "$REPO" \
  -e HOME="$HOME/.fa_home" -e PYTHONUSERBASE="$HOME/.fa_pyuser" -e USER="$(id -un)" -e LOGNAME="$(id -un)" \
  -e TORCHINDUCTOR_CACHE_DIR="$HOME/.fa_cache/inductor" -e TRITON_CACHE_DIR="$HOME/.fa_cache/triton" \
  -e XDG_CACHE_HOME="$HOME/.fa_cache" -e PYTHONUNBUFFERED=1 "$IMAGE" bash -c '
    LOG=ml/runs/'"$NAME"'/parity_log.txt
    export PATH=$PYTHONUSERBASE/bin:$PATH
    { pip install --user "rfdetr[onnx]==1.10.1" && PYTHONPATH=ml:$PYTHONPATH python ml/h200/export_parity.py --run ml/runs/'"$NAME"' --skip-export; } 2>&1 | tee -a "$LOG"
    echo "PARITY_DONE rc=${PIPESTATUS[0]}" | tee -a "$LOG"'
echo "Started on GPU $GPU."
