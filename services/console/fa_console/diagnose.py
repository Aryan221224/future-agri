"""``python -m fa_console.diagnose`` -- can this computer reach the data sources a farm build
uses, by which path, and how fast? Writes a report next to the code (diagnose_log.txt).

Each check has a hard time limit, so a stalled connection shows up as TIMEOUT instead of
a build that never finishes. Python downloads (requests: STAC, NASA POWER) and GDAL
downloads (rasterio /vsicurl: Sentinel-2, Copernicus DEM, SoilGrids) are tested
separately, because they find proxies and certificates in different ways.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import os
import platform
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import _paths  # noqa: F401

LIMIT_S = 60
LAT, LON = 28.466, 77.536          # farmland near Greater Noida; any land point works


def _report_dir() -> Path:
    """The checkout root, or the folder of FutureAgri.exe in a frozen build."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def _check(name: str, fn, lines: list[str]) -> object:
    t0 = time.time()
    ex = cf.ThreadPoolExecutor(1)
    fut = ex.submit(fn)
    try:
        res = fut.result(timeout=LIMIT_S)
        status = "OK"
    except cf.TimeoutError:
        res, status = f"no answer within {LIMIT_S} s", "TIMEOUT"
    except Exception as e:  # noqa: BLE001 -- the point is to report it
        res, status = f"{type(e).__name__}: {e}", "FAIL"
    ex.shutdown(wait=False)
    line = f"{status:8s} {time.time() - t0:6.1f} s  {name}: {str(res)[:300]}"
    print(line, flush=True)
    lines.append(line)
    return res if status == "OK" else None


def speed(argv) -> int:
    """How long does the satellite step take on this computer, and why? Prints each result
    as it arrives (every request has a 30 s limit), in under a minute on a normal link:
    1. the path the build now uses -- Planetary Computer computes field statistics
       server-side (~10 kB a scene); a full 6-month season is timed;
    2. the fallback -- raw image tiles from AWS us-west-2 (~3 MB a scene)."""
    import tempfile

    from shapely.geometry import box

    import fa_farm.sources.satellite as sat
    from fa_farm.sources.base import Cache, session
    lines: list[str] = []

    def say(text: str) -> None:
        print(text, flush=True)
        lines.append(text)
    say(f"Future Agri satellite speed check, {dt.datetime.now().isoformat(timespec='seconds')}")
    say("1. Planetary Computer (what the farm build uses first)")
    poly = box(LON, LAT, LON + 0.0024, LAT + 0.0024)
    end = dt.date.today() - dt.timedelta(days=5)
    start = end - dt.timedelta(days=182)
    t0 = time.time()

    def prog(frac, msg, d=None):
        if d and d.get("done"):
            say(f"   {msg}  ({d['bytes'] / 1e3:.0f} kB, {time.time() - t0:.1f} s)")
    try:
        ser, prov = sat.s2_cover_series(poly, start, end, Cache(Path(tempfile.mkdtemp(prefix="fa_speed_"))),
                                        progress=prog, budget_s=120)
        say(f"   => a 6-month season in {time.time() - t0:.1f} s: {prov.note}")
    except Exception as e:  # noqa: BLE001 -- the point is to report it
        say(f"   => FAILED after {time.time() - t0:.1f} s: {type(e).__name__}: {e}")

    say("2. AWS us-west-2 image tiles (the fallback), 4 slices of 0.5 MB")
    base = ("https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/43/R/GM/2025/11/"
            "S2B_43RGM_20251109_0_L2A/")
    size = 500_000
    for k, name in enumerate(["B02.tif", "B03.tif", "B04.tif", "B08.tif"]):
        t1 = time.time()
        try:
            r = session().get(base + name, headers={"Range": f"bytes={60_000_000 + k * size}-"
                                                             f"{60_000_000 + (k + 1) * size - 1}"}, timeout=30)
            r.raise_for_status()
            dt_s = time.time() - t1
            say(f"   {name}: {len(r.content) / 1e3:.0f} kB in {dt_s:.1f} s = {len(r.content) / 1e3 / dt_s:.0f} kB/s")
        except Exception as e:  # noqa: BLE001
            say(f"   {name}: FAILED after {time.time() - t1:.1f} s: {type(e).__name__}: {e}")
    out = _report_dir() / "diagnose_speed.txt"
    try:
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nreport written to {out}", flush=True)
    except OSError as e:
        print(f"\ncould not write {out}: {e}", flush=True)
    sys.stdout.flush()
    os._exit(0)


