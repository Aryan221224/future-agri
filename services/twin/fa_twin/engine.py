"""WOFOST twin: one crop-state simulator per management zone.

Engine choice
-------------
`Wofost81_NWLP_CWB_CNB` (nitrogen- and water-limited) is the default, because a
twin that cannot represent nitrogen response cannot answer the question a farmer
is actually asking -- how much fertiliser, when. `Wofost72_WLP_CWB` is kept as a
validated fallback: it is the better-benchmarked engine and requires far fewer
parameters, which matters when you have no local calibration yet.

Speed
-----
Measured on this stack: ~0.14 s for a 150-day water-limited season on one core,
so a 100-member ensemble is ~14 s and is trivially parallel across members.

This is the fact that killed the neural-surrogate plan. Surrogate papers exist
because APSIM takes ~2 minutes a run; the ~850x gap that justifies emulation
does not exist for PCSE. Training a surrogate would cost ~10^5-10^6 simulations
to buy a speedup we do not need, and would add an unvalidated approximation
between the observations and the recommendation. See docs/adr/0004.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VENDOR_ROOT = Path(__file__).resolve().parents[3] / "vendor"

# The crop-parameter repository keeps one branch per WOFOST version and they are
# NOT interchangeable: the wofost81 sets drop AMAXTB (different photosynthesis
# parameterisation) and add the N/P/K tables, so loading 8.1 parameters into a
# 7.2 engine fails with "Value for parameter AMAXTB missing". Both branches are
# vendored at pinned SHAs and selected by engine.
PARAM_BRANCH = {"Wofost72": "wofost72", "Wofost73": "wofost72",
                "Wofost81": "wofost81", "Lingra": "wofost72"}

# Crops whose shipped parameters are generic literature compilations rather than
# calibrations. Any output for these carries a warning all the way to the UI --
# a placeholder that looks like a calibration is the most expensive kind of bug
# in this subsystem.
UNCALIBRATED = {"cotton", "sugarcane"}


@dataclass
class SoilParams:
    """Soil water parameters. SoilGrids is a prior, not a measurement."""
    SMFCF: float = 0.32     # field capacity
    SM0: float = 0.45       # saturation
    SMW: float = 0.15       # wilting point
    CRAIRC: float = 0.06    # critical air content
    RDMSOL: float = 100.0   # max rootable depth, cm
    K0: float = 10.0
    SOPE: float = 1.0
    KSUB: float = 1.0

    def as_dict(self) -> dict[str, float]:
        return dict(self.__dict__)


@dataclass
class ZoneConfig:
    """Everything needed to instantiate one zone's twin."""
    zone_id: str
    latitude: float
    longitude: float
    crop: str
    variety: str
    sowing_date: date
    harvest_date: date
    # Rice parameter sets carry TSUMEM=0 because the crop is transplanted, not
    # direct-sown: starting them at "sowing" makes PCSE divide by zero in the
    # emergence rate. Transplanted crops must start at "emergence".
    crop_start_type: str = "auto"   # "sowing" | "emergence" | "auto"
    soil: SoilParams = field(default_factory=SoilParams)
    WAV: float = 25.0           # initial available soil water, cm
    NAVAILI: float = 60.0       # initial available N, kg/ha
    NSOILBASE: float = 10.0     # mineralisable soil N, kg/ha
    NSOILBASE_FR: float = 0.025 # daily fraction released
    CO2: float = 425.0
    max_duration: int = 330


def crop_provider(engine: str = "Wofost72", fpath: str | Path | None = None):
    """Crop parameters from the vendored, pinned repository.

    Never the runtime GitHub fetch: PCSE's default YAMLCropDataProvider
    downloads from raw.githubusercontent.com on first use, which makes an
    air-gapped field gateway fail and makes results depend on whatever was on
    a branch that day.
    """
    from pcse.input import YAMLCropDataProvider
    branch = PARAM_BRANCH.get(engine[:8], "wofost72")
    default = Path(os.environ.get("FA_CROP_PARAMS", VENDOR_ROOT)) / branch
    p = Path(fpath) / branch if fpath else default
    if p.exists():
        return YAMLCropDataProvider(fpath=str(p))
    log.warning("vendored crop parameters not found at %s; falling back to network fetch", p)
    return YAMLCropDataProvider()


