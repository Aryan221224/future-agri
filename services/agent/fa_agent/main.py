"""fa-agent on the drone's companion computer.

    python -m fa_agent run --config /etc/fa-agent.yaml      # fly: hardware + console
    python -m fa_agent sim [--job job.json] [--wind 3]        # fly a simulated K3-A Pro
    python -m fa_agent bench --config ...                     # props-off bench test (guided)
    python -m fa_agent check --config ...                     # read every sensor once and report

The 20 Hz loop: read RTK fix, battery and bridge status -> pilot.tick -> send the channels to
the bridge (200 ms time-to-live) and set the pump relay. If anything in the loop raises, the
agent stops sending frames: the bridge holds the aircraft and then the K3-A Pro returns home.
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("fa_agent")


@dataclass
class AgentConfig:
    console_url: str = "http://127.0.0.1:8099"
    token: str = ""
    bridge_port: str = "/dev/serial0"
    gnss_front_port: str = "/dev/ttyACM0"     # moving base: RTK position
    gnss_rear_port: str = "/dev/ttyACM1"      # rover: heading
    gnss_baud: int = 115200
    heading_offset_deg: float = 0.0           # antenna baseline vs the nose (front->rear along the body: 180)
    baseline_m: float | None = None
    home: dict | None = None                  # {"lat", "lon"}; else the job's home
    ntrip: dict | None = None                 # {"host", "port", "mount", "user", "password"}
    power: dict | None = field(default_factory=lambda: {"bus": 1, "addr": 0x40, "shunt_ohm": 0.0005})
    relay: dict | None = field(default_factory=lambda: {"chip": "gpiochip0", "line": 17})
    profile: str | None = None                # calibrated profile file; default data/k3a_pro.yaml
    spool: str = "/var/lib/fa-agent/spool.jsonl"

    @classmethod
    def load(cls, path: str) -> "AgentConfig":
        d = yaml.safe_load(Path(path).read_text()) or {}
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown settings in {path}: {', '.join(sorted(unknown))}")
        return cls(**d)


class SerialUbx(threading.Thread):
    def __init__(self, port: str, baud: int, on_msg):
        super().__init__(daemon=True, name=f"ubx {port}")
        import serial
        from .gnss import UbxParser
        self.ser = serial.Serial(port, baud, timeout=0.1)
        self.parser, self.on_msg = UbxParser(), on_msg
        self.write_lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self.write_lock:
            self.ser.write(data)

    def run(self) -> None:
        while True:
            data = self.ser.read(2048)
            if data:
                t = time.monotonic()
                for c, m, p in self.parser.feed(data):
                    self.on_msg(c, m, p, t)


class Agent:
    def __init__(self, cfg: AgentConfig):
        from .bridgelink import BridgeConfig, SerialBridge
        from .gnss import FixBuilder, NtripClient
        from .pilot import Pilot
        from .profile import K3Profile
        from .uplink import ConsoleLink
        self.cfg = cfg
        self.profile = K3Profile.load(cfg.profile)
        self.pilot = Pilot(self.profile)
        self.bridge = SerialBridge(cfg.bridge_port)
        p = self.profile
        self.bridge.configure(BridgeConfig(auto_ch=p.auto_ch, mode_ch=p.mode_ch, mode_gps_us=p.mode_gps_us,
                                           rth_ch=p.rth_ch, rth_on_us=p.rth_on_us, rth_off_us=p.rth_off_us,
                                           throttle_ch=p.throttle.ch, pump_ch=p.pump_ch, pump_off_us=p.pump_off_us))
        home = cfg.home or {"lat": 0.0, "lon": 0.0}
        self.fixes = FixBuilder(home["lat"], home["lon"], heading_offset_deg=cfg.heading_offset_deg,
                                baseline_m=cfg.baseline_m)
        self._fix_lock = threading.Lock()
        self.front = SerialUbx(cfg.gnss_front_port, cfg.gnss_baud, self._on_ubx)
        self.rear = SerialUbx(cfg.gnss_rear_port, cfg.gnss_baud, self._on_ubx) if cfg.gnss_rear_port else None
        self.power = self._power()
        self.relay = self._relay()
        self.link = ConsoleLink(cfg.console_url, cfg.token, cfg.spool)
        self.ntrip = None
        if cfg.ntrip:
            n = cfg.ntrip
            self.ntrip = NtripClient(n["host"], int(n.get("port", 2101)), n["mount"], n.get("user", ""),
                                     n.get("password", ""), sink=self.front.write, position=self._llh)
        self.commands: queue.Queue = queue.Queue()
        self.run_id: str | None = None
        self.seq = 0
        self.failed: str | None = None

    def _power(self):
        if not self.cfg.power:
            return None
        try:
            from .hw import INA228
            return INA228(**self.cfg.power)
        except Exception as e:  # noqa: BLE001 -- reported by preflight ("no reading from the power monitor")
            log.warning("power monitor unavailable: %s", e)
            return None

    def _relay(self):
        if not self.cfg.relay:
            return None
        try:
            from .hw import PumpRelay
            return PumpRelay(**self.cfg.relay)
        except Exception as e:  # noqa: BLE001
            log.warning("pump relay unavailable: %s", e)
            return None

    def _on_ubx(self, c, m, p, t):
        with self._fix_lock:
            self.fixes.on_ubx(c, m, p, t)

    def _llh(self):
        pv = self.fixes.pvt
        return None if pv is None else (pv.lat, pv.lon, pv.h_ell)

    # ---------------------------------------------------------------- threads
    def _link_loop(self) -> None:
        while True:
            for cmd in self.link.poll(20):
                self.commands.put(cmd)
            self.link.flush()

    def _state_loop(self) -> None:
        while True:
            self.link.state(self.snapshot())
            time.sleep(0.5)

    def snapshot(self) -> dict:
        pl = self.pilot
        with self._fix_lock:
            f = self.fixes.fix(time.monotonic())
        st = self.bridge.status
        return {"state": pl.state, "job_id": pl.job.job_id if pl.job else None, "sortie": pl.sortie,
                "pass": pl.pass_i, "spraying": pl.spraying, "xtrack_m": pl.xtrack, "preflight": pl.preflight,
                "events": [(e.t, e.state, e.reason) for e in pl.events[-20:]], "agent_error": self.failed,
                "fix": None if f is None else {"kind": f.kind, "e": round(f.e, 2), "n": round(f.n, 2),
                                               "h": round(f.h, 2), "heading": round(f.heading, 1),
                                               "heading_ok": f.heading_ok, "sats": f.sats},
                "bridge": None if st is None else {"source": st.source, "pilot_ok": st.pilot_ok,
                                                   "auto_allowed": st.auto_allowed, "errors": st.link_errors},
                "ntrip": None if self.ntrip is None else {"bytes": self.ntrip.bytes_in, "error": self.ntrip.error},
                "calibration": getattr(pl, "calibration", None), "queued": len(self.link.queue)}

    def _handle(self, cmd: dict) -> None:
        from .route import Job
        k, pl = cmd.get("kind"), self.pilot
        try:
            if k == "start_job":
                job = Job.from_json(cmd["job"])
                self.fixes.local = job.local
                self.run_id = cmd.get("run_id")
                pl.start(job)
            elif k == "calibrate":
                job = Job.from_json(cmd["job"])
                self.fixes.local = job.local
                pl.start_calibration(job)
            elif k == "abort":
                pl.abort(cmd.get("reason", "operator abort"))
            elif k == "pause":
                pl.pause()
            elif k == "resume":
                pl.resume()
            elif k == "battery_swapped":
                pl.battery_swapped()
            else:
                raise ValueError(f"unknown command {k!r}")
            self.link.ack(cmd.get("id", ""), True)
        except Exception as e:  # noqa: BLE001 -- reported back to the operator
            self.link.ack(cmd.get("id", ""), False, str(e))

    def run(self) -> None:
        self.front.start()
        if self.rear:
            self.rear.start()
        if self.ntrip:
            self.ntrip.start()
        threading.Thread(target=self._link_loop, daemon=True, name="link").start()
        threading.Thread(target=self._state_loop, daemon=True, name="state").start()
        period, nxt, last_batch = 1 / self.pilot.cfg.tick_hz, time.monotonic(), 0.0
        while True:
            now = time.monotonic()
            try:
                while not self.commands.empty():
                    self._handle(self.commands.get_nowait())
                with self._fix_lock:
                    fix = self.fixes.fix(now)
                power = self.power.read() if self.power else None
                st = self.bridge.poll()
                if st is not None:
                    st.at = st.at or now
                us = None if self.failed else self.pilot.tick(now, fix, power, st)
                if us is not None:
                    self.bridge.send(us, 200)
                if self.relay:
                    self.relay.set(self.pilot.spraying and not self.failed)
                if self.run_id and now - last_batch >= 1.0:
                    from .uplink import live_batch
                    self.seq += 1
                    self.link.enqueue(live_batch(self.run_id, self.seq, fix, self.fixes.local, self.pilot.sortie,
                                                 self.pilot.spraying, power))
                    last_batch = now
            except Exception as e:  # noqa: BLE001 -- stop flying: the bridge holds, then returns home
                if not self.failed:
                    log.exception("agent loop failed")
                    self.failed = f"{type(e).__name__}: {e}"
                    if self.relay:
                        try:
                            self.relay.set(False)
                        except Exception:  # noqa: BLE001
                            pass
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))


def _sim(args) -> int:
    from .route import Job
    from .sim import SimK3
    from .simrun import demo_job, run
    job = Job.from_json(json.loads(Path(args.job).read_text())) if args.job else demo_job()
    r = run(job, max_s=args.max_s, k3=SimK3(wind_ms=args.wind, seed=args.seed), calibrate=args.calibrate)
    s = r.summary()
    print(json.dumps({k: v for k, v in s.items() if k != "events"}, indent=2))
    for e in s["events"]:
        print(f"{e[0]:8.2f}  {e[1]:<10} {e[2]}")
    return 0 if r.state in ("DONE",) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fa_agent", description="Future Agri on-board agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    s = sub.add_parser("sim")
    s.add_argument("--job")
    s.add_argument("--wind", type=float, default=3.0)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--max-s", type=float, default=1800.0)
    s.add_argument("--calibrate", action="store_true")
    b = sub.add_parser("bench")
    b.add_argument("--config", required=True)
    c = sub.add_parser("check")
    c.add_argument("--config", required=True)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.cmd == "sim":
        return _sim(a)
    from .bench import bench, check
    if a.cmd == "bench":
        return bench(AgentConfig.load(a.config))
    if a.cmd == "check":
        return check(AgentConfig.load(a.config))
    Agent(AgentConfig.load(a.config)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
