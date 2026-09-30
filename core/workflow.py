# -*- coding: utf-8 -*-
"""
WetSpass-M Modern — Workflow Orchestrator.

Spin-up logic
-------------
The soil bucket is a state. Each spin-up cycle runs the same 12 monthly
input maps once, carrying soil moisture forward. After each cycle the
workflow computes:

    mean |S_end - S_start|     (mm/yr)

which measures how much the bucket is still changing. If this falls
below the tolerance (default 0.5% of mean annual P), the bucket has
converged and the remaining warm-up cycles are skipped. The final cycle
then writes the output rasters and the annual ΔS, which will be ≈ 0.

A convergence report is written to `spinup_convergence.csv`.
"""

import csv
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import numpy as np

try:
    import rasterio
    from rasterio.warp import reproject, Resampling
    from rasterio.windows import Window
    HAS_WARP = True
except ImportError:
    HAS_WARP = False
    Window = None

from .calculation_engine import WetSpassCalculator, WetSpassParameters
from .raster_io import RasterIO, BlockProcessor

logger = logging.getLogger("WetSpassModern")

CHECKPOINT_NAME = "wetspass_checkpoint.json"
SNOW_STATE_NAME = "snow_store_state.tif"
SOIL_STATE_NAME = "soilwater_storage_initial.tif"
CONVERGENCE_NAME = "spinup_convergence.csv"
CHECKPOINT_VERSION = 5


class WetSpassCancelledError(Exception):
    pass


LANDUSE_COLUMNS = {
    "vegarea": 4, "barearea": 5, "imp_area": 6, "owarea": 7,
    "rootdepth": 8, "lai": 9, "minstomata": 10, "zveg": 11,
    "landfactor": 13, "Aeroresit_wind1ms": 14,
}
LANDUSE_MIN_COLS = 14
SOIL_COLUMNS = {
    "fc": 3, "wp": 4, "residual_water_content": 6, "a1": 7,
    "evapo_depth": 8, "tension_ht": 9, "soilfactor": 12,
}
SOIL_MIN_COLS = 12

OUTPUT_FILENAME_MAP = {"recharge": "Recharge_diffusive"}

TABLE_REQUIREMENTS = [
    ("Landuses.TBL",           LANDUSE_MIN_COLS, "Land-use lookup"),
    ("Soil.TBL",               SOIL_MIN_COLS,    "Soil lookup"),
    ("RainyDaysPerMonth.TBL",  2,                "Rainy days per month"),
    ("DegreeDaysPerMonth.TBL", 2,                "Degree days (if snow)"),
]


def resolve_input_dirs(working_dir):
    wd = Path(working_dir)
    info = {"layout": "unknown", "maps_path": "", "tables_path": ""}
    for m, t, lbl in [
        (wd / "inputs" / "maps", wd / "inputs" / "tables",
         "inputs/maps + inputs/tables"),
        (wd / "maps",            wd / "tables",            "maps + tables"),
        (wd / "Inputs" / "Maps", wd / "Inputs" / "Tables",
         "Inputs/Maps + Inputs/Tables"),
        (wd / "Maps",            wd / "Tables",            "Maps + Tables"),
    ]:
        if m.is_dir() or t.is_dir():
            info.update(layout=lbl, maps_path=str(m), tables_path=str(t))
            return m, t, info
    info.update(layout="not found",
                maps_path=str(wd / "inputs" / "maps"),
                tables_path=str(wd / "inputs" / "tables"))
    return wd / "inputs" / "maps", wd / "inputs" / "tables", info


def _detect_delimiter(path):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            header = f.readline()
    except OSError:
        return "\t"
    c = {"\t": header.count("\t"), ",": header.count(","),
         ";": header.count(";")}
    b = max(c, key=c.get)
    return b if c[b] > 0 else None


def _read_rows(path):
    d = _detect_delimiter(path)
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            yield line.split() if d is None else [c.strip()
                                                  for c in line.split(d)]


def read_table_by_position(path, col_map):
    table = {n: {} for n in col_map}
    rows = _read_rows(path)
    try:
        next(rows)
    except StopIteration:
        return table
    for row in rows:
        if len(row) < 2:
            continue
        try:
            code = int(float(row[0]))
        except ValueError:
            continue
        for n, c1 in col_map.items():
            i = c1 - 1
            if i < len(row):
                try:
                    table[n][code] = float(row[i])
                except ValueError:
                    pass
    return table


def read_named_column(path, names):
    rows = list(_read_rows(path))
    if not rows:
        return []
    header = [h.lower() for h in rows[0]]
    body = rows[1:]
    idx = -1
    for w in names:
        try:
            idx = header.index(w.lower())
            break
        except ValueError:
            continue
    if idx < 0 and body and len(body[0]) == 1:
        idx = 0
    if idx < 0:
        return []
    out = []
    for row in body:
        if idx < len(row):
            try:
                out.append(float(row[idx]))
            except ValueError:
                pass
    return out


