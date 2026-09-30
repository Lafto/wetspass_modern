# -*- coding: utf-8 -*-
"""
WetSpass-M Modern — Raster I/O with block processing and auto-alignment.
=========================================================================
Reads each block of an input raster from whatever grid it lives on,
warping it on-the-fly to the DEM block's grid. This lets the user supply
inputs at different CRSs, resolutions, or extents and still get a valid
run — no external gdalwarp step required.

Alignment rules
---------------
- Rasters whose stem starts with `landuse` or `soil` are treated as
  categorical and resampled with nearest-neighbour.
- All other rasters are treated as continuous and resampled with bilinear.
- When the source is already on the DEM grid, the warp is a no-op
  (a straight window read).

Contract
--------
RasterIO.read(path)                  → (data, meta)
RasterIO.read_template(path)         → (None, meta)
RasterIO.write(path, data, meta)     → None

BlockProcessor.iter_blocks(meta)     → yields (row_off, col_off, h, w)
BlockProcessor.read_block(paths, ...)→ dict of {name: ndarray}
BlockProcessor.write_block(...)      → None
BlockProcessor.create_empty(...)     → None
"""

import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

try:
    import rasterio
    from rasterio.windows import Window
    from rasterio.warp import reproject, Resampling
    from rasterio.transform import array_bounds
    RASTERIO_AVAILABLE = True
except ImportError:
    rasterio = None
    Window = None
    reproject = None
    Resampling = None
    array_bounds = None
    RASTERIO_AVAILABLE = False

logger = logging.getLogger("WetSpassModern")


COMMON_SENTINELS = [
    -9999, -99999, -32768, -3.4028235e38, 3.4028235e38,
    1e20, -1e20, 1e30, -1e30,
]

PHYSICAL_RANGES = {
    "default":   (-1e6, 1e6),
    "rainfall":  (0.0, 5000.0),
    "pet":       (0.0, 2000.0),
    "temp":      (-80.0, 70.0),
    "wind":      (0.0, 200.0),
    "gwdepth":   (-1e5, 1e6),
    "dem":       (-1000.0, 10000.0),
    "slope":     (-1.0, 1000.0),
    "snowcover": (0.0, 1.0),
}


def _is_geographic(crs) -> bool:
    if crs is None:
        return False
    attr = getattr(crs, "is_geographic", None)
    if attr is not None:
        try:
            return bool(attr)
        except Exception:
            pass
    method = getattr(crs, "isGeographic", None)
    if callable(method):
        try:
            return bool(method())
        except Exception:
            pass
    try:
        epsg = crs.to_epsg()
        if epsg is not None and 4000 <= int(epsg) < 5000:
            return True
    except Exception:
        pass
    return False


def _categorical_from_name(path) -> bool:
    """
    True when the file should be resampled with nearest-neighbour.
    Land use and soil are the only categorical rasters in WetSpass-M.
    All other inputs are continuous.
    """
    stem = Path(str(path)).stem.lower()
    return stem.startswith("landuse") or stem == "soil"


# ===========================================================================
class RasterIO:
    """Full-raster I/O — suitable for grids up to a few tens of millions of
    cells. Larger grids should use BlockProcessor."""

    def __init__(self, nodata: float = -9999.0):
        self.nodata = nodata
        if not RASTERIO_AVAILABLE:
            raise ImportError(
                "rasterio is required. Install with: pip install rasterio")

    def read(self, path, kind: str = "default", mask_sentinels: bool = True):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Raster not found: {path}")
        with rasterio.open(path) as src:
            data = src.read(1).astype(np.float64)
            meta = {
                "transform": src.transform, "crs": src.crs,
                "width": src.width, "height": src.height,
                "nodata": src.nodata if src.nodata is not None else self.nodata,
                "count": src.count,
            }
            if src.nodata is not None:
                data[data == src.nodata] = np.nan
            if mask_sentinels:
                for s in COMMON_SENTINELS:
                    data[data == s] = np.nan
            lo, hi = PHYSICAL_RANGES.get(kind, PHYSICAL_RANGES["default"])
            data[(data < lo) | (data > hi)] = np.nan
        return data, meta

    def read_template(self, path):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Raster not found: {path}")
        with rasterio.open(path) as src:
            meta = {
                "transform": src.transform, "crs": src.crs,
                "width": src.width, "height": src.height,
                "nodata": (src.nodata if src.nodata is not None
                           else self.nodata),
                "count": src.count,
            }
        return None, meta

    def write(self, path, data, meta, nodata=None, compress="deflate"):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        nd = nodata if nodata is not None else meta.get("nodata", self.nodata)
        out = data.astype(np.float64, copy=True)
        out[~np.isfinite(out)] = nd
        f32_max = np.finfo(np.float32).max
        out = np.clip(out, -f32_max, f32_max)
        out[~np.isfinite(out)] = nd
        profile = {
            "driver": "GTiff", "dtype": "float32", "nodata": nd,
            "width": meta["width"], "height": meta["height"], "count": 1,
            "crs": meta.get("crs"), "transform": meta.get("transform"),
            "compress": compress, "tiled": True,
            "blockxsize": 256, "blockysize": 256,
        }
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(out.astype(np.float32), 1)

    def lookup_map(self, base_raster, lookup_dict, nodata=-9999):
        out = np.full_like(base_raster, nodata, dtype=np.float64)
        for code, val in lookup_dict.items():
            out[base_raster == code] = val
        out[out == nodata] = np.nan
        return out


