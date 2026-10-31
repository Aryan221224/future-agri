"""PX4 SITL mission execution over MAVSDK.

Verified stack pairing (this combination is the one PX4's own installer
produces; the pieces are not interchangeable):

    Ubuntu 24.04 LTS + PX4-Autopilot v1.17.0 + Gazebo Harmonic (gz-sim8)
    mavsdk==3.17.2

Three things bite everyone and are handled or flagged here:

1. **The connection URL scheme changed.** It is ``udpin://0.0.0.0:14540``. The
   older ``udp://:14540`` form is gone and fails with a confusing error.
2. **Gazebo's camera sensor is not wired to the MAVLink camera protocol.** A
   mission ``CameraAction`` fires the mission item but produces no image. Frames
   must be captured off the Gazebo Transport topic and stamped with an
   interpolated vehicle pose, and EXIF GPS written, or photogrammetry has
   nothing to georeference. ``capture_from_gz`` is where that belongs.
3. **``mission_progress`` does not fire for missions uploaded by a different
   ground station.** Upload from the same client that monitors.

``START_PHOTO_DISTANCE`` with ``camera_photo_distance_m`` is used rather than a
time interval, because distance triggering holds forward overlap constant
regardless of groundspeed and wind. Interval triggering does not, and the
resulting overlap variation is a common cause of failed reconstructions.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_URL = "udpin://0.0.0.0:14540"


@dataclass
class MissionSpec:
    waypoints: list[tuple[float, float]]      # (lat, lon)
    altitude_m: float
    speed_ms: float
    photo_distance_m: float


def swaths_to_waypoints(sortie, to_wgs84_fn) -> list[tuple[float, float]]:
    """Projected swath endpoints -> (lat, lon) waypoints, in flight order."""
    out: list[tuple[float, float]] = []
    for sw in sortie.swaths:
        for pt in (sw.start, sw.end):
            lon, lat = to_wgs84_fn(pt[0], pt[1])
            out.append((lat, lon))
    return out


async def run_mission(spec: MissionSpec, url: str = DEFAULT_URL,
                      timeout_s: float = 900.0) -> dict:  # pragma: no cover
    """Upload and fly one sortie in SITL, returning a flight record."""
    from mavsdk import System
    from mavsdk.mission import MissionItem, MissionPlan

    drone = System()
    await drone.connect(system_address=url)

    async for state in drone.core.connection_state():
        if state.is_connected:
            break
    async for health in drone.telemetry.health():
        if health.is_global_position_ok and health.is_home_position_ok:
            break

    items = []
    n = len(spec.waypoints)
    for i, (lat, lon) in enumerate(spec.waypoints):
        if i == 0:
            action = MissionItem.CameraAction.START_PHOTO_DISTANCE
        elif i == n - 1:
            action = MissionItem.CameraAction.STOP_PHOTO_DISTANCE
        else:
            action = MissionItem.CameraAction.NONE
        items.append(MissionItem(
            lat, lon, spec.altitude_m, spec.speed_ms, True,
            0.0, float("nan"), action, float("nan"), float("nan"),
            2.0, float("nan"), spec.photo_distance_m,
            MissionItem.VehicleAction.NONE))

    await drone.mission.set_return_to_launch_after_mission(True)
    await drone.mission.upload_mission(MissionPlan(items))
    await drone.action.arm()
    await drone.mission.start_mission()

    record = {"waypoints": n, "progress": [], "battery": []}

    async def watch_battery():
        async for b in drone.telemetry.battery():
            record["battery"].append(b.remaining_percent)

    task = asyncio.ensure_future(watch_battery())
    try:
        async for p in asyncio.wait_for(_aiter(drone.mission.mission_progress()),
                                        timeout=timeout_s):
            record["progress"].append((p.current, p.total))
            if p.total and p.current == p.total:
                break
    finally:
        task.cancel()

    await drone.action.return_to_launch()
    record["completed"] = True
    return record


async def _aiter(gen):  # pragma: no cover
    async for x in gen:
        yield x


def capture_from_gz(topic: str, out_dir: str) -> None:  # pragma: no cover
    """Subscribe to a Gazebo camera topic and write pose-stamped frames.

    Not implemented here because it needs the Gazebo Transport Python bindings
    present. Raising is deliberate: silently returning nothing would make a
    SITL run look like it produced imagery when it produced none, which is
    exactly the class of failure this codebase tries not to have.
    """
    raise NotImplementedError(
        "Gazebo frame capture requires gz-transport bindings on the SITL host. "
        "The MAVLink camera protocol is NOT connected to the gz camera sensor, "
        "so mission CameraActions alone produce no images -- see docs/adr/0007.")
