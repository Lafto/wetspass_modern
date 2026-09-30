# -*- coding: utf-8 -*-
"""
Focused (streambed) recharge estimation — universal scope.
==========================================================
The module operates on any stream network and any climate. It reports
only the FOCUSED-RECHARGE component of stream–aquifer exchange — the
infiltration losses from losing (perched) stream reaches. Gaining reaches
are identified per cell per month from the gwdepth rasters and excluded.
The summary reports the fraction of the network treated as losing vs
gaining so the user knows how much of the network contributed.

It does NOT estimate groundwater discharge to gaining streams. That is a
separate process requiring streambed elevation, head gradient and streambed
conductance — none of which are direct inputs to WetSpass-M.

Parameter defaults are tuned for arid / semi-arid ephemeral streams but
can be overridden by the user for any climate.
"""

import logging
from pathlib import Path
from typing import Callable, Dict, Optional

import numpy as np

from qgis.core import QgsVectorFileWriter, QgsField, QgsFeature
from qgis.PyQt.QtCore import QVariant

from .raster_io import RasterIO
from .workflow import read_named_column, resolve_input_dirs, _read_rows
from .stream_io import load_stream_layer, stream_mask, cellsize_meters
from .stream_derivation import (
    compute_hydrology, order_proxy_from_accumulation,
    route_and_apply_loss,
)

logger = logging.getLogger("WetSpassModern")


# ---------------------------------------------------------------------------
# Order → width table (metres). Values are for ephemeral arid streams;
# the engine caps each value at 0.5 × cellsize so a DEM-derived 1-pixel
# stream never claims more than half of its cell.
# ---------------------------------------------------------------------------
ORDER_WIDTH_M = {
    1:  0.5,  2:  1.0,  3:  2.0,  4:  4.0,
    5:  8.0,  6: 15.0,  7: 25.0,  8: 40.0,
}
DEFAULT_HIGH_ORDER_WIDTH = 60.0

DEFAULT_FLOW_DURATION_FRACTION = 0.3
DEFAULT_MAX_DAILY_DEPTH_MM = 20.0


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