# ===========================================================================
class BlockProcessor:
    """
    Streams rasters in fixed-size tiles with an overlap (halo).
    Auto-aligns every input to the DEM block grid, so inputs at different
    CRSs, resolutions, or extents are handled transparently.
    """

    def __init__(self, block_size: int = 1024, halo: int = 2,
                 nodata: float = -9999.0):
        if not RASTERIO_AVAILABLE:
            raise ImportError("rasterio is required")
        self.block_size = block_size
        self.halo = halo
        self.nodata = nodata

    # -----------------------------------------------------------------
    def iter_blocks(self, meta):
        H = meta["height"]
        W = meta["width"]
        bs = self.block_size
        for row in range(0, H, bs):
            for col in range(0, W, bs):
                h = min(bs, H - row)
                w = min(bs, W - col)
                yield row, col, h, w

    def n_blocks(self, meta) -> int:
        bs = self.block_size
        H, W = meta["height"], meta["width"]
        return ((H + bs - 1) // bs) * ((W + bs - 1) // bs)

    # -----------------------------------------------------------------
    def _read_aligned(self, src, dem_crs, block_transform, block_shape,
                      categorical: bool) -> np.ndarray:
        """
        Read the portion of `src` that covers the DEM block and warp it
        onto the DEM block's grid.
        """
        from rasterio.warp import transform_bounds

        # 1. Bounding box of the DEM block in DEM CRS
        dem_bounds = array_bounds(block_shape[0], block_shape[1],
                                   block_transform)

        # 2. Transform that bounding box to the source CRS
        try:
            src_bounds = transform_bounds(
                dem_crs, src.crs, *dem_bounds, densify_pts=21)
        except Exception:
            # If transform fails, fall back to reading the full source
            src_bounds = (src.bounds.left, src.bounds.bottom,
                          src.bounds.right, src.bounds.top)

        # 3. Source window covering that bounding box, padded by 2 px
        pad = 2
        col_off = max(0, int(np.floor(
            (src_bounds[0] - src.transform.c) / src.transform.a)) - pad)
        row_off = max(0, int(np.floor(
            (src_bounds[3] - src.transform.f) / src.transform.e)) - pad)
        col_end = min(src.width, int(np.ceil(
            (src_bounds[2] - src.transform.c) / src.transform.a)) + pad)
        row_end = min(src.height, int(np.ceil(
            (src_bounds[1] - src.transform.f) / src.transform.e)) + pad)

        if col_end <= col_off or row_end <= row_off:
            # Block does not overlap the source — return all NoData
            return np.full(block_shape, np.nan, dtype=np.float64)

        win = Window(col_off, row_off,
                     col_end - col_off, row_end - row_off)

        # 4. Read the source window
        src_arr = src.read(1, window=win).astype(np.float64)
        src_transform = src.window_transform(win)
        nd = src.nodata if src.nodata is not None else self.nodata
        src_arr[src_arr == nd] = np.nan
        for s in COMMON_SENTINELS:
            src_arr[src_arr == s] = np.nan

        # 5. Warp to the DEM block grid
        dst = np.full(block_shape, np.nan, dtype=np.float64)
        resampling = (Resampling.nearest if categorical
                       else Resampling.bilinear)
        try:
            reproject(
                source=src_arr,
                destination=dst,
                src_transform=src_transform,
                src_crs=src.crs,
                src_nodata=np.nan,
                dst_transform=block_transform,
                dst_crs=dem_crs,
                dst_nodata=np.nan,
                resampling=resampling,
            )
        except Exception as exc:
            logger.warning(
                "Auto-align failed for %s: %s — returning NoData block.",
                src.name, exc)
            return np.full(block_shape, np.nan, dtype=np.float64)

        return dst

    # -----------------------------------------------------------------
    def read_block(self, paths: Dict[str, Path],
                   row_off: int, col_off: int,
                   height: int, width: int,
                   meta: dict) -> Dict[str, np.ndarray]:
        """
        Read a block of rasters. Each raster is warped on the fly to the
        DEM block grid, so inputs with different CRS / resolution / extent
        are handled transparently.
        """
        H, W = meta["height"], meta["width"]
        halo = self.halo
        r0 = max(0, row_off - halo)
        c0 = max(0, col_off - halo)
        r1 = min(H, row_off + height + halo)
        c1 = min(W, col_off + width + halo)
        block_shape = (r1 - r0, c1 - c0)

        dem_transform = meta["transform"]
        dem_crs = meta["crs"]

        # Top-left corner of the DEM block
        block_transform = rasterio.Affine(
            dem_transform.a, dem_transform.b,
            dem_transform.c + c0 * dem_transform.a + r0 * dem_transform.b,
            dem_transform.d, dem_transform.e,
            dem_transform.f + c0 * dem_transform.d + r0 * dem_transform.e,
        )

        out: Dict[str, np.ndarray] = {}
        for name, path in paths.items():
            with rasterio.open(str(path)) as src:
                same_grid = (
                    src.crs == dem_crs
                    and src.transform == dem_transform
                    and src.width == W
                    and src.height == H
                )
                if same_grid:
                    # Same grid — plain window read
                    win = Window(c0, r0, c1 - c0, r1 - r0)
                    arr = src.read(1, window=win).astype(np.float64)
                    nd = (src.nodata if src.nodata is not None
                          else self.nodata)
                    arr[arr == nd] = np.nan
                    for s in COMMON_SENTINELS:
                        arr[arr == s] = np.nan
                    out[name] = arr
                else:
                    # Different grid — warp on the fly
                    categorical = _categorical_from_name(path)
                    out[name] = self._read_aligned(
                        src, dem_crs, block_transform, block_shape,
                        categorical)

        out["_block_geometry"] = {
            "r0": r0, "c0": c0, "r1": r1, "c1": c1,
            "row_off": row_off, "col_off": col_off,
            "height": height, "width": width,
        }
        return out

    # -----------------------------------------------------------------
    def interior_slice(self, block_meta: dict) -> Tuple[slice, slice]:
        g = block_meta
        top = g["row_off"] - g["r0"]
        left = g["col_off"] - g["c0"]
        bot = top + g["height"]
        right = left + g["width"]
        return slice(top, bot), slice(left, right)

    # -----------------------------------------------------------------
    def write_block(self, path, data: np.ndarray, block_meta: dict,
                    meta: dict, nodata=None, compress="deflate"):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"write_block: target file was never created: {path}")
        g = block_meta
        sl_r, sl_c = self.interior_slice(g)
        interior = data[sl_r, sl_c]
        nd = nodata if nodata is not None else self.nodata
        out = interior.astype(np.float64, copy=True)
        out[~np.isfinite(out)] = nd
        f32_max = np.finfo(np.float32).max
        out = np.clip(out, -f32_max, f32_max)
        out[~np.isfinite(out)] = nd
        win = Window(g["col_off"], g["row_off"], g["width"], g["height"])
        with rasterio.open(path, "r+") as dst:
            dst.write(out.astype(np.float32), 1, window=win)

    # -----------------------------------------------------------------
    def create_empty(self, path, meta: dict, nodata=None, compress="deflate"):
        """
        Create an empty GeoTIFF with the reference grid.
        Writes one NoData pixel so GDAL physically creates the file.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        nd = nodata if nodata is not None else self.nodata
        profile = {
            "driver": "GTiff",
            "dtype": "float32",
            "nodata": nd,
            "width": int(meta["width"]),
            "height": int(meta["height"]),
            "count": 1,
            "crs": meta.get("crs"),
            "transform": meta.get("transform"),
            "compress": compress,
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
        }
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(
                np.array([[nd]], dtype=np.float32),
                1,
                window=Window(0, 0, 1, 1),
            )
        if not path.exists():
            raise RuntimeError(
                f"create_empty: failed to physically create {path}")

    # -----------------------------------------------------------------
    def read_block_only(self, path, row_off, col_off,
                        height, width, meta):
        with rasterio.open(path) as src:
            arr = src.read(
                1, window=Window(col_off, row_off, width, height)
            ).astype(np.float64)
            nd = src.nodata if src.nodata is not None else self.nodata
        arr[arr == nd] = np.nan
        return arr

    def write_block_only(self, path, data, row_off, col_off,
                         height, width, nodata=None):
        nd = nodata if nodata is not None else self.nodata
        out = data.astype(np.float64, copy=True)
        out[~np.isfinite(out)] = nd
        f32_max = np.finfo(np.float32).max
        out = np.clip(out, -f32_max, f32_max)
        out[~np.isfinite(out)] = nd
        win = Window(col_off, row_off, width, height)
        with rasterio.open(path, "r+") as dst:
            dst.write(out.astype(np.float32), 1, window=win)