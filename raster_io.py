# -*- coding: utf-8 -*-
"""
WetSpass-M Modern — Raster I/O with block processing and auto-alignment.
"""

import contextlib
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


def _try_call(fn, default=None):
    try:
        return fn()
    except Exception as exc:
        logger.debug("_try_call: ignored %s: %s",
                     getattr(fn, "__name__", "callable"), exc)
        return default


def _is_geographic(crs) -> bool:
    if crs is None:
        return False
    attr = getattr(crs, "is_geographic", None)
    if attr is not None:
        result = _try_call(lambda: bool(attr))
        if result is not None:
            return result
    method = getattr(crs, "isGeographic", None)
    if callable(method):
        result = _try_call(lambda: bool(method()))
        if result is not None:
            return result
    epsg = _try_call(lambda: crs.to_epsg())
    if epsg is not None and 4000 <= int(epsg) < 5000:
        return True
    return False


def _categorical_from_name(path) -> bool:
    stem = Path(str(path)).stem.lower()
    return stem.startswith("landuse") or stem == "soil"


# ===========================================================================
# RasterIO
# ===========================================================================
class RasterIO:

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
                "nodata": (src.nodata if src.nodata is not None
                           else self.nodata),
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
# BlockProcessor
# ===========================================================================
class BlockProcessor:

    def __init__(self, block_size: int = 1024, halo: int = 2,
                 nodata: float = -9999.0):
        if not RASTERIO_AVAILABLE:
            raise ImportError("rasterio is required")
        self.block_size = block_size
        self.halo = halo
        self.nodata = nodata

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

    def _read_aligned(self, src, dem_crs, block_transform, block_shape,
                      categorical: bool) -> np.ndarray:
        from rasterio.warp import transform_bounds

        dem_bounds = array_bounds(block_shape[0], block_shape[1],
                                   block_transform)
        src_bounds = _try_call(
            lambda: transform_bounds(
                dem_crs, src.crs, *dem_bounds, densify_pts=21))
        if src_bounds is None:
            src_bounds = (src.bounds.left, src.bounds.bottom,
                          src.bounds.right, src.bounds.top)

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
            return np.full(block_shape, np.nan, dtype=np.float64)

        win = Window(col_off, row_off,
                     col_end - col_off, row_end - row_off)
        src_arr = src.read(1, window=win).astype(np.float64)
        src_transform = src.window_transform(win)
        nd = src.nodata if src.nodata is not None else self.nodata
        src_arr[src_arr == nd] = np.nan
        for s in COMMON_SENTINELS:
            src_arr[src_arr == s] = np.nan

        dst = np.full(block_shape, np.nan, dtype=np.float64)
        resampling = (Resampling.nearest if categorical
                       else Resampling.bilinear)

        def _do_reproject():
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
            return True

        ok = _try_call(_do_reproject, default=False)
        if not ok:
            logger.warning(
                "Auto-align failed for %s — returning NoData block.",
                src.name)
            return np.full(block_shape, np.nan, dtype=np.float64)
        return dst

    def read_block(self, paths: Dict[str, Path],
                   row_off: int, col_off: int,
                   height: int, width: int,
                   meta: dict) -> Dict[str, np.ndarray]:
        H, W = meta["height"], meta["width"]
        halo = self.halo
        r0 = max(0, row_off - halo)
        c0 = max(0, col_off - halo)
        r1 = min(H, row_off + height + halo)
        c1 = min(W, col_off + width + halo)
        block_shape = (r1 - r0, c1 - c0)

        dem_transform = meta["transform"]
        dem_crs = meta["crs"]

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
                    win = Window(c0, r0, c1 - c0, r1 - r0)
                    arr = src.read(1, window=win).astype(np.float64)
                    nd = (src.nodata if src.nodata is not None
                          else self.nodata)
                    arr[arr == nd] = np.nan
                    for s in COMMON_SENTINELS:
                        arr[arr == s] = np.nan
                    out[name] = arr
                else:
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

    def interior_slice(self, block_meta: dict) -> Tuple[slice, slice]:
        g = block_meta
        top = g["row_off"] - g["r0"]
        left = g["col_off"] - g["c0"]
        bot = top + g["height"]
        right = left + g["width"]
        return slice(top, bot), slice(left, right)

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

    def create_empty(self, path, meta: dict, nodata=None, compress="deflate"):
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


