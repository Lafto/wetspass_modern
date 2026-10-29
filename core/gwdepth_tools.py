# -*- coding: utf-8 -*-
"""
Groundwater-depth diagnostic.

Reads the monthly gwdepth rasters from the working directory and
returns per-month summary statistics. No interpolation is performed —
the user brings the gwdepth raster from QGIS or another tool.
"""

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("WetSpassModern")


def analyse_gwdepth(output_dir, working_dir):
    """
    Return per-month summary statistics of the gwdepth rasters.

    Returns dict {month: {"mean", "min", "max", "std",
                           "pct_lt_5m", "pct_lt_10m", "pct_gt_50m"}}.
    Empty dict if no rasters were found.
    """
    import rasterio
    out = {}
    gwd_dir = None
    for sub in ("Inputs/maps/gwdepth", "inputs/maps/gwdepth",
                "Maps/gwdepth", "maps/gwdepth"):
        p = Path(working_dir) / sub
        if p.is_dir():
            gwd_dir = p
            break
    if gwd_dir is None:
        return out

    for m in range(1, 13):
        for ext in (".tif", ".asc"):
            p = gwd_dir / f"gwdepth{m}{ext}"
            if not p.exists():
                continue
            try:
                with rasterio.open(str(p)) as src:
                    h8 = max(1, src.height // 8)
                    w8 = max(1, src.width // 8)
                    a = src.read(
                        1, out_shape=(h8, w8)).astype(np.float64)
                    nd = (src.nodata if src.nodata is not None
                          else -9999.0)
                    a = np.where(a == nd, np.nan, a)
                    a = np.where(np.isfinite(a), a, np.nan)
                    finite = a[np.isfinite(a)]
                    if finite.size == 0:
                        break
                    n = finite.size
                    out[m] = {
                        "mean":      float(finite.mean()),
                        "min":       float(finite.min()),
                        "max":       float(finite.max()),
                        "std":       float(finite.std()),
                        "pct_lt_5m":  float(
                            100.0 * (finite < 5).sum() / n),
                        "pct_lt_10m": float(
                            100.0 * (finite < 10).sum() / n),
                        "pct_gt_50m": float(
                            100.0 * (finite > 50).sum() / n),
                    }
            except Exception as exc:
                logger.warning("Could not read %s: %s", p, exc)
            break
    return out