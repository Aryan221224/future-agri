"""Weed detection on an orthomosaic: tiling, georeferencing, de-duplication, and the
console path from upload to an ingested scan."""
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fa_console.detect import detect_orthomosaic
from fa_gateway.perception import Detection
from test_console_farms import _create, app, fake_builder  # noqa: F401  (fixture)

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

X0, Y0, GSD = 670_000.0, 5_820_000.0, 0.01          # UTM 31N, 1 cm pixels


def _ortho(path, w=900, h=700):
    arr = np.zeros((3, h, w), np.uint8)
    arr[1, 300:310, 500:510] = 255                    # a green blob: weed, centre pixel (505, 305)
    arr[0, 100:110, 100:110] = 255                    # a red blob: the crop
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=3, dtype="uint8",
                       crs="EPSG:32631", transform=from_origin(X0, Y0, GSD, GSD)) as ds:
        ds.write(arr)
    return path


class BlobDetector:
    """Green blobs are weeds, red blobs are maize -- enough to check the geometry."""
    object_classes = ["maize", "weed"]
    classes = ["maize", "weed", "background"]

    def __init__(self):
        self.calls = 0

    def infer(self, img):
        self.calls += 1
        out = []
        for ch, cls in ((1, 1), (0, 0)):
            ys, xs = np.nonzero(img[..., ch] > 200)
            if len(xs):
                p = np.zeros(3)
                p[cls], p[2] = 0.9, 0.1
                out.append(Detection((float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)), p, 0.9))
        return out


def test_detect_orthomosaic_georeferences_points(tmp_path):
    from pyproj import Transformer
    det = BlobDetector()
    seen = []
    gj, info = detect_orthomosaic(_ortho(tmp_path / "o.tif"), det, tile=400, overlap=64,
                                  progress=lambda f, m: seen.append(f))
    feats = gj["features"]
    assert len(feats) == 1, feats                      # once, though two overlapping tiles saw it
    f = feats[0]
    assert f["properties"]["class"] == "weed" and f["properties"]["label"] == "weed"
    assert 0 < f["properties"]["score"] <= 1
    lon, lat = f["geometry"]["coordinates"]
    ex, ey = Transformer.from_crs(32631, 4326, always_xy=True).transform(X0 + 5.05, Y0 - 3.05)
    assert abs(lon - ex) < 3e-7 and abs(lat - ey) < 3e-7          # within ~2 cm
    assert info["gsd_m"] == pytest.approx(GSD) and info["crop"] == 1 and info["weeds"] == 1
    assert info["tiles"] == det.calls >= 4 and seen and seen[-1] == pytest.approx(1.0)


def test_empty_tiles_are_skipped(tmp_path):
    p = tmp_path / "blank.tif"
    with rasterio.open(p, "w", driver="GTiff", width=500, height=500, count=3, dtype="uint8",
                       crs="EPSG:32631", transform=from_origin(X0, Y0, GSD, GSD)) as ds:
        ds.write(np.zeros((3, 500, 500), np.uint8))
    det = BlobDetector()
    gj, info = detect_orthomosaic(p, det, tile=256, overlap=32)
    assert gj["features"] == [] and det.calls == 0 and info["skipped"] == info["tiles_total"]


def test_ortho_upload_queues_detect_and_result_is_ingested(app, tmp_path):
    buf = io.BytesIO(_ortho(tmp_path / "f.tif").read_bytes())
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        r = c.post(f"/api/farms/{fid}/session/ortho", files={"file": ("field.tif", buf.getvalue(), "image/tiff")})
        assert r.status_code == 422 and "GPU server" in r.text              # nothing can detect yet
        tok = c.get("/api/workers/token").json()["token"]
        assert c.post("/api/workers/claim", json={"token": tok, "worker": "h200", "kinds": ["detect"]}).status_code == 204
        r = c.post(f"/api/farms/{fid}/session/ortho", files={"file": ("field.tif", buf.getvalue(), "image/tiff")})
        assert r.status_code == 200, r.text
        job = r.json()["job"]
        assert job["kind"] == "detect" and job["target"] == "remote"
        assert c.post(f"/api/farms/{fid}/session/ortho", files={"file": ("x.png", b"png", "image/png")}).status_code == 422
        got = c.post("/api/workers/claim", json={"token": tok, "worker": "h200", "kinds": ["detect"]}).json()
        assert got["id"] == job["id"] and got["farm"]["id"] == fid
        assert c.get(got["files"]["ortho"], headers={"X-Worker-Token": tok}).content == buf.getvalue()
        assert c.get(got["files"]["ortho"]).status_code == 401
        gj = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [5.6205, 52.5205]},
             "properties": {"class": "weed", "score": 0.8}}]}
        r = c.post(f"/api/workers/{job['id']}/complete",
                   json={"token": tok, "result": {"summary": {"weeds": 1}, "geojson": gj, "gsd_m": 0.01}})
        assert r.status_code == 200, r.text
        d = c.get(f"/api/jobs/{job['id']}").json()
        assert d["status"] == "done" and "imported" in d["message"]
        ev = [e for e in c.get("/api/audit", params={"farm_id": fid}).json()["events"] if e["kind"] == "scan_uploaded"]
        assert ev and ev[-1]["payload"]["weeds"] == 1