# ===========================================================================
# Masking helpers
# ===========================================================================
def set_nodata_outside_dem(raster_path,
                           dem_path,
                           nodata_out: float = -9999.0,
                           tile: int = 2048,
                           progress_callback=None):
    from pathlib import Path as _P

    raster_path = _P(raster_path)
    dem_path = _P(dem_path)

    if not raster_path.exists():
        if progress_callback:
            progress_callback(0, 1, f"skip: {raster_path.name} not found")
        return "SKIP"

    with rasterio.open(str(dem_path)) as dem_src:
        dem_h, dem_w = dem_src.height, dem_src.width
        dem_nd = (dem_src.nodata if dem_src.nodata is not None
                  else -9999.0)

        with rasterio.open(str(raster_path)) as src:
            if (src.height, src.width) != (dem_h, dem_w):
                if progress_callback:
                    progress_callback(
                        0, 1,
                        f"skip: {raster_path.name} grid mismatch "
                        f"({src.height}×{src.width} vs "
                        f"{dem_h}×{dem_w})")
                return "SKIP"

            H, W = src.height, src.width
            src_nd = src.nodata if src.nodata is not None else -9999.0

            profile = src.profile.copy()
            profile.update(
                dtype="float32",
                nodata=nodata_out,
                compress="deflate",
                tiled=True,
                blockxsize=256,
                blockysize=256,
            )

            tmp_path = raster_path.with_suffix(
                raster_path.suffix + ".tmp.tif")

            n_tiles_x = (W + tile - 1) // tile
            n_tiles_y = (H + tile - 1) // tile
            n_tiles = n_tiles_x * n_tiles_y
            done = 0

            with rasterio.open(str(tmp_path), "w", **profile) as dst:
                for r0 in range(0, H, tile):
                    h = min(tile, H - r0)
                    for c0 in range(0, W, tile):
                        w = min(tile, W - c0)
                        win = Window(c0, r0, w, h)

                        dem_tile = dem_src.read(1, window=win)
                        if np.issubdtype(dem_tile.dtype, np.floating):
                            valid = ((dem_tile != dem_nd)
                                     & np.isfinite(dem_tile))
                        else:
                            valid = dem_tile != dem_nd

                        arr = src.read(1, window=win).astype(np.float32)
                        arr = np.where(valid, arr, nodata_out)
                        arr = np.where(arr == src_nd, nodata_out, arr)
                        arr = np.where(np.isfinite(arr), arr, nodata_out)

                        dst.write(arr.astype(np.float32), 1, window=win)
                        del dem_tile, arr, valid

                        done += 1
                        if progress_callback and (done % 8 == 0
                                                  or done == n_tiles):
                            progress_callback(
                                done, n_tiles,
                                f"masking {raster_path.name}…")

    try:
        raster_path.unlink()
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        if progress_callback:
            progress_callback(
                0, 1,
                f"fail: {raster_path.name} locked ({exc})")
        return "FAIL"
    tmp_path.rename(raster_path)
    return "OK"


def mask_diffusive_outputs_to_dem(output_dir,
                                  working_dir,
                                  files=None,
                                  nodata_out: float = -9999.0,
                                  tile: int = 2048,
                                  progress_callback=None):
    from pathlib import Path as _P

    output_dir = _P(output_dir)
    working_dir = _P(working_dir)

    dem_path = None
    for sub in ("Inputs/maps", "inputs/maps", "Maps", "maps"):
        for ext in (".tif", ".asc"):
            p = working_dir / sub / f"dem{ext}"
            if p.exists():
                dem_path = p
                break
        if dem_path is not None:
            break
    if dem_path is None:
        if progress_callback:
            progress_callback(0, 1,
                              f"DEM not found under {working_dir}")
        return {"ok": [], "skip": [], "fail": ["(no DEM)"]}

    if files is None:
        targets = ["Recharge_diffusive_annual.tif"]
        targets += [f"Recharge_diffusive_{m}.tif" for m in range(1, 13)]
        for optional in ("Recharge_Total_annual.tif",
                         "Recharge_focused_annual.tif"):
            if (output_dir / optional).exists():
                targets.append(optional)
    else:
        targets = list(files)

    result = {"ok": [], "skip": [], "fail": []}
    for name in targets:
        p = output_dir / name
        status = set_nodata_outside_dem(
            p, dem_path,
            nodata_out=nodata_out, tile=tile,
            progress_callback=progress_callback)
        if status == "OK":
            result["ok"].append(name)
        elif status == "SKIP":
            result["skip"].append(name)
        else:
            result["fail"].append(name)
    return result


