#!/usr/bin/env bash
# Global twin benchmark on the H200 server's CPUs (no GPU). Detached; log: $HOME/fa_global/runs/bench_log.txt
set -euo pipefail
IMAGE="${IMAGE:-nvcr.io/nvidia/pytorch:26.04-py3}"
REPO="$HOME/fa_global"
SEEDS="${SEEDS:-5}"
WORKERS="${WORKERS:-32}"
[ -d "$REPO" ] || { echo "MISSING_REPO $REPO"; exit 2; }
if docker ps --format '{{.Names}}' | grep -qx "fa-twin-bench"; then echo "Started. (already running)"; exit 0; fi
mkdir -p "$REPO/runs" "$HOME/.fa_home" "$HOME/.fa_pyuser" "$HOME/.fa_cache"
rm -f "$REPO/runs/bench_log.txt"
docker run -d --rm --name "fa-twin-bench" --cpus="${WORKERS}" --shm-size=8g \
  --user "$(id -u):$(id -g)" -v "$HOME:$HOME" -w "$REPO" \
  -e HOME="$HOME/.fa_home" -e PYTHONUSERBASE="$HOME/.fa_pyuser" -e USER="$(id -un)" \
  -e FA_DATA_CACHE="$REPO/data_cache" -e XDG_CACHE_HOME="$HOME/.fa_cache" -e PYTHONUNBUFFERED=1 "$IMAGE" bash -c '
    LOG=runs/bench_log.txt
    export PATH=$PYTHONUSERBASE/bin:$PATH
    { pip install --user -q pcse==6.0.13 "pandas>=2.2,<3" ortools shapely pyproj rasterio xarray zarr fsspec s3fs \
          pystac-client pyarrow icechunk requests pyyaml scipy aiohttp \
      && python bench/archetype_runs.py --seeds '"$SEEDS"' --workers '"$WORKERS"'; } 2>&1 | tee -a "$LOG"
    echo "BENCH_DONE rc=${PIPESTATUS[0]}" | tee -a "$LOG"'
echo "Started twin benchmark (seeds=$SEEDS, workers=$WORKERS)."
