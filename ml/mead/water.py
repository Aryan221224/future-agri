"""Irrigation water on the Mead pivots: the software's soil-water scheduler against what the farm
actually applied, season by season, 2014-2021 (AmeriFlux US-Ne1 and US-Ne2, maize years).

What is real here
-----------------
* Every irrigation pass the farm made, from the AmeriFlux BADM management records (DM_WATER,
  CC-BY-4.0). Depths are the field-average gross depth of each pass, transcribed by hand below
  because the records mix full circles, rain-interrupted partial circles, resumed passes and
  duplicated rows; a partial circle counts as depth x degrees covered / 360. Fertigation and
  chemigation passes (water carrying N or herbicide) are kept on both policies: they were made
  for the nitrogen, not for the water.
* Sowing date, seeding rate, hybrid maturity (GDU where the record gives it) and combine yield
  (bu/acre at 15.5 %) from the same records.
* Weather: NASA POWER with the MEAD 6 S (USC00255362) gauge's rain and temperature merged in.
  Soil: USDA SSURGO root zone under each pivot.
* Crop: the v4 model and its hybrid, calibrated on Ne1 2001-2003 only (nothing after 2003 was
  used to build it), run water-limited (WOFOST 7.2, classic water balance).

Policies (same weather, soil, crop, sowing, fertigation passes):
  farmer      the passes the farm made (as metered)
  twin        fa_planner.irrigation.IrrigationScheduler as shipped (trigger 55 % of plant-available
              water, 1.25 in pass = the pivot's standard pass, >= 4 days apart = one pivot rotation),
              irrigating from emergence to full dent (DVS 1.75); rain skip uses the gauge's next 2
              days, i.e. a perfect 2-day forecast. ``twin_noforecast`` drops that skip.
  rainfed     no irrigation at all (what the water is worth)
  potential   WOFOST potential production (water never limits)

    python ml/mead/water.py        # -> ml/results/mead/water.json

The yield under each policy is simulated; the farmer's simulated yield is also checked against
the combine. WOFOST 7.2 has no nitrogen and does not penalise over-watering or leaching, so this
measures water saved at equal simulated yield, not the nitrate that extra water carries away.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in ("packages/fa_core", "services/twin", "services/farm", "services/planner"):
    sys.path.insert(0, str(ROOT / p))
sys.path.insert(0, str(Path(__file__).parent))
warnings.filterwarnings("ignore")

import study as S  # noqa: E402

IN = 2.54          # cm per inch
BU = 0.06277       # t/ha per bu/acre of maize at 15.5 %
EMERGENCE = 0.95   # plants per seed (assumed; the records give seeds, not plant counts)
EFFICIENCY = 0.85  # centre-pivot application efficiency, both policies
STOP_DVS = 1.75    # last irrigation at full dent

# (start date, field-average inches, kind). kind "N" = fertigation / chemigation pass.
# Notes give how a partial or resumed pass was counted.
SEASONS = {
    ("Ne1", 2014): dict(sowing="04-21", seeds_ac=34000, gdu=2875, yield_bu=177.5, residue=0.10, events=[
        ("06-10", 0.30, "N"), ("07-02", 0.30, "N"), ("07-08", 1.25, "W"), ("07-14", 1.25, "W"),
        ("07-19", 1.25, "W"), ("07-25", 1.25, "W"), ("07-30", 1.25, "W"), ("08-18", 1.25, "W")]),
    ("Ne1", 2015): dict(sowing="04-29", seeds_ac=32000, gdu=2860, yield_bu=189.0, residue=0.10, events=[
        ("05-12", 0.26, "N"),                      # interrupted 5/12, completed 5/18: one pass
        ("07-01", 0.33, "N"),                      # interrupted 7/2, completed 7/4: one pass
        ("07-13", 1.28, "W"),                      # interrupted 7/14, completed 7/17-20: one pass
        ("07-24", 1.29 * 321 / 360, "W"),          # 265 CW to 226, stopped by rain
        ("08-03", 1.29 * 179 / 360, "W"),          # 47 CW to 226
        ("08-14", 1.29 * 39 / 360, "W")]),         # 226 to 265, completed
    ("Ne1", 2016): dict(sowing="05-19", seeds_ac=32000, gdu=2750, yield_bu=202.1, residue=0.10, events=[
        ("07-01", 0.49, "N"),                      # three rows, one fertigation pass (7/1-7/20)
        ("07-20", 0.90, "W"), ("07-28", 1.27, "W"),
        ("08-09", 1.27 * 192 / 360, "W")]),        # 267 CW to 99 only, cycle abandoned (rain)
    ("Ne1", 2017): dict(sowing="04-27", seeds_ac=35000, gdu=2750, yield_bu=225.0, residue=0.10, events=[
        ("06-30", 0.49, "N"), ("07-06", 1.25, "W"), ("07-14", 1.25, "W"), ("07-18", 1.25, "W"),
        ("07-31", 1.25, "W"),
        ("08-08", 1.25 * 209 / 360, "W")]),        # 259 CW to 108, end of irrigation
    ("Ne1", 2018): dict(sowing="05-08", seeds_ac=37000, gdu=2750, yield_bu=135.0, residue=0.10, events=[
        ("05-10", 0.58, "W"),                      # "Applied .58 inches"
        ("06-18", 0.69 * 101 / 360, "W"),          # 276 CW to 17, cancelled (profile full)
        ("07-16", 1.35, "W"), ("08-01", 1.34, "W"), ("08-08", 1.34, "W")]),
    ("Ne1", 2020): dict(sowing="04-20", seeds_ac=33000, gdu=2750, yield_bu=26837.9 / 122.76, residue=0.10, events=[
        ("04-24", 0.50, "W"), ("06-23", 0.68, "N"),
        ("06-29", 0.60 * 313 / 360, "W"),          # 303 CW to 187, then 187 to 256
        ("07-06", 1.25, "W"), ("07-14", 1.25, "W"), ("07-22", 1.25, "W"), ("08-02", 1.27, "W"),
        ("08-11", 1.25, "W"), ("08-24", 1.25, "W"), ("08-31", 1.00, "W")]),
    ("Ne1", 2021): dict(sowing="04-27", seeds_ac=33000, gdu=2750, yield_bu=226.7, residue=0.70, events=[
        ("04-30", 0.31, "N"),                      # UAN incorporation + herbicide activation
        ("07-09", 1.25, "W"), ("07-21", 1.25, "W"), ("07-28", 1.25, "W"),
        ("08-24", 1.25 * 263 / 360, "W")]),        # 270 CCW to 219, then to 007, cancelled (rain)
    ("Ne2", 2015): dict(sowing="04-29", seeds_ac=32000, gdu=2860, yield_bu=215.0, residue=0.50, events=[
        ("07-13", (1.32 * 75 + 1.25 * 285) / 360, "W"),   # 95 to 170, resumed 170 to 95
        ("07-24", 1.23 * 125 / 360, "W"),          # 95 CW to 220, rain
        ("08-03", 1.23 * 141 / 360, "W"),          # 220 to 001
        ("08-14", 1.25 * 94 / 360, "W")]),         # 001 to 95, completed
    ("Ne2", 2017): dict(sowing="05-07", seeds_ac=33500, gdu=2750, yield_bu=None, residue=0.50, events=[
        ("07-03", 0.31, "N"), ("07-11", 1.25, "W"), ("07-19", 1.25, "W"), ("07-31", 1.25, "W"),
        ("08-08", 1.25, "W")]),
    ("Ne2", 2019): dict(sowing="04-23", seeds_ac=33180, gdu=2750, yield_bu=None, residue=0.50, events=[
        ("07-02", 0.21, "N"), ("07-08", 1.20, "W"), ("07-15", 1.20, "W"), ("07-29", 1.20, "W"),
        ("08-09", 1.25, "W")]),
    ("Ne2", 2021): dict(sowing="04-26", seeds_ac=33000, gdu=2750, yield_bu=270.1, residue=0.50, events=[
        ("07-09", 1.16, "W"), ("07-21", 1.16, "W"), ("07-28", 1.16, "W")]),
}

_WX: dict = {}


def weather(site: str) -> list[dict]:
    """NASA POWER Sep 2013 - Dec 2021 with the MEAD 6 S gauge merged in."""
    if site not in _WX:
        from fa_farm.sources.base import Cache
        from fa_farm.sources.stations import merge_station
        from fa_farm.sources.weather import power_daily
        lat, lon, _ = S.SITES[site]
        cache = Cache(S.DATA / "cache")
        a, b = dt.date(2013, 9, 1), dt.date(2021, 12, 31)
        recs, _, elev = power_daily(lat, lon, a, b, cache)
        recs, prov, info = merge_station(recs, lat, lon, a, b, cache)
        _WX[site] = (recs, elev, info)
    return _WX[site]


def setup(site: str, y: int) -> dict:
    """The v4 field set-up for a 2014-2021 season."""
    from fa_farm.sources.base import Cache
    from fa_farm.sources.ssurgo import ssurgo_rootzone
    from fa_twin.hybrids import crop_table, rooting_override, tsums_for_hybrid
    from fa_twin.management import population_overrides, soil_evaporation_factor
    from fa_twin.soilwater import available_water_at_sowing
    s = SEASONS[(site, y)]
    lat, lon, _ = S.SITES[site]
    _, sp, _ = ssurgo_rootzone(lat, lon, Cache(S.DATA / "cache"))
    crop = crop_table("maize", "Grain_maize_204")
    wx, elev, _ = weather(site)
    hist, _ = S._weather_v4(site)
    hist = [r for r in hist if r["DAY"].year <= 2000]
    sow = dt.date.fromisoformat(f"{y}-{s['sowing']}")
    ph = tsums_for_hybrid(hist, s["sowing"], float(s["gdu"]), crop)
    evf = soil_evaporation_factor(s["residue"])
    wav = available_water_at_sowing(wx, sp.SMFCF, sp.SMW, sow, sp.RDMSOL, lat=lat, elev=elev, evap_factor=evf)
    cal = json.loads((S.OUT / "hybrid_calibration_v4.json").read_text())["params"]
    base = {k: cal[k] for k in S.CAL_PARAMS_V4}
    pop = round(s["seeds_ac"] / 0.404686 * EMERGENCE)
    ov = {**ph.overrides(), **rooting_override("maize", sp.RDMSOL, crop["RDMCR"]), "RRI": S.RRI_MAIZE}
    ov.pop("TSUMEM", None)
    ov.update(base)
    ov.update(population_overrides("maize", {**crop, **base}, pop))
    return {"soil": sp, "WAV": wav["WAV_cm"], "overrides": ov, "sowing": sow, "population": pop,
            "evap_factor": evf, "weather": [r for r in wx if dt.date(y - 1, 11, 1) <= r["DAY"] <= dt.date(y, 12, 31)],
            "elev": elev}


def _twin(site, y, st, engine, events=None):
    from fa_twin.engine import ZoneConfig, ZoneTwin
    from fa_twin.weather import records_provider
    lat, lon, _ = S.SITES[site]
    cfg = ZoneConfig(site, lat, lon, "maize", "Grain_maize_204", st["sowing"], dt.date(y, 10, 15),
                     soil=st["soil"], WAV=st["WAV"], param_overrides=st["overrides"])
    wx = records_provider(st["weather"], lat, lon, st["elev"], "POWER + GHCN gauge", soil_evap_factor=st["evap_factor"])
    return ZoneTwin(cfg, wx, events=events, engine=engine)


def _result(tw, applied_cm, passes):
    df = tw.output_frame()
    last = df.iloc[-1]
    return {"yield_t_ha": round(float(last["TWSO"]) / 1000 / (1 - S.MOIST), 2),
            "agb_t_ha": round(float(last["TAGP"]) / 1000, 2),
            "transpiration_cm": round(float(df["TRA"].fillna(0).sum()), 1) if "TRA" in df else None,
            "irrigation_mm": round(applied_cm * 10, 1), "passes": passes}


def farmer(site, y, st, kinds=("N", "W")):
    ev = [(dt.date.fromisoformat(f"{y}-{d}"), a * IN) for d, a, k in SEASONS[(site, y)]["events"] if k in kinds]
    events = [{"date": d, "irrigation_cm": cm, "efficiency": EFFICIENCY} for d, cm in ev]
    tw = _twin(site, y, st, "Wofost72_WLP_CWB", events=events or None)
    tw.run_to_end()
    return _result(tw, sum(cm for _, cm in ev), len(ev))


def scheduled(site, y, st, use_forecast=True):
    """Daily loop: the shipped scheduler reads the twin's root-zone soil moisture."""
    from pcse import signals
    from fa_planner.irrigation import IrrigationScheduler
    sch = IrrigationScheduler(trigger=0.55, amount_cm=1.25 * IN, min_interval_days=4,
                              rain_skip_mm=10.0 if use_forecast else 1e9, lookahead_days=2)
    fert = [(dt.date.fromisoformat(f"{y}-{d}"), a * IN) for d, a, k in SEASONS[(site, y)]["events"] if k == "N"]
    rain = {r["DAY"]: r["RAIN"] for r in st["weather"]}          # cm/day in PCSE records
    tw = _twin(site, y, st, "Wofost72_WLP_CWB",
               events=[{"date": d, "irrigation_cm": cm, "efficiency": EFFICIENCY} for d, cm in fert] or None)
    soil = st["soil"]
    day, last, applied, passes, log = st["sowing"], None, sum(cm for _, cm in fert), len(fert), []
    end = dt.date(y, 10, 15)
    while day < end and not tw.terminated:
        tw.run_till(day)
        x = tw.state()
        if x.get("DVS") is not None and 0.0 <= x["DVS"] < STOP_DVS:
            fc_mm = 10 * sum(rain.get(day + dt.timedelta(days=k), 0.0) for k in (1, 2))
            dec = sch.decide(day, x.get("SM"), soil.SMFCF, soil.SMW, last, fc_mm)
            if dec.irrigate:
                tw.model._send_signal(signal=signals.irrigate, amount=dec.amount_cm, efficiency=EFFICIENCY)
                last, applied, passes = day, applied + dec.amount_cm, passes + 1
                log.append(day.isoformat())
        day += dt.timedelta(days=1)
    tw.run_to_end()
    r = _result(tw, applied, passes)
    r["dates"] = log
    return r


