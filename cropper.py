# -*- coding: utf-8 -*-
"""
cropper.py

Robust raster cropping to a vector boundary.

Why this exists:
    QGIS's `gdal:rasterize` / "Mask rasters" tools frequently fail with
    "Duplicate argument -ot" or CRS mismatch errors. This module bypasses
    the GDAL CLI entirely and uses rasterio + geopandas, which handle
    CRS reprojection and boundary-pixel retention correctly.

Features:
    - Auto-reprojects the boundary vector to match each raster's CRS
      (cached per CRS so it only happens once).
    - Uses all_touched=True to retain edge pixels.
    - Physically crops rasters to the boundary bounding box.
    - Sets pixels outside the polygon to NoData (-9999).
    - Preserves original rasters by writing cropped files to a
      subfolder named 'cropped_to_boundary'.
    - Accepts an optional `file_list` — only those files are cropped.
    - Reports progress via callbacks so the GUI stays responsive.
"""

import contextlib
import logging
from pathlib import Path
from typing import Iterable, Optional

import geopandas as gpd
import rasterio
from rasterio.mask import mask as rio_mask

logger = logging.getLogger("WetSpassModern")

EXCLUDE_FILES = {
    "soilwater_storage_initial.tif",
    "snow_store_state.tif",
}

DEFAULT_NODATA = -9999.0


def crop_rasters_to_boundary(input_dir, shapefile_path, output_dir=None,
                              file_list: Optional[Iterable[str]] = None,
                              progress_callback=None, log_callback=None):
    """
    Crop rasters in `input_dir` to the boundary polygon in
    `shapefile_path`.

    Parameters
    ----------
    input_dir : str | Path
    shapefile_path : str | Path
    output_dir : str | Path | None
        If None, cropped files are written to
        `input_dir / "cropped_to_boundary"`. Originals are preserved.
    file_list : iterable of str | Path | None
        Optional list of specific file names (basenames) or full paths
        to crop. If None, all .tif files are cropped. If provided, only
        the listed files are processed.
    progress_callback : callable(step, total, message) | None
    log_callback : callable(message) | None

    Returns
    -------
    (bool, str)
    """
    def _log(msg):
        logger.info(msg)
        if log_callback:
            with contextlib.suppress(Exception):
                log_callback(str(msg))

    def _progress(step, total, msg):
        if progress_callback:
            with contextlib.suppress(Exception):
                progress_callback(int(step), int(total), str(msg))

    input_path = Path(input_dir)
    shapefile_path = Path(shapefile_path)

    if not input_path.is_dir():
        msg = f"Input folder does not exist: {input_path}"
        _log(msg)
        return False, msg

    if not shapefile_path.exists():
        msg = f"Boundary file does not exist: {shapefile_path}"
        _log(msg)
        return False, msg

    # Default output: a subfolder so the originals are preserved.
    if output_dir:
        output_path = Path(output_dir)
    else:
        output_path = input_path / "cropped_to_boundary"
    output_path.mkdir(parents=True, exist_ok=True)

    # --- Read the boundary -------------------------------------------
    _log(f"Reading boundary: {shapefile_path}")
    _progress(0, 1, "Reading boundary shapefile…")
    try:
        gdf = gpd.read_file(shapefile_path)
        if gdf.empty:
            msg = "Boundary shapefile is empty."
            _log(msg)
            return False, msg
        _log(f"Boundary loaded. CRS = {gdf.crs}, "
             f"features = {len(gdf)}")
    except Exception as exc:
        msg = f"Failed to read boundary file: {exc}"
        _log(msg)
        return False, msg

    # --- Determine which files to crop --------------------------------
    if file_list is not None:
        resolved = []
        for entry in file_list:
            p = Path(entry)
            if not p.is_absolute():
                p = input_path / p.name if p.suffix else input_path / p
            # Try exact path first
            if p.exists() and p.suffix.lower() == ".tif":
                if p.name not in EXCLUDE_FILES:
                    resolved.append(p)
                continue
            # Try just the basename
            p2 = input_path / Path(entry).name
            if p2.exists() and p2.suffix.lower() == ".tif":
                if p2.name not in EXCLUDE_FILES:
                    resolved.append(p2)
        tif_files = resolved
    else:
        tif_files = [
            f for f in input_path.glob("*.tif")
            if f.name not in EXCLUDE_FILES
        ]

    if not tif_files:
        msg = "No .tif files selected for cropping."
        _log(msg)
        return False, msg

    total = len(tif_files)
    _log(f"Selected {total} rasters to crop.")
    _log(f"Cropped outputs will be written to: {output_path}")
    _progress(0, total, f"Selected {total} rasters.")

    # Cache the boundary per CRS
    _boundary_cache = {}

    def _boundary_for(crs):
        key = str(crs)
        if key not in _boundary_cache:
            if gdf.crs is not None and crs is not None and gdf.crs != crs:
                _log(f"Reprojecting boundary to {crs} "
                     f"(first file at this CRS)…")
                _boundary_cache[key] = gdf.to_crs(crs)
            else:
                _boundary_cache[key] = gdf
        return _boundary_cache[key]

    # --- Process each raster -----------------------------------------
    success = 0
    failed = 0
    failed_files = []

    for idx, tif in enumerate(tif_files, 1):
        out_file = output_path / tif.name
        step = idx - 1
        _progress(step, total, f"[{idx}/{total}] Cropping {tif.name}…")
        _log(f"[{idx}/{total}] Cropping {tif.name}")
        try:
            with rasterio.open(tif) as src:
                gdf_reproj = _boundary_for(src.crs)

                out_image, out_transform = rio_mask(
                    src,
                    gdf_reproj.geometry,
                    crop=True,
                    nodata=DEFAULT_NODATA,
                    all_touched=True,
                )

                out_meta = src.meta.copy()
                out_meta.update({
                    "driver": "GTiff",
                    "height": out_image.shape[1],
                    "width": out_image.shape[2],
                    "transform": out_transform,
                    "nodata": DEFAULT_NODATA,
                })

            with rasterio.open(out_file, "w", **out_meta) as dst:
                dst.write(out_image)

            _log(f"[{idx}/{total}]   ✓ {tif.name} -> "
                 f"{out_image.shape[2]}x{out_image.shape[1]} pixels")
            success += 1

        except Exception as exc:
            _log(f"[{idx}/{total}]   ✗ {tif.name} FAILED: {exc}")
            failed += 1
            failed_files.append(tif.name)

    _progress(total, total, "Cropping complete.")

    msg = (f"Cropping complete. Success: {success}/{total}, "
           f"Failed: {failed}. Output: {output_path}")
    if failed_files:
        preview = failed_files[:5]
        msg += f" Failed files: {preview}"
        if len(failed_files) > 5:
            msg += f" ... (+{len(failed_files) - 5} more)"
    _log(msg)
    return failed == 0, msg


def list_available_rasters(input_dir):
    """
    Return a sorted list of .tif basenames in `input_dir`, excluding
    state/checkpoint files.
    """
    input_path = Path(input_dir)
    if not input_path.is_dir():
        return []
    files = [
        f.name for f in input_path.glob("*.tif")
        if f.name not in EXCLUDE_FILES
    ]
    return sorted(files)