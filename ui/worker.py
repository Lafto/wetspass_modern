# -*- coding: utf-8 -*-
"""Background worker thread for the WetSpass-M diffusive run."""

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

    def __init__(self, working_dir, output_dir, parameters,
                 start_step, end_step, simulate_snow,
                 create_simulation_file, simulation_filename,
                 keep_outputs,
                 output_periods=None,
                 wet_season=None,
                 dry_season=None,
                 seasonal_statistic="mean",
                 resume_from_checkpoint=False,
                 spin_up_cycles: int = 2,
                 save_soil_state: bool = True,
                 parent=None):
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
        self.wet_season = wet_season or [6, 7, 8, 9]
        self.dry_season = dry_season or [10, 11, 12, 1, 2, 3, 4, 5]
        self.seasonal_statistic = seasonal_statistic
        self.resume_from_checkpoint = resume_from_checkpoint
        self.spin_up_cycles = spin_up_cycles
        self.save_soil_state = save_soil_state
        self._cancelled = False
        self._pause_requested = False
        self._workflow = None
        self.last_status = None

    def cancel(self):
        self._cancelled = True
        if self._workflow is not None:
            try:
                self._workflow.cancel()
            except Exception:
                pass

    def request_pause(self):
        self._pause_requested = True
        if self._workflow is not None:
            try:
                self._workflow.pause()
            except Exception:
                pass

    def run(self):
        try:
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
                wet_season=self.wet_season,
                dry_season=self.dry_season,
                seasonal_statistic=self.seasonal_statistic,
                resume_from_checkpoint=self.resume_from_checkpoint,
                spin_up_cycles=self.spin_up_cycles,
                save_soil_state=self.save_soil_state,
            )
            if self._pause_requested:
                self._workflow.pause()
            if self._cancelled:
                self._workflow.cancel()

            def cb(step, total, msg):
                self.progress.emit(step, total, msg)
                self.log.emit(msg)
            self._workflow.progress_callback = cb
            results = self._workflow.run()

            status = results.get("status")
            if status == "paused":
                self.last_status = "paused"
                self.paused.emit(results)
            elif status == "cancelled":
                self.last_status = "cancelled"
                self.cancelled.emit(results)
            else:
                self.last_status = "completed"
                self.finished_ok.emit(results)
        except Exception as exc:
            self.last_status = "error"
            tb = traceback.format_exc()
            self.finished_error.emit(f"{exc}\n\n{tb}")
        finally:
            self._workflow = None