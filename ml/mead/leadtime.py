"""How early is the forecast good? Yield forecast on each held-out Mead season made on a series
of dates: the real weather up to that day, then every past year's weather (1984-2000) for the
rest of the season, averaged. Scored against the measured yield, with the same v4 model and
hybrid as everything else (calibrated on Ne1 2001-2003 only).

    python ml/mead/leadtime.py      # -> ml/results/mead/leadtime.json

The measured yield is known after the combine (mid-October); USDA's county estimate the next
February. Each forecast date is reported with its lead time before harvest and the compute
time it took.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import study as S  # noqa: E402
from ablation import CASES  # noqa: E402  (the 8 held-out site-years with a measured yield)

DATES = ("sowing", "06-01", "07-01", "07-15", "08-01", "08-15", "09-01", "harvest")
CLIM = range(1984, 2001)
HARVEST = (10, 15)


def _splice(wx, cut: dt.date, k: int, y: int):
    """Real weather to ``cut``, then year ``k``'s weather on the same calendar days."""
    byday = {r["DAY"]: r for r in wx}
    out = []
    for r in wx:
        d = r["DAY"]
        if not (dt.date(y - 1, 11, 1) <= d <= dt.date(y, 12, 31)):
            continue
        if d <= cut:
            out.append(r)
            continue
        try:
            src = byday.get(d.replace(year=k))
        except ValueError:          # 29 Feb
            src = None
        out.append({**(src or r), "DAY": d})
    return out


def forecast(site, y, theta, sci, cut):
    ys = []
    for k in CLIM:
        s2 = {**sci, "weather": _splice(sci["weather"], cut, k, y)}
        ys.append(S._sim_v4(site, y, theta, s2)["yield_t_ha"])
    return sum(ys) / len(ys), (max(ys) - min(ys))


def main() -> int:
    gt = S.ground_truth()
    cal = json.loads((S.OUT / "hybrid_calibration_v4.json").read_text())["params"]
    theta = [cal[k] for k in S.CAL_PARAMS_V4]
    rows = []
    for site, y in CASES:
        sci = S.science_inputs_v4(site, y)
        meas = gt[site][y]["yield_t_ha"]
        harv = dt.date(y, *HARVEST)
        for tag in DATES:
            cut = (sci["management"]["sowing"] if tag == "sowing" else harv if tag == "harvest"
                   else dt.date.fromisoformat(f"{y}-{tag}"))
            t0 = time.perf_counter()
            if tag == "harvest":
                f, spread = S._sim_v4(site, y, theta, sci)["yield_t_ha"], 0.0
            else:
                f, spread = forecast(site, y, theta, sci, cut)
            secs = time.perf_counter() - t0
            rows.append({"site": site, "year": y, "date": tag, "cut": cut.isoformat(), "lead_days": (harv - cut).days,
                         "forecast_t_ha": round(f, 2), "climate_spread_t_ha": round(spread, 2),
                         "measured_t_ha": round(meas, 2), "seconds": round(secs, 2)})
            print(site, y, tag, f"{f:.2f} (spread {spread:.2f}) vs {meas:.2f}  {secs:.1f} s", flush=True)
    summ = {}
    for tag in DATES:
        rs = [r for r in rows if r["date"] == tag]
        for g, keep in (("all", lambda r: True), ("irrigated", lambda r: r["site"] != "Ne3"),
                        ("rainfed", lambda r: r["site"] == "Ne3")):
            e = [r["forecast_t_ha"] - r["measured_t_ha"] for r in rs if keep(r)]
            summ.setdefault(tag, {})[g] = {"rmse": math.sqrt(sum(x * x for x in e) / len(e)), "bias": sum(e) / len(e)}
        summ[tag]["lead_days_mean"] = sum(r["lead_days"] for r in rs) / len(rs)
        summ[tag]["spread_mean"] = sum(r["climate_spread_t_ha"] for r in rs) / len(rs)
        summ[tag]["seconds_mean"] = sum(r["seconds"] for r in rs) / len(rs)
    (S.OUT / "leadtime.json").write_text(json.dumps({"rows": rows, "summary": summ}, indent=1))
    for tag, v in summ.items():
        print(f"{tag:8s} lead {v['lead_days_mean']:5.0f} d | all {v['all']['rmse']:.2f} | irr {v['irrigated']['rmse']:.2f} "
              f"| rainfed {v['rainfed']['rmse']:.2f} | climate spread {v['spread_mean']:.2f} | {v['seconds_mean']:.1f} s")
    return 0


if __name__ == "__main__" and "--plot" not in sys.argv:
    main()


def plot() -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    m = json.loads((S.OUT / "leadtime.json").read_text())["summary"]
    tags = list(m)
    lead = [m[t]["lead_days_mean"] for t in tags]
    fig, ax = plt.subplots(figsize=(7, 3.8))
    for g in ("irrigated", "rainfed", "all"):
        ax.plot(lead, [m[t][g]["rmse"] for t in tags], "o-", label=f"{g} RMSE")
    ax.plot(lead, [m[t]["spread_mean"] for t in tags], "k:", label="range across past weather years")
    ax.invert_xaxis()
    ax.set_xlabel("days before harvest (15 Oct)")
    ax.set_ylabel("t/ha")
    ax.set_title("Mead held-out seasons: forecast error vs lead time")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    p = S.OUT / "figures" / "leadtime.png"
    fig.savefig(p, dpi=160)
    plt.close(fig)
    return p


if __name__ == "__main__" and "--plot" in sys.argv:
    print(plot())
