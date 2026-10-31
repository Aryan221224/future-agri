#!/usr/bin/env bash
# Runs INSIDE the NVIDIA PyTorch container (started by launch.sh). No sudo, installs go to
# $PYTHONUSERBASE (kept between runs). Never touches NVIDIA's own torch build.
set -uo pipefail
NAME="${FA_NAME:-wg-medium}"; SIZE="${FA_SIZE:-medium}"; EPOCHS="${FA_EPOCHS:-150}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
RUN="ml/runs/$NAME"; mkdir -p "$RUN"; LOG="$RUN/pipeline_log.txt"
export PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1
export PATH="$PYTHONUSERBASE/bin:$PATH"
log() { echo "$@" | tee -a "$LOG"; }
step() {  # step "title" cmd...   (stops the whole run on failure)
  local t="$1"; shift; log ""; log "=== $t === $(date -Is)"
  local t0=$SECONDS
  "$@" 2>&1 | tee -a "$LOG"; local rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then log "!!! $t FAILED (exit $rc). Send this file: $PWD/$LOG"; exit "$rc"; fi
  log "    $t done in $(( (SECONDS - t0) / 60 )) min"
}
log "##### $(date -Is) name=$NAME size=$SIZE epochs=$EPOCHS host=$(hostname) gpu=$NVIDIA_VISIBLE_DEVICES"

step "GPU check" python -c "
import torch,sys
ok=torch.cuda.is_available()
print('torch',torch.__version__,'cuda',ok,torch.version.cuda, torch.cuda.get_device_name(0) if ok else '')
sys.exit(0 if ok else 3)"

# Keep NVIDIA's torch/torchvision exactly as they are while resolving rfdetr's dependencies.
python - > "$RUN/constraints.auto.txt" <<'PY'
from importlib.metadata import version
for p in ("torch", "torchvision"):
    try: print(f"{p}=={version(p)}")
    except Exception: pass
PY
log "constraints: $(tr '\n' ' ' < "$RUN/constraints.auto.txt")"

need=0; python - <<'PY' || need=1
import importlib.util, sys
mods = ["rfdetr", "pycocotools", "matplotlib", "PIL", "numpy", "scipy", "onnx", "onnxruntime"]
bad = [m for m in mods if importlib.util.find_spec(m) is None]
print("missing:", bad); sys.exit(1 if bad else 0)
PY
if [ "$need" -eq 1 ]; then
  log "installing rfdetr and helpers into $PYTHONUSERBASE ..."
  if ! pip install --user -c "$RUN/constraints.auto.txt" "rfdetr[train,onnx]==1.10.1" pycocotools matplotlib pillow scipy 2>&1 | tee -a "$LOG" | tail -3 \
     || ! python -c "import rfdetr"; then
    log "full install failed; retrying without the ONNX extra (export step will be skipped)"
    touch "$RUN/.no_onnx"
    step "pip install (train only)" pip install --user -c "$RUN/constraints.auto.txt" "rfdetr[train]==1.10.1" pycocotools matplotlib pillow scipy
  fi
fi
python -c "import rfdetr,torch; print('rfdetr ok, torch', torch.__version__)" 2>&1 | tee -a "$LOG" || { log "!!! rfdetr import failed"; exit 4; }

DATA="$FA_DATA/weedsgalore_coco"
if [ ! -f "$DATA/train/_annotations.coco.json" ]; then
  ZIP="$FA_DATA/downloads/weedsgalore-dataset.zip"
  if [ -f "$ZIP" ]; then step "1/4 data (from uploaded zip)" python ml/prepare_data.py --zip "$ZIP"
  else step "1/4 data (download 321 MB + convert)" python ml/prepare_data.py; fi
else log "dataset already prepared: $DATA"; fi

step "2/4 train RF-DETR-$SIZE" python ml/train.py --data "$DATA" --size "$SIZE" --name "$NAME" --epochs "$EPOCHS" --batch-size auto --workers 4
step "3/4 evaluate + calibrate" python ml/evaluate.py --run "$RUN"
if [ -f "$RUN/.no_onnx" ]; then log "export skipped (no ONNX packages)"
else step "4/4 export + latency" python ml/export.py --run "$RUN"; fi

# disk is tight: keep only the best checkpoints
find "$RUN" -maxdepth 1 -name 'checkpoint[0-9]*.pth' -delete 2>/dev/null
log ""; log "DONE $(date -Is). Results: ml/results/$NAME  (report.md, MODEL_CARD.md, coverage.png, shift.png, latency.json)"
du -sh "$RUN" "ml/results/$NAME" 2>/dev/null | tee -a "$LOG"
