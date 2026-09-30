# -*- coding: utf-8 -*-
"""
Focused Recharge tab — universal focused-recharge GUI.
=======================================================
The climate radio is guidance-only. It changes the defaults and the
warning text but never disables the Run button. Focused recharge is
estimated only from losing stream reaches; gaining reaches are identified
per cell per month and reported in the summary.
"""

from pathlib import Path

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QRadioButton, QGroupBox, QSpinBox,
    QDoubleSpinBox, QButtonGroup, QTextEdit, QMessageBox, QFileDialog,
    QSizePolicy, QProgressBar,
)
from qgis.core import QgsRasterLayer, QgsProject

from .focused_worker import FocusedRechargeWorker
from ..core.focused_recharge import FocusedRechargeEngine
from ..core.raster_io import RasterIO
import numpy as np


class FocusedRechargeTab(QWidget):

    request_log = pyqtSignal(str)

    def __init__(self, get_working_dir, get_output_dir,
                 get_time_range, parent=None):
        super().__init__(parent)
        self.get_working_dir = get_working_dir
        self.get_output_dir = get_output_dir
        self.get_time_range = get_time_range
        self.worker = None
        self._build_ui()

    # =================================================================
    # UI construction
    # =================================================================
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        intro = QLabel(
            "Estimate <b>focused recharge</b> along ephemeral or "
            "intermittent streambeds. Runoff is routed with D8 flow "
            "along the DEM's flow network; streambed cells absorb a "
            "fraction of the routed volume and the rest passes "
            "downstream. <b>Only losing reaches contribute</b> — cells "
            "where the water table is shallower than the threshold are "
            "treated as gaining and excluded. The summary reports the "
            "losing / gaining split. The module can be used in any "
            "climate; the climate radio below only adjusts the defaults.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        # ---- Climate regime (guidance only) --------------------------
        g_env = QGroupBox("Climate regime (guidance only — Run is never blocked)")
        ev = QVBoxLayout(g_env)
        self.radio_arid = QRadioButton(
            "Arid — most streams losing; focused recharge may dominate")
        self.radio_semi = QRadioButton(
            "Semi-arid — mixed; focused recharge often significant")
        self.radio_mixed = QRadioButton(
            "Mixed / transitional — check the reported losing fraction")
        self.radio_humid = QRadioButton(
            "Humid — most streams gaining; focused recharge likely small")
        self.radio_semi.setChecked(True)
        self.env_group = QButtonGroup(self)
        self.env_group.addButton(self.radio_arid, 0)
        self.env_group.addButton(self.radio_semi, 1)
        self.env_group.addButton(self.radio_mixed, 2)
        self.env_group.addButton(self.radio_humid, 3)
        self.env_group.buttonClicked.connect(self._on_environment_changed)
        ev.addWidget(self.radio_arid)
        ev.addWidget(self.radio_semi)
        ev.addWidget(self.radio_mixed)
        ev.addWidget(self.radio_humid)

        detect_row = QHBoxLayout()
        self.btn_detect = QPushButton("Auto-detect from gwdepth rasters")
        self.btn_detect.setToolTip(
            "Reads the gwdepth rasters and suggests the closest regime "
            "based on the mean depth to water table under the streams.")
        self.btn_detect.clicked.connect(self._auto_detect)
        detect_row.addWidget(self.btn_detect)
        detect_row.addStretch(1)
        ev.addLayout(detect_row)

        self.lbl_env_status = QLabel("—")
        self.lbl_env_status.setWordWrap(True)
        self.lbl_env_status.setStyleSheet(
            "color:#333; background:#f5f5f5; padding:6px; "
            "border-radius:3px; font-family:monospace;")
        ev.addWidget(self.lbl_env_status)

        self.chk_acknowledge = QCheckBox(
            "I understand the module reports only the losing component, "
            "not net stream–aquifer exchange.")
        self.chk_acknowledge.setChecked(True)
        ev.addWidget(self.chk_acknowledge)

        outer.addWidget(g_env)

        # ---- Stream source -------------------------------------------
        g_stream = QGroupBox("Stream network source")
        sv = QGridLayout(g_stream)

        self.radio_stream_dem = QRadioButton(
            "Derive from DEM (default — recommended)")
        self.radio_stream_shp = QRadioButton(
            "Use a stream shapefile (routing still uses the DEM)")
        self.stream_group = QButtonGroup(self)
        self.stream_group.addButton(self.radio_stream_dem, 0)
        self.stream_group.addButton(self.radio_stream_shp, 1)
        self.radio_stream_dem.setChecked(True)
        self.stream_group.buttonClicked.connect(self._on_stream_source_changed)

        sv.addWidget(self.radio_stream_dem, 0, 0, 1, 4)
        sv.addWidget(QLabel("Stream threshold [cells]:"), 1, 0)
        self.spin_threshold = QSpinBox()
        self.spin_threshold.setRange(50, 1_000_000)
        self.spin_threshold.setValue(500)
        self.spin_threshold.setToolTip(
            "D8 flow-accumulation threshold. Cells draining at least "
            "this many upstream cells are classified as stream.")
        sv.addWidget(self.spin_threshold, 1, 1)
        sv.addWidget(QLabel(
            "<i>Typical: 200 (small catchments) – 2000 (large)</i>"),
            1, 2, 1, 2)

        sv.addWidget(self.radio_stream_shp, 2, 0, 1, 4)
        sv.addWidget(QLabel("Stream shapefile:"), 3, 0)
        self.txt_stream = QLineEdit()
        self.txt_stream.setEnabled(False)
        sv.addWidget(self.txt_stream, 3, 1, 1, 2)
        self.btn_browse = QPushButton("...")
        self.btn_browse.setFixedWidth(32)
        self.btn_browse.setEnabled(False)
        self.btn_browse.clicked.connect(self._browse_stream)
        sv.addWidget(self.btn_browse, 3, 3)

        btn_check = QPushButton("Check prerequisites")
        btn_check.clicked.connect(self._check_stream)
        sv.addWidget(btn_check, 4, 0, 1, 4)

        outer.addWidget(g_stream)

        # ---- K_s ------------------------------------------------------
        g_ks = QGroupBox("Streambed hydraulic conductivity (K_s)")
        kv = QGridLayout(g_ks)
        self.radio_ks_soil = QRadioButton(
            "From soil raster (Ks / Ksat / SatHydCond column in Soil.TBL, m/s)")
        self.radio_ks_soil.setChecked(True)
        self.radio_ks_const = QRadioButton("Fixed value")
        self.ks_group = QButtonGroup(self)
        self.ks_group.addButton(self.radio_ks_soil, 0)
        self.ks_group.addButton(self.radio_ks_const, 1)
        self.ks_group.buttonClicked.connect(self._on_ks_mode_changed)
        kv.addWidget(self.radio_ks_soil, 0, 0, 1, 4)
        kv.addWidget(self.radio_ks_const, 1, 0, 1, 4)
        kv.addWidget(QLabel("K_s value [m/s]:"), 2, 0)
        self.spin_Ks = QDoubleSpinBox()
        self.spin_Ks.setDecimals(8)
        self.spin_Ks.setRange(1e-9, 1e-2)
        self.spin_Ks.setSingleStep(1e-7)
        self.spin_Ks.setValue(1e-6)
        kv.addWidget(self.spin_Ks, 2, 1)
        kv.addWidget(QLabel(
            "<i>Effective (clogged) values: 1e-7 to 1e-6 m/s. "
            "Clean gravel (1e-3+) is rarely representative.</i>"),
            2, 2, 1, 2)
        self.lbl_ks_status = QLabel(
            "K_s column will be detected in Soil.TBL.")
        self.lbl_ks_status.setStyleSheet("color:#555; font-style:italic;")
        kv.addWidget(self.lbl_ks_status, 3, 0, 1, 4)
        outer.addWidget(g_ks)

        # ---- Width ----------------------------------------------------
        g_w = QGroupBox("Channel width")
        wv = QGridLayout(g_w)
        self.radio_w_order = QRadioButton(
            "From stream order (Strahler from topology, or accumulation "
            "proxy when streams come from DEM)")
        self.radio_w_order.setChecked(True)
        self.radio_w_attr = QRadioButton(
            "From shapefile attribute (WIDTH / WIDTH_M / channel_width / W)")
        self.radio_w_const = QRadioButton("Fixed value")
        self.w_group = QButtonGroup(self)
        self.w_group.addButton(self.radio_w_order, 0)
        self.w_group.addButton(self.radio_w_attr, 1)
        self.w_group.addButton(self.radio_w_const, 2)
        self.w_group.buttonClicked.connect(self._on_width_mode_changed)
        wv.addWidget(self.radio_w_order, 0, 0, 1, 4)
        wv.addWidget(self.radio_w_attr, 1, 0, 1, 4)
        wv.addWidget(self.radio_w_const, 2, 0, 1, 4)

        wv.addWidget(QLabel("Fixed width [m]:"), 3, 0)
        self.spin_W = QDoubleSpinBox()
        self.spin_W.setRange(0.5, 200.0)
        self.spin_W.setDecimals(2)
        self.spin_W.setValue(2.0)
        wv.addWidget(self.spin_W, 3, 1)

        wv.addWidget(QLabel("Delta widening factor (order ≥ 6):"), 3, 2)
        self.spin_delta = QDoubleSpinBox()
        self.spin_delta.setRange(1.0, 5.0)
        self.spin_delta.setDecimals(2)
        self.spin_delta.setSingleStep(0.1)
        self.spin_delta.setValue(1.0)
        wv.addWidget(self.spin_delta, 3, 3)

        outer.addWidget(g_w)

        # ---- Losing/gaining threshold + physical cap -----------------
        g_hyd = QGroupBox("Losing / gaining decision & physical cap")
        hv = QGridLayout(g_hyd)
        hv.addWidget(QLabel("Depth-to-water-table threshold [m]:"), 0, 0)
        self.spin_gw_threshold = QDoubleSpinBox()
        self.spin_gw_threshold.setRange(0.0, 100.0)
        self.spin_gw_threshold.setDecimals(2)
        self.spin_gw_threshold.setValue(3.0)
        hv.addWidget(self.spin_gw_threshold, 0, 1)
        hv.addWidget(QLabel(
            "<i>Stream cells with shallower water table are treated as "
            "gaining and skip loss.</i>"), 0, 2, 1, 2)

        hv.addWidget(QLabel("Max infiltration depth [mm/day]:"), 1, 0)
        self.spin_max_infil = QDoubleSpinBox()
        self.spin_max_infil.setRange(1.0, 5000.0)
        self.spin_max_infil.setDecimals(0)
        self.spin_max_infil.setValue(20.0)
        self.spin_max_infil.setToolTip(
            "Physical cap on the daily infiltration depth on a streambed "
            "cell. Typical clogged values: 10–30 mm/day.")
        hv.addWidget(self.spin_max_infil, 1, 1)
        hv.addWidget(QLabel(
            "<i>Prevents unrealistic concentration on headwater cells.</i>"),
            1, 2, 1, 2)

        hv.addWidget(QLabel("Flow-duration fraction (× rainy days):"), 2, 0)
        self.spin_flow_frac = QDoubleSpinBox()
        self.spin_flow_frac.setRange(0.05, 1.0)
        self.spin_flow_frac.setDecimals(2)
        self.spin_flow_frac.setSingleStep(0.05)
        self.spin_flow_frac.setValue(0.30)
        self.spin_flow_frac.setToolTip(
            "Fraction of each rainy day during which ephemeral streams "
            "actually carry water. Typical: 0.20–0.40.")
        hv.addWidget(self.spin_flow_frac, 2, 1)
        hv.addWidget(QLabel(
            "<i>Empirical. 1.0 = streams flow all day, every rainy day.</i>"),
            2, 2, 1, 2)

        outer.addWidget(g_hyd)

        # ---- Actions --------------------------------------------------
        btn_row = QHBoxLayout()
        self.lbl_lock = QLabel("")
        self.lbl_lock.setStyleSheet("color:#b00; font-weight:bold;")
        btn_row.addWidget(self.lbl_lock, 1)

        self.btn_combine = QPushButton("Combine diffusive + focused → Total")
        self.btn_combine.setToolTip(
            "Sum Recharge_diffusive_annual.tif and "
            "Recharge_focused_annual.tif into Recharge_Total_annual.tif.")
        self.btn_combine.clicked.connect(self._combine)
        btn_row.addWidget(self.btn_combine)

        self.btn_run = QPushButton("Run focused recharge")
        self.btn_run.setFixedWidth(200)
        self.btn_run.clicked.connect(self._run)
        btn_row.addWidget(self.btn_run)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        btn_row.addWidget(self.btn_cancel)

        outer.addLayout(btn_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        outer.addWidget(self.progress)

        outer.addWidget(QLabel("Log:"))
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(QFont("Courier", 8))
        self.txt_log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer.addWidget(self.txt_log, 1)

        self._on_environment_changed()
        self._on_ks_mode_changed()
        self._on_width_mode_changed()
        self._on_stream_source_changed()

    # =================================================================
    # Toggle handlers
    # =================================================================
    def _on_environment_changed(self, _=None):
        # Run is always enabled. The climate only tunes the defaults.
        if self.radio_humid.isChecked():
            self.lbl_lock.setText(
                "Note: in humid regions most streams are gaining, so "
                "focused recharge is likely small. The tool will run "
                "anyway and report the losing fraction.")
            # Adjust defaults if user hasn't touched them yet
            if abs(self.spin_Ks.value() - 1e-6) < 1e-12:
                self.spin_Ks.setValue(5e-7)
            if abs(self.spin_flow_frac.value() - 0.30) < 1e-12:
                self.spin_flow_frac.setValue(0.50)
        elif self.radio_arid.isChecked():
            self.lbl_lock.setText("")
            if abs(self.spin_Ks.value() - 1e-6) < 1e-12:
                self.spin_Ks.setValue(2e-6)
            if abs(self.spin_flow_frac.value() - 0.30) < 1e-12:
                self.spin_flow_frac.setValue(0.20)
        else:
            self.lbl_lock.setText("")

    def _on_ks_mode_changed(self, _=None):
        self.spin_Ks.setEnabled(self.radio_ks_const.isChecked())

    def _on_width_mode_changed(self, _=None):
        self.spin_W.setEnabled(self.radio_w_const.isChecked())
        self.spin_delta.setEnabled(self.radio_w_order.isChecked())
        if self.radio_stream_dem.isChecked() and self.radio_w_attr.isChecked():
            self.radio_w_order.setChecked(True)
            self.spin_W.setEnabled(False)
            self.spin_delta.setEnabled(True)

    def _on_stream_source_changed(self, _=None):
        dem = self.radio_stream_dem.isChecked()
        self.txt_stream.setEnabled(not dem)
        self.btn_browse.setEnabled(not dem)
        self.spin_threshold.setEnabled(dem)
        if dem and self.radio_w_attr.isChecked():
            self.radio_w_order.setChecked(True)

    # =================================================================
    # Guard against re-entrant runs
    # =================================================================
    def _is_running(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _current_regime(self) -> str:
        if self.radio_arid.isChecked():
            return "arid"
        if self.radio_semi.isChecked():
            return "semi-arid"
        if self.radio_humid.isChecked():
            return "humid"
        return "mixed"

    # =================================================================
    # Auto-detect
    # =================================================================
    def _auto_detect(self):
        if self._is_running():
            QMessageBox.information(
                self, "Auto-detect",
                "A focused recharge run is already in progress.")
            return

        wd = self.get_working_dir()
        if not wd:
            QMessageBox.warning(self, "Auto-detect",
                                "Set the working directory first.")
            return
        if self.radio_stream_shp.isChecked() and not self.txt_stream.text().strip():
            QMessageBox.warning(self, "Auto-detect",
                                "Select the stream shapefile first.")
            return
        out_dir = self.get_output_dir() or wd
        start, end = self.get_time_range()

        engine = FocusedRechargeEngine(
            working_dir=wd, output_dir=out_dir,
            start_step=start, end_step=end,
            stream_path=self.txt_stream.text().strip() or None,
            stream_source=("dem" if self.radio_stream_dem.isChecked()
                           else "shapefile"),
            dem_stream_threshold=self.spin_threshold.value(),
            gwdepth_threshold_m=self.spin_gw_threshold.value(),
            environment="mixed")
        try:
            res = engine.auto_detect_environment()
        except Exception as exc:
            QMessageBox.critical(self, "Auto-detect failed", str(exc))
            return
        env = res.get("environment", "unknown")
        self.lbl_env_status.setText(res.get("message", ""))
        if env == "arid":
            self.radio_arid.setChecked(True)
        elif env == "semi-arid":
            self.radio_semi.setChecked(True)
        elif env == "humid":
            self.radio_humid.setChecked(True)
        else:
            self.radio_mixed.setChecked(True)
        self._on_environment_changed()

    # =================================================================
    # Browse
    # =================================================================
    def _browse_stream(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select stream shapefile", "",
            "Vector (*.shp *.gpkg *.geojson);;All files (*.*)")
        if path:
            self.txt_stream.setText(path)
            if self.get_working_dir():
                self._auto_detect()

    # =================================================================
    # Engine builder
    # =================================================================
    def _engine(self):
        wd = self.get_working_dir()
        out_dir = self.get_output_dir() or wd
        start, end = self.get_time_range()
        ks_mode = "from_soil" if self.radio_ks_soil.isChecked() else "constant"
        if self.radio_w_order.isChecked():
            wm = "from_order"
        elif self.radio_w_attr.isChecked():
            wm = "from_attribute"
        else:
            wm = "constant"
        return FocusedRechargeEngine(
            working_dir=wd,
            output_dir=out_dir,
            start_step=start,
            end_step=end,
            stream_path=self.txt_stream.text().strip() or None,
            stream_source=("dem" if self.radio_stream_dem.isChecked()
                           else "shapefile"),
            dem_stream_threshold=self.spin_threshold.value(),
            Ks=self.spin_Ks.value(),
            channel_width_m=self.spin_W.value(),
            gwdepth_threshold_m=self.spin_gw_threshold.value(),
            environment=self._current_regime(),
            ks_mode=ks_mode,
            width_mode=wm,
            delta_factor=self.spin_delta.value(),
            max_daily_depth_mm=self.spin_max_infil.value(),
            flow_duration_fraction=self.spin_flow_frac.value())

    # =================================================================
    # Check prerequisites
    # =================================================================
    def _check_stream(self):
        if self._is_running():
            QMessageBox.information(
                self, "Check",
                "A focused recharge run is already in progress.")
            return
        wd = self.get_working_dir()
        if not wd:
            QMessageBox.warning(self, "Check",
                                "Set the working directory first.")
            return
        if self.radio_stream_shp.isChecked() and not self.txt_stream.text().strip():
            QMessageBox.warning(self, "Check",
                                "Select a stream shapefile.")
            return
        try:
            pr = self._engine().check_prerequisites()
        except Exception as exc:
            QMessageBox.critical(self, "Check failed", str(exc))
            return
        lines = [
            f"Stream:   {'OK' if pr['stream_ok'] else 'X'}  {pr['stream_message']}",
            f"Runoff:   {'OK' if pr['months_ok'] else 'X'}  {pr['months_message']}",
            f"GWdepth:  {'OK' if pr['gwdepth_ok'] else 'X'}  {pr['gwdepth_message']}",
            f"K_s:      {'OK' if pr['ks_ok'] else 'X'}  {pr['ks_message']}",
            f"Width:    {'OK' if pr['width_ok'] else 'X'}  {pr['width_message']}",
        ]
        self.txt_log.append("[Check]\n" + "\n".join(lines))
        self.lbl_ks_status.setText(pr["ks_message"])
        QMessageBox.information(self, "Check prerequisites", "\n".join(lines))

    # =================================================================
    # Run
    # =================================================================
    def _run(self):
        if self._is_running():
            QMessageBox.information(
                self, "Focused recharge",
                "A run is already in progress. Wait for it to finish, "
                "or press Cancel first.")
            return

        # Confirmatory message for humid / unknown users
        if self.radio_humid.isChecked() and not self.chk_acknowledge.isChecked():
            QMessageBox.warning(
                self, "Focused recharge",
                "Humid regime selected but the acknowledgement box is "
                "unchecked. Please tick it or change the regime.")
            return

        if self.radio_stream_shp.isChecked() and not self.txt_stream.text().strip():
            QMessageBox.warning(self, "Focused recharge",
                                "Select a stream shapefile, or switch to "
                                "'Derive from DEM'.")
            return
        wd = self.get_working_dir()
        out_dir = self.get_output_dir()
        if not wd or not out_dir:
            QMessageBox.warning(self, "Focused recharge",
                                "Set Working Directory and Output Directory first.")
            return

        start, end = self.get_time_range()
        env = self._current_regime()
        ks_mode = "from_soil" if self.radio_ks_soil.isChecked() else "constant"
        if self.radio_w_order.isChecked():
            wm = "from_order"
        elif self.radio_w_attr.isChecked():
            wm = "from_attribute"
        else:
            wm = "constant"

        # Reset UI state
        self.txt_log.clear()
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        self.worker = FocusedRechargeWorker(
            working_dir=wd,
            output_dir=out_dir,
            start_step=start,
            end_step=end,
            stream_path=self.txt_stream.text().strip() or None,
            stream_source=("dem" if self.radio_stream_dem.isChecked()
                           else "shapefile"),
            dem_stream_threshold=self.spin_threshold.value(),
            Ks=self.spin_Ks.value(),
            channel_width_m=self.spin_W.value(),
            gwdepth_threshold_m=self.spin_gw_threshold.value(),
            environment=env,
            ks_mode=ks_mode,
            width_mode=wm,
            delta_factor=self.spin_delta.value(),
            max_daily_depth_mm=self.spin_max_infil.value(),
            flow_duration_fraction=self.spin_flow_frac.value())

        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._on_log)
        self.worker.finished_ok.connect(self._on_finished_ok)
        self.worker.finished_error.connect(self._on_finished_error)

        self.worker.start()

    def _cancel(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)

    # =================================================================
    # Combine
    # =================================================================
    def _combine(self):
        out_dir = self.get_output_dir()
        if not out_dir:
            QMessageBox.warning(self, "Combine",
                                "Output directory not set.")
            return
        out_dir = Path(out_dir)
        diff = out_dir / "Recharge_diffusive_annual.tif"
        foc = out_dir / "Recharge_focused_annual.tif"
        if not diff.exists() or not foc.exists():
            QMessageBox.warning(
                self, "Combine",
                f"Both files must exist:\n"
                f"  {diff.name}: {'found' if diff.exists() else 'MISSING'}\n"
                f"  {foc.name}: {'found' if foc.exists() else 'MISSING'}")
            return

        rio = RasterIO()
        a, meta = rio.read(diff)
        b, _ = rio.read(foc)
        if a.shape != b.shape:
            QMessageBox.critical(self, "Combine",
                                 f"Shape mismatch: {a.shape} vs {b.shape}")
            return

        total = np.nansum(np.stack([a, b]), axis=0)
        both_nan = np.isnan(a) & np.isnan(b)
        total[both_nan] = np.nan

        rio.write(out_dir / "Recharge_Total_annual.tif", total, meta)
        layer = QgsRasterLayer(
            str(out_dir / "Recharge_Total_annual.tif"),
            "Recharge Total (mm/year)")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        self.txt_log.append(
            "Combined:\n"
            f"  {diff.name}\n"
            f"  {foc.name}\n"
            f"→ Recharge_Total_annual.tif")

    # =================================================================
    # Worker signals
    # =================================================================
    def _on_progress(self, step, total, msg):
        if total > 0:
            self.progress.setValue(int(100 * step / total))

    def _on_log(self, msg):
        self.txt_log.append(msg)
        sb = self.txt_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_finished_ok(self, results):
        self.worker = None

        self.progress.setValue(100)
        self.progress.setFormat("Done (%p%)")
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)

        out_dir = Path(results.get("output_dir", ""))
        annual = out_dir / "Recharge_focused_annual.tif"
        if annual.exists():
            layer = QgsRasterLayer(str(annual),
                                   "Recharge Focused (mm/year)")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)

        avg_mm = results.get("annual_avg_mm", 0.0)
        frac = results.get("annual_loss_fraction", 0.0)
        streambed_mm = results.get("streambed_avg_mm", 0.0)
        total_foc = results.get("total_focused_m3", 0.0)
        total_av = results.get("total_available_m3", 0.0)
        total_out = results.get("total_outlet_m3", 0.0)
        area_km2 = results.get("catchment_area_m2", 0.0) / 1e6
        losing_frac = results.get("losing_fraction", 0.0)
        n_stream = results.get("n_stream_cells", 0)

        self.txt_log.append("\n" + "=" * 55)
        self.txt_log.append("  FOCUSED RECHARGE SUMMARY")
        self.txt_log.append("=" * 55)
        self.txt_log.append(f"  Regime                    : "
                            f"{self._current_regime().upper()}")
        self.txt_log.append(f"  Catchment area            : {area_km2:.3f} km²")
        self.txt_log.append(f"  Stream cells (total)      : {n_stream}")
        self.txt_log.append(f"  Treated as losing         : "
                            f"{losing_frac*100:.1f} %")
        self.txt_log.append(f"  Treated as gaining        : "
                            f"{(1-losing_frac)*100:.1f} %")
        self.txt_log.append(f"  Total runoff generated    : {total_av:.3e} m³")
        self.txt_log.append(f"  Total focused recharge    : {total_foc:.3e} m³")
        self.txt_log.append(f"  Routed to outlets         : {total_out:.3e} m³")
        self.txt_log.append(f"  Loss fraction             : {frac*100:.2f} %")
        self.txt_log.append(f"  CATCHMENT-AVERAGE focused : {avg_mm:.3f} mm/year")
        self.txt_log.append(f"  Streambed-average infilt. : {streambed_mm:.3f} mm/year")
        self.txt_log.append("=" * 55)
        self.txt_log.append(
            "  The CATCHMENT-AVERAGE is the number to compare with the "
            "diffusive recharge mean. The per-cell raster shows the "
            "concentrated streambed infiltration.")
        self.txt_log.append("")

        QMessageBox.information(
            self, "Focused Recharge",
            f"Focused recharge complete.\n\n"
            f"Losing stream cells: {losing_frac*100:.1f} %\n"
            f"Catchment-average focused recharge: {avg_mm:.3f} mm/year\n"
            f"Streambed-average infiltration: {streambed_mm:.3f} mm/year\n"
            f"Loss fraction: {frac*100:.2f} %\n\n"
            f"Output: {out_dir}")

    def _on_finished_error(self, err):
        self.worker = None
        self.progress.setFormat("Failed")
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.txt_log.append("\n[ERROR]\n" + err)
        QMessageBox.critical(self, "Focused Recharge",
                             f"Focused recharge failed:\n\n{err[:600]}")