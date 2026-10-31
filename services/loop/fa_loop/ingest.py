"""One field pass -> what the twin needs.

The audit settled what one pass at day 30-45 is for: **canopy cover and growth
stage** (they cut the 30-day canopy forecast error by a third to a half) and
**detection** (the weed-density map that decides herbicide). It is not a yield
measurement, and it is a *sampled* walk -- about 240 frames for a 12-plot trial
-- not a full map: mapping 25 ha at weed resolution would be ~21,900 frames.

Inputs are the sampled stations' nadir photos (fixed exposure, grey card; the
protocol check in ``fa_gateway.phone`` refuses an auto-exposure series) and,
optionally, a detector with an action gate for weed counting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class PassObservation:
    canopy_cover: float
    canopy_cover_sd: float
    growth_stage_dvs: float | None
    weed_density_cells: np.ndarray | None        # weeds/m2 per sampled station
    n_frames: int
    review_items: int = 0
    notes: list[str] = field(default_factory=list)


#: BBCH principal growth stage -> WOFOST DVS for potato (0 = emergence,
#: 1 = tuber initiation, 2 = maturity in Potato_701's convention). Coarse and
#: crop-specific: a field agronomist's staging is the input, this only maps it.
BBCH_TO_DVS_POTATO = {0: 0.0, 1: 0.1, 2: 0.25, 3: 0.5, 4: 1.0, 5: 1.2, 6: 1.35,
                      7: 1.5, 8: 1.8, 9: 2.0}


def bbch_to_dvs(bbch: int, crop: str = "potato") -> float | None:
    if crop != "potato":
        return None
    return BBCH_TO_DVS_POTATO.get(int(bbch) // 10)


def one_pass_from_photos(station_frames: dict[str, list[Path]], gains: dict | None = None,
                         bbch: int | None = None, crop: str = "potato", detector=None,
                         action_gate=None, frame_area_m2: float | None = None) -> PassObservation:
    """Measure canopy cover per station and (optionally) weed density per station."""
    from fa_gateway.phone import load_rgb, measure_subplot

    covers, dens, notes = [], [], []
    n_frames = reviews = 0
    for station, frames in station_frames.items():
        m = measure_subplot(station, list(frames), gains)
        covers.append(m.canopy_cover)
        n_frames += m.n_frames
        if m.note:
            notes.append(f"{station}: {m.note.strip()}")
        if detector is not None and action_gate is not None and frame_area_m2:
            weeds = 0
            for p in frames:
                res = detector(load_rgb(p))
                for det in res.detections:
                    aset = action_gate.predict(det.probs)
                    if aset == ["weed"]:
                        weeds += 1
                    elif len(aset) != 1:
                        reviews += 1
            dens.append(weeds / (frame_area_m2 * max(1, len(frames))))
    cc = np.asarray(covers, float)
    if reviews:
        notes.append(f"{reviews} detections need review (crop/weed unclear); "
                     "weed density is a lower bound until reviewed")
    return PassObservation(
        canopy_cover=float(cc.mean()), canopy_cover_sd=float(cc.std(ddof=1)) if len(cc) > 1 else 0.05,
        growth_stage_dvs=bbch_to_dvs(bbch, crop) if bbch is not None else None,
        weed_density_cells=np.asarray(dens) if dens else None, n_frames=n_frames,
        review_items=reviews, notes=notes)
