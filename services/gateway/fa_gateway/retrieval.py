"""From captured imagery to a twin-assimilable LAI observation.

Index choice is not a detail here -- it decides whether the campaign measures
anything in the half of the season that matters.

**Do not use NDVI as the LAI observable.** It saturates at LAI ~2-3 (Gitelson:
"NDVI loses sensitivity when LAI exceeds about 2"). A cereal canopy peaks at
LAI 5-7, so NDVI is blind across roughly the top half of the season -- exactly
the window that determines yield.

WDRVI is the free fix: same two bands as NDVI, one line of arithmetic, and
about +31% dynamic range at high LAI. If a red-edge band exists, CIred-edge and
NDRE are better still. GCC from plain RGB is a legitimate fallback but saturates
around LAI 4.5, so it captures canopy development and senescence timing and goes
flat at peak.

The retrieval carries a conformal interval, not a point estimate, because the
twin's EnKF weights observations by their stated error and an overconfident
observation actively degrades the analysis.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


# ---------------------------------------------------------------------------
# Reflectance calibration
# ---------------------------------------------------------------------------

@dataclass
class PanelCalibration:
    """Empirical-line calibration from in-scene reference panel(s).

    With two panels of known reflectance you fit DN = a*rho + b per band and
    invert -- gain and offset. With one panel you get gain only, which assumes
    the dark offset is negligible. On an uncooled sensor at 45 C it is not, so
    single-panel calibration is a compromise and the code says so rather than
    hiding it.
    """
    gain: dict[str, float]
    offset: dict[str, float]
    n_panels: int
    panel_reflectance: dict[str, float]

    def to_reflectance(self, dn: np.ndarray, band: str) -> np.ndarray:
        g = self.gain.get(band, 1.0)
        b = self.offset.get(band, 0.0)
        return np.clip((np.asarray(dn, float) - b) * g, 0.0, 1.5)

    @property
    def offset_is_assumed(self) -> bool:
        return self.n_panels < 2


def calibrate_from_panels(panel_dn: dict[str, list[float]],
                          panel_reflectance: dict[str, list[float]]
                          ) -> PanelCalibration:
    """Fit the empirical line per band from panel observations."""
    gain, offset, npan = {}, {}, 0
    ref_out = {}
    for band, dns in panel_dn.items():
        refl = panel_reflectance.get(band) or panel_reflectance.get("all")
        if refl is None or len(dns) == 0:
            continue
        npan = max(npan, len(dns))
        d = np.asarray(dns, float)
        r = np.asarray(refl, float)
        if len(d) >= 2:
            A = np.vstack([r, np.ones_like(r)]).T
            slope, intercept = np.linalg.lstsq(A, d, rcond=None)[0]
            gain[band] = 1.0 / slope if abs(slope) > 1e-9 else 1.0
            offset[band] = intercept
        else:
            gain[band] = float(r[0] / d[0]) if d[0] else 1.0
            offset[band] = 0.0
        ref_out[band] = float(np.mean(r))
    return PanelCalibration(gain, offset, npan, ref_out)


# ---------------------------------------------------------------------------
# Vegetation indices
# ---------------------------------------------------------------------------

def ndvi(red, nir):
    red, nir = np.asarray(red, float), np.asarray(nir, float)
    return (nir - red) / np.maximum(nir + red, 1e-9)


def wdrvi(red, nir, alpha: float = 0.20):
    """Wide Dynamic Range VI -- NDVI's bands, far more range at high LAI.

    alpha = 0.20 is Gitelson's recommended value. This is the single cheapest
    improvement available to any system currently computing NDVI.
    """
    red, nir = np.asarray(red, float), np.asarray(nir, float)
    return (alpha * nir - red) / np.maximum(alpha * nir + red, 1e-9)


def ndre(rededge, nir):
    re_, nir = np.asarray(rededge, float), np.asarray(nir, float)
    return (nir - re_) / np.maximum(nir + re_, 1e-9)


def ci_rededge(rededge, nir):
    """Chlorophyll index (red edge). Near-linear against LAI well past NDVI's ceiling."""
    re_, nir = np.asarray(rededge, float), np.asarray(nir, float)
    return nir / np.maximum(re_, 1e-9) - 1.0


