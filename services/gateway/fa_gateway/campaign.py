"""Design and validate the initial camera campaign.

The campaign exists to give the twin something worth forecasting from. Two
benchmarks in this repository set its requirements, and neither is obvious:

* ``bench/forecast_horizon.py`` -- a SINGLE capture buys nothing. Measured: LAI
  error at +30 d was 0.110 with no captures and 0.123 with one. One observation
  pins where the crop is and says nothing about how fast it is developing, and
  over a month it is the development rate that dominates. Two captures start to
  work because two points constrain a slope; three is where the gain plateaus.

* ``bench/act_now_vs_observe_first.py`` -- the campaign only pays if it actually
  tightens the growth-rate parameters below a spread of roughly 0.15. At 0.18 it
  bought nothing at all, because the planner remained too uncertain to justify
  any intervention and correctly recommended none.

Those two results have one shared consequence, and it is the single most
important thing about campaign design:

    **Revisit the same stations. Do not tour the farm.**

Three captures of the same ground spaced across three weeks constrain the
trajectory, which is what tightens the rate parameters. Three captures of three
different parts of the farm constrain spatial variation and tell you nothing
about rates. Identical cost, identical effort, and only the first clears the
threshold that makes the whole exercise worth doing.

This module builds a campaign that satisfies that constraint by construction,
and refuses to emit one that does not.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import numpy as np
from shapely.geometry import Point, Polygon, mapping, shape
from shapely.ops import unary_union


# ---------------------------------------------------------------------------
# How many stations
# ---------------------------------------------------------------------------

def stations_needed(cv: float = 0.20, precision: float = 0.10,
                    confidence: float = 0.90) -> int:
    """Sample size to estimate field-mean LAI to a given relative precision.

    Standard finite-sample formula n = (z * CV / precision)^2. Field LAI in a
    managed cereal crop typically has a spatial CV of 0.15-0.25; at CV 0.20 and
    +/-10% precision with 90% confidence this gives ~11 stations.

    This is the number that makes the *field mean* defensible. It is not the
    number needed to map spatial pattern -- that is a different and much larger
    problem, and it is the drone's job, not the campaign's.
    """
    z = {0.80: 1.282, 0.90: 1.645, 0.95: 1.960}.get(round(confidence, 2), 1.645)
    return max(3, math.ceil((z * cv / precision) ** 2))


# ---------------------------------------------------------------------------
# Where the stations go
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Station:
    """A fixed observation point, revisited every round."""
    id: str
    lon: float
    lat: float
    x_m: float
    y_m: float
    stratum: str

    def as_geojson(self) -> dict:
        return {"type": "Feature",
                "properties": {"station": self.id, "stratum": self.stratum},
                "geometry": mapping(Point(self.lon, self.lat))}


def place_stations(field_geom, n: int, crs=None, edge_buffer_m: float = 15.0,
                   seed: int = 0) -> list[Station]:
    """Site stations on a stratified grid inside the field.

    Stratified rather than random, because with ~11 points pure random sampling
    routinely leaves whole sections of a field unvisited -- and the campaign's
    job is to represent the field, not to be statistically fashionable.

    ``edge_buffer_m`` keeps stations off headlands, where compaction, turning
    damage and edge effects make the crop unrepresentative of the field.
    """
    from fa_core.geo import to_local, to_wgs84

    geom = shape(field_geom) if isinstance(field_geom, dict) else field_geom
    local, crs = to_local(geom) if crs is None else (geom, crs)
    inner = local.buffer(-edge_buffer_m)
    if inner.is_empty:
        inner = local
    minx, miny, maxx, maxy = inner.bounds

    # Grid shaped to the field's aspect ratio so cells stay near-square.
    aspect = (maxx - minx) / max(maxy - miny, 1e-9)
    ny = max(1, int(round(math.sqrt(n / max(aspect, 1e-9)))))
    nx = max(1, math.ceil(n / ny))

    rng = np.random.default_rng(seed)
    out: list[Station] = []
    for j in range(ny):
        for i in range(nx):
            if len(out) >= n:
                break
            cx = minx + (i + 0.5) * (maxx - minx) / nx
            cy = miny + (j + 0.5) * (maxy - miny) / ny
            # Jitter within the cell, then pull back inside if it lands out.
            for _ in range(30):
                px = cx + rng.uniform(-0.3, 0.3) * (maxx - minx) / nx
                py = cy + rng.uniform(-0.3, 0.3) * (maxy - miny) / ny
                if inner.covers(Point(px, py)):
                    break
            else:
                px, py = cx, cy
                if not inner.covers(Point(px, py)):
                    p = inner.representative_point()
                    px, py = p.x, p.y
            lonlat = to_wgs84(Point(px, py), crs)
            out.append(Station(id=f"S{len(out) + 1:02d}", lon=lonlat.x, lat=lonlat.y,
                               x_m=px, y_m=py, stratum=f"r{j + 1}c{i + 1}"))
    return out


# ---------------------------------------------------------------------------
# When to visit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Visit:
    round_no: int
    planned_date: date
    window_start: time
    window_end: time
    note: str = ""


def schedule_visits(start: date, n_rounds: int = 3, span_days: int = 21,
                    window=(time(11, 0), time(14, 0))) -> list[Visit]:
    """Evenly spaced revisits across the campaign window.

    The 11:00-14:00 window is not arbitrary. Published UAV work over winter
    wheat found that flights inside it gave prescription-map overlap above 80%
    between repeat acquisitions, while an 08:00 acquisition collapsed to 56.6%
    and a 17:00 one to 44.6-70.9%. Low sun elevation changes the sunlit/shaded
    canopy mix so much that it swamps the difference between good and bad
    vegetation indices. Holding the time-of-day window is free and it is the
    highest-return discipline in the whole protocol.
    """
    if n_rounds < 2:
        raise ValueError(
            "a campaign of fewer than 2 rounds cannot constrain the crop's "
            "development rate; bench/forecast_horizon.py measures a single "
            "capture as no better than none")
    step = span_days / (n_rounds - 1)
    return [Visit(i + 1, start + timedelta(days=int(round(i * step))),
                  window[0], window[1],
                  "baseline" if i == 0 else ("final" if i == n_rounds - 1 else "mid"))
            for i in range(n_rounds)]


# ---------------------------------------------------------------------------
# What to capture at each station
# ---------------------------------------------------------------------------

CAPTURE_PROTOCOL = [
    ("panel_before", "Calibrated reference panel filling >=30% of frame, "
                     "same distance every time, camera not shadowing it"),
    ("nadir_canopy", "Camera looking straight down at canopy from fixed height "
                     "(1.5-2.0 m), monopod or tripod arm, same height every visit"),
    ("oblique_row", "45 deg oblique along the crop row, for canopy structure and "
                    "to see leaf undersides where many diseases start"),
    ("closeup_leaf", "Macro of 3-5 representative leaves at ~0.3 m, for lesion-scale "
                     "detail the drone can never resolve"),
    ("wide_context", "Wide shot showing the station marker and surroundings, so a "
                     "reviewer can tell later whether the sample was representative"),
    ("panel_after", "Reference panel again -- brackets the station's illumination "
                    "and catches a cloud passing mid-capture"),
]


@dataclass
class CampaignPlan:
    field_geoid: str
    stations: list[Station]
    visits: list[Visit]
    crop: str
    variety: str
    area_ha: float
    captures_per_station: int = len(CAPTURE_PROTOCOL)

    @property
    def total_captures(self) -> int:
        return len(self.stations) * len(self.visits) * self.captures_per_station

    def estimated_field_time_min(self, min_per_station: float = 4.0,
                                 walk_speed_ms: float = 1.1) -> float:
        """Rough time per visit: walking the stations plus dwell at each."""
        pts = [(s.x_m, s.y_m) for s in self.stations]
        walk = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        return len(self.stations) * min_per_station + walk / walk_speed_ms / 60.0

    def validate(self) -> list[str]:
        """Refuse a campaign that cannot meet the twin's requirements.

        Each check corresponds to a measured failure, not a style preference.
        """
        problems: list[str] = []
        if len(self.visits) < 2:
            problems.append(
                "fewer than 2 rounds: a single capture measured no better than "
                "no capture at all (bench/forecast_horizon.py)")
        if len(self.visits) >= 2:
            span = (self.visits[-1].planned_date - self.visits[0].planned_date).days
            if span < 12:
                problems.append(
                    f"campaign spans only {span} days: too short to resolve a "
                    "development rate against observation noise; aim for ~21")
        ids = [s.id for s in self.stations]
        if len(set(ids)) != len(ids):
            problems.append("duplicate station ids")
        if len(self.stations) < 3:
            problems.append("fewer than 3 stations cannot represent field variability")
        return problems

    def as_geojson(self) -> dict:
        return {"type": "FeatureCollection",
                "features": [s.as_geojson() for s in self.stations]}

    def field_sheet(self) -> str:
        """A printable sheet for whoever actually walks the field."""
        L: list[str] = []
        L.append(f"FUTURE AGRI - INITIAL CAMERA CAMPAIGN")
        L.append(f"field {self.field_geoid}   {self.area_ha:.2f} ha   "
                 f"{self.crop}/{self.variety}")
        L.append("=" * 72)
        L.append("")
        L.append("NON-NEGOTIABLE SETTINGS -- set once, never change mid-campaign:")
        L.append("  [ ] Exposure: MANUAL and FIXED. Auto-exposure dropped a published")
        L.append("      nitrogen retrieval from R2 0.40 to 0.13. Note the value here: ______")
        L.append("  [ ] White balance: MANUAL and FIXED.  Value: ______")
        L.append("  [ ] Focus: fixed. Note it. Refocusing mid-season breaks comparability.")
        L.append("  [ ] Camera height and tilt: measure and record. Same every visit.")
        L.append("  [ ] Time window: 11:00-14:00 local STANDARD time only.")
        L.append("  [ ] Skip the visit if the sky is broken/popcorn cloud. Reschedule.")
        L.append("      Uniform overcast is acceptable; patchy sun is not.")
        L.append("")
        L.append("REFERENCE PANEL:")
        L.append("  [ ] Two panels. One travels and is used every capture; one stays")
        L.append("      COVERED and is exposed only at visit 1 and the final visit.")
        L.append("      The covered one measures how much the working panel degraded.")
        L.append("      Without it you are guessing at your own calibration drift.")
        L.append("")
        L.append("VISITS:")
        for v in self.visits:
            L.append(f"  Round {v.round_no} ({v.note}): {v.planned_date:%a %d %b %Y}  "
                     f"{v.window_start:%H:%M}-{v.window_end:%H:%M}   date done: ________")
        L.append("")
        L.append(f"STATIONS ({len(self.stations)}) -- VISIT THE SAME ONES EVERY ROUND.")
        L.append("  Mark each permanently (painted stake / driven rod + flagging tape).")
        L.append("  Revisiting the same ground is what makes the campaign work at all;")
        L.append("  sampling new spots each round constrains nothing about growth rate.")
        L.append("")
        L.append(f"  {'id':<5s} {'latitude':>11s} {'longitude':>11s}  r1  r2  r3   notes")
        for s in self.stations:
            L.append(f"  {s.id:<5s} {s.lat:11.6f} {s.lon:11.6f}  [ ] [ ] [ ]   ____________")
        L.append("")
        L.append(f"AT EACH STATION ({self.captures_per_station} captures):")
        for i, (name, desc) in enumerate(CAPTURE_PROTOCOL, 1):
            L.append(f"  {i}. {name:<14s} {desc}")
        L.append("")
        L.append("ALSO RECORD, EVERY VISIT:")
        L.append("  [ ] Growth stage (BBCH or local scale) at each station")
        L.append("  [ ] Any operation since last visit: irrigation, fertiliser, spray")
        L.append("      -- with dates and rates. The twin cannot infer these and will")
        L.append("      attribute their effects to the wrong cause if you omit them.")
        L.append("  [ ] Rainfall since last visit, if a gauge is on site")
        L.append("  [ ] Anything visibly wrong: lodging, pest damage, waterlogging")
        L.append("")
        L.append(f"TOTAL: {self.total_captures} captures over {len(self.visits)} visits, "
                 f"~{self.estimated_field_time_min():.0f} min per visit")
        return "\n".join(L)


def build_campaign(field_geom, geoid: str, crop: str, variety: str,
                   start: date, n_stations: int | None = None,
                   n_rounds: int = 3, span_days: int = 21,
                   cv: float = 0.20, precision: float = 0.10) -> CampaignPlan:
    from fa_core.geo import area_ha

    n = n_stations or stations_needed(cv, precision)
    stations = place_stations(field_geom, n)
    visits = schedule_visits(start, n_rounds, span_days)
    return CampaignPlan(field_geoid=geoid, stations=stations, visits=visits,
                        crop=crop, variety=variety, area_ha=area_ha(field_geom))
