"""``python -m fa_console`` -- serve the console (127.0.0.1:8099 by default)."""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser


def main(argv=None) -> int:
    from . import _paths  # noqa: F401
    from .config import Settings
    ap = argparse.ArgumentParser(prog="fa_console", description="Future Agri Mission Console")
    ap.add_argument("--host", default=None, help="bind address (default FA_BIND or 127.0.0.1)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--open", action="store_true", help="open the browser")
    a = ap.parse_args(argv)
    s = Settings()
    if a.host:
        s.bind = a.host
    if a.port:
        s.port = a.port
    import uvicorn
    from .app import create_app
    if s.lan_exposed:
        print(f"WARNING: no login -- anyone who can reach {s.bind}:{s.port} can operate this console.",
              file=sys.stderr)
    url = f"http://{'127.0.0.1' if s.bind in ('0.0.0.0', '::') else s.bind}:{s.port}/"
    print(f"Future Agri Mission Console on {url}  (data: {s.home})")
    if a.open:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(s), host=s.bind, port=s.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