def main() -> int:
    if "--speed" in sys.argv:
        return speed(sys.argv)
    import rasterio
    from shapely.geometry import box

    from fa_farm.sources.base import Cache, gdal_env, session
    lines = [f"Future Agri data check, {dt.datetime.now().isoformat(timespec='seconds')}",
             f"python {sys.version.split()[0]} on {platform.platform()}",
             f"rasterio {rasterio.__version__}, GDAL {rasterio.__gdal_version__}",
             "proxy (environment): " + str({k: v for k, v in os.environ.items()
                                            if k.lower() in ("http_proxy", "https_proxy", "no_proxy")} or "none"),
             "proxy (system, as Python sees it): " + str({k: v for k, v in urllib.request.getproxies().items()
                                                          if k in ("http", "https", "no")} or "none"),
             "certificates (environment): " + str({k: os.environ[k] for k in ("CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE",
                                                   "SSL_CERT_FILE") if k in os.environ} or "none"),
             "GDAL settings used by the build: " + str(gdal_env()), ""]
    for ln in lines:
        print(ln, flush=True)
    poly = box(LON, LAT, LON + 0.0009, LAT + 0.0009)
    tmp = Cache(Path(tempfile.mkdtemp(prefix="fa_diag_")))

    _check("Python  -> Earth Search STAC (catalogue)",
           lambda: session().get("https://earth-search.aws.element84.com/v1", timeout=30).status_code, lines)

    def search():
        from pystac_client import Client
        end = dt.date.today()
        items = list(Client.open("https://earth-search.aws.element84.com/v1").search(
            collections=["sentinel-2-l2a"], intersects=poly.__geo_interface__,
            datetime=f"{end - dt.timedelta(days=60)}/{end}", max_items=5).items())
        return items
    items = _check("Python  -> Sentinel-2 search (pystac-client)", search, lines) or []
    href = items[0].assets["red"].href if items else None
    if href:
        _check("Python  -> one Sentinel-2 image, first 16 kB",
               lambda: len(session().get(href, headers={"Range": "bytes=0-16383"}, timeout=30).content), lines)

        def gdal_read(extra: dict):
            def go():
                with rasterio.Env(**{**gdal_env(), **extra}):
                    with rasterio.open(href) as src:
                        return f"{src.width}x{src.height} {src.crs}"
            return go
        _check("GDAL    -> same image, build settings", gdal_read({}), lines)
        _check("GDAL    -> same image, 20 s timeouts",
               gdal_read({"GDAL_HTTP_TIMEOUT": "20", "GDAL_HTTP_CONNECTTIMEOUT": "10"}), lines)
        sys_proxy = urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http")
        if sys_proxy:
            _check(f"GDAL    -> same image through the system proxy {sys_proxy}",
                   gdal_read({"GDAL_HTTP_PROXY": sys_proxy.split("://")[-1].rstrip("/"),
                              "GDAL_HTTP_TIMEOUT": "20", "GDAL_HTTP_CONNECTTIMEOUT": "10"}), lines)
        import fa_farm.sources.satellite as sat
        _check("GDAL    -> one scene's field NDVI from image tiles (fallback)",
               lambda: sat._field_ndvi(items[0], poly), lines)

    def mpc():
        import fa_farm.sources.satellite as sat
        end = dt.date.today()
        found = sat._mpc_search(poly, end - dt.timedelta(days=60), end, 60)
        return sat._field_ndvi_mpc(found[0], poly) if found else "no scenes"
    _check("Python  -> one scene's field NDVI from Planetary Computer (build path)", mpc, lines)

    from fa_farm.sources.terrain import dem_window
    _check("GDAL    -> Copernicus DEM (terrain)", lambda: dem_window(LAT, LON, 200.0, tmp)[1], lines)
    from fa_farm.sources.soil import soilgrids_profile
    _check("GDAL    -> SoilGrids (soil)", lambda: type(soilgrids_profile(LAT, LON, tmp)[0]).__name__, lines)
    from fa_farm.sources.weather import power_daily
    _check("Python  -> NASA POWER (weather)",
           lambda: len(power_daily(LAT, LON, dt.date(2024, 1, 1), dt.date(2024, 1, 31), tmp)[0]), lines)

    out = _report_dir() / "diagnose_log.txt"
    try:
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nreport written to {out}", flush=True)
    except OSError as e:
        print(f"\ncould not write {out}: {e}", flush=True)
    sys.stdout.flush()
    os._exit(0)                     # a stalled GDAL thread must not keep the window open


if __name__ == "__main__":
    sys.exit(main())
