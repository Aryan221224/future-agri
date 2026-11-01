#!/usr/bin/env bash
# Closed-loop benchmark on the H200 server's CPUs (no GPU). Detached; log: $HOME/fa_global/runs/closed_loop_log.txt
# Results: $HOME/fa_global/ml/results/closed_loop/closed_loop.{json,md}
set -euo pipefail
IMAGE="${IMAGE:-nvcr.io/nvidia/pytorch:26.04-py3}"
REPO="$HOME/fa_global"
SEEDS="${SEEDS:-0-19}"
TEMPLATES="${TEMPLATES:-all}"
WORKERS="${WORKERS:-32}"
[ -d "$REPO" ] || { echo "MISSING_REPO $REPO"; exit 2; }
if docker ps --format '{{.Names}}' | grep -qx "fa-closed-loop"; then echo "Started. (already running)"; exit 0; fi
mkdir -p "$REPO/runs" "$HOME/.fa_home" "$HOME/.fa_pyuser" "$HOME/.fa_cache"
rm -f "$REPO/runs/closed_loop_log.txt"
docker run -d --rm --name "fa-closed-loop" --cpus="${WORKERS}" --shm-size=8g \
  --user "$(id -u):$(id -g)" -v "$HOME:$HOME" -w "$REPO" \
  -e HOME="$HOME/.fa_home" -e PYTHONUSERBASE="$HOME/.fa_pyuser" -e USER="$(id -un)" \
  -e FA_DATA_CACHE="$REPO/data_cache" -e XDG_CACHE_HOME="$HOME/.fa_cache" -e PYTHONUNBUFFERED=1 "$IMAGE" bash -c '
    LOG=runs/closed_loop_log.txt
    export PATH=$PYTHONUSERBASE/bin:$PATH
    { pip install --user -q pcse==6.0.13 "pandas>=2.2,<3" ortools shapely pyproj rasterio xarray zarr fsspec s3fs \
          pystac-client pyarrow icechunk requests pyyaml scipy aiohttp \
      && python ml/closed_loop_bench.py --templates '"$TEMPLATES"' --seeds '"$SEEDS"' --workers '"$WORKERS"'; } 2>&1 | tee -a "$LOG"
    echo "CLOSED_LOOP_DONE rc=${PIPESTATUS[0]}" | tee -a "$LOG"'
echo "Started closed-loop benchmark (templates=$TEMPLATES, seeds=$SEEDS, workers=$WORKERS)."