class FocusedRechargeEngine:
    """
    Universal focused-recharge engine. Operates on any stream network.

    Parameters
    ----------
    environment : str
        One of "arid", "semi-arid", "mixed", "humid". Used only to
        select default parameters and to emit an informational warning.
        It never blocks execution.
    """

    def __init__(self,
                 working_dir,
                 output_dir,
                 start_step: int = 1,
                 end_step: int = 12,
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
                 flow_duration_fraction: float = DEFAULT_FLOW_DURATION_FRACTION):
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

        self.maps_dir, self.tables_dir, _ = resolve_input_dirs(self.working_dir)

        self._stream_mask = None
        self._meta = None
        self._cellsize_m = 1.0
        self._rainy_days = []
        self._monthly_results = []
        self._ks_raster = None
        self._w_raster = None
        self._hydrology = None

        self.progress_callback: Optional[Callable] = None

    def _report(self, step, total, msg):
        if self.progress_callback:
            self.progress_callback(step, total, msg)
        logger.info("[Focused %d/%d] %s", step, total, msg)

    # -----------------------------------------------------------------
    # File finders
    # -----------------------------------------------------------------
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
                f"Cell_runoff_{month}.tif not found. Run the main model first.")
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
        dem_path = self._find_dem()
        self._report(0, 0, "Computing D8 hydrology from DEM "
                            "(smoothing + priority-flood + D8)…")
        filled, fdir, acc, meta = compute_hydrology(
            dem_path,
            smooth_sigma=1.0,
            debug=True,
            output_dir=self.output_dir)
        self._cellsize_m = cellsize_meters(meta)
        self._hydrology = (filled, fdir, acc, meta)
        return self._hydrology

    # -----------------------------------------------------------------
    # Streambed K_s from Soil.TBL
    # -----------------------------------------------------------------
    def _build_Ks_raster(self):
        soil_path = self._find_soil()
        if soil_path is None:
            return None, {}
        table_path = self.tables_dir / "Soil.TBL"
        if not table_path.exists():
            return None, {}
        try:
            rows = list(_read_rows(table_path))
        except Exception:
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
            logger.info("No K_s column in Soil.TBL → using fixed value.")
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

        rio = RasterIO()
        soil, _ = rio.read(soil_path, kind="default")
        ks_raster = np.full_like(soil, np.nan, dtype=np.float64)
        for code, ks in code_to_ks.items():
            ks_raster[soil == code] = ks
        logger.info("K_s from Soil.TBL column '%s' — %d classes",
                    matched, len(code_to_ks))
        return ks_raster, code_to_ks

    # -----------------------------------------------------------------
    # Stream mask
    # -----------------------------------------------------------------
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
                    f"{self.dem_stream_threshold}. Lower it and retry.")
            return mask

        tmp = self.output_dir / "_stream_mask_tmp.tif"
        mask, _ = stream_mask(self.stream_path, dem_path, tmp)
        try:
            tmp.unlink()
        except OSError:
            pass
        return mask

    # -----------------------------------------------------------------
    # Width raster
    # -----------------------------------------------------------------
    def _build_width_raster(self, meta, stream_mask):
        shape = (meta["height"], meta["width"])

        if self.width_mode == "constant":
            return np.full(shape, self.W_fixed, dtype=np.float64)

        if self.stream_source == "dem":
            _, _, acc, _ = self._get_hydrology()
            order = order_proxy_from_accumulation(
                acc, stream_mask, self.dem_stream_threshold)
            w_raster = np.zeros(shape, dtype=np.float64)
            for ord_val, base_w in ORDER_WIDTH_M.items():
                w = base_w * self.delta_factor if ord_val >= 6 else base_w
                w_raster[order == ord_val] = w
            w_raster[order > 8] = DEFAULT_HIGH_ORDER_WIDTH * self.delta_factor
            w_raster[~stream_mask] = np.nan
            w_raster[w_raster == 0] = np.nan
            return w_raster

        # Shapefile path
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
                base = ORDER_WIDTH_M.get(o, DEFAULT_HIGH_ORDER_WIDTH)
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
        if writer.hasError() != QgsVectorFileWriter.NoError:
            raise RuntimeError(f"Temp shapefile failed: "
                               f"{writer.errorMessage()}")

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

        processing.run("gdal:rasterize", {
            "INPUT": str(temp_shp), "FIELD": "__width__", "BURN": 0,
            "USE_Z": False, "UNITS": 1,
            "WIDTH": meta["width"], "HEIGHT": meta["height"],
            "NODATA": 0, "INIT": 0, "INVERT": False,
            "EXTRA": (
                f"-te {meta['transform'][2]} "
                f"{meta['transform'][2] + meta['transform'][0] * meta['width']} "
                f"{meta['transform'][5] + meta['transform'][4] * meta['height']} "
                f"{meta['transform'][5]} "
                f"-tr {meta['transform'][0]} {-meta['transform'][4]} "
                f"-a_srs {meta['crs'].toWkt() if meta['crs'] else ''} "
                f"-ot Float32 -co COMPRESS=DEFLATE"),
            "OUTPUT": str(temp_raster),
        })

        rio = RasterIO()
        w, _ = rio.read(temp_raster)
        w = np.where(np.isfinite(w) & (w > 0), w, np.nan)

        for suffix in ("", ".dbf", ".shx", ".prj", ".cpg"):
            try:
                Path(str(temp_shp) + suffix).unlink()
            except OSError:
                pass
        try:
            temp_raster.unlink()
        except OSError:
            pass

        return w

    # -----------------------------------------------------------------
    # Prerequisites
    # -----------------------------------------------------------------
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
                r["stream_message"] = (
                    f"OK — {layer.featureCount()} line features, "
                    f"CRS: {layer.crs().authid() or 'unknown'}")
            except Exception as exc:
                r["stream_message"] = str(exc)
                return r

        missing = [m for m in range(self.start_step, self.end_step + 1)
                   if not (self.output_dir / f"Cell_runoff_{m}.tif").exists()]
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
                    f"K_s from Soil.TBL — {len(cmap)} classes, "
                    f"range {min(cmap.values()):.1e}–"
                    f"{max(cmap.values()):.1e} m/s")
            else:
                r["ks_ok"] = True
                r["ks_message"] = (
                    f"No K_s column in Soil.TBL → fixed "
                    f"{self.Ks_fixed:.1e} m/s")
        else:
            r["ks_ok"] = True
            r["ks_message"] = f"Fixed K_s = {self.Ks_fixed:.1e} m/s"

        if self.width_mode == "constant":
            r["width_ok"] = True
            r["width_message"] = f"Fixed width = {self.W_fixed:.2f} m"
        elif self.stream_source == "dem":
            r["width_ok"] = True
            r["width_message"] = (
                "Width from DEM-derived Strahler-order proxy; "
                f"delta factor = {self.delta_factor:.2f}")
        else:
            r["width_ok"] = True
            r["width_message"] = (
                "Width from shapefile order/attribute; "
                f"delta factor = {self.delta_factor:.2f}")

        r["overall_ok"] = all([r["stream_ok"], r["months_ok"],
                                r["gwdepth_ok"], r["ks_ok"],
                                r["width_ok"]])
        return r

    # -----------------------------------------------------------------
    # Auto-detect environment (guidance only)
    # -----------------------------------------------------------------
    def auto_detect_environment(self):
        rio = RasterIO()
        dem = self._find_dem()
        _, meta = rio.read_template(dem)
        self._cellsize_m = cellsize_meters(meta)

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
                gw, _ = rio.read(self._find_gwdepth(m), kind="gwdepth")
                v = mask & np.isfinite(gw)
                if v.any():
                    depths.append(float(np.mean(gw[v])))
            except Exception as exc:
                logger.warning("gwdepth %d: %s", m, exc)

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
        msg = (f"Mean gwdepth under streams: {mean_depth:.2f} m "
               f"(threshold {self.gwdepth_threshold:.2f} m). "
               f"Suggested regime: {suggested.upper()}.")
        return {"environment": suggested, "message": msg,
                "mean_gwdepth": mean_depth, "n_stream_cells": n}

    # -----------------------------------------------------------------
    # Main run — universal
    # -----------------------------------------------------------------
    def run(self):
        # --- Informational warning if the user chose a climate that is
        #     generally unfavourable for focused recharge. NOT a blocker.
        if self.environment == "humid":
            logger.warning(
                "Humid regime selected. In humid regions most stream "
                "reaches are gaining, so focused recharge is likely to "
                "be a small fraction of total recharge. The module will "
                "still run and will report the losing / gaining split.")

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

        filled, fdir, acc, meta = self._get_hydrology()
        self._meta = meta
        cell_area_m2 = self._cellsize_m ** 2
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._stream_mask = self._build_stream_mask(self._find_dem())
        n_total = int(self._stream_mask.sum())
        if n_total == 0:
            raise RuntimeError("Stream mask is empty.")

        if self.ks_mode == "from_soil":
            ks_raster, _ = self._build_Ks_raster()
            if ks_raster is None:
                ks_raster = np.full_like(self._stream_mask.astype(float),
                                          self.Ks_fixed)
        else:
            ks_raster = np.full_like(self._stream_mask.astype(float),
                                      self.Ks_fixed)
        ks_raster = np.where(np.isfinite(ks_raster),
                             ks_raster, self.Ks_fixed)
        self._ks_raster = ks_raster

        self._report(0, 0, "Building channel-width raster…")
        w_raster = self._build_width_raster(meta, self._stream_mask)

        # Cap width at 0.5 × cellsize
        w_max = 0.5 * self._cellsize_m
        w_raster = np.where(np.isfinite(w_raster) & (w_raster > 0),
                            w_raster, np.nan)
        w_raster = np.minimum(w_raster, w_max)
        w_raster = np.where(self._stream_mask & ~np.isfinite(w_raster),
                            self.W_fixed, w_raster)
        self._w_raster = w_raster

        valid_dem = np.isfinite(filled)
        catchment_area_m2 = float(valid_dem.sum() * cell_area_m2)

        # Diagnostic setup summary
        w_on_stream = w_raster[self._stream_mask]
        ks_on_stream = ks_raster[self._stream_mask]
        max_w = float(np.nanmax(w_on_stream)) if w_on_stream.size else 0.0
        mean_w = float(np.nanmean(w_on_stream)) if w_on_stream.size else 0.0
        max_ks = float(np.nanmax(ks_on_stream)) if ks_on_stream.size else 0.0

        logger.info("=" * 60)
        logger.info("FOCUSED RECHARGE — SETUP")
        logger.info("  Regime (guidance)   : %s", self.environment.upper())
        logger.info("  Cell size           : %.2f m", self._cellsize_m)
        logger.info("  Cell area           : %.2f m²", cell_area_m2)
        logger.info("  Catchment area      : %.3f km²",
                    catchment_area_m2 / 1e6)
        logger.info("  Stream cells        : %d", n_total)
        logger.info("  K_s on streams      : mean=%.2e  max=%.2e m/s",
                    float(np.nanmean(ks_on_stream)) if ks_on_stream.size else 0.0,
                    max_ks)
        logger.info("  Width on streams    : mean=%.2f  max=%.2f m "
                    "(capped at %.2f)", mean_w, max_w, w_max)
        logger.info("  Flow duration frac  : %.2f × rainy_days",
                    self.flow_duration_fraction)
        logger.info("  Max infil cap       : %.0f mm/day",
                    self.max_daily_depth_mm)
        logger.info("=" * 60)

        months = list(range(self.start_step, self.end_step + 1))
        total = len(months)
        annual_loss_m3 = None
        total_available_m3 = 0.0
        total_outlet_m3 = 0.0
        total_focused_m3 = 0.0
        streambed_area_running = 0.0
        total_losing_cell_months = 0
        total_stream_cell_months = 0

        rio = RasterIO()

        for i, month in enumerate(months, 1):
            self._report(i, total, f"Focused recharge — month {month}…")

            runoff, _ = rio.read(self._find_cell_runoff(month),
                                 kind="rainfall")
            gwdepth, _ = rio.read(self._find_gwdepth(month), kind="gwdepth")

            losing_ok = (self._stream_mask
                         & np.isfinite(gwdepth)
                         & (gwdepth >= self.gwdepth_threshold))

            # Track the losing / gaining split
            n_losing = int(losing_ok.sum())
            n_stream_valid = int((self._stream_mask
                                  & np.isfinite(gwdepth)).sum())
            total_losing_cell_months += n_losing
            total_stream_cell_months += n_stream_valid

            runoff_m = np.where(np.isfinite(runoff), runoff / 1000.0, 0.0)
            Ks_use = np.where(losing_ok, ks_raster, 0.0)
            W_use = np.where(losing_ok, w_raster, 0.0)

            rainy_days = (float(self._rainy_days[month - 1])
                          if self._rainy_days else 10.0)
            flow_days = max(0.5, min(
                rainy_days * self.flow_duration_fraction, 15.0))
            T_sec = flow_days * 86400.0

            V_loss, V_outlet, V_available = route_and_apply_loss(
                fdir=fdir, filled=filled,
                runoff_m=runoff_m, cell_area=cell_area_m2,
                is_stream=losing_ok,
                Ks=Ks_use, W=W_use,
                T_seconds=T_sec, dx=self._cellsize_m,
                max_daily_depth_mm=self.max_daily_depth_mm)

            out = V_loss / cell_area_m2 * 1000.0
            rio.write(self.output_dir / f"Recharge_focused_{month}.tif",
                      out, meta)

            if annual_loss_m3 is None:
                annual_loss_m3 = V_loss.copy()
            else:
                annual_loss_m3 += V_loss

            focused_month_m3 = float(V_loss.sum())
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
                "Month %d: losing=%d/%d (%.1f%%)  flow_days=%.2f  "
                "V_gen=%.3e m³  V_focused=%.3e m³  loss=%.1f%%  "
                "catchment_avg=%.3f mm",
                month, n_losing, n_stream_valid, 100.0 * losing_fraction,
                flow_days, V_available, focused_month_m3,
                100.0 * loss_fraction, R_focused_avg_mm)

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

        if annual_loss_m3 is not None:
            annual_mm = annual_loss_m3 / cell_area_m2 * 1000.0
            rio.write(self.output_dir / "Recharge_focused_annual.tif",
                      annual_mm, meta)

        annual_avg_mm = (total_focused_m3 / catchment_area_m2 * 1000.0
                         if catchment_area_m2 > 0 else 0.0)
        annual_loss_fraction = (total_focused_m3 / total_available_m3
                                if total_available_m3 > 0 else 0.0)
        streambed_avg_mm = (total_focused_m3 / streambed_area_running * 1000.0
                            if streambed_area_running > 0 else 0.0)
        overall_losing_fraction = (
            total_losing_cell_months / total_stream_cell_months
            if total_stream_cell_months > 0 else 0.0)

        self._report(total, total, "Focused recharge complete.")
        logger.info("=" * 60)
        logger.info("FOCUSED RECHARGE SUMMARY")
        logger.info("  Regime (guidance)         : %s",
                    self.environment.upper())
        logger.info("  Catchment area            : %.3f km²",
                    catchment_area_m2 / 1e6)
        logger.info("  Stream cells (total)      : %d", n_total)
        logger.info("  Treated as losing         : %.1f %% "
                    "(%d cell-months)",
                    100.0 * overall_losing_fraction,
                    total_losing_cell_months)
        logger.info("  Treated as gaining        : %.1f %%",
                    100.0 * (1.0 - overall_losing_fraction))
        logger.info("  Total runoff generated    : %.3e m³",
                    total_available_m3)
        logger.info("  Total focused recharge    : %.3e m³",
                    total_focused_m3)
        logger.info("  Total leaving at outlets  : %.3e m³",
                    total_outlet_m3)
        logger.info("  Loss fraction             : %.2f %%",
                    100.0 * annual_loss_fraction)
        logger.info("  CATCHMENT-AVERAGE focused : %.3f mm/year",
                    annual_avg_mm)
        logger.info("  Streambed-average infilt. : %.3f mm/year",
                    streambed_avg_mm)
        logger.info("=" * 60)

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