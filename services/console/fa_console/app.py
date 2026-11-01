"""FastAPI application: /api routers, the SPA and its PWA files, error mapping."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from . import _paths  # noqa: F401  (service packages on sys.path)
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402

from . import __version__  # noqa: E402
from .config import Settings  # noqa: E402
from .errors import Busy, NotFound, TooLarge  # noqa: E402
from .jobs import JobManager  # noqa: E402
from .store import Store  # noqa: E402

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("fa_console")

PLACEHOLDER = """<!doctype html><html><head><meta charset="utf-8"><title>Future Agri Console</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:system-ui;padding:2rem;max-width:40rem">
<h1>Future Agri Mission Console</h1><p>The API is running. The web interface has not been built
yet: run <code>make console-build</code> (or <code>npm run build</code> in <code>apps/console</code>).
API docs: <a href="/api/docs">/api/docs</a>.</p></body></html>"""


def create_app(settings: Settings | None = None, start_workers: bool = True) -> FastAPI:
    settings = settings or Settings()
    settings.home.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    jobs = JobManager(store, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_workers:
            jobs.start()
        yield
        jobs.stop()

    app = FastAPI(title="Future Agri Mission Console", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.settings, app.state.store, app.state.jobs = settings, store, jobs
    _errors(app)
    limit = int(settings.max_upload_mb) * 1024 * 1024

    @app.middleware("http")
    async def size_guard(request: Request, call_next):
        n = request.headers.get("content-length")
        if n and n.isdigit() and int(n) > limit:
            return JSONResponse({"detail": f"upload larger than {settings.max_upload_mb} MB"}, status_code=413)
        return await call_next(request)

    from .routes import ROUTERS
    for mod in ROUTERS:
        mod.init(app) if hasattr(mod, "init") else None
        app.include_router(mod.router)
    _spa(app)
    return app


def _errors(app: FastAPI) -> None:
    from fa_farm.sources.base import DataUnavailable

    def h(code: int):
        async def handler(request: Request, exc: Exception):
            body = {"detail": str(exc) or type(exc).__name__}
            if isinstance(exc, DataUnavailable):
                body["layer"] = getattr(exc, "layer", None)
            return JSONResponse(body, status_code=code)
        return handler

    from fa_runner.world import WaitingForData
    app.add_exception_handler(WaitingForData, h(409))
    app.add_exception_handler(TooLarge, h(413))
    app.add_exception_handler(ValueError, h(422))
    app.add_exception_handler(Busy, h(409))
    app.add_exception_handler(NotFound, h(404))
    app.add_exception_handler(KeyError, h(404))
    app.add_exception_handler(PermissionError, h(401))
    app.add_exception_handler(DataUnavailable, h(503))


def _spa(app: FastAPI) -> None:
    root = STATIC.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        if path:
            f = (root / path).resolve()
            if f.is_file() and root in f.parents:
                headers = {"Cache-Control": "no-cache"} if f.name in ("sw.js", "index.html") else None
                return FileResponse(f, headers=headers)
        index = root / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(PLACEHOLDER)
