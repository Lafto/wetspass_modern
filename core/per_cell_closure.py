# -*- coding: utf-8 -*-
"""
Per-cell water balance closure diagnostic.

Writes wb_error_annual.tif = P − AET − SR − R_total − ΔS
cell by cell, plus a small text summary (wb_error_summary.txt).

The closure equation is the Wetspass-M 2016 convention:
    closure = P − AET − SR − R_total − ΔS
where AET already contains interception, groundwater-fed ET, and
open-water evaporation. Interception is NOT subtracted again.
"""

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("WetSpassModern")


def _find_rain_dir(working_dir: Path):
    for sub in ("inputs/maps/rain", "Inputs/maps/rain",
                "maps/rain", "Maps/rain"):
        p = working_dir / sub
        if p.is_dir():
            return p
    return None


def compute_per_cell_closure(output_dir, working_dir):
    """
    Build the residual raster and return a summary dict.

    Required inputs (on the diffusive grid):
        Interception_annual.tif               — optional (not used in closure)
        Cell_evapotranspiration_annual.tif    — required
        Cell_runoff_annual.tif                — required
        Recharge_Total_annual.tif  (or
        Recharge_diffusive_annual.tif)        — required
        soilwater_storage_delta_annual.tif    — optional (assumed 0 if absent)
        inputs/maps/rain/rain1.tif … rain12.tif — required

    Output:
        <output_dir>/wb_error_annual.tif
    """
    try:
        import rasterio
        from rasterio.windows import Window
    except ImportError:
        raise RuntimeError("rasterio is required")

    output_dir = Path(output_dir)
    working_dir = Path(working_dir)

    # ---- Reference grid = the diffusive annual raster ----
    ref = output_dir / "Recharge_diffusive_annual.tif"
    if not ref.exists():
        raise FileNotFoundError(
            f"{ref} not found — run the diffusive model first.")

    rain_dir = _find_rain_dir(working_dir)
    if rain_dir is None:
        raise FileNotFoundError(
            "Rainfall folder not found under the working directory.")

    # ---- Required rasters ----
    AET_path = output_dir / "Cell_evapotranspiration_annual.tif"
    SR_path = output_dir / "Cell_runoff_annual.tif"
    Rt_path = output_dir / "Recharge_Total_annual.tif"
    if not Rt_path.exists():
        Rt_path = output_dir / "Recharge_diffusive_annual.tif"
    dS_path = output_dir / "soilwater_storage_delta_annual.tif"

    for p in (AET_path, SR_path, Rt_path):
        if not p.exists():
            raise FileNotFoundError(f"Missing required raster: {p}")

    # ---- Rain files ----
    rain_files = []
    for m in range(1, 13):
        for ext in (".tif", ".asc"):
            p = rain_dir / f"rain{m}{ext}"
            if p.exists():
                rain_files.append(p)
                break
    if not rain_files:
        raise FileNotFoundError(
            f"No rain*.tif files found in {rain_dir}")

    with rasterio.open(str(ref)) as src:
        H, W = src.height, src.width
        ref_nd = src.nodata if src.nodata is not None else -9999.0
        profile = src.profile.copy()
        profile.update(dtype="float32", nodata=-9999.0,
                       compress="deflate", tiled=True,
                       blockxsize=256, blockysize=256)

    out_path = output_dir / "wb_error_annual.tif"

    # ---- Accumulators for the summary ----
    total_sum = 0.0
    abs_sum = 0.0
    total_count = 0
    n_above_5 = 0
    n_above_10 = 0
    n_above_20 = 0
    min_val = None
    max_val = None

    tile = 1024

    def _read_window(path, win):
        with rasterio.open(str(path)) as src:
            a = src.read(1, window=win).astype(np.float64)
            nd = src.nodata if src.nodata is not None else -9999.0
            a = np.where(a == nd, np.nan, a)
            a = np.where(np.isfinite(a), a, np.nan)
        return a

    with rasterio.open(str(out_path), "w", **profile) as dst:
        for r0 in range(0, H, tile):
            h = min(tile, H - r0)
            for c0 in range(0, W, tile):
                w = min(tile, W - c0)
                win = Window(c0, r0, w, h)

                # Accumulate P over the 12 monthly files.
                P = None
                for rf in rain_files:
                    a = _read_window(rf, win)
                    if P is None:
                        P = np.zeros_like(a)
                    P = P + np.where(np.isfinite(a), a, 0.0)

                if P is None:
                    residual = np.full((h, w), np.nan)
                else:
                    AET = _read_window(AET_path, win)
                    SR = _read_window(SR_path, win)
                    Rt = _read_window(Rt_path, win)
                    if dS_path.exists():
                        dS = _read_window(dS_path, win)
                    else:
                        dS = np.zeros((h, w), dtype=np.float64)

                    # Wetspass-M 2016 closure (AET already includes I).
                    residual = P - (AET + SR + Rt + dS)

                # Mask out where the reference raster is nodata.
                with rasterio.open(str(ref)) as src:
                    ref_a = src.read(1, window=win).astype(np.float64)
                valid = (ref_a != ref_nd) & np.isfinite(ref_a)
                residual = np.where(valid, residual, np.nan)

                v = residual[np.isfinite(residual)]
                if v.size:
                    total_sum += float(v.sum())
                    abs_sum += float(np.abs(v).sum())
                    total_count += int(v.size)
                    n_above_5 += int((np.abs(v) > 5).sum())
                    n_above_10 += int((np.abs(v) > 10).sum())
                    n_above_20 += int((np.abs(v) > 20).sum())
                    vmin = float(v.min())
                    vmax = float(v.max())
                    min_val = vmin if min_val is None else min(min_val, vmin)
                    max_val = vmax if max_val is None else max(max_val, vmax)

                out_arr = np.where(np.isfinite(residual), residual, -9999.0)
                dst.write(out_arr.astype(np.float32), 1, window=win)

    summary = {
        "output_raster": str(out_path),
        "n_cells": total_count,
        "mean_residual": total_sum / total_count if total_count else 0.0,
        "mean_abs_residual": (abs_sum / total_count
                              if total_count else 0.0),
        "min_residual": min_val,
        "max_residual": max_val,
        "pct_abs_gt_5":  100.0 * n_above_5 / max(total_count, 1),
        "pct_abs_gt_10": 100.0 * n_above_10 / max(total_count, 1),
        "pct_abs_gt_20": 100.0 * n_above_20 / max(total_count, 1),
    }
    return summary


