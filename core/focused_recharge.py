# -*- coding: utf-8 -*-
"""
Focused (streambed) recharge estimation — universal, memory-bounded,
with auto-checkpointing.
"""

import contextlib
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

import numpy as np

try:
    import rasterio
    from rasterio.windows import Window
    from rasterio.warp import reproject, Resampling
    from rasterio.enums import Resampling as _RS
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False
    Window = None
    reproject = None
    Resampling = None

from qgis.core import QgsVectorFileWriter, QgsField, QgsFeature
from qgis.PyQt.QtCore import QVariant

from .raster_io import RasterIO, _is_geographic
from .workflow import read_named_column, resolve_input_dirs, _read_rows
from .stream_io import load_stream_layer, stream_mask, cellsize_meters
from .stream_derivation import (
    compute_hydrology, order_proxy_from_accumulation,
    route_and_apply_loss,
)
from .run_report import write_focused_run_report

logger = logging.getLogger("WetSpassModern")

FOCUSED_CHECKPOINT_NAME = "focused_recharge_checkpoint.json"
FOCUSED_CHECKPOINT_VERSION = 2
RUNNING_ANNUAL_NAME = "_focused_annual_running.npy"
HYDROLOGY_FILLED = "_hydrology_filled.tif"
HYDROLOGY_FDIR = "_hydrology_fdir.tif"
HYDROLOGY_ACC = "_hydrology_acc.tif"

D8_MAX_CELLS = 64_000_000
CHANNEL_WIDTH_MAX_M = 60.0

ORDER_WIDTH_M = {
    1:  0.5,  2:  1.0,  3:  2.0,  4:  4.0,
    5:  8.0,  6: 15.0,  7: 25.0,  8: 40.0,
}
DEFAULT_HIGH_ORDER_WIDTH = CHANNEL_WIDTH_MAX_M
DEFAULT_FLOW_DURATION_FRACTION = 0.3
DEFAULT_MAX_DAILY_DEPTH_MM = 20.0


