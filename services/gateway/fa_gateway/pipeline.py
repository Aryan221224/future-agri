"""The field-edge loop: frames in, calibrated derived products out.

This module is where the architecture's central claim is implemented and where
it can be measured: **the uplink carries derived products, not imagery.**

A survey sortie produces hundreds of 20 MP frames. Sending those to a cloud
service over a rural link is not a bandwidth optimisation problem, it is an
impossibility -- which is why systems designed that way stall at pilot stage. So
inference runs at the edge and what crosses the link is a per-zone summary with
calibrated uncertainty. ``bench/bandwidth.py`` measures the resulting ratio
rather than asserting it.

The second job of this module is the review gate. A finding whose conformal
prediction set is not a singleton goes to a human instead of into an automated
decision. That rule is what makes the difference between a system that
occasionally sprays the wrong thing and one that says "I am not sure about this
patch" -- and it is cheap, because it falls out of the calibration that was
needed anyway.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

BACKGROUND = "background"   # the "not a plant" class of a trained RF-DETR (fa_gateway.rfdetr)


@dataclass
class ZoneSummary:
    """What actually crosses the uplink for one management zone."""
    zone_id: str
    observed_at: str
    n_frames: int
    n_detections: int
    class_counts: dict[str, int]
    density_per_ha: dict[str, float]
    review_count: int
    review_fraction: float
    mean_conformal_set_size: float
    coverage_target: float
    model_version: str
    calibration_stratum: str
    calibrated: bool
    lai: float | None = None
    lai_lo: float | None = None
    lai_hi: float | None = None
    notes: str = ""
    #: Action-level counts (crop / weed) when an ``ActionGate`` routes detections.
    action_counts: dict[str, int] | None = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, separators=(",", ":"))

    def payload_bytes(self) -> int:
        return len(self.to_json().encode())


@dataclass
class ReviewItem:
    """A finding a human must look at.

    Carries the frame reference rather than the frame: the imagery stays at the
    edge until someone actually opens the item, at which point one crop is
    fetched on demand. Pushing every uncertain frame to the cloud would undo the
    whole point of edge inference.
    """
    frame_id: str
    zone_id: str
    bbox: tuple[float, float, float, float]
    label_set: list[str]
    top_class: str
    reason: str
    observed_at: str


class EdgePipeline:
    """Detection -> conformal gate -> zone aggregation."""

    def __init__(self, detector, calibrator=None, classes: list[str] | None = None,
                 crop: str = "unknown", stage: str = "unknown", site: str = "unknown",
                 action_gate=None):
        """``action_gate`` (``fa_core.uncertainty.ActionGate``), when given, decides
        routing on crop / weed / background instead of species: a detection is
        actionable when the gate is sure which of those it is, which is all a
        spray decision needs. Species counts are still reported."""
        self.detector = detector
        self.calibrator = calibrator
        self.action_gate = action_gate
        self.classes = classes or getattr(detector, "classes", [])
        self.crop, self.stage, self.site = crop, stage, site
        self.raw_bytes = 0
        self.frames = 0
        self.total_latency_ms = 0.0

    def _calibration(self):
        if self.calibrator is None:
            return None, False
        return self.calibrator.get(self.crop, self.stage, self.site)

    def process_zone(self, zone_id: str, frames, area_ha: float,
                     observed_at: datetime | None = None,
                     lai_estimates: list[float] | None = None,
                     ) -> tuple[ZoneSummary, list[ReviewItem]]:
        cal, stratum_hit = self._calibration()
        counts: dict[str, int] = {}
        reviews: list[ReviewItem] = []
        set_sizes: list[int] = []
        n_det = 0
        n_frames = 0
        when = (observed_at or datetime.now(timezone.utc)).isoformat()

        action_counts: dict[str, int] = {}
        for image in frames:
            n_frames += 1
            self.frames += 1
            self.raw_bytes += int(np.asarray(image).nbytes)
            res = self.detector(image)
            self.total_latency_ms += res.latency_ms
            for det in res.detections:
                top = self.classes[det.top_class] if self.classes else str(det.top_class)
                if self.action_gate is not None:
                    aset = self.action_gate.predict(det.probs)
                    if aset == [BACKGROUND]:
                        continue
                    n_det += 1
                    if top != BACKGROUND:
                        counts[top] = counts.get(top, 0) + 1
                    set_sizes.append(len(aset))
                    if len(aset) == 1:
                        action_counts[aset[0]] = action_counts.get(aset[0], 0) + 1
                    else:
                        reviews.append(ReviewItem(
                            frame_id=res.frame_id, zone_id=zone_id, bbox=det.bbox,
                            label_set=aset, top_class=top,
                            reason="action unclear: " + " or ".join(aset), observed_at=when))
                    continue
                label_set = None
                if cal is not None and hasattr(cal, "predict_set"):
                    label_set = cal.predict_set(det.probs)
                # A trained RF-DETR reports "background" as a class (see
                # fa_gateway.rfdetr). A detection the gate is sure is not a
                # plant is dropped; one that *might* not be a plant is reviewed.
                if (label_set == [BACKGROUND]) or (label_set is None and top == BACKGROUND):
                    continue
                n_det += 1
                if top != BACKGROUND:
                    counts[top] = counts.get(top, 0) + 1
                if label_set is not None:
                    set_sizes.append(len(label_set))
                    if len(label_set) != 1:
                        reviews.append(ReviewItem(
                            frame_id=res.frame_id, zone_id=zone_id, bbox=det.bbox,
                            label_set=label_set, top_class=top,
                            reason=("out-of-distribution: empty conformal set"
                                    if len(label_set) == 0 else
                                    "may not be a plant: " + ", ".join(label_set)
                                    if BACKGROUND in label_set else
                                    f"ambiguous between {len(label_set)} classes"),
                            observed_at=when))
                else:
                    set_sizes.append(1)

        density = {k: (v / area_ha if area_ha > 0 else float("nan"))
                   for k, v in counts.items()}
        lai = lo = hi = None
        if lai_estimates:
            arr = np.asarray(lai_estimates, float)
            lai = float(arr.mean())
            if cal is not None and hasattr(cal, "interval"):
                lo, hi = cal.interval(lai)

        notes = ""
        if not stratum_hit and self.calibrator is not None:
            notes = (f"no calibration for stratum {self.crop}/{self.stage}/{self.site}; "
                     "fell back to the global quantile and flagged for review")

        summary = ZoneSummary(
            zone_id=zone_id, observed_at=when, n_frames=n_frames, n_detections=n_det,
            class_counts=counts, density_per_ha={k: round(v, 2) for k, v in density.items()},
            review_count=len(reviews),
            review_fraction=(len(reviews) / n_det if n_det else 0.0),
            mean_conformal_set_size=float(np.mean(set_sizes)) if set_sizes else 0.0,
            coverage_target=(1 - cal.alpha) if cal is not None else float("nan"),
            model_version=getattr(self.detector, "model_version", "unknown"),
            calibration_stratum=(f"{self.crop}|{self.stage}|{self.site}"
                                 if stratum_hit else "global"),
            calibrated=stratum_hit, lai=lai, lai_lo=lo, lai_hi=hi, notes=notes,
            action_counts=action_counts if self.action_gate is not None else None,
        )
        if self.action_gate is not None:
            summary.coverage_target = 1 - self.action_gate.cal.alpha
            summary.calibration_stratum = "action"
            summary.calibrated = True
        return summary, reviews

    @property
    def mean_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.frames, 1)
