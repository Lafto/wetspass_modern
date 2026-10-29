# -*- coding: utf-8 -*-
"""
Focused Recharge tab — universal focused-recharge GUI with
auto-checkpointing support.

Combine methods (selectable at runtime, default = Addition):
    Addition              R_total = R_diffusive + R_focused
    Replace-by-max        R_total = max(R_diffusive, R_focused)
    Focused-over-diff     R_total = R_focused on stream cells else R_diffusive

Physical cap
------------
When enabled, the combine result is clipped at an upper bound derived
from the mean annual precipitation (automatically read from the monthly
rain rasters in the working directory). The climate zone is classified
using the standard table:

    P (mm/yr)          Recharge fraction      Max plausible recharge
    100 - 300          1 - 5 %                5 - 15 mm/yr
    300 - 700          5 - 15 %               15 - 100 mm/yr
    700 - 1500         20 - 35 %              150 - 500 mm/yr
    1500 - 3000        30 - 50 %              450 - 1500 mm/yr
    3000 - 5000        40 - 60 %              1200 - 3000 mm/yr

The cap is set to the *upper* bound of the matched zone
(frac_upper × mean_annual_P), so the cap never clips legitimate values.
The user can override with a custom value, and a Preview button reports
how many cells would be clipped before committing to a combine run.

Climate regime auto-selection
-----------------------------
The climate-regime radio group is auto-selected from the same mean
annual P computed for the cap:

    P < 300 mm/yr        -> Arid
    300 <= P < 700       -> Semi-arid
    700 <= P < 1200      -> Mixed / transitional
    P >= 1200            -> Humid

The gwdepth-based classification remains available as a secondary
cross-check via the "Auto-detect from gwdepth (secondary)" button.
"""

import contextlib
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


# =====================================================================
# Climate zones for the physical cap (P_min, P_max, frac_min, frac_max)
# =====================================================================
CLIMATE_ZONES = [
    (0,     300,   0.01, 0.05, "Arid"),
    (300,   700,   0.05, 0.15, "Semi-arid"),
    (700,   1500,  0.20, 0.35, "Humid temperate"),
    (1500,  3000,  0.30, 0.50, "Wet tropical"),
    (3000,  5000,  0.40, 0.60, "Extreme cloud forest / karst"),
]


def classify_climate(mean_P_mm):
    """Return a dict describing the zone matched to `mean_P_mm`."""
    if mean_P_mm is None or mean_P_mm <= 0:
        return None
    for pmin, pmax, fmin, fmax, name in CLIMATE_ZONES:
        if pmin <= mean_P_mm < pmax:
            return {
                "name": name,
                "P_min": pmin, "P_max": pmax,
                "frac_min": fmin, "frac_max": fmax,
                "cap_recommended": mean_P_mm * fmax,
                "cap_min": mean_P_mm * fmin,
            }
    if mean_P_mm >= 5000:
        return {
            "name": "Extreme (> 5000 mm/yr)",
            "P_min": 5000, "P_max": float("inf"),
            "frac_min": 0.40, "frac_max": 0.60,
            "cap_recommended": mean_P_mm * 0.60,
            "cap_min": mean_P_mm * 0.40,
        }
    return None


# =====================================================================
# Climate regime auto-selection from mean annual P
# (P_min, P_max, regime_id, label). First match wins.
# =====================================================================
REGIME_FROM_P = [
    (0,     300,          "arid",      "Arid"),
    (300,   700,          "semi-arid", "Semi-arid"),
    (700,   1200,         "mixed",     "Mixed / transitional"),
    (1200,  float("inf"), "humid",     "Humid"),
]


def regime_from_P(mean_P_mm):
    """Return (regime_id, label) for a given mean annual P, or None."""
    if mean_P_mm is None or mean_P_mm <= 0:
        return None
    for pmin, pmax, rid, label in REGIME_FROM_P:
        if pmin <= mean_P_mm < pmax:
            return rid, label
    return None