def inspect_inputs(working_dir, start_step=1, end_step=12,
                   simulate_snow=False):
    wd = Path(working_dir)
    maps_dir, tables_dir, layout = resolve_input_dirs(wd)
    report = {"working_dir": str(wd), "layout": layout["layout"],
              "maps_dir": str(maps_dir), "tables_dir": str(tables_dir),
              "tables": [], "folders": [], "grid_consistency": [],
              "overall_ok": True}

    for fname, min_cols, desc in TABLE_REQUIREMENTS:
        if fname == "DegreeDaysPerMonth.TBL" and not simulate_snow:
            continue
        e = {"name": fname, "ok": False, "message": "", "path": ""}
        p = tables_dir / fname
        e["path"] = str(p)
        if not p.exists():
            e["message"] = f"Not found at {p}"
            report["overall_ok"] = False
            report["tables"].append(e)
            continue
        try:
            rows = list(_read_rows(p))
            if not rows:
                e["message"] = "File is empty"
                report["overall_ok"] = False
            else:
                nc, nd = len(rows[0]), len(rows) - 1
                if nc < min_cols:
                    e["message"] = f"Only {nc} cols (need >= {min_cols})"
                    report["overall_ok"] = False
                elif nd < 1:
                    e["message"] = f"Header OK ({nc} cols) but no data rows"
                    report["overall_ok"] = False
                else:
                    e["ok"] = True
                    prev = ", ".join(rows[0][:5]) + (" ..." if nc > 5 else "")
                    e["message"] = f"OK - {nc} cols, {nd} rows. Header: {prev}"
        except Exception as exc:
            e["message"] = f"Read error: {exc}"
            report["overall_ok"] = False
        report["tables"].append(e)

    for label, base, required in [
        ("DEM",       "dem",     True),
        ("Land use",  "landuse", True),
        ("Soil",      "soil",    True),
        ("Slope",     "slope",   False),
    ]:
        found = next((maps_dir / f"{base}{ext}" for ext in (".tif", ".asc")
                      if (maps_dir / f"{base}{ext}").exists()), None)
        if found is not None:
            report["folders"].append({
                "name": label, "ok": True,
                "message": f"OK - {found.name}"})
        elif required:
            report["folders"].append({
                "name": label, "ok": False,
                "message": f"Missing {base}.tif (required)"})
            report["overall_ok"] = False
        else:
            report["folders"].append({
                "name": label, "ok": True,
                "message": "Not provided - will be derived from DEM"})

    months = list(range(start_step, end_step + 1))
    for label, sub, prefix in [("Rainfall", "rain", "rain"),
                                ("PET", "pet", "pet"),
                                ("Temperature", "temp", "temp"),
                                ("Wind", "wind", "wind"),
                                ("GW depth", "gwdepth", "gwdepth")]:
        folder = maps_dir / sub
        if not folder.is_dir():
            report["folders"].append(
                {"name": label, "ok": False,
                 "message": f"Folder missing: {folder}"})
            report["overall_ok"] = False
            continue
        present = [m for m in months
                   if (folder / f"{prefix}{m}.tif").exists()
                   or (folder / f"{prefix}{m}.asc").exists()]
        missing = [m for m in months if m not in present]
        ok = not missing
        msg = f"{len(present)}/{len(months)} files"
        if missing:
            msg += f" — missing months {missing}"
        report["folders"].append({"name": label, "ok": ok, "message": msg})
        if not ok:
            report["overall_ok"] = False

    if simulate_snow:
        folder = maps_dir / "snow"
        if not folder.is_dir():
            report["folders"].append({
                "name": "Snow cover", "ok": False,
                "message": f"Folder missing: {folder}"})
            report["overall_ok"] = False
        else:
            present = [m for m in months
                       if (folder / f"snowcover{m}.tif").exists()
                       or (folder / f"snowcover{m}.asc").exists()]
            missing = [m for m in months if m not in present]
            ok = not missing
            msg = f"{len(present)}/{len(months)} files"
            if missing:
                msg += f" — missing months {missing}"
            report["folders"].append({
                "name": "Snow cover", "ok": ok, "message": msg})
            if not ok:
                report["overall_ok"] = False

    gc = report["grid_consistency"]
    try:
        if rasterio is None:
            gc.append({"name": "Grid consistency", "ok": False,
                       "message": "rasterio not available"})
        else:
            ref_path = next((maps_dir / f"dem{ext}" for ext in (".tif", ".asc")
                             if (maps_dir / f"dem{ext}").exists()), None)
            if ref_path is None:
                gc.append({"name": "Grid consistency", "ok": False,
                           "message": "DEM not found"})
            else:
                with rasterio.open(str(ref_path)) as src:
                    ref_shape = (src.height, src.width)
                    ref_crs = src.crs
                gc.append({
                    "name": "DEM (reference)", "ok": True,
                    "message": f"{ref_shape[0]}x{ref_shape[1]}, CRS {ref_crs}",
                })
                samples = []
                for lbl, base in [("Land use", "landuse"),
                                   ("Soil", "soil"),
                                   ("Slope", "slope")]:
                    p = next((maps_dir / f"{base}{ext}"
                              for ext in (".tif", ".asc")
                              if (maps_dir / f"{base}{ext}").exists()), None)
                    if p is not None:
                        samples.append((lbl, p))
                for lbl, sub, prefix in [("Rainfall", "rain", "rain"),
                                         ("PET", "pet", "pet"),
                                         ("Temperature", "temp", "temp"),
                                         ("Wind", "wind", "wind"),
                                         ("GW depth", "gwdepth", "gwdepth")]:
                    folder = maps_dir / sub
                    if not folder.is_dir():
                        continue
                    sample = next(
                        (folder / f"{prefix}{m}{ext}"
                         for m in months for ext in (".tif", ".asc")
                         if (folder / f"{prefix}{m}{ext}").exists()),
                        None)
                    if sample is not None:
                        samples.append((lbl, sample))
                for lbl, sample in samples:
                    try:
                        with rasterio.open(str(sample)) as src:
                            shp = (src.height, src.width)
                            same = (shp == ref_shape and src.crs == ref_crs)
                            crs_str = str(src.crs)
                        msg = f"{shp[0]}x{shp[1]}, CRS {crs_str}"
                        if not same:
                            msg += "  <-- NOT aligned to DEM"
                        gc.append({"name": lbl, "ok": same, "message": msg})
                    except Exception as exc:
                        gc.append({"name": lbl, "ok": False,
                                   "message": f"Read error: {exc}"})
    except Exception as exc:
        logger.warning("Grid check failed: %s", exc)
        gc.append({"name": "Grid consistency", "ok": False,
                   "message": f"Check failed: {exc}"})
    return report


