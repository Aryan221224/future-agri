"""The agent's link to the Mission Console: outbound only, so it works through 4G NAT and
any Wi-Fi. Commands come by long poll; state goes up twice a second; telemetry and camera
counts go up as fa.live/1 batches with store-and-forward (nothing is lost while the link
is down, and a batch sent twice counts once).

    GET  /api/agent/poll?wait=20        -> {"commands": [{"id", "kind", ...}]}
    POST /api/agent/state               <- the pilot's state, fix, battery, bridge
    POST /api/agent/ack                 <- {"id", "ok", "error"?}
    POST /api/live-run/ingest           <- fa.live/1 batches (existing console endpoint)
All with the device token in X-Device-Token.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path


class ConsoleLink:
    def __init__(self, url: str, token: str, spool: str | Path | None = None, timeout_s: float = 10.0,
                 session=None):
        import requests
        self.url = url.rstrip("/")
        self.h = {"X-Device-Token": token}
        self.http = session or requests.Session()
        self.timeout = timeout_s
        self.spool = Path(spool) if spool else None
        self.queue: deque = deque(self._load())
        self.online = False
        self.last_ok = 0.0
        self.error: str | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- commands
    def poll(self, wait_s: int = 20) -> list[dict]:
        try:
            r = self.http.get(f"{self.url}/api/agent/poll", params={"wait": wait_s}, headers=self.h,
                              timeout=wait_s + self.timeout)
            r.raise_for_status()
            self._ok()
            return list(r.json().get("commands", []))
        except Exception as e:  # noqa: BLE001
            self._fail(e)
            return []

    def ack(self, cid: str, ok: bool, error: str | None = None) -> None:
        self._post("/api/agent/ack", {"id": cid, "ok": ok, "error": error})

    def state(self, body: dict) -> bool:
        return self._post("/api/agent/state", body)

    # ---------------------------------------------------------------- telemetry (store and forward)
    def enqueue(self, batch: dict) -> None:
        with self._lock:
            self.queue.append(batch)
            while len(self.queue) > 5000:               # ~1.5 h at 1 batch/s: keep the newest
                self.queue.popleft()
            self._save()

    def flush(self, max_n: int = 50) -> int:
        sent = 0
        while self.queue and sent < max_n:
            b = self.queue[0]
            if not self._post("/api/live-run/ingest", b, accept=(200, 404, 409, 422)):
                break
            with self._lock:
                self.queue.popleft()
                self._save()
            sent += 1
        return sent

    # ---------------------------------------------------------------- plumbing
    def _post(self, path: str, body: dict, accept=(200,)) -> bool:
        try:
            r = self.http.post(self.url + path, json=body, headers=self.h, timeout=self.timeout)
            if r.status_code not in accept:
                r.raise_for_status()
            self._ok()
            return True
        except Exception as e:  # noqa: BLE001
            self._fail(e)
            return False

    def _ok(self) -> None:
        self.online, self.last_ok, self.error = True, time.monotonic(), None

    def _fail(self, e: Exception) -> None:
        self.online, self.error = False, f"{type(e).__name__}: {e}"

    def _load(self) -> list:
        if self.spool and self.spool.exists():
            try:
                return [json.loads(x) for x in self.spool.read_text().splitlines() if x.strip()]
            except (OSError, ValueError):
                return []
        return []

    def _save(self) -> None:
        if self.spool:
            tmp = self.spool.with_suffix(".tmp")
            tmp.write_text("\n".join(json.dumps(b) for b in self.queue))
            tmp.replace(self.spool)


def live_batch(run_id: str, seq: int, fix, local, sortie: int, spraying: bool, power=None,
               cells: list | None = None) -> dict:
    """One fa.live/1 batch with the aircraft's position (and the camera's per-cell counts)."""
    from datetime import datetime, timezone
    t = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tel = []
    if fix is not None:
        lon, lat = local.to_lonlat(fix.e, fix.n)
        p = {"t": t, "lat": lat, "lon": lon, "alt_agl_m": round(fix.h, 2), "heading_deg": round(fix.heading, 1),
             "speed_ms": round((fix.v_e ** 2 + fix.v_n ** 2) ** 0.5, 2), "spraying": bool(spraying),
             "sortie": max(1, sortie + 1)}
        if power is not None:
            p["soc"] = None
        tel.append(p)
    return {"schema": "fa.live/1", "run_id": run_id, "seq": seq, "sent_at": t, "telemetry": tel,
            "cells": list(cells or [])}
