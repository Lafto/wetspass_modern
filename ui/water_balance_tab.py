# -*- coding: utf-8 -*-
"""
Water Balance tab — annual water balance visualization.

Reads:
    Interception_annual.tif            (informational; already inside AET)
    Cell_evapotranspiration_annual.tif (total AET, includes interception,
                                        groundwater-fed and open-water
                                        evaporation)
    Cell_runoff_annual.tif
    Recharge_Total_annual.tif          (produced by Combine as
                                        max(R_diffusive, R_focused))
    soilwater_storage_delta_annual.tif (ΔS)
    rain*.tif                          (for precipitation)

Closure equation (Wetspass-M 2016 convention)
---------------------------------------------
    closure = P − AET − SR − R_total − ΔS

AET already contains interception, groundwater-fed transpiration,
groundwater evaporation, and open-water evaporation. Interception is
reported as a separate raster for informational use only and is NOT
subtracted again in the closure equation — doing so would double-count
the interception term.
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
# Recommended ranges
# ===========================================================================
RECOMMENDED_TERM_RANGES = {
    "I":     (1.0, 25.0,
              "Interception in semi-arid shrubland / savanna"),
    "AET":   (40.0, 75.0,
              "Total AET is the dominant sink in semi-arid climates"),
    "SR":    (5.0, 25.0,
              "Surface runoff coefficient for semi-arid catchments"),
    "R_tot": (5.0, 25.0,
              "Total recharge (diffusive + focused combined by max)"),
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
# Overlay widget — PNG base + transparent SVG text layer
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

    TARGETS = [
        ("Interception", "Interception_annual.tif"),
        ("AET",          "Cell_evapotranspiration_annual.tif"),
        ("SR",           "Cell_runoff_annual.tif"),
        ("R_total",      "Recharge_Total_annual.tif"),
        ("dS",           "soilwater_storage_delta_annual.tif"),
    ]

    UPDATE_TARGETS = [
        ("R_total", "Recharge_Total_annual.tif"),
    ]

    def __init__(self, output_dir: Path, working_dir: Path,
                 block_size: int = 1024, use_overview: bool = True,
                 precip_decim: int = 16, include_precip: bool = True,
                 targets=None, parent=None):
        super().__init__(parent)
        self.output_dir    = Path(output_dir)
        self.working_dir   = Path(working_dir)
        self.block_size    = block_size
        self.use_overview  = use_overview
        self.precip_decim  = precip_decim
        self.include_precip = include_precip
        self.targets       = targets or self.TARGETS
        self._cancelled    = False

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
                    "Inputs/maps/rain", "Maps/rain"):
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
            "Annual water balance (Wetspass-M 2016 formulation). Reads "
            "<b>Recharge_Total_annual.tif</b> (produced by the Combine "
            "step as max(R_diffusive, R_focused) on each cell). "
            "Closure: <b>P = AET + SR + R_total + ΔS</b>. "
            "Interception is already included in AET and is shown for "
            "information only."))

        btn_row = QHBoxLayout()
        self.btn_load = QPushButton("Load from output folder")
        self.btn_load.clicked.connect(self._load_from_output)
        btn_row.addWidget(self.btn_load)

        self.btn_update = QPushButton("Update recharge")
        self.btn_update.setToolTip(
            "Re-read only Recharge_Total_annual.tif and recompute the "
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
        self.table.setMinimumHeight(220)
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
            self._rebuild_table("+")
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
            self._rebuild_table("+")
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
    # Overlay SVG — haloed labels
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
        SR = g("SR"); Rtot = g("R_total")
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
                           "Surface runoff", fmtv(SR, " mm/yr")))
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
            QMessageBox.information(self, "Water balance",
                                    "A load is already in progress.")
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
        self.load_progress.setFormat("Updating total recharge… %p%")

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
        new_rtot = values.get("R_total")
        if new_rtot is None:
            QMessageBox.warning(
                self, "Update recharge",
                "Recharge_Total_annual.tif not found — the previous "
                "value is kept.")
            return
        self.values["R_total"] = new_rtot
        AET = self.values.get("AET") or 0.0
        SR = self.values.get("SR") or 0.0
        dS = self.values.get("dS") or 0.0
        P = self.values.get("P")
        if P is not None:
            # AET already includes interception; do not subtract I again.
            self.values["err"] = P - (AET + SR + new_rtot + dS)
        self._refresh_overlay()
        self._rebuild_table("+")
        self._refresh_banner()
        QMessageBox.information(
            self, "Update recharge",
            f"R_total updated to {new_rtot:.3f} mm/yr.\n"
            f"Closure = {self.values.get('err', 0.0):+.3f} mm/yr.")

    # =================================================================
    def _apply_values(self, values: dict, allow_missing_P: bool):
        I       = values.get("Interception")
        AET     = values.get("AET")
        SR      = values.get("SR")
        R_total = values.get("R_total")
        dS      = values.get("dS")
        P       = values.get("P")

        if I is None: I = self.values.get("I")
        if AET is None: AET = self.values.get("AET")
        if SR is None: SR = self.values.get("SR")
        if R_total is None: R_total = self.values.get("R_total")
        if dS is None: dS = self.values.get("dS")
        if P is None: P = self.values.get("P")

        def nz(x):
            return 0.0 if x is None else float(x)

        I = nz(I); AET = nz(AET); SR = nz(SR)
        R_total = nz(R_total); dS = nz(dS)

        if allow_missing_P or P is None:
            P_val = P
            err = None
        else:
            P_val = P
            # AET already includes interception, groundwater-fed ET,
            # and open-water evaporation. Interception is NOT subtracted
            # again here — doing so would double-count it.
            err = P_val - (AET + SR + R_total + dS)

        self.values = {
            "P": P_val, "I": I, "AET": AET, "SR": SR,
            "R_total": R_total, "dS": dS,
            "err": err, "_gw_sign": "+",
        }

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
        self._rebuild_table("+")
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
    def _rebuild_table(self, gw_sign: str):
        v = self.values
        P = v.get("P")
        rows = [
            ("P",      "Precipitation (P)",              v.get("P")),
            ("AET",    "Actual ET (AET, includes I)",    v.get("AET")),
            ("I",      "Interception (I) — informational, "
                       "already inside AET",             v.get("I")),
            ("SR",     "Surface runoff (SR)",            v.get("SR")),
            ("R_tot",  "Total recharge (R_total)",       v.get("R_total")),
            ("dS",     "Change in soil storage (ΔS)",    v.get("dS")),
            ("err",    "Mass balance closure",           v.get("err")),
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
            range_text = "—"; color = "#666666"; status_text = "—"
            if key == "err":
                rtext, tag, color, _, _ = self._evaluate_closure(mm, P)
                range_text = (rtext if rtext != "—"
                              else "|closure| < 2 mm/yr AND < 0.5 % of P")
                status_text = tag
            elif key == "I":
                # Interception informational row: no status check
                range_text = "(informational — already in AET)"
                status_text = "—"
            elif key is not None:
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
    def _refresh_banner(self):
        """
        Coloured closure banner with the Wetspass-M 2016 formula.

        Formula shown:
            closure = P − AET − SR − R_total − ΔS

        AET already contains interception, groundwater-fed ET, and
        open-water evaporation, so interception is NOT subtracted
        again in the closure.
        """
        err = self.values.get("err")
        P = self.values.get("P")

        if err is None:
            self.banner.setStyleSheet(
                "QFrame { background:#f0f0f0; border:1px solid #c2c7cc; "
                "border-radius:5px; padding:8px; }")
            self.lbl_banner_title.setStyleSheet("color:#333;")
            self.lbl_banner_msg.setStyleSheet("color:#333;")
            self.lbl_banner_title.setText("Mass balance closure")
            self.lbl_banner_msg.setText(
                "Load the rasters to see the closure diagnostic.<br>"
                "<code>closure = P − AET − SR − R_total − ΔS</code>")
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

        title = (
            f"Mass balance closure = {err:+.2f} mm/yr{pct_txt}   "
            f"—   {tag}   "
            f"(recommended: &lt; 5 mm/yr AND &lt; 1.5 % of P; "
            f"acceptable &lt; 10 mm/yr AND &lt; 3 % of P)"
        )

        formula = "closure = P − AET − SR − R_total − ΔS"

        substitution = ""
        if P is not None:
            AET_ = self.values.get("AET") or 0.0
            SR_ = self.values.get("SR") or 0.0
            Rt = self.values.get("R_total") or 0.0
            dS_ = self.values.get("dS") or 0.0
            substitution = (
                f"= {P:.2f} − {AET_:.2f} − {SR_:.2f} "
                f"− {Rt:.2f} − {dS_:.2f}"
                f"  =  {err:+.2f} mm/yr"
            )

        note = ("AET already includes interception and groundwater-fed "
                "evaporation, so interception is not subtracted again.")

        if substitution:
            msg_full = (f"{msg}<br>"
                        f"<code>{formula}</code><br>"
                        f"<code>{substitution}</code><br>"
                        f"<i>{note}</i>")
        else:
            msg_full = (f"{msg}<br><code>{formula}</code><br>"
                        f"<i>{note}</i>")

        self.lbl_banner_title.setText(title)
        self.lbl_banner_msg.setText(msg_full)

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
            ("P",      "Precipitation (P)",              v.get("P")),
            ("AET",    "Actual ET (AET, includes I)",    v.get("AET")),
            ("I",      "Interception (I) — informational, "
                       "already inside AET",             v.get("I")),
            ("SR",     "Surface runoff (SR)",            v.get("SR")),
            ("R_tot",  "Total recharge (R_total)",       v.get("R_total")),
            ("dS",     "Change in soil storage (dS)",    v.get("dS")),
            ("err",    "Mass balance closure",           v.get("err")),
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
            elif key == "I":
                range_text = "(informational — already in AET)"
                status_text = "—"
            elif key is not None:
                rtext, status, _, _ = self._evaluate_term_pct(key, mm, P)
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
            "Closure formula\tclosure = P − AET − SR − R_total "
            "− ΔS\t\t\t\t")

        err = v.get("err")
        if err is not None and P and P > 0:
            AET_ = v.get("AET") or 0.0
            SR_ = v.get("SR") or 0.0
            Rt = v.get("R_total") or 0.0
            dS_ = v.get("dS") or 0.0
            _, tag, _, _, _ = self._evaluate_closure(err, P)
            lines.append(
                f"Closure substitution\t"
                f"= {P:.2f} − {AET_:.2f} − {SR_:.2f} "
                f"− {Rt:.2f} − {dS_:.2f}"
                f"\t= {err:+.2f} mm/yr\t\t"
                f"< 5 mm/yr AND < 1.5 % of P\t{tag}")

        lines.append(
            "Note\tInterception is included inside AET and is not "
            "subtracted again in the closure.\t\t\t\t")
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