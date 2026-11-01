"""Device emulator for testing the live feeds without hardware.

    python -m fa_console.emulate --server http://127.0.0.1:8099 --token <device token> \\
        --kind soil_moisture|tracker --lat 52.52 --lon 5.62 --count 60 --interval 2

Readings are labelled as emulated in their payload. A tracker flies a serpentine
over a 100 m square at the given point."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import sys
import time
import urllib.request


def post(server: str, token: str, readings: list[dict]) -> dict:
    req = urllib.request.Request(server.rstrip("/") + "/api/ingest",
                                 data=json.dumps({"token": token, "readings": readings}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def readings(kind: str, i: int, lat: float, lon: float) -> list[dict]:
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    if kind == "tracker":
        lane, along = divmod(i, 20)
        x = (along if lane % 2 == 0 else 19 - along) * 5.0
        y = lane * 5.0
        dlat, dlon = y / 111_320, x / (111_320 * math.cos(math.radians(lat)))
        return [{"kind": "position", "ts": ts, "payload": {"lat": lat + dlat, "lon": lon + dlon, "alt_m": 2.5,
                                                           "heading_deg": 90 if lane % 2 == 0 else 270,
                                                           "speed_ms": 5.0, "spraying": True, "emulated": True}}]
    base = 28 - 0.05 * i + random.gauss(0, 0.4)
    return [{"kind": "soil_moisture", "ts": ts, "value": round(max(5.0, base), 2), "payload": {"emulated": True}},
            {"kind": "soil_temp", "ts": ts, "value": round(16 + 3 * math.sin(i / 10), 2), "payload": {"emulated": True}}]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fa_console.emulate")
    ap.add_argument("--server", default="http://127.0.0.1:8099")
    ap.add_argument("--token", required=True)
    ap.add_argument("--kind", choices=("soil_moisture", "tracker"), default="soil_moisture")
    ap.add_argument("--lat", type=float, default=0.0)
    ap.add_argument("--lon", type=float, default=0.0)
    ap.add_argument("--count", type=int, default=60)
    ap.add_argument("--interval", type=float, default=2.0)
    a = ap.parse_args(argv)
    for i in range(a.count):
        out = post(a.server, a.token, readings(a.kind, i, a.lat, a.lon))
        print(json.dumps(out))
        if i < a.count - 1:
            time.sleep(a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
