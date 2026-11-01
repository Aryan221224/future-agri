#!/usr/bin/env bash
# Runs the Future Agri worker on this GPU server. Started by Connect-GPU-Server on the
# laptop, inside its SSH session, which tunnels 127.0.0.1:$FA_TUNNEL_PORT here back to
# the console. The worker stops by itself when that session closes (console unreachable
# for 2 minutes).
#
#   FA_WORKER_MODE  auto (default: docker if the image is here, else a venv) | docker | venv
#   FA_GPU          auto (default: the idlest GPU, none if all are busy) | <index> | none
#   FA_KINDS        perfect,mission,detect
#   FA_DETECTOR_WEIGHTS  an ml/runs/<name> folder; default: newest in ~/futureagri/ml/runs
set -euo pipefail
umask 077
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${FA_TUNNEL_PORT:-18099}"
KINDS="${FA_KINDS:-perfect,mission,detect}"
MODE="${FA_WORKER_MODE:-auto}"
GPU="${FA_GPU:-auto}"
IMAGE="${FA_IMAGE:-nvcr.io/nvidia/pytorch:26.04-py3}"
NAME="${FA_WORKER_NAME:-$(hostname -s)}"
TOKEN_FILE="$DIR/worker.token"
REQ="$DIR/scripts/console/requirements-worker.txt"
CACHE="${FA_DATA_CACHE:-$HOME/.fa_cache/futureagri}"
WHOME="$HOME/.futureagri/worker"
PP=""
for p in packages/fa_core services/twin services/planner services/gateway services/loop services/farm \
         services/scan services/drone services/runner services/console; do PP="$PP:$DIR/$p"; done
export PYTHONPATH="${PP#:}"
[ -f "$TOKEN_FILE" ] || { echo "The worker token is missing: run Connect-GPU-Server again on the laptop."; exit 2; }

if [ -z "${FA_DETECTOR_WEIGHTS:-}" ]; then
  for r in $(ls -dt "$HOME"/futureagri/ml/runs/*/ 2>/dev/null); do
    if [ -f "$r/run.json" ]; then FA_DETECTOR_WEIGHTS="${r%/}"; break; fi
  done
fi
export FA_DETECTOR_WEIGHTS="${FA_DETECTOR_WEIGHTS:-}"
mkdir -p "$CACHE" "$WHOME"
ARGS=(--server "http://127.0.0.1:$PORT" --token-file "$TOKEN_FILE" --name "$NAME" --kinds "$KINDS"
      --home "$WHOME" --cache "$CACHE" --give-up-after 120)

if [ "$MODE" = auto ]; then
  if command -v docker >/dev/null 2>&1 && docker image inspect "$IMAGE" >/dev/null 2>&1; then MODE=docker; else MODE=venv; fi
fi
echo "Future Agri worker on $(hostname -s): $MODE mode, console via 127.0.0.1:$PORT, jobs: $KINDS"
if [ -n "$FA_DETECTOR_WEIGHTS" ]; then echo "weed detector: $FA_DETECTOR_WEIGHTS"; else echo "weed detector: none found, detection stays off"; fi

if [ "$MODE" = docker ]; then
  GPUARG=()
  if [ "$GPU" = auto ]; then
    GPU=none
    if command -v nvidia-smi >/dev/null 2>&1; then
      # the GPUs are shared: take the idlest one, or none if every one is busy
      read -r G USED UTIL < <(nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader,nounits \
          | tr -d ' ' | awk -F, '{print $1, $2, $3}' | sort -k2,2n | head -1) || true
      if [ -n "${G:-}" ] && [ "${USED:-999999}" -le 8000 ] && [ "${UTIL:-100}" -le 50 ]; then GPU="$G"; fi
    fi
  fi
  if [ "$GPU" != none ]; then GPUARG=(--gpus "device=$GPU"); echo "using GPU $GPU"; else echo "no idle GPU: CPU only (iterations are CPU work anyway)"; fi
  CNAME="fa-worker-$(id -un)"
  docker rm -f "$CNAME" >/dev/null 2>&1 || true            # an older worker of ours, left behind
  trap 'docker stop -t 5 "$CNAME" >/dev/null 2>&1 || true' EXIT
  mkdir -p "$HOME/.fa_home" "$HOME/.fa_pyuser"
  docker run --rm --init --name "$CNAME" --network host ${GPUARG[@]+"${GPUARG[@]}"} --shm-size=8g \
    --user "$(id -u):$(id -g)" -v "$HOME:$HOME" -w "$DIR" \
    -e HOME="$HOME/.fa_home" -e PYTHONUSERBASE="$HOME/.fa_pyuser" -e PYTHONPATH -e FA_DETECTOR_WEIGHTS \
    -e XDG_CACHE_HOME="$HOME/.fa_cache" -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PYTHONUNBUFFERED=1 \
    "$IMAGE" bash -c 'pip install --user -q -r "$0" "rfdetr==1.10.1" && exec python -m fa_console.worker "$@"' "$REQ" "${ARGS[@]}"
else
  PY="${FA_PYTHON:-python3}"
  VENV="$HOME/.futureagri/venv"
  [ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install -q --disable-pip-version-check -r "$REQ"
  PIDF="$WHOME/worker.pid"
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then kill "$(cat "$PIDF")" 2>/dev/null || true; sleep 1; fi
  PYTHONUNBUFFERED=1 "$VENV/bin/python" -m fa_console.worker "${ARGS[@]}" &
  echo $! > "$PIDF"
  trap 'kill "$(cat "$PIDF")" 2>/dev/null || true; rm -f "$PIDF"' EXIT
  wait
fi
