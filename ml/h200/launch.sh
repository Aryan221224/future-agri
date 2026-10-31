#!/usr/bin/env bash
# Start the perception training on a FREE H200 GPU, detached (survives SSH drop and laptop sleep).
#
#   cd /home/paawan_kumar/futureagri
#   bash ml/h200/launch.sh                 # RF-DETR-Medium, 150 epochs, picks the freest GPU
#   bash ml/h200/launch.sh large 100       # size, epochs
#   GPU=3 bash ml/h200/launch.sh           # force a GPU index
#
# Everything lives under $HOME. The container is removed when it ends; the log stays in
# ml/runs/<name>/pipeline_log.txt.
set -euo pipefail
SIZE="${1:-medium}"; EPOCHS="${2:-150}"; NAME="${3:-wg-$SIZE}"
IMAGE="${IMAGE:-nvcr.io/nvidia/pytorch:26.04-py3}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
case "$REPO" in "$HOME"/*) ;; *) echo "Repo must be inside $HOME (is: $REPO)"; exit 2;; esac
[ -f "$REPO/ml/train.py" ] || { echo "Run from the futureagri folder (ml/train.py not found)"; exit 2; }

GPU="${GPU:-auto}"
if [ "$GPU" = "auto" ]; then
  # least memory in use; refuse if every GPU is busy (the GPUs are shared)
  read -r GPU USED UTIL < <(nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader,nounits \
      | tr -d ' ' | awk -F, '{print $1, $2, $3}' | sort -k2,2n | head -1)
  if [ "${USED:-999999}" -gt 4000 ] || [ "${UTIL:-100}" -gt 40 ]; then
    echo "No idle GPU (best is GPU $GPU: ${USED} MiB used, ${UTIL}% util)."
    nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv
    echo "Wait, or force one you know is free:  GPU=<n> bash ml/h200/launch.sh"; exit 3
  fi
fi
echo "Using GPU $GPU, image $IMAGE, run name $NAME"

mkdir -p "$HOME/data" "$HOME/.fa_home" "$HOME/.fa_pyuser" "$HOME/.fa_cache" "$REPO/ml/runs/$NAME"
docker run -d --rm --name "fa-$NAME" \
  --gpus "device=$GPU" --shm-size=16g \
  --user "$(id -u):$(id -g)" \
  -v "$HOME:$HOME" -w "$REPO" \
  -e HOME="$HOME/.fa_home" -e PYTHONUSERBASE="$HOME/.fa_pyuser" \
  -e FA_DATA="$HOME/data" -e HF_HOME="$HOME/.fa_cache/hf" -e TORCH_HOME="$HOME/.fa_cache/torch" \
  -e XDG_CACHE_HOME="$HOME/.fa_cache" -e FA_NAME="$NAME" -e FA_SIZE="$SIZE" -e FA_EPOCHS="$EPOCHS" \
  "$IMAGE" bash "$REPO/ml/h200/in_container.sh"
echo
echo "Started. Watch it:      tail -f $REPO/ml/runs/$NAME/pipeline_log.txt"
echo "Is it running:          docker ps --filter name=fa-$NAME"
echo "Stop it (frees GPU):    docker stop fa-$NAME"
echo "You can close PuTTY; the job keeps running."
