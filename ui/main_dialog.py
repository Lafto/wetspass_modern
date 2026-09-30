# -*- coding: utf-8 -*-
"""WetSpass-M Modern — Main Tabbed Dialog."""

from pathlib import Path

from qgis.PyQt.QtCore import Qt, pyqtSignal, QTimer, QUrl, QEvent
from qgis.PyQt.QtGui import QFont, QIcon, QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QRadioButton, QGroupBox,
    QSpinBox, QDoubleSpinBox, QComboBox, QProgressBar, QTextEdit,
    QFileDialog, QMessageBox, QScrollArea, QFrame, QSizePolicy,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QTextBrowser,
)
from qgis.core import QgsRasterLayer, QgsProject

from .worker import WetSpassWorker
from ..core.calculation_engine import WetSpassParameters
from ..core.workflow import WetSpassWorkflow, inspect_inputs


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
     "AET of the Cell (includes I; also GW in compat mode)",             True),
    ("Cell_gw_discharge",
     "Groundwater + open-water part of AET (diagnostic)",                False),
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
TAB_WATER_BALANCE, TAB_FOCUSED, TAB_ABOUT, TAB_HELP = 8, 9, 10, 11

USER_GUIDE_FILENAME = "WetSpassM_UserGuide.pdf"


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
    scroll.setFrameShape(QFrame.NoFrame)
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
            Qt.Window | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)
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

        # Event filter on the QGIS main window — intercept close requests
        # while a model run is active.
        self._main_window = None
        try:
            self._main_window = self.iface.mainWindow()
            if self._main_window is not None:
                self._main_window.installEventFilter(self)
        except Exception:
            self._main_window = None

    # -----------------------------------------------------------------
    # Close interception
    # -----------------------------------------------------------------
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
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return False
        try:
            self.worker.cancel()
            self.worker.wait(5000)
        except Exception:
            pass
        return True

    def eventFilter(self, obj, event):
        if (obj is self._main_window
                and event.type() == QEvent.Close
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
        try:
            if self._main_window is not None:
                self._main_window.removeEventFilter(self)
        except Exception:
            pass
        self._main_window = None
        if self.worker is not None and self.worker.isRunning():
            try:
                self.worker.cancel()
                self.worker.wait(3000)
            except Exception:
                pass

    # -----------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------
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
        self.tabs.addTab(scrollable(self._build_water_balance_tab()),  "Water Balance")
        self.tabs.addTab(scrollable(self._build_focused_tab()),        "Focused Recharge")
        self.tabs.addTab(scrollable(self._build_about_tab()),          "About")
        self.tabs.addTab(scrollable(self._build_help_tab()),           "Help")
        root.addWidget(self.tabs, 1)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        status_frame = QFrame()
        status_frame.setFrameShape(QFrame.StyledPanel)
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
            lbl.setFont(QFont("", 9, QFont.Bold))
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
        self.pick_working_dir.pathChanged.connect(self._on_working_dir_changed)

        self.pick_dem = PathPicker("file", "GeoTIFF (*.tif *.tiff);;ASCII Grid (*.asc)")
        add_row("DEM map:", self.pick_dem)
        self.pick_landuse = PathPicker("file", "GeoTIFF (*.tif *.tiff);;ASCII Grid (*.asc)")
        add_row("Land use map:", self.pick_landuse)
        self.pick_soil = PathPicker("file", "GeoTIFF (*.tif *.tiff);;ASCII Grid (*.asc)")
        add_row("Soil map:", self.pick_soil)
        self.pick_slope = PathPicker("file", "GeoTIFF (*.tif *.tiff);;ASCII Grid (*.asc)")
        add_row("Slope map:", self.pick_slope)

        self.chk_slope_from_dem = QCheckBox("Create slope map from DEM directly")
        self.chk_slope_from_dem.toggled.connect(self._on_slope_toggle)
        layout.addWidget(self.chk_slope_from_dem, row, 1, 1, 2); row += 1
        # Initialise the dependent fields to match the checkbox state
        self._on_slope_toggle(self.chk_slope_from_dem.isChecked())

        self.spin_no_steps = LabeledSpin("No of time steps:", 12, 1, 120, is_int=True)
        layout.addWidget(self.spin_no_steps, row, 0, 1, 3); row += 1
        self.spin_start_step = LabeledSpin("Starting time step:", 1, 1, 120, is_int=True)
        layout.addWidget(self.spin_start_step, row, 0, 1, 3); row += 1
        self.spin_end_step = LabeledSpin("Final time step:", 12, 1, 120, is_int=True)
        layout.addWidget(self.spin_end_step, row, 0, 1, 3); row += 1

        qbox = QGroupBox("Initial discharge (surface water interaction)")
        qgrid = QGridLayout(qbox)
        self.spin_q0_surf = LabeledSpin("Q0 Surface [m³/month]:", 0.0, -1e9, 1e9, decimals=2)
        qgrid.addWidget(self.spin_q0_surf, 0, 0)
        self.spin_q0_base = LabeledSpin("Q0 Sub-surf. [m³/month]:", 0.0, -1e9, 1e9, decimals=2)
        qgrid.addWidget(self.spin_q0_base, 1, 0)
        layout.addWidget(qbox, row, 0, 1, 3); row += 1

        layout.setRowStretch(row, 1)
        nav = QHBoxLayout()
        btn_check = QPushButton("Check Inputs")
        btn_check.clicked.connect(self._run_check)
        nav.addWidget(btn_check)
        nav.addStretch(1)
        btn_next = QPushButton("Next >>")
        btn_next.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_CHECK))
        nav.addWidget(btn_next)
        layout.addLayout(nav, row, 0, 1, 3)
        return page

    def _build_check_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)
        outer.addWidget(QLabel("Press <b>Check Inputs</b> to verify inputs."))
        self.tree_status = QTreeWidget()
        self.tree_status.setColumnCount(3)
        self.tree_status.setHeaderLabels(["Item", "Status", "Details"])
        self.tree_status.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree_status.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree_status.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tree_status.setAlternatingRowColors(True)
        outer.addWidget(self.tree_status, 1)
        nav = QHBoxLayout()
        btn_prev = QPushButton("<< Inputs")
        btn_prev.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_INPUTS))
        nav.addWidget(btn_prev)
        btn_check = QPushButton("Check Inputs")
        btn_check.clicked.connect(self._run_check)
        nav.addWidget(btn_check)
        nav.addStretch(1)
        btn_next = QPushButton("Next >>")
        btn_next.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_SETTINGS))
        nav.addWidget(btn_next)
        outer.addLayout(nav)
        return page

    def _build_settings_tab(self):
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(14, 14, 14, 14)
        grid.setVerticalSpacing(8)
        row = 0

        def add_row(label, widget):
            nonlocal row
            lbl = QLabel(label)
            lbl.setFont(QFont("", 9, QFont.Bold))
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
        self.spin_first_step_no = LabeledSpin("First Step Number:", 1, 1, 120, is_int=True)
        grid.addWidget(self.spin_first_step_no, row, 0, 1, 2); row += 1
        self.spin_steps_per_period = LabeledSpin("Steps per Period:", 12, 1, 120, is_int=True)
        grid.addWidget(self.spin_steps_per_period, row, 0, 1, 2); row += 1
        grid.setRowStretch(row, 1)
        nav = QHBoxLayout()
        btn_prev = QPushButton("<< Check Inputs")
        btn_prev.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_CHECK))
        nav.addWidget(btn_prev)
        btn_def = QPushButton("Load Defaults")
        btn_def.clicked.connect(self._load_defaults)
        nav.addWidget(btn_def)
        nav.addStretch(1)
        btn_next = QPushButton("Next >>")
        btn_next.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_PARAMS))
        nav.addWidget(btn_next)
        grid.addLayout(nav, row, 0, 1, 2)
        return page

    def _build_params_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)
        g_global = QGroupBox("Global parameters")
        gv = QVBoxLayout(g_global)
        self.p_a_int = LabeledSpin('"a" interception [mm/day]:', 4.5, 0.1, 20)
        self.p_alfa = LabeledSpin("Alfa coefficient:", 1.5, 0.1, 10)
        self.p_lp = LabeledSpin("LP coefficient (0.5–0.85):", 0.85, 0.05, 1.0)
        self.p_intens = LabeledSpin("Average intensity [mm/h]:", 4.0, 0.1, 100)
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
        self.chk_surface_interaction.toggled.connect(self._on_surface_interaction)
        outer.addWidget(self.chk_surface_interaction)
        # Initialise the dependent fields to match the checkbox state
        self._on_surface_interaction(self.chk_surface_interaction.isChecked())

        g_snow = QGroupBox("Snowmelt processing")
        sv = QVBoxLayout(g_snow)
        self.p_base_temp = LabeledSpin("Base temperature:", 0.0, -50, 50)
        self.p_melt_fact = LabeledSpin("Melt factor:", 0.02, 0, 5)
        self.p_snow_dens = LabeledSpin("Snow density:", 0.1, 0.01, 1.0)
        for w in (self.p_base_temp, self.p_melt_fact, self.p_snow_dens):
            sv.addWidget(w)
        outer.addWidget(g_snow)
        # Snowmelt fields start disabled until the user enables snow on the
        # Run tab. The enabled state is applied below.
        self._on_snow_toggle(False)

        outer.addStretch(1)
        nav = QHBoxLayout()
        btn_prev = QPushButton("<< Settings")
        btn_prev.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_SETTINGS))
        nav.addWidget(btn_prev)
        btn_def = QPushButton("Load Defaults")
        btn_def.clicked.connect(self._load_default_params)
        nav.addWidget(btn_def)
        nav.addStretch(1)
        btn_next = QPushButton("Next >>")
        btn_next.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_OUTPUT_PERIOD))
        nav.addWidget(btn_next)
        outer.addLayout(nav)
        return page

    def _build_output_period_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)
        g_types = QGroupBox("Output temporal resolution")
        tv = QVBoxLayout(g_types)
        self.chk_monthly_out = QCheckBox("Monthly — one file per month")
        self.chk_monthly_out.setChecked(True)
        self.chk_seasonal_out = QCheckBox("Seasonal — wet + dry")
        self.chk_seasonal_out.setChecked(True)
        self.chk_annual_out = QCheckBox("Annual — single file (mm/year)")
        self.chk_annual_out.setChecked(True)
        tv.addWidget(self.chk_monthly_out)
        tv.addWidget(self.chk_seasonal_out)
        tv.addWidget(self.chk_annual_out)
        outer.addWidget(g_types)

        g_stat = QGroupBox("Seasonal statistic")
        sl = QVBoxLayout(g_stat)
        self.radio_season_mean = QRadioButton("Mean — average mm per month across the season")
        self.radio_season_mean.setChecked(True)
        self.radio_season_sum = QRadioButton("Sum — total mm accumulated over the season")
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
            "color:#333; background:#f5f5f5; padding:6px; border-radius:3px;")
        outer.addWidget(self.lbl_season_preview)

        self.combo_preset.currentIndexChanged.connect(self._on_preset_changed)
        for c in (self.combo_wet_start, self.combo_wet_end,
                  self.combo_dry_start, self.combo_dry_end):
            c.currentIndexChanged.connect(self._on_season_changed)
        self._apply_preset(1)

        outer.addStretch(1)
        nav = QHBoxLayout()
        btn_prev = QPushButton("<< Parameters")
        btn_prev.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_PARAMS))
        nav.addWidget(btn_prev)
        nav.addStretch(1)
        btn_next = QPushButton("Next >>")
        btn_next.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_RUN))
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
        stat = ("Mean (mm/month avg)" if self.radio_season_mean.isChecked()
                else "Sum (total mm)")
        txt = (f"<b>Wet season</b> ({len(wet)} months): {', '.join(wn)}<br>"
               f"<b>Dry season</b> ({len(dry)} months): {', '.join(dn)}<br>"
               f"<b>Seasonal statistic:</b> {stat}")
        if overlap:
            on = [MONTH_NAMES[m - 1] for m in overlap]
            txt += (f"<br><span style='color:#b00;'><b>Warning:</b> "
                    f"{', '.join(on)} appear in BOTH seasons.</span>")
        self.lbl_season_preview.setText(txt)

    def _build_run_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)
        row = QHBoxLayout()
        lbl = QLabel("Output Directory:")
        lbl.setFont(QFont("", 9, QFont.Bold))
        row.addWidget(lbl)
        self.pick_output_dir = PathPicker("dir")
        row.addWidget(self.pick_output_dir, 1)
        outer.addLayout(row)
        self.chk_add_irrig = QCheckBox("Add irrigated water to rainfall from irrigation folder.")
        outer.addWidget(self.chk_add_irrig)
        self.chk_frac_from_dir = QCheckBox("Read fractions from provided Fractions folder.")
        outer.addWidget(self.chk_frac_from_dir)
        sim_row = QHBoxLayout()
        self.chk_sim_file = QCheckBox("Create a simulation file for calibration with the provided name:")
        self.chk_sim_file.setChecked(True)
        sim_row.addWidget(self.chk_sim_file)
        self.txt_sim_filename = QLineEdit("Simulated.tbl")
        self.txt_sim_filename.setFixedWidth(160)
        sim_row.addWidget(self.txt_sim_filename)
        sim_row.addStretch(1)
        outer.addLayout(sim_row)
        self.chk_snow = QCheckBox("Include snow processes in the modelling")
        self.chk_snow.toggled.connect(self._on_snow_toggle)
        outer.addWidget(self.chk_snow)
        # Initialise the dependent fields (snow parameters on the
        # Parameters tab) to match the checkbox state.
        self._on_snow_toggle(self.chk_snow.isChecked())

        g_compat = QGroupBox("Model version")
        cv = QVBoxLayout(g_compat)
        self.chk_steady_state = QCheckBox(
            "Wetspass-M 2016 compatibility mode")
        self.chk_steady_state.setToolTip(
            "Reproduces the original Wetspass-M 2016 (steady-state) physics:\n"
            "  • soil moisture reset to zero every month (ΔS ≡ 0)\n"
            "  • soil-derived ET capped at rainfall (not P − runoff)\n"
            "  • groundwater-fed ET folded into Cell_evapotranspiration\n"
            "  • wb_error uses ET adjusted by the recharge correction\n\n"
            "Use this mode to validate against a legacy Wetspass-M 2016 "
            "run.\n\n"
            "Leave unchecked for the improved dynamic bucket (recommended).")
        self.chk_steady_state.toggled.connect(self._on_steady_state_toggled)
        cv.addWidget(self.chk_steady_state)
        cnote = QLabel(
            "<i>Compatibility mode disables the soil bucket, ignores "
            "soil-moisture state files, and runs a single pass. The Water "
            "Balance tab will report ΔS = 0.</i>")
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
        self.spin_spin_up.setValue(2)
        self.spin_spin_up.setToolTip(
            "Extra full-year passes run BEFORE the reported run, so the "
            "soil-water bucket reaches a repeating seasonal cycle.\n\n"
            "Recommended by climate:\n"
            "  • Maritime temperate (Belgium, UK, PNW):  1\n"
            "  • Continental / Mediterranean:            2\n"
            "  • Monsoonal (Ethiopia, India, W. Africa): 2–3\n"
            "  • Arid / semi-arid with multi-year memory: 3–5\n\n"
            "Convergence is verified automatically after each cycle; "
            "see spinup_convergence.csv in the output folder.")
        surow.addWidget(self.spin_spin_up)
        surow.addWidget(QLabel("(recommended: 2 for most climates)"))
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
            "<i>Convergence metric: mean |ΔS| across the grid, compared "
            "against max(1 mm, 0.5 % of mean annual P). See the About tab "
            "for the full description.</i>")
        note2.setWordWrap(True); note2.setStyleSheet("color:#555;")
        sv.addWidget(note2)
        outer.addWidget(g_soil)

        g_lai = QGroupBox("Leaf Area Index (LAI) Mode")
        lv = QVBoxLayout(g_lai)
        self.radio_lai_landuse = QRadioButton("Automatically from Landuse Map lookups")
        self.radio_lai_landuse.setChecked(True)
        self.radio_lai_inputs = QRadioButton("Read LAI from 'Inputs'")
        lv.addWidget(self.radio_lai_landuse)
        lv.addWidget(self.radio_lai_inputs)
        self.chk_lai_per_step = QCheckBox("Read LAI per step from LAI folder")
        lv.addWidget(self.chk_lai_per_step)
        lai_row = QHBoxLayout()
        lai_row.addWidget(QLabel("LAI map:"))
        self.pick_lai_map = PathPicker("file", "GeoTIFF (*.tif *.tiff);;ASCII Grid (*.asc)")
        lai_row.addWidget(self.pick_lai_map, 1)
        lv.addLayout(lai_row)
        outer.addWidget(g_lai)
        self.radio_lai_inputs.toggled.connect(self._on_lai_mode)
        self.chk_lai_per_step.toggled.connect(self._on_lai_mode)
        self._on_lai_mode()

        self.checkpoint_banner = QGroupBox("Paused run detected")
        self.checkpoint_banner.setStyleSheet(
            "QGroupBox { background: #fff7e0; border: 1px solid #d6a800; "
            "border-radius: 4px; margin-top: 8px; padding: 8px; } "
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
        btn_prev.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_OUTPUT_PERIOD))
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

    def _build_progress_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setFont(QFont("", 10, QFont.Bold))
        outer.addWidget(self.lbl_status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p% — %v of %m steps")
        outer.addWidget(self.progress_bar)
        outer.addWidget(QLabel("Log:"))
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(QFont("Courier", 8))
        outer.addWidget(self.txt_log, 1)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
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

    def _build_results_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.addWidget(QLabel("Tick the check box next to each item of interest in model outputs."))
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

    def _build_water_balance_tab(self):
        from .water_balance_tab import WaterBalanceTab
        self.water_balance_tab = WaterBalanceTab(
            get_working_dir=lambda: self.pick_working_dir.text().strip(),
            get_output_dir=lambda: self.pick_output_dir.text().strip())
        return self.water_balance_tab

    def _build_focused_tab(self):
        from .focused_tab import FocusedRechargeTab
        self.focused_tab = FocusedRechargeTab(
            get_working_dir=lambda: self.pick_working_dir.text().strip(),
            get_output_dir=lambda: self.pick_output_dir.text().strip(),
            get_time_range=lambda: (self.spin_start_step.value(),
                                     self.spin_end_step.value()))
        return self.focused_tab

    # ------------------------------------------------------------------
    # About tab
    # ------------------------------------------------------------------
    def _build_about_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16)
        html = """
        <h2>WetSpass-M Modern</h2>
        <p><b>Version 2.0.0</b> — spatially distributed monthly water
        balance model for any climate, land-use or soil type.</p>

        <h3>Scientific basis</h3>

        <h4>Is the model steady-state?</h4>
        <p>The original <b>Wetspass-M 2016</b> (Abdollahi, Bashir &amp;
        Batelaan) is a <b>quasi-steady-state</b> monthly water balance
        model. It assumes soil-water storage is in balance within each
        monthly step, so ΔS ≡ 0, and soil moisture is reset to zero at
        the start of every month. This is documented in Batelaan &amp;
        De Smedt (2001) and Batelaan &amp; Woldeamlak (2004), and is a
        defensible simplification for maritime temperate climates — the
        model's original Belgian study region — where monthly rainfall is
        fairly evenly distributed and the root-zone reservoir changes
        little from month to month.</p>

        <p><b>The default configuration of this version is not
        quasi-steady-state at the monthly scale.</b> It is a dynamic
        (state-tracking) monthly water balance model: soil moisture is
        carried as an explicit state variable from one month to the next,
        and monthly ΔS is a real, non-zero flux. At the annual scale —
        after spin-up — the model converges to steady state, so the annual
        average ΔS ≈ 0 while the monthly values are non-zero. This
        preserves the seasonal redistribution of water that a steady-state
        formulation cannot represent.</p>

        <h4>Why the dynamic bucket matters</h4>
        <p>In seasonally-concentrated rainfall regimes (monsoonal,
        Mediterranean, semi-arid, arid), the root-zone soil-moisture
        reservoir typically swings by 40–150 mm within a single year.
        Setting ΔS = 0 forces all wet-season excess into immediate runoff
        or recharge and starves dry-season evapotranspiration. The
        consequences are:</p>
        <ul>
          <li>seasonal recharge concentrated entirely into the wet months,
          producing a physically unrealistic spike;</li>
          <li>dry-season actual evapotranspiration underestimated because
          no stored water is available;</li>
          <li>catchment-scale closure error inflated when the model tries
          to compensate by folding the residual back into AET.</li>
        </ul>
        <p>Numerical experiments show the steady-state approximation biases
        annual recharge by 5–15 % and misplaces 30–60 % of the seasonal
        distribution in seasonally-concentrated rainfall regimes. The
        bucket restores the physical memory of the soil column.</p>

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
Storage-fed ET        Q_gw  = ET_gw_transp + ET_gw_evapo + ET_open_water
        </pre>
        <p><b>Mass balance (dynamic mode):</b></p>
        <pre>P = AET + SR + R + ΔS,     where  ΔS = S(t+1) − S(t)</pre>
        <p>Numerical closure error is zero at every cell and every month
        by construction. The ΔS term is the physically meaningful residual
        that the original steady-state formulation set to zero.</p>

        <h4>Interpreting the closure error</h4>
        <p>Numerical closure error is the residual that remains after
        summing all fluxes and storage changes in the annual balance:</p>
        <pre>closure = P − AET − SR − R_total − ΔS</pre>
        <p>It is not exactly zero in practice because of:</p>
        <ul>
          <li><b>Float32 round-off</b> in the GeoTIFF output rasters;</li>
          <li><b>Block-wise aggregation</b> — each 512×512 block
          accumulates its own round-off independently;</li>
          <li><b>Valid-cell mask differences</b> between rasters, from
          cells that are valid in some months and nodata in others.</li>
        </ul>
        <p>Hydrological modelling convention accepts closure errors up to
        the following thresholds, expressed as a fraction of mean annual
        precipitation:</p>
        <table cellpadding="4" cellspacing="0" border="0">
          <tr>
            <td><b>&lt; 1 % of P</b></td>
            <td>excellent — publication-quality</td>
            <td>e.g. &lt; 12 mm/yr on P = 1170 mm/yr</td>
          </tr>
          <tr>
            <td><b>1–5 % of P</b></td>
            <td>acceptable with caveats</td>
            <td>e.g. 12–59 mm/yr on P = 1170 mm/yr</td>
          </tr>
          <tr>
            <td><b>&gt; 5 % of P</b></td>
            <td>needs investigation</td>
            <td>e.g. &gt; 59 mm/yr on P = 1170 mm/yr</td>
          </tr>
        </table>
        <p>For a typical monsoonal or semi-arid catchment, a residual in
        the 1–5 mm/yr range is common and simply reflects the numerical
        floor of the computation. Values above 5 % of P usually indicate
        one of the following:</p>
        <ol>
          <li>Input rasters misaligned to the DEM grid — run <b>Check
          Inputs</b>;</li>
          <li>The soil bucket not yet converged — check
          <code>spinup_convergence.csv</code>;</li>
          <li>Parameters (LP, Alfa, LAI, rooting depth) inconsistent with
          local conditions, producing ET + SR &gt; P on many cells;</li>
          <li>Stale output rasters from a previous run with different
          inputs — clear the output folder and re-run.</li>
        </ol>
        <p>When reporting results, quote the closure error explicitly in
        the methods section, e.g.: <i>"The annual water balance closed to
        within 0.2 % of precipitation, consistent with numerical
        round-off."</i> Reviewers expect to see this.</p>

        <h4>Other improvements relative to Wetspass-M 2016</h4>
        <ol>
          <li><b>ET capped at (P − SR), not P.</b> The original allowed
          ET + runoff to exceed rainfall on high-runoff cells, driving
          recharge negative and then clamped to zero, which produced a
          large apparent closure error at the catchment scale. The cap is
          now the physically available water after runoff.</li>
          <li><b>Groundwater-fed ET reported separately.</b> Transpiration
          and evaporation from a shallow water table are physical, but
          drawn from storage, not precipitation. The original folded them
          into AET, double-counting them in the closure test. This version
          writes them as <code>Cell_gw_discharge</code> outside the P
          balance.</li>
          <li><b>Nodata-preserving aggregation.</b> Annual rasters now
          share the same valid-cell mask as the input rainfall, so the
          Water Balance tab compares like with like.</li>
        </ol>

        <h3>Spin-up</h3>

        <h4>Why spin-up is needed</h4>
        <p>Because soil moisture is now a state, the same 12 monthly input
        maps must be run through repeatedly until the bucket reaches a
        repeating annual cycle — that is, until the soil moisture at the
        start of a year equals the soil moisture at the end of the same
        year, at every grid cell. Only then does the reported year have a
        well-defined mass balance and a ΔS that can be compared with P.</p>

        <h4>Recommended starting point</h4>
        <table cellpadding="4" cellspacing="0" border="0">
          <tr><td><b>Maritime temperate</b></td>
              <td>(Belgium, UK, Pacific NW)</td><td><b>1</b></td></tr>
          <tr><td><b>Continental / Mediterranean</b></td>
              <td>(Central Europe, Chile, California)</td><td><b>2</b></td></tr>
          <tr><td><b>Monsoonal</b></td>
              <td>(Ethiopia, India, West Africa)</td><td><b>2–3</b></td></tr>
          <tr><td><b>Arid / semi-arid</b></td>
              <td>(Sahel, Arabian Peninsula, Central Australia)</td>
              <td><b>3–5</b></td></tr>
        </table>
        <p>The spinner on the Run tab sets the <b>number of extra warm-up
        passes</b> run before the reported pass. Set to 2 (default),
        the model runs three passes in total.</p>

        <h4>How to know when spin-up has converged</h4>
        <p>You do not have to guess. After every spin-up cycle the model
        computes the mean absolute change in soil moisture across the
        whole grid:</p>
        <pre>mean |S_end − S_start|     [mm/yr]</pre>
        <p>If this falls below a tolerance, the bucket has converged and
        the remaining warm-up cycles are <b>skipped automatically</b>.
        The default tolerance is:</p>
        <pre>tolerance = max(1.0 mm, 0.5% × mean annual P)</pre>
        <p>For a catchment with mean annual P = 1170 mm, that is about
        5.9 mm/yr. You can tighten this by editing
        <code>convergence_tol_frac</code> in the workflow constructor
        (for example 0.001 for a 0.1 % tolerance).</p>
        <p>You will see the convergence status in the Progress log:</p>
        <pre>
Spin-up tolerance: 5.86 mm/yr (0.50% of mean P = 1171.7 mm)
Spin-up cycle 1/3...
  mean |ΔS| = 47.210 mm (4.031% of P), mean ΔS = 41.305 mm — not converged
Spin-up cycle 2/3...
  mean |ΔS| = 8.417 mm (0.718% of P), mean ΔS = 7.442 mm — not converged
Spin-up cycle 3/3...
  mean |ΔS| = 1.209 mm (0.103% of P), mean ΔS = 0.918 mm — CONVERGED
Early exit: bucket converged after 3 spin-up cycle(s). Skipping remaining warm-ups.
Final cycle — writing outputs...
        </pre>
        <p>In this example, three warm-up cycles were needed. If you had
        set the spinner to 5, the workflow would still have exited after
        cycle 3 — extra spinner values are cheap because unused cycles are
        never executed.</p>

        <h4>Reading <code>spinup_convergence.csv</code></h4>
        <p>Every run writes a permanent record of the convergence history
        to <code>outputs/spinup_convergence.csv</code>. Example:</p>
        <pre>
# Spin-up convergence report
# mean_annual_P_mm,1171.6800
# tolerance_mm,5.8584
# tolerance_frac,0.005000
# converged_after_cycle,3

cycle,mean_abs_dS_mm,mean_dS_mm,pct_of_P,tol_mm,converged
1,47.210314,41.305442,4.031023,5.858400,no
2,8.417295,7.441808,0.718202,5.858400,no
3,1.208741,0.918376,0.103163,5.858400,yes
        </pre>
        <p>Interpretation:</p>
        <ul>
          <li><b>cycle</b> — the spin-up pass number (1-based).</li>
          <li><b>mean_abs_dS_mm</b> — the convergence metric for that pass.</li>
          <li><b>mean_dS_mm</b> — signed mean ΔS (positive = bucket filling
          on average, negative = draining).</li>
          <li><b>pct_of_P</b> — the convergence metric as a percentage of
          mean annual precipitation.</li>
          <li><b>converged</b> — yes if the metric fell below the tolerance
          at the end of that cycle.</li>
        </ul>

        <h4>Do you need to increase the spin-up spinner?</h4>
        <p>No, in most cases. The workflow exits early when the bucket is
        stable, so an over-generous spinner value costs nothing. The only
        situation where you should raise the spinner is if the CSV shows
        <code>converged_after_cycle</code> equal to the spinner value — that
        means the bucket was still drifting when the last allowed warm-up
        finished, and the final cycle will inherit a residual ΔS. In that
        case add 1 or 2 cycles and re-run.</p>
        <p>Conversely, if the CSV shows convergence at cycle 1 or 2 and
        you had set the spinner to 5, you can safely lower it to the
        reported value for clarity in future runs — though it makes no
        numerical difference.</p>

        <h4>Manual verification</h4>
        <p>Two independent checks confirm the bucket has converged:</p>
        <ol>
          <li>Open <b>Water Balance</b> and confirm that <b>ΔS</b> (soil
          storage change) is close to zero — within the tolerance reported
          in the Progress log.</li>
          <li>Open <code>spinup_convergence.csv</code> and confirm the last
          row has <code>converged = yes</code>.</li>
        </ol>
        <p>If both checks pass, the model has reached a repeating annual
        cycle and the reported mass balance is trustworthy.</p>

        <h4>Wetspass-M 2016 compatibility mode</h4>
        <p>Ticking the <b>compatibility mode</b> checkbox on the Run tab
        restores the original 2016 physics exactly:</p>
        <ul>
          <li>soil moisture reset to zero every month (ΔS ≡ 0);</li>
          <li>soil-derived ET capped at rainfall, not (P − runoff);</li>
          <li>groundwater-fed ET folded back into
          <code>Cell_evapotranspiration</code>;</li>
          <li>the recharge-driven ET adjustment reapplied, so
          <code>wb_error</code> matches the original.</li>
        </ul>
        <p>Compatibility mode disables the bucket, ignores any soil-state
        file, and runs a single pass. Use it to validate the modernized
        code against a legacy Wetspass-M 2016 installation on identical
        inputs. Cell-by-cell differences should be at the level of
        floating-point round-off.</p>

        <h4>Surface water interaction</h4>
        <p>When enabled on the <b>Parameters</b> tab, the model
        aggregates the per-pixel fluxes into a catchment-scale monthly
        discharge time series using a simple linear routing scheme:</p>
        <pre>
Q_surface(m) = (q0_surface · x) + 1000 · (1 − x) · area_km² · mean(SR_m)
Q_base(m)    = (q0_base    · β) + 1000 · (1 − β) · contribution · area_km² · mean(R_m)
        </pre>
        <p>The results are written as two extra columns in
        <code>Simulated.tbl</code>: <code>Qsurf[m³/mnt]</code> and
        <code>Qb[m³/mnt]</code>. They do not affect any of the
        per-pixel rasters.</p>
        <p>Check the box when you have observed monthly streamflow to
        compare against, or when you need to report catchment yield or
        baseflow separately. Leave it off when you only need the spatial
        recharge and ET maps.</p>
        <ul>
          <li><b>Basin area</b> — total catchment area in km².</li>
          <li><b>x coefficient</b> — fraction of surface discharge
          carried over from the previous month (attenuation).</li>
          <li><b>Beta coefficient</b> — same role for baseflow.</li>
          <li><b>Contribution factor</b> — dimensionless recharge-to-
          baseflow partition.</li>
          <li><b>Q0</b> on the Inputs tab — initial discharges carried
          into the first simulated month.</li>
        </ul>

        <h4>Universal applicability</h4>
        <p>The bucket physics are climate-independent and no parameter
        assumes a particular region. The spin-up count is the only
        climate-sensitive knob, and it is user-configurable and
        self-verifying.</p>

        <h4>Key references</h4>
        <ul>
          <li>Abdollahi, K., Bashir, I., &amp; Batelaan, O. (2016).
          <i>WetSpass-M: spatially distributed monthly water balance
          model</i>. Vrije Universiteit Brussel.</li>
          <li>Batelaan, O., &amp; De Smedt, F. (2001). WetSpass: a
          flexible, GIS-based, distributed recharge methodology for
          regional groundwater modelling. <i>IAHS Publication</i>, 269,
          11–18.</li>
          <li>Batelaan, O., &amp; Woldeamlak, S. T. (2004).
          <i>WetSpass-M documentation</i>. Vrije Universiteit Brussel.</li>
          <li>Arnold, J. G., Srinivasan, R., Muttiah, R. S., &amp;
          Williams, J. R. (1998). Large-area hydrologic modeling and
          assessment: Part I. Model development. <i>Journal of the
          American Water Resources Association</i>, 34(1), 73–89.
          [SWAT soil-water bucket]</li>
          <li>Bergström, S. (1992). <i>The HBV model — its structure and
          applications</i>. SMHI Reports Hydrology, No. 4.
          [HBV soil-moisture routine]</li>
        </ul>

        <h3>Original authors (2016)</h3>
        <p>K. Abdollahi, I. Bashir, O. Batelaan — Vrije Universiteit Brussel</p>

        <h3>Modernization (2026)</h3>
        <p>M. G. Gurmu &lt;mggurmu@gmail.com&gt;</p>
        """
        label = QLabel(html)
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        outer.addWidget(label)
        outer.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # Help tab
    # ------------------------------------------------------------------
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
        title.setFont(QFont("", 14, QFont.Bold))
        outer.addWidget(title)

        intro = QLabel(
            "This tab provides quick reference material and gives access to "
            "the full PDF user guide. If you are new to the model, read the "
            "<b>About</b> tab first for the scientific background, then "
            "follow the workflow below.")
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
          <li>Fill the <b>Inputs</b> tab — working directory, DEM, land use,
              soil, slope. Click <b>Auto-load</b> if your inputs are laid
              out in the standard folder structure.</li>
          <li>Open <b>Check Inputs</b> and confirm that every table and
              raster is green. Fix any red rows before continuing.</li>
          <li>Review the <b>Parameters</b> tab. The defaults match
              Wetspass-M 2016 (LP = 0.85, Alfa = 1.5). Only change them if
              you have local calibration evidence.</li>
          <li>Choose output resolution on <b>Output Period</b>.</li>
          <li>On <b>Run</b>: set the output directory, leave
              <b>Spin-up cycles = 2</b> unless you have a reason to change
              it, and leave <b>compatibility mode OFF</b> for the improved
              dynamic bucket.</li>
          <li>Press <b>Run</b>. Watch the <b>Progress</b> tab. The
              workflow will report convergence and exit spin-up early when
              the bucket is stable.</li>
          <li>Open the <b>Water Balance</b> tab and click
              <b>Load from output folder</b>. Check that the closure error
              is close to zero.</li>
          <li>If you need focused (streambed) recharge, go to the
              <b>Focused Recharge</b> tab, run it, then click
              <b>Combine diffusive + focused → Total</b>, then return to
              <b>Water Balance</b> and click <b>Update recharge</b>.</li>
        </ol>

        <h3>What each tab does</h3>
        <table cellpadding="4" cellspacing="0" border="0">
          <tr><td><b>Inputs</b></td>
              <td>Path to the working directory and the primary rasters.</td></tr>
          <tr><td><b>Check Inputs</b></td>
              <td>Verifies every required file and its grid alignment.</td></tr>
          <tr><td><b>Settings</b></td>
              <td>File name prefixes and time step configuration.</td></tr>
          <tr><td><b>Parameters</b></td>
              <td>Global and local calibration coefficients.</td></tr>
          <tr><td><b>Output Period</b></td>
              <td>Monthly / seasonal / annual aggregation settings.</td></tr>
          <tr><td><b>Run</b></td>
              <td>Output directory, spin-up, compatibility mode, start.</td></tr>
          <tr><td><b>Progress</b></td>
              <td>Live log and progress bar. Cancel / pause controls.</td></tr>
          <tr><td><b>Results</b></td>
              <td>Which rasters to keep as output.</td></tr>
          <tr><td><b>Water Balance</b></td>
              <td>Annual closure check with the ΔS term.</td></tr>
          <tr><td><b>Focused Recharge</b></td>
              <td>Streambed infiltration along the DEM network.</td></tr>
          <tr><td><b>About</b></td>
              <td>Scientific justification, equations, references.</td></tr>
          <tr><td><b>Help</b></td>
              <td>This tab — quick reference and PDF manual.</td></tr>
        </table>

        <h3>Where outputs go</h3>
        <p>All output rasters are written to the folder selected on the
        <b>Run</b> tab, in the pattern:</p>
        <pre>