class FocusedRechargeTab(QWidget):

    request_log = pyqtSignal(str)

    def __init__(self, get_working_dir, get_output_dir,
                 get_time_range, parent=None):
        super().__init__(parent)
        self.get_working_dir = get_working_dir
        self.get_output_dir = get_output_dir
        self.get_time_range = get_time_range
        self.worker = None

        # Cached rainfall-derived state
        self._mean_annual_rainfall_mm = None
        self._recommended_cap_mm = None
        self._climate_info = None

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
            "losing / gaining split.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        self.resume_banner = QGroupBox(
            "Paused focused recharge run detected")
        self.resume_banner.setStyleSheet(
            "QGroupBox { background: #fff7e0; border: 1px solid #d6a800; "
            "border-radius: 4px; margin-top: 8px; padding: 8px; } "
            "QGroupBox::title { color: #8a6300; }")
        rb_layout = QHBoxLayout(self.resume_banner)
        self.resume_label = QLabel()
        self.resume_label.setWordWrap(True)
        rb_layout.addWidget(self.resume_label, 1)
        self.btn_discard_ck = QPushButton("Discard checkpoint")
        self.btn_discard_ck.setFixedWidth(150)
        self.btn_discard_ck.clicked.connect(self._discard_checkpoint)
        rb_layout.addWidget(self.btn_discard_ck)
        self.resume_banner.hide()
        outer.addWidget(self.resume_banner)

        self.chk_auto_checkpoint = QCheckBox(
            "Enable automatic checkpointing "
            "(saves progress after every month)")
        self.chk_auto_checkpoint.setChecked(True)
        outer.addWidget(self.chk_auto_checkpoint)

        # ---- Climate regime ------------------------------------------
        g_env = QGroupBox(
            "Climate regime (guidance only — Run is never blocked)")
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
        self.btn_detect = QPushButton(
            "Auto-detect from gwdepth (secondary)")
        self.btn_detect.setToolTip(
            "Classify the climate regime from mean depth-to-water-table\n"
            "under stream cells. Secondary to the rainfall-based\n"
            "classification, which is more reliable when gwdepth\n"
            "rasters come from sparse borehole interpolation.")
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
            "I understand the module reports only the losing component.")
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
        self.stream_group.buttonClicked.connect(
            self._on_stream_source_changed)

        sv.addWidget(self.radio_stream_dem, 0, 0, 1, 4)
        sv.addWidget(QLabel("Stream threshold [cells]:"), 1, 0)
        self.spin_threshold = QSpinBox()
        self.spin_threshold.setRange(50, 1_000_000)
        self.spin_threshold.setValue(500)
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

        # ---- Ks -------------------------------------------------------
        g_ks = QGroupBox("Streambed hydraulic conductivity (K_s)")
        kv = QGridLayout(g_ks)
        self.radio_ks_soil = QRadioButton(
            "From soil raster (Ks / Ksat / SatHydCond column "
            "in Soil.TBL, m/s)")
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
            "<i>Effective (clogged) values: 1e-7 to 1e-6 m/s.</i>"),
            2, 2, 1, 2)
        self.lbl_ks_status = QLabel(
            "K_s column will be detected in Soil.TBL.")
        self.lbl_ks_status.setStyleSheet(
            "color:#555; font-style:italic;")
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
            "From shapefile attribute "
            "(WIDTH / WIDTH_M / channel_width / W)")
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
        wv.addWidget(QLabel("Delta widening factor (order >= 6):"),
                     3, 2)
        self.spin_delta = QDoubleSpinBox()
        self.spin_delta.setRange(1.0, 5.0)
        self.spin_delta.setDecimals(2)
        self.spin_delta.setSingleStep(0.1)
        self.spin_delta.setValue(1.0)
        wv.addWidget(self.spin_delta, 3, 3)
        outer.addWidget(g_w)

        # ---- Losing/gaining ------------------------------------------
        g_hyd = QGroupBox(
            "Losing / gaining decision & physical cap")
        hv = QGridLayout(g_hyd)
        hv.addWidget(QLabel(
            "Depth-to-water-table threshold [m]:"), 0, 0)
        self.spin_gw_threshold = QDoubleSpinBox()
        self.spin_gw_threshold.setRange(0.0, 100.0)
        self.spin_gw_threshold.setDecimals(2)
        self.spin_gw_threshold.setValue(3.0)
        hv.addWidget(self.spin_gw_threshold, 0, 1)
        hv.addWidget(QLabel(
            "<i>Stream cells with shallower water table are "
            "gaining.</i>"),
            0, 2, 1, 2)

        hv.addWidget(QLabel(
            "Max infiltration depth [mm/day]:"), 1, 0)
        self.spin_max_infil = QDoubleSpinBox()
        self.spin_max_infil.setRange(0.1, 5000.0)
        self.spin_max_infil.setDecimals(1)
        self.spin_max_infil.setValue(5.0)
        hv.addWidget(self.spin_max_infil, 1, 1)
        hv.addWidget(QLabel(
            "<i>Clay: 0.1-0.5 | Silt: 0.5-2 | Sand: 2-10 | Gravel: 10-100 (mm/day)</i>"),
            1, 2, 1, 2)

        hv.addWidget(QLabel(
            "Flow-duration fraction (x rainy days):"), 2, 0)
        self.spin_flow_frac = QDoubleSpinBox()
        self.spin_flow_frac.setRange(0.05, 1.0)
        self.spin_flow_frac.setDecimals(2)
        self.spin_flow_frac.setSingleStep(0.05)
        self.spin_flow_frac.setValue(0.30)
        hv.addWidget(self.spin_flow_frac, 2, 1)
        hv.addWidget(QLabel(
            "<i>Empirical. 1.0 = streams flow all day, every rainy "
            "day.</i>"),
            2, 2, 1, 2)
        outer.addWidget(g_hyd)

        # =============================================================
        # Combine method group
        # =============================================================
        g_combine = QGroupBox(
            "Combine method — diffusive + focused -> total")
        cv = QVBoxLayout(g_combine)

        self.radio_combine_add = QRadioButton(
            "Addition (recommended) — "
            "R_total = R_diffusive + R_focused")
        self.radio_combine_add.setToolTip(
            "Correct when diffusive and focused recharge are physically\n"
            "independent pathways, which is the standard WetSpass-M\n"
            "assumption. Each water molecule is counted exactly once.")
        self.radio_combine_max = QRadioButton(
            "Replace-by-max (conservative) — "
            "R_total = max(R_diffusive, R_focused)")
        self.radio_combine_max.setToolTip(
            "Picks the larger of the two values at each cell. Avoids any\n"
            "risk of double-counting, but systematically underestimates\n"
            "total recharge at stream cells by min(R_diff, R_foc).")
        self.radio_combine_foc = QRadioButton(
            "Focused-over-diffusive — R_total = R_focused on stream "
            "cells, R_diffusive elsewhere")
        self.radio_combine_foc.setToolTip(
            "Use this only if you believe the diffusive model already\n"
            "includes some streambed loss and you want to replace it\n"
            "with the focused estimate at stream cells.")

        self.radio_combine_add.setChecked(True)

        self.combine_group = QButtonGroup(self)
        self.combine_group.addButton(self.radio_combine_add, 0)
        self.combine_group.addButton(self.radio_combine_max, 1)
        self.combine_group.addButton(self.radio_combine_foc, 2)
        self.combine_group.buttonClicked.connect(
            self._on_combine_method_changed)

        cv.addWidget(self.radio_combine_add)
        cv.addWidget(self.radio_combine_max)
        cv.addWidget(self.radio_combine_foc)

        # --- Physical cap sub-box ------------------------------------
        cap_box = QGroupBox(
            "Physical cap — derived from mean annual rainfall")
        capv = QVBoxLayout(cap_box)

        # Row 1: rainfall + recompute
        row1 = QHBoxLayout()
        self.lbl_rain = QLabel("Annual rainfall: — mm/yr")
        self.lbl_rain.setStyleSheet(
            "font-family:monospace; font-size:10pt;")
        row1.addWidget(self.lbl_rain, 1)

        self.btn_recompute_rain = QPushButton("Recompute from inputs")
        self.btn_recompute_rain.setToolTip(
            "Read the monthly rain*.tif rasters from the working\n"
            "directory, compute mean annual precipitation, classify\n"
            "the climate zone, and auto-select the climate regime.")
        self.btn_recompute_rain.clicked.connect(
            self._recompute_annual_rainfall)
        row1.addWidget(self.btn_recompute_rain)
        capv.addLayout(row1)

        # Row 2: climate zone
        self.lbl_climate = QLabel("Climate zone: —")
        self.lbl_climate.setStyleSheet(
            "font-family:monospace; font-size:10pt;")
        self.lbl_climate.setWordWrap(True)
        capv.addWidget(self.lbl_climate)

        # Row 3: recommended cap
        self.lbl_recommended_cap = QLabel(
            "Recommended max recharge: — mm/yr")
        self.lbl_recommended_cap.setStyleSheet(
            "font-family:monospace; font-size:10pt; font-weight:bold; "
            "color:#0a5;")
        capv.addWidget(self.lbl_recommended_cap)

        # Row 4: cap controls
        cap_row = QHBoxLayout()
        self.chk_cap = QCheckBox("Apply this cap during combine")
        self.chk_cap.setToolTip(
            "When ticked, the combined output is clipped at the\n"
            "recommended max recharge. This is a safety net — it\n"
            "hides the symptoms of upstream routing outliers but does\n"
            "not fix them.")
        self.chk_cap.toggled.connect(self._on_cap_toggled)
        cap_row.addWidget(self.chk_cap)

        self.chk_override_cap = QCheckBox("Override with:")
        self.chk_override_cap.setToolTip(
            "Tick to enter a custom cap instead of the recommended "
            "value.")
        self.chk_override_cap.toggled.connect(
            self._on_override_cap_toggled)
        cap_row.addWidget(self.chk_override_cap)

        self.spin_cap = QDoubleSpinBox()
        self.spin_cap.setRange(0.0, 100000.0)
        self.spin_cap.setDecimals(1)
        self.spin_cap.setSingleStep(50.0)
        self.spin_cap.setValue(0.0)
        self.spin_cap.setSuffix(" mm/year")
        self.spin_cap.setEnabled(False)
        cap_row.addWidget(self.spin_cap)

        self.btn_preview_cap = QPushButton("Preview cap effect")
        self.btn_preview_cap.setToolTip(
            "Scan the combine inputs and report how many cells would\n"
            "be clipped, their value distribution, and the before/after\n"
            "statistics. Nothing is written to disk.")
        self.btn_preview_cap.clicked.connect(self._preview_cap_effect)
        cap_row.addWidget(self.btn_preview_cap)

        self.chk_cap_focused = QCheckBox(
            "Also write capped focused raster")
        self.chk_cap_focused.setToolTip(
            "When ticked, a second file is written alongside the\n"
            "original:\n"
            "    Recharge_focused_annual_capped.tif\n"
            "with the same cap applied. The original focused raster\n"
            "is never overwritten.")
        cap_row.addWidget(self.chk_cap_focused)

        cap_row.addStretch(1)
        capv.addLayout(cap_row)

        note = QLabel(
            "<i>Standard reference table (Arid -> Extreme cloud "
            "forest): the recommended cap equals the upper bound of "
            "the plausible recharge fraction for the matched zone "
            "(e.g. 35 % of P for humid temperate).</i>")
        note.setWordWrap(True)
        note.setStyleSheet("color:#555;")
        capv.addWidget(note)

        cv.addWidget(cap_box)
        outer.addWidget(g_combine)

        # ---- Action buttons ------------------------------------------
        btn_row = QHBoxLayout()
        self.lbl_lock = QLabel("")
        self.lbl_lock.setStyleSheet("color:#b00; font-weight:bold;")
        btn_row.addWidget(self.lbl_lock, 1)

        self.btn_combine = QPushButton("Combine -> Total recharge")
        self.btn_combine.setToolTip(
            "Combine Recharge_diffusive_annual.tif and\n"
            "Recharge_focused_annual.tif into "
            "Recharge_Total_annual.tif\n"
            "using the method selected above.")
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
        self.txt_log.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding)
        outer.addWidget(self.txt_log, 1)

        self._on_environment_changed()
        self._on_ks_mode_changed()
        self._on_width_mode_changed()
        self._on_stream_source_changed()
        self._refresh_resume_banner()

    # =================================================================
    # State helpers
    # =================================================================
    def _refresh_resume_banner(self):
        out_dir = self.get_output_dir() or ""
        if not out_dir:
            self.resume_banner.hide()
            return
        try:
            ck = FocusedRechargeEngine.read_checkpoint(out_dir)
        except Exception as exc:
            print("read_checkpoint failed:", exc)
            ck = None
        if ck is None:
            self.resume_banner.hide()
            return
        next_m = ck.get("next_month", "?")
        done = ck.get("completed_months", [])
        paused_at = ck.get("paused_at", "?")
        self.resume_label.setText(
            f"<b>Resumable focused run found.</b><br>"
            f"Paused at {paused_at}. Completed months: {done}. "
            f"Next month to process: {next_m}.<br>"
            f"Clicking <b>Run</b> will resume automatically.")
        self.resume_banner.show()

    def _discard_checkpoint(self):
        out_dir = self.get_output_dir() or ""
        if not out_dir:
            return
        if QMessageBox.question(
                self, "Discard focused checkpoint",
                "Delete the paused focused recharge checkpoint?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                ) != QMessageBox.StandardButton.Yes:
            return
        try:
            FocusedRechargeEngine.discard_checkpoint(out_dir)
            self.txt_log.append("[Focused] Checkpoint deleted.")
        except Exception as exc:
            QMessageBox.warning(self, "Discard checkpoint", str(exc))
        self._refresh_resume_banner()

    def _on_environment_changed(self, _=None):
        if self.radio_humid.isChecked():
            self.lbl_lock.setText(
                "Note: in humid regions most streams are gaining.")
        else:
            self.lbl_lock.setText("")

    def _set_regime_radio(self, regime_id):
        """Programmatically select the climate-regime radio button."""
        if regime_id == "arid":
            self.radio_arid.setChecked(True)
        elif regime_id == "semi-arid":
            self.radio_semi.setChecked(True)
        elif regime_id == "humid":
            self.radio_humid.setChecked(True)
        else:
            self.radio_mixed.setChecked(True)
        self._on_environment_changed()

    def _on_ks_mode_changed(self, _=None):
        self.spin_Ks.setEnabled(self.radio_ks_const.isChecked())

    def _on_width_mode_changed(self, _=None):
        self.spin_W.setEnabled(self.radio_w_const.isChecked())
        self.spin_delta.setEnabled(self.radio_w_order.isChecked())

    def _on_stream_source_changed(self, _=None):
        dem = self.radio_stream_dem.isChecked()
        self.txt_stream.setEnabled(not dem)
        self.btn_browse.setEnabled(not dem)
        self.spin_threshold.setEnabled(dem)

    def _on_combine_method_changed(self, _=None):
        pass

    def _on_cap_toggled(self, checked):
        self.spin_cap.setEnabled(
            checked and self.chk_override_cap.isChecked())

    def _on_override_cap_toggled(self, checked):
        self.spin_cap.setEnabled(checked and self.chk_cap.isChecked())

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

    def _current_combine_method(self) -> str:
        if self.radio_combine_max.isChecked():
            return "max"
        if self.radio_combine_foc.isChecked():
            return "focused_over_diffusive"
        return "addition"

    # =================================================================
    # Rainfall-derived physical cap + climate regime
    # =================================================================
    def _recompute_annual_rainfall(self):
        """
        Read the monthly rainfall rasters, compute mean annual P,
        classify the climate zone (for the cap), auto-select the
        matching climate-regime radio button, and fill the
        recommended cap.
        """
        mean_P, msg = self._read_mean_annual_rainfall()
        self.txt_log.append(f"[Cap] {msg}")

        if mean_P is None:
            self.lbl_rain.setText("Annual rainfall: — mm/yr")
            self.lbl_climate.setText("Climate zone: —")
            self.lbl_recommended_cap.setText(
                "Recommended max recharge: — mm/yr")
            self._mean_annual_rainfall_mm = None
            self._recommended_cap_mm = None
            self._climate_info = None
            QMessageBox.warning(
                self, "Recompute rainfall",
                f"Could not compute mean annual rainfall.\n\n{msg}")
            return

        info = classify_climate(mean_P)
        self._mean_annual_rainfall_mm = float(mean_P)
        self._climate_info = info
        self._recommended_cap_mm = (
            float(info["cap_recommended"]) if info else None
        )

        self.lbl_rain.setText(
            f"Annual rainfall: {mean_P:,.1f} mm/yr")
        if info is not None:
            frac_min_pct = info["frac_min"] * 100.0
            frac_max_pct = info["frac_max"] * 100.0
            self.lbl_climate.setText(
                f"Climate zone: {info['name']} "
                f"({info['P_min']}–{info['P_max']} mm, "
                f"{frac_min_pct:.0f}–{frac_max_pct:.0f}% recharge)")
            self.lbl_recommended_cap.setText(
                f"Recommended max recharge: "
                f"{info['cap_recommended']:,.1f} mm/yr "
                f"({frac_max_pct:.0f}% x {mean_P:,.1f} mm/yr)")

            if not self.chk_override_cap.isChecked():
                self.spin_cap.setValue(info["cap_recommended"])
        else:
            self.lbl_climate.setText("Climate zone: unknown")
            self.lbl_recommended_cap.setText(
                "Recommended max recharge: — mm/yr")

        # ---- Auto-select the climate-regime radio from rainfall ------
        rr = regime_from_P(mean_P)
        if rr is not None:
            rid, label = rr
            self._set_regime_radio(rid)
            self.lbl_env_status.setText(
                f"Regime auto-selected from rainfall:\n"
                f"  P = {mean_P:,.1f} mm/yr -> {label}\n"
                f"  Thresholds: <300 arid | "
                f"300-700 semi-arid | "
                f"700-1200 mixed | "
                f">=1200 humid\n"
                f"  (click 'Auto-detect from gwdepth (secondary)' "
                f"to cross-check with depth-to-water-table)")
            self.txt_log.append(
                f"[Cap] Climate regime auto-set to '{label}' "
                f"from P = {mean_P:,.1f} mm/yr.")

    def _read_mean_annual_rainfall(self):
        """
        Read monthly rain rasters and return (mean_annual_mm, message).
        """
        try:
            import rasterio
            from rasterio.windows import Window
        except Exception as exc:
            return None, f"rasterio import failed: {exc}"

        try:
            from ..core.workflow import resolve_input_dirs
        except Exception as exc:
            return None, f"workflow import failed: {exc}"

        wd = self.get_working_dir()
        if not wd:
            return None, "Working directory not set."

        try:
            maps_dir, _, _ = resolve_input_dirs(wd)
        except Exception as exc:
            return None, f"Cannot resolve input dirs: {exc}"

        start, end = self.get_time_range()
        months = list(range(start, end + 1))

        candidate_dirs = [
            maps_dir / "rain",
            maps_dir / "Rain",
            maps_dir / "rainfall",
            maps_dir / "Rainfall",
            maps_dir,
        ]

        total_sum = 0.0
        total_count = 0
        missing = []
        tile = 2048

        for m in months:
            p = None
            for d in candidate_dirs:
                if not d.is_dir():
                    continue
                for ext in (".tif", ".asc"):
                    cand = d / f"rain{m}{ext}"
                    if cand.exists():
                        p = cand
                        break
                if p is not None:
                    break

            if p is None:
                missing.append(m)
                continue

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
                return None, f"Failed reading rain{m}: {exc}"

        if total_count == 0:
            return None, "No rainfall rasters found in the working dir."

        msg = (f"Mean annual P computed from {total_count} monthly "
               f"raster(s): {total_sum:,.1f} mm/yr.")
        if missing:
            msg += f" Missing months: {missing}."
        return total_sum, msg

    # =================================================================
    # Preview cap effect
    # =================================================================
    def _preview_cap_effect(self):
        """
        Scan the combine inputs, apply the currently selected combine
        method and cap in memory (tile-by-tile), and report how many
        cells would be clipped and what the before/after statistics
        would be. Nothing is written to disk.
        """
        import rasterio
        from rasterio.windows import Window

        out_dir = self.get_output_dir()
        if not out_dir:
            QMessageBox.warning(self, "Preview",
                                "Output directory not set.")
            return
        out_dir = Path(out_dir)
        diff = out_dir / "Recharge_diffusive_annual.tif"
        foc = out_dir / "Recharge_focused_annual.tif"
        if not diff.exists() or not foc.exists():
            QMessageBox.warning(
                self, "Preview",
                f"Both files must exist:\n"
                f"  {diff.name}: "
                f"{'found' if diff.exists() else 'MISSING'}\n"
                f"  {foc.name}:  "
                f"{'found' if foc.exists() else 'MISSING'}")
            return

        cap_mm = None
        if self.chk_cap.isChecked():
            if self.chk_override_cap.isChecked():
                cap_mm = float(self.spin_cap.value())
                if cap_mm <= 0:
                    cap_mm = None
            else:
                if self._recommended_cap_mm is None:
                    QMessageBox.warning(
                        self, "Preview",
                        "You enabled the cap but no rainfall has been\n"
                        "read yet. Click 'Recompute from inputs' first,\n"
                        "or tick 'Override with' and enter a value.")
                    return
                cap_mm = float(self._recommended_cap_mm)

        method = self._current_combine_method()
        method_label = {
            "addition": "addition",
            "max": "replace-by-max",
            "focused_over_diffusive": "focused-over-diffusive",
        }.get(method, method)

        self.txt_log.append("")
        self.txt_log.append("=" * 60)
        self.txt_log.append("[Preview] Scanning combine inputs…")
        self.txt_log.append(
            f"[Preview] method = {method_label}, "
            f"cap = "
            f"{'none' if cap_mm is None else f'{cap_mm:.1f} mm/yr'}")

        try:
            dsrc = rasterio.open(str(diff))
            fsrc = rasterio.open(str(foc))
        except Exception as exc:
            QMessageBox.critical(self, "Preview", f"Open failed: {exc}")
            return

        try:
            if (dsrc.width, dsrc.height) != (fsrc.width, fsrc.height):
                QMessageBox.critical(
                    self, "Preview",
                    "Grids do not match. Cannot preview.")
                return

            H, W = dsrc.height, dsrc.width
            d_nd = (dsrc.nodata if dsrc.nodata is not None
                    else -9999.0)
            f_nd = (fsrc.nodata if fsrc.nodata is not None
                    else -9999.0)

            raw_sum = 0.0
            raw_max = float("-inf")
            raw_min = float("inf")

            capped_sum = 0.0
            capped_max = float("-inf")
            capped_min = float("inf")

            n_valid = 0
            n_clipped = 0
            clipped_ratios = []

            tile = 2048
            for r0 in range(0, H, tile):
                h = min(tile, H - r0)
                for c0 in range(0, W, tile):
                    w = min(tile, W - c0)
                    win = Window(c0, r0, w, h)

                    a = dsrc.read(1, window=win).astype(np.float64)
                    b = fsrc.read(1, window=win).astype(np.float64)

                    a = np.where(a == d_nd, np.nan, a)
                    b = np.where(b == f_nd, np.nan, b)
                    a = np.where(np.isfinite(a), a, np.nan)
                    b = np.where(np.isfinite(b), b, np.nan)

                    both = np.isfinite(a) & np.isfinite(b)
                    only_a = np.isfinite(a) & ~np.isfinite(b)
                    only_b = ~np.isfinite(a) & np.isfinite(b)

                    out = np.full(a.shape, np.nan, dtype=np.float64)

                    if method == "addition":
                        out[both] = a[both] + b[both]
                        out[only_a] = a[only_a]
                        out[only_b] = b[only_b]
                    elif method == "max":
                        out[both] = np.maximum(a[both], b[both])
                        out[only_a] = a[only_a]
                        out[only_b] = b[only_b]
                    elif method == "focused_over_diffusive":
                        out[both] = b[both]
                        out[only_a] = a[only_a]
                        out[only_b] = b[only_b]
                    else:
                        out[both] = a[both] + b[both]
                        out[only_a] = a[only_a]
                        out[only_b] = b[only_b]

                    fin = np.isfinite(out)
                    n_valid += int(fin.sum())

                    if fin.any():
                        vals = out[fin]
                        raw_sum += float(vals.sum())
                        raw_max = max(raw_max, float(vals.max()))
                        raw_min = min(raw_min, float(vals.min()))

                    if cap_mm is not None and fin.any():
                        above = fin & (out > cap_mm)
                        n_here = int(above.sum())
                        if n_here > 0:
                            n_clipped += n_here
                            ratios = out[above] / cap_mm
                            clipped_ratios.append(ratios)

                        capped_vals = np.where(
                            fin, np.minimum(out, cap_mm), np.nan)
                        cf = np.isfinite(capped_vals)
                        if cf.any():
                            cv = capped_vals[cf]
                            capped_sum += float(cv.sum())
                            capped_max = max(
                                capped_max, float(cv.max()))
                            capped_min = min(
                                capped_min, float(cv.min()))
                    else:
                        if fin.any():
                            capped_sum += float(vals.sum())
                            capped_max = max(
                                capped_max, float(vals.max()))
                            capped_min = min(
                                capped_min, float(vals.min()))

                    del a, b, out

        except Exception as exc:
            QMessageBox.critical(self, "Preview failed", str(exc))
            self.txt_log.append(f"[Preview error] {exc}")
            with contextlib.suppress(Exception):
                dsrc.close()
            with contextlib.suppress(Exception):
                fsrc.close()
            return

        with contextlib.suppress(Exception):
            dsrc.close()
        with contextlib.suppress(Exception):
            fsrc.close()

        if n_valid == 0:
            self.txt_log.append("[Preview] No valid cells found.")
            QMessageBox.information(
                self, "Preview cap effect",
                "No valid cells found in the inputs.")
            return

        raw_mean = raw_sum / n_valid
        capped_mean = capped_sum / n_valid if n_valid else 0.0

        self.txt_log.append("")
        self.txt_log.append("-" * 60)
        self.txt_log.append("CAP EFFECT PREVIEW")
        self.txt_log.append("-" * 60)
        self.txt_log.append(
            f"  Valid cells             : {n_valid:,}")
        self.txt_log.append(
            f"  Method                  : {method_label}")
        self.txt_log.append(
            f"  Cap                     : "
            f"{'none' if cap_mm is None else f'{cap_mm:,.1f} mm/yr'}")

        self.txt_log.append("")
        self.txt_log.append("  BEFORE cap")
        self.txt_log.append(
            f"    Min                   : {raw_min:,.3f} mm/yr")
        self.txt_log.append(
            f"    Mean                  : {raw_mean:,.3f} mm/yr")
        self.txt_log.append(
            f"    Max                   : {raw_max:,.3f} mm/yr")

        if cap_mm is not None:
            self.txt_log.append("")
            self.txt_log.append("  AFTER cap")
            self.txt_log.append(
                f"    Min                   : "
                f"{capped_min:,.3f} mm/yr")
            self.txt_log.append(
                f"    Mean                  : "
                f"{capped_mean:,.3f} mm/yr")
            self.txt_log.append(
                f"    Max                   : "
                f"{capped_max:,.3f} mm/yr")

            pct_clipped = 100.0 * n_clipped / n_valid
            mean_drop = raw_mean - capped_mean
            self.txt_log.append("")
            self.txt_log.append("  CLIPPING SUMMARY")
            self.txt_log.append(
                f"    Cells clipped         : {n_clipped:,} "
                f"({pct_clipped:.4f} % of valid cells)")
            self.txt_log.append(
                f"    Mean drop (all cells) : "
                f"{mean_drop:,.4f} mm/yr")
            if n_clipped > 0:
                self.txt_log.append(
                    f"    Overall max reduction : "
                    f"{raw_max - capped_max:,.3f} mm/yr")

                all_ratios = np.concatenate(clipped_ratios)
                bins = [(1.0, 1.5), (1.5, 2.0), (2.0, 5.0),
                        (5.0, 10.0), (10.0, 50.0),
                        (50.0, 100.0), (100.0, float("inf"))]
                self.txt_log.append("")
                self.txt_log.append(
                    "  Distribution of clipped values (x cap):")
                for lo, hi in bins:
                    n_here = int(((all_ratios >= lo)
                                  & (all_ratios < hi)).sum())
                    if n_here == 0:
                        continue
                    hi_label = ("inf" if hi == float("inf")
                                else f"{hi:g}")
                    self.txt_log.append(
                        f"    {lo:>5g}x – {hi_label:>5}   "
                        f"{n_here:>10,} cells")

        self.txt_log.append("-" * 60)
        self.txt_log.append("")

        if cap_mm is None:
            msg = (f"No cap is currently enabled.\n\n"
                   f"Method: {method_label}\n"
                   f"Valid cells: {n_valid:,}\n"
                   f"Range: {raw_min:,.1f} to {raw_max:,.1f} mm/yr\n"
                   f"Mean:  {raw_mean:,.3f} mm/yr\n\n"
                   f"Tick 'Apply this cap during combine' to see the "
                   f"clipping effect.")
        else:
            msg = (f"Method: {method_label}\n"
                   f"Cap:    {cap_mm:,.1f} mm/yr\n\n"
                   f"Before cap:\n"
                   f"  min  = {raw_min:,.1f} mm/yr\n"
                   f"  mean = {raw_mean:,.3f} mm/yr\n"
                   f"  max  = {raw_max:,.1f} mm/yr\n\n"
                   f"After cap:\n"
                   f"  min  = {capped_min:,.1f} mm/yr\n"
                   f"  mean = {capped_mean:,.3f} mm/yr\n"
                   f"  max  = {capped_max:,.1f} mm/yr\n\n"
                   f"Cells clipped: {n_clipped:,} "
                   f"({100.0 * n_clipped / n_valid:.4f} % of "
                   f"{n_valid:,} valid cells).\n"
                   f"Mean drop: {raw_mean - capped_mean:,.4f} mm/yr.")
        QMessageBox.information(self, "Preview cap effect", msg)

    # =================================================================
    # Actions
    # =================================================================
    def _auto_detect(self):
        if self._is_running():
            return
        wd = self.get_working_dir()
        if not wd:
            QMessageBox.warning(self, "Auto-detect",
                                "Set the working directory first.")
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
        self.lbl_env_status.setText(
            f"Regime from gwdepth (secondary): {env}\n"
            f"{res.get('message', '')}")
        if env == "arid":
            self.radio_arid.setChecked(True)
        elif env == "semi-arid":
            self.radio_semi.setChecked(True)
        elif env == "humid":
            self.radio_humid.setChecked(True)
        else:
            self.radio_mixed.setChecked(True)
        self._on_environment_changed()

    def _browse_stream(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select stream shapefile", "",
            "Vector (*.shp *.gpkg *.geojson);;All files (*.*)")
        if path:
            self.txt_stream.setText(path)

    def _engine(self):
        wd = self.get_working_dir()
        out_dir = self.get_output_dir() or wd
        start, end = self.get_time_range()
        ks_mode = ("from_soil" if self.radio_ks_soil.isChecked()
                   else "constant")
        if self.radio_w_order.isChecked():
            wm = "from_order"
        elif self.radio_w_attr.isChecked():
            wm = "from_attribute"
        else:
            wm = "constant"
        return FocusedRechargeEngine(
            working_dir=wd, output_dir=out_dir,
            start_step=start, end_step=end,
            stream_path=self.txt_stream.text().strip() or None,
            stream_source=("dem" if self.radio_stream_dem.isChecked()
                           else "shapefile"),
            dem_stream_threshold=self.spin_threshold.value(),
            Ks=self.spin_Ks.value(),
            channel_width_m=self.spin_W.value(),
            gwdepth_threshold_m=self.spin_gw_threshold.value(),
            environment=self._current_regime(),
            ks_mode=ks_mode, width_mode=wm,
            delta_factor=self.spin_delta.value(),
            max_daily_depth_mm=self.spin_max_infil.value(),
            flow_duration_fraction=self.spin_flow_frac.value(),
            auto_checkpoint=self.chk_auto_checkpoint.isChecked())

    def _check_stream(self):
        if self._is_running():
            return
        wd = self.get_working_dir()
        if not wd:
            QMessageBox.warning(self, "Check",
                                "Set the working directory first.")
            return
        try:
            pr = self._engine().check_prerequisites()
        except Exception as exc:
            QMessageBox.critical(self, "Check failed", str(exc))
            return
        lines = [
            f"Stream:   {'OK' if pr['stream_ok'] else 'X'}  "
            f"{pr['stream_message']}",
            f"Runoff:   {'OK' if pr['months_ok'] else 'X'}  "
            f"{pr['months_message']}",
            f"GWdepth:  {'OK' if pr['gwdepth_ok'] else 'X'}  "
            f"{pr['gwdepth_message']}",
            f"K_s:      {'OK' if pr['ks_ok'] else 'X'}  "
            f"{pr['ks_message']}",
            f"Width:    {'OK' if pr['width_ok'] else 'X'}  "
            f"{pr['width_message']}",
        ]
        self.txt_log.append("[Check]\n" + "\n".join(lines))
        self.lbl_ks_status.setText(pr["ks_message"])
        QMessageBox.information(
            self, "Check prerequisites", "\n".join(lines))
        self._refresh_resume_banner()

    def _run(self):
        if self._is_running():
            return
        wd = self.get_working_dir()
        out_dir = self.get_output_dir()
        if not wd or not out_dir:
            QMessageBox.warning(self, "Focused recharge",
                                "Set Working Directory and Output "
                                "Directory first.")
            return

        start, end = self.get_time_range()
        env = self._current_regime()
        ks_mode = ("from_soil" if self.radio_ks_soil.isChecked()
                   else "constant")
        if self.radio_w_order.isChecked():
            wm = "from_order"
        elif self.radio_w_attr.isChecked():
            wm = "from_attribute"
        else:
            wm = "constant"

        self.txt_log.clear()
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        self.worker = FocusedRechargeWorker(
            working_dir=wd, output_dir=out_dir,
            start_step=start, end_step=end,
            stream_path=self.txt_stream.text().strip() or None,
            stream_source=("dem" if self.radio_stream_dem.isChecked()
                           else "shapefile"),
            dem_stream_threshold=self.spin_threshold.value(),
            Ks=self.spin_Ks.value(),
            channel_width_m=self.spin_W.value(),
            gwdepth_threshold_m=self.spin_gw_threshold.value(),
            environment=env, ks_mode=ks_mode, width_mode=wm,
            delta_factor=self.spin_delta.value(),
            max_daily_depth_mm=self.spin_max_infil.value(),
            flow_duration_fraction=self.spin_flow_frac.value(),
            auto_checkpoint=self.chk_auto_checkpoint.isChecked(),
            resume_from_checkpoint=True)
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
    # Combine helpers
    # =================================================================
    @staticmethod
    def _dem_valid_mask_from_diffusive(diff_path, H, W):
        """Build a DEM-valid mask at the output grid."""
        import rasterio
        from rasterio.windows import Window
        mask = np.zeros((H, W), dtype=bool)
        tile = 2048
        with rasterio.open(str(diff_path)) as src:
            nd = src.nodata if src.nodata is not None else -9999.0
            for r0 in range(0, H, tile):
                h = min(tile, H - r0)
                for c0 in range(0, W, tile):
                    w = min(tile, W - c0)
                    arr = src.read(1, window=Window(c0, r0, w, h))
                    valid = (arr != nd) & np.isfinite(arr)
                    mask[r0:r0 + h, c0:c0 + w] = valid
        return mask

    @staticmethod
    def _write_combined_tiled(out_path, H, W, profile,
                              diff_src, foc_src, valid_mask,
                              method="addition",
                              cap_mm=None,
                              tile=2048,
                              d_nd=-9999.0, f_nd=-9999.0,
                              out_nd=-9999.0,
                              progress_cb=None):
        """
        Combine diffusive + focused recharge, tile-by-tile.
        """
        import rasterio
        from rasterio.windows import Window
        n_tiles_x = (W + tile - 1) // tile
        n_tiles_y = (H + tile - 1) // tile
        n_tiles = n_tiles_x * n_tiles_y
        done = 0

        apply_cap = (cap_mm is not None and cap_mm > 0)

        with rasterio.open(str(out_path), "w", **profile) as dst:
            for r0 in range(0, H, tile):
                h = min(tile, H - r0)
                for c0 in range(0, W, tile):
                    w = min(tile, W - c0)
                    win = Window(c0, r0, w, h)

                    a = diff_src.read(1, window=win).astype(np.float64)
                    b = foc_src.read(1, window=win).astype(np.float64)

                    a = np.where(a == d_nd, np.nan, a)
                    b = np.where(b == f_nd, np.nan, b)
                    a = np.where(np.isfinite(a), a, np.nan)
                    b = np.where(np.isfinite(b), b, np.nan)

                    both = np.isfinite(a) & np.isfinite(b)
                    only_a = np.isfinite(a) & ~np.isfinite(b)
                    only_b = ~np.isfinite(a) & np.isfinite(b)

                    out = np.full(a.shape, np.nan, dtype=np.float64)

                    if method == "addition":
                        out[both] = a[both] + b[both]
                        out[only_a] = a[only_a]
                        out[only_b] = b[only_b]
                    elif method == "max":
                        out[both] = np.maximum(a[both], b[both])
                        out[only_a] = a[only_a]
                        out[only_b] = b[only_b]
                    elif method == "focused_over_diffusive":
                        out[both] = b[both]
                        out[only_a] = a[only_a]
                        out[only_b] = b[only_b]
                    else:
                        out[both] = a[both] + b[both]
                        out[only_a] = a[only_a]
                        out[only_b] = b[only_b]

                    if apply_cap:
                        finite = np.isfinite(out)
                        out = np.where(finite,
                                       np.minimum(out, cap_mm),
                                       out)

                    vm = valid_mask[r0:r0 + h, c0:c0 + w]
                    out = np.where(vm, out, np.nan)
                    out = np.where(np.isfinite(out), out, out_nd)

                    dst.write(out.astype(np.float32), 1, window=win)
                    del a, b, out

                    done += 1
                    if progress_cb is not None and (
                            done % 8 == 0 or done == n_tiles):
                        progress_cb(done, n_tiles)

    @staticmethod
    def _write_capped_raster_tiled(out_path, H, W, profile,
                                   src, valid_mask, cap_mm,
                                   tile=2048,
                                   nd=-9999.0, out_nd=-9999.0,
                                   progress_cb=None):
        """Write a capped copy of a single input raster."""
        import rasterio
        from rasterio.windows import Window
        n_tiles_x = (W + tile - 1) // tile
        n_tiles_y = (H + tile - 1) // tile
        n_tiles = n_tiles_x * n_tiles_y
        done = 0

        with rasterio.open(str(out_path), "w", **profile) as dst:
            for r0 in range(0, H, tile):
                h = min(tile, H - r0)
                for c0 in range(0, W, tile):
                    w = min(tile, W - c0)
                    win = Window(c0, r0, w, h)

                    a = src.read(1, window=win).astype(np.float64)
                    a = np.where(a == nd, np.nan, a)
                    a = np.where(np.isfinite(a), a, np.nan)
                    a = np.where(np.isfinite(a),
                                 np.minimum(a, cap_mm),
                                 a)

                    vm = valid_mask[r0:r0 + h, c0:c0 + w]
                    a = np.where(vm, a, np.nan)
                    a = np.where(np.isfinite(a), a, out_nd)

                    dst.write(a.astype(np.float32), 1, window=win)
                    del a

                    done += 1
                    if progress_cb is not None and (
                            done % 8 == 0 or done == n_tiles):
                        progress_cb(done, n_tiles)

    # =================================================================
    # COMBINE
    # =================================================================
    def _combine(self):
        import rasterio

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
                f"  {diff.name}: "
                f"{'found' if diff.exists() else 'MISSING'}\n"
                f"  {foc.name}:  "
                f"{'found' if foc.exists() else 'MISSING'}")
            return

        method = self._current_combine_method()

        cap_mm = None
        if self.chk_cap.isChecked():
            if self.chk_override_cap.isChecked():
                cap_mm = float(self.spin_cap.value())
                if cap_mm <= 0:
                    cap_mm = None
            else:
                if self._recommended_cap_mm is None:
                    QMessageBox.warning(
                        self, "Combine",
                        "You enabled the physical cap but no rainfall\n"
                        "has been read yet.\n\n"
                        "Click 'Recompute from inputs' first, or tick\n"
                        "'Override with' and enter a custom value.")
                    return
                cap_mm = float(self._recommended_cap_mm)

        try:
            dsrc = rasterio.open(str(diff))
            fsrc = rasterio.open(str(foc))
        except Exception as exc:
            QMessageBox.critical(self, "Combine", f"Open failed: {exc}")
            return

        try:
            if (dsrc.width, dsrc.height) != (fsrc.width, fsrc.height):
                QMessageBox.critical(
                    self, "Combine",
                    f"Grid mismatch:\n"
                    f"  {diff.name}: {dsrc.height} x {dsrc.width}\n"
                    f"  {foc.name}:  {fsrc.height} x {fsrc.width}")
                return
            if dsrc.transform != fsrc.transform or dsrc.crs != fsrc.crs:
                QMessageBox.critical(
                    self, "Combine",
                    "Rasters are not on the same transform / CRS.")
                return

            H, W = dsrc.height, dsrc.width
            d_nd = (dsrc.nodata if dsrc.nodata is not None
                    else -9999.0)
            f_nd = (fsrc.nodata if fsrc.nodata is not None
                    else -9999.0)
            out_path = out_dir / "Recharge_Total_annual.tif"

            profile = {
                "driver": "GTiff", "dtype": "float32", "nodata": d_nd,
                "width": W, "height": H, "count": 1,
                "crs": dsrc.crs, "transform": dsrc.transform,
                "compress": "deflate", "tiled": True,
                "blockxsize": 256, "blockysize": 256,
            }

            method_label = {
                "addition": "addition",
                "max": "replace-by-max",
                "focused_over_diffusive": "focused-over-diffusive",
            }.get(method, method)

            cap_label = ""
            if cap_mm is not None:
                src_label = ("override"
                             if self.chk_override_cap.isChecked()
                             else "recommended")
                cap_label = f", cap={cap_mm:.1f} mm/yr ({src_label})"
                if self._mean_annual_rainfall_mm is not None:
                    cap_label += (
                        f", P={self._mean_annual_rainfall_mm:.1f}"
                        f" mm/yr")
            else:
                cap_label = ", no cap"

            self.txt_log.append(
                f"[Combine] grid {H} x {W}, tile 2048 px, "
                f"method={method_label}{cap_label}, "
                f"nodata outside DEM mask.")
            self.progress.setValue(0)
            self.progress.setFormat("Building DEM mask… %p%")

            valid_mask = self._dem_valid_mask_from_diffusive(
                diff, H, W)
            n_valid = int(valid_mask.sum())
            self.txt_log.append(
                f"[Combine] DEM-valid cells: {n_valid} "
                f"({100.0*n_valid/valid_mask.size:.2f} % of grid)")

            def prog(done, total):
                pct = int(100 * done / max(total, 1))
                self.progress.setValue(pct)
                self.progress.setFormat(
                    f"Combining… %p%  ({done}/{total})")

            self._write_combined_tiled(
                out_path, H, W, profile,
                dsrc, fsrc, valid_mask,
                method=method,
                cap_mm=cap_mm,
                tile=2048, d_nd=d_nd, f_nd=f_nd, out_nd=d_nd,
                progress_cb=prog)

            # ---- Also write a capped focused raster ---------------
            if (cap_mm is not None
                    and self.chk_cap_focused.isChecked()):
                foc_capped_path = (
                    out_dir / "Recharge_focused_annual_capped.tif")
                self.txt_log.append(
                    f"[Combine] also writing "
                    f"{foc_capped_path.name} with cap={cap_mm:.1f} "
                    f"mm/yr")
                self._write_capped_raster_tiled(
                    foc_capped_path, H, W, profile,
                    fsrc, valid_mask, cap_mm,
                    tile=2048, nd=f_nd, out_nd=d_nd,
                    progress_cb=prog)

        except Exception as exc:
            self.progress.setFormat("Combine failed")
            QMessageBox.critical(self, "Combine failed", str(exc))
            self.txt_log.append(f"[Combine error] {exc}")
            with contextlib.suppress(Exception):
                dsrc.close()
            with contextlib.suppress(Exception):
                fsrc.close()
            return

        with contextlib.suppress(Exception):
            dsrc.close()
        with contextlib.suppress(Exception):
            fsrc.close()

        layer = QgsRasterLayer(
            str(out_path), "Recharge Total (mm/year)")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)

        self.progress.setValue(100)
        self.progress.setFormat("Combine done (%p%)")
        self.txt_log.append(
            f"Combined ({method_label}{cap_label}, DEM-masked):\n"
            f"  {diff.name}\n"
            f"  {foc.name}\n"
            "-> Recharge_Total_annual.tif")

        cap_msg = ("No cap." if cap_mm is None
                   else f"Cap: {cap_mm:.1f} mm/year.")
        extra = ""
        if cap_mm is not None and self.chk_cap_focused.isChecked():
            extra = ("\n\nAlso wrote:\n"
                     "  Recharge_focused_annual_capped.tif")
        QMessageBox.information(
            self, "Combine",
            f"Wrote:\n{out_path}\n\n"
            f"Method: {method_label}.\n"
            f"{cap_msg}{extra}\n"
            "Nodata outside the DEM mask.")

    # =================================================================
    # Worker callbacks
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
            layer = QgsRasterLayer(
                str(annual), "Recharge Focused (mm/year)")
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
        self.txt_log.append(
            f"  Regime                    : "
            f"{self._current_regime().upper()}")
        self.txt_log.append(
            f"  Catchment area            : {area_km2:.3f} km2")
        self.txt_log.append(
            f"  Stream cells (total)      : {n_stream}")
        self.txt_log.append(
            f"  Treated as losing         : {losing_frac*100:.1f} %")
        self.txt_log.append(
            f"  Total runoff generated    : {total_av:.3e} m3")
        self.txt_log.append(
            f"  Total focused recharge    : {total_foc:.3e} m3")
        self.txt_log.append(
            f"  Routed to outlets         : {total_out:.3e} m3")
        self.txt_log.append(
            f"  Loss fraction             : {frac*100:.2f} %")
        self.txt_log.append(
            f"  CATCHMENT-AVERAGE focused : {avg_mm:.3f} mm/year")
        self.txt_log.append(
            f"  Streambed-average infilt. : "
            f"{streambed_mm:.3f} mm/year")
        self.txt_log.append("=" * 55)
        self.txt_log.append("")

        QMessageBox.information(
            self, "Focused Recharge",
            f"Focused recharge complete.\n\n"
            f"Losing stream cells: {losing_frac*100:.1f} %\n"
            f"Catchment-average focused recharge: "
            f"{avg_mm:.3f} mm/year\n"
            f"Streambed-average infiltration: "
            f"{streambed_mm:.3f} mm/year\n"
            f"Loss fraction: {frac*100:.2f} %\n\n"
            f"Output: {out_dir}")

        self._refresh_resume_banner()

    def _on_finished_error(self, err):
        self.worker = None
        self.progress.setFormat("Failed")
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.txt_log.append("\n[ERROR]\n" + err)
        QMessageBox.critical(self, "Focused Recharge",
                             f"Focused recharge failed:\n\n{err[:600]}")
        self._refresh_resume_banner()