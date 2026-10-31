"""Domain model.

Adopted, not invented. The structure is farmOS's (Asset / Log / Quantity /
Term / Plan), which is the only open agricultural record model that has been
stress-tested by real farms across livestock, row crop and research use. Two of
its ideas are load-bearing here and worth stating explicitly:

1. **Asset vs Log.** An Asset is a durable thing (a field, a crop planting, a
   drone). A Log is something that happened, at a time, with geometry. Every log
   distinguishes what it happened *to* (``asset_ids``) from where it happened
   *in* (``location_ids``). Spraying plot 3 of field 12 is one log referencing
   the planting asset and the field location -- not a bespoke "spray" table.

2. **Log status.** ``pending`` / ``done`` / ``abandoned`` gives us planned vs
   executed missions for free. A prescription is a pending input log; the
   as-applied record is the same log moved to done with measured quantities. No
   second schema, and the plan/actual comparison is a single query.

The land hierarchy (Farm -> Site -> Parcel -> ManagementZone) comes from
OpenAgri's OCSM. ManagementZone is the prescription unit.

Everything is append-only and versioned: rows are never updated in place. This
is what makes edge/cloud sync tractable (see docs/adr/0006) and it is a hard
requirement for an as-applied audit trail anyone would defend in an argument
about a misapplication.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class AssetType(StrEnum):
    land = "land"              # a field or parcel
    plant = "plant"            # a crop planting (a season's crop in a field)
    equipment = "equipment"    # drone, gateway, sprayer
    sensor = "sensor"          # a fixed sensor
    material = "material"      # a product: fertiliser, pesticide, seed
    structure = "structure"


class LogType(StrEnum):
    observation = "observation"    # a flight, a scouting note, a sensor summary
    input = "input"                # an application: spray, fertiliser, irrigation
    seeding = "seeding"
    transplanting = "transplanting"
    harvest = "harvest"
    activity = "activity"
    lab_test = "lab_test"
    maintenance = "maintenance"


class LogStatus(StrEnum):
    pending = "pending"        # planned; a prescription lives here
    done = "done"              # executed; as-applied data attached
    abandoned = "abandoned"    # planned then cancelled -- kept, never deleted


class Measure(StrEnum):
    area = "area"
    volume = "volume"
    mass = "mass"
    count = "count"
    ratio = "ratio"
    rate = "rate"
    length = "length"
    time = "time"
    temperature = "temperature"
    energy = "energy"


class Quantity(BaseModel):
    """A measured or prescribed number with its unit and role.

    ``label`` carries the agronomic role ("applied", "prescribed", "observed"),
    which is what lets one log hold both sides of a plan/actual comparison.
    """
    model_config = ConfigDict(extra="forbid")

    measure: Measure
    value: float
    unit: str
    label: str = ""
    material_id: str | None = None   # which product this quantity refers to

    @field_validator("unit")
    @classmethod
    def _unit_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("unit is required; a bare number is not a quantity")
        return v


class Asset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_uuid)
    type: AssetType
    name: str
    geoid: str | None = None            # for land assets: content-addressed identity
    geometry: dict[str, Any] | None = None   # GeoJSON, EPSG:4326
    parent_id: str | None = None        # Farm -> Site -> Parcel -> ManagementZone
    status: Literal["active", "archived"] = "active"
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


class Log(BaseModel):
    """Something that happened (or is planned to happen)."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_uuid)
    type: LogType
    name: str = ""
    timestamp: datetime = Field(default_factory=_now)
    status: LogStatus = LogStatus.pending
    asset_ids: list[str] = Field(default_factory=list)      # happened TO
    location_ids: list[str] = Field(default_factory=list)   # happened IN
    geometry: dict[str, Any] | None = None
    quantities: list[Quantity] = Field(default_factory=list)
    notes: str = ""
    # Provenance. Non-negotiable: every recommendation must be attributable to
    # the model version that produced it, or post-hoc "why did we spray that"
    # is unanswerable.
    source: str = ""                   # "planner", "agronomist:asha", "gateway:fa-gw-01"
    model_version: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Version(BaseModel):
    """One immutable version of a mutable object.

    Mutable state (prescriptions, field boundaries, agronomist annotations) is
    stored as an append-only chain rather than updated in place, so edge/cloud
    sync degenerates to append-only replication -- the same trivially-mergeable
    problem as sensor data. Two versions sharing a ``parent_version`` is a
    detected conflict, surfaced to a human rather than silently auto-merged.
    Merging two fertiliser prescriptions by algorithm produces a document that
    converges and is agronomically meaningless.
    """
    model_config = ConfigDict(extra="forbid")

    object_id: str
    version_id: str = Field(default_factory=_uuid)
    parent_version: str | None = None
    author: str
    device_id: str = "cloud"
    created_at: datetime = Field(default_factory=_now)
    payload: dict[str, Any]


class Observation(BaseModel):
    """A single derived-product observation from a flight or sensor.

    This is the unit that crosses the uplink. Full-resolution imagery stays at
    the edge; what travels is this. The conformal fields are what make an
    observation actionable or reviewable -- see fa_core.uncertainty.
    """
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_uuid)
    zone_id: str
    observed_at: datetime
    variable: str                      # "lai", "canopy_cover", "weed_density", "ndre"
    value: float
    unit: str
    # Calibrated uncertainty, not a softmax score.
    lo: float | None = None
    hi: float | None = None
    coverage: float | None = None      # nominal coverage of [lo, hi], e.g. 0.90
    label_set: list[str] | None = None # conformal class set; len != 1 -> review
    needs_review: bool = False
    source: str = ""
    model_version: str | None = None
    n_pixels: int | None = None


class ManagementZone(BaseModel):
    """The unit a prescription is written against."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_uuid)
    parcel_id: str
    name: str
    geometry: dict[str, Any]
    area_ha: float
    soil: dict[str, float] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)


class PrescriptionCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_id: str
    product_id: str
    rate: float
    unit: str = "L/ha"


class Prescription(BaseModel):
    """A plan to apply something, constrained to what is legally applicable.

    ``label_ok`` is set by the compliance check, not by the planner. A
    prescription that has not passed it must never reach a mission.
    """
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_uuid)
    parcel_id: str
    created_at: datetime = Field(default_factory=_now)
    valid_from: date
    valid_to: date
    cells: list[PrescriptionCell]
    label_ok: bool = False
    rationale: str = ""
    source: str = "planner"
    model_version: str | None = None
    expected_value: dict[str, float] = Field(default_factory=dict)