def blind_setup(site: str, y: int) -> dict:
    """What a twin would know without the farm's own instruments: gridded weather only (no rain
    gauge) and the global soil map (SoilGrids) instead of the soil survey."""
    from fa_farm.sources.base import Cache
    from fa_farm.sources.soil import soilgrids_rootzone
    from fa_farm.sources.weather import power_daily
    from fa_twin.soilwater import available_water_at_sowing
    st = dict(setup(site, y))
    lat, lon, _ = S.SITES[site]
    cache = Cache(S.DATA / "cache")
    recs, _, _ = power_daily(lat, lon, dt.date(2013, 9, 1), dt.date(2021, 12, 31), cache)
    _, sp, _ = soilgrids_rootzone(lat, lon, cache)
    st["soil"] = sp
    st["weather"] = [r for r in recs if dt.date(y - 1, 11, 1) <= r["DAY"] <= dt.date(y, 12, 31)]
    st["WAV"] = available_water_at_sowing(recs, sp.SMFCF, sp.SMW, st["sowing"], sp.RDMSOL, lat=lat, elev=st["elev"],
                                          evap_factor=st["evap_factor"])["WAV_cm"]
    return st


def blind(site, y, st, bst):
    """The blind twin decides; the field (gauge weather, survey soil) receives its passes."""
    dec = scheduled(site, y, bst)
    ev = [(dt.date.fromisoformat(f"{y}-{d}"), a * IN) for d, a, k in SEASONS[(site, y)]["events"] if k == "N"]
    ev += [(dt.date.fromisoformat(d), 1.25 * IN) for d in dec["dates"]]
    tw = _twin(site, y, st, "Wofost72_WLP_CWB",
               events=[{"date": d, "irrigation_cm": cm, "efficiency": EFFICIENCY} for d, cm in ev] or None)
    tw.run_to_end()
    r = _result(tw, sum(cm for _, cm in ev), len(ev))
    r["dates"] = dec["dates"]
    return r


