"""WOFOST 8.1 nitrogen response at Bennett: can the twin explain the yield gap?

Build-plan step 3 asked for 8.1 nitrogen runs to model the gap between the
water-limited ceiling (~45-50 t/ha fresh potato) and farm yields (~25 t/ha).
This measures yield against N rate for potato and Indian-calibrated wheat over
several weather seasons and runs ``check_nitrogen_response`` on each.

    python bench/nitrogen_response.py --seasons 6
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import warnings
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/twin"):
    sys.path.insert(0, str(ROOT / p))
warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

from fa_twin.engine import ZoneConfig, ZoneTwin, check_nitrogen_response  # noqa: E402
from fa_twin.weather import generate_records, records_provider  # noqa: E402

LAT, LON = 28.45, 77.58
CROPS = {
    "potato": dict(variety="Potato_701", sow=dt.date(2025, 10, 25), harvest=dt.date(2026, 2, 10),
                   wstart=dt.date(2025, 10, 1), irr_every=10, irr_cm=4.0, n_irr=10, dm=0.20),
    "wheat": dict(variety="IN_NW_timely", sow=dt.date(2025, 11, 15), harvest=dt.date(2026, 4, 30),
                  wstart=dt.date(2025, 11, 1), irr_every=20, irr_cm=6.0, n_irr=6, dm=1.0),
}
RATES = (0, 60, 120, 180, 240)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, default=6)
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / "nitrogen_response.json"))
    a = ap.parse_args()
    res = {"note": "WOFOST 8.1 NWLP, classic N balance, NAVAILI=30 + NSOILBASE=10 kg/ha; "
                   "N split at sowing and +30 d; irrigated; normals-calibrated weather"}
    for crop, c in CROPS.items():
        table = {r: [] for r in RATES}
        gates = []
        for s in range(a.seasons):
            w = records_provider(generate_records(c["wstart"], 220, 700 + s), LAT, LON)
            cfg = ZoneConfig(zone_id="n", latitude=LAT, longitude=LON, crop=crop, variety=c["variety"],
                             sowing_date=c["sow"], harvest_date=c["harvest"], NAVAILI=30.0)
            irr = [{"date": c["sow"] + dt.timedelta(days=c["irr_every"] * (i + 1)),
                    "irrigation_cm": c["irr_cm"]} for i in range(c["n_irr"])]
            for r in RATES:
                ev = list(irr)
                if r:
                    ev += [{"date": c["sow"], "N_amount": r / 2},
                           {"date": c["sow"] + dt.timedelta(days=30), "N_amount": r / 2}]
                tw = ZoneTwin(cfg, w, events=ev, engine="Wofost81_NWLP_CWB_CNB")
                tw.run_to_end()
                table[r].append(float(tw.summary().get("TWSO") or 0.0) / c["dm"] / 1000)
            if s == 0:
                g = check_nitrogen_response(cfg, w, irr)
                gates.append({"ok": g.ok, "reasons": g.reasons})
        res[crop] = {"yield_t_ha_by_N": {str(r): round(float(np.mean(v)), 2) for r, v in table.items()},
                     "unit": "fresh t/ha" if c["dm"] < 1 else "grain t/ha (dry)",
                     "gain_0_to_240_pct": round(100 * (np.mean(table[240]) / np.mean(table[0]) - 1), 1),
                     "nitrogen_gate": gates[0]}
        print(crop, json.dumps(res[crop], indent=1))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(res, open(a.out, "w"), indent=1)
    print("->", a.out)


if __name__ == "__main__":
    main()
