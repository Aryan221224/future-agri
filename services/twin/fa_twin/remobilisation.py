"""Stem-reserve remobilisation to the grain under drought (maize), for WOFOST 7.2.

WOFOST 7.2 fills grain only from the day's assimilate: when drought cuts photosynthesis during
grain fill, grain growth falls with it and the sugars stored in the stem stay there. Maize does
not behave like that. CERES-Maize (DSSAT, ``MZ_GROSUB.for``) and Hybrid-Maize (Yang et al. 2004,
Field Crops Res. 87:131-154, which adopts it) draw the stem down when the grain's daily demand
exceeds the assimilate supply, to a floor of 85 % of the stem weight at silking:

    SWMIN  = STMWT * 0.85                        (at silking)
    GROGRN = RGFILL * GPP * G3 * 0.001 * (0.45 + 0.55 * SWFAC)
    if CARBO < GROGRN:  STMWT = max(STMWT + CARBO - GROGRN, SWMIN)

That rule is carried over unchanged, with one translation: WOFOST has no kernel number, so the
grain's demand is the grain growth WOFOST itself would have made without water stress (the
storage-organ share of potential net assimilation), cut by CERES' own water-stress sink factor
``0.45 + 0.55 * RFTRA``. The shortfall below that demand moves from living stem to grain, one
for one in dry matter as in CERES, until the stem reaches its floor.

Nothing here is fitted. Without water stress (RFTRA = 1) the demand equals the actual grain
growth and nothing moves, so irrigated and potential-production runs are unchanged. CERES also
lets 0.5 %/day of leaf weight go to the grain once the stem is near its floor; that small term
is left out.

The two reallocation rates WOFOST 7.2 already declares (REALLOC_ST, REALLOC_SO, "not applicable
in WOFOST72", always 0) carry the transfer, so stem and storage organ integrate it through
their own code and the mass balance holds: TAGP does not change, TWSO rises, TWST falls.
"""
from __future__ import annotations

from pcse.crop.wofost72 import Wofost72
from pcse.traitlets import Float

STEM_FLOOR = 0.85        # CERES-Maize SWMIN / STMWT at silking
SINK_BASE = 0.45         # CERES-Maize grain demand under water stress: 0.45 + 0.55 * SWFAC
SILKING_DVS = 1.0        # WOFOST anthesis


_PARENT_CALC_RATES = Wofost72.__dict__["calc_rates"].f

#: kg/ha remobilised per running model, keyed by id(kiosk): PCSE drops the crop object at harvest.
TOTALS: dict = {}


def remobilised(model) -> float | None:
    """Stem dry matter moved to the grain so far (kg/ha), or None if the model does not remobilise."""
    return TOTALS.get(id(getattr(model, "kiosk", None)))


