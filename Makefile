.PHONY: help install demo test bench lint licences up down clean sitl ml-smoke
.DEFAULT_GOAL := help

PY ?= python3
PYTHONPATH := packages/fa_core:services/twin:services/planner:services/gateway:sim
export PYTHONPATH

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install Python dependencies
	$(PY) -m pip install -r requirements.txt

demo:  ## Run the full closed loop with no hardware (~45 s)
	$(PY) scripts/demo.py

test:  ## Run the test suite
	$(PY) -m pytest tests/ -q -p no:warnings

bench: ## Reproduce every number quoted in the docs (~10 min)
	$(PY) bench/osse_assimilation.py --trials 16 --members 30 --cadence-days 7
	$(PY) bench/planner_comparison.py --scenarios 5 --epochs 4 --beam 5
	$(PY) bench/bandwidth.py
	$(PY) bench/conformal_coverage.py

ml-smoke:  ## Train/evaluate/export on synthetic data (CPU, checks plumbing only)
	$(PY) ml/prepare_data.py --synthetic
	$(PY) ml/train.py --smoke --size nano --resolution 384 --device cpu --data ../data/weedsgalore_synthetic_coco
	$(PY) ml/evaluate.py --run ml/runs/smoke-nano --min-stratum 5
	$(PY) ml/export.py --run ml/runs/smoke-nano --images 3 --iters 5

licences:  ## Fail if copyleft enters the runtime dependency tree
	$(PY) scripts/check_licences.py
	$(PY) scripts/check_licences.py --requirements ml/requirements-gpu.txt

lint:  ## Static checks
	$(PY) -m ruff check . || true
	$(PY) -m pyflakes packages services sim bench scripts tests || true

up:  ## Start the stack (db, mqtt, api, ingest, web)
	docker compose up -d --build

down:  ## Stop the stack
	docker compose down

sitl:  ## Print the verified PX4 SITL setup (see docs/adr/0007)
	@echo "Ubuntu 24.04 + PX4 v1.17.0 + Gazebo Harmonic + mavsdk 3.17.2"
	@echo "  git clone https://github.com/PX4/PX4-Autopilot.git --recursive"
	@echo "  cd PX4-Autopilot && git checkout v1.17.0 && git submodule update --init --recursive"
	@echo "  bash ./Tools/setup/ubuntu.sh   # installs gz-harmonic; then reboot"
	@echo "  make px4_sitl gz_x500_mono_cam_down"
	@echo "NOTE: mission CameraActions produce NO images in gz -- see docs/adr/0007"

clean:  ## Remove caches and generated results
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache bench/results
