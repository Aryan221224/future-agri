"""Closed-loop benchmark: does a spray job that corrects itself do better than the plan?

For each template farm x seed: run the mission (the twin's own policy, every proposal
approved) until the first herbicide job is proposed, move to the first day it can be
flown, then fork the season three ways from the same state:

* open          -- the job as planned (``fly_now``);
* closed        -- sortie by sortie with the sprayer camera and re-planning;
* closed_valve  -- the same plus an in-pass spot valve (can only reduce product).

Each arm records the litres, sorties and flight time of that job, the true weed density
7 days later, and (unless ``finish=False``) the season's yield and net per ha. Arms are
compared pairwise against open on the same farm and seed (mean difference and a 95 %
interval over runs). Forecasts are climatology, so runs are reproducible offline.

    python ml/closed_loop_bench.py --templates all --seeds 0-9 --workers 8 --out ml/results/closed_loop
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/fa_core", "services/twin", "services/planner", "services/loop", "services/farm",
           "services/scan", "services/drone", "services/runner"):
    if str(ROOT / _p) not in sys.path:
        sys.path.insert(0, str(ROOT / _p))

ARMS = ("open", "closed", "closed_valve")
SEASON_YEAR = {"A1": 2025, "A2": 2025, "A3": 2024, "A4": 2025, "A5": 2025, "A6": 2025, "A8": 2025}
METRICS = ("litres", "sorties", "time_min", "weeds_after_m2", "yield_t_ha", "net_ha")


def build_template(template_id: str):
    from fa_farm.build import build_virtual_farm
    from fa_farm.sources.base import Cache
    return build_virtual_farm(template_id, SEASON_YEAR.get(template_id, 2025), Cache(),
                              cell_m=5.0 if template_id == "A6" else 10.0)   # as bench/archetype_runs.py


def _kwargs(vf, seed: int) -> dict:
    from fa_runner.scenarios import ForecastFeed, ForecastSource
    lat, lon = vf.spec.centroid
    return dict(vf=vf, policy="twin", seed=seed, feed=ForecastFeed(lat, lon, vf.history,
                sources=[ForecastSource.CLIMATOLOGY]), plan_members=6, approvals="manual")


def _to_job(vf, seed: int):
    """(session on the first flyable day of the first herbicide job, that job) or (None, why)."""
    from fa_runner.scenarios import flight_record
    from fa_runner.session import SeasonSession
    s = SeasonSession(**_kwargs(vf, seed))
    op = None
    while not s.done:
        s.run_until(stop_on_decision=True)
        props = [o for o in s.ops.values() if o.status == "proposed"]
        op = next((o for o in props if o.product == "herbicide"), None)
        if op:
            break
        for o in props:
            s.approve(o.id)
    if op is None:
        return None, "no herbicide job proposed this season"
    for _ in range(60):
        rec = s.world.weather(s.day)
        if rec is not None and s.window.ok(flight_record(rec)) and op.status == "proposed":
            return (s, op), ""
        if op.status != "proposed":
            break
        s.step()
    return None, "the herbicide job found no flyable day"


def run_one(args) -> list[dict]:
    """All arms of one farm and seed; a run that cannot be simulated is a skipped row, never
    the end of the benchmark."""
    try:
        return _run_one(args)
    except Exception as e:  # noqa: BLE001 -- recorded and reported, the other runs go on
        return [{"template": args[0], "seed": args[1], "arm": None, "skipped": f"{type(e).__name__}: {e}"[:300]}]


def _run_one(args) -> list[dict]:
    template, seed, finish, factory = args
    import copy

    from fa_runner.session import SeasonSession
    t0 = time.time()
    vf0 = (factory or build_template)(template)
    found, why = _to_job(copy.deepcopy(vf0), seed)
    if found is None:
        return [{"template": template, "seed": seed, "arm": None, "skipped": why}]
    base, op = found
    rows = []
    for arm in ARMS:
        s = SeasonSession.restore(_kwargs(copy.deepcopy(vf0), seed), base.actions)
        litres0, time0, sorties0 = s.st["litres"], s.st["time"], s.st["sorties"]
        out = s.fly_now(op.id) if arm == "open" else s.fly_closed_loop(op.id, valve=arm == "closed_valve")
        if not out["flown"]:
            rows.append({"template": template, "seed": seed, "arm": arm, "skipped": out["reason"]})
            continue
        day = s.day
        row = {"template": template, "seed": seed, "arm": arm, "op_id": op.id, "day": day.isoformat(),
               "litres": round(s.st["litres"] - litres0, 3), "sorties": s.st["sorties"] - sorties0,
               "time_min": round(s.st["time"] - time0, 2)}
        s.run_until(day + dt.timedelta(days=7), approve_all=True)
        row["weeds_after_m2"] = round(float(s.world.truth_state().weeds_m2.mean()), 3)
        if finish:
            s.run_until(approve_all=True)
            r = s.finish()
            row.update(yield_t_ha=round(r.yield_marketed_kg_ha / 1000, 3), net_ha=r.net_ha, currency=r.currency)
        row["seconds"] = round(time.time() - t0, 1)
        rows.append(row)
    return rows


def _paired(rows: list[dict], arm: str, metric: str) -> tuple[float | None, list | None, int]:
    by = {(r["template"], r["seed"], r["arm"]): r for r in rows if r.get("arm")}
    d = [by[(t, s, arm)][metric] - by[(t, s, "open")][metric] for (t, s, a) in by
         if a == arm and (t, s, "open") in by and metric in by[(t, s, arm)] and metric in by[(t, s, "open")]]
    if not d:
        return None, None, 0
    m = sum(d) / len(d)
    if len(d) < 2:
        return m, None, 1
    sd = math.sqrt(sum((x - m) ** 2 for x in d) / (len(d) - 1))
    h = 1.96 * sd / math.sqrt(len(d))
    return m, [m - h, m + h], len(d)


def summarise(rows: list[dict]) -> dict:
    out = {}
    for arm in ARMS:
        rs = [r for r in rows if r.get("arm") == arm]
        a = {"n": len(rs)}
        for k in METRICS:
            v = [r[k] for r in rs if k in r]
            if v:
                m = sum(v) / len(v)
                a[f"{k}_mean"] = round(m, 3)
                a[f"{k}_sd"] = round(math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1)), 3) if len(v) > 1 else None
            if arm != "open":
                dm, ci, _ = _paired(rows, arm, k)
                key = k.replace("_m2", "").replace("_t_ha", "").replace("_ha", "")
                a[f"{key}_diff_mean"] = None if dm is None else round(dm, 3)
                a[f"{key}_diff_ci95"] = None if ci is None else [round(x, 3) for x in ci]
        out[arm] = a
    return out


def _md(summary: dict, rows: list[dict]) -> str:
    def f(v):
        return "–" if v is None else (f"{v:.3g}" if isinstance(v, float) else str(v))

    def ci(v):
        return "–" if not v else f"[{v[0]:.3g}, {v[1]:.3g}]"
    lines = ["# Closed-loop benchmark", "",
             f"{len({(r['template'], r['seed']) for r in rows})} farm-seeds; "
             f"{sum(1 for r in rows if r.get('skipped'))} skipped rows.", "",
             "| arm | n | litres | weeds 7 d after (m⁻²) | yield t/ha | net/ha | Δ litres vs open (95 % CI) | "
             "Δ weeds vs open | Δ net vs open |", "|---|---|---|---|---|---|---|---|---|"]
    for arm, a in summary.items():
        lines.append(f"| {arm} | {a['n']} | {f(a.get('litres_mean'))} | {f(a.get('weeds_after_m2_mean'))} | "
                     f"{f(a.get('yield_t_ha_mean'))} | {f(a.get('net_ha_mean'))} | "
                     f"{f(a.get('litres_diff_mean'))} {ci(a.get('litres_diff_ci95'))} | "
                     f"{f(a.get('weeds_after_diff_mean'))} {ci(a.get('weeds_after_diff_ci95'))} | "
                     f"{f(a.get('net_diff_mean'))} {ci(a.get('net_diff_ci95'))} |")
    skipped = [r for r in rows if r.get("skipped")]
    if skipped:
        lines += ["", "Skipped:", *[f"- {r['template']} seed {r['seed']} {r.get('arm') or ''}: {r['skipped']}"
                                    for r in skipped]]
    return "\n".join(lines) + "\n"


def run_bench(templates: list[str], seeds: list[int], out: Path, workers: int = 1, farm_factory=None,
              finish: bool = True) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(t, s, finish, farm_factory) for t in templates for s in seeds]
    rows: list[dict] = []
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(workers) as ex:
            for r in ex.map(run_one, jobs):
                rows += r
                print(json.dumps(r), flush=True)
    else:
        for j in jobs:
            r = run_one(j)
            rows += r
            print(json.dumps(r), flush=True)
    summary = summarise(rows)
    res = {"created": dt.datetime.now().isoformat(timespec="seconds"), "templates": templates, "seeds": seeds,
           "finish": finish, "summary": summary, "rows": rows}
    (out / "closed_loop.json").write_text(json.dumps(res, indent=1))
    (out / "closed_loop.md").write_text(_md(summary, rows))
    return res


def _seeds(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in s.split(",")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--templates", default="all", help="'all' or comma list, e.g. A1,A5")
    ap.add_argument("--seeds", default="0-4")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "ml" / "results" / "closed_loop"))
    ap.add_argument("--no-finish", action="store_true", help="skip finishing the season (no yield/net)")
    a = ap.parse_args()
    if a.templates == "all":
        from fa_farm.archetypes import load_archetypes
        templates = [k for k, v in load_archetypes().items() if v.weeds]
    else:
        templates = a.templates.split(",")
    res = run_bench(templates, _seeds(a.seeds), Path(a.out), a.workers, finish=not a.no_finish)
    print((Path(a.out) / "closed_loop.md").read_text())
    return 0 if res["rows"] else 1


if __name__ == "__main__":
    sys.exit(main())