# ===========================================================================
# Bounded-memory helpers
# ===========================================================================
def _read_decimated(path: Path, max_cells: int, kind: str = "default"):
    if not HAS_RASTERIO:
        raise RuntimeError("rasterio is required for _read_decimated.")
    with rasterio.open(str(path)) as src:
        H, W = src.height, src.width
        k = max(1, int(np.ceil(np.sqrt(H * W / max(1, max_cells)))))
        if k == 1:
            arr = src.read(1).astype(np.float32)
            meta_small = {
                "transform": src.transform, "crs": src.crs,
                "width": W, "height": H,
                "nodata": (src.nodata if src.nodata is not None
                           else -9999.0),
                "count": 1,
            }
        else:
            h_small = max(1, H // k)
            w_small = max(1, W // k)
            arr = src.read(
                1,
                out_shape=(h_small, w_small),
                resampling=_RS.average,
            ).astype(np.float32)
            t = src.transform
            meta_small = {
                "transform": rasterio.Affine(
                    t.a * k, t.b, t.c,
                    t.d, t.e * k, t.f,
                ),
                "crs": src.crs,
                "width": w_small, "height": h_small,
                "nodata": (src.nodata if src.nodata is not None
                           else -9999.0),
                "count": 1,
            }
    nd = meta_small["nodata"]
    arr[arr == nd] = np.nan
    for s in (-99999, -32768, -3.4028235e38, 1e20, -1e20, 1e30, -1e30):
        arr[arr == s] = np.nan
    return arr, meta_small


def _find_dimensions(dem_path) -> str:
    try:
        with rasterio.open(str(dem_path)) as src:
            return f"{src.height} × {src.width}"
    except Exception as exc:
        logger.debug("_find_dimensions: %s", exc)
        return "unknown"


def _diffusive_reference_meta(output_dir: Path) -> Optional[dict]:
    p = Path(output_dir) / "Recharge_diffusive_annual.tif"
    if not p.exists():
        return None
    with rasterio.open(str(p)) as src:
        return {
            "transform": src.transform, "crs": src.crs,
            "width": src.width, "height": src.height,
            "nodata": (src.nodata if src.nodata is not None
                       else -9999.0),
            "count": 1,
        }


def _upsample_array_to(src_arr: np.ndarray,
                       src_meta: dict,
                       dst_meta: dict,
                       resampling=Resampling.bilinear,
                       fill_value: float = 0.0) -> np.ndarray:
    if (src_arr.shape == (dst_meta["height"], dst_meta["width"])
            and src_meta["transform"] == dst_meta["transform"]
            and src_meta["crs"] == dst_meta["crs"]):
        return src_arr.astype(np.float32, copy=False)
    dst = np.full((dst_meta["height"], dst_meta["width"]),
                  fill_value, dtype=np.float32)
    reproject(
        source=src_arr.astype(np.float32),
        destination=dst,
        src_transform=src_meta["transform"],
        src_crs=src_meta["crs"],
        src_nodata=np.nan,
        dst_transform=dst_meta["transform"],
        dst_crs=dst_meta["crs"],
        dst_nodata=np.nan,
        resampling=resampling,
    )
    return dst


def _write_upsampled_tiled(src_arr: np.ndarray,
                           src_meta: dict,
                           dst_meta: dict,
                           out_path,
                           resampling=Resampling.bilinear,
                           tile: int = 2048,
                           nodata: float = -9999.0,
                           valid_mask: Optional[np.ndarray] = None) -> None:
    """Write a full-grid GeoTIFF tile-by-tile (peak ~16 MB)."""
    import rasterio as _rio
    from rasterio.windows import Window as _Win
    H = dst_meta["height"]
    W = dst_meta["width"]
    profile = {
        "driver": "GTiff", "dtype": "float32", "nodata": nodata,
        "width": W, "height": H, "count": 1,
        "crs": dst_meta["crs"], "transform": dst_meta["transform"],
        "compress": "deflate", "tiled": True,
        "blockxsize": 256, "blockysize": 256,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    src_f32 = np.asarray(src_arr, dtype=np.float32)
    with _rio.open(str(out_path), "w", **profile) as dst:
        for r0 in range(0, H, tile):
            h = min(tile, H - r0)
            for c0 in range(0, W, tile):
                w = min(tile, W - c0)
                win = _Win(c0, r0, w, h)
                dst_t = _rio.windows.transform(win, dst_meta["transform"])
                buf = np.full((h, w), np.nan, dtype=np.float32)
                reproject(
                    source=src_f32,
                    destination=buf,
                    src_transform=src_meta["transform"],
                    src_crs=src_meta["crs"],
                    src_nodata=np.nan,
                    dst_transform=dst_t,
                    dst_crs=dst_meta["crs"],
                    dst_nodata=np.nan,
                    resampling=resampling,
                )
                if valid_mask is not None:
                    vm = valid_mask[r0:r0 + h, c0:c0 + w]
                    buf = np.where(vm, buf, np.nan)
                buf = np.where(np.isfinite(buf), buf, nodata)
                dst.write(buf.astype(np.float32), 1, window=win)
                del buf


def _sample_array_onto_dst_grid(src_arr, src_meta, dst_meta,
                                resampling=Resampling.nearest,
                                fill_value: float = 0.0) -> np.ndarray:
    return _upsample_array_to(src_arr, src_meta, dst_meta,
                              resampling=resampling,
                              fill_value=fill_value)


def _compute_strahler_order(layer, snap_tolerance=None) -> Dict[int, int]:
    if snap_tolerance is None:
        crs = layer.crs()
        snap_tolerance = 1e-7 if (crs and crs.isGeographic()) else 1.0
    features = list(layer.getFeatures())
    if not features:
        return {}
    def node_key(pt):
        return (round(pt.x() / snap_tolerance),
                round(pt.y() / snap_tolerance))
    ends_at, feat_nodes = {}, {}
    for f in features:
        line = f.geometry().asPolyline()
        if len(line) < 2:
            continue
        s, e = node_key(line[0]), node_key(line[-1])
        feat_nodes[f.id()] = (s, e)
        ends_at.setdefault(e, []).append(f.id())
    upstream_of = {}
    for fid, (s, _) in feat_nodes.items():
        ups = list(ends_at.get(s, []))
        if fid in ups:
            ups.remove(fid)
        upstream_of[fid] = ups
    order = {}
    for _ in range(len(features) + 5):
        changed = False
        for fid in feat_nodes:
            if fid in order:
                continue
            ups = upstream_of.get(fid, [])
            if not ups:
                order[fid] = 1
                changed = True
            elif all(u in order for u in ups):
                vals = [order[u] for u in ups]
                mx = max(vals)
                order[fid] = mx if vals.count(mx) == 1 else mx + 1
                changed = True
        if not changed:
            break
    for fid in feat_nodes:
        order.setdefault(fid, 1)
    return order


# ===========================================================================
# Engine
# ===========================================================================
class FocusedRechargeEngine:

    def __init__(self, working_dir, output_dir,
                 start_step: int = 1, end_step: int = 12,
                 stream_path: Optional[str] = None,
                 stream_source: str = "dem",
                 dem_stream_threshold: int = 500,
                 Ks: float = 1e-6,
                 channel_width_m: float = 2.0,
                 gwdepth_threshold_m: float = 3.0,
                 environment: str = "semi-arid",
                 ks_mode: str = "from_soil",
                 width_mode: str = "from_order",
                 delta_factor: float = 1.0,
                 max_daily_depth_mm: float = DEFAULT_MAX_DAILY_DEPTH_MM,
                 flow_duration_fraction: float = DEFAULT_FLOW_DURATION_FRACTION,
                 auto_checkpoint: bool = True,
                 resume_from_checkpoint: bool = False):
        self.working_dir = Path(working_dir)
        self.output_dir = Path(output_dir)
        self.stream_path = Path(stream_path) if stream_path else None
        self.stream_source = stream_source.lower()
        self.dem_stream_threshold = int(dem_stream_threshold)
        self.start_step = start_step
        self.end_step = end_step
        self.Ks_fixed = float(Ks)
        self.W_fixed = float(channel_width_m)
        self.gwdepth_threshold = float(gwdepth_threshold_m)
        self.environment = (environment or "mixed").lower()
        self.ks_mode = ks_mode
        self.width_mode = width_mode
        self.delta_factor = float(delta_factor)
        self.max_daily_depth_mm = float(max_daily_depth_mm)
        self.flow_duration_fraction = float(flow_duration_fraction)
        self.auto_checkpoint = bool(auto_checkpoint)
        self._resume_from_checkpoint = bool(resume_from_checkpoint)
        self.maps_dir, self.tables_dir, _ = resolve_input_dirs(self.working_dir)
        self._stream_mask = None
        self._meta = None
        self._meta_full = None
        self._meta_out = None
        self._cellsize_m = 1.0
        self._rainy_days = []
        self._monthly_results = []
        self._ks_raster = None
        self._w_raster = None
        self._hydrology = None
        self._valid_mask_out = None
        self._checkpoint_path = self.output_dir / FOCUSED_CHECKPOINT_NAME
        self._running_annual_path = self.output_dir / RUNNING_ANNUAL_NAME
        self._checkpoint_created_at = datetime.now().isoformat(
            timespec="seconds")
        self.progress_callback: Optional[Callable] = None

    def _report(self, step, total, msg):
        if self.progress_callback:
            with contextlib.suppress(Exception):
                self.progress_callback(step, total, msg)
        logger.info("[Focused %d/%d] %s", step, total, msg)

    def _focused_params_hash(self) -> str:
        tokens = [
            str(self.start_step), str(self.end_step),
            str(self.stream_path) if self.stream_path else "",
            self.stream_source, str(self.dem_stream_threshold),
            f"{self.Ks_fixed}", f"{self.W_fixed}",
            f"{self.gwdepth_threshold}", self.environment,
            self.ks_mode, self.width_mode,
            f"{self.delta_factor}", f"{self.max_daily_depth_mm}",
            f"{self.flow_duration_fraction}",
            f"wmax={CHANNEL_WIDTH_MAX_M}",
        ]
        return hashlib.sha256("|".join(tokens).encode()).hexdigest()[:16]

    @staticmethod
    def read_checkpoint(output_dir):
        p = Path(output_dir) / FOCUSED_CHECKPOINT_NAME
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Could not read focused checkpoint %s: %s",
                           p, exc)
            return None

    @staticmethod
    def discard_checkpoint(output_dir):
        p = Path(output_dir) / FOCUSED_CHECKPOINT_NAME
        if p.exists():
            with contextlib.suppress(OSError):
                p.unlink()
        npy = Path(output_dir) / RUNNING_ANNUAL_NAME
        if npy.exists():
            with contextlib.suppress(OSError):
                npy.unlink()

    def _save_checkpoint(self, next_month, completed_months, counters):
        data = {
            "version": FOCUSED_CHECKPOINT_VERSION,
            "created_at": self._checkpoint_created_at,
            "paused_at": datetime.now().isoformat(timespec="seconds"),
            "working_dir": str(self.working_dir),
            "output_dir": str(self.output_dir),
            "start_step": self.start_step,
            "end_step": self.end_step,
            "stream_path": str(self.stream_path) if self.stream_path else None,
            "stream_source": self.stream_source,
            "dem_stream_threshold": self.dem_stream_threshold,
            "Ks": self.Ks_fixed,
            "channel_width_m": self.W_fixed,
            "gwdepth_threshold_m": self.gwdepth_threshold,
            "environment": self.environment,
            "ks_mode": self.ks_mode,
            "width_mode": self.width_mode,
            "delta_factor": self.delta_factor,
            "max_daily_depth_mm": self.max_daily_depth_mm,
            "flow_duration_fraction": self.flow_duration_fraction,
            "params_hash": self._focused_params_hash(),
            "completed_months": sorted(completed_months),
            "next_month": int(next_month),
            "total_focused_m3": counters["total_focused_m3"],
            "total_available_m3": counters["total_available_m3"],
            "total_outlet_m3": counters["total_outlet_m3"],
            "streambed_area_running": counters["streambed_area_running"],
            "total_losing_cell_months": counters["total_losing_cell_months"],
            "total_stream_cell_months": counters["total_stream_cell_months"],
            "monthly_results": self._monthly_results,
            "status": "running",
        }
        tmp = self._checkpoint_path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(str(tmp), str(self._checkpoint_path))
            logger.info(
                "[Focused checkpoint] saved: next_month=%s completed=%s",
                next_month, sorted(completed_months))
        except Exception as exc:
            logger.error("Failed to save focused checkpoint: %s", exc)

    def _find_dem(self):
        for ext in (".tif", ".asc"):
            p = self.maps_dir / f"dem{ext}"
            if p.exists():
                return p
        raise FileNotFoundError(f"DEM not found in {self.maps_dir}")

    def _find_soil(self):
        for ext in (".tif", ".asc"):
            p = self.maps_dir / f"soil{ext}"
            if p.exists():
                return p
        return None

    def _find_gwdepth(self, month):
        for ext in (".tif", ".asc"):
            p = self.maps_dir / "gwdepth" / f"gwdepth{month}{ext}"
            if p.exists():
                return p
        raise FileNotFoundError(f"gwdepth not found for month {month}")

    def _find_cell_runoff(self, month):
        p = self.output_dir / f"Cell_runoff_{month}.tif"
        if not p.exists():
            raise FileNotFoundError(
                f"Cell_runoff_{month}.tif not found. "
                f"Run the main model first.")
        return p

    def _load_rainy_days(self):
        p = self.tables_dir / "RainyDaysPerMonth.TBL"
        if p.exists():
            self._rainy_days = read_named_column(
                p, ["RainyDays", "Rainydays", "rainy_days"])
        if not self._rainy_days:
            self._rainy_days = [10.0] * 12

    def _get_hydrology(self):
        if self._hydrology is not None:
            return self._hydrology
        filled_path = self.output_dir / HYDROLOGY_FILLED
        fdir_path = self.output_dir / HYDROLOGY_FDIR
        acc_path = self.output_dir / HYDROLOGY_ACC
        if (filled_path.exists() and fdir_path.exists()
                and acc_path.exists()):
            self._report(0, 0,
                         "Reusing cached D8 hydrology from previous run…")
            try:
                filled, meta_small = _read_decimated(
                    filled_path, D8_MAX_CELLS, kind="dem")
                self._report(0, 0,
                             f"  filled  : {meta_small['height']} × "
                             f"{meta_small['width']}")
                fdir_f, _ = _read_decimated(fdir_path, D8_MAX_CELLS)
                fdir = np.where(np.isfinite(fdir_f),
                                fdir_f, 0).astype(np.int16)
                self._report(0, 0, f"  fdir    : {fdir.shape}")
                acc, _ = _read_decimated(acc_path, D8_MAX_CELLS)
                self._report(0, 0, f"  acc     : {acc.shape}")
                self._cellsize_m = cellsize_meters(meta_small)
                self._hydrology = (filled, fdir, acc, meta_small)
                self._report(0, 0, "  cached hydrology loaded OK.")
                return self._hydrology
            except Exception as exc:
                import traceback
                logger.warning(
                    "Cached hydrology unusable (%s: %s) — "
                    "recomputing.\n%s",
                    type(exc).__name__, exc, traceback.format_exc())
                self._hydrology = None
        dem_path = self._find_dem()
        self._report(0, 0, "Computing Multi-Flow (MFD) hydrology from DEM "
                            "(priority-flood + MFD routing)…")
        try:
            filled, fdir, acc, meta = compute_hydrology(
                dem_path, smooth_sigma=0.0, debug=False, output_dir=None,
                max_cells=D8_MAX_CELLS, preserve_grid=False)
        except MemoryError as exc:
            logger.error("Out of memory during D8 recompute: %s", exc)
            raise RuntimeError(
                "Out of memory during D8 hydrology. The DEM is "
                f"{_find_dimensions(dem_path)} cells.") from exc
        self._cellsize_m = cellsize_meters(meta)
        self._hydrology = (filled, fdir, acc, meta)
        try:
            rio = RasterIO()
            rio.write(filled_path, filled, meta, nodata=-9999.0)
            rio.write(fdir_path, fdir.astype(np.float64), meta, nodata=0.0)
            rio.write(acc_path, acc, meta, nodata=-9999.0)
            logger.info(
                "Cached D8 hydrology rasters (%d × %d).",
                meta["height"], meta["width"])
        except Exception as exc:
            logger.warning("Could not cache hydrology: %s", exc)
        return self._hydrology

    def _build_Ks_raster(self, target_meta=None):
        soil_path = self._find_soil()
        if soil_path is None:
            return None, {}
        table_path = self.tables_dir / "Soil.TBL"
        if not table_path.exists():
            return None, {}
        try:
            rows = list(_read_rows(table_path))
        except Exception as exc:
            logger.debug("_build_Ks_raster: cannot read Soil.TBL: %s", exc)
            return None, {}
        if len(rows) < 2:
            return None, {}
        header = [h.strip() for h in rows[0]]
        def norm(s):
            return s.lower().replace(" ", "").replace("_", "")
        candidates = {"ks", "ksat", "ksatms", "k_s", "sathydcond",
                      "saturatedhydraulicconductivity", "hydcond",
                      "permeability"}
        col_idx = -1
        matched = ""
        for i, name in enumerate(header):
            if norm(name) in candidates:
                col_idx = i
                matched = name
                break
        if col_idx < 0:
            return None, {}
        code_to_ks = {}
        for row in rows[1:]:
            if len(row) <= col_idx:
                continue
            try:
                code = int(float(row[0]))
                ks = float(row[col_idx])
                if ks > 0:
                    code_to_ks[code] = ks
            except ValueError:
                continue
        if not code_to_ks:
            return None, {}
        self._report(0, 0,
                     f"Reading soil raster decimated for K_s lookup "
                     f"({len(code_to_ks)} classes, column '{matched}')…")
        soil, soil_meta = _read_decimated(soil_path, D8_MAX_CELLS,
                                            kind="default")

        # Resample the soil-derived Ks raster to match the hydrology
        # grid if the shapes differ.
        if target_meta is not None:
            target_shape = (target_meta["height"], target_meta["width"])
            if soil.shape != target_shape:
                self._report(0, 0,
                             f"Resampling Ks raster from {soil.shape} "
                             f"to {target_shape} to match hydrology grid…")
                soil = _upsample_array_to(
                    soil, soil_meta, target_meta,
                    resampling=Resampling.nearest, fill_value=np.nan)

        ks_raster = np.full(soil.shape, np.nan, dtype=np.float32)
        for code, ks in code_to_ks.items():
            ks_raster[soil == code] = ks
        logger.info("K_s from Soil.TBL column '%s' — %d classes.",
                    matched, len(code_to_ks))
        return ks_raster, code_to_ks

    def _build_stream_mask(self, dem_path):
        if self.stream_source == "dem":
            self._report(0, 0,
                         f"Thresholding D8 accumulation "
                         f"(threshold={self.dem_stream_threshold})…")
            _, _, acc, _ = self._get_hydrology()
            mask = (acc >= self.dem_stream_threshold)
            mask &= np.isfinite(acc)
            if int(mask.sum()) == 0:
                raise RuntimeError(
                    f"No stream cells at threshold="
                    f"{self.dem_stream_threshold}.")
            return mask
        tmp = self.output_dir / "_stream_mask_tmp.tif"
        mask, _ = stream_mask(self.stream_path, dem_path, tmp)
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
        return mask

    def _build_width_raster(self, meta, stream_mask):
        H, W = meta["height"], meta["width"]
        shape = (H, W)
        if stream_mask.shape != shape:
            raise RuntimeError(
                f"Stream mask shape {stream_mask.shape} != D8 {shape}.")
        if self.width_mode == "constant":
            return np.full(shape, self.W_fixed, dtype=np.float32)
        if self.stream_source == "dem":
            _, _, acc, _ = self._get_hydrology()
            if acc.shape != shape:
                raise RuntimeError("Accumulation shape mismatch.")
            order = order_proxy_from_accumulation(
                acc, stream_mask, self.dem_stream_threshold)
            w_raster = np.zeros(shape, dtype=np.float32)
            for ord_val, base_w in ORDER_WIDTH_M.items():
                w = base_w * self.delta_factor if ord_val >= 6 else base_w
                w_raster[order == ord_val] = w
            w_raster[order > 8] = (CHANNEL_WIDTH_MAX_M
                                    * self.delta_factor)
            w_raster[~stream_mask] = np.nan
            w_raster[w_raster == 0] = np.nan
            return w_raster

        layer = load_stream_layer(self.stream_path)
        fields = {f.name().lower(): f.name() for f in layer.fields()}
        width_of_fid = {}
        if self.width_mode == "from_attribute":
            wcol = None
            for cand in ("width", "width_m", "channel_width", "w"):
                if cand in fields:
                    wcol = fields[cand]
                    break
            if wcol is None:
                self.width_mode = "from_order"
        if self.width_mode == "from_attribute":
            for f in layer.getFeatures():
                try:
                    width_of_fid[f.id()] = float(f[wcol])
                except (TypeError, ValueError):
                    width_of_fid[f.id()] = self.W_fixed
        else:
            order_col = None
            for cand in ("order", "strahler", "stream_order",
                         "strahlerord", "ord_stra", "streamorder"):
                if cand in fields:
                    order_col = fields[cand]
                    break
            if order_col:
                order_of_fid = {}
                for f in layer.getFeatures():
                    try:
                        order_of_fid[f.id()] = int(f[order_col])
                    except (TypeError, ValueError):
                        order_of_fid[f.id()] = 1
            else:
                order_of_fid = _compute_strahler_order(layer)
            for fid, o in order_of_fid.items():
                base = ORDER_WIDTH_M.get(o, CHANNEL_WIDTH_MAX_M)
                if o >= 6:
                    base *= self.delta_factor
                width_of_fid[fid] = base

        from qgis import processing
        temp_shp = self.output_dir / "_temp_width.shp"
        temp_raster = self.output_dir / "_temp_width.tif"
        all_fields = layer.fields()
        new_fields = [QgsField("__width__", QVariant.Double)]
        writer = QgsVectorFileWriter(
            str(temp_shp), "UTF-8",
            all_fields + new_fields, layer.wkbType(),
            layer.crs(), "ESRI Shapefile")
        for f in layer.getFeatures():
            nf = QgsFeature()
            nf.setGeometry(f.geometry())
            nf.setFields(all_fields + new_fields, True)
            for i, a in enumerate(f.attributes()):
                nf.setAttribute(i, a)
            nf.setAttribute("__width__",
                            width_of_fid.get(f.id(), self.W_fixed))
            writer.addFeature(nf)
        del writer

        # Build a single-line CRS string for the EXTENT token.
        crs_wkt_str = ""
        if meta.get("crs") is not None:
            try:
                crs_wkt_str = meta["crs"].to_wkt()
            except AttributeError:
                try:
                    crs_wkt_str = meta["crs"].wkt
                except Exception as exc:
                    logger.debug("crs.wkt fallback failed: %s", exc)
                    crs_wkt_str = str(meta["crs"])

        # Note: -ot is passed via DATA_TYPE, not via EXTRA. Adding it in
        # both places caused a "Duplicate argument -ot" error.
        processing.run("gdal:rasterize", {
            "INPUT": str(temp_shp), "FIELD": "__width__", "BURN": 0,
            "USE_Z": False, "UNITS": 1,
            "WIDTH": int(meta["width"]), "HEIGHT": int(meta["height"]),
            "NODATA": 0, "INIT": 0, "INVERT": False,
            "DATA_TYPE": 5,  # Float32
            "EXTRA": (
                f"-te {meta['transform'][2]} "
                f"{meta['transform'][2] + meta['transform'][0] * meta['width']} "
                f"{meta['transform'][5] + meta['transform'][4] * meta['height']} "
                f"{meta['transform'][5]} "
                f"-tr {meta['transform'][0]} {-meta['transform'][4]} "
                f"-a_srs {crs_wkt_str} "
                f"-co COMPRESS=DEFLATE"),
            "OUTPUT": str(temp_raster),
        })

        rio = RasterIO()
        w, _ = rio.read(temp_raster)
        w = np.where(np.isfinite(w) & (w > 0), w, np.nan)
        for suffix in ("", ".dbf", ".shx", ".prj", ".cpg"):
            p = Path(str(temp_shp) + suffix)
            if p.exists():
                with contextlib.suppress(OSError):
                    p.unlink()
        if temp_raster.exists():
            with contextlib.suppress(OSError):
                temp_raster.unlink()
        return w

    def check_prerequisites(self):
        r = {"stream_ok": False, "stream_message": "",
             "months_ok": False, "months_message": "",
             "gwdepth_ok": False, "gwdepth_message": "",
             "ks_ok": False, "ks_message": "",
             "width_ok": False, "width_message": "",
             "overall_ok": False}
        if self.stream_source == "dem":
            try:
                self._find_dem()
                r["stream_ok"] = True
                r["stream_message"] = (
                    f"Streams derived from DEM "
                    f"(threshold = {self.dem_stream_threshold} cells)")
            except Exception as exc:
                r["stream_message"] = str(exc)
                return r
        else:
            try:
                layer = load_stream_layer(self.stream_path)
                r["stream_ok"] = True
                r["stream_message"] = f"OK — {layer.featureCount()} features"
            except Exception as exc:
                r["stream_message"] = str(exc)
                return r
        missing = [m for m in range(self.start_step, self.end_step + 1)
                   if not (self.output_dir
                           / f"Cell_runoff_{m}.tif").exists()]
        if missing:
            r["months_message"] = f"Missing Cell_runoff for: {missing}"
        else:
            r["months_ok"] = True
            n = self.end_step - self.start_step + 1
            r["months_message"] = f"OK — {n} monthly rasters present"
        missing_gw = []
        for m in range(self.start_step, self.end_step + 1):
            try:
                self._find_gwdepth(m)
            except FileNotFoundError:
                missing_gw.append(m)
        if missing_gw:
            r["gwdepth_message"] = f"Missing gwdepth for: {missing_gw}"
        else:
            r["gwdepth_ok"] = True
            r["gwdepth_message"] = "OK — all monthly gwdepth present"
        if self.ks_mode == "from_soil":
            _, cmap = self._build_Ks_raster()
            if cmap:
                r["ks_ok"] = True
                r["ks_message"] = (
                    f"K_s from Soil.TBL — {len(cmap)} classes")
            else:
                r["ks_ok"] = True
                r["ks_message"] = (
                    f"No K_s column → fixed {self.Ks_fixed:.1e} m/s")
        else:
            r["ks_ok"] = True
            r["ks_message"] = f"Fixed K_s = {self.Ks_fixed:.1e} m/s"
        if self.width_mode == "constant":
            r["width_ok"] = True
            r["width_message"] = f"Fixed width = {self.W_fixed:.2f} m"
        else:
            r["width_ok"] = True
            r["width_message"] = (
                f"Width from {self.width_mode}; max = "
                f"{CHANNEL_WIDTH_MAX_M:.0f} m")
        r["overall_ok"] = all([r["stream_ok"], r["months_ok"],
                                r["gwdepth_ok"], r["ks_ok"],
                                r["width_ok"]])
        return r

    def auto_detect_environment(self):
        rio = RasterIO()
        dem = self._find_dem()
        _, meta_full = rio.read_template(dem)
        self._meta_full = meta_full
        mask = self._build_stream_mask(dem)
        self._stream_mask = mask
        n = int(mask.sum())
        if n == 0:
            return {"environment": "unknown",
                    "message": "No stream cells found.",
                    "mean_gwdepth": None, "n_stream_cells": 0}
        depths = []
        for m in range(self.start_step, self.end_step + 1):
            try:
                gw, gw_meta = rio.read(self._find_gwdepth(m),
                                        kind="gwdepth")
                gw_small = _sample_array_onto_dst_grid(
                    gw, gw_meta, self._hydrology[3],
                    resampling=Resampling.bilinear, fill_value=np.nan)
                v = mask & np.isfinite(gw_small)
                if v.any():
                    depths.append(float(np.mean(gw_small[v])))
            except Exception as exc:
                logger.debug(
                    "auto_detect_environment: skipping month %s: %s",
                    m, exc)
        if not depths:
            return {"environment": "unknown",
                    "message": "Could not read gwdepth.",
                    "mean_gwdepth": None, "n_stream_cells": n}
        mean_depth = float(np.mean(depths))
        if mean_depth >= 10.0:
            suggested = "arid"
        elif mean_depth >= self.gwdepth_threshold:
            suggested = "semi-arid"
        else:
            suggested = "mixed"
        return {"environment": suggested,
                "message": (f"Mean gwdepth under streams: "
                            f"{mean_depth:.2f} m"),
                "mean_gwdepth": mean_depth, "n_stream_cells": n}

    def run(self):
        if self.environment == "humid":
            logger.warning("Humid regime selected — most streams likely "
                           "gaining. The module will still run.")
        pr = self.check_prerequisites()
        if not pr["overall_ok"]:
            raise RuntimeError(
                f"Pre-run check failed:\n"
                f"  Stream:   {pr['stream_message']}\n"
                f"  Runoff:   {pr['months_message']}\n"
                f"  GW depth: {pr['gwdepth_message']}\n"
                f"  K_s:      {pr['ks_message']}\n"
                f"  Width:    {pr['width_message']}")

        self._load_rainy_days()

        ck = None
        if self.auto_checkpoint or self._resume_from_checkpoint:
            ck = self.read_checkpoint(self.output_dir)
        if ck is not None:
            if ck.get("params_hash") != self._focused_params_hash():
                logger.warning("Checkpoint params do not match — "
                               "starting fresh.")
                ck = None
            else:
                self._report(
                    0, 0,
                    f"Resuming focused recharge from month "
                    f"{ck.get('next_month')} (completed: "
                    f"{ck.get('completed_months')})")
                self._checkpoint_created_at = ck.get(
                    "created_at", self._checkpoint_created_at)

        if ck is not None:
            completed_months = set(int(m) for m in ck.get(
                "completed_months", []))
            self._monthly_results = list(ck.get("monthly_results", []))
            total_focused_m3 = float(ck.get("total_focused_m3", 0.0))
            total_available_m3 = float(ck.get("total_available_m3", 0.0))
            total_outlet_m3 = float(ck.get("total_outlet_m3", 0.0))
            streambed_area_running = float(ck.get(
                "streambed_area_running", 0.0))
            total_losing_cell_months = int(ck.get(
                "total_losing_cell_months", 0))
            total_stream_cell_months = int(ck.get(
                "total_stream_cell_months", 0))
        else:
            completed_months = set()
            self._monthly_results = []
            total_focused_m3 = 0.0
            total_available_m3 = 0.0
            total_outlet_m3 = 0.0
            streambed_area_running = 0.0
            total_losing_cell_months = 0
            total_stream_cell_months = 0

        filled, fdir, acc, meta = self._get_hydrology()
        self._meta = meta
        cell_area_m2 = self._cellsize_m ** 2
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._meta_out = _diffusive_reference_meta(self.output_dir)
        if self._meta_out is None:
            self._report(0, 0,
                         "Recharge_diffusive_annual.tif not found — "
                         "output on the DEM grid.")
            _, self._meta_out = RasterIO().read_template(self._find_dem())
        else:
            self._report(0, 0,
                         "Writing on diffusive grid "
                         f"({self._meta_out['height']} × "
                         f"{self._meta_out['width']}).")

        self._report(0, 0, "Building DEM valid mask on output grid…")
        valid_small = np.isfinite(filled).astype(np.float32)
        valid_full = _upsample_array_to(
            valid_small, meta, self._meta_out,
            resampling=Resampling.nearest, fill_value=0.0)
        self._valid_mask_out = (valid_full > 0.5)
        n_out = int(self._valid_mask_out.sum())
        logger.info("Output grid valid cells: %d (%.1f %% of full grid).",
                    n_out,
                    100.0 * n_out / self._valid_mask_out.size)

        self._stream_mask = self._build_stream_mask(self._find_dem())
        n_total = int(self._stream_mask.sum())
        if n_total == 0:
            raise RuntimeError("Stream mask is empty.")

        if self.ks_mode == "from_soil":
            ks_raster, _ = self._build_Ks_raster(target_meta=meta)
            if ks_raster is None:
                ks_raster = np.full_like(
                    self._stream_mask.astype(np.float32), self.Ks_fixed)
            else:
                ks_raster = np.where(np.isfinite(ks_raster),
                                     ks_raster,
                                     self.Ks_fixed).astype(np.float32)
        else:
            ks_raster = np.full_like(
                self._stream_mask.astype(np.float32), self.Ks_fixed)
        ks_raster = np.where(np.isfinite(ks_raster),
                             ks_raster, self.Ks_fixed)
        self._ks_raster = ks_raster

        self._report(0, 0, "Building channel-width raster…")
        w_raster = self._build_width_raster(meta, self._stream_mask)
        w_max = CHANNEL_WIDTH_MAX_M
        w_raster = np.where(np.isfinite(w_raster) & (w_raster > 0),
                            w_raster, np.nan)
        w_raster = np.minimum(w_raster, w_max)
        w_raster = np.where(self._stream_mask & ~np.isfinite(w_raster),
                            self.W_fixed, w_raster)
        self._w_raster = w_raster

        valid_dem = np.isfinite(filled)
        catchment_area_m2 = float(valid_dem.sum() * cell_area_m2)

        logger.info("=" * 60)
        logger.info("FOCUSED RECHARGE — SETUP")
        logger.info("  D8 grid       : %d × %d (cellsize %.2f m)",
                    meta["height"], meta["width"], self._cellsize_m)
        logger.info("  Output grid   : %d × %d",
                    self._meta_out["height"], self._meta_out["width"])
        logger.info("  Catchment area: %.3f km²",
                    catchment_area_m2 / 1e6)
        logger.info("  Stream cells  : %d", n_total)
        logger.info("  Max width cap : %.1f m", w_max)
        logger.info("=" * 60)

        annual_mm_small = None
        if ck is not None and self._running_annual_path.exists():
            try:
                arr = np.load(str(self._running_annual_path))
                if arr.shape == (meta["height"], meta["width"]):
                    annual_mm_small = arr.astype(np.float32, copy=True)
            except Exception as exc:
                logger.warning("Could not reload running buffer: %s", exc)

        months = list(range(self.start_step, self.end_step + 1))
        total = len(months)
        rio = RasterIO()

        for i, month in enumerate(months, 1):
            if month in completed_months:
                self._report(i, total,
                             f"Focused recharge — month {month} "
                             f"(already complete, skipping)")
                continue
            self._report(i, total, f"Focused recharge — month {month}…")

            runoff_full, runoff_meta = rio.read(
                self._find_cell_runoff(month), kind="rainfall")
            gwdepth_full, gw_meta = rio.read(
                self._find_gwdepth(month), kind="gwdepth")
            runoff = _sample_array_onto_dst_grid(
                runoff_full, runoff_meta, meta,
                resampling=Resampling.bilinear, fill_value=np.nan)
            gwdepth = _sample_array_onto_dst_grid(
                gwdepth_full, gw_meta, meta,
                resampling=Resampling.bilinear, fill_value=np.nan)
            del runoff_full, gwdepth_full

            losing_ok = (self._stream_mask
                         & np.isfinite(gwdepth)
                         & (gwdepth >= self.gwdepth_threshold))
            n_losing = int(losing_ok.sum())
            n_stream_valid = int((self._stream_mask
                                  & np.isfinite(gwdepth)).sum())
            total_losing_cell_months += n_losing
            total_stream_cell_months += n_stream_valid

            runoff_m = np.where(np.isfinite(runoff),
                                runoff / 1000.0, 0.0)
            Ks_use = np.where(losing_ok, ks_raster, 0.0)
            W_use = np.where(losing_ok, w_raster, 0.0)

            rainy_days = (float(self._rainy_days[month - 1])
                          if self._rainy_days else 10.0)
            flow_days = max(0.5, min(
                rainy_days * self.flow_duration_fraction, 15.0))
            T_sec = flow_days * 86400.0

            V_loss_small, V_outlet, V_available = route_and_apply_loss(
                fdir=fdir, filled=filled,
                runoff_m=runoff_m, cell_area=cell_area_m2,
                is_stream=losing_ok,
                Ks=Ks_use, W=W_use,
                T_seconds=T_sec, dx=self._cellsize_m,
                max_daily_depth_mm=self.max_daily_depth_mm)

            # Enforce mass balance: a cell cannot infiltrate more
            # water than is physically available at that cell.
            V_loss_small = np.minimum(V_loss_small, V_available)

            # Enforce the per-month depth cap on the annual sum.
            max_month_depth_mm = self.max_daily_depth_mm * flow_days
            loss_mm_small = (V_loss_small / cell_area_m2
                             * 1000.0).astype(np.float32)
            loss_mm_small = np.minimum(loss_mm_small,
                                       max_month_depth_mm)

            _write_upsampled_tiled(
                loss_mm_small, meta, self._meta_out,
                self.output_dir / f"Recharge_focused_{month}.tif",
                resampling=Resampling.bilinear,
                tile=2048,
                nodata=self._meta_out.get("nodata", -9999.0),
                valid_mask=self._valid_mask_out)

            if annual_mm_small is None:
                annual_mm_small = loss_mm_small.copy()
            else:
                annual_mm_small += loss_mm_small

            focused_month_m3 = float(V_loss_small.sum())
            total_focused_m3 += focused_month_m3
            total_available_m3 += V_available
            total_outlet_m3 += V_outlet

            if losing_ok.any():
                month_streambed_area = float(
                    np.nansum(W_use[losing_ok]) * self._cellsize_m)
                streambed_area_running += month_streambed_area

            R_focused_avg_mm = (focused_month_m3 / catchment_area_m2
                                 * 1000.0)
            loss_fraction = (focused_month_m3 / V_available
                             if V_available > 0 else 0.0)
            losing_fraction = (n_losing / n_stream_valid
                               if n_stream_valid > 0 else 0.0)

            logger.info(
                "Month %d: losing=%d/%d (%.1f%%)  V_gen=%.3e m³  "
                "V_focused=%.3e m³  loss=%.1f%%  catchment_avg=%.3f mm",
                month, n_losing, n_stream_valid,
                100.0 * losing_fraction, V_available,
                focused_month_m3, 100.0 * loss_fraction,
                R_focused_avg_mm)

            self._monthly_results.append({
                "month": month,
                "n_losing": n_losing,
                "n_stream_valid": n_stream_valid,
                "flow_days": flow_days,
                "V_available": V_available,
                "V_focused": focused_month_m3,
                "V_outlet": V_outlet,
                "R_focused_avg_mm": R_focused_avg_mm,
                "loss_fraction": loss_fraction,
                "losing_fraction": losing_fraction,
            })
            completed_months.add(month)

            if self.auto_checkpoint:
                try:
                    np.save(str(self._running_annual_path),
                            annual_mm_small.astype(np.float32))
                except Exception as exc:
                    logger.warning("Could not save running buffer: %s", exc)
                self._save_checkpoint(
                    next_month=month + 1,
                    completed_months=completed_months,
                    counters={
                        "total_focused_m3": total_focused_m3,
                        "total_available_m3": total_available_m3,
                        "total_outlet_m3": total_outlet_m3,
                        "streambed_area_running": streambed_area_running,
                        "total_losing_cell_months":
                            total_losing_cell_months,
                        "total_stream_cell_months":
                            total_stream_cell_months,
                    })

        if annual_mm_small is not None:
            logger.info("Writing annual focused recharge tile-wise "
                        "(DEM-masked)…")
            _write_upsampled_tiled(
                annual_mm_small.astype(np.float32), meta, self._meta_out,
                self.output_dir / "Recharge_focused_annual.tif",
                resampling=Resampling.bilinear,
                tile=2048,
                nodata=self._meta_out.get("nodata", -9999.0),
                valid_mask=self._valid_mask_out)
            logger.info(
                "Wrote Recharge_focused_annual.tif on diffusive grid, "
                "masked to the DEM.")

        annual_avg_mm = (total_focused_m3 / catchment_area_m2 * 1000.0
                         if catchment_area_m2 > 0 else 0.0)
        annual_loss_fraction = (total_focused_m3 / total_available_m3
                                if total_available_m3 > 0 else 0.0)
        streambed_avg_mm = (total_focused_m3 / streambed_area_running
                            * 1000.0
                            if streambed_area_running > 0 else 0.0)
        overall_losing_fraction = (
            total_losing_cell_months / total_stream_cell_months
            if total_stream_cell_months > 0 else 0.0)

        self._report(total, total, "Focused recharge complete.")
        logger.info("=" * 60)
        logger.info("FOCUSED RECHARGE SUMMARY")
        logger.info("  Catchment area            : %.3f km²",
                    catchment_area_m2 / 1e6)
        logger.info("  Stream cells (total)      : %d", n_total)
        logger.info("  Treated as losing         : %.1f %%",
                    100.0 * overall_losing_fraction)
        logger.info("  Total focused recharge    : %.3e m³",
                    total_focused_m3)
        logger.info("  CATCHMENT-AVERAGE focused : %.3f mm/year",
                    annual_avg_mm)
        logger.info("  Streambed-average infilt. : %.3f mm/year",
                    streambed_avg_mm)
        logger.info("=" * 60)

        try:
            write_focused_run_report(
                output_dir=self.output_dir,
                working_dir=self.working_dir,
                engine=self,
                results={
                    "output_dir": str(self.output_dir),
                    "monthly_results": self._monthly_results,
                    "environment": self.environment,
                    "catchment_area_m2": catchment_area_m2,
                    "n_stream_cells": n_total,
                    "losing_fraction": overall_losing_fraction,
                    "total_focused_m3": total_focused_m3,
                    "total_available_m3": total_available_m3,
                    "total_outlet_m3": total_outlet_m3,
                    "annual_avg_mm": annual_avg_mm,
                    "annual_loss_fraction": annual_loss_fraction,
                    "streambed_avg_mm": streambed_avg_mm,
                })
        except Exception as exc:
            logger.warning("Could not write focused run report: %s", exc)

        self.discard_checkpoint(self.output_dir)

        return {
            "output_dir": str(self.output_dir),
            "monthly_results": self._monthly_results,
            "environment": self.environment,
            "catchment_area_m2": catchment_area_m2,
            "n_stream_cells": n_total,
            "losing_fraction": overall_losing_fraction,
            "total_focused_m3": total_focused_m3,
            "total_available_m3": total_available_m3,
            "total_outlet_m3": total_outlet_m3,
            "annual_avg_mm": annual_avg_mm,
            "annual_loss_fraction": annual_loss_fraction,
            "streambed_avg_mm": streambed_avg_mm,
        }