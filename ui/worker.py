# -*- coding: utf-8 -*-
"""Background worker thread for the WetSpass-M diffusive run."""

import contextlib
import traceback
from pathlib import Path

from qgis.PyQt.QtCore import QThread, pyqtSignal

from ..core.workflow import WetSpassWorkflow


class WetSpassWorker(QThread):

    progress = pyqtSignal(int, int, str)
    log = pyqtSignal(str)
    finished_ok = pyqtSignal(dict)
    finished_error = pyqtSignal(str)
    paused = pyqtSignal(dict)
    cancelled = pyqtSignal(dict)
    disk_error = pyqtSignal(dict)

    def __init__(self, working_dir, output_dir, parameters,
                 start_step, end_step, simulate_snow,
                 create_simulation_file, simulation_filename,
                 keep_outputs,
                 output_periods=None,
                 seasons=None,
                 seasonal_statistics=None,
                 resume_from_checkpoint=False,
                 spin_up_cycles: int = 2,
                 save_soil_state: bool = True,
                 auto_checkpoint: bool = True,
                 gw_depth_auto_cap: bool = True,
                 gw_depth_cap_m=None,
                 skip_cropping=False,
                 add_irrigation=False, read_fractions=False, irrig_dir=None, snow_dir=None, parent=None):

        super().__init__(parent)
        self.working_dir = Path(working_dir)
        self.output_dir = Path(output_dir)
        self.parameters = parameters
        self.start_step = start_step
        self.end_step = end_step
        self.simulate_snow = simulate_snow
        self.create_simulation_file = create_simulation_file
        self.simulation_filename = simulation_filename
        self.keep_outputs = keep_outputs
        self.output_periods = output_periods or ["monthly"]
        self.seasons = seasons or []
        self.seasonal_statistics = seasonal_statistics or ["mean"]
        self.resume_from_checkpoint = resume_from_checkpoint
        self.spin_up_cycles = spin_up_cycles
        self.save_soil_state = save_soil_state
        self.auto_checkpoint = auto_checkpoint
        self.gw_depth_auto_cap = gw_depth_auto_cap
        self.gw_depth_cap_m = gw_depth_cap_m
        self.skip_cropping = skip_cropping
        self.add_irrigation = add_irrigation
        self.irrig_dir = irrig_dir
        self.snow_dir = snow_dir
        self.read_fractions = read_fractions
        self.add_irrigation = add_irrigation
        self.irrig_dir = irrig_dir
        self.snow_dir = snow_dir
        self.read_fractions = read_fractions
        self.add_irrigation = add_irrigation
        self.irrig_dir = irrig_dir
        self.snow_dir = snow_dir
        self.read_fractions = read_fractions
        self._cancelled = False
        self._pause_crop_requested = False
        self._pause_requested = False
        self._skip_to_final_requested = False
        self._workflow = None
        self._signals_connected = True
        self.last_status = None

    def cancel(self):
        self._cancelled = True
        if self._workflow is not None:
            with contextlib.suppress(Exception):
                self._workflow.cancel()

    def request_pause(self):
        self._pause_requested = True
        if self._workflow is not None:
            with contextlib.suppress(Exception):
                self._workflow.pause()

    def request_pause_crop(self):
        self._pause_crop_requested = True

    def skip_to_final(self):
        self._skip_to_final_requested = True
        if self._workflow is not None:
            with contextlib.suppress(Exception):
                self._workflow.skip_to_final()

    def disconnect_signals(self):
        if not self._signals_connected:
            return
        for sig in (self.progress, self.log,
                    self.finished_ok, self.finished_error,
                    self.paused, self.cancelled, self.disk_error):
            with contextlib.suppress(Exception):
                sig.disconnect()
        self._signals_connected = False

    def _emit(self, signal, *args):
        if not self._signals_connected:
            return
        with contextlib.suppress(Exception):
            signal.emit(*args)

    def _auto_crop_worker(self):
        if getattr(self, 'skip_cropping', False):
            self._emit(self.log, 'Skip cropping is active. Using original inputs.')
            return

        try:
            import geopandas as gpd
            from rasterio.mask import mask as rio_mask
            import rasterio
            from pathlib import Path
            
            working_dir = Path(self.working_dir)
            output_dir = Path(self.output_dir)
            crop_dir = output_dir / "cropped_inputs"
            
            boundary = getattr(self, 'mask_vector_path', None)
            if not boundary or not Path(boundary).exists():
                shp_dir = working_dir / "inputs" / "shapefiles"
                if shp_dir.exists():
                    shps = list(shp_dir.glob("*.shp"))
                    if shps: boundary = str(shps[0])
                if not boundary:
                    shps = list(working_dir.rglob("*.shp"))
                    if shps: boundary = str(shps[0])
            
            if not boundary or not Path(boundary).exists():
                if (crop_dir / "dem.tif").exists():
                    self.working_dir = crop_dir
                    self._emit(self.log, "Reusing existing cropped inputs.")
                    return
                self._emit(self.log, "Warning: No shapefile found to crop to!")
                return

            self._emit(self.log, f"Using boundary: {Path(boundary).name}")
            self._emit(self.log, f"Saving cropped files to: {crop_dir}")
            gdf = gpd.read_file(boundary)
            
            maps_dir = working_dir / "inputs" / "maps"
            if not maps_dir.exists():
                maps_dir = working_dir / "maps"

            tasks = []
            for f in ["dem.tif", "landuse.tif", "soil.tif", "slope.tif"]:
                if (maps_dir / f).exists():
                    tasks.append((maps_dir / f, crop_dir / f))
                    
            for folder in ["rain", "pet", "temp", "wind", "gwdepth", "snow"]:
                if (maps_dir / folder).exists():
                    for m in range(1, 13):
                        for ext in [".tif", ".asc"]:
                            src = maps_dir / folder / f"{folder}{m}{ext}"
                            if src.exists():
                                tasks.append((src, crop_dir / folder / f"{folder}{m}.tif"))
                                break

            remaining_tasks = []
            for src, dst in tasks:
                if not dst.exists():
                    remaining_tasks.append((src, dst))
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)

            if not remaining_tasks:
                self.working_dir = crop_dir
                self._emit(self.log, "All files already cropped. Reusing inputs.")
                return

            total_tasks = len(remaining_tasks)
            self._emit(self.log, f"Resuming cropping. {total_tasks} files remaining...")

            def _crop(src, dst):
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    with rasterio.open(src) as s:
                        g = gdf.to_crs(s.crs) if gdf.crs != s.crs else gdf
                        out_img, out_trans = rio_mask(s, g.geometry, crop=True, nodata=-9999.0)
                        meta = s.meta.copy()
                        meta.update({"driver": "GTiff", "height": out_img.shape[1], "width": out_img.shape[2], "transform": out_trans, "nodata": -9999.0})
                    with rasterio.open(dst, "w", **meta) as d:
                        d.write(out_img)
                except Exception as e:
                    self._emit(self.log, f"Failed to crop {src.name}: {e}")

            for i, (src, dst) in enumerate(remaining_tasks, 1):
                # CHECK PAUSE BEFORE CROPPING EACH FILE
                if self._pause_crop_requested or self._cancelled:
                    self._emit(self.log, "Cropping paused by user.")
                    return
                
                _crop(src, dst)
                self._emit(self.progress, i, total_tasks, f"Cropping {src.name} ({i}/{total_tasks})")
                self._emit(self.log, f"Cropping {src.name}...")

            self.working_dir = crop_dir
            self._emit(self.log, "Auto-crop complete. Model will use the clipped rasters.")
            
        except Exception as e:
            self._emit(self.log, f"Auto-crop skipped: {e}")

    def run(self):
        try:
            # Perform auto-crop first
            self._auto_crop_worker()

            self._workflow = WetSpassWorkflow(
                working_dir=self.working_dir,
                output_dir=self.output_dir,
                parameters=self.parameters,
                start_step=self.start_step,
                end_step=self.end_step,
                simulate_snow=self.simulate_snow,
                create_simulation_file=self.create_simulation_file,
                simulation_filename=self.simulation_filename,
                keep_outputs=self.keep_outputs,
                output_periods=self.output_periods,
                seasons=self.seasons,
                seasonal_statistics=self.seasonal_statistics,
                resume_from_checkpoint=self.resume_from_checkpoint,
                spin_up_cycles=self.spin_up_cycles,
                save_soil_state=self.save_soil_state,
                auto_checkpoint=self.auto_checkpoint,
                gw_depth_auto_cap=self.gw_depth_auto_cap,
                gw_depth_cap_m=self.gw_depth_cap_m,
                mask_to_dem=not bool(getattr(self, 'mask_vector_path', None)),
            )
            if self._pause_requested:
                self._workflow.pause()
            if self._cancelled:
                self._workflow.cancel()
            if self._skip_to_final_requested:
                self._workflow.skip_to_final()

            def cb(step, total, msg):
                self._emit(self.progress, step, total, msg)
                self._emit(self.log, msg)

            self._workflow.progress_callback = cb
            results = self._workflow.run()

            status = results.get("status")
            if status == "paused":
                self.last_status = "paused"
                self._emit(self.paused, results)
            elif status == "cancelled":
                self.last_status = "cancelled"
                self._emit(self.cancelled, results)
            elif status == "disk_error":
                self.last_status = "disk_error"
                self._emit(self.disk_error, results)
            else:
                self.last_status = "completed"
                self._emit(self.finished_ok, results)
        except Exception as exc:
            self.last_status = "error"
            tb = traceback.format_exc()
            self._emit(self.finished_error, f"{exc}\n\n{tb}")
        finally:
            self._workflow = None