def gcc(r, g, b):
    """Green chromatic coordinate -- the PhenoCam observable, RGB only.

    Deliberately normalises out brightness, which is what makes it robust
    without radiometric calibration and also what makes it *not* a reflectance.
    Saturates near LAI 4.5.
    """
    r, g, b = (np.asarray(x, float) for x in (r, g, b))
    return g / np.maximum(r + g + b, 1e-9)


def exg(r, g, b):
    """Excess green -- cheap soil/plant separation for canopy cover."""
    r, g, b = (np.asarray(x, float) for x in (r, g, b))
    tot = np.maximum(r + g + b, 1e-9)
    rn, gn, bn = r / tot, g / tot, b / tot
    return 2 * gn - rn - bn


def canopy_cover(r, g, b, threshold: float | None = None) -> float:
    """Green fraction by Otsu-thresholded ExG. Robust, and does not saturate."""
    e = exg(r, g, b)
    if threshold is None:
        vals = e.ravel()
        lo, hi = np.percentile(vals, [1, 99])
        bins = np.linspace(lo, hi, 128)
        hist, _ = np.histogram(vals, bins=bins)
        w = np.cumsum(hist)
        m = np.cumsum(hist * (bins[:-1] + np.diff(bins) / 2))
        tot_w, tot_m = w[-1], m[-1]
        with np.errstate(divide="ignore", invalid="ignore"):
            between = (tot_m * w / np.maximum(tot_w, 1) - m) ** 2 / \
                      np.maximum(w * (tot_w - w), 1e-9)
        threshold = float(bins[int(np.nanargmax(between))])
    return float(np.mean(e > threshold))


# ---------------------------------------------------------------------------
# LAI retrieval
# ---------------------------------------------------------------------------

@dataclass
class LAIModel:
    """Empirical VI -> LAI relation, with its own honest error term.

    ``rmse`` is not decoration: it becomes the observation standard deviation
    the EnKF uses. The literature's operational UAV retrievals sit at RMSE
    0.6-1.0, and the assimilation literature says you want <= 0.3 for a 50%
    yield-error reduction. Stating the real number is what keeps the filter from
    over-trusting the observation -- and what makes the gap visible instead of
    quietly assumed away.
    """
    index: str
    form: str = "exp"          # "exp": LAI = a*exp(b*VI);  "lin": LAI = a*VI + b
    a: float = 0.35
    b: float = 3.0
    rmse: float = 0.7
    valid_lo: float = -1.0
    valid_hi: float = 1.0
    saturates_above_lai: float | None = None
    source: str = "uncalibrated default -- fit against local ground truth"

    def predict(self, vi_value: float) -> float:
        v = float(np.clip(vi_value, self.valid_lo, self.valid_hi))
        lai = self.a * math.exp(self.b * v) if self.form == "exp" else self.a * v + self.b
        return float(np.clip(lai, 0.0, 10.0))

    def observation_sd(self, lai: float) -> float:
        """Error grows with LAI, and blows up past saturation."""
        sd = max(self.rmse, 0.20 * lai)
        if self.saturates_above_lai and lai > self.saturates_above_lai:
            sd *= 2.5          # beyond saturation the index is barely informative
        return sd