class Wofost72Remob(Wofost72):
    """WOFOST 7.2 crop with CERES-Maize stem-reserve remobilisation under water stress."""

    stem_floor = Float(-1.0)            # kg/ha, set at silking (-1 = not yet)
    remobilised = Float(0.0)            # kg/ha moved stem -> grain this season

    def initialize(self, day, kiosk, parvalues):
        super().initialize(day, kiosk, parvalues)
        self.stem_floor = -1.0
        self.remobilised = 0.0
        TOTALS[id(kiosk)] = 0.0

    def calc_rates(self, day, drv):
        # Not super(): PCSE's @prepare_rates descriptor rebinds ``calc_rates`` on the instance at
        # first access, which would bypass this override from the second day on.
        self.rates.unlock()
        try:
            _PARENT_CALC_RATES(self, day, drv)
        finally:
            self.rates.lock()
        if self.pheno.get_variable("STAGE") == "emerging":
            return
        k = self.kiosk
        dvs = k["DVS"]
        wst = self.st_dynamics.states.WST
        if dvs < SILKING_DVS:
            return
        if self.stem_floor < 0:
            self.stem_floor = STEM_FLOOR * wst
        rf = float(k["RFTRA"])
        if rf >= 1.0 or dvs >= 2.0:
            return
        transfer = min(max(0.0, self._grain_demand(day, drv, rf) - self.so_dynamics.rates.GRSO),
                       max(0.0, wst + self.st_dynamics.rates.GWST - self.stem_floor))  # stem after today's own growth
        if transfer <= 0.0:
            return
        self.remobilised += transfer
        TOTALS[id(k)] = self.remobilised
        for obj, name, delta in ((self, "REALLOC_ST", transfer), (self, "REALLOC_SO", transfer),
                                 (self.st_dynamics, "GWST", -transfer), (self.so_dynamics, "GWSO", transfer)):
            rates = obj.rates
            rates.unlock()
            setattr(rates, name, getattr(rates, name) + delta)
            rates.lock()

    def _grain_demand(self, day, drv, rf: float) -> float:
        """Grain growth without water stress, cut by the CERES sink factor (kg DM/ha/d)."""
        p = self.params
        pgass = self._potential_gass(day, drv)
        pmres = min(pgass, self.mres(day, drv))
        pf = self.part.calc_rates(day, drv)
        cvf = 1.0 / ((pf.FL / p.CVL + pf.FS / p.CVS + pf.FO / p.CVO) * (1.0 - pf.FR) + pf.FR / p.CVR)
        potential_grso = cvf * (pgass - pmres) * (1.0 - pf.FR) * pf.FO
        return potential_grso * (SINK_BASE + (1.0 - SINK_BASE) * rf)

    def _potential_gass(self, day, drv) -> float:
        """WOFOST72_Assimilation.__call__ without its side effect: the parent's call today has
        already pushed TMIN into the 7-day buffer, so it is read, not pushed again."""
        from pcse.crop.assimilation import totass7
        from pcse.util import astro
        a = self.assim
        ap, k = a.params, self.kiosk
        tminra = sum(a._TMNSAV) / len(a._TMNSAV)
        DAYL, DAYLP, SINLD, COSLD, DIFPP, ATMTR, DSINBE, ANGOT = astro(day, drv.LAT, drv.IRRAD)
        amax = ap.AMAXTB(k.DVS) * ap.TMPFTB(drv.DTEMP)
        dtga = totass7(DAYL, amax, ap.EFFTB(drv.DTEMP), k.LAI, ap.KDIFTB(k.DVS), drv.IRRAD, DIFPP, DSINBE,
                       SINLD, COSLD)
        return dtga * ap.TMNFTB(tminra) * 30.0 / 44.0


def engine_class(base: str):
    """A PCSE Engine running ``base``'s soil and agromanagement with the remobilising crop."""
    from pcse import models
    from pcse.engine import Engine
    import os
    import tempfile
    if base != "Wofost72_WLP_CWB":
        return getattr(models, base)
    if base not in _CLASSES:
        here = os.path.join(tempfile.gettempdir(), "fa_twin_Wofost72_WLP_CWB_REMOB.conf")
        with open(here, "w") as f:
            f.write(_CONF)
        _CLASSES[base] = type("Wofost72_WLP_CWB_REMOB", (Engine,), {"config": here})
    return _CLASSES[base]


_CLASSES: dict = {}
_CONF = '''
from pcse.soil.classic_waterbalance import WaterbalanceFD
from pcse.agromanager import AgroManager
from fa_twin.remobilisation import Wofost72Remob
SOIL = WaterbalanceFD
CROP = Wofost72Remob
AGROMANAGEMENT = AgroManager
OUTPUT_VARS = ["DVS", "LAI", "TAGP", "TWSO", "TWLV", "TWST", "TWRT", "TRA", "RD", "SM", "WWLOW", "RFTRA"]
OUTPUT_INTERVAL = "daily"
OUTPUT_INTERVAL_DAYS = 1
OUTPUT_WEEKDAY = 0
SUMMARY_OUTPUT_VARS = ["DVS", "LAIMAX", "TAGP", "TWSO", "TWLV", "TWST", "TWRT", "CTRAT", "RD", "DOS", "DOE", "DOA",
                       "DOM", "DOH", "DOV", "CEVST"]
TERMINAL_OUTPUT_VARS = ["WTRAT", "EVST", "EVWT", "TSR", "RAINT", "TOTINF", "TOTIRR", "PERCT", "LOSST"]
'''