class WetSpassWorkflow:

    def __init__(self, working_dir, output_dir, parameters,
                 start_step=1, end_step=12, simulate_snow=False,
                 create_simulation_file=True,
                 simulation_filename="Simulated.tbl",
                 keep_outputs: Optional[Set[str]] = None,
                 output_periods: Optional[List[str]] = None,
                 wet_season: Optional[List[int]] = None,
                 dry_season: Optional[List[int]] = None,
                 seasonal_statistic: str = "mean",
                 block_size: Optional[int] = None,
                 resume_from_checkpoint: bool = False,
                 spin_up_cycles: int = 2,
                 save_soil_state: bool = True,
                 convergence_tol_frac: float = 0.005,
                 convergence_tol_mm: float = 1.0):
        self.working_dir = Path(working_dir)
        self.output_dir = Path(output_dir)
        self.params = parameters
        self.start_step = start_step
        self.end_step = end_step
        self.simulate_snow = simulate_snow
        self.create_simulation_file = create_simulation_file
        self.simulation_filename = simulation_filename
        self.keep_outputs = keep_outputs
        self.output_periods = output_periods or ["monthly"]
        self.wet_season = wet_season or [6, 7, 8, 9]
        self.dry_season = dry_season or [10, 11, 12, 1, 2, 3, 4, 5]
        self.seasonal_statistic = (seasonal_statistic or "mean").lower()
        if self.seasonal_statistic not in ("sum", "mean"):
            self.seasonal_statistic = "mean"

        if parameters.steady_state_mode:
            self.spin_up_cycles = 0
            self.save_soil_state = False
        else:
            self.spin_up_cycles = max(0, int(spin_up_cycles))
            self.save_soil_state = save_soil_state

        self.convergence_tol_frac = float(convergence_tol_frac)
        self.convergence_tol_mm = float(convergence_tol_mm)

        self._raster_io = None
        self._ref_meta = None
        self._block_proc = None
        self._block_size = block_size
        self.maps_dir, self.tables_dir, _ = resolve_input_dirs(self.working_dir)
        self.rainy_days: List[float] = []
        self.degree_days: List[float] = []
        self.monthly_stats: List[Dict] = []
        self.progress_callback: Optional[Callable] = None
        self._pause_requested = False
        self._cancel_requested = False
        self._checkpoint_path = self.output_dir / CHECKPOINT_NAME
        self._snow_state_path = self.output_dir / SNOW_STATE_NAME
        self._soil_state_path = self.output_dir / SOIL_STATE_NAME
        self._checkpoint: Optional[Dict] = None
        self._next_month: Optional[int] = None
        self._next_block: int = 0
        self._completed_months: List[int] = []
        self._pass1_was_completed: bool = False
        self._pass1_next_block: int = 0
        self._checkpoint_created_at: str = ""
        self._snow_store_snapshot = None
        self._soil_storage_state: Optional[np.ndarray] = None

        if resume_from_checkpoint:
            self._load_checkpoint()

    @property
    def raster_io(self):
        if self._raster_io is None:
            self._raster_io = RasterIO()
        return self._raster_io

    @property
    def block_proc(self):
        if self._block_proc is None:
            bs = self._block_size or self._auto_block_size()
            self._block_proc = BlockProcessor(block_size=bs, halo=2)
        return self._block_proc

    def _auto_block_size(self) -> int:
        try:
            import psutil
            free_gb = psutil.virtual_memory().available / 1e9
        except ImportError:
            free_gb = 8.0
        target = free_gb * 0.25 * 1e9
        bs = int((target / (50 * 8)) ** 0.5)
        for cap in (256, 512, 1024, 2048, 4096):
            if bs <= cap:
                return cap
        return 4096

    def _report(self, step, total, msg):
        try:
            s, t = int(step), int(total)
        except Exception:
            s, t = 0, 1
        if self.progress_callback:
            self.progress_callback(s, t, str(msg))
        logger.info("[%d/%d] %s", s, t, msg)

    def pause(self):
        self._pause_requested = True

    def cancel(self):
        self._cancel_requested = True

    # -----------------------------------------------------------------
    # Convergence metric: mean annual precipitation
    # Block-wise, memory-safe, and reports progress per month.
    # -----------------------------------------------------------------
    def _mean_annual_precip(self) -> float:
        """
        Sum of the 12 monthly rainfall means, computed block-wise so that
        a full raster is never loaded into memory. Reports progress
        through the progress_callback for every month.
        """
        if not HAS_WARP:
            # No rasterio; fall back to a nominal value.
            self._report(0, 1,
                         "mean_P: rasterio unavailable — using 1000 mm/yr")
            return 1000.0

        total_sum = 0.0
        total_count = 0
        months = list(range(self.start_step, self.end_step + 1))
        n = len(months)
        tile = 2048

        for i, m in enumerate(months, 1):
            p = self._find_raster("rain", "rain", m)
            if p is None:
                continue
            self._report(i, n,
                         f"Computing mean annual P: "
                         f"month {m} ({i}/{n})…")
            try:
                with rasterio.open(str(p)) as src:
                    nd = (src.nodata if src.nodata is not None
                          else -9999.0)
                    block_sum = 0.0
                    block_count = 0
                    for r in range(0, src.height, tile):
                        h = min(tile, src.height - r)
                        for c in range(0, src.width, tile):
                            w = min(tile, src.width - c)
                            arr = src.read(
                                1, window=Window(c, r, w, h)
                            ).astype(np.float64)
                            arr[arr == nd] = np.nan
                            valid = arr[np.isfinite(arr)]
                            if valid.size:
                                block_sum += float(valid.sum())
                                block_count += int(valid.size)
                    if block_count:
                        total_sum += block_sum / block_count
                        total_count += 1
            except Exception as exc:
                logger.warning("mean_P: could not read %s: %s", p, exc)

        if total_count == 0:
            self._report(0, 1,
                         "mean_P: no rainfall rasters found — "
                         "using 1000 mm/yr for tolerance.")
            return 1000.0
        return total_sum

    # -----------------------------------------------------------------
    def _write_spinup_report(self, rows, tol_mm, mean_P, converged_after):
        if not rows:
            return
        path = self.output_dir / CONVERGENCE_NAME
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["# Spin-up convergence report"])
                w.writerow([f"# mean_annual_P_mm", f"{mean_P:.4f}"])
                w.writerow([f"# tolerance_mm", f"{tol_mm:.4f}"])
                w.writerow([f"# tolerance_frac",
                            f"{self.convergence_tol_frac:.6f}"])
                w.writerow([f"# converged_after_cycle",
                            converged_after if converged_after else ""])
                w.writerow([])
                w.writerow(["cycle", "mean_abs_dS_mm", "mean_dS_mm",
                            "pct_of_P", "tol_mm", "converged"])
                for r in rows:
                    w.writerow([r["cycle"],
                                f"{r['mean_abs_dS_mm']:.6f}",
                                f"{r['mean_dS_mm']:.6f}",
                                f"{r['pct_of_P']:.6f}",
                                f"{tol_mm:.6f}",
                                "yes" if r["converged"] else "no"])
            logger.info("Wrote spin-up report: %s", path)
        except Exception as exc:
            logger.warning("Could not write convergence report: %s", exc)

    # -----------------------------------------------------------------
    @staticmethod
    def has_checkpoint(output_dir) -> bool:
        return (Path(output_dir) / CHECKPOINT_NAME).exists()

    @staticmethod
    def read_checkpoint(output_dir):
        p = Path(output_dir) / CHECKPOINT_NAME
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def discard_checkpoint(output_dir):
        p = Path(output_dir) / CHECKPOINT_NAME
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    def _params_hash(self) -> str:
        p = self.params
        tokens = [
            f"{p.a_interception}", f"{p.alfa}", f"{p.w_slope}",
            f"{p.w_landuse}", f"{p.w_soil}", f"{p.x_coef}", f"{p.lp}",
            f"{p.intensity}", f"{p.beta}", f"{p.contribution}",
            f"{p.base_temp}", f"{p.melt_factor}", f"{p.snow_density}",
            f"{p.area_km2}", f"{self.start_step}", f"{self.end_step}",
            f"{self.simulate_snow}", f"{sorted(self.output_periods)}",
            f"{self.spin_up_cycles}",
            f"{self.params.steady_state_mode}",
            f"{self.convergence_tol_frac}",
            f"{self.convergence_tol_mm}",
            f"{sorted(self.keep_outputs) if self.keep_outputs else 'all'}",
        ]
        return hashlib.sha256(",".join(tokens).encode()).hexdigest()[:16]

    def _save_checkpoint(self, current_month, next_block,
                         pass1_complete, pass1_next_block):
        snow_state_file = None
        if self.simulate_snow and self._snow_store_snapshot is not None:
            try:
                self.raster_io.write(self._snow_state_path,
                                      self._snow_store_snapshot,
                                      self._ref_meta)
                snow_state_file = self._snow_state_path.name
            except Exception:
                pass
        if (self._soil_storage_state is not None
                and not self.params.steady_state_mode):
            try:
                self.raster_io.write(self._soil_state_path,
                                      self._soil_storage_state,
                                      self._ref_meta)
            except Exception:
                pass

        data = {
            "version": CHECKPOINT_VERSION,
            "created_at": self._checkpoint_created_at,
            "paused_at": datetime.now().isoformat(timespec="seconds"),
            "working_dir": str(self.working_dir),
            "output_dir": str(self.output_dir),
            "start_step": self.start_step,
            "end_step": self.end_step,
            "simulate_snow": self.simulate_snow,
            "output_periods": list(self.output_periods),
            "wet_season": list(self.wet_season),
            "dry_season": list(self.dry_season),
            "seasonal_statistic": self.seasonal_statistic,
            "keep_outputs": sorted(self.keep_outputs) if self.keep_outputs else None,
            "parameters_hash": self._params_hash(),
            "pass1_complete": bool(pass1_complete),
            "pass1_next_block": int(pass1_next_block),
            "current_month": int(current_month),
            "next_block_index": int(next_block),
            "completed_months": list(self._completed_months),
            "snow_state_file": snow_state_file,
            "status": "paused",
        }
        tmp = self._checkpoint_path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(str(tmp), str(self._checkpoint_path))
        except Exception as exc:
            logger.error("Failed to save checkpoint: %s", exc)

    def _load_checkpoint(self):
        data = self.read_checkpoint(self.output_dir)
        if data is None:
            raise FileNotFoundError("No checkpoint found.")
        if data.get("version") != CHECKPOINT_VERSION:
            raise RuntimeError("Checkpoint version mismatch.")
        if data.get("parameters_hash") != self._params_hash():
            raise RuntimeError("Checkpoint parameters do not match.")
        self._checkpoint = data
        self._checkpoint_created_at = data.get("created_at",
                                                datetime.now().isoformat())
        self._next_month = data["current_month"]
        self._next_block = data["next_block_index"]
        self._completed_months = list(data.get("completed_months", []))
        self._pass1_was_completed = bool(data.get("pass1_complete", False))
        self._pass1_next_block = int(data.get("pass1_next_block", 0))

    def load_tables(self):
        p = self.tables_dir / "RainyDaysPerMonth.TBL"
        if p.exists():
            self.rainy_days = read_named_column(
                p, ["RainyDays", "Rainydays", "rainy_days",
                    "Rainy_Days", "Rainy"])
        if not self.rainy_days:
            self.rainy_days = [10.0] * 12
        if self.simulate_snow:
            p = self.tables_dir / "DegreeDaysPerMonth.TBL"
            if p.exists():
                self.degree_days = read_named_column(
                    p, ["DegreeDays", "Degreedays", "degree_days",
                        "Degree_Days"])
            if not self.degree_days:
                self.degree_days = [30.0] * 12

    def _find_dem(self):
        for ext in (".tif", ".asc"):
            p = self.maps_dir / f"dem{ext}"
            if p.exists():
                return p
        raise FileNotFoundError(f"DEM not found in {self.maps_dir}")

    def _find_raster(self, sub, prefix, month=None, exts=(".tif", ".asc")):
        for ext in exts:
            name = (f"{prefix}{month}{ext}" if month is not None
                    else f"{prefix}{ext}")
            p = (self.maps_dir / Path(sub) / name if sub
                 else self.maps_dir / name)
            if p.exists():
                return p
        return None

    def _validate_alignment(self):
        if not HAS_WARP:
            raise RuntimeError("rasterio is required for alignment check.")
        ref = self._ref_meta
        ref_crs, ref_transform = ref["crs"], ref["transform"]
        ref_w, ref_h = ref["width"], ref["height"]
        paths = [("dem", self._find_dem()),
                 ("landuse", self._find_raster(None, "landuse")),
                 ("soil", self._find_raster(None, "soil")),
                 ("slope", self._find_raster(None, "slope"))]
        for m in range(self.start_step, self.end_step + 1):
            for label, sub, prefix in [
                ("rain", "rain", "rain"), ("pet", "pet", "pet"),
                ("temp", "temp", "temp"), ("wind", "wind", "wind"),
                ("gwdepth", "gwdepth", "gwdepth"),
            ]:
                paths.append((f"{label}{m}",
                              self._find_raster(sub, prefix, m)))
        mismatched = []
        for name, path in paths:
            if path is None:
                continue
            try:
                with rasterio.open(str(path)) as src:
                    ok_crs = (src.crs == ref_crs)
                    ok_transform = (src.transform == ref_transform)
                    ok_size = (src.width == ref_w and src.height == ref_h)
                    if not (ok_crs and ok_transform and ok_size):
                        mismatched.append({
                            "name": name, "file": path.name,
                            "shape": (src.height, src.width),
                            "ref_shape": (ref_h, ref_w),
                            "crs": str(src.crs), "ref_crs": str(ref_crs),
                            "size_ok": ok_size, "crs_ok": ok_crs,
                            "transform_ok": ok_transform,
                        })
            except Exception as exc:
                mismatched.append({"name": name, "file": path.name,
                                    "error": str(exc)})
        if not mismatched:
            return
        lines = [
            "Input rasters are not on the same grid as the DEM.", "",
            f"DEM grid  : {ref_h} x {ref_w} cells, CRS = {ref_crs}",
            f"Inputs    : {len(paths)} checked, "
            f"{len(mismatched)} do not match.", "",
            "Mismatched rasters:",
        ]
        for entry in mismatched[:25]:
            if "error" in entry:
                lines.append(f"  - {entry['name']:<12} "
                             f"({entry['file']}): ERROR {entry['error']}")
            else:
                flags = []
                if not entry["size_ok"]:
                    flags.append(f"shape {entry['shape']} vs "
                                 f"DEM {entry['ref_shape']}")
                if not entry["crs_ok"]:
                    flags.append(f"crs {entry['crs']} vs {entry['ref_crs']}")
                if not entry["transform_ok"]:
                    flags.append("transform differs")
                lines.append(f"  - {entry['name']:<12} "
                             f"({entry['file']}): " + "; ".join(flags))
        raise RuntimeError("\n".join(lines))

    def _verify_lookups_on_disk(self) -> bool:
        ld = self.output_dir / "Lookups"
        if not ld.is_dir():
            return False
        required = (list(LANDUSE_COLUMNS.keys()) + list(SOIL_COLUMNS.keys())
                    + ["vegratio", "bareratio", "gamma", "slope"])
        for n in required:
            p = ld / f"{n}.tif"
            if not p.exists() or p.stat().st_size < 1000:
                return False
        return True

    def _discover_lookups(self):
        ld = self.output_dir / "Lookups"
        lookups = {}
        for n in (list(LANDUSE_COLUMNS.keys()) + list(SOIL_COLUMNS.keys())
                  + ["vegratio", "bareratio", "gamma", "slope"]):
            p = ld / f"{n}.tif"
            if p.exists():
                lookups[n] = p
        lookups["dem"] = self._find_dem()
        return lookups

    def _build_static_lookups(self):
        ld = self.output_dir / "Lookups"
        ld.mkdir(parents=True, exist_ok=True)
        dem_path = self._find_dem()
        _, meta = self.raster_io.read_template(dem_path)
        self._ref_meta = dict(meta)
        landuse_table = read_table_by_position(
            self.tables_dir / "Landuses.TBL", LANDUSE_COLUMNS)
        soil_table = read_table_by_position(
            self.tables_dir / "Soil.TBL", SOIL_COLUMNS)
        landuse_path = self._find_raster(None, "landuse")
        soil_path = self._find_raster(None, "soil")
        slope_path = self._find_raster(None, "slope")
        if landuse_path is None or soil_path is None:
            raise FileNotFoundError("landuse / soil raster missing.")

        lookups_out = {}
        landuse_names = list(LANDUSE_COLUMNS.keys())
        soil_names = list(SOIL_COLUMNS.keys())
        for n in landuse_names + soil_names:
            lookups_out[n] = ld / f"{n}.tif"
        lookups_out["vegratio"]  = ld / "vegratio.tif"
        lookups_out["bareratio"] = ld / "bareratio.tif"
        lookups_out["gamma"]     = ld / "gamma.tif"
        lookups_out["slope"]     = ld / "slope.tif"
        lookups_out["dem"]       = dem_path
        if slope_path is not None:
            lookups_out["slope"] = slope_path
        computed_names = (landuse_names + soil_names
                          + ["vegratio", "bareratio", "gamma"])
        if slope_path is None:
            computed_names.append("slope")
        for n in computed_names:
            p = lookups_out[n]
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        for n in computed_names:
            self.block_proc.create_empty(lookups_out[n], meta, nodata=-9999.0)

        blocks = list(self.block_proc.iter_blocks(meta))
        total = len(blocks)
        calc = WetSpassCalculator(self.params)
        for i, (r, c, h, w) in enumerate(blocks, 1):
            if self._cancel_requested:
                raise WetSpassCancelledError(
                    f"Cancelled during Pass 1 at block {i} of {total}")
            self._report(i, total,
                         f"Pass 1 static lookups — block {i} of {total}")
            block_in = self.block_proc.read_block(
                {"landuse": landuse_path, "soil": soil_path, "dem": dem_path},
                r, c, h, w, meta)
            g = block_in["_block_geometry"]
            lu, soil, dem = block_in["landuse"], block_in["soil"], block_in["dem"]
            for name in landuse_names:
                codes = landuse_table.get(name, {})
                arr = np.full_like(lu, np.nan, dtype=np.float64)
                for code, val in codes.items():
                    arr[lu == code] = val
                self.block_proc.write_block(lookups_out[name], arr, g, meta)
            for name in soil_names:
                codes = soil_table.get(name, {})
                arr = np.full_like(soil, np.nan, dtype=np.float64)
                for code, val in codes.items():
                    arr[soil == code] = val
                self.block_proc.write_block(lookups_out[name], arr, g, meta)
            veg = np.full_like(lu, np.nan, dtype=np.float64)
            bare = np.full_like(lu, np.nan, dtype=np.float64)
            for code, val in landuse_table.get("vegarea", {}).items():
                veg[lu == code] = val
            for code, val in landuse_table.get("barearea", {}).items():
                bare[lu == code] = val
            vr, br = calc.calculate_veg_ratios(veg, bare)
            self.block_proc.write_block(lookups_out["vegratio"], vr, g, meta)
            self.block_proc.write_block(lookups_out["bareratio"], br, g, meta)
            gamma = calc.calculate_gamma(dem)
            self.block_proc.write_block(lookups_out["gamma"], gamma, g, meta)
            if slope_path is None:
                cs = (abs(meta["transform"][0])
                      if meta.get("transform") else 1.0)
                slope = np.maximum(calc.calculate_slope(dem, cs), 0.3)
                self.block_proc.write_block(
                    lookups_out["slope"], slope, g, meta)
        return lookups_out

    def _run_month_blocks(self, month, static_paths, output_paths,
                          month_offset, n_months, total_blocks,
                          write_outputs=True):
        meta = self._ref_meta
        calc = WetSpassCalculator(self.params, self.rainy_days)
        if self.simulate_snow and self._snow_state_path.exists():
            try:
                snap, _ = self.raster_io.read(self._snow_state_path)
                if snap.shape == (meta["height"], meta["width"]):
                    calc.snow_store = snap
            except Exception:
                pass
        month_paths = {
            "rainfall": self._find_raster("rain", "rain", month),
            "pet":      self._find_raster("pet", "pet", month),
            "temp":     self._find_raster("temp", "temp", month),
            "wind":     self._find_raster("wind", "wind", month),
            "gwdepth":  self._find_raster("gwdepth", "gwdepth", month),
        }
        for k, v in month_paths.items():
            if v is None:
                raise FileNotFoundError(f"Missing {k} raster for month {month}")
        all_in = {k: str(v) for k, v in static_paths.items()}
        for k, v in month_paths.items():
            all_in["m_" + k] = str(v)

        blocks = list(self.block_proc.iter_blocks(meta))
        block_stats = []
        for i, (r, c, h, w) in enumerate(blocks, 1):
            if self._cancel_requested:
                raise WetSpassCancelledError(
                    f"Cancelled during month {month} at block "
                    f"{i} of {total_blocks}")
            if self._pause_requested:
                return {"paused": True, "next_block": i - 1,
                        "stats": {row[0]: row[1] for row in block_stats}}
            step = int(month_offset) + i
            overall = int(n_months) * int(total_blocks)
            self._report(step, overall,
                         f"Month {month} - block {i} of {total_blocks}")
            block = self.block_proc.read_block(all_in, r, c, h, w, meta)
            g = block["_block_geometry"]
            r0, c0, r1, c1 = g["r0"], g["c0"], g["r1"], g["c1"]
            soil_slice = self._soil_storage_state[r0:r1, c0:c1].copy()
            inputs = {}
            for k in ("rainfall", "pet", "temp", "wind", "gwdepth"):
                inputs[k] = block["m_" + k]
            for k in ("dem", "vegarea", "barearea", "imp_area", "owarea",
                      "rootdepth", "lai", "minstomata", "zveg", "landfactor",
                      "Aeroresit_wind1ms", "fc", "wp",
                      "residual_water_content", "a1", "evapo_depth",
                      "tension_ht", "soilfactor", "gamma", "slope"):
                if k in block:
                    inputs[k] = block[k]
            inputs["soil_storage"] = soil_slice
            is_first = (month == self.start_step and i == 1)
            outputs = calc.calculate_month(month, inputs,
                                            is_first_step=is_first)
            sl_r, sl_c = self.block_proc.interior_slice(g)
            S_new_block = outputs.get("soilwater_storage")
            if S_new_block is not None and not self.params.steady_state_mode:
                ir0, ic0 = g["row_off"], g["col_off"]
                self._soil_storage_state[ir0:ir0 + g["height"],
                                         ic0:ic0 + g["width"]] = \
                    S_new_block[sl_r, sl_c]
            if write_outputs:
                for name, arr in outputs.items():
                    if not isinstance(arr, np.ndarray):
                        continue
                    out_key = OUTPUT_FILENAME_MAP.get(name, name)
                    if out_key not in output_paths:
                        continue
                    self.block_proc.write_block(output_paths[out_key],
                                                 arr, g, meta)
                stat_keys = ("Cell_evapotranspiration", "Cell_runoff",
                             "Interception", "recharge")
                if i == 1:
                    block_stats = []
                    for k in stat_keys:
                        v = outputs.get(k)
                        if v is None:
                            continue
                        inter = v[sl_r, sl_c]
                        inter = inter[np.isfinite(inter)]
                        block_stats.append(
                            [k, float(inter.mean()) if inter.size else 0.0,
                             int(inter.size)])
                else:
                    for j, k in enumerate(stat_keys):
                        if j >= len(block_stats) or block_stats[j][0] != k:
                            continue
                        v = outputs.get(k)
                        if v is None:
                            continue
                        inter = v[sl_r, sl_c]
                        inter = inter[np.isfinite(inter)]
                        if inter.size:
                            prev_mean = float(block_stats[j][1])
                            prev_n = int(block_stats[j][2])
                            new_n = prev_n + int(inter.size)
                            new_mean = ((prev_mean * prev_n
                                         + float(inter.sum())) / float(new_n))
                            block_stats[j][1] = new_mean
                            block_stats[j][2] = new_n
        if self.simulate_snow and calc.snow_store is not None:
            self._snow_store_snapshot = calc.snow_store
        return {"paused": False, "next_block": len(blocks),
                "stats": {row[0]: row[1] for row in block_stats}}

    def _run_one_cycle(self, is_final, static_paths, output_names,
                       meta, months, n_months, total_blocks):
        self._next_month = self.start_step
        self._next_block = 0
        self._pause_requested = False
        if is_final:
            self._completed_months = []

        sim_file = None
        if is_final and self.create_simulation_file:
            sim_path = self.output_dir / self.simulation_filename
            sim_file = open(str(sim_path), "w")
            sim_file.write("*" * 74 + "\n")
            sim_file.write("WetSpass-M Modern Simulation Results\n")
            if self.params.steady_state_mode:
                sim_file.write("(compatibility mode: Wetspass-M 2016)\n")
            sim_file.write("*" * 74 + "\n")
            hdr = "TimeStep\tAET\tRunoff\tInterception\tRecharge"
            if self.params.area_km2 > 0:
                hdr += "\tQsurf[m3/mnt]\tQb[m3/mnt]"
            sim_file.write(hdr + "\n")

        try:
            for m_idx, month in enumerate(months):
                month_paths = {}
                if is_final:
                    self._report(0, 1,
                                 f"Preparing outputs for month {month}...")
                    for name in output_names:
                        p = self.output_dir / f"{name}_{month}.tif"
                        if not p.exists() or p.stat().st_size < 100:
                            self.block_proc.create_empty(
                                p, meta, nodata=-9999.0)
                        month_paths[name] = p

                result = self._run_month_blocks(
                    month=month, static_paths=static_paths,
                    output_paths=month_paths,
                    month_offset=m_idx * total_blocks,
                    n_months=n_months, total_blocks=total_blocks,
                    write_outputs=is_final)

                if result["paused"]:
                    self._save_checkpoint(
                        current_month=month, next_block=result["next_block"],
                        pass1_complete=True, pass1_next_block=0)
                    return {"status": "paused", "current_month": month,
                            "next_block": result["next_block"],
                            "completed_months": list(self._completed_months),
                            "output_dir": str(self.output_dir)}

                if is_final:
                    stats = result["stats"]
                    if sim_file:
                        line = (f"{month}\t"
                                f"{stats.get('Cell_evapotranspiration', 0.0):.6f}\t"
                                f"{stats.get('Cell_runoff', 0.0):.6f}\t"
                                f"{stats.get('Interception', 0.0):.6f}\t"
                                f"{stats.get('recharge', 0.0):.6f}")
                        sim_file.write(line + "\n")
                        sim_file.flush()
                    self.monthly_stats.append({"TimeStep": month, **stats})
                    self._completed_months.append(month)
        finally:
            if sim_file:
                sim_file.close()
        return None

    def _aggregate(self, months, output_names, tag, divisor=1):
        import rasterio as _rio
        meta = self._ref_meta
        for name in output_names:
            if name == "soilwater_storage":
                continue
            srcs = [self.output_dir / f"{name}_{m}.tif" for m in months]
            srcs = [p for p in srcs if p.exists()]
            if not srcs:
                continue
            out_path = self.output_dir / f"{name}_{tag}.tif"
            self.block_proc.create_empty(out_path, meta, nodata=-9999.0)
            for r, c, h, w in self.block_proc.iter_blocks(meta):
                if self._cancel_requested:
                    raise WetSpassCancelledError(
                        f"Cancelled during aggregation ({tag}) of {name}")
                total_sum = None
                total_count = None
                for p in srcs:
                    with _rio.open(str(p)) as src:
                        arr = src.read(1, window=Window(c, r, w, h)
                                       ).astype(np.float64)
                        nd = (src.nodata if src.nodata is not None
                              else -9999.0)
                        arr[arr == nd] = np.nan
                    valid = np.isfinite(arr)
                    clean = np.where(valid, arr, 0.0)
                    cnt = valid.astype(np.float64)
                    if total_sum is None:
                        total_sum = clean
                        total_count = cnt
                    else:
                        total_sum = total_sum + clean
                        total_count = total_count + cnt
                final = np.where(total_count > 0, total_sum, np.nan)
                if divisor > 1:
                    final = final / float(divisor)
                self.block_proc.write_block_only(
                    out_path, final, r, c, h, w)

    def run(self):
        try:
            return self._run_impl()
        except WetSpassCancelledError as exc:
            return {"status": "cancelled",
                    "output_dir": str(self.output_dir),
                    "message": str(exc)}

    def _run_impl(self):
        self._checkpoint_created_at = (self._checkpoint or {}).get(
            "created_at", datetime.now().isoformat(timespec="seconds"))
        self._snow_store_snapshot = None

        if self.params.steady_state_mode:
            self._report(0, 1,
                         "Compatibility mode ON: Wetspass-M 2016 physics.")
        self._report(0, 1, "Reading tables...")
        self.load_tables()
        dem_path = self._find_dem()
        _, meta = self.raster_io.read_template(dem_path)
        self._ref_meta = dict(meta)
        H, W = meta["height"], meta["width"]

        if self.params.steady_state_mode:
            self._soil_storage_state = np.zeros((H, W), dtype=np.float64)
        elif self._soil_state_path.exists():
            try:
                self._soil_storage_state, _ = self.raster_io.read(
                    self._soil_state_path)
                logger.info("Loaded soil storage from %s",
                            self._soil_state_path)
            except Exception as exc:
                logger.warning("Could not read soil state: %s", exc)
                self._soil_storage_state = np.zeros((H, W), dtype=np.float64)
        else:
            logger.info("No previous soil state found — starting from zeros.")
            self._soil_storage_state = np.zeros((H, W), dtype=np.float64)

        self._report(0, 1, "Checking alignment...")
        self._validate_alignment()

        if self._pass1_was_completed and self._verify_lookups_on_disk():
            self._report(0, 1, "Pass 1: reusing existing lookups.")
            static_paths = self._discover_lookups()
        else:
            self._report(0, 1, "Pass 1: building static lookups...")
            static_paths = self._build_static_lookups()
            self._pass1_was_completed = True

        all_names = [
            "Interception", "total_Interception", "evap_rate", "Ch",
            "Cell_runoff", "Csr", "vegrunoff", "barerunoff",
            "imperrunoff", "owrunoff",
            "Cell_evapotranspiration", "Cell_gw_discharge",
            "Cell_actualtranspiration", "Cell_actual_baresoil_evapo",
            "Cell_ow_evaporation", "Cell_imper_evaporation",
            "Cell_gw_transpiration", "Cell_gw_evaporation", "recharge",
            "soilwater_storage", "wb_error", "penmann_coefficient",
        ]
        keep = self.keep_outputs
        if keep is None:
            keep = set(all_names)
        output_names = set()
        for n in keep:
            output_names.add(OUTPUT_FILENAME_MAP.get(n, n))

        months = list(range(self.start_step, self.end_step + 1))
        n_months = len(months)
        total_blocks = self.block_proc.n_blocks(meta)

        # ---------------------------------------------------------------
        # Convergence tolerance
        # ---------------------------------------------------------------
        if self.params.steady_state_mode:
            mean_P = 1000.0
            tol_mm = 0.0
        else:
            self._report(0, 1,
                         "Pass 1 complete. Computing spin-up tolerance…")
            mean_P = self._mean_annual_precip()
            tol_mm = max(self.convergence_tol_mm,
                         self.convergence_tol_frac * mean_P)
            self._report(0, 1,
                         f"Spin-up tolerance: {tol_mm:.2f} mm/yr "
                         f"({self.convergence_tol_frac*100:.2f}% of "
                         f"mean P = {mean_P:.1f} mm)")

        # ---------------------------------------------------------------
        # Spin-up cycles (may exit early on convergence)
        # ---------------------------------------------------------------
        spinup_rows = []
        converged_after = None

        for i in range(self.spin_up_cycles):
            prev_state = self._soil_storage_state.copy()
            self._report(0, 1,
                         f"Spin-up cycle {i+1}/{self.spin_up_cycles}...")
            paused = self._run_one_cycle(
                is_final=False, static_paths=static_paths,
                output_names=output_names, meta=meta,
                months=months, n_months=n_months,
                total_blocks=total_blocks)
            if paused:
                return paused

            curr_state = self._soil_storage_state
            delta = curr_state - prev_state
            mean_abs_dS = float(np.nanmean(np.abs(delta)))
            mean_dS = float(np.nanmean(delta))
            pct = 100.0 * mean_abs_dS / max(mean_P, 1e-6)
            converged = mean_abs_dS < tol_mm
            spinup_rows.append({
                "cycle": i + 1,
                "mean_abs_dS_mm": mean_abs_dS,
                "mean_dS_mm": mean_dS,
                "pct_of_P": pct,
                "converged": converged,
            })
            self._report(0, 1,
                         f"  mean |ΔS| = {mean_abs_dS:.3f} mm "
                         f"({pct:.3f}% of P), mean ΔS = {mean_dS:.3f} mm — "
                         f"{'CONVERGED' if converged else 'not converged'}")
            if converged:
                converged_after = i + 1
                self._report(0, 1,
                             f"Early exit: bucket converged after "
                             f"{converged_after} spin-up cycle(s). "
                             f"Skipping remaining warm-ups.")
                break

        self._write_spinup_report(spinup_rows, tol_mm, mean_P,
                                   converged_after)

        # ---------------------------------------------------------------
        # Final cycle
        # ---------------------------------------------------------------
        soil_state_start_of_final = self._soil_storage_state.copy()
        self._report(0, 1, "Final cycle — writing outputs...")
        paused = self._run_one_cycle(
            is_final=True, static_paths=static_paths,
            output_names=output_names, meta=meta,
            months=months, n_months=n_months,
            total_blocks=total_blocks)
        if paused:
            return paused

        if self.save_soil_state and not self.params.steady_state_mode:
            self.raster_io.write(self._soil_state_path,
                                  self._soil_storage_state, meta)

        if (soil_state_start_of_final is not None
                and not self.params.steady_state_mode):
            delta = self._soil_storage_state - soil_state_start_of_final
            mean_dS_final = float(np.nanmean(delta))
            self._report(0, 1,
                         f"Final cycle mean ΔS = {mean_dS_final:.3f} mm/yr "
                         f"(this is what the Water Balance panel will show)")
            self.raster_io.write(
                self.output_dir / "soilwater_storage_delta_annual.tif",
                delta, meta)

        annual_months = list(range(self.start_step, self.end_step + 1))
        wet_months = [m for m in annual_months if m in set(self.wet_season)]
        dry_months = [m for m in annual_months if m in set(self.dry_season)]
        if "annual" in self.output_periods:
            self._report(0, 1, "Pass 3: annual aggregation...")
            self._aggregate(annual_months, output_names, "annual")
        if "seasonal" in self.output_periods:
            if wet_months:
                div = (len(wet_months)
                       if self.seasonal_statistic == "mean" else 1)
                self._aggregate(wet_months, output_names,
                                "wet_season", divisor=div)
            if dry_months:
                div = (len(dry_months)
                       if self.seasonal_statistic == "mean" else 1)
                self._aggregate(dry_months, output_names,
                                "dry_season", divisor=div)

        self.discard_checkpoint(self.output_dir)
        if self._snow_state_path.exists():
            try:
                self._snow_state_path.unlink()
            except OSError:
                pass
        if self._soil_state_path.exists() and self.params.steady_state_mode:
            try:
                self._soil_state_path.unlink()
            except OSError:
                pass

        self._report(1, 1, "Model run complete!")
        return {"status": "completed",
                "monthly_stats": self.monthly_stats,
                "output_dir": str(self.output_dir),
                "months_processed": n_months}