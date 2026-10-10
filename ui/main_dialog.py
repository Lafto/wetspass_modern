# -*- coding: utf-8 -*-
"""WetSpass-M Modern — Main Tabbed Dialog."""

import contextlib
import re
from pathlib import Path

from qgis.PyQt.QtCore import Qt, pyqtSignal, QTimer, QUrl, QEvent, QSettings
from qgis.PyQt.QtGui import QFont, QIcon, QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QRadioButton, QGroupBox,
    QSpinBox, QDoubleSpinBox, QComboBox, QProgressBar, QTextEdit,
    QFileDialog, QMessageBox, QScrollArea, QFrame, QSizePolicy,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QTextBrowser, QApplication,
    QListWidget, QListWidgetItem, QDialogButtonBox, QAbstractItemView,
)
from qgis.core import QgsRasterLayer, QgsProject, Qgis

from .worker import WetSpassWorker
from .boundary_preview import BoundaryPreview
from ..core.calculation_engine import WetSpassParameters
from ..core.workflow import WetSpassWorkflow, inspect_inputs

try:
    from ..core.cropper import (
        crop_rasters_to_boundary,
        list_available_rasters,
    )
except ImportError:
    crop_rasters_to_boundary = None
    list_available_rasters = None


MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

SEASON_PRESETS = [
    ("Custom", None),
    ("Ethiopian — Kiremt (wet, Jun–Sep) / Bega + Belg (dry, Oct–May)",
     ((6, 9), (10, 5))),
    ("West African — Wet (May–Oct) / Dry (Nov–Apr)", ((5, 10), (11, 4))),
    ("Mediterranean — Wet (Nov–Mar) / Dry (Apr–Oct)", ((11, 3), (4, 10))),
    ("South Asian Monsoon — Wet (Jun–Sep) / Dry (Oct–May)",
     ((6, 9), (10, 5))),
    ("Temperate N. Hemisphere — Summer (Jun–Aug) / Winter (Dec–Feb)",
     ((6, 8), (12, 2))),
    ("Southern Hemisphere — Wet (Nov–Mar) / Dry (Apr–Oct)",
     ((11, 3), (4, 10))),
]

OUTPUT_OPTIONS = [
    ("Interception",                "Interception of the Cell",             True),
    ("Csr",                         "Runoff Coefficient (Csr)",             False),
    ("Cell_runoff",                 "Runoff of the Cell",                   True),
    ("Cell_evapotranspiration",
     "AET of the Cell (includes I; and groundwater-fed and open-water "
     "evaporation)",                                                     True),
    ("Cell_gw_discharge",
     "Groundwater + open-water part of AET (diagnostic; not needed for "
     "the standard Wetspass-M 2016 water balance)",                      False),
    ("recharge",                    "Recharge_diffusive — diffuse recharge",
     True),
    ("vegrunoff",                   "Runoff in Vegetated Area",             False),
    ("barerunoff",                  "Runoff of Bare Soil",                  False),
    ("imperrunoff",                 "Runoff of Impervious Area",            False),
    ("owrunoff",                    "Runoff on Open Water",                 False),
    ("Cell_actualtranspiration",    "Total Actual Transpiration of Pixel",  False),
    ("Cell_actual_baresoil_evapo",  "Evaporation from Bare Soil",           False),
    ("Cell_ow_evaporation",         "Evaporation from Open Water",          False),
    ("Cell_imper_evaporation",      "Evaporation from Impervious Surface",  False),
    ("Cell_gw_transpiration",       "Groundwater Transpiration",            False),
    ("Cell_gw_evaporation",         "Groundwater Evaporation",              False),
    ("penmann_coefficient",         "Penmann Coefficient",                  False),
    ("total_Interception",          "Total Interception",                   False),
]

TAB_INPUTS, TAB_CHECK, TAB_SETTINGS, TAB_PARAMS = 0, 1, 2, 3
TAB_OUTPUT_PERIOD, TAB_RUN, TAB_PROGRESS, TAB_RESULTS = 4, 5, 6, 7
TAB_FOCUSED, TAB_WATER_BALANCE, TAB_CALIBRATION = 8, 9, 10
TAB_ABOUT, TAB_HELP = 11, 12

USER_GUIDE_FILENAME = "WetSpassM_UserGuide.pdf"


_PERIOD_SUFFIX_RE = re.compile(
    r"^(?P<var>.+?)_(?P<period>\d+|annual|wet_season|dry_season)$"
)


def _variable_of(filename: str) -> str:
    stem = Path(filename).stem
    m = _PERIOD_SUFFIX_RE.match(stem)
    return m.group("var") if m else stem


def _period_of(filename: str) -> str:
    stem = Path(filename).stem
    m = _PERIOD_SUFFIX_RE.match(stem)
    if not m:
        return ""
    p = m.group("period")
    if p.isdigit():
        return f"Month {p}"
    return {"annual": "Annual",
            "wet_season": "Wet season",
            "dry_season": "Dry season"}.get(p, p)


