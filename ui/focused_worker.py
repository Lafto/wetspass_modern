# -*- coding: utf-8 -*-
"""
Background worker for focused-recharge estimation.
===================================================
Runs FocusedRechargeEngine in a QThread so the QGIS GUI stays responsive.

Parameter defaults are tuned for arid / semi-arid ephemeral streams:
    K_s                  = 1e-6 m/s  (silty-sandy clogged alluvium)
    max_daily_depth_mm   = 20 mm/day (average for a clogged bed)
    flow_duration_frac   = 0.3       (streams flow part of each rainy day)
"""

import traceback
from pathlib import Path

from qgis.PyQt.QtCore import QThread, pyqtSignal

from ..core.focused_recharge import FocusedRechargeEngine


class FocusedRechargeWorker(QThread):
    """Run FocusedRechargeEngine in a background thread."""

    # Signals emitted to the GUI
    progress = pyqtSignal(int, int, str)      # step, total, message
    log = pyqtSignal(str)                     # log line
    finished_ok = pyqtSignal(dict)            # results dict
    finished_error = pyqtSignal(str)          # error message

    def __init__(self,
                 working_dir,
                 output_dir,
                 start_step,
                 end_step,
                 stream_path=None,
                 stream_source="dem",
                 dem_stream_threshold=500,
                 Ks=1e-6,
                 channel_width_m=2.0,
                 gwdepth_threshold_m=3.0,
                 environment="semi-arid",
                 ks_mode="from_soil",
                 width_mode="from_order",
                 delta_factor=1.0,
                 max_daily_depth_mm=20.0,
                 flow_duration_fraction=0.3,
                 parent=None):
        super().__init__(parent)
        self.working_dir = Path(working_dir)
        self.output_dir = Path(output_dir)
        self.start_step = start_step
        self.end_step = end_step
        self.stream_path = stream_path
        self.stream_source = stream_source
        self.dem_stream_threshold = dem_stream_threshold
        self.Ks = Ks
        self.channel_width_m = channel_width_m
        self.gwdepth_threshold_m = gwdepth_threshold_m
        self.environment = environment
        self.ks_mode = ks_mode
        self.width_mode = width_mode
        self.delta_factor = delta_factor
        self.max_daily_depth_mm = max_daily_depth_mm
        self.flow_duration_fraction = flow_duration_fraction
        self._cancelled = False

    def cancel(self):
        """Request cancellation. The engine checks this between months."""
        self._cancelled = True

    def run(self):
        try:
            engine = FocusedRechargeEngine(
                working_dir=self.working_dir,
                output_dir=self.output_dir,
                start_step=self.start_step,
                end_step=self.end_step,
                stream_path=self.stream_path,
                stream_source=self.stream_source,
                dem_stream_threshold=self.dem_stream_threshold,
                Ks=self.Ks,
                channel_width_m=self.channel_width_m,
                gwdepth_threshold_m=self.gwdepth_threshold_m,
                environment=self.environment,
                ks_mode=self.ks_mode,
                width_mode=self.width_mode,
                delta_factor=self.delta_factor,
                max_daily_depth_mm=self.max_daily_depth_mm,
                flow_duration_fraction=self.flow_duration_fraction,
            )

            def cb(step, total, msg):
                if self._cancelled:
                    raise RuntimeError("Cancelled by user")
                self.progress.emit(step, total, msg)
                self.log.emit(msg)

            engine.progress_callback = cb
            results = engine.run()
            self.finished_ok.emit(results)

        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            self.finished_error.emit(f"{exc}\n\n{tb}")