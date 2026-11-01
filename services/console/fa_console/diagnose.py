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
    """Is throughput to the Sentinel-2 bucket limited per connection or for the whole link?
    Downloads 4 x 1.5 MB slices of real images one after another, then 4 others at once."""
    from fa_farm.sources.base import session
    base = ("https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/43/R/GM/2025/11/"
            "S2B_43RGM_20251109_0_L2A/")
    files = ["B02.tif", "B03.tif", "B04.tif", "B08.tif"]
    size = 1_500_000

    def get(name: str, start: int) -> int:
        r = session().get(base + name, headers={"Range": f"bytes={start}-{start + size - 1}"}, timeout=120)
        r.raise_for_status()
        return len(r.content)
    lines = [f"Future Agri link speed to the Sentinel-2 bucket (AWS us-west-2), {dt.datetime.now().isoformat(timespec='seconds')}"]
    t0 = time.time()
    got = sum(get(f, 60_000_000) for f in files)
    t_seq = time.time() - t0
    lines.append(f"one at a time: {got / 1e6:.1f} MB in {t_seq:.1f} s = {got / 1e3 / t_seq:.0f} kB/s")
    t0 = time.time()
    with cf.ThreadPoolExecutor(4) as ex:
        got = sum(ex.map(lambda f: get(f, 90_000_000), files))
    t_par = time.time() - t0
    lines.append(f"four at once:  {got / 1e6:.1f} MB in {t_par:.1f} s = {got / 1e3 / t_par:.0f} kB/s"
                 f"  ({t_seq / t_par:.1f}x)")
    for ln in lines:
        print(ln, flush=True)
    out = Path(__file__).resolve().parents[3] / "diagnose_speed.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport written to {out}", flush=True)
    return 0


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
        _check("GDAL    -> one scene's field NDVI (what the build does per scene)",
               lambda: sat._field_ndvi(items[0], poly), lines)

    from fa_farm.sources.terrain import dem_window
    _check("GDAL    -> Copernicus DEM (terrain)", lambda: dem_window(LAT, LON, 200.0, tmp)[1], lines)
    from fa_farm.sources.soil import soilgrids_profile
    _check("GDAL    -> SoilGrids (soil)", lambda: type(soilgrids_profile(LAT, LON, tmp)[0]).__name__, lines)
    from fa_farm.sources.weather import power_daily
    _check("Python  -> NASA POWER (weather)",
           lambda: len(power_daily(LAT, LON, dt.date(2024, 1, 1), dt.date(2024, 1, 31), tmp)[0]), lines)

    out = Path(__file__).resolve().parents[3] / "diagnose_log.txt"
    try:
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nreport written to {out}", flush=True)
    except OSError as e:
        print(f"\ncould not write {out}: {e}", flush=True)
    sys.stdout.flush()
    os._exit(0)                     # a stalled GDAL thread must not keep the window open


if __name__ == "__main__":
    sys.exit(main())
