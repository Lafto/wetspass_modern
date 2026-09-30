# -*- coding: utf-8 -*-
"""
Stream shapefile I/O and rasterization
=======================================
Reads a line vector layer (streams) and rasterizes it onto the DEM grid
using QGIS's native gdal:rasterize algorithm.

Functions
---------
load_stream_layer(path)          → QgsVectorLayer (validated line layer)
rasterize_streams(src, dem, out) → path to written GeoTIFF
stream_mask(src, dem, out)       → (bool ndarray, meta)
cellsize_meters(meta)            → float
"""

import logging
from pathlib import Path
from typing import Tuple

import numpy as np

from qgis.core import QgsVectorLayer
from qgis import processing

from .raster_io import RasterIO, _is_geographic

logger = logging.getLogger("WetSpassModern")


# ===========================================================================
# Vector loading
# ===========================================================================
def load_stream_layer(stream_path) -> QgsVectorLayer:
    """Open a stream shapefile / GPKG / GeoJSON as a QgsVectorLayer."""
    stream_path = str(stream_path)
    layer = QgsVectorLayer(stream_path, "streams", "ogr")
    if not layer.isValid():
        raise FileNotFoundError(
            f"Could not open stream layer: {stream_path}")
    if layer.geometryType() != 1:   # 1 = Line geometry
        raise ValueError(
            f"Stream layer must be a line layer (got geometry type "
            f"{layer.geometryType()})")
    if layer.featureCount() == 0:
        raise ValueError("Stream layer contains no features")
    return layer


# ===========================================================================
# Rasterization
# ===========================================================================
def _extent_string_from_template(meta) -> str:
    """
    Build the EXTENT argument for gdal:rasterize from a DEM template.
    Format expected by QGIS:  "xmin,xmax,ymin,ymax [CRS]"
    """
    transform = meta.get("transform")
    if transform is None:
        raise ValueError("DEM metadata has no transform")

    xmin = transform[2]
    ymax = transform[5]
    xmax = xmin + transform[0] * meta["width"]
    ymin = ymax + transform[4] * meta["height"]

    extent = f"{xmin},{xmax},{ymin},{ymax}"

    crs = meta.get("crs")
    if crs is not None:
        try:
            authid = crs.authid()
            if authid:
                extent += f" [{authid}]"
        except Exception:
            pass
    return extent


def rasterize_streams(stream_path, dem_path, output_path,
                      burn_value: int = 1) -> str:
    """
    Rasterize a stream line layer onto the DEM grid.

    Uses `-at` (all-touched) so thin lines are not dropped when they
    cross a cell edge without hitting its center.

    Returns the output GeoTIFF path.
    """
    stream_path = str(stream_path)
    dem_path = str(dem_path)
    output_path = str(output_path)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    rio = RasterIO()
    _, meta = rio.read_template(dem_path)

    extent = _extent_string_from_template(meta)
    width = int(meta["width"])
    height = int(meta["height"])

    processing.run(
        "gdal:rasterize",
        {
            "INPUT": stream_path,
            "FIELD": None,
            "BURN": int(burn_value),
            "USE_Z": False,
            "UNITS": 1,                 # 1 = Georeferenced units
            "WIDTH": width,
            "HEIGHT": height,
            "EXTENT": extent,
            "NODATA": 0,
            "INIT": 0,
            "INVERT": False,
            "EXTRA": "-at -co COMPRESS=DEFLATE -co TILED=YES",
            "OUTPUT": output_path,
        },
        is_child_algorithm=True,
    )

    if not Path(output_path).exists():
        raise RuntimeError(
            f"gdal:rasterize did not produce {output_path}. "
            "Check that the vector CRS and the DEM extent overlap.")

    logger.info("Rasterized streams → %s", output_path)
    return output_path


def stream_mask(stream_path, dem_path, output_path) -> Tuple[np.ndarray, dict]:
    """
    Rasterize the stream network and return (boolean_mask, meta).
    mask is True on stream cells.
    """
    rasterize_streams(stream_path, dem_path, output_path, burn_value=1)
    rio = RasterIO()
    arr, meta = rio.read(output_path)
    mask = np.isfinite(arr) & (arr > 0)
    return mask, meta


# ===========================================================================
# Cell-size in metres
# ===========================================================================
def cellsize_meters(meta) -> float:
    """
    Return the cell size in metres.
    Warns (but does not fail) when the CRS is geographic.
    """
    transform = meta.get("transform")
    if transform is None:
        return 1.0

    dx = abs(transform[0])
    dy = abs(transform[4])
    crs = meta.get("crs")

    if _is_geographic(crs):
        # Approximate: 1 degree ≈ 111 320 m (at the equator)
        approx = dx * 111_320.0
        logger.warning(
            "DEM CRS is geographic. Cell size approximated as %.1f m. "
            "For accurate focused-recharge volumes, reproject the DEM "
            "to a projected CRS.", approx)
        return approx

    return 0.5 * (dx + dy)