"""Record small real-data fixtures for offline tests (run with network).

python scripts/record_farm_fixtures.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import requests

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "farm"
OUT.mkdir(parents=True, exist_ok=True)
CA = "/root/.ccr/ca-bundle.crt"


def power():
    url = "https://power.larc.nasa.gov/api/temporal/daily/point"
    params = dict(parameters="T2M_MAX,T2M_MIN,T2M,PRECTOTCORR,ALLSKY_SFC_SW_DWN,WS2M,T2MDEW,RH2M",
                  community="AG", longitude=5.6, latitude=52.5, start="20250401", end="20250430",
                  format="JSON")
    r = requests.get(url, params=params, timeout=120, verify=CA)
    r.raise_for_status()
    (OUT / "power_nl_202504.json").write_text(json.dumps(r.json()))


def ifs():
    import icechunk
    import xarray as xr
    st = icechunk.s3_storage(bucket="dynamical-ecmwf-ifs-ens",
                             prefix="ecmwf-ifs-ens-forecast-15-day-0-25-degree/v0.1.0.icechunk",
                             region="us-west-2", anonymous=True)
    ds = xr.open_zarr(icechunk.Repository.open(st).readonly_session("main").store, chunks=None)
    pt = ds.sel(init_time="2025-06-02T00", latitude=52.5, longitude=5.5, method="nearest")
    arrs = {v: pt[v].load().values.astype("float32") for v in
            ("temperature_2m", "precipitation_surface", "downward_short_wave_radiation_flux_surface",
             "dew_point_temperature_2m", "wind_u_10m", "wind_v_10m")}
    arrs["lead_hours"] = (pt.lead_time.values / np.timedelta64(1, "h")).astype("float32")
    np.savez_compressed(OUT / "ifs_nl_20250602.npz", **arrs)


if __name__ == "__main__":
    power()
    ifs()
    print("fixtures written to", OUT)
