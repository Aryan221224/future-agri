"""Make the repository's service packages importable when the console runs from a
checkout (``python -m fa_console`` with ``services/console`` on the path)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for p in ("packages/fa_core", "services/twin", "services/planner", "services/loop", "services/farm",
          "services/scan", "services/drone", "services/runner", "services/console", "services/agent", "services/aircraft"):
    s = str(ROOT / p)
    if (ROOT / p).exists() and s not in sys.path:
        sys.path.insert(0, s)
