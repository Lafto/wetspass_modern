# -*- coding: utf-8 -*-
"""
Water Balance tab — annual water balance visualization.

Reads:
    Interception_annual.tif             (informational; already inside AET)
    Cell_evapotranspiration_annual.tif  (total AET: interception, soil,
                                         groundwater-fed, open-water)
    Cell_runoff_annual.tif              (total surface runoff from the model)
    Recharge_diffusive_annual.tif       (diffusive recharge)
    Recharge_focused_annual.tif         (focused recharge; optional)
    Recharge_Total_annual.tif           (diffusive + focused; optional)
    soilwater_storage_delta_annual.tif  (ΔS)
    rain*.tif                           (precipitation)

Closure equation (Wetspass-M 2016 convention, reconciled)
---------------------------------------------------------
    closure = P + SW_Inflow + GW_Inflow − AET − SR_net − R_total − ΔS

where
    SR_net  = SR − R_focused          (runoff net of the focused term)
    R_total = R_diffusive + R_focused (total recharge)

Algebraically equivalent to the diffusive-engine closure:
    closure = P + SW_Inflow + GW_Inflow − AET − SR − R_diffusive − ΔS   (≡ 0)

The reconciliation is required because the focused-recharge engine
extracts its input from the surface-runoff raster. The water that
eventually becomes focused recharge was already counted in SR.
Adding R_focused to the closure without subtracting it from SR
would double-count that water, and the closure would show a
systematic negative residual equal to −R_focused.

Four reconciliation modes are auto-selected based on which rasters
are present:

    reconciled_focused : R_focused and R_total present
                         SR_net = SR − R_focused, R_eff = R_total
    total_no_focused   : R_total present but R_focused missing
                         SR_net = SR, R_eff = R_diffusive (safety)
    diffusive_only     : Only R_diffusive present
                         SR_net = SR, R_eff = R_diffusive
    no_recharge        : No recharge raster found
                         SR_net = SR, R_eff = 0
"""

import contextlib
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QRectF, QSettings
from qgis.PyQt.QtGui import QPixmap, QPainter, QColor, QGuiApplication
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QSizePolicy, QGroupBox, QFileDialog,
    QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QDoubleSpinBox, QFrame,
)

try:
    import rasterio
    from rasterio.windows import Window
    from rasterio.enums import Resampling
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False


SETTINGS_KEY_AREA = "wetspass_modern/water_balance/catchment_area_km2"


# ===========================================================================
# Recommended ranges (percent of P)
# ===========================================================================
RECOMMENDED_TERM_RANGES = {
    "I":     (1.0, 25.0,
              "Interception in semi-arid shrubland / savanna"),
    "AET":   (40.0, 90.0,
              "Total AET is the dominant sink in semi-arid climates"),
    "SR":    (5.0, 25.0,
              "Surface runoff coefficient for semi-arid catchments"),
    "R_tot": (5.0, 15.0,
              "Total recharge (diffusive + focused) for semi-arid"),
    "dS":    (-5.0, 5.0,
              "Annual soil-storage change; near zero after spin-up"),
}

CLOSURE_GRADES = [
    (2.0,   0.5,  "#1b7a3e", "Excellent",
     "At the numerical-noise floor of WetSpass-M."),
    (5.0,   1.5,  "#1e5fa8", "Good",
     "Accepted target for a semi-distributed water balance."),
    (10.0,  3.0,  "#b06f00", "Acceptable — document",
     "Report the residual in the methods section."),
    (20.0,  6.0,  "#c25a00", "Marginal — investigate",
     "Above the typical WetSpass-M tolerance."),
    (1e9,   1e9,  "#b83232", "Investigate",
     "Larger than expected from numerical non-closure alone."),
]


# ===========================================================================
# Help image
# ===========================================================================
HELP_DIR = Path(__file__).resolve().parent.parent / "help"


def _find_help_image() -> Optional[Path]:
    if not HELP_DIR.is_dir():
        return None
    for name in ("water_balance_background.png",
                 "water_balance.png",
                 "water_balance_background.jpg",
                 "water_balance.jpg"):
        p = HELP_DIR / name
        if p.is_file():
            return p
    for p in sorted(HELP_DIR.iterdir()):
        if p.is_file() and p.stem.lower().startswith("water_balance"):
            return p
    return None


HELP_IMAGE: Optional[Path] = _find_help_image()


# ===========================================================================
# Overlay widget — PNG base + SVG text on top
# ===========================================================================
class _OverlayLabel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(520)
        self._pixmap: Optional[QPixmap] = None
        self._svg_text_provider = None
        self._svg: Optional[QSvgRenderer] = None

    def set_pixmap(self, pm: Optional[QPixmap]):
        self._pixmap = pm
        self._rebuild_svg()
        self.update()

    def set_svg_provider(self, provider):
        self._svg_text_provider = provider
        self._rebuild_svg()
        self.update()

    def refresh_svg(self):
        self._rebuild_svg()
        self.update()

    def _rebuild_svg(self):
        if self._svg_text_provider is None:
            self._svg = None
            return
        if self._pixmap is not None and self._pixmap.height() > 0:
            aspect = self._pixmap.width() / self._pixmap.height()
        else:
            aspect = 1.0
        svg_text = self._svg_text_provider(aspect)
        self._svg = QSvgRenderer(bytes(svg_text, "utf-8"))

    def _target_rect(self):
        if self._pixmap is None:
            return None
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw <= 0 or ph <= 0:
            return None
        ww, wh = self.width(), self.height()
        scale = min(ww / pw, wh / ph)
        w, h = int(pw * scale), int(ph * scale)
        return (ww - w) // 2, (wh - h) // 2, w, h

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        rect = self._target_rect()
        if rect is None:
            painter.end()
            return
        x, y, w, h = rect
        if self._pixmap is not None:
            painter.drawPixmap(x, y, w, h, self._pixmap)
        if self._svg is not None and self._svg.isValid():
            self._svg.render(painter, QRectF(x, y, w, h))
        painter.end()


