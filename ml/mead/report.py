"""Score every model version against the Mead measurements on the same held-out seasons.

    python ml/mead/report.py          # -> ml/results/mead/metrics.json and figures/*.png

Test set: every maize site-year except the three training seasons (Ne1 2001-2003), for every
version -- also those that were never calibrated -- so the numbers are comparable. Yield is
at 15.5 % moisture. Baseline: the mean measured yield of the training seasons, i.e. what a
calibrated-on-irrigated guess would say for any field.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import study as S  # noqa: E402

VERSIONS = {
    "v1": ("runs.json", "Generic WOFOST maize + gridded weather + SoilGrids topsoil"),
    "v2": ("runs_v2.json", "+ root-zone soil, deep rooting, hybrid thermal time, soil water at sowing"),
    "v3": ("runs_v3.json", "+ regional hybrid calibrated on 3 irrigated seasons"),
    "v4": ("runs_v4.json", "+ measured sowing date and stand, residue, rain gauge, soil survey, root front"),
}
MODES = ("forward", "midseason", "anchored")
MODE_LABEL = {"forward": "at sowing (no satellite)", "midseason": "15 July (satellite so far)",
              "anchored": "end of season (all satellite)"}


def _stats(pairs):
    if not pairs:
        return None
    e = [p - m for p, m in pairs]
    return {"n": len(e), "rmse": math.sqrt(sum(x * x for x in e) / len(e)), "bias": sum(e) / len(e),
            "mae": sum(abs(x) for x in e) / len(e),
            "mape": (100 * sum(abs(p - m) / m for p, m in pairs if m > 0) / max(1, sum(1 for _, m in pairs if m > 0)))}


def score() -> dict:
    gt = S.ground_truth()
    train = {tuple(t) for t in S.TRAIN}
    base = sum(gt[s][y]["yield_t_ha"] for s, y in train) / len(train)
    out = {"test_set": [], "baseline_t_ha": base, "versions": {}}
    keys = [(s, y) for s in S.SITES for y in S.MAIZE[s] if (s, y) not in train]
    out["test_set"] = [f"{s}_{y}" for s, y in keys]
    groups = {"all": lambda s: True, "irrigated": lambda s: S.SITES[s][2], "rainfed": lambda s: not S.SITES[s][2]}
    b = {}
    for g, f in groups.items():
        b[g] = _stats([(base, gt[s][y]["yield_t_ha"]) for s, y in keys if f(s) and "yield_t_ha" in gt[s][y]])
    out["baseline"] = b
    for v, (fn, label) in VERSIONS.items():
        p = S.OUT / fn
        if not p.exists():
            continue
        runs = json.loads(p.read_text())
        vo = {"label": label}
        for mode in MODES:
            mo = {}
            for g, f in groups.items():
                yp, ap, lp = [], [], []
                for s, y in keys:
                    r = runs.get(f"{s}_{y}", {}).get(mode)
                    if not r or not f(s):
                        continue
                    t = gt[s][y]
                    if "yield_t_ha" in t:
                        yp.append((r["yield_t_ha"], t["yield_t_ha"]))
                    if "agb_t_ha" in t:
                        ap.append((r["agb_t_ha"], t["agb_t_ha"]))
                    for d, val in t["lai"]:
                        sim = r["lai"].get(d.isoformat())
                        if sim is not None:
                            lp.append((sim, val))
                mo[g] = {"yield": _stats(yp), "agb": _stats(ap), "lai": _stats(lp)}
            vo[mode] = mo
        vo["per_site_year"] = {f"{s}_{y}": {m: round(runs[f"{s}_{y}"][m]["yield_t_ha"], 2) for m in MODES
                                            if f"{s}_{y}" in runs}
                               for s, y in keys if f"{s}_{y}" in runs}
        out["versions"][v] = vo
    out["measured"] = {f"{s}_{y}": gt[s][y].get("yield_t_ha") for s in S.SITES for y in S.MAIZE[s]}
    (S.OUT / "metrics.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def figures(m: dict) -> list[Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig_dir = S.OUT / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    gt = S.ground_truth()
    paths = []
    vs = [v for v in VERSIONS if v in m["versions"]]

    # 1 RMSE by version, irrigated vs rainfed, at sowing
    fig, ax = plt.subplots(figsize=(7, 4))
    w = 0.38
    for i, g in enumerate(("irrigated", "rainfed")):
        vals = [m["versions"][v]["forward"][g]["yield"]["rmse"] for v in vs]
        ax.bar([k + (i - 0.5) * w for k in range(len(vs))], vals, w, label=g)
    for i, (g, c) in enumerate((("irrigated", "tab:blue"), ("rainfed", "tab:orange"))):
        ax.axhline(m["baseline"][g]["rmse"], ls=":", c=c, lw=1.2,
                   label=f"naive 'use the average', {g} ({m['baseline'][g]['rmse']:.2f})")
    ax.set_xticks(range(len(vs)), ["v1 generic", "v2 + physics", "v3 + hybrid", "v4 + real field"][:len(vs)])
    ax.set_ylabel("yield RMSE, t/ha (held-out seasons)")
    ax.set_title("Forecast at sowing: error by model version")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    paths.append(fig_dir / "rmse_by_version.png")
    fig.savefig(paths[-1], dpi=160)
    plt.close(fig)

    # 2 predicted vs measured, v1 vs v4
    fig, ax = plt.subplots(figsize=(5, 5))
    for v, mk in (("v1", "o"), (vs[-1], "s")):
        xs, ys, cs = [], [], []
        for k, yl in m["versions"][v]["per_site_year"].items():
            meas = m["measured"].get(k)
            if meas is None:
                continue
            xs.append(meas)
            ys.append(yl["forward"])
            cs.append("tab:blue" if S.SITES[k.split("_")[0]][2] else "tab:orange")
        ax.scatter(xs, ys, marker=mk, c=cs, alpha=0.85 if v != "v1" else 0.35)
        ax.scatter([], [], marker=mk, c="grey", alpha=0.85 if v != "v1" else 0.35,
                   label="v1 generic crop model" if v == "v1" else f"{v} the updated twin")
    ax.plot([0, 16], [0, 16], c="grey", lw=0.8)
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 16)
    ax.set_xlabel("measured yield, t/ha")
    ax.set_ylabel("forecast at sowing, t/ha")
    ax.set_title("Held-out seasons (blue irrigated, orange rainfed)")
    ax.legend(frameon=False)
    fig.tight_layout()
    paths.append(fig_dir / "pred_vs_meas.png")
    fig.savefig(paths[-1], dpi=160)
    plt.close(fig)

    # 3 Ne3 LAI through the season, measured vs v1 vs latest
    for y in (2003, 2005):
        fig, ax = plt.subplots(figsize=(7, 3.6))
        for v, st in (("v1", ":"), ("v3", "--"), (vs[-1], "-")):
            runs = json.loads((S.OUT / VERSIONS[v][0]).read_text())
            lai = runs[f"Ne3_{y}"]["forward"]["lai"]
            ds = sorted(lai)
            ax.plot([dt.date.fromisoformat(d[:10]) for d in ds], [lai[d] for d in ds], st, label=f"{v} forecast")
        md = gt["Ne3"][y]["lai"]
        ax.plot([d for d, _ in md], [v for _, v in md], "ko", label="measured (AmeriFlux)")
        ax.set_ylabel("leaf area index")
        ax.set_title(f"Rainfed Ne3 {y}: canopy the model expects vs the field")
        ax.legend(frameon=False, fontsize=8)
        fig.autofmt_xdate()
        fig.tight_layout()
        paths.append(fig_dir / f"ne3_{y}_lai.png")
        fig.savefig(paths[-1], dpi=160)
        plt.close(fig)
    return paths


if __name__ == "__main__":
    m = score()
    for v, vo in m["versions"].items():
        for mode in MODES:
            row = []
            for g in ("irrigated", "rainfed", "all"):
                s = vo[mode][g]["yield"]
                row.append(f"{g} RMSE {s['rmse']:.2f} bias {s['bias']:+.2f}" if s else f"{g} -")
            print(v, f"{mode:9s}", " | ".join(row))
    print("baseline", {g: round(s["rmse"], 2) for g, s in m["baseline"].items()})
    for p in figures(m):
        print(p)
