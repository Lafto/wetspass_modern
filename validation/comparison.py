# -*- coding: utf-8 -*-
"""
WetSpass-M Modern — Validation
==============================
Cell-by-cell comparison between legacy (ASCII) and modern (GeoTIFF) outputs.
Computes RMSE, MAE, R², max absolute difference.
"""

import logging
from pathlib import Path

import numpy as np

try:
    import rasterio
except ImportError:
    rasterio = None

logger = logging.getLogger("WetSpassModern")


def _read_raster(path):
    path = Path(path)
    if path.suffix.lower() == ".asc":
        with open(path, "r") as f:
            hdr = {}
            for _ in range(6):
                parts = f.readline().split()
                hdr[parts[0].lower()] = float(parts[1])
            data = np.loadtxt(f)
        return data, {"nodata": hdr.get("nodata_value", -9999)}
    else:
        if rasterio is None:
            raise ImportError("rasterio required for GeoTIFF reading")
        with rasterio.open(path) as src:
            data = src.read(1).astype(np.float64)
            nd = src.nodata if src.nodata is not None else -9999
        return data, {"nodata": nd}


def compare_rasters(old_path, new_path, variable_name="unknown",
                    tolerance=1e-6):
    """
    Cell-by-cell comparison between legacy and modern results.

    Returns dict with status, RMSE, MAE, max_diff, R².
    """
    old, om = _read_raster(old_path)
    new, nm = _read_raster(new_path)

    if old.shape != new.shape:
        return {"variable": variable_name, "status": "FAILED",
                "error": f"shape {old.shape} vs {new.shape}"}

    nd = om.get("nodata", -9999)
    mask = (old != nd) & (new != nd) & np.isfinite(old) & np.isfinite(new)
    if not np.any(mask):
        return {"variable": variable_name, "status": "FAILED",
                "error": "no valid cells"}

    ov, nv = old[mask], new[mask]
    diff = nv - ov
    rmse = float(np.sqrt(np.mean(diff ** 2)))
    mae = float(np.mean(np.abs(diff)))
    maxd = float(np.max(np.abs(diff)))
    ss_res = float(np.sum(diff ** 2))
    ss_tot = float(np.sum((ov - np.mean(ov)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return {
        "variable": variable_name,
        "status": "PASSED" if maxd <= tolerance else "FAILED",
        "n_cells": int(np.sum(mask)),
        "rmse": rmse, "mae": mae, "max_diff": maxd, "r_squared": r2,
        "mean_old": float(np.mean(ov)), "mean_new": float(np.mean(nv)),
    }


def compare_directories(old_dir, new_dir, pattern="recharge",
                        tolerance=1e-6):
    """Compare all matching rasters between two directories."""
    old_dir = Path(old_dir); new_dir = Path(new_dir)
    old_files = list(old_dir.glob(f"{pattern}*.asc"))

    summary = {"total": 0, "passed": 0, "failed": 0, "details": []}
    for old_file in old_files:
        base = old_file.stem
        new_file = new_dir / f"{base}.tif"
        if not new_file.exists():
            new_file = new_dir / f"{pattern}.tif"
        if new_file.exists():
            result = compare_rasters(old_file, new_file, base, tolerance)
            summary["total"] += 1
            if result["status"] == "PASSED":
                summary["passed"] += 1
            else:
                summary["failed"] += 1
            summary["details"].append(result)

    summary["overall_status"] = (
        "PASSED" if summary["failed"] == 0 else "FAILED")
    return summary


def generate_report(results, output_path):
    """Write a validation report to a text file."""
    with open(output_path, "w") as f:
        f.write("=" * 80 + "\n")
        f.write("WetSpass-M Modern — Validation Report\n")
        f.write("=" * 80 + "\n\n")
        for r in results:
            f.write(f"Variable: {r.get('variable')}\n")
            f.write(f"  Status: {r.get('status')}\n")
            for k in ("n_cells", "rmse", "mae", "max_diff",
                      "r_squared", "mean_old", "mean_new"):
                if k in r:
                    f.write(f"  {k}: {r[k]}\n")
            f.write("\n")