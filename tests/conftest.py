import os
import sys
import tempfile
from pathlib import Path

# The API persists the review queue; keep tests out of the user's home directory.
os.environ.setdefault("FA_STATE_DIR", tempfile.mkdtemp(prefix="fa_state_"))

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/twin", "services/planner",
          "services/gateway", "services/loop", "services/api", "sim", "ml"):
    sys.path.insert(0, str(ROOT / p))
