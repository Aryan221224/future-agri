import os
import sys
import tempfile
from pathlib import Path

# The API persists the review queue; keep tests out of the user's home directory.
os.environ.setdefault("FA_STATE_DIR", tempfile.mkdtemp(prefix="fa_state_"))

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/twin", "services/planner",
          "services/gateway", "services/loop", "services/api", "services/farm",
          "services/scan", "services/drone", "services/runner", "services/console", "sim", "ml"):
    sys.path.insert(0, str(ROOT / p))


import pytest  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "network: needs internet; run with FA_NETWORK=1")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("FA_NETWORK") == "1":
        return
    skip = pytest.mark.skip(reason="network test; set FA_NETWORK=1")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
