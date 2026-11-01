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
    config.addinivalue_line("markers", "slow: minutes of compute; run with FA_SLOW=1")
    config.addinivalue_line("markers", "e2e: browser tests against the built console; run with FA_E2E=1")


def pytest_collection_modifyitems(config, items):
    gates = {"network": "FA_NETWORK", "slow": "FA_SLOW", "e2e": "FA_E2E"}
    for mark, env in gates.items():
        if os.environ.get(env) == "1":
            continue
        skip = pytest.mark.skip(reason=f"{mark} test; set {env}=1")
        for item in items:
            if mark in item.keywords:
                item.add_marker(skip)
