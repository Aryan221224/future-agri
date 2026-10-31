"""Local dev server: the API under /api and the web pages at /, as nginx does in docker.

    make twin-ui        # then open http://localhost:8099/twin.html
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/twin", "services/planner", "services/gateway",
          "services/loop", "services/api"):
    sys.path.insert(0, str(ROOT / p))

from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from fa_api.main import app as api  # noqa: E402

app = FastAPI(title="Future Agri dev server")
app.mount("/api", api)
app.mount("/", StaticFiles(directory=str(ROOT / "web"), html=True))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8099)