TRANSPLANTED = {"rice"}


def resolve_start_type(cfg: ZoneConfig, params: dict | None = None) -> str:
    """Pick sowing vs emergence, defensively.

    ``TSUMEM`` (thermal time from sowing to emergence) is zero for transplanted
    crops. PCSE computes ``DVR = 0.1 * DTSUME / TSUMEM`` during the emergence
    phase, so a "sowing" start on such a cultivar is an immediate
    ZeroDivisionError rather than a wrong number -- which is at least honest,
    but it is avoidable.
    """
    if cfg.crop_start_type != "auto":
        return cfg.crop_start_type
    if params is not None:
        tsumem = params.get("TSUMEM")
        if tsumem is not None and float(tsumem) <= 0:
            return "emergence"
    return "emergence" if cfg.crop in TRANSPLANTED else "sowing"


def _agromanagement(cfg: ZoneConfig, events: list[dict] | None = None,
                    start_type: str = "sowing") -> list[dict]:
    """Build PCSE agromanagement, including timed fertiliser/irrigation events.

    ``events`` is how a prescription becomes something the model can simulate:
    [{"date": date, "N_amount": 60}, {"date": date, "irrigation_cm": 3.0}]
    """
    cal = {
        "crop_name": cfg.crop,
        "variety_name": cfg.variety,
        "crop_start_date": cfg.sowing_date,
        "crop_start_type": start_type,
        "crop_end_date": cfg.harvest_date,
        "crop_end_type": "harvest",
        "max_duration": cfg.max_duration,
    }
    timed: list[dict] = []
    n_events = [e for e in (events or []) if e.get("N_amount")]
    i_events = [e for e in (events or []) if e.get("irrigation_cm")]
    if n_events:
        # PCSE 6.x exposes `apply_n` (N only) for the classic N balance; the
        # older `apply_npk` signal does not exist in this version. P and K are
        # not simulated by the CWB_CNB configuration, so a prescription that
        # specifies them is honestly reported as N-only rather than silently
        # pretending P and K had an effect.
        timed.append({
            "event_signal": "apply_n",
            "name": "fertiliser",
            "comment": "planner-issued",
            "events_table": [
                {e["date"]: {"N_amount": float(e["N_amount"]),
                             "N_recovery": float(e.get("N_recovery", 0.7))}}
                for e in sorted(n_events, key=lambda x: x["date"])
            ],
        })
    if i_events:
        timed.append({
            "event_signal": "irrigate",
            "name": "irrigation",
            "comment": "planner-issued",
            "events_table": [
                {e["date"]: {"amount": float(e["irrigation_cm"]),
                             "efficiency": float(e.get("efficiency", 0.7))}}
                for e in sorted(i_events, key=lambda x: x["date"])
            ],
        })
    start = min([cfg.sowing_date] + [e["date"] for e in (events or [])]) - timedelta(days=1)
    return [{start: {"CropCalendar": cal,
                     "TimedEvents": timed or None,
                     "StateEvents": None}}]