&lt;output&gt;/
├── Lookups/                          static lookup rasters
├── Interception_1.tif ... 12.tif     monthly outputs
├── Cell_runoff_1.tif  ... 12.tif
├── Cell_evapotranspiration_1.tif ...
├── Recharge_diffusive_1.tif ...
├── Recharge_diffusive_annual.tif     annual aggregate
├── Recharge_focused_annual.tif       (after Focused Recharge tab)
├── Recharge_Total_annual.tif         (after Combine button)
├── soilwater_storage_initial.tif     bucket state for next run
├── soilwater_storage_delta_annual.tif
├── spinup_convergence.csv            spin-up convergence report
└── Simulated.tbl                     per-month mean fluxes
        </pre>

        <h3>Common errors and fixes</h3>
        <table cellpadding="4" cellspacing="0" border="0">
          <tr><td><b>"Input rasters are not on the same grid as the DEM"</b></td>
              <td>Use QGIS → Raster → Projections → Warp to reproject every
              input to the DEM's CRS, extent, and resolution. Use
              bilinear for climate data and nearest for land use / soil.</td></tr>
          <tr><td><b>"Missing Cell_runoff for month N"</b> when running
              Focused Recharge</td>
              <td>Run the main model first — the Focused Recharge module
              needs the monthly runoff rasters as input.</td></tr>
          <tr><td><b>Large closure error in Water Balance</b></td>
              <td>Check that ΔS is small in the panel. If it is not, the
              bucket has not converged — increase spin-up cycles by 1 and
              re-run. If ΔS is small but the error is still large,
              check that all inputs are aligned to the DEM.</td></tr>
          <tr><td><b>Recharge raster is all zeros</b></td>
              <td>Confirm the run completed (check for "Model run
              complete"). If yes, check ET in the Water Balance panel —
              if AET ≈ P, ET is consuming all available water and
              recharge is genuinely zero. Adjust calibration.</td></tr>
          <tr><td><b>"Checkpoint parameters do not match"</b> on Resume</td>
              <td>A parameter, spin-up count, or compatibility setting was
              changed after pausing. Click <b>Discard checkpoint</b> on
              the Run tab and start a fresh run.</td></tr>
          <tr><td><b>Model appears stuck on a spin-up cycle</b></td>
              <td>It is not stuck. Each cycle runs 12 months. Watch the
              "Month N - block M" lines in the Progress log. The
              workflow exits early when convergence is reached — check
              the log for the "CONVERGED" message.</td></tr>
          <tr><td><b>"[Focused Recharge] No Cell_runoff_*.tif found"</b></td>
              <td>You clicked the Focused Recharge tab before the main
              model finished. This is an informational hint, not an
              error. It is suppressed while a run is in progress and
              only shown once per session.</td></tr>
          <tr><td><b>Closing QGIS during a run</b></td>
              <td>A confirmation dialog appears. Answer <b>No</b> to keep
              the run going, or <b>Yes</b> to cancel and close. To keep a
              run for later, use <b>Pause run</b> on the Run tab instead
              of closing.</td></tr>
        </table>

        <h3>Tips</h3>
        <ul>
          <li>The PDF user guide (button above) contains the full
              reference — input file formats, lookup table definitions,
              calibration guidance, and worked examples.</li>
          <li>The <b>About</b> tab documents the physics, the change from
              quasi-steady-state to dynamic bucket, and how to verify
              spin-up convergence.</li>
          <li>You can reproduce the original Wetspass-M 2016 output
              exactly by ticking <b>Wetspass-M 2016 compatibility mode</b>
              on the Run tab.</li>
          <li>All rasters are GeoTIFF with LZW compression. QGIS reads
              them directly.</li>
        </ul>
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
        import subprocess, sys
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p.parent)])
        except Exception as exc:
            QMessageBox.warning(self, "Show in folder",
                                f"Could not open folder: {exc}")

    def _show_user_guide_path(self):
        p = self._user_guide_path()
        QMessageBox.information(
            self, "User Guide path",
            f"Expected location:\n{p}\n\n"
            f"Exists: {'yes' if p.exists() else 'no'}\n\n"
            f"Place '{USER_GUIDE_FILENAME}' in the plugin's "
            f"'help/' folder to enable the Open button.")

    # =================================================================
    # Defaults / event handlers
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

    def _on_steady_state_toggled(self, checked):
        if checked:
            self.spin_spin_up.setEnabled(False)
            self.spin_spin_up.setValue(0)
            self.chk_save_soil_state.setEnabled(False)
            self.chk_save_soil_state.setChecked(False)
        else:
            self.spin_spin_up.setEnabled(True)
            self.spin_spin_up.setValue(2)
            self.chk_save_soil_state.setEnabled(True)
            self.chk_save_soil_state.setChecked(True)

    def _reset_soil_state(self):
        out_dir = Path(self.pick_output_dir.text().strip() or "")
        if not out_dir.is_dir():
            QMessageBox.warning(self, "Reset", "Output directory not set.")
            return
        state = out_dir / "soilwater_storage_initial.tif"
        if state.exists():
            if QMessageBox.question(
                    self, "Reset soil moisture",
                    f"Delete {state.name}?\n\nThe next run will start from "
                    f"a dry soil profile.",
                    QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
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

    # -----------------------------------------------------------------
    # Tab-changed handler (fixed: no noise during an active run)
    # -----------------------------------------------------------------
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
            QMessageBox.warning(self, "Resume", "No checkpoint was found.")
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
                    "mean" if self.radio_season_mean.isChecked() else "sum"),
                spin_up_cycles=self.spin_spin_up.value(),
                save_soil_state=self.chk_save_soil_state.isChecked())
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
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            WetSpassWorkflow.discard_checkpoint(out_dir)
        except Exception:
            pass
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
        if maps_dir is None:
            return
        files = list(maps_dir.glob("*.tif")) + list(maps_dir.glob("*.asc"))

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
        out_dir = wd_path / "outputs"
        if not self.pick_output_dir.text():
            self.pick_output_dir.setText(str(out_dir))
        try:
            self._run_check(silent=True)
        except Exception:
            pass

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
            import traceback
            self.txt_log.append("[Check Inputs] Exception:\n"
                                + traceback.format_exc())
            if not silent:
                QMessageBox.critical(self, "Check Inputs", f"Failed: {exc}")
            return
        self.tree_status.clear()
        hdr = QTreeWidgetItem(["Search paths", "", ""])
        hdr.addChild(QTreeWidgetItem(["Working dir", "", report["working_dir"]]))
        hdr.addChild(QTreeWidgetItem(["Layout used", "", report["layout"]]))
        hdr.addChild(QTreeWidgetItem(["Maps dir", "", report["maps_dir"]]))
        hdr.addChild(QTreeWidgetItem(["Tables dir", "", report["tables_dir"]]))
        hdr.setExpanded(True)
        self.tree_status.addTopLevelItem(hdr)
        t_root = QTreeWidgetItem(["Lookup tables", "", ""])
        self.tree_status.addTopLevelItem(t_root)
        for entry in report["tables"]:
            icon = "✔" if entry["ok"] else "✘"
            color = QColor(0, 140, 0) if entry["ok"] else QColor(190, 0, 0)
            item = QTreeWidgetItem([entry["name"], icon, entry["message"]])
            item.setForeground(1, color)
            item.setToolTip(2, entry.get("path", ""))
            t_root.addChild(item)
        t_root.setExpanded(True)
        f_root = QTreeWidgetItem(["Input rasters", "", ""])
        self.tree_status.addTopLevelItem(f_root)
        for entry in report["folders"]:
            icon = "✔" if entry["ok"] else "✘"
            color = QColor(0, 140, 0) if entry["ok"] else QColor(190, 0, 0)
            item = QTreeWidgetItem([entry["name"], icon, entry["message"]])
            item.setForeground(1, color)
            f_root.addChild(item)
        f_root.setExpanded(True)
        gc = report.get("grid_consistency") or []
        g_root = QTreeWidgetItem(["Grid consistency", "", ""])
        self.tree_status.addTopLevelItem(g_root)
        if not gc:
            g_root.addChild(QTreeWidgetItem(["(not checked)", "?", "No data"]))
        else:
            for entry in gc:
                icon = "✔" if entry["ok"] else "✘"
                color = (QColor(0, 140, 0) if entry["ok"]
                         else QColor(200, 120, 0))
                item = QTreeWidgetItem([entry["name"], icon, entry["message"]])
                item.setForeground(1, color)
                g_root.addChild(item)
        g_root.setExpanded(True)
        n_t_ok = sum(1 for e in report["tables"] if e["ok"])
        n_t = len(report["tables"])
        n_f_ok = sum(1 for e in report["folders"] if e["ok"])
        n_f = len(report["folders"])
        self.txt_log.append(
            f"[Check Inputs] layout={report['layout']}  "
            f"{n_t_ok}/{n_t} tables, {n_f_ok}/{n_f} rasters")
        if report["overall_ok"]:
            self.status_label.setText("Input check passed.")
            self.status_progress.setValue(100)
        else:
            self.status_label.setText(
                f"Input check: {n_t_ok}/{n_t} tables, "
                f"{n_f_ok}/{n_f} rasters OK")
            self.status_progress.setValue(0)
        if not silent:
            self.tabs.setCurrentIndex(TAB_CHECK)

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
        keep = {k for k, cb in self.output_checkboxes.items() if cb.isChecked()}
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
            errors.append("Slope map is empty (or check 'Create from DEM').")
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
            save_soil_state=self.chk_save_soil_state.isChecked())
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._on_log)
        self.worker.finished_ok.connect(self._on_finished_ok)
        self.worker.finished_error.connect(self._on_finished_error)
        self.worker.paused.connect(self._on_paused)
        self.worker.cancelled.connect(self._on_cancelled)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p% — %v of %m steps")
        self.status_progress.setValue(0)
        self._pause_requested_by_user = False
        self._refresh_run_tab_state()
        self._poll_timer.start()
        self.status_label.setText("Resuming…" if resume else "Running…")
        self.lbl_status.setText("Resuming…" if resume else "Running…")
        self.tabs.setCurrentIndex(TAB_PROGRESS)
        self.txt_log.append(f"\n[Run {'resumed' if resume else 'started'}]")
        self.worker.start()

    def _pause_run(self):
        if self.worker is None or not self.worker.isRunning():
            return
        self._pause_requested_by_user = True
        self.worker.request_pause()
        self.lbl_status.setText(
            "Pause requested — will stop after current block…")
        self.btn_pause.setEnabled(False)
        self.btn_cancel.setEnabled(True)

    def _cancel_run(self):
        if self.worker is None or not self.worker.isRunning():
            return
        self.worker.cancel()
        self.lbl_status.setText(
            "Cancel requested — will stop after current block…")
        self.btn_cancel.setEnabled(False)
        self.btn_pause.setEnabled(False)

    def _on_progress(self, step, total, msg):
        if self.progress_bar.maximum() != total:
            self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(step)
        self.progress_bar.setFormat(f"%p% — step {step} of {total}")
        self.status_progress.setValue(int(100 * step / max(total, 1)))

    def _on_log(self, msg):
        self.txt_log.append(msg)
        sb = self.txt_log.verticalScrollBar()
        sb.setValue(sb.maximum())
        self.status_label.setText(msg)

    def _on_finished_ok(self, results):
        self._poll_timer.stop()
        self.worker = None
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.progress_bar.setFormat("Done (%p%)")
        self.status_progress.setValue(100)
        self.lbl_status.setText("Model run complete.")
        self.status_label.setText("Model run complete.")
        self.btn_cancel.setEnabled(False)
        self._refresh_run_tab_state()
        output_dir = Path(results.get("output_dir", ""))
        annual = output_dir / "Recharge_diffusive_annual.tif"
        if annual.exists():
            layer = QgsRasterLayer(str(annual), "Recharge Diffusive (mm/yr)")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
        try:
            if hasattr(self, "water_balance_tab"):
                self.water_balance_tab._load_from_output()
        except Exception:
            pass
        QMessageBox.information(self, "WetSpass-M Modern",
                                f"Model run complete.\n\nOutput: {output_dir}")

    def _on_paused(self, results):
        self._poll_timer.stop()
        self.worker = None
        self.progress_bar.setFormat("Paused at step %v of %m")
        self.lbl_status.setText("Run paused. Checkpoint saved.")
        self.status_label.setText("Paused — click 'Resume run' to continue.")
        self._refresh_run_tab_state()
        QMessageBox.information(self, "WetSpass-M Modern",
                                "Run paused. Click 'Resume run' on the Run tab.")

    def _on_cancelled(self, results):
        self._poll_timer.stop()
        self.worker = None
        self.progress_bar.setFormat("Cancelled at step %v of %m")
        self.lbl_status.setText("Run cancelled.")
        self.btn_cancel.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self._refresh_run_tab_state()
        QMessageBox.information(self, "WetSpass-M Modern",
                                "Run cancelled. Press 'Run' to restart.")

    def _on_finished_error(self, err):
        self._poll_timer.stop()
        self.worker = None
        self.lbl_status.setText("Failed.")
        self.status_label.setText("Model failed.")
        self.btn_cancel.setEnabled(False)
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
        elif status == "paused" or (info is not None
                                     and self._pause_requested_by_user):
            self.worker = None
            self._refresh_run_tab_state()
            self.progress_bar.setFormat("Paused at step %v of %m")
            self.btn_cancel.setEnabled(False)