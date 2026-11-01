"""Browser tests against the built console: uvicorn in a thread over a temporary
data folder, the farm builder patched to the synthetic 1 ha farm (offline), and
Chromium via Playwright. Run with FA_E2E=1 after ``npm run build``."""
import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
SHOTS = HERE.parents[1] / "runs" / "console_e2e"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="session")
def console(tmp_path_factory):
    import uvicorn
    from fa_console.app import STATIC, create_app
    from fa_console.config import Settings
    from test_console_farms import fake_builder
    if not (STATIC / "index.html").exists():
        pytest.skip("console not built: run npm run build in apps/console")
    home = tmp_path_factory.mktemp("e2e")
    port = _free_port()
    app = create_app(Settings(home=home / "home", cache=home / "cache", offline=True, port=port), start_workers=True)
    app.state.farms.builder = fake_builder
    app.state.sessions.defaults["plan_members"] = 4
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    yield {"base": base, "app": app}
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, console):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
    yield pg
    ctx.close()


def shot(pg, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    pg.screenshot(path=str(SHOTS / f"{name}.png"), full_page=False)


def square(side_m=100, lat=52.52, lon=5.62) -> dict:
    import math
    dlat = side_m / 111_320
    dlon = side_m / (111_320 * math.cos(math.radians(lat)))
    ring = [[lon, lat], [lon + dlon, lat], [lon + dlon, lat + dlat], [lon, lat + dlat], [lon, lat]]
    return {"type": "Polygon", "coordinates": [ring]}


def make_farm(console, name="E2E field") -> str:
    """Create and build a farm through the API (the wizard has its own test)."""
    body = json.dumps({"name": name, "mode": "sim", "template": "A5", "polygon": square(), "year": 2025}).encode()
    req = urllib.request.Request(console["base"] + "/api/farms", data=body, headers={"Content-Type": "application/json"})
    fid = json.loads(urllib.request.urlopen(req).read())["farm"]["id"]
    for _ in range(300):
        f = json.loads(urllib.request.urlopen(console["base"] + f"/api/farms/{fid}").read())
        if f["status"] in ("ready", "error"):
            break
        time.sleep(0.2)
    assert f["status"] == "ready", f
    return fid