def run_one(site, y):
    st = setup(site, y)
    s = SEASONS[(site, y)]
    t0 = time.perf_counter()
    out = {"site": site, "year": y, "sowing": st["sowing"].isoformat(), "population": st["population"],
           "measured_t_ha": round(s["yield_bu"] * BU, 2) if s["yield_bu"] else None,
           "farmer": farmer(site, y, st),
           "twin": scheduled(site, y, st),
           "twin_noforecast": scheduled(site, y, st, use_forecast=False),
           "twin_blind": blind(site, y, st, blind_setup(site, y)),
           "rainfed": farmer(site, y, st, kinds=()),
           "fertigation_only": farmer(site, y, st, kinds=("N",))}
    pp = _twin(site, y, st, "Wofost72_PP")
    pp.run_to_end()
    out["potential"] = _result(pp, 0.0, 0)
    out["seconds"] = round(time.perf_counter() - t0, 2)
    return out


def main() -> int:
    rows = []
    for site, y in SEASONS:
        r = run_one(site, y)
        rows.append(r)
        f, t = r["farmer"], r["twin"]
        print(f"{site} {y}: farmer {f['irrigation_mm']:.0f} mm -> {f['yield_t_ha']:.2f} | twin {t['irrigation_mm']:.0f} mm "
              f"-> {t['yield_t_ha']:.2f} | no-fcst {r['twin_noforecast']['irrigation_mm']:.0f} mm -> "
              f"{r['twin_noforecast']['yield_t_ha']:.2f} | blind {r['twin_blind']['irrigation_mm']:.0f} mm -> "
              f"{r['twin_blind']['yield_t_ha']:.2f} | rainfed {r['rainfed']['yield_t_ha']:.2f} | potential "
              f"{r['potential']['yield_t_ha']:.2f} | measured {r['measured_t_ha']} | {r['seconds']} s", flush=True)
    summ = {}
    for pol in ("farmer", "twin", "twin_noforecast", "twin_blind", "rainfed", "fertigation_only", "potential"):
        summ[pol] = {"irrigation_mm_mean": sum(r[pol]["irrigation_mm"] for r in rows) / len(rows),
                     "yield_t_ha_mean": sum(r[pol]["yield_t_ha"] for r in rows) / len(rows),
                     "passes_total": sum(r[pol]["passes"] for r in rows)}
    m = [(r["farmer"]["yield_t_ha"], r["measured_t_ha"]) for r in rows if r["measured_t_ha"]]
    e = [p - q for p, q in m]
    summ["farmer_sim_vs_combine"] = {"n": len(e), "rmse": (sum(x * x for x in e) / len(e)) ** 0.5, "bias": sum(e) / len(e)}
    (S.OUT / "water.json").write_text(json.dumps({"seasons": rows, "summary": summ,
                                                  "assumptions": {"emergence": EMERGENCE, "efficiency": EFFICIENCY,
                                                                  "stop_dvs": STOP_DVS}}, indent=1, default=str))
    for k, v in summ.items():
        print(k, {a: round(b, 2) for a, b in v.items()})
    return 0