def _apply_mask_array_to_raster(raster_path,
                                 mask_arr: np.ndarray,
                                 nodata_out: float = -9999.0,
                                 tile: int = 2048,
                                 progress_callback=None):
    from pathlib import Path as _P
    raster_path = _P(raster_path)
    if not raster_path.exists():
        return "SKIP"

    with rasterio.open(str(raster_path)) as src:
        if (src.height, src.width) != mask_arr.shape:
            return "SKIP"
        H, W = src.height, src.width
        src_nd = src.nodata if src.nodata is not None else -9999.0
        profile = src.profile.copy()
        profile.update(dtype="float32", nodata=nodata_out,
                       compress="deflate", tiled=True,
                       blockxsize=256, blockysize=256)

        tmp_path = raster_path.with_suffix(raster_path.suffix + ".tmp.tif")
        n_tiles = ((H + tile - 1) // tile) * ((W + tile - 1) // tile)
        done = 0

        with rasterio.open(str(tmp_path), "w", **profile) as dst:
            for r0 in range(0, H, tile):
                h = min(tile, H - r0)
                for c0 in range(0, W, tile):
                    w = min(tile, W - c0)
                    win = Window(c0, r0, w, h)
                    arr = src.read(1, window=win).astype(np.float32)
                    m = mask_arr[r0:r0 + h, c0:c0 + w] > 0
                    arr = np.where(m, arr, nodata_out)
                    arr = np.where(arr == src_nd, nodata_out, arr)
                    arr = np.where(np.isfinite(arr), arr, nodata_out)
                    dst.write(arr.astype(np.float32), 1, window=win)

                    done += 1
                    if progress_callback and (done % 8 == 0
                                              or done == n_tiles):
                        progress_callback(
                            done, n_tiles,
                            f"masking {raster_path.name}…")

    try:
        raster_path.unlink()
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        if progress_callback:
            progress_callback(
                0, 1,
                f"fail: {raster_path.name} locked ({exc})")
        return "FAIL"
    tmp_path.rename(raster_path)
    return "OK"


def mask_rasters_to_boundary(output_dir,
                              boundary_path,
                              reference_raster,
                              files=None,
                              nodata_out: float = -9999.0,
                              tile: int = 2048,
                              progress_callback=None):
    """
    Rasterize a boundary polygon onto the grid of `reference_raster`,
    then set nodata outside the polygon on every target raster.

    The CRS is passed as a SHORT EPSG code embedded in the EXTENT
    string, not as a WKT argument. Full WKT contains spaces and
    brackets that break QGIS's EXTRA token parser and cause
    gdal:rasterize to fail with a syntax error.
    """
    from pathlib import Path as _P
    import tempfile

    try:
        from qgis import processing
    except ImportError:
        msg = "QGIS processing not available"
        if progress_callback:
            progress_callback(0, 1, msg)
        return {"ok": [], "skip": [], "fail": [msg]}

    output_dir = _P(output_dir)
    boundary_path = _P(boundary_path)
    reference_raster = _P(reference_raster)

    if not boundary_path.exists():
        msg = f"boundary not found: {boundary_path}"
        if progress_callback:
            progress_callback(0, 1, msg)
        return {"ok": [], "skip": [], "fail": [msg]}

    if not reference_raster.exists():
        msg = f"reference not found: {reference_raster}"
        if progress_callback:
            progress_callback(0, 1, msg)
        return {"ok": [], "skip": [], "fail": [msg]}

    if progress_callback:
        progress_callback(0, 1, "Rasterizing boundary polygon…")

    # ---- Read the reference grid ----
    with rasterio.open(str(reference_raster)) as ref:
        ref_w, ref_h = ref.width, ref.height
        t = ref.transform
        crs = ref.crs

    # ---- Compute the reference extent ----
    xmin = t.c
    ymax = t.f
    xmax = xmin + t.a * ref_w
    ymin = ymax + t.e * ref_h

    # ---- Get a SHORT CRS string (EPSG preferred) ----
    crs_short = ""
    if crs is not None:
        epsg = _try_call(lambda: crs.to_epsg())
        if epsg is not None:
            crs_short = f"EPSG:{epsg}"
        else:
            wkt = _try_call(lambda: crs.to_wkt())
            if wkt:
                wkt = " ".join(wkt.split())
                crs_short = f'"{wkt}"'

    # ---- EXTENT with optional CRS in brackets ----
    extent_str = f"{xmin},{xmax},{ymin},{ymax}"
    if crs_short:
        extent_str = f"{extent_str} [{crs_short}]"

    # ---- EXTRA: no -a_srs here (CRS is in EXTENT). No -te either. ----
    extra = "-at -ot Byte -co COMPRESS=DEFLATE"

    tmp_dir = _P(tempfile.gettempdir()) / "wetspass_mask"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_mask = tmp_dir / "boundary_mask.tif"

    # Delete any stale mask from a previous run
    if tmp_mask.exists():
        with contextlib.suppress(OSError):
            tmp_mask.unlink()

    logger.info(
        "mask_rasters_to_boundary: "
        "input=%s width=%s height=%s extent=%s extra=%s output=%s",
        boundary_path, ref_w, ref_h, extent_str, extra, tmp_mask)

    try:
        processing.run("gdal:rasterize", {
            "INPUT": str(boundary_path),
            "FIELD": None,
            "BURN": 1,
            "USE_Z": False,
            "UNITS": 1,
            "WIDTH": int(ref_w),
            "HEIGHT": int(ref_h),
            "EXTENT": extent_str,
            "NODATA": 0,
            "INIT": 0,
            "INVERT": False,
            "EXTRA": extra,
            "OUTPUT": str(tmp_mask),
        })
    except Exception as exc:
        import traceback
        err = f"gdal:rasterize failed: {exc}"
        logger.error("%s\n%s", err, traceback.format_exc())
        if progress_callback:
            progress_callback(0, 1, err)
        return {"ok": [], "skip": [], "fail": [err]}

    if not tmp_mask.exists():
        msg = ("gdal:rasterize produced no output. "
               "The boundary polygon may not overlap the reference "
               "raster, or its CRS may differ.")
        if progress_callback:
            progress_callback(0, 1, msg)
        return {"ok": [], "skip": [], "fail": [msg]}

    # ---- Read the boundary mask into memory ----
    with rasterio.open(str(tmp_mask)) as msk:
        mask_arr = msk.read(1)

    n_inside = int((mask_arr > 0).sum())
    total_cells = mask_arr.size
    if n_inside == 0:
        msg = ("The rasterized boundary contains no cells inside the "
               "reference grid. Check that the boundary shapefile and "
               "the reference raster are in the same CRS and overlap.")
        if progress_callback:
            progress_callback(0, 1, msg)
        return {"ok": [], "skip": [], "fail": [msg]}

    logger.info(
        "Boundary mask covers %d of %d cells (%.2f %%).",
        n_inside, total_cells, 100.0 * n_inside / total_cells)

    # ---- Target list ----
    if files is None:
        targets = ["Recharge_diffusive_annual.tif"]
        targets += [f"Recharge_diffusive_{m}.tif" for m in range(1, 13)]
        for optional in ("Recharge_Total_annual.tif",
                         "Recharge_focused_annual.tif"):
            if (output_dir / optional).exists():
                targets.append(optional)
    else:
        targets = list(files)

    # ---- Apply mask to each target ----
    result = {"ok": [], "skip": [], "fail": []}
    for name in targets:
        p = output_dir / name
        status = _apply_mask_array_to_raster(
            p, mask_arr, nodata_out, tile, progress_callback)
        if status == "OK":
            result["ok"].append(name)
        elif status == "SKIP":
            result["skip"].append(name)
        else:
            result["fail"].append(
                f"{name}: file locked or unreadable")
    return result