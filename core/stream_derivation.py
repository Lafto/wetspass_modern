# -*- coding: utf-8 -*-
"""
Stream network derivation from a DEM, plus D8 hydrology helpers.
=================================================================
Robust chain for DEMs with flat areas (Rift Valley floors, lake margins):

    1. Optional Gaussian smoothing (removes DEM noise)
    2. Priority-flood with epsilon (breaks true flat surfaces)
    3. D8 with fallback to the LOWEST neighbour on flats
    4. Flow accumulation that preserves every cell's own contribution
    5. Routing that treats DEM-boundary cells as proper outlets
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

from .raster_io import RasterIO, _is_geographic

logger = logging.getLogger("WetSpassModern")


# ESRI convention
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
    """Light Gaussian smoothing to suppress DEM noise before flow routing."""
    if sigma <= 0:
        return dem
    mask = np.isfinite(dem)
    if not mask.any():
        return dem

    try:
        from scipy.ndimage import gaussian_filter
        vals = np.where(mask, dem, 0.0)
        smoothed = gaussian_filter(vals, sigma=sigma, mode="nearest")
        weights = gaussian_filter(mask.astype(np.float64),
                                   sigma=sigma, mode="nearest")
        with np.errstate(divide="ignore", invalid="ignore"):
            out = smoothed / weights
        out[~mask] = np.nan
        return out
    except ImportError:
        p = np.pad(dem, 1, mode="edge")
        out = np.zeros_like(dem)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out += p[1 + di:1 + di + dem.shape[0],
                         1 + dj:1 + dj + dem.shape[1]]
        return out / 9.0


# ===========================================================================
# Priority-flood with epsilon
# ===========================================================================
def _priority_flood(dem: np.ndarray, epsilon: float = 1e-4) -> np.ndarray:
    import heapq

    rows, cols = dem.shape
    filled = np.full((rows, cols), np.inf, dtype=np.float64)
    pq = []

    for i in range(rows):
        for j in (0, cols - 1):
            if np.isfinite(dem[i, j]):
                heapq.heappush(pq, (dem[i, j], i, j))
                filled[i, j] = dem[i, j]
    for j in range(cols):
        for i in (0, rows - 1):
            if np.isfinite(dem[i, j]) and not np.isfinite(filled[i, j]):
                heapq.heappush(pq, (dem[i, j], i, j))
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
                    heapq.heappush(pq, (new_elev, ni, nj))

    filled[~np.isfinite(dem)] = np.nan
    return filled


# ===========================================================================
# D8 flow direction
# ===========================================================================
def _d8_direction(filled: np.ndarray, cellsize: float = 1.0) -> np.ndarray:
    """
    D8 direction.

    Cells with a strictly lower neighbour route to the steepest of those.
    Cells with no strictly lower neighbour (flats after epsilon flood)
    route to the LOWEST neighbour — the one with the LARGEST positive
    difference (center − neighbour).
    """
    rows, cols = filled.shape
    p = np.pad(filled, 1, mode="edge")
    center = filled
    dist_diag = cellsize * 1.41421356

    differences = np.stack([
        center - p[1:-1, 2:],       # E
        center - p[2:,   2:],       # SE
        center - p[2:,   1:-1],     # S
        center - p[2:,   :-2],      # SW
        center - p[1:-1, :-2],      # W
        center - p[:-2,  :-2],      # NW
        center - p[:-2,  1:-1],     # N
        center - p[:-2,  2:],       # NE
    ])

    positive = np.maximum(differences, 0.0)
    max_idx = np.argmax(positive, axis=0)
    max_val = np.max(positive, axis=0)

    fdir = _D8_CODES[max_idx].astype(np.int16)

    # Flat fallback: route to the LOWEST neighbour
    no_down = (max_val <= 0) & np.isfinite(filled)
    if no_down.any():
        best_idx = np.argmax(differences, axis=0)
        fdir[no_down] = _D8_CODES[best_idx[no_down]].astype(np.int16)

    fdir[~np.isfinite(filled)] = 0
    return fdir


# ===========================================================================
# Flow accumulation
# ===========================================================================
def _d8_accumulate(fdir: np.ndarray, filled: np.ndarray) -> np.ndarray:
    rows, cols = fdir.shape
    n = rows * cols

    acc = np.ones((rows, cols), dtype=np.float64)
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
# Public API
# ===========================================================================
def compute_hydrology(dem_path,
                      cellsize: Optional[float] = None,
                      smooth_sigma: float = 1.0,
                      debug: bool = False,
                      output_dir: Optional[Path] = None
                      ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    rio = RasterIO()
    dem, meta = rio.read(dem_path, kind="dem")

    if cellsize is None:
        t = meta.get("transform")
        if t is not None:
            dx = abs(t[0])
            cellsize = dx * 111320.0 if _is_geographic(meta.get("crs")) else dx
        else:
            cellsize = 1.0

    if smooth_sigma > 0:
        logger.info("Smoothing DEM (sigma = %.2f cells)…", smooth_sigma)
        dem = _smooth_dem(dem, sigma=smooth_sigma)

    logger.info("Filling sinks (priority-flood with epsilon)…")
    filled = _priority_flood(dem)

    logger.info("Computing D8 flow direction…")
    fdir = _d8_direction(filled, cellsize)

    n_flat = int(((fdir == 0) & np.isfinite(filled)).sum())
    n_total = int(np.isfinite(filled).sum())
    if n_flat:
        logger.info("  %d cells still have no flow direction (%.2f%% of "
                    "valid cells)", n_flat, 100.0 * n_flat / max(n_total, 1))

    logger.info("Computing flow accumulation…")
    acc = _d8_accumulate(fdir, filled)

    if debug and output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        rio.write(output_dir / "filled_dem.tif", filled, meta)
        rio.write(output_dir / "flow_direction.tif",
                  fdir.astype(np.float64), meta, nodata=0.0)

    return filled, fdir, acc, meta


def derive_stream_mask(dem_path,
                       output_dir,
                       threshold_cells: int = 500,
                       cellsize: Optional[float] = None,
                       smooth_sigma: float = 1.0,
                       debug: bool = True
                       ) -> Tuple[np.ndarray, np.ndarray, dict]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filled, fdir, acc, meta = compute_hydrology(
        dem_path, cellsize,
        smooth_sigma=smooth_sigma,
        debug=debug, output_dir=output_dir)

    mask = acc >= threshold_cells
    mask &= np.isfinite(filled)

    n_stream = int(mask.sum())
    logger.info("Derived stream mask: %d cells (threshold=%d)",
                n_stream, threshold_cells)
    if n_stream == 0:
        raise RuntimeError(
            f"No stream cells found with threshold={threshold_cells}. "
            f"Try a lower threshold (typical: 100–2000 for a "
            f"1500×1800 DEM).")

    rio = RasterIO()
    rio.write(output_dir / "streams_from_dem.tif",
              mask.astype(np.float64), meta, nodata=0.0)
    rio.write(output_dir / "flow_accumulation.tif",
              acc, meta, nodata=-9999.0)

    return mask, acc, meta


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
    """
    Route runoff downstream and apply exponential transmission loss.

    Physical cap: the daily infiltration depth on each stream cell is
    additionally bounded by `max_daily_depth_mm`.

    Boundary handling: cells whose D8 direction points to a non-DEM cell
    (NaN in `filled`) are treated as outlets, so their routed volume is
    correctly counted rather than silently dropped.

    Returns
    -------
    V_loss      : ndarray [m³]
    V_outlet    : float   [m³]
    V_available : float   [m³]
    """
    rows, cols = fdir.shape
    n = rows * cols

    runoff_flat = np.where(np.isfinite(runoff_m), runoff_m, 0.0).flatten()
    V_local_flat = runoff_flat * cell_area
    V_routed_flat = V_local_flat.copy()
    V_loss_flat = np.zeros(n, dtype=np.float64)

    rr, cc = np.mgrid[0:rows, 0:cols]
    rr_flat = rr.flatten()
    cc_flat = cc.flatten()
    fdir_flat = fdir.flatten()
    downstream = np.full(n, -1, dtype=np.int64)

    for code, (dy, dxi) in D8_OFFSETS.items():
        mask = (fdir_flat == code)
        if not mask.any():
            continue
        ni = rr_flat[mask] + dy
        nj = cc_flat[mask] + dxi
        valid = (ni >= 0) & (ni < rows) & (nj >= 0) & (nj < cols)
        fids = np.where(mask)[0][valid]
        nids = ni[valid] * cols + nj[valid]
        downstream[fids] = nids

    # -----------------------------------------------------------------
    # NEW: DEM validity check
    #
    # A boundary cell of the DEM grid can have a D8 direction that
    # points to a pixel inside the raster but outside the *valid* DEM
    # (a NaN hole, or a corner where the mask is irregular). Those
    # downstream cells are never processed in the routing loop, so
    # anything routed to them is silently lost. Treat them as outlets
    # instead, so the mass balance closes.
    # -----------------------------------------------------------------
    elev_flat = filled.flatten()
    valid_flat = np.isfinite(elev_flat)

    has_ds = downstream >= 0
    if has_ds.any():
        ds_valid = np.zeros(n, dtype=bool)
        ds_valid[has_ds] = valid_flat[downstream[has_ds]]
        downstream[has_ds & ~ds_valid] = -1

    order = np.argsort(-np.where(valid_flat, elev_flat, -np.inf))

    stream_flat = is_stream.flatten().astype(bool)
    Ks_flat = Ks.flatten() if isinstance(Ks, np.ndarray) \
              else np.full(n, float(Ks))
    W_flat = W.flatten() if isinstance(W, np.ndarray) \
             else np.full(n, float(W))
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