if __name__ == "__main__" and "--plot" not in sys.argv:
    main()


DIESEL_GAL_PER_ACRE_IN = 1.88 * 1.25   # UNL CropWatch: 125 ft lift, 35 psi, at 80 % pump performance
CO2_KG_PER_GAL = 10.18                 # US EPA, diesel


def energy(mm: float) -> dict:
    gal_ac = mm / 25.4 * DIESEL_GAL_PER_ACRE_IN
    gal_ha = gal_ac / 0.404686
    return {"m3_ha": mm * 10, "diesel_l_ha": gal_ha * 3.78541, "co2_kg_ha": gal_ha * CO2_KG_PER_GAL}


def plot() -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    d = json.loads((S.OUT / "water.json").read_text())["seasons"]
    lab = [f"{r['site']}\n{r['year']}" for r in d]
    x = range(len(d))
    fig, ax = plt.subplots(figsize=(9, 3.8))
    for i, (pol, name) in enumerate((("farmer", "farm (metered)"), ("twin", "twin, survey soil + gauge"),
                                     ("twin_blind", "twin, global soil map, no gauge"))):
        ax.bar([k + (i - 1) * 0.27 for k in x], [r[pol]["irrigation_mm"] for r in d], 0.27, label=name)
    ax.set_xticks(list(x), lab, fontsize=8)
    ax.set_ylabel("irrigation, mm per season")
    ax.set_title("Mead pivots 2014-2021: water applied (simulated yield within 1 % except Ne1 2020 blind)")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    p = S.OUT / "figures" / "water.png"
    fig.savefig(p, dpi=160)
    plt.close(fig)
    return p


if __name__ == "__main__" and "--plot" in sys.argv:
    print(plot())