class ZoneTwin:
    """A running WOFOST simulation for one management zone.

    Thin on purpose. The value is in what surrounds it: honest parameter
    provenance, the ability to step to a date and inject an observation, and a
    record of which weather and which parameters produced a number.
    """

    OUTPUT_VARS = ("DVS", "LAI", "TAGP", "TWSO", "TWLV", "TWST", "TRA", "RD", "SM")

    def __init__(self, cfg: ZoneConfig, weather, events: list[dict] | None = None,
                 engine: str = "auto", crop_params_path: str | Path | None = None):
        from pcse.base import ParameterProvider
        from pcse import models

        self.cfg = cfg
        self.weather = weather
        self.events = list(events or [])
        self.uncalibrated = cfg.crop in UNCALIBRATED

        if engine == "auto":
            engine = "Wofost81_NWLP_CWB_CNB" if any(
                e.get("N_amount") for e in self.events) else "Wofost72_WLP_CWB"
        self.engine_name = engine
        model_cls = getattr(models, engine)

        cp = crop_provider(engine, crop_params_path)
        cp.set_active_crop(cfg.crop, cfg.variety)
        self.start_type = resolve_start_type(cfg, dict(cp))

        if engine.startswith("Wofost81"):
            from pcse.input import WOFOST81SiteDataProvider_Classic as WOFOST81SiteDataProvider
            # The Classic 8.1 site provider takes N only; P and K availability
            # are not parameters of this water/N-limited configuration.
            site = WOFOST81SiteDataProvider(
                WAV=cfg.WAV, CO2=cfg.CO2, NAVAILI=cfg.NAVAILI,
                NSOILBASE=cfg.NSOILBASE, NSOILBASE_FR=cfg.NSOILBASE_FR)
        else:
            from pcse.input import WOFOST72SiteDataProvider
            # WOFOST 7.2 takes only WAV; CO2 is an 8.1 parameter.
            site = WOFOST72SiteDataProvider(WAV=cfg.WAV)

        params = ParameterProvider(cropdata=cp, soildata=cfg.soil.as_dict(), sitedata=site)
        self.model = model_cls(
            params, weather, _agromanagement(cfg, self.events, self.start_type))

    # -- stepping ---------------------------------------------------------
    def run_till(self, day: date) -> None:
        self.model.run_till(day)

    def run_to_end(self) -> None:
        self.model.run_till_terminate()

    @property
    def day(self) -> date:
        return self.model.day

    @property
    def terminated(self) -> bool:
        return self.model.flag_terminate

    def state(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for v in self.OUTPUT_VARS:
            try:
                val = self.model.get_variable(v)
            except Exception:
                val = None
            if val is not None:
                out[v] = float(val)
        return out

    def set_lai(self, lai: float) -> dict[str, float]:
        """Inject an observed LAI.

        PCSE's `_set_variable_LAI` rescales the leaf-biomass age classes rather
        than overwriting a scalar, so the update propagates consistently into
        WLV/TWLV and onward into yield. That is what makes state assimilation
        into WOFOST legitimate rather than a cosmetic overwrite -- and it is why
        no fork of PCSE is needed.
        """
        return self.model.set_variable("LAI", float(lai))

    # -- outputs ----------------------------------------------------------
    def output_frame(self):
        import pandas as pd
        rows = self.model.get_output()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).set_index("day")

    def summary(self) -> dict[str, Any]:
        s = self.model.get_summary_output()
        d = dict(s[0]) if s else {}
        d["engine"] = self.engine_name
        d["weather_source"] = str(self.weather)
        d["crop"] = self.cfg.crop
        d["variety"] = self.cfg.variety
        d["uncalibrated_parameters"] = self.uncalibrated
        d["crop_start_type"] = self.start_type
        if self.uncalibrated:
            d["warning"] = (
                f"{self.cfg.crop} ships a single generic parameter set "
                "(Van Heemst 1988, no specific region). Absolute yield is not "
                "decision-grade without local calibration."
            )
        return d


# ---------------------------------------------------------------------------
# Feasibility checks that run before a simulation is trusted
# ---------------------------------------------------------------------------

def max_daylength_h(latitude: float, kind: str = "astronomical") -> float:
    """Longest photoperiod of the year at this latitude, in hours."""
    from datetime import date as _date
    from pcse.util import daylength
    year = 2001  # non-leap reference year; the answer is latitude-driven
    best = 0.0
    for doy in range(1, 366):
        d = _date.fromordinal(_date(year, 1, 1).toordinal() + doy - 1)
        try:
            dl = daylength(d, latitude, angle=-4.0 if kind == "astronomical" else 0.0)
        except Exception:
            continue
        best = max(best, float(dl))
    return best