def write_closure_report(output_dir, summary):
    """Write a small text file next to wb_error_annual.tif."""
    output_dir = Path(output_dir)
    p = output_dir / "wb_error_summary.txt"
    lines = []
    lines.append("=" * 60)
    lines.append("Per-cell water balance residual")
    lines.append("=" * 60)
    lines.append(f"Raster       : {summary['output_raster']}")
    lines.append(f"Cells        : {summary['n_cells']:,}")
    lines.append(f"Mean         : {summary['mean_residual']:+.4f} mm/yr")
    lines.append(f"Mean |value| : {summary['mean_abs_residual']:.4f} mm/yr")
    lines.append(f"Min          : {summary['min_residual']}")
    lines.append(f"Max          : {summary['max_residual']}")
    lines.append("")
    lines.append("Fraction of cells with |residual| above threshold:")
    lines.append(f"  >  5 mm/yr : {summary['pct_abs_gt_5']:.2f} %")
    lines.append(f"  > 10 mm/yr : {summary['pct_abs_gt_10']:.2f} %")
    lines.append(f"  > 20 mm/yr : {summary['pct_abs_gt_20']:.2f} %")
    lines.append("")
    lines.append("Interpretation:")
    lines.append("  * If mean |value| >> mean, the residual is dominated")
    lines.append("    by a handful of outlier cells (e.g. boundary or")
    lines.append("    open-water cells).")
    lines.append("  * If mean and mean |value| are similar, the residual")
    lines.append("    is systematic and present everywhere.")
    lines.append("")
    lines.append("Formula used:  closure = P − AET − SR − R_total − ΔS")
    lines.append("(AET already includes interception, so I is not")
    lines.append("subtracted again.)")
    lines.append("=" * 60)
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)