class CropSelectionDialog(QDialog):
    """
    Dialog that lists every raster in the output folder, grouped by
    variable, and lets the user pick which ones to crop.
    """

    def __init__(self, input_dir, parent=None):
        super().__init__(parent)
        self.input_dir = Path(input_dir)
        self.setWindowTitle("Select rasters to crop")
        self.resize(560, 620)
        self._build_ui()
        self._populate()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        info = QLabel(
            f"Source folder:\n<b>{self.input_dir}</b><br><br>"
            "Tick the rasters you want cropped. The originals are never "
            "modified — cropped copies are written to "
            "<code>cropped_to_boundary/</code> inside the source folder."
        )
        info.setWordWrap(True)
        outer.addWidget(info)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Filter:"))
        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText(
            "Type to filter, e.g. 'Recharge' or 'annual'…")
        self.txt_filter.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.txt_filter, 1)
        outer.addLayout(search_row)

        btn_row = QHBoxLayout()
        self.btn_all = QPushButton("Select all")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        btn_row.addWidget(self.btn_all)

        self.btn_none = QPushButton("Select none")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(self.btn_none)

        self.btn_invert = QPushButton("Invert")
        self.btn_invert.clicked.connect(self._invert)
        btn_row.addWidget(self.btn_invert)

        btn_row.addSpacing(12)

        self.btn_only_annual = QPushButton("Only annual")
        self.btn_only_annual.setToolTip("Select only the _annual.tif files")
        self.btn_only_annual.clicked.connect(self._select_only_annual)
        btn_row.addWidget(self.btn_only_annual)

        self.btn_only_recharge = QPushButton("Only Recharge")
        self.btn_only_recharge.setToolTip(
            "Select only files whose name starts with 'Recharge_'")
        self.btn_only_recharge.clicked.connect(self._select_only_recharge)
        btn_row.addWidget(self.btn_only_recharge)

        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Raster", "Period"])
        self.tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        outer.addWidget(self.tree, 1)

        self.lbl_status = QLabel("0 files selected")
        self.lbl_status.setStyleSheet("color:#444;")
        outer.addWidget(self.lbl_status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _populate(self):
        self.tree.clear()
        self._var_items = {}
        if list_available_rasters is None:
            return
        files = list_available_rasters(self.input_dir)
        if not files:
            item = QTreeWidgetItem(["(no rasters found)", ""])
            self.tree.addTopLevelItem(item)
            return
        grouped = {}
        for fname in files:
            var = _variable_of(fname)
            grouped.setdefault(var, []).append(fname)

        for var in sorted(grouped.keys()):
            var_item = QTreeWidgetItem([var, ""])
            var_item.setFlags(
                var_item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate)
            var_item.setCheckState(0, Qt.CheckState.Checked)
            var_item.setExpanded(True)
            self.tree.addTopLevelItem(var_item)
            self._var_items[var] = var_item

            for fname in sorted(grouped[var]):
                child = QTreeWidgetItem([fname, _period_of(fname)])
                child.setFlags(
                    child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked)
                child.setData(0, Qt.ItemDataRole.UserRole, fname)
                var_item.addChild(child)

        self.tree.itemChanged.connect(self._on_item_changed)
        self._update_status()

    def _on_item_changed(self, item, column):
        self._update_status()

    def _iter_leaf_items(self):
        for i in range(self.tree.topLevelItemCount()):
            var_item = self.tree.topLevelItem(i)
            for j in range(var_item.childCount()):
                yield var_item, var_item.child(j)

    def _set_all(self, state):
        self.tree.blockSignals(True)
        check = Qt.CheckState.Checked if state else Qt.CheckState.Unchecked
        for i in range(self.tree.topLevelItemCount()):
            var_item = self.tree.topLevelItem(i)
            var_item.setCheckState(0, check)
            for j in range(var_item.childCount()):
                var_item.child(j).setCheckState(0, check)
        self.tree.blockSignals(False)
        self._update_status()

    def _invert(self):
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            var_item = self.tree.topLevelItem(i)
            for j in range(var_item.childCount()):
                child = var_item.child(j)
                new = (Qt.CheckState.Unchecked
                       if child.checkState(0) == Qt.CheckState.Checked
                       else Qt.CheckState.Checked)
                child.setCheckState(0, new)
        self.tree.blockSignals(False)
        self._update_status()

    def _select_only_annual(self):
        self.tree.blockSignals(True)
        for var_item, child in self._iter_leaf_items():
            fname = child.data(0, Qt.ItemDataRole.UserRole) or ""
            if fname.endswith("_annual.tif"):
                child.setCheckState(0, Qt.CheckState.Checked)
            else:
                child.setCheckState(0, Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._update_status()

    def _select_only_recharge(self):
        self.tree.blockSignals(True)
        for var_item, child in self._iter_leaf_items():
            fname = child.data(0, Qt.ItemDataRole.UserRole) or ""
            if fname.startswith("Recharge_"):
                child.setCheckState(0, Qt.CheckState.Checked)
            else:
                child.setCheckState(0, Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._update_status()

    def _apply_filter(self, text):
        text = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            var_item = self.tree.topLevelItem(i)
            any_visible = False
            for j in range(var_item.childCount()):
                child = var_item.child(j)
                fname = (child.data(0, Qt.ItemDataRole.UserRole)
                         or child.text(0) or "")
                visible = (not text) or (text in fname.lower())
                child.setHidden(not visible)
                if visible:
                    any_visible = True
            var_item.setHidden(not any_visible)

    def _update_status(self):
        n_checked = 0
        n_total = 0
        for _, child in self._iter_leaf_items():
            n_total += 1
            if child.checkState(0) == Qt.CheckState.Checked:
                n_checked += 1
        self.lbl_status.setText(f"{n_checked} of {n_total} files selected")

    def selected_files(self):
        out = []
        for _, child in self._iter_leaf_items():
            if child.checkState(0) == Qt.CheckState.Checked:
                fname = child.data(0, Qt.ItemDataRole.UserRole)
                if fname:
                    out.append(fname)
        return out


class PathPicker(QWidget):
    pathChanged = pyqtSignal(str)

    def __init__(self, mode="file", file_filter="", parent=None):
        super().__init__(parent)
        self.mode = mode
        self.file_filter = file_filter
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.edit = QLineEdit()
        layout.addWidget(self.edit, 1)
        self.btn = QPushButton("...")
        self.btn.setFixedWidth(32)
        self.btn.clicked.connect(self._browse)
        layout.addWidget(self.btn)
        self.edit.textChanged.connect(self.pathChanged.emit)
        self.edit.returnPressed.connect(
            lambda: self.pathChanged.emit(self.edit.text()))

    def _browse(self):
        if self.mode == "dir":
            path = QFileDialog.getExistingDirectory(
                self, "Select folder", self.edit.text() or "")
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select file", self.edit.text() or "",
                self.file_filter or "All files (*.*)")
        if path:
            self.edit.setText(path)
            self.pathChanged.emit(path)

    def text(self):
        return self.edit.text()

    def setText(self, t):
        self.edit.setText(t)

    def setEnabled(self, en):
        super().setEnabled(en)
        self.edit.setEnabled(en)
        self.btn.setEnabled(en)


class LabeledSpin(QWidget):
    def __init__(self, label, value, minimum=0.0, maximum=1e9,
                 decimals=4, is_int=False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(label)
        self.label.setMinimumWidth(190)
        layout.addWidget(self.label)
        if is_int:
            self.spin = QSpinBox()
            self.spin.setRange(int(minimum), int(maximum))
        else:
            self.spin = QDoubleSpinBox()
            self.spin.setRange(minimum, maximum)
            self.spin.setDecimals(decimals)
            self.spin.setSingleStep(0.01)
        self.spin.setValue(value)
        layout.addWidget(self.spin, 1)

    def value(self):
        return self.spin.value()

    def setValue(self, v):
        self.spin.setValue(v)

    def setEnabled(self, en):
        super().setEnabled(en)
        self.label.setEnabled(en)
        self.spin.setEnabled(en)


def scrollable(page):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(page)
    return scroll


class WetSpassMainDialog(QDialog):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.worker = None
        self._paused_mode = False
        self._pause_requested_by_user = False
        self._allow_close = False
        self._focused_hint_shown = False

        self.setWindowTitle("WetSpass-M Modern")
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)

        screen = self.screen() or (
            self.parent().screen() if self.parent() else None)
        if screen is not None:
            geo = screen.availableGeometry()
            self.resize(max(900, int(geo.width() * 0.80)),
                        max(650, int(geo.height() * 0.85)))
        else:
            self.resize(1000, 720)

        icon_path = Path(__file__).parent.parent / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(700)
        self._poll_timer.timeout.connect(self._poll_worker_state)

        self._build_ui()
        self._load_defaults()

        self._main_window = None
        with contextlib.suppress(Exception):
            self._main_window = self.iface.mainWindow()
            if self._main_window is not None:
                self._main_window.installEventFilter(self)

    def _worker_running(self) -> bool:
        return (self.worker is not None and self.worker.isRunning())

    def _confirm_cancel(self, on_qgis_close: bool) -> bool:
        if on_qgis_close:
            msg = ("A WetSpass-M model run is in progress.\n\n"
                   "If you close QGIS now, the run will be cancelled "
                   "and any partial output will be lost.\n\n"
                   "Do you want to cancel the run and close QGIS?")
        else:
            msg = ("A WetSpass-M model run is in progress.\n\n"
                   "Do you want to cancel the run and close the dialog?\n\n"
                   "If you choose No, the dialog will be hidden and the "
                   "run will continue in the background.")
        reply = QMessageBox.question(
            self, "WetSpass-M is running", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return False
        with contextlib.suppress(Exception):
            self.worker.cancel()
            self.worker.wait(5000)
        return True

    def eventFilter(self, obj, event):
        if (obj is self._main_window
                and event.type() == QEvent.Type.Close
                and self._worker_running()):
            if not self._confirm_cancel(on_qgis_close=True):
                event.ignore()
                return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        if self._worker_running() and not self._allow_close:
            if not self._confirm_cancel(on_qgis_close=False):
                event.ignore()
                self.hide()
                return
        event.accept()

    def cleanup(self):
        with contextlib.suppress(Exception):
            if self._main_window is not None:
                self._main_window.removeEventFilter(self)
        self._main_window = None
        if self.worker is not None and self.worker.isRunning():
            with contextlib.suppress(Exception):
                self.worker.cancel()
                self.worker.wait(3000)

    # =================================================================
    # Build UI
    # =================================================================
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self.tabs = QTabWidget()
        self.tabs.addTab(scrollable(self._build_inputs_tab()),         "Inputs")
        self.tabs.addTab(self._build_check_tab(),                      "Check Inputs")
        self.tabs.addTab(scrollable(self._build_settings_tab()),       "Settings")
        self.tabs.addTab(scrollable(self._build_params_tab()),         "Parameters")
        self.tabs.addTab(scrollable(self._build_output_period_tab()),  "Output Period")
        self.tabs.addTab(scrollable(self._build_run_tab()),            "Run")
        self.tabs.addTab(self._build_progress_tab(),                   "Progress")
        self.tabs.addTab(scrollable(self._build_results_tab()),        "Results")
        self.tabs.addTab(scrollable(self._build_focused_tab()),        "Focused Recharge")
        self.tabs.addTab(scrollable(self._build_water_balance_tab()),  "Water Balance")
        self.tabs.addTab(self._build_calibration_tab(),                "Calibration")
        self.tabs.addTab(scrollable(self._build_about_tab()),          "About")
        self.tabs.addTab(scrollable(self._build_help_tab()),           "Help")
        root.addWidget(self.tabs, 1)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        root.addLayout(self._build_tools_row())

        status_frame = QFrame()
        status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(6, 2, 6, 2)
        self.status_label = QLabel("Ready.")
        self.status_label.setMinimumWidth(220)
        status_layout.addWidget(self.status_label, 1)
        self.status_progress = QProgressBar()
        self.status_progress.setRange(0, 100)
        self.status_progress.setValue(0)
        self.status_progress.setMaximumWidth(360)
        status_layout.addWidget(self.status_progress)
        root.addWidget(status_frame)

    # =================================================================
    # Tools row — crop button replaces the old mask button
    # =================================================================
    def _build_tools_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        def _wd():
            return self.pick_working_dir.text().strip()

        def _od():
            return self.pick_output_dir.text().strip()

        def _add_tool(label, tooltip, factory):
            try:
                act = factory()
                btn = QPushButton(label)
                btn.setToolTip(tooltip)
                btn.clicked.connect(act.trigger)
                row.addWidget(btn)
            except Exception as exc:  # noqa: BLE001
                import traceback
                print(f"[wetspass_modern] tool '{label}' unavailable: "
                      f"{exc}")
                print(traceback.format_exc())
                btn = QPushButton(label)
                btn.setEnabled(False)
                btn.setToolTip(f"Unavailable: {exc}")
                row.addWidget(btn)

        def _import(name):
            try:
                mod = __import__(
                    "wetspass_modern.ui.tools_menu", fromlist=[name])
                return getattr(mod, name, None)
            except Exception as exc:  # noqa: BLE001
                import traceback
                print(f"[wetspass_modern] cannot import {name}: {exc}")
                print(traceback.format_exc())
                return None

        # --- Crop button: opens the file-selection dialog ---
        self.btn_crop = QPushButton("Crop outputs to boundary")
        self.btn_crop.setToolTip(
            "Crop selected output rasters to the project boundary polygon.\n"
            "You will be asked which rasters to crop.\n"
            "Originals are preserved — cropped copies go to a subfolder."
        )
        self.btn_crop.clicked.connect(self._on_crop_outputs_clicked)
        row.addWidget(self.btn_crop)

        make_gwdepth   = _import("make_gwdepth_analyse_action")
        make_closure   = _import("make_percell_closure_action")
        make_rep_diff  = _import("make_open_diffusive_report_action")
        make_rep_foc   = _import("make_open_focused_report_action")

        if make_gwdepth is not None:
            _add_tool(
                "Analyse gwdepth",
                "Per-month statistics of the gwdepth rasters.",
                lambda: make_gwdepth(self, _wd, _od))
        else:
            btn = QPushButton("Analyse gwdepth")
            btn.setEnabled(False)
            btn.setToolTip("tools_menu.make_gwdepth_analyse_action "
                           "is not available")
            row.addWidget(btn)

        if make_closure is not None:
            _add_tool(
                "Per-cell closure",
                "Write wb_error_annual.tif = P − AET − SR\n"
                "− R_total − ΔS, cell by cell.",
                lambda: make_closure(self, _wd, _od))
        else:
            btn = QPushButton("Per-cell closure")
            btn.setEnabled(False)
            btn.setToolTip("tools_menu.make_percell_closure_action "
                           "is not available")
            row.addWidget(btn)

        if make_rep_diff is not None:
            _add_tool(
                "Diffusive report",
                "Open RunSummary.txt from the output folder.",
                lambda: make_rep_diff(self, _od))
        else:
            btn = QPushButton("Diffusive report")
            btn.setEnabled(False)
            btn.setToolTip("tools_menu.make_open_diffusive_report_action "
                           "is not available")
            row.addWidget(btn)

        if make_rep_foc is not None:
            _add_tool(
                "Focused report",
                "Open RunSummary_focused.txt from the output folder.",
                lambda: make_rep_foc(self, _od))
        else:
            btn = QPushButton("Focused report")
            btn.setEnabled(False)
            btn.setToolTip("tools_menu.make_open_focused_report_action "
                           "is not available")
            row.addWidget(btn)

        row.addStretch(1)
        return row

    # =================================================================
    # Crop handler
    # =================================================================
    def _on_crop_outputs_clicked(self):
        """Show the selection dialog, then crop the chosen rasters."""
        if crop_rasters_to_boundary is None:
            QMessageBox.critical(
                self, "Crop outputs",
                "cropper.py module not found.\n\n"
                "Please ensure cropper.py is placed in the core folder."
            )
            return

        output_dir = self.pick_output_dir.text().strip()
        boundary_path = self.pick_boundary.text().strip()

        if not output_dir or not boundary_path:
            QMessageBox.warning(
                self, "Missing paths",
                "Set both the Output Directory (Run tab) and the\n"
                "Project Boundary shapefile (Inputs tab) first."
            )
            return

        if not Path(output_dir).is_dir():
            QMessageBox.warning(
                self, "Invalid path",
                f"Output directory does not exist:\n{output_dir}"
            )
            return

        if not Path(boundary_path).exists():
            QMessageBox.warning(
                self, "Invalid path",
                f"Boundary shapefile does not exist:\n{boundary_path}"
            )
            return

        # 1. Show the file-selection dialog
        dlg = CropSelectionDialog(output_dir, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected = dlg.selected_files()
        if not selected:
            QMessageBox.information(
                self, "Crop outputs",
                "No rasters were selected — nothing to crop."
            )
            return

        # 2. Switch to Progress so the log is visible
        with contextlib.suppress(Exception):
            self.tabs.setCurrentIndex(TAB_PROGRESS)

        self.txt_log.append("")
        self.txt_log.append("=" * 60)
        self.txt_log.append("CROP OUTPUTS TO BOUNDARY — starting")
        self.txt_log.append(f"  Output folder : {output_dir}")
        self.txt_log.append(f"  Boundary      : {boundary_path}")
        self.txt_log.append(f"  Files selected: {len(selected)}")
        self.txt_log.append(f"  Originals preserved. Cropped files go to:")
        self.txt_log.append(f"  {output_dir}\\cropped_to_boundary")
        self.txt_log.append("=" * 60)

        self.status_label.setText(
            f"Cropping {len(selected)} rasters to boundary…")
        self.lbl_status.setText(
            f"Cropping {len(selected)} rasters to boundary…")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Cropping — starting…")
        self.status_progress.setValue(0)

        self.btn_crop.setEnabled(False)
        self.btn_crop.setText("Cropping…")
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        QApplication.processEvents()

        def _progress(step, total, msg):
            with contextlib.suppress(Exception):
                if total > 0:
                    if self.progress_bar.maximum() != total:
                        self.progress_bar.setRange(0, total)
                    self.progress_bar.setValue(step)
                    self.progress_bar.setFormat(
                        f"Cropping — {step} of {total} files")
                    self.status_progress.setValue(
                        int(100 * step / max(total, 1)))
                self.status_label.setText(msg)
                QApplication.processEvents()

        def _log(msg):
            with contextlib.suppress(Exception):
                self.txt_log.append(msg)
                sb = self.txt_log.verticalScrollBar()
                sb.setValue(sb.maximum())
                QApplication.processEvents()

        success = False
        message = "Cropping was not attempted."
        try:
            success, message = crop_rasters_to_boundary(
                input_dir=output_dir,
                shapefile_path=boundary_path,
                output_dir=None,
                file_list=selected,
                progress_callback=_progress,
                log_callback=_log,
            )
        except Exception as exc:
            import traceback
            traceback.print_exc()
            message = f"Unexpected error: {exc}"
            _log(traceback.format_exc())
        finally:
            QApplication.restoreOverrideCursor()
            self.btn_crop.setEnabled(True)
            self.btn_crop.setText("Crop outputs to boundary")

        self.progress_bar.setValue(self.progress_bar.maximum())
        self.progress_bar.setFormat("Cropping — done")
        self.status_progress.setValue(100)
        self.status_label.setText("Cropping complete.")
        self.lbl_status.setText("Cropping complete.")
        self.txt_log.append("")
        self.txt_log.append(f"{'✓' if success else '✗'} {message}")
        self.txt_log.append("=" * 60)

        if success:
            self.iface.messageBar().pushMessage(
                "Crop outputs", message,
                level=Qgis.Success, duration=10)
            QMessageBox.information(
                self, "Crop outputs",
                f"Cropping finished.\n\n{message}")
        else:
            self.iface.messageBar().pushMessage(
                "Crop outputs", message,
                level=Qgis.Warning, duration=10)
            QMessageBox.warning(
                self, "Crop outputs",
                f"Cropping did not complete cleanly.\n\n{message}")

    # =================================================================
    # Inputs
    # =================================================================
    def _build_inputs_tab(self):
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(10)
        row = 0

        def add_row(label, widget):
            nonlocal row
            lbl = QLabel(label)
            lbl.setFont(QFont("", 9, QFont.Weight.Bold))
            layout.addWidget(lbl, row, 0)
            layout.addWidget(widget, row, 1, 1, 2)
            row += 1

        wd_widget = QWidget()
        wd_row = QHBoxLayout(wd_widget)
        wd_row.setContentsMargins(0, 0, 0, 0)
        wd_row.setSpacing(4)
        self.pick_working_dir = PathPicker("dir")
        wd_row.addWidget(self.pick_working_dir, 1)
        self.btn_autoload = QPushButton("Auto-load")
        self.btn_autoload.setFixedWidth(90)
        self.btn_autoload.clicked.connect(self._autoload_from_working_dir)
        wd_row.addWidget(self.btn_autoload)
        add_row("Working Directory:", wd_widget)
        self.pick_working_dir.pathChanged.connect(
            self._on_working_dir_changed)

        self.pick_dem = PathPicker(
            "file", "GeoTIFF (*.tif *.tiff);;ASCII Grid (*.asc)")
        add_row("DEM map:", self.pick_dem)
        self.pick_landuse = PathPicker(
            "file", "GeoTIFF (*.tif *.tiff);;ASCII Grid (*.asc)")
        add_row("Land use map:", self.pick_landuse)
        self.pick_soil = PathPicker(
            "file", "GeoTIFF (*.tif *.tiff);;ASCII Grid (*.asc)")
        add_row("Soil map:", self.pick_soil)
        self.pick_slope = PathPicker(
            "file", "GeoTIFF (*.tif *.tiff);;ASCII Grid (*.asc)")
        add_row("Slope map:", self.pick_slope)

        self.pick_boundary = PathPicker(
            "file",
            "Vector (*.shp *.gpkg *.geojson);;All files (*.*)")
        add_row("Project boundary shapefile:", self.pick_boundary)
        self.pick_boundary.pathChanged.connect(self._on_boundary_changed)

        self.chk_slope_from_dem = QCheckBox(
            "Create slope map from DEM directly")
        self.chk_slope_from_dem.toggled.connect(self._on_slope_toggle)
        layout.addWidget(self.chk_slope_from_dem, row, 1, 1, 2); row += 1
        self._on_slope_toggle(self.chk_slope_from_dem.isChecked())

        self.spin_no_steps = LabeledSpin(
            "No of time steps:", 12, 1, 120, is_int=True)
        layout.addWidget(self.spin_no_steps, row, 0, 1, 3); row += 1
        self.spin_start_step = LabeledSpin(
            "Starting time step:", 1, 1, 120, is_int=True)
        layout.addWidget(self.spin_start_step, row, 0, 1, 3); row += 1
        self.spin_end_step = LabeledSpin(
            "Final time step:", 12, 1, 120, is_int=True)
        layout.addWidget(self.spin_end_step, row, 0, 1, 3); row += 1

        qbox = QGroupBox("Initial discharge (surface water interaction)")
        qgrid = QGridLayout(qbox)
        self.spin_q0_surf = LabeledSpin(
            "Q0 Surface [m³/month]:", 0.0, -1e9, 1e9, decimals=2)
        qgrid.addWidget(self.spin_q0_surf, 0, 0)
        self.spin_q0_base = LabeledSpin(
            "Q0 Sub-surf. [m³/month]:", 0.0, -1e9, 1e9, decimals=2)
        qgrid.addWidget(self.spin_q0_base, 1, 0)
        layout.addWidget(qbox, row, 0, 1, 3); row += 1

        layout.setRowStretch(row, 1)
        nav = QHBoxLayout()
        btn_check = QPushButton("Check Inputs")
        btn_check.clicked.connect(self._run_check)
        nav.addWidget(btn_check)
        nav.addStretch(1)
        btn_next = QPushButton("Next >>")
        btn_next.clicked.connect(
            lambda: self.tabs.setCurrentIndex(TAB_CHECK))
        nav.addWidget(btn_next)
        layout.addLayout(nav, row, 0, 1, 3)
        return page

    # =================================================================
    # Check Inputs
    # =================================================================
    def _build_check_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)
        outer.addWidget(QLabel(
            "Press <b>Check Inputs</b> to verify inputs."))

        body = QHBoxLayout()
        body.setSpacing(10)

        self.tree_status = QTreeWidget()
        self.tree_status.setColumnCount(3)
        self.tree_status.setHeaderLabels(["Item", "Status", "Details"])
        self.tree_status.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree_status.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree_status.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self.tree_status.setAlternatingRowColors(True)
        body.addWidget(self.tree_status, 3)

        preview_frame = QFrame()
        preview_frame.setFrameShape(QFrame.Shape.StyledPanel)
        preview_frame.setMinimumWidth(260)
        preview_frame.setMaximumWidth(420)
        pv_layout = QVBoxLayout(preview_frame)
        pv_layout.setContentsMargins(8, 8, 8, 8)
        pv_layout.setSpacing(6)

        pv_title = QLabel("<b>Project boundary preview</b>")
        pv_layout.addWidget(pv_title)

        self.boundary_preview = BoundaryPreview()
        self.boundary_preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        pv_layout.addWidget(self.boundary_preview, 1)

        btn_reload_preview = QPushButton("Refresh preview")
        btn_reload_preview.clicked.connect(self._refresh_boundary_preview)
        pv_layout.addWidget(btn_reload_preview)

        body.addWidget(preview_frame, 1)
        outer.addLayout(body, 1)

        nav = QHBoxLayout()
        btn_prev = QPushButton("<< Inputs")
        btn_prev.clicked.connect(
            lambda: self.tabs.setCurrentIndex(TAB_INPUTS))
        nav.addWidget(btn_prev)
        btn_check = QPushButton("Check Inputs")
        btn_check.clicked.connect(self._run_check)
        nav.addWidget(btn_check)
        nav.addStretch(1)
        btn_next = QPushButton("Next >>")
        btn_next.clicked.connect(
            lambda: self.tabs.setCurrentIndex(TAB_SETTINGS))
        nav.addWidget(btn_next)
        outer.addLayout(nav)
        return page

    # =================================================================
    # Settings
    # =================================================================
    def _build_settings_tab(self):
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(14, 14, 14, 14)
        grid.setVerticalSpacing(8)
        row = 0

        def add_row(label, widget):
            nonlocal row
            lbl = QLabel(label)
            lbl.setFont(QFont("", 9, QFont.Weight.Bold))
            grid.addWidget(lbl, row, 0)
            grid.addWidget(widget, row, 1)
            row += 1

        self.txt_prefix_rain = QLineEdit("rain"); add_row("Prefix rainfall files:", self.txt_prefix_rain)
        self.txt_prefix_snow = QLineEdit("snowcover"); add_row("Prefix SnowCover file:", self.txt_prefix_snow)
        self.txt_prefix_pet = QLineEdit("pet"); add_row("Prefix PET file:", self.txt_prefix_pet)
        self.txt_prefix_temp = QLineEdit("temp"); add_row("Prefix Temperature:", self.txt_prefix_temp)
        self.txt_prefix_wind = QLineEdit("wind"); add_row("Prefix Wind file:", self.txt_prefix_wind)
        self.txt_prefix_gwdepth = QLineEdit("gwdepth"); add_row("Prefix GWDepth file:", self.txt_prefix_gwdepth)
        self.txt_prefix_irrig = QLineEdit("irrig"); add_row("Prefix Irrigation cover file:", self.txt_prefix_irrig)
        self.txt_prefix_lai = QLineEdit("lai"); add_row("Prefix LAI file:", self.txt_prefix_lai)
        self.txt_nodata = QLineEdit("-9999"); add_row("No data value of grids:", self.txt_nodata)
        self.spin_first_step_no = LabeledSpin(
            "First Step Number:", 1, 1, 120, is_int=True)
        grid.addWidget(self.spin_first_step_no, row, 0, 1, 2); row += 1
        self.spin_steps_per_period = LabeledSpin(
            "Steps per Period:", 12, 1, 120, is_int=True)
        grid.addWidget(self.spin_steps_per_period, row, 0, 1, 2); row += 1
        grid.setRowStretch(row, 1)
        nav = QHBoxLayout()
        btn_prev = QPushButton("<< Check Inputs")
        btn_prev.clicked.connect(
            lambda: self.tabs.setCurrentIndex(TAB_CHECK))
        nav.addWidget(btn_prev)
        btn_def = QPushButton("Load Defaults")
        btn_def.clicked.connect(self._load_defaults)
        nav.addWidget(btn_def)
        nav.addStretch(1)
        btn_next = QPushButton("Next >>")
        btn_next.clicked.connect(
            lambda: self.tabs.setCurrentIndex(TAB_PARAMS))
        nav.addWidget(btn_next)
        grid.addLayout(nav, row, 0, 1, 2)
        return page

    # =================================================================
    # Parameters
    # =================================================================
    def _build_params_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)
        g_global = QGroupBox("Global parameters")
        gv = QVBoxLayout(g_global)
        self.p_a_int = LabeledSpin(
            '"a" interception [mm/day]:', 4.5, 0.1, 20)
        self.p_alfa = LabeledSpin("Alfa coefficient:", 1.5, 0.1, 10)
        self.p_lp = LabeledSpin(
            "LP coefficient (0.5–0.85):", 0.85, 0.05, 1.0)
        self.p_intens = LabeledSpin(
            "Average intensity [mm/h]:", 4.0, 0.1, 100)
        for w in (self.p_a_int, self.p_alfa, self.p_lp, self.p_intens):
            gv.addWidget(w)
        outer.addWidget(g_global)
        g_local = QGroupBox("Local parameters")
        lv = QVBoxLayout(g_local)
        self.p_w_slope = LabeledSpin("Slope factor:", 0.4, 0, 1)
        self.p_w_landuse = LabeledSpin("Land factor:", 0.3, 0, 1)
        self.p_w_soil = LabeledSpin("Soil factor:", 0.3, 0, 1)
        for w in (self.p_w_slope, self.p_w_landuse, self.p_w_soil):
            lv.addWidget(w)
        outer.addWidget(g_local)
        g_int = QGroupBox("Surface water interaction")
        iv = QVBoxLayout(g_int)
        self.p_area = LabeledSpin("Basin area [km²]:", 10.0, 0, 1e9)
        self.p_x = LabeledSpin('"x" coefficient (0–1):', 0.5, 0, 1)
        self.p_beta = LabeledSpin("Beta coefficient (0–1):", 0.75, 0, 1)
        self.p_contrib = LabeledSpin("Contribution factor:", 0.5, 0, 1)
        for w in (self.p_area, self.p_x, self.p_beta, self.p_contrib):
            iv.addWidget(w)
        outer.addWidget(g_int)
        self.chk_surface_interaction = QCheckBox(
            "Simulate surface water interaction in the result file")
        self.chk_surface_interaction.toggled.connect(
            self._on_surface_interaction)
        outer.addWidget(self.chk_surface_interaction)
        self._on_surface_interaction(
            self.chk_surface_interaction.isChecked())

        g_snow = QGroupBox("Snowmelt processing")
        sv = QVBoxLayout(g_snow)
        self.p_base_temp = LabeledSpin("Base temperature:", 0.0, -50, 50)
        self.p_melt_fact = LabeledSpin("Melt factor:", 0.02, 0, 5)
        self.p_snow_dens = LabeledSpin(
            "Snow density:", 0.1, 0.01, 1.0)
        for w in (self.p_base_temp, self.p_melt_fact, self.p_snow_dens):
            sv.addWidget(w)
        outer.addWidget(g_snow)
        self._on_snow_toggle(False)

        outer.addStretch(1)
        nav = QHBoxLayout()
        btn_prev = QPushButton("<< Settings")
        btn_prev.clicked.connect(
            lambda: self.tabs.setCurrentIndex(TAB_SETTINGS))
        nav.addWidget(btn_prev)
        btn_def = QPushButton("Load Defaults")
        btn_def.clicked.connect(self._load_default_params)
        nav.addWidget(btn_def)
        nav.addStretch(1)
        btn_next = QPushButton("Next >>")
        btn_next.clicked.connect(
            lambda: self.tabs.setCurrentIndex(TAB_OUTPUT_PERIOD))
        nav.addWidget(btn_next)
        outer.addLayout(nav)
        return page

    # =================================================================
    # Output Period
    # =================================================================
    def _build_output_period_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)
        g_types = QGroupBox("Output temporal resolution")
        tv = QVBoxLayout(g_types)
        self.chk_monthly_out = QCheckBox(
            "Monthly — one file per month")
        self.chk_monthly_out.setChecked(True)
        self.chk_seasonal_out = QCheckBox("Seasonal — wet + dry")
        self.chk_seasonal_out.setChecked(True)
        self.chk_annual_out = QCheckBox(
            "Annual — single file (mm/year)")
        self.chk_annual_out.setChecked(True)
        tv.addWidget(self.chk_monthly_out)
        tv.addWidget(self.chk_seasonal_out)
        tv.addWidget(self.chk_annual_out)
        outer.addWidget(g_types)

        g_stat = QGroupBox("Seasonal statistic")
        sl = QVBoxLayout(g_stat)
        self.radio_season_mean = QRadioButton(
            "Mean — average mm per month across the season")
        self.radio_season_sum = QRadioButton(
            "Sum — total mm accumulated over the season")
        self.radio_season_sum.setChecked(True)
        sl.addWidget(self.radio_season_mean)
        sl.addWidget(self.radio_season_sum)
        outer.addWidget(g_stat)

        g_preset = QGroupBox("Season preset")
        pv = QHBoxLayout(g_preset)
        self.combo_preset = QComboBox()
        for label, _ in SEASON_PRESETS:
            self.combo_preset.addItem(label)
        self.combo_preset.setCurrentIndex(1)
        pv.addWidget(self.combo_preset, 1)
        outer.addWidget(g_preset)

        g_seasons = QGroupBox("Season definitions")
        sv = QGridLayout(g_seasons)
        sv.addWidget(QLabel("<b>Wet season</b>"), 0, 0, 1, 4)
        sv.addWidget(QLabel("From month:"), 1, 0)
        self.combo_wet_start = QComboBox()
        for i, name in enumerate(MONTH_NAMES, 1):
            self.combo_wet_start.addItem(name, i)
        self.combo_wet_start.setCurrentIndex(5)
        sv.addWidget(self.combo_wet_start, 1, 1)
        sv.addWidget(QLabel("To month:"), 1, 2)
        self.combo_wet_end = QComboBox()
        for i, name in enumerate(MONTH_NAMES, 1):
            self.combo_wet_end.addItem(name, i)
        self.combo_wet_end.setCurrentIndex(8)
        sv.addWidget(self.combo_wet_end, 1, 3)
        sv.addWidget(QLabel("<b>Dry season</b>"), 2, 0, 1, 4)
        sv.addWidget(QLabel("From month:"), 3, 0)
        self.combo_dry_start = QComboBox()
        for i, name in enumerate(MONTH_NAMES, 1):
            self.combo_dry_start.addItem(name, i)
        self.combo_dry_start.setCurrentIndex(9)
        sv.addWidget(self.combo_dry_start, 3, 1)
        sv.addWidget(QLabel("To month:"), 3, 2)
        self.combo_dry_end = QComboBox()
        for i, name in enumerate(MONTH_NAMES, 1):
            self.combo_dry_end.addItem(name, i)
        self.combo_dry_end.setCurrentIndex(4)
        sv.addWidget(self.combo_dry_end, 3, 3)
        outer.addWidget(g_seasons)

        self.lbl_season_preview = QLabel()
        self.lbl_season_preview.setWordWrap(True)
        self.lbl_season_preview.setStyleSheet(
            "color:#333; background:#f5f5f5; padding:6px; "
            "border-radius:3px;")
        outer.addWidget(self.lbl_season_preview)

        self.combo_preset.currentIndexChanged.connect(
            self._on_preset_changed)
        for c in (self.combo_wet_start, self.combo_wet_end,
                  self.combo_dry_start, self.combo_dry_end):
            c.currentIndexChanged.connect(self._on_season_changed)
        self._apply_preset(1)

        outer.addStretch(1)
        nav = QHBoxLayout()
        btn_prev = QPushButton("<< Parameters")
        btn_prev.clicked.connect(
            lambda: self.tabs.setCurrentIndex(TAB_PARAMS))
        nav.addWidget(btn_prev)
        nav.addStretch(1)
        btn_next = QPushButton("Next >>")
        btn_next.clicked.connect(
            lambda: self.tabs.setCurrentIndex(TAB_RUN))
        nav.addWidget(btn_next)
        outer.addLayout(nav)
        return page

    def _month_list(self, start, end):
        if start <= end:
            return list(range(start, end + 1))
        return list(range(start, 13)) + list(range(1, end + 1))

    def _apply_preset(self, idx):
        if idx <= 0:
            return
        entry = SEASON_PRESETS[idx]
        if entry[1] is None:
            return
        (ws, we), (ds, de) = entry[1]
        combos = (self.combo_wet_start, self.combo_wet_end,
                  self.combo_dry_start, self.combo_dry_end)
        for c in combos:
            c.blockSignals(True)
        self.combo_wet_start.setCurrentIndex(ws - 1)
        self.combo_wet_end.setCurrentIndex(we - 1)
        self.combo_dry_start.setCurrentIndex(ds - 1)
        self.combo_dry_end.setCurrentIndex(de - 1)
        for c in combos:
            c.blockSignals(False)
        self._update_season_preview()

    def _on_preset_changed(self, idx):
        if idx <= 0:
            return
        self._apply_preset(idx)

    def _on_season_changed(self, _idx):
        if self.combo_preset.currentIndex() != 0:
            self.combo_preset.blockSignals(True)
            self.combo_preset.setCurrentIndex(0)
            self.combo_preset.blockSignals(False)
        self._update_season_preview()

    def _update_season_preview(self):
        wet = self._month_list(self.combo_wet_start.currentData(),
                                self.combo_wet_end.currentData())
        dry = self._month_list(self.combo_dry_start.currentData(),
                                self.combo_dry_end.currentData())
        wn = [MONTH_NAMES[m - 1] for m in wet]
        dn = [MONTH_NAMES[m - 1] for m in dry]
        overlap = sorted(set(wet) & set(dry))
        stat = ("Sum (total mm)"
                if self.radio_season_sum.isChecked()
                else "Mean (mm/month avg)")
        txt = (f"<b>Wet season</b> ({len(wet)} months): "
               f"{', '.join(wn)}<br>"
               f"<b>Dry season</b> ({len(dry)} months): "
               f"{', '.join(dn)}<br>"
               f"<b>Seasonal statistic:</b> {stat}")
        if overlap:
            on = [MONTH_NAMES[m - 1] for m in overlap]
            txt += (f"<br><span style='color:#b00;'><b>Warning:</b> "
                    f"{', '.join(on)} appear in BOTH seasons.</span>")
        self.lbl_season_preview.setText(txt)

    # =================================================================
    # Run
    # =================================================================
    def _build_run_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)
        row = QHBoxLayout()
        lbl = QLabel("Output Directory:")
        lbl.setFont(QFont("", 9, QFont.Weight.Bold))
        row.addWidget(lbl)
        self.pick_output_dir = PathPicker("dir")
        row.addWidget(self.pick_output_dir, 1)
        outer.addLayout(row)
        self.chk_add_irrig = QCheckBox(
            "Add irrigated water to rainfall from irrigation folder.")
        outer.addWidget(self.chk_add_irrig)
        self.chk_frac_from_dir = QCheckBox(
            "Read fractions from provided Fractions folder.")
        outer.addWidget(self.chk_frac_from_dir)
        sim_row = QHBoxLayout()
        self.chk_sim_file = QCheckBox(
            "Create a simulation file for calibration with the "
            "provided name:")
        self.chk_sim_file.setChecked(True)
        sim_row.addWidget(self.chk_sim_file)
        self.txt_sim_filename = QLineEdit("Simulated.tbl")
        self.txt_sim_filename.setFixedWidth(160)
        sim_row.addWidget(self.txt_sim_filename)
        sim_row.addStretch(1)
        outer.addLayout(sim_row)
        self.chk_snow = QCheckBox(
            "Include snow processes in the modelling")
        self.chk_snow.toggled.connect(self._on_snow_toggle)
        outer.addWidget(self.chk_snow)
        self._on_snow_toggle(self.chk_snow.isChecked())

        g_compat = QGroupBox("Model version")
        cv = QVBoxLayout(g_compat)
        self.chk_steady_state = QCheckBox(
            "Wetspass-M 2016 compatibility mode")
        self.chk_steady_state.setToolTip(
            "Reproduces the original Wetspass-M 2016 (steady-state) "
            "physics:\n"
            "  • soil moisture reset to zero every month (ΔS ≡ 0)\n"
            "  • soil-derived ET capped at rainfall (not P − runoff)\n"
            "  • groundwater-fed ET folded into "
            "Cell_evapotranspiration\n"
            "  • wb_error uses ET adjusted by the recharge correction")
        self.chk_steady_state.toggled.connect(
            self._on_steady_state_toggled)
        cv.addWidget(self.chk_steady_state)
        cnote = QLabel(
            "<i>Compatibility mode disables the soil bucket, ignores "
            "soil-moisture state files, and runs a single pass. The "
            "Water Balance tab will report ΔS = 0.</i>")
        cnote.setWordWrap(True); cnote.setStyleSheet("color:#555;")
        cv.addWidget(cnote)
        outer.addWidget(g_compat)

        g_soil = QGroupBox("Soil-water bucket & spin-up")
        sv = QVBoxLayout(g_soil)
        note1 = QLabel(
            "This model tracks soil moisture from month to month (a "
            "soil-water bucket). It must be run through the same 12 "
            "monthly input maps several times before the soil moisture "
            "reaches a repeating annual cycle. The workflow exits early "
            "when the bucket has converged, so an over-generous spinner "
            "value costs nothing.")
        note1.setWordWrap(True); sv.addWidget(note1)
        surow = QHBoxLayout()
        surow.addWidget(QLabel("Spin-up cycles:"))
        self.spin_spin_up = QSpinBox()
        self.spin_spin_up.setRange(0, 10)
        self.spin_spin_up.setValue(7)
        self.spin_spin_up.setToolTip(
            "Maximum number of full-year passes run BEFORE the reported "
            "run, so the soil-water bucket reaches a repeating seasonal "
            "cycle.\n\n"
            "The workflow checks convergence after every cycle and "
            "exits early as soon as the bucket has stabilised — so a "
            "generous value costs nothing. The default of 7 is safe "
            "for any climate; reduce it only if you want a hard upper "
            "limit on how long the spin-up can take.")
        surow.addWidget(self.spin_spin_up)
        surow.addWidget(QLabel(
            "(default: 7 — exits early on convergence)"))
        surow.addStretch(1)
        sv.addLayout(surow)
        self.chk_save_soil_state = QCheckBox(
            "Save final soil moisture to disk for future runs")
        self.chk_save_soil_state.setChecked(True)
        sv.addWidget(self.chk_save_soil_state)
        self.btn_reset_soil = QPushButton("Reset soil moisture state")
        self.btn_reset_soil.clicked.connect(self._reset_soil_state)
        sv.addWidget(self.btn_reset_soil)
        note2 = QLabel(
            "<i>Convergence metric: mean |ΔS| across the grid, "
            "compared against max(1 mm, 0.5 % of mean annual P).</i>")
        note2.setWordWrap(True); note2.setStyleSheet("color:#555;")
        sv.addWidget(note2)

        self.chk_auto_checkpoint = QCheckBox(
            "Enable automatic checkpointing "
            "(saves progress after every month)")
        self.chk_auto_checkpoint.setChecked(True)
        self.chk_auto_checkpoint.setToolTip(
            "When enabled, the workflow silently saves its state to "
            "the output folder after every completed month.\n\n"
            "Only one month of computation is ever at risk.")
        sv.addWidget(self.chk_auto_checkpoint)

        outer.addWidget(g_soil)

        g_lai = QGroupBox("Leaf Area Index (LAI) Mode")
        lv = QVBoxLayout(g_lai)
        self.radio_lai_landuse = QRadioButton(
            "Automatically from Landuse Map lookups")
        self.radio_lai_landuse.setChecked(True)
        self.radio_lai_inputs = QRadioButton("Read LAI from 'Inputs'")
        lv.addWidget(self.radio_lai_landuse)
        lv.addWidget(self.radio_lai_inputs)
        self.chk_lai_per_step = QCheckBox(
            "Read LAI per step from LAI folder")
        lv.addWidget(self.chk_lai_per_step)
        lai_row = QHBoxLayout()
        lai_row.addWidget(QLabel("LAI map:"))
        self.pick_lai_map = PathPicker(
            "file",
            "GeoTIFF (*.tif *.tiff);;ASCII Grid (*.asc)")
        lai_row.addWidget(self.pick_lai_map, 1)
        lv.addLayout(lai_row)
        outer.addWidget(g_lai)
        self.radio_lai_inputs.toggled.connect(self._on_lai_mode)
        self.chk_lai_per_step.toggled.connect(self._on_lai_mode)
        self._on_lai_mode()

        self.checkpoint_banner = QGroupBox("Paused run detected")
        self.checkpoint_banner.setStyleSheet(
            "QGroupBox { background: #fff7e0; border: 1px solid "
            "#d6a800; border-radius: 4px; margin-top: 8px; padding: 8px; } "
            "QGroupBox::title { color: #8a6300; }")
        cb_layout = QHBoxLayout(self.checkpoint_banner)
        self.checkpoint_label = QLabel()
        self.checkpoint_label.setWordWrap(True)
        cb_layout.addWidget(self.checkpoint_label, 1)
        self.checkpoint_banner.hide()
        outer.addWidget(self.checkpoint_banner)
        outer.addStretch(1)
        nav = QHBoxLayout()
        btn_prev = QPushButton("<< Output Period")
        btn_prev.clicked.connect(
            lambda: self.tabs.setCurrentIndex(TAB_OUTPUT_PERIOD))
        nav.addWidget(btn_prev)
        btn_check = QPushButton("Check Inputs")
        btn_check.clicked.connect(self._run_check)
        nav.addWidget(btn_check)
        nav.addStretch(1)
        self.btn_discard = QPushButton("Discard checkpoint")
        self.btn_discard.clicked.connect(self._discard_checkpoint)
        self.btn_discard.setEnabled(False)
        nav.addWidget(self.btn_discard)
        self.btn_pause = QPushButton("Pause run")
        self.btn_pause.setFixedWidth(140)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._on_pause_button_clicked)
        nav.addWidget(self.btn_pause)
        self.btn_run = QPushButton("Run")
        self.btn_run.setFixedWidth(110)
        self.btn_run.setDefault(True)
        self.btn_run.clicked.connect(self._start_run)
        nav.addWidget(self.btn_run)
        outer.addLayout(nav)
        return page

    # =================================================================
    # Progress
    # =================================================================
    def _build_progress_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setFont(QFont("", 10, QFont.Weight.Bold))
        outer.addWidget(self.lbl_status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p% — %v of %m steps")
        outer.addWidget(self.progress_bar)

        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("Log:"))
        log_header.addStretch(1)
        self.btn_clear_log = QPushButton("Clear log")
        self.btn_clear_log.setToolTip(
            "Clear the log window. The on-disk QGIS log is not affected.")
        self.btn_clear_log.clicked.connect(self._clear_log)
        log_header.addWidget(self.btn_clear_log)
        outer.addLayout(log_header)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(QFont("Courier", 8))
        outer.addWidget(self.txt_log, 1)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_skip_final = QPushButton("Skip to final cycle")
        self.btn_skip_final.setToolTip(
            "Abandon the remaining spin-up cycles and jump straight to "
            "the final output cycle.")
        self.btn_skip_final.setEnabled(False)
        self.btn_skip_final.clicked.connect(self._skip_to_final)
        btn_row.addWidget(self.btn_skip_final)
        self.btn_cancel = QPushButton("Cancel run")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_run)
        btn_row.addWidget(self.btn_cancel)
        self.btn_close = QPushButton("Close dialog")
        self.btn_close.clicked.connect(self._close_dialog)
        btn_row.addWidget(self.btn_close)
        outer.addLayout(btn_row)
        return page

    def _close_dialog(self):
        self.hide()

    def _clear_log(self):
        self.txt_log.clear()
        self.status_label.setText("Log cleared.")

    # =================================================================
    # Results
    # =================================================================
    def _build_results_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.addWidget(QLabel(
            "Tick the check box next to each item of interest in "
            "model outputs."))
        cols = QHBoxLayout()
        col1 = QVBoxLayout()
        col2 = QVBoxLayout()
        self.output_checkboxes = {}
        for i, (key, label, default) in enumerate(OUTPUT_OPTIONS):
            cb = QCheckBox(label)
            cb.setChecked(default)
            self.output_checkboxes[key] = cb
            if i < (len(OUTPUT_OPTIONS) + 1) // 2:
                col1.addWidget(cb)
            else:
                col2.addWidget(cb)
        col1.addStretch(1)
        col2.addStretch(1)
        cols.addLayout(col1, 1)
        cols.addLayout(col2, 1)
        outer.addLayout(cols, 1)
        return page

    # =================================================================
    # Focused recharge tab
    # =================================================================
    def _build_focused_tab(self):
        from .focused_tab import FocusedRechargeTab
        self.focused_tab = FocusedRechargeTab(
            get_working_dir=lambda: self.pick_working_dir.text().strip(),
            get_output_dir=lambda: self.pick_output_dir.text().strip(),
            get_time_range=lambda: (self.spin_start_step.value(),
                                     self.spin_end_step.value()))
        return self.focused_tab

    # =================================================================
    # Water balance tab
    # =================================================================
    def _build_water_balance_tab(self):
        from .water_balance_tab import WaterBalanceTab
        self.water_balance_tab = WaterBalanceTab(
            get_working_dir=lambda: self.pick_working_dir.text().strip(),
            get_output_dir=lambda: self.pick_output_dir.text().strip())
        return self.water_balance_tab

    # =================================================================
    # Calibration tab
    # =================================================================
    def _build_calibration_tab(self):
        from .calibration_tab import CalibrationTab
        self.calibration_tab = CalibrationTab(
            get_output_dir=lambda: self.pick_output_dir.text().strip())
        return self.calibration_tab

    # =================================================================
    # About
    # =================================================================
    def _build_about_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16)
        html = """
        <h2>WetSpass-M Modern</h2>
        <p><b>Version 2.1.0</b> — spatially distributed monthly water
        balance model for any climate, land-use or soil type.</p>

        <h3>Scientific basis</h3>

        <h4>Is the model steady-state?</h4>
        <p>The original <b>Wetspass-M 2016</b> (Abdollahi, Bashir &amp;
        Batelaan) is a <b>quasi-steady-state</b> monthly water balance
        model. It assumes soil-water storage is in balance within each
        monthly step, so ΔS ≡ 0, and soil moisture is reset to zero at
        the start of every month.</p>

        <p><b>The default configuration of this version is not
        quasi-steady-state at the monthly scale.</b> It is a dynamic
        (state-tracking) monthly water balance model: soil moisture is
        carried as an explicit state variable from one month to the
        next, and monthly ΔS is a real, non-zero flux.</p>

        <h4>Governing equations (per grid cell, per month)</h4>
        <pre>
Interception          I     = f(LAI, rainfall, rainy days)
Surface runoff        SR    = f(rainfall, slope, land use, soil, I)
Available to soil     A     = S_prev + P − I − SR − ET_impervious
Soil-derived ET       E_s   = min(E_s_potential, A)   (tp + bare, capped)
Storage capacity      S_max = (fc − wp) · rootdepth · 1000     [mm]
Recharge              R     = max(0, A − E_s − S_max)          [overflow]
Soil moisture         S     = clamp(A − E_s, 0, S_max)         [state t+1]
Actual ET to atm.     AET   = I + E_s + ET_impervious
                                  + ET_gw_transp + ET_gw_evapo
                                  + ET_open_water
        </pre>
        <p><b>Mass balance (Wetspass-M 2016 convention):</b></p>
        <pre>P = AET + SR + R_total + ΔS,   where ΔS = S(t+1) − S(t)
closure = P − AET − SR − R_total − ΔS</pre>
        <p>AET already contains interception, groundwater-fed
        transpiration, groundwater evaporation, and open-water
        evaporation. Interception is reported as a separate raster for
        informational use only and is not subtracted again in the
        closure equation.</p>

        <h4>Interpreting the closure error</h4>
        <p>Hydrological modelling convention accepts closure errors
        below 1 % of mean annual P as excellent, 1–5 % as acceptable
        with caveats, and above 5 % as needing investigation.</p>

        <h3>Spin-up</h3>
        <p>The soil-water bucket needs several passes through the same
        12 monthly maps before it reaches a repeating annual cycle. The
        workflow checks convergence after every cycle and exits early
        as soon as the bucket has stabilised. The default maximum is 7
        cycles — safe for any climate, because the workflow stops as
        soon as convergence is met.</p>

        <h3>Automatic checkpointing</h3>
        <p>The workflow saves its state after every completed month. If
        the machine loses power or the drive is disconnected, the next
        time you press Run it resumes from the last completed month.</p>

        <h3>Original authors (2016)</h3>
        <p>K. Abdollahi, I. Bashir, O. Batelaan — Vrije Universiteit
        Brussel</p>

        <h3>Modernization (2026)</h3>
        <p>M. G. Gurmu &lt;mggurmu@gmail.com&gt;</p>
        """
        label = QLabel(html)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        outer.addWidget(label)
        outer.addStretch(1)
        return page

    # =================================================================
    # Help
    # =================================================================
    def _user_guide_path(self):
        plugin_root = Path(__file__).parent.parent
        candidates = [
            plugin_root / "help" / USER_GUIDE_FILENAME,
            plugin_root / "docs" / USER_GUIDE_FILENAME,
            plugin_root / USER_GUIDE_FILENAME,
        ]
        for p in candidates:
            if p.exists():
                return p
        return candidates[0]

    def _build_help_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        title = QLabel("WetSpass-M Modern — User Guide")
        title.setFont(QFont("", 14, QFont.Weight.Bold))
        outer.addWidget(title)

        intro = QLabel(
            "This tab provides quick reference material and gives "
            "access to the full PDF user guide.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        pdf_row = QHBoxLayout()
        self.btn_open_pdf = QPushButton("📄  Open User Guide (PDF)")
        self.btn_open_pdf.setMinimumHeight(36)
        self.btn_open_pdf.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 6px 14px; }")
        self.btn_open_pdf.clicked.connect(self._open_user_guide)
        pdf_row.addWidget(self.btn_open_pdf)

        self.btn_reveal_pdf = QPushButton("Show in folder")
        self.btn_reveal_pdf.clicked.connect(self._reveal_user_guide)
        pdf_row.addWidget(self.btn_reveal_pdf)

        self.btn_help_path = QPushButton("Show expected path")
        self.btn_help_path.clicked.connect(self._show_user_guide_path)
        pdf_row.addWidget(self.btn_help_path)
        pdf_row.addStretch(1)
        outer.addLayout(pdf_row)

        self.help_browser = QTextBrowser()
        self.help_browser.setOpenExternalLinks(True)
        self.help_browser.setStyleSheet(
            "QTextBrowser { background:#fafafa; border:1px solid #ccc; "
            "border-radius:4px; padding:8px; }")
        self.help_browser.setHtml(self._help_html())
        outer.addWidget(self.help_browser, 1)
        return page

    def _help_html(self):
        return """
        <h3>Recommended workflow</h3>
        <ol>
          <li>Fill the <b>Inputs</b> tab — working directory, DEM,
              land use, soil, slope, and the <b>project boundary
              shapefile</b>. Click <b>Auto-load</b> if your inputs are
              laid out in the standard folder structure.</li>
          <li>Open <b>Check Inputs</b> and confirm that every table
              and raster is green.</li>
          <li>Review the <b>Parameters</b> tab.</li>
          <li>Choose output resolution on <b>Output Period</b>.</li>
          <li>On <b>Run</b>: set the output directory. Leave spin-up at
              the default 7. Keep automatic checkpointing on.</li>
          <li>Press <b>Run</b>. Watch the <b>Progress</b> tab.</li>
          <li>If you need focused recharge, open the <b>Focused
              Recharge</b> tab, click <b>Recompute from inputs</b>,
              run it, then click <b>Combine → Total recharge</b>.</li>
          <li>Open <b>Water Balance</b> and click
              <b>Load from output folder</b>.</li>
          <li>Open <b>Calibration</b> to compare against observed
              data if you have any.</li>
          <li>To remove the rectangular zero collar from the outputs,
              click <b>Crop outputs to boundary</b> above the status
              bar.</li>
        </ol>

        <h3>Crop outputs to boundary</h3>
        <p>Click <b>Crop outputs to boundary</b> to open a dialog that
        lists every raster in your output folder, grouped by variable
        and period. Tick the ones you want cropped, then click OK.
        Originals are preserved — cropped copies go to
        <code>cropped_to_boundary/</code> inside the source folder.</p>

        <h3>Run reports</h3>
        <p>After each diffusive run, <code>RunSummary.txt</code> and
        <code>_run_metadata.json</code> are written to the output
        folder. After each focused-recharge run,
        <code>RunSummary_focused.txt</code> and
        <code>_run_metadata_focused.json</code> are written.</p>

        <h3>Mass balance closure</h3>
        <p>The closure equation is:</p>
        <pre>closure = P − AET − SR − R_total − ΔS</pre>
        <p><b>Note:</b> AET already includes interception, groundwater-
        fed ET, and open-water evaporation. The diagnostics
        Cell_gw_discharge, Cell_ow_evaporation, and
        Cell_imper_evaporation are components inside AET and are not
        subtracted again in the closure.</p>

        <h3>gwdepth input</h3>
        <p>The gwdepth rasters are inputs the user prepares outside
        the plugin (e.g. by interpolating borehole observations with
        QGIS's IDW or kriging tools). The <b>Analyse gwdepth</b>
        button in the tools row reports the per-month distribution.</p>
        """

    def _open_user_guide(self):
        p = self._user_guide_path()
        if not p.exists():
            QMessageBox.warning(
                self, "User Guide",
                f"User guide PDF not found.\n\n"
                f"Expected location:\n{p}\n\n"
                f"Place '{USER_GUIDE_FILENAME}' in the plugin's "
                f"'help/' folder.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _reveal_user_guide(self):
        p = self._user_guide_path()
        if not p.exists():
            QMessageBox.warning(
                self, "User Guide",
                f"User guide PDF not found.\n\n"
                f"Expected location:\n{p}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))

    def _show_user_guide_path(self):
        p = self._user_guide_path()
        QMessageBox.information(
            self, "User Guide path",
            f"Expected location:\n{p}\n\n"
            f"Exists: {'yes' if p.exists() else 'no'}")

    # =================================================================
    # Defaults
    # =================================================================
    def _load_defaults(self):
        self.txt_prefix_rain.setText("rain")
        self.txt_prefix_snow.setText("snowcover")
        self.txt_prefix_pet.setText("pet")
        self.txt_prefix_temp.setText("temp")
        self.txt_prefix_wind.setText("wind")
        self.txt_prefix_gwdepth.setText("gwdepth")
        self.txt_prefix_irrig.setText("irrig")
        self.txt_prefix_lai.setText("lai")
        self.txt_nodata.setText("-9999")
        self.spin_first_step_no.setValue(1)
        self.spin_steps_per_period.setValue(12)
        self._refresh_run_tab_state()

    def _load_default_params(self):
        self.p_a_int.setValue(4.5); self.p_alfa.setValue(1.5)
        self.p_lp.setValue(0.85); self.p_intens.setValue(4.0)
        self.p_w_slope.setValue(0.4); self.p_w_landuse.setValue(0.3)
        self.p_w_soil.setValue(0.3); self.p_area.setValue(10.0)
        self.p_x.setValue(0.5); self.p_beta.setValue(0.75)
        self.p_contrib.setValue(0.5); self.p_base_temp.setValue(0.0)
        self.p_melt_fact.setValue(0.02); self.p_snow_dens.setValue(0.1)

    # =================================================================
    # Boundary helpers
    # =================================================================
    def _on_boundary_changed(self, _path):
        with contextlib.suppress(Exception):
            QSettings().setValue(
                "wetspass_modern/boundary_path",
                self.pick_boundary.text().strip())
        if hasattr(self, "boundary_preview"):
            self._refresh_boundary_preview()

    def _refresh_boundary_preview(self):
        if not hasattr(self, "boundary_preview"):
            return
        path = ""
        if hasattr(self, "pick_boundary"):
            path = self.pick_boundary.text().strip()
        if not path:
            self.boundary_preview.set_boundary(None)
            return
        p = Path(path)
        if not p.exists():
            self.boundary_preview.set_boundary(None)
            return
        self.boundary_preview.set_boundary(path)

    def _check_boundary_for_report(self, report):
        result = {"ok": False,
                  "message": "No boundary shapefile set.",
                  "path": ""}
        if not hasattr(self, "pick_boundary"):
            return result
        path = self.pick_boundary.text().strip()
        if not path:
            return result
        result["path"] = path
        p = Path(path)
        if not p.exists():
            result["message"] = f"File not found: {p}"
            return result
        try:
            from qgis.core import QgsVectorLayer
            layer = QgsVectorLayer(path, "boundary", "ogr")
            if not layer.isValid():
                result["message"] = "Layer is not a valid vector file."
                return result
            if layer.featureCount() == 0:
                result["message"] = "Shapefile has zero features."
                return result
            sample = next(layer.getFeatures(), None)
            if sample is None:
                result["message"] = "No geometry in first feature."
                return result
            geom = sample.geometry()
            if geom is None or geom.isEmpty():
                result["message"] = "First feature is empty."
                return result
            if geom.type() not in (2, 3):
                result["message"] = (
                    "Layer must be a polygon boundary; it is "
                    f"geometry type {geom.type()}.")
                return result
            crs = layer.crs()
            authid = crs.authid() or "unknown"
            ext = layer.extent()
            result["ok"] = True
            result["message"] = (
                f"OK — {layer.featureCount()} feature(s), "
                f"CRS {authid}, "
                f"bbox {ext.xMinimum():.1f}, {ext.yMinimum():.1f}, "
                f"{ext.xMaximum():.1f}, {ext.yMaximum():.1f}")
        except Exception as exc:
            result["message"] = f"Read error: {exc}"
        return result

    # =================================================================
    # Misc handlers
    # =================================================================
    def _on_steady_state_toggled(self, checked):
        if checked:
            self.spin_spin_up.setEnabled(False)
            self.spin_spin_up.setValue(0)
            self.chk_save_soil_state.setEnabled(False)
            self.chk_save_soil_state.setChecked(False)
        else:
            self.spin_spin_up.setEnabled(True)
            self.spin_spin_up.setValue(7)
            self.chk_save_soil_state.setEnabled(True)
            self.chk_save_soil_state.setChecked(True)

    def _reset_soil_state(self):
        out_dir = Path(self.pick_output_dir.text().strip() or "")
        if not out_dir.is_dir():
            QMessageBox.warning(self, "Reset",
                                "Output directory not set.")
            return
        state = out_dir / "soilwater_storage_initial.tif"
        if state.exists():
            if QMessageBox.question(
                    self, "Reset soil moisture",
                    f"Delete {state.name}?\n\nThe next run will start "
                    f"from a dry soil profile.",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                    ) == QMessageBox.StandardButton.Yes:
                try:
                    state.unlink()
                    QMessageBox.information(
                        self, "Reset", "Soil-moisture state deleted.")
                except OSError as exc:
                    QMessageBox.warning(self, "Reset", str(exc))
        else:
            QMessageBox.information(
                self, "Reset", "No soil-moisture state file exists.")

    def _on_slope_toggle(self, checked):
        self.pick_slope.setEnabled(not checked)

    def _on_snow_toggle(self, checked):
        self.p_base_temp.setEnabled(checked)
        self.p_melt_fact.setEnabled(checked)
        self.p_snow_dens.setEnabled(checked)

    def _on_surface_interaction(self, checked):
        self.p_area.setEnabled(checked); self.p_x.setEnabled(checked)
        self.p_beta.setEnabled(checked); self.p_contrib.setEnabled(checked)

    def _on_lai_mode(self):
        if self.radio_lai_landuse.isChecked():
            self.pick_lai_map.setEnabled(False)
            self.chk_lai_per_step.setEnabled(False)
            self.chk_lai_per_step.setChecked(False)
        else:
            self.pick_lai_map.setEnabled(
                not self.chk_lai_per_step.isChecked())
            self.chk_lai_per_step.setEnabled(True)

    def _on_tab_changed(self, index):
        if index == TAB_CHECK:
            self._run_check(silent=True)
        elif index == TAB_RUN:
            self._refresh_run_tab_state()
        elif index == TAB_FOCUSED:
            if self.worker is not None and self.worker.isRunning():
                return
            out = Path(self.pick_output_dir.text().strip())
            if not out or not out.is_dir():
                return
            has_runoff = any(out.glob("Cell_runoff_*.tif"))
            if has_runoff:
                return
            if self._focused_hint_shown:
                return
            self._focused_hint_shown = True
            self.txt_log.append("")
            self.txt_log.append("─" * 60)
            self.txt_log.append(
                "[Focused Recharge] The main model has not yet produced "
                "Cell_runoff rasters in the output folder. Run the main "
                "model first, then return to this tab.")
            self.txt_log.append("─" * 60)

    def _checkpoint_info(self):
        out_dir = self.pick_output_dir.text().strip()
        if not out_dir:
            return None
        try:
            return WetSpassWorkflow.read_checkpoint(out_dir)
        except Exception:
            return None

    def _refresh_run_tab_state(self):
        if not hasattr(self, "btn_pause"):
            return
        has_ck = self._checkpoint_info() is not None
        running = self.worker is not None and self.worker.isRunning()
        if running:
            self.btn_run.setEnabled(False)
            self.btn_pause.setText("Pause run")
            self.btn_pause.setEnabled(True)
            self.btn_discard.setEnabled(False)
            self._paused_mode = False
        elif has_ck:
            self.btn_run.setEnabled(False)
            self.btn_pause.setText("Resume run")
            self.btn_pause.setEnabled(True)
            self.btn_discard.setEnabled(True)
            self._paused_mode = True
            info = self._checkpoint_info()
            paused_at = info.get("paused_at", "?")
            cur_m = info.get("current_month", "?")
            next_b = info.get("next_block_index", "?")
            done = info.get("completed_months", [])
            self.checkpoint_label.setText(
                f"<b>Paused run found</b> — paused at {paused_at}<br>"
                f"Completed months: {done}<br>"
                f"Resume will start at month {cur_m}, block {next_b}.")
            self.checkpoint_banner.show()
        else:
            self.btn_run.setEnabled(True)
            self.btn_pause.setText("Pause run")
            self.btn_pause.setEnabled(False)
            self.btn_discard.setEnabled(False)
            self._paused_mode = False
            self.checkpoint_banner.hide()

    def _resume_from_checkpoint(self):
        info = self._checkpoint_info()
        if info is None:
            QMessageBox.warning(self, "Resume",
                                "No checkpoint was found.")
            return
        try:
            params = self._collect_parameters()
            out_dir = self.pick_output_dir.text().strip()
            wf = WetSpassWorkflow(
                working_dir=self.pick_working_dir.text(),
                output_dir=out_dir, parameters=params,
                start_step=self.spin_start_step.value(),
                end_step=self.spin_end_step.value(),
                simulate_snow=self.chk_snow.isChecked(),
                keep_outputs=self._collect_keep_outputs(),
                output_periods=(
                    (["monthly"] if self.chk_monthly_out.isChecked() else [])
                    + (["seasonal"] if self.chk_seasonal_out.isChecked() else [])
                    + (["annual"] if self.chk_annual_out.isChecked() else []))
                    or ["monthly"],
                wet_season=self._month_list(
                    self.combo_wet_start.currentData(),
                    self.combo_wet_end.currentData()),
                dry_season=self._month_list(
                    self.combo_dry_start.currentData(),
                    self.combo_dry_end.currentData()),
                seasonal_statistic=(
                    "mean" if self.radio_season_mean.isChecked()
                    else "sum"),
                spin_up_cycles=self.spin_spin_up.value(),
                save_soil_state=self.chk_save_soil_state.isChecked(),
                auto_checkpoint=self.chk_auto_checkpoint.isChecked())
            if wf._params_hash() != info.get("parameters_hash", ""):
                QMessageBox.warning(
                    self, "Resume",
                    "Current parameters differ from those used by the "
                    "paused run.\n\nRestore them, or click "
                    "'Discard checkpoint'.")
                return
        except Exception as exc:
            QMessageBox.warning(self, "Resume",
                                f"Could not validate checkpoint: {exc}")
            return
        self._launch_worker(resume=True)

    def _discard_checkpoint(self):
        out_dir = self.pick_output_dir.text().strip()
        if not out_dir:
            return
        if QMessageBox.question(
                self, "Discard checkpoint",
                "Delete the paused checkpoint?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                ) != QMessageBox.StandardButton.Yes:
            return
        with contextlib.suppress(Exception):
            WetSpassWorkflow.discard_checkpoint(out_dir)
        self._refresh_run_tab_state()

    def _on_working_dir_changed(self, path):
        if path and Path(path).is_dir():
            self._autoload_from_working_dir(silent=True)
            self._refresh_run_tab_state()

    def _autoload_from_working_dir(self, silent=False):
        wd = self.pick_working_dir.text().strip()
        if not wd:
            if not silent:
                QMessageBox.warning(self, "Auto-load",
                                    "Set the working directory first.")
            return
        wd_path = Path(wd)
        if not wd_path.is_dir():
            return

        candidate_dirs = [wd_path / "inputs" / "maps", wd_path / "maps",
                          wd_path / "Inputs" / "Maps", wd_path / "Maps"]
        maps_dir = next((d for d in candidate_dirs if d.is_dir()), None)
        if maps_dir is not None:
            files = (list(maps_dir.glob("*.tif"))
                     + list(maps_dir.glob("*.asc")))

            def _find(prefixes):
                for f in files:
                    stem = f.stem.lower()
                    for p in prefixes:
                        if stem.startswith(p):
                            return f
                return None

            dem_f = _find(["elevation", "dem"])
            lu_f = _find(["landuse", "land_use", "landcover"])
            soil_f = _find(["soil"])
            slope_f = _find(["slop"])
            lai_f = _find(["lai"])
            if dem_f: self.pick_dem.setText(str(dem_f))
            if lu_f: self.pick_landuse.setText(str(lu_f))
            if soil_f: self.pick_soil.setText(str(soil_f))
            if slope_f: self.pick_slope.setText(str(slope_f))
            if lai_f: self.pick_lai_map.setText(str(lai_f))

        if hasattr(self, "pick_boundary"):
            boundary_subdirs = [
                wd_path,
                wd_path / "inputs",
                wd_path / "Inputs",
                wd_path / "inputs" / "shapefiles",
                wd_path / "inputs" / "Shapefiles",
                wd_path / "Inputs" / "shapefiles",
                wd_path / "Inputs" / "Shapefiles",
                wd_path / "Inputs" / "maps",
                wd_path / "inputs" / "maps",
            ]

            boundary_named = []
            boundary_any = []
            for pattern in ("*.shp", "*.gpkg", "*.geojson"):
                for d in boundary_subdirs:
                    if not d.is_dir():
                        continue
                    for c in d.glob(pattern):
                        boundary_any.append(c)
                        stem = c.stem.lower()
                        if ("boundary" in stem or "catchment" in stem
                                or "basin" in stem):
                            boundary_named.append(c)

            picked = None
            if boundary_named:
                picked = boundary_named[0]
            elif boundary_any:
                picked = boundary_any[0]

            current = self.pick_boundary.text().strip()
            if picked is not None:
                new = str(picked)
                if new != current:
                    self.pick_boundary.setText(new)
                    self._on_boundary_changed(new)
            else:
                if current:
                    self.pick_boundary.setText("")
                    self._on_boundary_changed("")

        out_dir = wd_path / "outputs"
        if not self.pick_output_dir.text():
            self.pick_output_dir.setText(str(out_dir))

        with contextlib.suppress(Exception):
            self._run_check(silent=True)

    # =================================================================
    # Check inputs
    # =================================================================
    def _run_check(self, silent=False):
        wd = self.pick_working_dir.text().strip()
        if not wd:
            if not silent:
                QMessageBox.warning(self, "Check Inputs",
                                    "Working directory is empty.")
            return
        try:
            report = inspect_inputs(
                working_dir=wd,
                start_step=self.spin_start_step.value(),
                end_step=self.spin_end_step.value(),
                simulate_snow=self.chk_snow.isChecked())
        except Exception as exc:
            if not silent:
                import traceback
                self.txt_log.append("[Check Inputs] Exception:\n"
                                    + traceback.format_exc())
                QMessageBox.critical(self, "Check Inputs",
                                     f"Failed: {exc}")
            return

        self.tree_status.clear()

        hdr = QTreeWidgetItem(["Search paths", "", ""])
        hdr.addChild(QTreeWidgetItem(
            ["Working dir", "", report["working_dir"]]))
        hdr.addChild(QTreeWidgetItem(
            ["Layout used", "", report["layout"]]))
        hdr.addChild(QTreeWidgetItem(
            ["Maps dir", "", report["maps_dir"]]))
        hdr.addChild(QTreeWidgetItem(
            ["Tables dir", "", report["tables_dir"]]))
        hdr.setExpanded(True)
        self.tree_status.addTopLevelItem(hdr)

        t_root = QTreeWidgetItem(["Lookup tables", "", ""])
        self.tree_status.addTopLevelItem(t_root)
        for entry in report["tables"]:
            icon = "✔" if entry["ok"] else "✘"
            color = QColor(0, 140, 0) if entry["ok"] else QColor(190, 0, 0)
            item = QTreeWidgetItem(
                [entry["name"], icon, entry["message"]])
            item.setForeground(1, color)
            item.setToolTip(2, entry.get("path", ""))
            t_root.addChild(item)
        t_root.setExpanded(True)

        f_root = QTreeWidgetItem(["Input rasters", "", ""])
        self.tree_status.addTopLevelItem(f_root)
        for entry in report["folders"]:
            icon = "✔" if entry["ok"] else "✘"
            color = QColor(0, 140, 0) if entry["ok"] else QColor(190, 0, 0)
            item = QTreeWidgetItem(
                [entry["name"], icon, entry["message"]])
            item.setForeground(1, color)
            f_root.addChild(item)
        f_root.setExpanded(True)

        b_root = QTreeWidgetItem(["Project boundary", "", ""])
        self.tree_status.addTopLevelItem(b_root)
        b_info = self._check_boundary_for_report(report)
        b_icon = "✔" if b_info["ok"] else "✘"
        b_color = (QColor(0, 140, 0) if b_info["ok"]
                   else QColor(190, 0, 0))
        b_item = QTreeWidgetItem([
            "Boundary shapefile",
            b_icon,
            b_info["message"]])
        b_item.setForeground(1, b_color)
        b_item.setToolTip(2, b_info.get("path", ""))
        b_root.addChild(b_item)
        b_root.setExpanded(True)

        gc = report.get("grid_consistency") or []
        g_root = QTreeWidgetItem(["Grid consistency", "", ""])
        self.tree_status.addTopLevelItem(g_root)
        if not gc:
            g_root.addChild(QTreeWidgetItem(
                ["(not checked)", "?", "No data"]))
        else:
            for entry in gc:
                icon = "✔" if entry["ok"] else "✘"
                color = (QColor(0, 140, 0) if entry["ok"]
                         else QColor(200, 120, 0))
                item = QTreeWidgetItem(
                    [entry["name"], icon, entry["message"]])
                item.setForeground(1, color)
                g_root.addChild(item)
        g_root.setExpanded(True)

        self._refresh_boundary_preview()

        n_t_ok = sum(1 for e in report["tables"] if e["ok"])
        n_t = len(report["tables"])
        n_f_ok = sum(1 for e in report["folders"] if e["ok"])
        n_f = len(report["folders"])
        if not silent:
            self.txt_log.append(
                f"[Check Inputs] layout={report['layout']}  "
                f"{n_t_ok}/{n_t} tables, {n_f_ok}/{n_f} rasters, "
                f"boundary={'OK' if b_info['ok'] else 'missing'}")

        overall_ok = (report["overall_ok"]
                      and (b_info["ok"]
                           or not self.pick_boundary.text().strip()))
        if overall_ok:
            self.status_label.setText("Input check passed.")
            self.status_progress.setValue(100)
        else:
            self.status_label.setText(
                f"Input check: {n_t_ok}/{n_t} tables, "
                f"{n_f_ok}/{n_f} rasters, "
                f"boundary={'OK' if b_info['ok'] else 'X'}")
            self.status_progress.setValue(0)
        if not silent:
            self.tabs.setCurrentIndex(TAB_CHECK)

    # =================================================================
    # Parameter collection
    # =================================================================
    def _collect_parameters(self):
        return WetSpassParameters(
            a_interception=self.p_a_int.value(),
            alfa=self.p_alfa.value(),
            w_slope=self.p_w_slope.value(),
            w_landuse=self.p_w_landuse.value(),
            w_soil=self.p_w_soil.value(),
            x_coef=self.p_x.value(),
            lp=self.p_lp.value(),
            intensity=self.p_intens.value(),
            beta=self.p_beta.value(),
            contribution=self.p_contrib.value(),
            base_temp=self.p_base_temp.value(),
            melt_factor=self.p_melt_fact.value(),
            snow_density=self.p_snow_dens.value(),
            area_km2=self.p_area.value(),
            q0_surface=self.spin_q0_surf.value(),
            q0_base=self.spin_q0_base.value(),
            steady_state_mode=self.chk_steady_state.isChecked())

    def _collect_keep_outputs(self):
        keep = {k for k, cb in self.output_checkboxes.items()
                if cb.isChecked()}
        if not keep:
            keep = {"recharge", "Cell_evapotranspiration"}
        return keep

    def _validate_inputs(self):
        errors = []
        if not self.pick_working_dir.text():
            errors.append("Working directory is empty.")
        if not self.pick_dem.text():
            errors.append("DEM map is empty.")
        if not self.pick_landuse.text():
            errors.append("Land use map is empty.")
        if not self.pick_soil.text():
            errors.append("Soil map is empty.")
        if (not self.chk_slope_from_dem.isChecked()
                and not self.pick_slope.text()):
            errors.append("Slope map is empty "
                          "(or check 'Create from DEM').")
        if not self.pick_output_dir.text():
            errors.append("Output directory is empty.")
        return "\n".join(errors)

    def _on_pause_button_clicked(self):
        if self._paused_mode:
            self._resume_from_checkpoint()
        else:
            self._pause_run()

    def _start_run(self):
        err = self._validate_inputs()
        if err:
            QMessageBox.warning(self, "Missing inputs", err)
            self.tabs.setCurrentIndex(TAB_INPUTS)
            return
        self._run_check(silent=False)
        self._launch_worker(resume=False)

    # =================================================================
    # Worker launch
    # =================================================================
    def _launch_worker(self, resume):
        output_dir = Path(self.pick_output_dir.text())
        output_dir.mkdir(parents=True, exist_ok=True)
        params = self._collect_parameters()
        keep_outputs = self._collect_keep_outputs()
        periods = []
        if self.chk_monthly_out.isChecked():
            periods.append("monthly")
        if self.chk_seasonal_out.isChecked():
            periods.append("seasonal")
        if self.chk_annual_out.isChecked():
            periods.append("annual")
        if not periods:
            periods = ["monthly"]
        wet = self._month_list(self.combo_wet_start.currentData(),
                                self.combo_wet_end.currentData())
        dry = self._month_list(self.combo_dry_start.currentData(),
                                self.combo_dry_end.currentData())
        stat = "mean" if self.radio_season_mean.isChecked() else "sum"
        start = self.spin_start_step.value()
        end = self.spin_end_step.value()
        if end < start:
            QMessageBox.warning(self, "Invalid time range",
                                "Final step must be >= starting step.")
            return
        self.worker = WetSpassWorker(
            working_dir=self.pick_working_dir.text(),
            output_dir=str(output_dir),
            parameters=params,
            start_step=start, end_step=end,
            simulate_snow=self.chk_snow.isChecked(),
            create_simulation_file=self.chk_sim_file.isChecked(),
            simulation_filename=self.txt_sim_filename.text().strip()
                                or "Simulated.tbl",
            keep_outputs=keep_outputs,
            output_periods=periods,
            wet_season=wet, dry_season=dry,
            seasonal_statistic=stat,
            resume_from_checkpoint=resume,
            spin_up_cycles=self.spin_spin_up.value(),
            save_soil_state=self.chk_save_soil_state.isChecked(),
            auto_checkpoint=self.chk_auto_checkpoint.isChecked())
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._on_log)
        self.worker.finished_ok.connect(self._on_finished_ok)
        self.worker.finished_error.connect(self._on_finished_error)
        self.worker.paused.connect(self._on_paused)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.disk_error.connect(self._on_disk_error)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p% — %v of %m steps")
        self.status_progress.setValue(0)
        self._pause_requested_by_user = False
        self._refresh_run_tab_state()
        self._poll_timer.start()
        self.status_label.setText("Resuming…" if resume else "Running…")
        self.lbl_status.setText("Resuming…" if resume else "Running…")
        self.tabs.setCurrentIndex(TAB_PROGRESS)
        self.txt_log.append(
            f"\n[Run {'resumed' if resume else 'started'}]")

        self.btn_cancel.setEnabled(True)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("Pause run")
        self.btn_skip_final.setEnabled(True)

        self.worker.start()

    def _pause_run(self):
        if self.worker is None or not self.worker.isRunning():
            return
        self._pause_requested_by_user = True
        self.worker.request_pause()
        self.lbl_status.setText(
            "Pause requested — will stop after the current block…")
        self.status_label.setText("Pausing after current block…")
        self.progress_bar.setFormat(
            "Pausing after current block… (%p%)")
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("Pausing…")
        self.btn_cancel.setEnabled(True)

    def _skip_to_final(self):
        if self.worker is None or not self.worker.isRunning():
            return
        self.worker.skip_to_final()
        self.lbl_status.setText(
            "Skip to final cycle requested — will switch after the "
            "current spin-up cycle finishes.")
        self.status_label.setText(
            "Skip requested — switching to final cycle…")
        self.btn_skip_final.setEnabled(False)

    def _cancel_run(self):
        if self.worker is None or not self.worker.isRunning():
            return
        self.worker.cancel()
        self.lbl_status.setText(
            "Cancel requested — will stop after current block…")
        self.status_label.setText(
            "Cancel requested — stopping after current block…")
        self.btn_cancel.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_skip_final.setEnabled(False)

    # =================================================================
    # Worker callbacks
    # =================================================================
    def _on_progress(self, step, total, msg):
        with contextlib.suppress(Exception):
            if self.progress_bar.maximum() != total:
                self.progress_bar.setRange(0, max(total, 1))
            self.progress_bar.setValue(step)
            self.progress_bar.setFormat(f"%p% — step {step} of {total}")
            self.status_progress.setValue(int(100 * step / max(total, 1)))
            self.status_label.setText(msg)

    def _on_log(self, msg):
        self.txt_log.append(msg)
        with contextlib.suppress(Exception):
            sb = self.txt_log.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _on_finished_ok(self, results):
        self._poll_timer.stop()
        self.worker = None
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.progress_bar.setFormat("Done (%p%)")
        self.status_progress.setValue(100)
        self.lbl_status.setText("Model run complete.")
        self.status_label.setText("Model run complete.")
        self.btn_cancel.setEnabled(False)
        self.btn_skip_final.setEnabled(False)
        self._refresh_run_tab_state()
        output_dir = Path(results.get("output_dir", ""))
        annual = output_dir / "Recharge_diffusive_annual.tif"
        if annual.exists():
            layer = QgsRasterLayer(str(annual),
                                   "Recharge Diffusive (mm/yr)")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
        with contextlib.suppress(Exception):
            if hasattr(self, "water_balance_tab"):
                self.water_balance_tab._load_from_output()
        QMessageBox.information(
            self, "WetSpass-M Modern",
            f"Model run complete.\n\nOutput: {output_dir}")

    def _on_paused(self, results):
        self._poll_timer.stop()
        self.worker = None
        self.progress_bar.setFormat("Paused at step %v of %m")
        self.lbl_status.setText("Run paused. Checkpoint saved.")
        self.status_label.setText(
            "Paused — click 'Resume run' to continue.")
        self.btn_cancel.setEnabled(False)
        self.btn_skip_final.setEnabled(False)
        self._refresh_run_tab_state()
        QMessageBox.information(
            self, "WetSpass-M Modern",
            "Run paused. Click 'Resume run' on the Run tab.")

    def _on_cancelled(self, results):
        self._poll_timer.stop()
        self.worker = None
        self.progress_bar.setFormat("Cancelled at step %v of %m")
        self.lbl_status.setText("Run cancelled.")
        self.btn_cancel.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_skip_final.setEnabled(False)
        self._refresh_run_tab_state()
        QMessageBox.information(self, "WetSpass-M Modern",
                                "Run cancelled. Press 'Run' to restart.")

    def _on_disk_error(self, results):
        self._poll_timer.stop()
        self.worker = None
        self.progress_bar.setFormat("Paused — disk unavailable")
        self.lbl_status.setText(
            "Output device unavailable. Reconnect and resume.")
        self.status_label.setText(
            "Paused — reconnect the output drive, then Resume.")
        self.btn_cancel.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_skip_final.setEnabled(False)
        self._refresh_run_tab_state()

        msg = results.get("message", "")
        month = results.get("current_month", "?")
        next_b = results.get("next_block", "?")

        self.txt_log.append(
            "\n" + "=" * 60 + "\n"
            "OUTPUT DEVICE UNAVAILABLE\n"
            f"  Stopped at month : {month}\n"
            f"  Next block       : {next_b}\n"
            f"  Reason           : {msg}\n"
            "=" * 60)
        self.txt_log.append(
            "The run is safely paused and a checkpoint has been "
            "written.\nReconnect the external drive, then click "
            "'Resume run' on the Run tab to continue.")

        QMessageBox.warning(
            self, "WetSpass-M Modern",
            f"The output device became unavailable and the run has "
            f"been paused.\n\n"
            f"Stopped at month {month}.\n\n"
            f"Reconnect the external drive, then click "
            f"'Resume run' on the Run tab to continue from where it "
            f"stopped.\n\n"
            f"Details: {msg[:400]}")

    def _on_finished_error(self, err):
        self._poll_timer.stop()
        self.worker = None
        self.lbl_status.setText("Failed.")
        self.status_label.setText("Model failed.")
        self.btn_cancel.setEnabled(False)
        self.btn_skip_final.setEnabled(False)
        self._refresh_run_tab_state()
        self.txt_log.append("\n[ERROR]\n" + err)
        QMessageBox.critical(self, "WetSpass-M Modern",
                             f"Model failed:\n\n{err[:600]}")

    def _poll_worker_state(self):
        if self.worker is None:
            self._poll_timer.stop()
            return
        if self.worker.isRunning():
            return
        self._poll_timer.stop()
        status = getattr(self.worker, "last_status", None)
        info = self._checkpoint_info()
        if status == "cancelled":
            self.worker = None
            self._refresh_run_tab_state()
            self.progress_bar.setFormat("Cancelled at step %v of %m")
            self.btn_cancel.setEnabled(False)
            self.btn_skip_final.setEnabled(False)
        elif status == "disk_error":
            self.worker = None
            self._refresh_run_tab_state()
            self.progress_bar.setFormat("Paused — disk unavailable")
            self.btn_cancel.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_skip_final.setEnabled(False)
            self.lbl_status.setText(
                "Output device unavailable. Reconnect and resume.")
        elif status == "paused" or (info is not None
                                     and self._pause_requested_by_user):
            self.worker = None
            self._refresh_run_tab_state()
            self.progress_bar.setFormat("Paused at step %v of %m")
            self.btn_cancel.setEnabled(False)
            self.btn_skip_final.setEnabled(False)