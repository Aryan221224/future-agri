#!/usr/bin/env bash
# Run on the H200 host (PuTTY), before anything else:
#   cd /home/paawan_kumar/futureagri && bash ml/h200/preflight.sh
# Prints: free GPUs, free disk in your home, whether Docker works, and which download
# hosts the server can reach (the training needs PyPI, HuggingFace, the dataset host, and
# Google Cloud Storage for the RF-DETR pretrained weights).
set -u
echo "== user / home =="; id -un; echo "$HOME"
echo; echo "== GPUs (index, used MiB, total MiB, util %) =="
nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>&1
echo; echo "== free disk in home =="; df -h "$HOME" | tail -1
echo; echo "== docker =="; docker --version 2>&1; docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' 2>&1 | grep -E "pytorch|tensorflow" | head
echo; echo "== network (HTTP code; 000 = blocked) =="
for u in https://pypi.org/simple/pip/ https://files.pythonhosted.org/ https://huggingface.co/ \
         https://doidata.gfz-potsdam.de/weedsgalore_e_celikkan_2024/ https://storage.googleapis.com/ \
         https://download.pytorch.org/ ; do
  printf '%-70s ' "$u"; curl -s -o /dev/null -m 10 -w '%{http_code}\n' -I "$u" || echo 000
done
echo; echo "If any line above is 000, tell Claude which: that host must be reached from your laptop instead (see ml/H200.md, 'Blocked downloads')."
