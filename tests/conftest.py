import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/twin", "services/planner",
          "services/gateway", "sim", "ml"):
    sys.path.insert(0, str(ROOT / p))
