"""A regular cell grid over a real field polygon, in local UTM metres."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from shapely import contains_xy


@dataclass
class FarmGrid:
    polygon_local: object          # shapely Polygon in local UTM metres
    crs: object                    # pyproj CRS of the local frame
    cell_m: float
    xs: np.ndarray                 # cell-centre x of grid columns
    ys: np.ndarray                 # cell-centre y of grid rows
    mask: np.ndarray               # (ny, nx) True where the cell centre is inside the field
    polygon_wgs84: object = None

    @classmethod
    def from_wgs84(cls, polygon_wgs84, cell_m: float = 5.0) -> "FarmGrid":
        from fa_core.geo import to_local
        poly, crs = to_local(polygon_wgs84)
        minx, miny, maxx, maxy = poly.bounds
        xs = np.arange(minx + cell_m / 2, maxx, cell_m)
        ys = np.arange(miny + cell_m / 2, maxy, cell_m)
        if len(xs) == 0:
            xs = np.array([(minx + maxx) / 2])
        if len(ys) == 0:
            ys = np.array([(miny + maxy) / 2])
        X, Y = np.meshgrid(xs, ys)
        mask = contains_xy(poly, X, Y)
        if not mask.any():                  # sliver narrower than one cell: keep the centroid cell
            c = poly.representative_point()
            ix = int(np.argmin(np.abs(xs - c.x)))
            iy = int(np.argmin(np.abs(ys - c.y)))
            mask[iy, ix] = True
        return cls(poly, crs, float(cell_m), xs, ys, mask, polygon_wgs84)

    @property
    def shape(self) -> tuple[int, int]:
        return self.mask.shape

    @property
    def n_cells(self) -> int:
        return int(self.mask.sum())

    @property
    def area_ha(self) -> float:
        return self.n_cells * self.cell_m ** 2 / 1e4

    @property
    def bounds_local(self) -> tuple[float, float, float, float]:
        return self.polygon_local.bounds

    @property
    def cells_xy(self) -> np.ndarray:
        """(n, 2) centres of active cells, row-major order."""
        X, Y = np.meshgrid(self.xs, self.ys)
        return np.column_stack([X[self.mask], Y[self.mask]])

    def to_raster(self, values: np.ndarray, fill=np.nan) -> np.ndarray:
        out = np.full(self.shape, fill, dtype=float)
        out[self.mask] = values
        return out

    def from_raster(self, raster: np.ndarray) -> np.ndarray:
        return raster[self.mask]

    def cells_in(self, geom_local) -> np.ndarray:
        xy = self.cells_xy
        return contains_xy(geom_local, xy[:, 0], xy[:, 1])

    def cells_lonlat(self) -> np.ndarray:
        from pyproj import Transformer
        tr = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
        xy = self.cells_xy
        lon, lat = tr.transform(xy[:, 0], xy[:, 1])
        return np.column_stack([lon, lat])
