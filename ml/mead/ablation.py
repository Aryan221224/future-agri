"""Which inputs carry the v4 improvement? Remove one at a time (forecast at sowing, calibrated
hybrid fixed), plus a sensitivity check on how plant population is represented.

    python ml/mead/ablation.py   # -> ml/results/mead/ablation.json

Diagnostics on the held-out seasons: they explain the result, they were not used to choose
the model (v4 was fixed before these runs).
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import study as S  # noqa: E402

CASES = [("Ne1", 2004), ("Ne1", 2005), ("Ne2", 2001), ("Ne2", 2003), ("Ne2", 2005),
         ("Ne3", 2001), ("Ne3", 2003), ("Ne3", 2005)]


def _gridded(site):
    hist = json.loads((S.DATA / "history_1984_2000.json").read_text())
    for r in hist:
        r["DAY"] = dt.date.fromisoformat(r["DAY"])
    return hist + S._hist(site)["weather"]


def variant(site, y, name, theta):
    from fa_farm.sources.base import Cache
    from fa_farm.sources.soil import soilgrids_rootzone
    from fa_twin.management import population_overrides
    from fa_twin.soilwater import available_water_at_sowing
    sci = S.science_inputs_v4(site, y)
    sci = {**sci, "management": dict(sci["management"]), "overrides": dict(sci["overrides"])}
    lat, lon, _ = S.SITES[site]
    mg = sci["management"]
    redo_wav = False
    extra = {}
    if name == "no stand":
        mg["population"] = 80000
    elif name == "no gauge":
        sci["weather"] = _gridded(site)
        redo_wav = True
    elif name == "no residue":
        mg["soil_evap_factor"] = 1.0
        redo_wav = True
    elif name == "SoilGrids not SSURGO":
        _, sci["soil"], _ = soilgrids_rootzone(lat, lon, Cache(S.DATA / "cache"))
        redo_wav = True
    elif name == "shipped root front":
        sci["overrides"]["RRI"] = 2.2
    elif name == "1 May sowing":
        mg["sowing"] = dt.date(y, 5, 1)
        redo_wav = True
    elif name in ("stand: seed weight only", "stand: sqrt leaf share"):
        base = {"SPAN": theta[0], "RGRLAI": theta[1], "TDWI": theta[2], "TSUMEM": theta[3]}
        r = mg["population"] / 80000
        if name == "stand: seed weight only":
            extra = {"TDWI": theta[2] * r, "FLTB": sci["crop"]["FLTB"], "FSTB": sci["crop"]["FSTB"]}
        else:
            pop = population_overrides("maize", {**sci["crop"], **base}, 80000 * math.sqrt(r))
            extra = {**pop, "TDWI": theta[2] * r}
    if redo_wav:
        sp = sci["soil"]
        sci["WAV"] = available_water_at_sowing(sci["weather"], sp.SMFCF, sp.SMW, mg["sowing"], sp.RDMSOL, lat=lat,
                                               elev=S._hist(site)["elevation_m"],
                                               evap_factor=mg["soil_evap_factor"])["WAV_cm"]
    sci["management"] = mg
    r = S._sim_v4(site, y, theta, sci, extra=extra or None)
    return {"yield": r["yield_t_ha"], "agb": r["agb_t_ha"], "lai_max": max(r["lai"].values()), "lai": r["lai"]}


def main():
    gt = S.ground_truth()
    cal = json.loads((S.OUT / "hybrid_calibration_v4.json").read_text())["params"]
    theta = [cal[k] for k in S.CAL_PARAMS_V4]
    names = ["v4", "no stand", "no gauge", "no residue", "SoilGrids not SSURGO", "shipped root front",
             "1 May sowing", "stand: seed weight only", "stand: sqrt leaf share"]
    out = {}
    for name in names:
        rows = {}
        for s, y in CASES:
            r = variant(s, y, name, theta)
            t = gt[s][y]
            lai_e = [(r["lai"].get(d.isoformat(), 0.0) - v) for d, v in t["lai"]]
            rows[f"{s}_{y}"] = {"yield": round(r["yield"], 2), "meas": round(t["yield_t_ha"], 2), "agb": round(r["agb"], 2),
                                "agb_meas": round(t["agb_t_ha"], 2), "lai_max": round(r["lai_max"], 2),
                                "lai_max_meas": round(max(v for _, v in t["lai"]), 2),
                                "lai_rmse": round(math.sqrt(sum(e * e for e in lai_e) / len(lai_e)), 2)}
        summ = {}
        for g, keep in (("irrigated", lambda k: not k.startswith("Ne3")), ("rainfed", lambda k: k.startswith("Ne3"))):
            e = [v["yield"] - v["meas"] for k, v in rows.items() if keep(k)]
            a = [v["agb"] - v["agb_meas"] for k, v in rows.items() if keep(k)]
            summ[g] = {"yield_rmse": math.sqrt(sum(x * x for x in e) / len(e)), "yield_bias": sum(e) / len(e),
                       "agb_bias": sum(a) / len(a),
                       "lai_rmse": sum(v["lai_rmse"] for k, v in rows.items() if keep(k)) / len(e)}
        out[name] = {"summary": summ, "rows": rows}
        print(f"{name:24s}", " | ".join(f"{g} yield RMSE {v['yield_rmse']:.2f} bias {v['yield_bias']:+.2f} "
                                         f"AGB bias {v['agb_bias']:+.2f} LAI RMSE {v['lai_rmse']:.2f}"
                                         for g, v in summ.items()), flush=True)
    (S.OUT / "ablation.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
