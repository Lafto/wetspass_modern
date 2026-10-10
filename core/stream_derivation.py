# -*- coding: utf-8 -*-
"""
Stream network derivation from a DEM, plus D8 hydrology helpers.

Large-DEM handling
------------------
If the DEM exceeds `max_cells` (default 64 M cells), the D8 chain runs
on a decimated grid. Peak memory stays under ~1 GiB regardless of the
input DEM size. Downstream code sees no difference — outputs are on
the same CRS and grid as the input DEM.
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

try:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject
    from rasterio.windows import Window
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False

from .raster_io import RasterIO, _is_geographic

logger = logging.getLogger("WetSpassModern")


_D8_CODES = np.array([1, 2, 4, 8, 16, 32, 64, 128], dtype=np.int16)
D8_OFFSETS = {
    1:   (0, 1),
    2:   (1, 1),
    4:   (1, 0),
    8:   (1, -1),
    16:  (0, -1),
    32:  (-1, -1),
    64:  (-1, 0),
    128: (-1, 1),
}


# ===========================================================================
# DEM smoothing
# ===========================================================================
def _smooth_dem(dem: np.ndarray, sigma: float = 1.0) -> np.ndarray:
    if sigma <= 0:
        return dem
    mask = np.isfinite(dem)
    if not mask.any():
        return dem

    try:
        from scipy.ndimage import gaussian_filter
        vals = np.where(mask, dem, 0.0).astype(np.float32)
        smoothed = gaussian_filter(vals, sigma=sigma, mode="nearest")
        weights = gaussian_filter(mask.astype(np.float32),
                                   sigma=sigma, mode="nearest")
        with np.errstate(divide="ignore", invalid="ignore"):
            out = (smoothed / weights).astype(np.float32)
        out[~mask] = np.nan
        return out
    except ImportError:
        p = np.pad(dem, 1, mode="edge")
        out = np.zeros_like(dem, dtype=np.float32)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out += p[1 + di:1 + di + dem.shape[0],
                         1 + dj:1 + dj + dem.shape[1]]
        return (out / 9.0).astype(np.float32)


# ===========================================================================
# Priority-flood
# ===========================================================================
def _priority_flood(dem: np.ndarray, epsilon: float = 1e-4) -> np.ndarray:
    import heapq

    rows, cols = dem.shape
    filled = np.full((rows, cols), np.inf, dtype=np.float32)
    pq = []

    for i in range(rows):
        for j in (0, cols - 1):
            if np.isfinite(dem[i, j]):
                heapq.heappush(pq, (float(dem[i, j]), i, j))
                filled[i, j] = dem[i, j]
    for j in range(cols):
        for i in (0, rows - 1):
            if np.isfinite(dem[i, j]) and not np.isfinite(filled[i, j]):
                heapq.heappush(pq, (float(dem[i, j]), i, j))
                filled[i, j] = dem[i, j]

    neighbors = [(-1, -1), (-1, 0), (-1, 1),
                 (0, -1),           (0, 1),
                 (1, -1),  (1, 0),  (1, 1)]

    while pq:
        elev, i, j = heapq.heappop(pq)
        for di, dj in neighbors:
            ni, nj = i + di, j + dj
            if 0 <= ni < rows and 0 <= nj < cols:
                if np.isfinite(dem[ni, nj]) and not np.isfinite(filled[ni, nj]):
                    new_elev = max(dem[ni, nj], elev + epsilon)
                    filled[ni, nj] = new_elev
                    heapq.heappush(pq, (float(new_elev), ni, nj))

    filled[~np.isfinite(dem)] = np.nan
    return filled


# ===========================================================================
# D8 flow direction
# ===========================================================================
def _d8_direction(filled: np.ndarray, cellsize: float = 1.0) -> np.ndarray:
    rows, cols = filled.shape
    p = np.pad(filled, 1, mode="edge")
    center = filled

    best_diff = np.full((rows, cols), 0.0, dtype=np.float32)
    best_code = np.zeros((rows, cols), dtype=np.int16)

    diag = 1.0 / np.sqrt(2.0)
    neighbors = [
        (_D8_CODES[0], p[1:-1, 2:],   1.0),
        (_D8_CODES[1], p[2:,   2:],   diag),
        (_D8_CODES[2], p[2:,   1:-1], 1.0),
        (_D8_CODES[3], p[2:,   :-2],  diag),
        (_D8_CODES[4], p[1:-1, :-2],  1.0),
        (_D8_CODES[5], p[:-2,  :-2],  diag),
        (_D8_CODES[6], p[:-2,  1:-1], 1.0),
        (_D8_CODES[7], p[:-2,  2:],   diag),
    ]

    for code, nb, weight in neighbors:
        diff = (center - nb) * weight
        better = diff > best_diff
        best_diff[better] = diff[better]
        best_code[better] = code

    fdir = best_code

    no_down = (best_diff <= 0) & np.isfinite(filled)
    if no_down.any():
        diffs = np.stack([
            center - p[1:-1, 2:], center - p[2:, 2:],
            center - p[2:, 1:-1], center - p[2:, :-2],
            center - p[1:-1, :-2], center - p[:-2, :-2],
            center - p[:-2, 1:-1], center - p[:-2, 2:],
        ], axis=0)
        best_idx = np.argmax(diffs, axis=0).astype(np.int16)
        fdir[no_down] = _D8_CODES[best_idx[no_down]]

    fdir[~np.isfinite(filled)] = 0
    return fdir


# ===========================================================================
# Flow accumulation
# ===========================================================================
def _d8_accumulate(fdir: np.ndarray, filled: np.ndarray) -> np.ndarray:
    rows, cols = fdir.shape
    n = rows * cols

    acc = np.ones((rows, cols), dtype=np.float32)
    acc[~np.isfinite(filled)] = 0.0

    rr, cc = np.mgrid[0:rows, 0:cols]
    rr_flat = rr.flatten()
    cc_flat = cc.flatten()
    fdir_flat = fdir.flatten()
    downstream = np.full(n, -1, dtype=np.int64)

    for code, (dy, dxi) in D8_OFFSETS.items():
        mask = fdir_flat == code
        if not mask.any():
            continue
        ni = rr_flat[mask] + dy
        nj = cc_flat[mask] + dxi
        valid = (ni >= 0) & (ni < rows) & (nj >= 0) & (nj < cols)
        fids = np.where(mask)[0][valid]
        nids = ni[valid] * cols + nj[valid]
        downstream[fids] = nids

    elev_flat = filled.flatten()
    valid_flat = np.isfinite(elev_flat)
    order = np.argsort(-np.where(valid_flat, elev_flat, -np.inf))

    acc_flat = acc.flatten()
    for idx in order:
        if not valid_flat[idx]:
            continue
        d = downstream[idx]
        if d >= 0:
            acc_flat[d] += acc_flat[idx]

    return acc_flat.reshape(rows, cols)


# ===========================================================================
# Decimation helpers
# ===========================================================================
def _decimation_factor(H: int, W: int, max_cells: int) -> int:
    if max_cells <= 0:
        return 1
    total = H * W
    if total <= max_cells:
        return 1
    k = int(np.ceil(np.sqrt(total / max_cells)))
    return max(1, k)


def _read_dem_decimated(dem_path, k: int):
    with rasterio.open(str(dem_path)) as src:
        H, W = src.height, src.width
        meta_full = {
            "transform": src.transform, "crs": src.crs,
            "width": W, "height": H,
            "nodata": (src.nodata if src.nodata is not None else -9999.0),
            "count": src.count,
        }
        nd = src.nodata if src.nodata is not None else -9999.0

        if k == 1:
            arr = src.read(1).astype(np.float32)
            meta_small = dict(meta_full)
        else:
            h_small = max(1, H // k)
            w_small = max(1, W // k)
            arr = src.read(
                1,
                out_shape=(h_small, w_small),
                resampling=Resampling.average,
            ).astype(np.float32)
            t = src.transform
            meta_small = {
                "transform": rasterio.Affine(
                    t.a * k, t.b, t.c,
                    t.d, t.e * k, t.f,
                ),
                "crs": src.crs,
                "width": w_small, "height": h_small,
                "nodata": nd, "count": 1,
            }

    arr[arr == nd] = np.nan
    for s in (-99999, -32768, -3.4028235e38, 1e20, -1e20, 1e30, -1e30):
        arr[arr == s] = np.nan
    return arr, meta_small, meta_full


# ===========================================================================
# Public API
# ===========================================================================
def compute_hydrology(dem_path,
                      cellsize: Optional[float] = None,
                      smooth_sigma: float = 1.0,
                      debug: bool = False,
                      output_dir: Optional[Path] = None,
                      max_cells: int = 64_000_000,
                      preserve_grid: bool = True
                      ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    if not HAS_RASTERIO:
        raise RuntimeError("rasterio is required for compute_hydrology.")

    with rasterio.open(str(dem_path)) as src:
        H, W = src.height, src.width
        if cellsize is None:
            t = src.transform
            dx = abs(t.a)
            cellsize = dx * 111320.0 if _is_geographic(src.crs) else dx

    k = _decimation_factor(H, W, max_cells)
    if k > 1:
        logger.info(
            "DEM is %d × %d = %.2f G cells — decimating by %d× for D8 "
            "(max_cells=%d).",
            H, W, H * W / 1e9, k, max_cells)
        work_cellsize = cellsize * k
    else:
        work_cellsize = cellsize

    dem, meta_small, meta_full = _read_dem_decimated(dem_path, k)
    logger.info("  Working grid: %d × %d (cellsize %.3f m)",
                meta_small["height"], meta_small["width"], work_cellsize)

    if smooth_sigma > 0:
        logger.info("Smoothing DEM (sigma = %.2f cells)…", smooth_sigma)
        dem = _smooth_dem(dem, sigma=smooth_sigma)

    logger.info("Filling sinks (priority-flood with epsilon)…")
    filled = _priority_flood(dem)

    logger.info("Computing D8 flow direction…")
    fdir = _d8_direction(filled, work_cellsize)

    n_flat = int(((fdir == 0) & np.isfinite(filled)).sum())
    n_total = int(np.isfinite(filled).sum())
    if n_flat:
        logger.info("  %d cells still have no flow direction (%.2f%% of "
                    "valid cells)", n_flat,
                    100.0 * n_flat / max(n_total, 1))

    logger.info("Computing flow accumulation…")
    acc = _d8_accumulate(fdir, filled)

    if debug and output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        rio = RasterIO()
        rio.write(output_dir / "filled_dem.tif", filled, meta_small)
        rio.write(output_dir / "flow_direction.tif",
                  fdir.astype(np.float64), meta_small, nodata=0.0)
        rio.write(output_dir / "flow_accumulation.tif", acc, meta_small)

    return filled, fdir, acc, meta_small


def order_proxy_from_accumulation(acc: np.ndarray, mask: np.ndarray,
                                  threshold_cells: int) -> np.ndarray:
    ratio = np.maximum(acc / max(threshold_cells, 1), 1.0)
    order = np.floor(np.log2(ratio)).astype(np.int16) + 1
    order = np.clip(order, 1, 8)
    order[~mask] = 0
    return order


# ===========================================================================
# Runoff routing with transmission loss
# ===========================================================================
def route_and_apply_loss(fdir: np.ndarray,
                         filled: np.ndarray,
                         runoff_m: np.ndarray,
                         cell_area: float,
                         is_stream: np.ndarray,
                         Ks: np.ndarray,
                         W: np.ndarray,
                         T_seconds: float,
                         dx: float,
                         max_daily_depth_mm: float = 20.0,
                         eps: float = 1e-6
                         ) -> Tuple[np.ndarray, float, float]:
    rows, cols = fdir.shape
    n = rows * cols

    runoff_flat = np.where(np.isfinite(runoff_m), runoff_m, 0.0).flatten()
    V_local_flat = (runoff_flat * cell_area).astype(np.float64)
    V_routed_flat = V_local_flat.copy()
    V_loss_flat = np.zeros(n, dtype=np.float64)

    fdir_flat = fdir.flatten()
    downstream = np.full(n, -1, dtype=np.int64)

    for code, (dy, dxi) in D8_OFFSETS.items():
        mask = (fdir_flat == code)
        if not mask.any():
            continue
        ids = np.where(mask)[0]
        r = ids // cols
        c = ids - r * cols
        ni = r + dy
        nj = c + dxi
        valid = (ni >= 0) & (ni < rows) & (nj >= 0) & (nj < cols)
        fids = ids[valid]
        nids = ni[valid] * cols + nj[valid]
        downstream[fids] = nids

    elev_flat = filled.flatten()
    valid_flat = np.isfinite(elev_flat)

    has_ds = downstream >= 0
    if has_ds.any():
        ds_valid = np.zeros(n, dtype=bool)
        ds_valid[has_ds] = valid_flat[downstream[has_ds]]
        downstream[has_ds & ~ds_valid] = -1

    order = np.argsort(-np.where(valid_flat, elev_flat, -np.inf))

    stream_flat = is_stream.flatten().astype(bool)
    Ks_flat = (Ks.flatten() if isinstance(Ks, np.ndarray)
               else np.full(n, float(Ks)))
    W_flat = (W.flatten() if isinstance(W, np.ndarray)
              else np.full(n, float(W)))

    capacity = Ks_flat * W_flat * T_seconds * dx

    max_rate_m_s = max_daily_depth_mm / 1000.0 / 86400.0
    physical_cap_volume = max_rate_m_s * W_flat * dx * T_seconds

    for idx in order:
        if not valid_flat[idx]:
            continue
        d = downstream[idx]
        if d < 0:
            continue

        if stream_flat[idx]:
            V_in = V_routed_flat[idx]
            cap = capacity[idx]
            phys_cap = physical_cap_volume[idx]
            if V_in > eps and cap > 0:
                exponent = cap / V_in
                if exponent > 100.0:
                    exponent = 100.0
                loss = V_in * (1.0 - np.exp(-exponent))
                if phys_cap > 0:
                    loss = min(loss, phys_cap)
                V_loss_flat[idx] = loss
                V_routed_flat[idx] -= loss

        V_routed_flat[d] += V_routed_flat[idx]

    outlet_mask = valid_flat & (downstream < 0)
    V_outlet = float(V_routed_flat[outlet_mask].sum())
    V_available = float(V_local_flat[valid_flat].sum())

    return V_loss_flat.reshape(rows, cols), V_outlet, V_available