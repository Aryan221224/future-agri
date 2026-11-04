"""USDA SSURGO soil survey for US fields: the mapped soil's measured horizons.

SoilGrids is a global 250 m machine-learning estimate; inside the United States the NRCS soil
survey (SSURGO) maps each field's soil series with horizon-by-horizon water retention from
laboratory data. The dominant component's volumetric water content at 1/3 bar (field
capacity) and 15 bar (wilting point) and saturated conductivity are depth-weighted over the
root zone. Soil Data Access (SDA) is public and needs no key.

    ssurgo_rootzone(lat, lon, cache, depth_cm=150) -> (horizons, SoilParams, Provenance)
"""
from __future__ import annotations

from .base import Cache, DataUnavailable, Provenance, session, today

SDA = "https://SDMDataAccess.sc.egov.usda.gov/Tabular/post.rest"


def _query(lat: float, lon: float) -> list[dict]:
    q = ("SELECT mu.muname, c.compname, c.comppct_r, h.hzdept_r, h.hzdepb_r, h.awc_r, h.wthirdbar_r, "
         "h.wfifteenbar_r, h.wsatiated_r, h.ksat_r, h.sandtotal_r, h.claytotal_r, h.om_r "
         f"FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('POINT({lon:.6f} {lat:.6f})') AS k "
         "INNER JOIN mapunit mu ON mu.mukey = k.mukey "
         "INNER JOIN component c ON c.mukey = mu.mukey AND c.majcompflag = 'Yes' "
         "INNER JOIN chorizon h ON h.cokey = c.cokey ORDER BY c.comppct_r DESC, h.hzdept_r")
    r = session().post(SDA, json={"format": "JSON+COLUMNNAME", "query": q}, timeout=60)
    r.raise_for_status()
    t = r.json().get("Table") or []
    if len(t) < 2:
        raise DataUnavailable("soil (SSURGO)", f"no SSURGO map unit at ({lat}, {lon}) -- outside the US?")
    cols = t[0]
    rows = [dict(zip(cols, x)) for x in t[1:]]
    top = rows[0]["compname"]
    return [r for r in rows if r["compname"] == top]


def ssurgo_rootzone(lat: float, lon: float, cache: Cache, depth_cm: float = 150.0):
    from fa_twin.engine import SoilParams
    key = cache.key("ssurgo", round(lat, 5), round(lon, 5))
    rows = cache.get_json(key)
    if rows is None:
        rows = _query(lat, lon)
        cache.put_json(key, rows)

    def f(x):
        try:
            return None if x in (None, "") else float(x)
        except ValueError:
            return None
    acc = {"fc": 0.0, "wp": 0.0, "sat": 0.0, "ks": 0.0, "w": 0.0}
    for h in rows:
        top, bot = f(h["hzdept_r"]), f(h["hzdepb_r"])
        fc, wp, awc = f(h["wthirdbar_r"]), f(h["wfifteenbar_r"]), f(h["awc_r"])
        if top is None or bot is None or fc is None or wp is None or top >= depth_cm:
            continue
        w = min(bot, depth_cm) - top
        sat = f(h["wsatiated_r"]) or max(fc + 8.0, 45.0)
        # NRCS's available water capacity (awc_r) is the survey's own plant-available estimate
        # (field capacity taken at 1/10 bar for coarse soils, 1/3 bar otherwise, adjusted for
        # rock fragments); field capacity = wilting point + AWC keeps the bucket consistent with it.
        if awc is not None and 0.0 < awc < 0.4:
            fc = wp + awc * 100
        acc["fc"] += fc / 100 * w
        acc["wp"] += wp / 100 * w
        acc["sat"] += sat / 100 * w
        acc["ks"] += (f(h["ksat_r"]) or 5.0) * 0.36 * 24 / 10 * w      # um/s -> cm/day
        acc["w"] += w
    if acc["w"] < 0.5 * depth_cm:
        raise DataUnavailable("soil (SSURGO)", "horizons do not reach half the root zone")
    W = acc["w"]
    smfcf, smw, sm0, k0 = acc["fc"] / W, acc["wp"] / W, acc["sat"] / W, acc["ks"] / W
    sp = SoilParams(SMFCF=round(smfcf, 4), SM0=round(max(sm0, smfcf + 0.05), 4), SMW=round(smw, 4), CRAIRC=0.06,
                    RDMSOL=float(min(depth_cm, W)), K0=round(k0, 3), SOPE=round(min(max(k0, 0.5), 10.0), 3),
                    KSUB=round(min(max(0.5 * k0, 0.2), 5.0), 3))
    prov = Provenance(f"USDA NRCS SSURGO: {rows[0]['muname']} ({rows[0]['compname']}, {rows[0]['comppct_r']} %), "
                      f"0-{depth_cm:.0f} cm horizons", SDA, "public domain (USDA)", today(),
                      "Soil Survey Staff, NRCS, USDA. Soil Survey Geographic (SSURGO) Database", True,
                      "the mapped soil series' laboratory water retention, not a measurement of this field")
    return rows, sp, prov