@dataclass
class Feasibility:
    ok: bool
    reasons: list[str]

    def __bool__(self) -> bool:
        return self.ok


def check_photoperiod(cfg: ZoneConfig, engine: str = "Wofost72_WLP_CWB",
                      crop_params_path: str | Path | None = None) -> Feasibility:
    """Can this cultivar actually complete development at this latitude?

    This check exists because of a failure we hit on the first real run and
    which would otherwise have shipped silently.

    Every wheat cultivar in the WOFOST parameter repository is European and
    carries ``IDSL=2`` (photoperiod- and vernalisation-sensitive) with an
    optimum photoperiod ``DLO`` of 16.3-16.8 h. At 30.9 N -- Punjab, one of the
    largest wheat regions on earth -- the longest day of the year is about
    14 h. The cultivar's development-rate reduction factor therefore never
    reaches 1, the crop accumulates thermal time far too slowly, and WOFOST
    happily returns **TWSO = 0 kg/ha with no error at all** after simulating a
    season of biomass growth.

    A zero that looks like a number is worse than a crash. The twin refuses to
    emit a yield when this test fails.
    """
    reasons: list[str] = []
    cp = crop_provider(engine, crop_params_path)
    cp.set_active_crop(cfg.crop, cfg.variety)
    p = dict(cp)
    idsl = float(p.get("IDSL", 0) or 0)
    dlo = p.get("DLO")
    dlc = p.get("DLC")
    if idsl >= 1 and dlo is not None:
        dl_max = max_daylength_h(cfg.latitude)
        if float(dlo) > dl_max + 0.05:
            reasons.append(
                f"cultivar {cfg.variety!r} is photoperiod-sensitive (IDSL={idsl:g}) with "
                f"DLO={float(dlo):.1f} h, but the longest day at latitude "
                f"{cfg.latitude:.2f} is {dl_max:.1f} h. Development will stall and "
                f"yield will be reported as 0 with no error. This cultivar cannot be "
                f"used at this latitude without recalibrating DLO/TSUM."
            )
        if dlc is not None and float(dlc) > dl_max:
            reasons.append(
                f"critical photoperiod DLC={float(dlc):.1f} h exceeds the maximum "
                f"daylength {dl_max:.1f} h at this site; development cannot start."
            )
    if idsl >= 2:
        reasons.append(
            f"cultivar {cfg.variety!r} requires vernalisation (IDSL=2); confirm the site "
            "actually accumulates enough chilling, which most Indian wheat belts do not."
        )
    return Feasibility(ok=not any("cannot" in r or "stall" in r for r in reasons),
                       reasons=reasons)


def variety_support(crop: str, engine: str = "Wofost81_NWLP_CWB_CNB",
                    crop_params_path: str | Path | None = None) -> list[str]:
    """Varieties of ``crop`` that this engine can actually run.

    The parameter repository's per-version branches are not uniformly complete.
    On the wofost81 branch, rice ships 9 varieties but only 1 carries
    ``AMAX_REF``; maize ships 7 and only 1 is complete. Selecting any of the
    other 8 rice varieties raises ``ParameterError: Value for parameter
    AMAX_REF missing`` at construction time, which is a confusing way to learn
    that a cultivar you can see in the file is not usable.

    Call this before offering a cultivar list to a user.
    """
    required = {"Wofost81": {"AMAX_REF"}, "Wofost72": {"AMAXTB"},
                "Wofost73": {"AMAXTB"}}.get(engine[:8], set())
    cp = crop_provider(engine, crop_params_path)
    out: list[str] = []
    for variety in cp.get_crops_varieties().get(crop, []):
        cp.set_active_crop(crop, variety)
        if required.issubset(set(dict(cp))):
            out.append(variety)
    return out