# ===========================================================================
# Background loader
# ===========================================================================
class _RasterMeanLoader(QThread):
    progress       = pyqtSignal(int, int, str)
    annual_ready   = pyqtSignal(dict)
    finished_ok    = pyqtSignal(dict)
    finished_error = pyqtSignal(str)

    # Read every rasters that could matter for the closure.
    # Some may be missing — the tab handles that downstream.
    TARGETS = [
        ("Interception", "Interception_annual.tif"),
        ("AET",          "Cell_evapotranspiration_annual.tif"),
        ("SR",           "Cell_runoff_annual.tif"),
        ("R_diff",       "Recharge_diffusive_annual.tif"),
        ("R_focused",    "Recharge_focused_annual.tif"),
        ("R_total",      "Recharge_Total_annual.tif"),
        ("dS",           "soilwater_storage_delta_annual.tif"),
    ]

    UPDATE_TARGETS = [
        ("R_diff",    "Recharge_diffusive_annual.tif"),
        ("R_focused", "Recharge_focused_annual.tif"),
        ("R_total",   "Recharge_Total_annual.tif"),
    ]

    def __init__(self, output_dir: Path, working_dir: Path,
                 block_size: int = 1024, use_overview: bool = True,
                 precip_decim: int = 16, include_precip: bool = True,
                 targets=None, parent=None):
        super().__init__(parent)
        self.output_dir     = Path(output_dir)
        self.working_dir    = Path(working_dir)
        self.block_size     = block_size
        self.use_overview   = use_overview
        self.precip_decim   = precip_decim
        self.include_precip = include_precip
        self.targets        = targets or self.TARGETS
        self._cancelled     = False

    def cancel(self):
        self._cancelled = True

    @staticmethod
    def _block_mean(path: Path, block_size: int,
                    use_overview: bool, decim_override: int = 0
                    ) -> Optional[float]:
        if not path.exists():
            return None
        with rasterio.open(str(path)) as src:
            nd = src.nodata
            decim = 1
            if decim_override and decim_override > 1:
                decim = decim_override
            elif use_overview:
                factors = src.overviews(1) or []
                if factors:
                    decim = max(f for f in factors if f <= 8) or 1

            total = 0.0
            count = 0
            for r in range(0, src.height, block_size):
                h = min(block_size, src.height - r)
                for c in range(0, src.width, block_size):
                    w = min(block_size, src.width - c)
                    kw = {"window": Window(c, r, w, h)}
                    if decim > 1:
                        oh = max(1, h // decim)
                        ow = max(1, w // decim)
                        kw["out_shape"] = (oh, ow)
                        kw["resampling"] = Resampling.average
                    arr = src.read(1, **kw).astype(np.float64)
                    if nd is not None:
                        arr = np.where(arr == nd, np.nan, arr)
                    arr = np.where(arr <= -1e30, np.nan, arr)
                    v = arr[np.isfinite(arr)]
                    if v.size:
                        total += float(v.sum())
                        count += int(v.size)
            return total / count if count else None

    def _annual_precip_mean(self) -> Optional[float]:
        wd = self.working_dir
        if not wd or not wd.is_dir():
            return None
        rain_dir = None
        for sub in ("inputs/maps/rain", "maps/rain",
                    "Inputs/maps/rain", "Maps/rain",
                    "Inputs/Maps/rain", "Inputs/maps/Rain"):
            p = wd / sub
            if p.is_dir():
                rain_dir = p
                break
        if rain_dir is None:
            print(f"[water balance] no rainfall folder found under {wd}")
            return None

        total = 0.0
        found = 0
        for m in range(1, 13):
            if self._cancelled:
                return None
            hit = False
            for ext in (".tif", ".asc"):
                p = rain_dir / f"rain{m}{ext}"
                if p.exists():
                    v = self._block_mean(
                        p, self.block_size, self.use_overview,
                        decim_override=self.precip_decim)
                    if v is not None:
                        total += v
                        found += 1
                    hit = True
                    break
            if not hit:
                print(f"[water balance] missing rain file for month {m}")
            self.progress.emit(8, 8, f"Rainfall month {m}/12 …")
        return total if found > 0 else None

    def run(self):
        try:
            values: dict = {}
            n_targets = len(self.targets)
            total_steps = n_targets + (1 if self.include_precip else 0)
            for i, (key, fname) in enumerate(self.targets, 1):
                if self._cancelled:
                    return
                self.progress.emit(i, total_steps, f"Reading {fname} …")
                path = self.output_dir / fname
                values[key] = self._block_mean(
                    path, self.block_size, self.use_overview)
            if not self._cancelled:
                self.annual_ready.emit(dict(values))
            if self.include_precip and not self._cancelled:
                self.progress.emit(n_targets + 1, total_steps,
                                   "Summing monthly rainfall …")
                values["P"] = self._annual_precip_mean()
            if not self._cancelled:
                self.finished_ok.emit(values)
        except Exception as exc:
            import traceback
            self.finished_error.emit(f"{exc}\n\n{traceback.format_exc()}")


# ===========================================================================
# The tab
# ===========================================================================
class WaterBalanceTab(QWidget):

    def __init__(self, get_working_dir: Callable,
                 get_output_dir: Callable, parent=None):
        super().__init__(parent)
        self.get_working_dir = get_working_dir
        self.get_output_dir = get_output_dir
        self.values: dict = {}
        self._area_km2_from_raster: Optional[float] = None
        self._area_user_set: bool = False
        self._loader: Optional[_RasterMeanLoader] = None
        self._pixmap_original: Optional[QPixmap] = None
        self._build_ui()
        self._load_help_image()
        self.overlay.set_svg_provider(self._overlay_svg)
        self._restore_saved_area()

    # =================================================================
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        outer.addWidget(QLabel(
            "Annual water balance. Reads <b>Recharge_Total_annual.tif</b> "
            "(diffusive + focused) and reconciles it with the surface "
            "runoff term. Focused recharge is extracted from the runoff "
            "raster, so the closure uses "
            "<b>SR_net = SR − R_focused</b>:<br><br>"
            "<code>closure = P − AET − (SR − R_focused) − R_total "
            "− ΔS</code><br><br>"
            "This is algebraically identical to the diffusive-engine "
            "closure. Interception is already inside AET and is shown "
            "for information only."))

        btn_row = QHBoxLayout()
        self.btn_load = QPushButton("Load from output folder")
        self.btn_load.clicked.connect(self._load_from_output)
        btn_row.addWidget(self.btn_load)

        self.btn_update = QPushButton("Update recharge")
        self.btn_update.setToolTip(
            "Re-read only the recharge rasters and recompute the "
            "closure. Use after Combine, without re-reading other "
            "rasters.")
        self.btn_update.clicked.connect(self._update_recharge)
        self.btn_update.setEnabled(False)
        btn_row.addWidget(self.btn_update)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_load)
        btn_row.addWidget(self.btn_cancel)

        self.btn_export = QPushButton("Export PNG…")
        self.btn_export.clicked.connect(self._export_png)
        self.btn_export.setEnabled(False)
        btn_row.addWidget(self.btn_export)

        self.btn_copy = QPushButton("Copy table to clipboard")
        self.btn_copy.clicked.connect(self._copy_table)
        self.btn_copy.setEnabled(False)
        btn_row.addWidget(self.btn_copy)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        area_row = QHBoxLayout()
        area_row.addWidget(QLabel(
            "<b>Catchment area for volumetric values:</b>"))
        self.spin_area = QDoubleSpinBox()
        self.spin_area.setRange(0.001, 1e9)
        self.spin_area.setDecimals(3)
        self.spin_area.setValue(0.0)
        self.spin_area.setSuffix(" km²")
        self.spin_area.setSingleStep(1.0)
        self.spin_area.setMinimumWidth(180)
        self.spin_area.valueChanged.connect(self._on_area_changed)
        area_row.addWidget(self.spin_area)

        self.btn_auto_area = QPushButton("Auto from raster")
        self.btn_auto_area.clicked.connect(self._auto_area_from_raster)
        area_row.addWidget(self.btn_auto_area)

        area_row.addWidget(QLabel(
            "<i>Override with the official study-boundary area.</i>"))
        area_row.addStretch(1)
        outer.addLayout(area_row)

        self.load_progress = QProgressBar()
        self.load_progress.setRange(0, 8)
        self.load_progress.setValue(0)
        self.load_progress.setVisible(False)
        outer.addWidget(self.load_progress)

        diagram_box = QGroupBox(
            "Annual water balance (mm/year, catchment average)")
        dv = QVBoxLayout(diagram_box)
        self.overlay = _OverlayLabel()
        dv.addWidget(self.overlay)
        outer.addWidget(diagram_box, 1)

        self.banner = QFrame()
        self.banner.setFrameShape(QFrame.StyledPanel)
        self.banner.setStyleSheet(
            "QFrame { background:#f0f0f0; border:1px solid #c2c7cc; "
            "border-radius:5px; padding:8px; }")
        banner_layout = QVBoxLayout(self.banner)
        self.lbl_banner_title = QLabel("Mass balance closure")
        f = self.lbl_banner_title.font()
        f.setBold(True)
        self.lbl_banner_title.setFont(f)
        self.lbl_banner_title.setWordWrap(True)
        self.lbl_banner_msg = QLabel("—")
        self.lbl_banner_msg.setWordWrap(True)
        self.lbl_banner_msg.setTextFormat(Qt.RichText)
        banner_layout.addWidget(self.lbl_banner_title)
        banner_layout.addWidget(self.lbl_banner_msg)
        outer.addWidget(self.banner)

        self.table_group = QGroupBox(
            "Values used in the diagram (mm/yr, % of P, and m³/yr)")
        tv = QVBoxLayout(self.table_group)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Term", "mm/yr", "% of P", "m³/yr",
             "Recommended range", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hdr = self.table.horizontalHeader()
        for col, mode in enumerate([
                QHeaderView.ResizeToContents,
                QHeaderView.ResizeToContents,
                QHeaderView.ResizeToContents,
                QHeaderView.ResizeToContents,
                QHeaderView.Stretch,
                QHeaderView.ResizeToContents]):
            hdr.setSectionResizeMode(col, mode)
        self.table.setMinimumHeight(280)
        tv.addWidget(self.table)

        row = QHBoxLayout()
        self.lbl_area = QLabel("Catchment area: —")
        self.lbl_area.setStyleSheet("color:#555; font-style:italic;")
        row.addWidget(self.lbl_area)
        row.addStretch(1)
        self.lbl_hint = QLabel(
            "Use <b>Copy table to clipboard</b> to paste into "
            "Excel/Word.")
        self.lbl_hint.setStyleSheet("color:#555;")
        row.addWidget(self.lbl_hint)
        tv.addLayout(row)
        outer.addWidget(self.table_group)

    # =================================================================
    def _load_help_image(self):
        if HELP_IMAGE is None or not HELP_IMAGE.is_file():
            return
        pm = QPixmap(str(HELP_IMAGE))
        if pm.isNull():
            return
        self._pixmap_original = pm
        self.overlay.set_pixmap(pm)

    # =================================================================
    # Area handling
    # =================================================================
    def _restore_saved_area(self):
        v = QSettings().value(SETTINGS_KEY_AREA, None)
        if v is None:
            return
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        if v <= 0:
            return
        self.spin_area.blockSignals(True)
        self.spin_area.setValue(v)
        self.spin_area.blockSignals(False)
        self._area_user_set = True
        self.lbl_area.setText(
            f"Catchment area (user-set): {v:,.3f} km²")

    def _save_area(self):
        with contextlib.suppress(Exception):
            QSettings().setValue(SETTINGS_KEY_AREA,
                                 self.spin_area.value())

    def _on_area_changed(self, value):
        if value > 0:
            self._area_user_set = True
            self._save_area()
            self.lbl_area.setText(
                f"Catchment area (user-set): {value:,.3f} km²")
        else:
            self._area_user_set = False
            self.lbl_area.setText("Catchment area: —")
        if self.values:
            self._rebuild_table()
            self._refresh_banner()

    def _auto_area_from_raster(self):
        out_dir = Path(self.get_output_dir() or "")
        area_km2 = self._compute_area_from_raster(out_dir)
        if area_km2 is None:
            QMessageBox.warning(
                self, "Auto area",
                "Could not compute area from "
                "Recharge_Total_annual.tif.")
            return
        self._area_km2_from_raster = area_km2
        self.spin_area.blockSignals(True)
        self.spin_area.setValue(area_km2)
        self.spin_area.blockSignals(False)
        self._area_user_set = False
        self.lbl_area.setText(
            f"Catchment area (from raster): {area_km2:,.3f} km²")
        if self.values:
            self._rebuild_table()
            self._refresh_banner()

    def _compute_area_from_raster(self, out_dir: Path) -> Optional[float]:
        p = out_dir / "Recharge_Total_annual.tif"
        if not p.exists():
            p = out_dir / "Recharge_diffusive_annual.tif"
        if not p.exists():
            return None
        try:
            with rasterio.open(str(p)) as src:
                h8 = max(1, src.height // 8)
                w8 = max(1, src.width // 8)
                arr = src.read(1, out_shape=(h8, w8))
                nd = src.nodata if src.nodata is not None else -9999.0
                valid_small = int(((arr != nd) & np.isfinite(arr)).sum())
                scale = (src.height * src.width) / (h8 * w8)
                n_valid = valid_small * scale
                cell_m = abs(src.transform.a)
                return float(n_valid) * cell_m * cell_m / 1e6
        except Exception as exc:
            print(f"[water balance] could not compute area: {exc}")
            return None

    def _current_area_km2(self) -> Optional[float]:
        v = self.spin_area.value()
        if v > 0:
            return v
        if self._area_km2_from_raster is not None:
            return self._area_km2_from_raster
        return None

    def _to_m3(self, mm: Optional[float]) -> Optional[float]:
        if mm is None:
            return None
        area_km2 = self._current_area_km2()
        if area_km2 is None:
            return None
        return mm * area_km2 * 1e3

    # =================================================================
    # Overlay
    # =================================================================
    def _refresh_overlay(self):
        self.overlay.refresh_svg()

    def _overlay_svg(self, aspect: float) -> str:
        vw = 1000.0
        vh = 1000.0 / max(aspect, 1e-6)

        def fx(v):
            return v * vw

        def fy(v):
            return v * vh

        def g(k):
            return None if not self.values else self.values.get(k)

        def fmtv(x, suffix=""):
            return "—" if x is None else f"{x:.1f}{suffix}"

        P = g("P"); I = g("I"); AET = g("AET")
        SR_net = g("SR_net"); Rtot = g("R_total")
        dS = g("dS"); err = g("err")

        POS = {
            "AET":   (0.02, 0.04, "start"),
            "P":     (0.98, 0.04, "end"),
            "I":     (0.18, 0.32, "start"),
            "SR":    (0.98, 0.28, "end"),
            "Rtot":  (0.32, 0.62, "start"),
            "dS":    (0.02, 0.955, "start"),
            "err":   (0.98, 0.955, "end"),
        }

        c_precip = "#1e5fa8"; c_et = "#b83232"
        c_runoff = "#d97706"; c_rech = "#1b7a3e"
        c_text = "#1a1a1a"; c_muted = "#666666"

        if err is None:
            closure = "Mass balance closure: —"; cc = c_muted
        else:
            closure = f"Mass balance closure: {err:+.2f} mm/yr"
            cc = c_rech if abs(err) < 5.0 else c_et

        FONT_TITLE = 20
        FONT_VALUE = 24
        FONT_SMALL = 17
        HALO = 4

        def _text(x, y, anchor, color, text, size):
            halo = (
                f'<text x="{x:.0f}" y="{y:.0f}" text-anchor="{anchor}" '
                f'font-family="DejaVu Sans, Arial, sans-serif" '
                f'font-size="{size}" font-weight="bold" '
                f'fill="none" stroke="#ffffff" '
                f'stroke-width="{HALO}" stroke-linejoin="round" '
                f'stroke-linecap="round" '
                f'paint-order="stroke fill">{text}</text>')
            fill = (
                f'<text x="{x:.0f}" y="{y:.0f}" text-anchor="{anchor}" '
                f'font-family="DejaVu Sans, Arial, sans-serif" '
                f'font-size="{size}" font-weight="bold" '
                f'fill="{color}">{text}</text>')
            return halo + fill

        def block(x, y, anchor, color, title, value):
            return (_text(x, y, anchor, color, title, FONT_TITLE)
                    + _text(x, y + 26, anchor, color, value, FONT_VALUE))

        def small(x, y, anchor, color, text):
            return _text(x, y, anchor, color, text, FONT_SMALL)

        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" '
                 f'viewBox="0 0 {vw:.0f} {vh:.0f}" '
                 f'preserveAspectRatio="none">']

        x, y, a = POS["AET"]
        parts.append(block(fx(x), fy(y), a, c_et,
                           "Actual ET (AET, incl. I)",
                           fmtv(AET, " mm/yr")))
        x, y, a = POS["P"]
        parts.append(block(fx(x), fy(y), a, c_precip,
                           "Precipitation (P)", fmtv(P, " mm/yr")))
        x, y, a = POS["I"]
        parts.append(block(fx(x), fy(y), a, c_text,
                           "Interception (I, in AET)",
                           fmtv(I, " mm/yr")))
        x, y, a = POS["SR"]
        parts.append(block(fx(x), fy(y), a, c_runoff,
                           "Runoff (net of R_focused)",
                           fmtv(SR_net, " mm/yr")))
        x, y, a = POS["Rtot"]
        parts.append(block(fx(x), fy(y), a, c_rech,
                           "Total recharge (diff + focused)",
                           fmtv(Rtot, " mm/yr")))
        if dS is not None:
            x, y, a = POS["dS"]
            parts.append(small(fx(x), fy(y), a, c_text,
                               f"Δ soil storage (ΔS): "
                               f"{fmtv(dS, ' mm/yr')}"))
        x, y, a = POS["err"]
        parts.append(small(fx(x), fy(y), a, cc, closure))
        parts.append('</svg>')
        return "".join(parts)

    # =================================================================
    # Loading
    # =================================================================
    def _load_from_output(self):
        if not HAS_RASTERIO:
            QMessageBox.warning(self, "Water balance",
                                "rasterio is not available.")
            return
        if self._loader is not None and self._loader.isRunning():
            # Silently ignore — a load is already in progress
            return
        out_dir = Path(self.get_output_dir() or "")
        if not out_dir.is_dir():
            QMessageBox.warning(self, "Water balance",
                                "Output directory is not set.")
            return
        wd = Path(self.get_working_dir() or "")

        self.btn_load.setEnabled(False)
        self.btn_update.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_export.setEnabled(False)
        self.btn_copy.setEnabled(False)
        self.load_progress.setVisible(True)
        self.load_progress.setValue(0)
        self.load_progress.setFormat("Loading annual rasters… %p%")

        self._loader = _RasterMeanLoader(out_dir, wd, parent=self)
        self._loader.progress.connect(self._on_load_progress)
        self._loader.annual_ready.connect(self._on_annual_ready)
        self._loader.finished_ok.connect(self._on_load_finished)
        self._loader.finished_error.connect(self._on_load_error)
        self._loader.start()

    def _update_recharge(self):
        if not HAS_RASTERIO:
            return
        if self._loader is not None and self._loader.isRunning():
            return
        out_dir = Path(self.get_output_dir() or "")
        if not out_dir.is_dir():
            QMessageBox.warning(self, "Water balance",
                                "Output directory is not set.")
            return
        wd = Path(self.get_working_dir() or "")

        self.btn_load.setEnabled(False)
        self.btn_update.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.load_progress.setVisible(True)
        self.load_progress.setValue(0)
        self.load_progress.setFormat("Updating recharge rasters… %p%")

        self._loader = _RasterMeanLoader(
            out_dir, wd,
            include_precip=False,
            targets=_RasterMeanLoader.UPDATE_TARGETS,
            parent=self)
        self._loader.progress.connect(self._on_load_progress)
        self._loader.annual_ready.connect(self._on_update_ready)
        self._loader.finished_ok.connect(self._on_update_finished)
        self._loader.finished_error.connect(self._on_load_error)
        self._loader.start()

    def _cancel_load(self):
        if self._loader is not None:
            self._loader.cancel()
            self._loader.wait(2000)
        self._reset_load_ui()

    def _reset_load_ui(self):
        self.btn_load.setEnabled(True)
        self.btn_update.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.load_progress.setVisible(False)
        self.load_progress.setValue(0)
        self.load_progress.setFormat("%p%")

    def _on_load_progress(self, current, total, message):
        self.load_progress.setRange(0, total)
        self.load_progress.setValue(current)
        self.load_progress.setFormat(f"{message}  (%v / %m)")

    def _on_load_error(self, err):
        self._reset_load_ui()
        QMessageBox.critical(self, "Water balance",
                             f"Failed to load rasters:\n\n{err[:600]}")

    def _on_annual_ready(self, values: dict):
        self._apply_values(values, allow_missing_P=True)

    def _on_load_finished(self, values: dict):
        self._reset_load_ui()
        self._apply_values(values, allow_missing_P=False)
        if self.values.get("P") is not None:
            self.btn_export.setEnabled(True)
            self.btn_copy.setEnabled(True)
            self.btn_update.setEnabled(True)

    def _on_update_ready(self, values: dict):
        pass

    def _on_update_finished(self, values: dict):
        self._reset_load_ui()

        # Merge newly-read recharge values into the existing state
        for k in ("R_diff", "R_focused", "R_total"):
            if values.get(k) is not None:
                self.values[k] = values[k]

        # Re-reconcile and recompute the closure
        self._reconcile_recharge_terms()
        P = self.values.get("P")
        if P is not None:
            AET = self.values.get("AET") or 0.0
            SR_net = self.values.get("SR_net") or 0.0
            Rtot = self.values.get("R_total") or 0.0
            dS = self.values.get("dS") or 0.0
            self.values["err"] = P - (AET + SR_net + Rtot + dS)

        self._refresh_overlay()
        self._rebuild_table()
        self._refresh_banner()

        Rtot = self.values.get("R_total")
        err = self.values.get("err")
        msg = "Recharge updated."
        if Rtot is not None:
            msg += f"\nR_total = {Rtot:.3f} mm/yr."
        if err is not None:
            msg += f"\nClosure = {err:+.3f} mm/yr."
        QMessageBox.information(self, "Update recharge", msg)

    # =================================================================
    # Reconcile recharge / runoff terms
    # =================================================================
    def _reconcile_recharge_terms(self):
        """
        Populate SR_net, R_effective, and the reconciliation mode.

        The closure is:
            err = P − AET − SR_net − R_total − ΔS

        where SR_net = SR − R_focused when focused recharge is
        available. Falls back to R_diffusive with full SR when the
        focused raster is missing.

        Modes
        -----
        reconciled_focused : R_focused present
            SR_net = SR − R_focused, R_eff = R_total
        total_no_focused   : R_total present, R_focused missing
            SR_net = SR, R_eff = R_diffusive (avoids double-counting)
        diffusive_only     : Only R_diffusive present
            SR_net = SR, R_eff = R_diffusive
        no_recharge        : Nothing available
            SR_net = SR, R_eff = 0
        """
        SR_full = self.values.get("SR")
        R_diff  = self.values.get("R_diff")
        R_foc   = self.values.get("R_focused")
        R_tot   = self.values.get("R_total")

        def nz(x):
            return 0.0 if x is None else float(x)

        if R_foc is not None and R_foc > 0 and SR_full is not None:
            # Full reconciliation: subtract focused from SR, use R_total.
            SR_net = max(nz(SR_full) - nz(R_foc), 0.0)
            R_effective = nz(R_tot) if R_tot is not None else nz(R_diff)
            mode = "reconciled_focused"
            note = ("Focused recharge extracted from SR; closure uses "
                    "SR_net = SR − R_focused.")
        elif R_tot is not None and R_tot > 0:
            # R_total present but no focused raster — cannot reconcile.
            # Use R_diffusive with full SR if possible.
            SR_net = nz(SR_full)
            R_effective = nz(R_diff) if R_diff is not None else nz(R_tot)
            mode = "total_no_focused"
            note = ("R_total present but Recharge_focused_annual.tif "
                    "missing. Using Recharge_diffusive in the closure "
                    "to avoid double-counting focused recharge.")
        elif R_diff is not None:
            # Diffusive-only run.
            SR_net = nz(SR_full)
            R_effective = nz(R_diff)
            mode = "diffusive_only"
            note = "Diffusive-only run; closure uses SR and R_diffusive."
        else:
            # Nothing available.
            SR_net = nz(SR_full)
            R_effective = 0.0
            mode = "no_recharge"
            note = ("No recharge raster found. Add "
                    "Recharge_diffusive_annual.tif to the output "
                    "folder.")

        self.values["SR_net"] = SR_net
        self.values["R_effective"] = R_effective
        self.values["_reconcile_mode"] = mode
        self.values["_reconcile_note"] = note

    # =================================================================
    def _apply_values(self, values: dict, allow_missing_P: bool):
        # Merge new values into existing state
        for k in ("Interception", "AET", "SR",
                  "R_diff", "R_focused", "R_total", "dS", "P"):
            if values.get(k) is not None:
                self.values[k] = values[k]

        # Normalise
        def nz(x):
            return 0.0 if x is None else float(x)

        I   = nz(self.values.get("Interception"))
        AET = nz(self.values.get("AET"))
        SR  = nz(self.values.get("SR"))
        dS  = nz(self.values.get("dS"))
        P   = self.values.get("P")

        self.values["I"] = I
        self.values["AET"] = AET
        self.values["SR"] = SR
        self.values["dS"] = dS

        # Reconcile recharge terms → SR_net, R_effective, mode, note
        self._reconcile_recharge_terms()

        # Compute closure
        err = None
        err_old = None
        if allow_missing_P or P is None:
            pass
        else:
            SR_net = self.values.get("SR_net") or 0.0
            R_eff  = self.values.get("R_effective") or 0.0
            
            # Read surface water inflow if it exists
            sw_inflow = 0.0
            sw_file = Path(self.get_output_dir() or "") / "surface_water_inflow.txt"
            if sw_file.exists():
                try:
                    with open(sw_file, 'r') as sf:
                        sw_inflow = float(sf.read().strip())
                except Exception as e:
                    print(f"[water balance] Could not read surface_water_inflow.txt: {e}")
            
            self.values["sw_inflow"] = sw_inflow
            
            # Read groundwater inflow
            gw_inflow = 0.0
            gw_file = Path(self.get_output_dir() or "") / "groundwater_inflow.txt"
            if gw_file.exists():
                try:
                    with open(gw_file, 'r') as gf:
                        gw_inflow = float(gf.read().strip())
                except Exception as e:
                    print(f"[water balance] Could not read groundwater_inflow.txt: {e}")
            self.values["gw_inflow"] = gw_inflow
            
            # NEW CLOSURE (with SW_Inflow and GW_Inflow)
            err = (P + sw_inflow + gw_inflow) - (AET + SR_net + R_eff + dS)
            # OLD CLOSURE (without SW_Inflow)
            err_old = P - (AET + SR_net + R_eff + dS)
            
        self.values["err"] = err
        self.values["err_old"] = err_old

        # Auto-area
        if not self._area_user_set and self._area_km2_from_raster is None:
            out_dir = Path(self.get_output_dir() or "")
            area_km2 = self._compute_area_from_raster(out_dir)
            if area_km2 is not None:
                self._area_km2_from_raster = area_km2
                self.spin_area.blockSignals(True)
                self.spin_area.setValue(area_km2)
                self.spin_area.blockSignals(False)
                self.lbl_area.setText(
                    f"Catchment area (from raster): {area_km2:,.3f} km²")
        elif self._area_user_set:
            self.lbl_area.setText(
                f"Catchment area (user-set): "
                f"{self.spin_area.value():,.3f} km²")

        self._refresh_overlay()
        self._rebuild_table()
        self._refresh_banner()

    # =================================================================
    # Range evaluation
    # =================================================================
    def _evaluate_term_pct(self, key, value_mm, P):
        if key not in RECOMMENDED_TERM_RANGES:
            return ("—", "na", "#666666", None)
        lo, hi, _ = RECOMMENDED_TERM_RANGES[key]
        range_text = f"{lo:g}–{hi:g} % of P"
        if value_mm is None or P is None or P <= 0:
            return (range_text, "na", "#666666", None)
        pct = 100.0 * value_mm / P
        width = max(hi - lo, 1e-6)
        if pct < lo:
            margin = (lo - pct) / width
            return (range_text, "low",
                    "#b06f00" if margin < 0.2 else "#b83232", pct)
        if pct > hi:
            margin = (pct - hi) / width
            return (range_text, "high",
                    "#b06f00" if margin < 0.2 else "#b83232", pct)
        near = min(pct - lo, hi - pct) / width
        if near < 0.2:
            return (range_text, "ok-warn", "#1e5fa8", pct)
        return (range_text, "ok", "#1b7a3e", pct)

    def _evaluate_closure(self, err_mm, P):
        if err_mm is None:
            return ("—", "na", "#666666", "—",
                    "Load the rasters to see the closure diagnostic.")
        a = abs(err_mm)
        pct = None
        if P and P > 0:
            pct = 100.0 * a / P
        for max_mm, max_pct, color, tag, msg in CLOSURE_GRADES:
            ok_mm = a < max_mm
            ok_pct = (pct is None) or (pct < max_pct)
            if ok_mm and ok_pct:
                if max_mm >= 1e8:
                    return ("—", tag, color, tag, msg)
                return (f"< {max_mm:g} mm/yr AND < {max_pct:g} % of P",
                        tag, color, tag, msg)
        return ("—", "na", "#666666", "—", "")

    # =================================================================
    # Table
    # =================================================================
    def _rebuild_table(self):
        v = self.values
        P = v.get("P")
        rows = [
            ("P",       "Precipitation (P)",                    v.get("P")),
            ("AET",     "Actual ET (AET, includes I)",          v.get("AET")),
            ("I",       "Interception (I) — informational, "
                        "already inside AET",                   v.get("I")),
            ("SR",      "Surface runoff, total (SR)",           v.get("SR")),
            ("Rfoc",    "Focused recharge extracted from SR "
                        "(informational)",                      v.get("R_focused")),
            ("SRnet",   "Surface runoff, net of focused "
                        "(SR − R_focused)",                     v.get("SR_net")),
            ("R_tot",   "Total recharge (R_total)",             v.get("R_total")),
            ("dS",      "Change in soil storage (ΔS)",          v.get("dS")),
            ("sw_inflow", "Surface Water Inflow (SW_Inflow)",   v.get("sw_inflow")),
            ("gw_inflow", "Groundwater Inflow (GW_Inflow)",     v.get("gw_inflow")),
            ("err_old", "Mass balance (Original)",               v.get("err_old")),
            ("err",     "Mass balance (w/ SW Inflow)",          v.get("err")),
        ]

        self.table.setRowCount(len(rows))
        for i, (key, label, mm) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(label))

            it_mm = QTableWidgetItem(
                "—" if mm is None else f"{mm:+.3f}")
            it_mm.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, 1, it_mm)

            if mm is None or P is None or P <= 0:
                it_pct = QTableWidgetItem("—")
            else:
                it_pct = QTableWidgetItem(f"{100.0*mm/P:+.2f} %")
            it_pct.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, 2, it_pct)

            if mm is None:
                it_m3 = QTableWidgetItem("—")
            else:
                m3 = self._to_m3(mm)
                it_m3 = QTableWidgetItem(
                    "—" if m3 is None else f"{m3:,.0f}")
            it_m3.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, 3, it_m3)

            # Range and status
            range_text = "—"; color = "#666666"; status_text = "—"

            if key == "err":
                rtext, tag, color, _, _ = self._evaluate_closure(mm, P)
                range_text = (rtext if rtext != "—"
                              else "|closure| < 2 mm/yr AND < 0.5 % of P")
                status_text = tag
            elif key == "I":
                range_text = "(informational — already in AET)"
                status_text = "—"
            elif key == "Rfoc":
                range_text = "(extracted from SR; not counted twice)"
                status_text = "—"
            elif key == "SRnet":
                range_text = "(SR − R_focused)"
                status_text = "—"
            elif key == "SR":
                rtext, status, color, _ = self._evaluate_term_pct(
                    "SR", mm, P)
                range_text = rtext
                status_text = {
                    "ok":       "OK",
                    "ok-warn":  "OK (near edge)",
                    "low":      "Below range",
                    "high":     "Above range",
                    "na":       "—",
                }.get(status, "—")
            elif key == "R_tot":
                rtext, status, color, _ = self._evaluate_term_pct(
                    "R_tot", mm, P)
                range_text = rtext
                status_text = {
                    "ok":       "OK",
                    "ok-warn":  "OK (near edge)",
                    "low":      "Below range",
                    "high":     "Above range",
                    "na":       "—",
                }.get(status, "—")
            elif key in RECOMMENDED_TERM_RANGES:
                rtext, status, color, _ = self._evaluate_term_pct(
                    key, mm, P)
                range_text = rtext
                status_text = {
                    "ok":       "OK",
                    "ok-warn":  "OK (near edge)",
                    "low":      "Below range",
                    "high":     "Above range",
                    "na":       "—",
                }.get(status, "—")

            self.table.setItem(i, 4, QTableWidgetItem(range_text))
            it_status = QTableWidgetItem(status_text)
            it_status.setForeground(QColor(color))
            self.table.setItem(i, 5, it_status)

    # =================================================================
    # Banner
    # =================================================================
    def _refresh_banner(self):
        err = self.values.get("err")
        P = self.values.get("P")
        note = self.values.get("_reconcile_note", "")

        if err is None:
            self.banner.setStyleSheet(
                "QFrame { background:#f0f0f0; border:1px solid #c2c7cc; "
                "border-radius:5px; padding:8px; }")
            self.lbl_banner_title.setStyleSheet("color:#333;")
            self.lbl_banner_msg.setStyleSheet("color:#333;")
            self.lbl_banner_title.setText("Mass balance closure")
            self.lbl_banner_msg.setText(
                "Load the rasters to see the closure diagnostic.<br><br>"
                "<table width='100%' border='0' style='font-size: 10pt; text-align: left;'>"
                "<tr>"
                "<td width='50%' valign='top'><b style='color: #1b7a3e;'>Formula 1 — Original (No SW Inflow)</b><br>"
                "<code>closure = P − AET − SR_net − R_total − ΔS</code></td>"
                "<td width='50%' valign='top'><b style='color: #1976d2;'>Formula 2 — Modified (With SW Inflow)</b><br>"
                "<code>closure = P + SW_Inflow + GW_Inflow − AET − SR_net − R_total − ΔS</code></td>"
                "</tr></table>")
            return

        _, tag, color, _, msg = self._evaluate_closure(err, P)

        bg_map = {
            "#1b7a3e": "#e6f4ea",
            "#1e5fa8": "#e7f0fb",
            "#b06f00": "#fff6e0",
            "#c25a00": "#fff0dd",
            "#b83232": "#fdecea",
        }
        bg = bg_map.get(color, "#f0f0f0")

        self.banner.setStyleSheet(
            f"QFrame {{ background:{bg}; border:1px solid {color}; "
            f"border-radius:5px; padding:8px; }}")
        self.lbl_banner_title.setStyleSheet(
            f"color:{color}; font-weight:bold;")
        self.lbl_banner_msg.setStyleSheet(f"color:{color};")

        pct_txt = ""
        if P and P > 0:
            pct_txt = f"  ({100.0*abs(err)/P:+.2f} % of P)"

        err_old = self.values.get("err_old")
        _, tag_old, _, _, _ = self._evaluate_closure(err_old, P) if err_old is not None else ("—", "na", "#666666", "—", "—")
        old_pct_txt = f" ({100.0*abs(err_old)/P:+.2f} % of P)" if P and P > 0 and err_old is not None else ""
        
        title = (
            f"Mass balance (w/ SW Inflow) = {err:+.2f} mm/yr{pct_txt} — {tag}<br>"
            f"<span style='font-size:10pt;'>Original closure (no SW Inflow) = {err_old:+.2f} mm/yr{old_pct_txt} — {tag_old}</span><br>"
            f"<span style='font-size:9pt;'>(recommended: &lt; 5 mm/yr AND &lt; 1.5 % of P; "
            f"acceptable &lt; 10 mm/yr AND &lt; 3 % of P)</span>"
        )

        formula = ""
        substitution = ""
        if err is not None and err_old is not None:
            AET_ = self.values.get("AET") or 0.0
            SR_net = self.values.get("SR_net") or 0.0
            Rt = self.values.get("R_total") or 0.0
            dS_ = self.values.get("dS") or 0.0
            sw_val = self.values.get("sw_inflow") or 0.0
            gw_val = self.values.get("gw_inflow") or 0.0
            
            _, tag_orig, _, _, _ = self._evaluate_closure(err_old, P)
            _, tag_mod, _, _, _ = self._evaluate_closure(err, P)
            
            substitution = f"""
            <table width='100%' border='0' style='font-size: 10pt; text-align: left; margin-top: 10px;'>
              <tr>
                <td width='50%' valign='top' style='padding-right: 10px;'>
                  <b style='color: #1b7a3e; font-size: 11pt;'>Formula 1 — Original (No SW Inflow)</b><br>
                  <code>closure = P − AET − SR_net − R_total − ΔS</code><br><br>
                  <b>Evaluation:</b><br>
                  <code>= {P:.2f} − {AET_:.2f} − {SR_net:.2f} − {Rt:.2f} − {dS_:.2f}</code><br>
                  <code>= <b>{err_old:+.2f} mm/yr</b> [{tag_orig}]</code>
                </td>
                <td width='50%' valign='top'>
                  <b style='color: #1976d2; font-size: 11pt;'>Formula 2 — Modified (With SW Inflow)</b><br>
                  <code>closure = P + SW_Inflow + GW_Inflow − AET − SR_net − R_total − ΔS</code><br><br>
                  <b>Evaluation:</b><br>
                  <code>= {P:.2f} + {sw_val:.2f} + {gw_val:.2f} − {AET_:.2f} − {SR_net:.2f} − {Rt:.2f} − {dS_:.2f}</code><br>
                  <code>= <b>{err:+.2f} mm/yr</b> [{tag_mod}]</code>
                </td>
              </tr>
            </table>
            """
        msg_full = formula + substitution

        self.lbl_banner_msg.setText(msg_full)

    # =================================================================
    # Copy
    # =================================================================
    def _copy_table(self):
        if not self.values:
            return
        v = self.values
        P = v.get("P")
        area_km2 = self._current_area_km2()
        area_note = ("user-set" if self._area_user_set
                     else ("auto from raster" if area_km2 is not None
                           else "unknown"))
        lines = ["Term\tmm/yr\t% of P\tm3/yr\tRecommended range\tStatus"]
        rows = [
            ("P",       "Precipitation (P)",                    v.get("P")),
            ("AET",     "Actual ET (AET, includes I)",          v.get("AET")),
            ("I",       "Interception (I) — informational, "
                        "already inside AET",                   v.get("I")),
            ("SR",      "Surface runoff, total (SR)",           v.get("SR")),
            ("Rfoc",    "Focused recharge extracted from SR",   v.get("R_focused")),
            ("SRnet",   "Surface runoff, net of focused",       v.get("SR_net")),
            ("R_tot",   "Total recharge (R_total)",             v.get("R_total")),
            ("dS",      "Change in soil storage (dS)",          v.get("dS")),
            ("err_old", "Mass balance (Original)",               v.get("err_old")),
            ("err",     "Mass balance (w/ SW Inflow)",          v.get("err")),
        ]
        for key, label, mm in rows:
            range_text = "—"; status_text = "—"; pct_text = ""
            if mm is not None and P and P > 0:
                pct_text = f"{100.0*mm/P:+.2f} %"
            if key == "err":
                rtext, tag, _, _, _ = self._evaluate_closure(mm, P)
                range_text = (rtext if rtext != "—"
                              else "|closure| < 2 mm/yr AND < 0.5 % of P")
                status_text = tag
            elif key in ("I", "Rfoc", "SRnet"):
                range_text = "(informational)"
                status_text = "—"
            elif key in ("SR", "R_tot"):
                rtext, status, _, _ = self._evaluate_term_pct(
                    key, mm, P)
                range_text = rtext
                status_text = {
                    "ok":       "OK",
                    "ok-warn":  "OK (near edge)",
                    "low":      "Below range",
                    "high":     "Above range",
                    "na":       "—",
                }.get(status, "—")
            elif key in RECOMMENDED_TERM_RANGES:
                rtext, status, _, _ = self._evaluate_term_pct(
                    key, mm, P)
                range_text = rtext
                status_text = {
                    "ok":       "OK",
                    "ok-warn":  "OK (near edge)",
                    "low":      "Below range",
                    "high":     "Above range",
                    "na":       "—",
                }.get(status, "—")
            if mm is None:
                lines.append(f"{label}\t\t\t\t{range_text}\t{status_text}")
            else:
                m3 = self._to_m3(mm)
                m3s = "" if m3 is None else f"{m3:.0f}"
                lines.append(
                    f"{label}\t{mm:.3f}\t{pct_text}\t{m3s}\t"
                    f"{range_text}\t{status_text}")
        lines.append("")
        if area_km2 is not None:
            lines.append(
                f"Catchment area (km2)\t{area_km2:.3f}\t\t\t"
                f"{area_note}\t")

        lines.append(
            "Closure formula\tclosure = P − AET − (SR − R_focused) "
            "− R_total − ΔS\t\t\t\t")
        lines.append(
            "Definition\tSR_net = SR − R_focused\t\t\t\t")

        err = v.get("err")
        if err is not None and P and P > 0:
            AET_   = v.get("AET") or 0.0
            SR_net = self.values.get("SR_net") or 0.0
            Rt     = v.get("R_total") or 0.0
            dS_    = v.get("dS") or 0.0
            _, tag, _, _, _ = self._evaluate_closure(err, P)
            lines.append(
                f"Closure substitution\t"
                f"= {P:.2f} + {self.values.get('sw_inflow') or 0.0:.2f} − {AET_:.2f} − {SR_net:.2f} "
                f"− {Rt:.2f} − {dS_:.2f}"
                f"\t= {err:+.2f} mm/yr\t\t"
                f"< 5 mm/yr AND < 1.5 % of P\t{tag}")

        lines.append(
            "Note\tFocused recharge is extracted from the surface "
            "runoff term; SR_net = SR − R_focused avoids double-"
            "counting it in the closure.\t\t\t\t")
        lines.append(f"Source\t{self.get_output_dir() or ''}\t\t\t\t")

        QGuiApplication.clipboard().setText("\n".join(lines))
        QMessageBox.information(
            self, "Copy table",
            "Copied to clipboard as tab-separated text with columns:\n"
            "  Term | mm/yr | % of P | m³/yr | Recommended range | "
            "Status\n\n"
            "Plus the closure formula and substitution at the end.\n\n"
            "Paste into Excel, Word, or Google Sheets with Ctrl+V.")

    # =================================================================
    # Export
    # =================================================================
    def _export_png(self):
        if self._pixmap_original is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export water balance diagram",
            "water_balance.png", "PNG (*.png);;All files (*.*)")
        if not path:
            return
        try:
            pm = self._pixmap_original
            canvas = QPixmap(pm.size())
            canvas.fill(QColor("#ffffff"))
            painter = QPainter(canvas)
            painter.drawPixmap(0, 0, pm)
            aspect = pm.width() / max(pm.height(), 1)
            svg = self._overlay_svg(aspect)
            renderer = QSvgRenderer(bytes(svg, "utf-8"))
            renderer.render(painter, QRectF(0, 0, pm.width(), pm.height()))
            painter.end()
            canvas.save(path, "PNG")
            QMessageBox.information(self, "Export",
                                    f"Diagram saved to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))