#: Defaults chosen so the *good* index is the easy one to reach for.
DEFAULT_MODELS = {
    "wdrvi": LAIModel("wdrvi", "exp", 1.55, 2.1, 0.70, -1.0, 1.0, None,
                      "generic cereal; recalibrate locally"),
    "ci_rededge": LAIModel("ci_rededge", "lin", 1.05, 0.15, 0.65, 0.0, 8.0, None,
                           "generic cereal; recalibrate locally"),
    "ndre": LAIModel("ndre", "exp", 0.55, 4.2, 0.75, -0.2, 0.8, None,
                     "generic cereal; recalibrate locally"),
    "ndvi": LAIModel("ndvi", "exp", 0.30, 3.2, 0.95, -0.2, 0.95, 2.5,
                     "NOT RECOMMENDED as the LAI observable -- saturates near LAI 2-3"),
    "gcc": LAIModel("gcc", "lin", 22.0, -6.4, 0.90, 0.28, 0.50, 4.5,
                    "RGB fallback; saturates near LAI 4.5"),
}


@dataclass
class RetrievedLAI:
    station_id: str
    lai: float
    sd: float
    lo: float
    hi: float
    index: str
    index_value: float
    saturated: bool
    needs_review: bool
    note: str = ""


def retrieve_lai(station_id: str, bands: dict[str, np.ndarray],
                 model: LAIModel | None = None, prefer: str | None = None,
                 coverage: float = 0.90) -> RetrievedLAI:
    """Retrieve LAI from calibrated per-band reflectance at one station.

    Picks the best index the available bands support, unless told otherwise:
    CIred-edge > WDRVI > GCC. NDVI is never chosen automatically.
    """
    have = set(bands)
    if prefer:
        index = prefer
    elif {"rededge", "nir"} <= have:
        index = "ci_rededge"
    elif {"red", "nir"} <= have:
        index = "wdrvi"
    elif {"red", "green", "blue"} <= have:
        index = "gcc"
    else:
        raise ValueError(f"cannot retrieve LAI from bands {sorted(have)}")

    if index == "ci_rededge":
        vi = float(np.median(ci_rededge(bands["rededge"], bands["nir"])))
    elif index == "ndre":
        vi = float(np.median(ndre(bands["rededge"], bands["nir"])))
    elif index == "wdrvi":
        vi = float(np.median(wdrvi(bands["red"], bands["nir"])))
    elif index == "ndvi":
        vi = float(np.median(ndvi(bands["red"], bands["nir"])))
    else:
        vi = float(np.median(gcc(bands["red"], bands["green"], bands["blue"])))

    m = model or DEFAULT_MODELS[index]
    lai = m.predict(vi)
    sd = m.observation_sd(lai)
    z = {0.80: 1.282, 0.90: 1.645, 0.95: 1.960}.get(round(coverage, 2), 1.645)
    saturated = bool(m.saturates_above_lai and lai >= m.saturates_above_lai)
    note = ""
    if saturated:
        note = (f"{index} saturates above LAI {m.saturates_above_lai}; this "
                "observation carries little information and is down-weighted")
    return RetrievedLAI(station_id=station_id, lai=lai, sd=sd,
                        lo=max(0.0, lai - z * sd), hi=lai + z * sd,
                        index=index, index_value=vi, saturated=saturated,
                        needs_review=saturated or sd > 1.0, note=note)


def aggregate_field(retrievals: list[RetrievedLAI]) -> tuple[float, float, float]:
    """Field mean LAI, its standard error, and the observed spatial CV.

    The standard error -- not the per-station sd -- is what the twin should
    assimilate when the observable is a field-mean state. The spatial CV is
    worth returning because it tells you whether the station count you chose
    was right: if the observed CV is well above the value assumed when sizing
    the campaign, the field mean is less precise than planned.
    """
    if not retrievals:
        raise ValueError("no retrievals to aggregate")
    vals = np.array([r.lai for r in retrievals], float)
    mean = float(vals.mean())
    spatial_sd = float(vals.std(ddof=1)) if len(vals) > 1 else 0.0
    meas = float(np.sqrt(np.mean([r.sd ** 2 for r in retrievals])))
    se = math.sqrt(spatial_sd**2 / max(len(vals), 1) + meas**2 / max(len(vals), 1))
    cv = spatial_sd / mean if mean > 0 else float("nan")
    return mean, se, cv
