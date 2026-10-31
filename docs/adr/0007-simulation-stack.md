# 0007 — PX4 v1.17 + Gazebo Harmonic on Ubuntu 24.04; no ROS 2

**Status:** accepted · **Date:** 2026-09-09

## The pinning, and why it is not negotiable

```
Ubuntu 24.04 LTS  +  PX4-Autopilot v1.17.0  +  Gazebo Harmonic (gz-sim8)  +  mavsdk 3.17.2
```

This is not a preference — it is what PX4's own `Tools/setup/ubuntu.sh` installs
on `release/1.17`. There is no code path in that installer that installs Ionic
or Jetty. PX4 `main` *can* build against them via a `GZ_DISTRO` env var, but
that is an escape hatch, not a supported path.

Three traps:

* **Gazebo Ionic reaches EOL in December 2026.** Picking "the newest Gazebo"
  chooses a dead runtime. Harmonic is LTS to May 2029.
* **Ubuntu 25.10 is a hard stop** — the installer explicitly skips simulator
  installation.
* Pin the OSRF apt repo. One `apt upgrade` pulling Ionic breaks the build with a
  missing `gz-transport13`.

## No ROS 2

The strongest evidence is PX4's own documentation, which still recommends
**ROS 2 Humble on Ubuntu 22.04** — two LTS releases behind. So the options are
building a 2026 system on 2022 infrastructure, or going off PX4's supported path
and owning `px4_msgs` branch-matching.

And ROS 2 buys nothing on our critical path: generate a coverage path, upload a
mission, fly it, collect frames, run photogrammetry. Mission upload is MAVSDK
over MAVLink/UDP. Frame capture is a Gazebo Transport subscription. ODM is a
container with a REST API. ROS 2 would add colcon, a message-versioning
translation node, a distro/Ubuntu constraint and DDS discovery — which is
painful in containers — for no capability we need.

**The decision is cheap to reverse.** Harmonic and ROS 2 Jazzy both EOL in May
2029 and Jazzy's Tier 1 platform is Ubuntu 24.04 with binary `ros_gz` for
Harmonic. The day we need Nav2 or `opennav_coverage`, it is `apt install
ros-jazzy-ros-gz` on the identical host. That reversibility is the reason to
skip it now rather than hedge.

## The undocumented part that will cost the most time

**Gazebo's camera sensor is not connected to the MAVLink camera protocol.** A
mission `CameraAction` fires the mission item and produces **no image**. Frames
must be captured off the Gazebo Transport topic, stamped with an interpolated
vehicle pose, and written with EXIF GPS — or ODM has nothing to georeference.
`fa_sim.px4.capture_from_gz` raises rather than returning nothing, because a
SITL run that looks like it produced imagery and did not is worse than a crash.

The stock `mono_cam` is also 1.2 MP at ~100° HFOV with no distortion or noise
model. A real survey camera is 20 MP at 70–84°. Fork the SDF, or photogrammetry
is solving a trivially clean problem and the results will not transfer.

## Kinematic simulator alongside SITL

`fa_sim.kinematic` is a dependency-free multirotor model integrating position at
commanded speed with the router's energy model. It is not a flight-dynamics
simulator and the code says so. It exists so the **whole** loop — coverage,
sortie decomposition, capture, inference, gating, assimilation, replanning —
runs in CI and on any laptop in ~40 s, before hardware exists. Anything
depending on aerodynamics, wind or control dynamics must be tested in SITL.

## Coverage: Fields2Cover when present, Shapely fallback always

Fields2Cover 2.1.0 (BSD-3, released 2026-09-03) is the right library and is used
when importable. But it is an **sdist only** — `pip install` compiles the whole
C++ library against GDAL, GEOS and OR-Tools C++, which is the most fragile step
in the stack. Pin it in a Docker layer. The Shapely fallback implements the same
boustrophedon decomposition so the system never hard-fails on a build problem.

Note F2C 2.1.0 fixed a real correctness bug in `NSwathModified::computeCost`
(`(i-1) % ring.size()` on a `size_t` wraps to `SIZE_MAX % n`), so a polygon's
cost depended on which vertex its ring started from. Do not use 2.0.0.

**What F2C cannot do in any version: energy.** Its `Robot` type has width, speed
and turning radius, and no battery, capacity or refill field anywhere. Since an
agricultural multirotor flies 15–40 min and a real field needs hours, *every*
mission is multi-sortie. That decomposition is ours — `fa_planner.routing`.
