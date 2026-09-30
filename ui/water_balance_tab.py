# -*- coding: utf-8 -*-
"""
Water Balance tab.

The diagram uses a landscape cross-section (saved as
`help/water_balance_background.png` inside the plugin folder) as
background, with the WetSpass-M fluxes overlaid as clean text labels
positioned at physically meaningful locations.

The values table uses a QTableWidget — select and Ctrl+C to copy, or
click "Copy Table" to send the whole table to the clipboard as TSV
ready for Excel or Word.
"""

import base64
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QGuiApplication, QFont, QColor
from qgis.PyQt.QtSvg import QSvgWidget
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QSizePolicy, QGroupBox, QFileDialog, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QHeaderView,
)

try:
    import rasterio
    from rasterio.windows import Window
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False


BACKGROUND_FILENAMES = [
    "water_balance_background.png",
    "water_balance_background.jpg",
    "water_balance_background.jpeg",
]


def classify_closure(err_mm: float, p_mm: float):
    """Return (verdict_text, hex_colour) for a closure error in mm/yr."""
    if p_mm <= 0:
        return ("n/a", "#666666")
    frac = abs(err_mm) / p_mm
    if frac < 0.01:
        return ("excellent", "#1b7a3e")
    if frac < 0.05:
        return ("acceptable", "#b8860b")
    return ("investigate", "#b83232")


class WaterBalanceTab(QWidget):

    def __init__(self, get_working_dir: Callable,
                 get_output_dir: Callable, parent=None):
        super().__init__(parent)
        self.get_working_dir = get_working_dir
        self.get_output_dir = get_output_dir
        self.values = {}
        self._build_ui()
        self._show_placeholder()

    # =================================================================
    # Background image loader
    # =================================================================
    def _background_data_uri(self) -> Optional[str]:
        plugin_root = Path(__file__).parent.parent
        for name in BACKGROUND_FILENAMES:
            for sub in ("help", "images", ""):
                p = plugin_root / sub / name if sub else plugin_root / name
                if p.exists():
                    try:
                        ext = p.suffix.lower().lstrip(".")
                        mime = ("image/jpeg" if ext in ("jpg", "jpeg")
                                else "image/png")
                        data = base64.b64encode(
                            p.read_bytes()).decode("ascii")
                        return f"data:{mime};base64,{data}"
                    except Exception:
                        continue
        return None

    # =================================================================
    # UI construction
    # =================================================================
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)

        # ---- Buttons ----------------------------------------------------
        btn_row = QHBoxLayout()
        self.btn_load = QPushButton("Load from output folder")
        self.btn_load.clicked.connect(self._load_from_output)
        btn_row.addWidget(self.btn_load)

        self.btn_update = QPushButton("Update recharge")
        self.btn_update.setToolTip(
            "Re-read the recharge rasters after running the Focused "
            "Recharge tab.")
        self.btn_update.clicked.connect(self._update_recharge)
        self.btn_update.setEnabled(False)
        btn_row.addWidget(self.btn_update)

        self.btn_copy = QPushButton("📋  Copy Table")
        self.btn_copy.setToolTip(
            "Copy the values table to the clipboard as tab-separated "
            "values. Paste directly into Excel or Word.")
        self.btn_copy.clicked.connect(self._copy_table)
        self.btn_copy.setEnabled(False)
        btn_row.addWidget(self.btn_copy)

        self.btn_export = QPushButton("Export diagram (SVG)")
        self.btn_export.clicked.connect(self._export_svg)
        self.btn_export.setEnabled(False)
        btn_row.addWidget(self.btn_export)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        # ---- Diagram ----------------------------------------------------
        self.svg_widget = QSvgWidget()
        self.svg_widget.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.svg_widget.setMinimumHeight(280)
        outer.addWidget(self.svg_widget, 1)

        # ---- Values table ----------------------------------------------
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["Component", "Symbol", "Value (mm/yr)"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.ContiguousSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.StrongFocus)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(self.table, 0)

        # ---- Status line ------------------------------------------------
        self.lbl_status = QLabel("No output loaded yet.")
        self.lbl_status.setStyleSheet(
            "color:#333; background:#f5f5f5; padding:3px 6px; "
            "border-radius:3px; font-family:monospace; font-size:10px;")
        self.lbl_status.setWordWrap(True)
        outer.addWidget(self.lbl_status, 0)

    # =================================================================
    # Raster readers
    # =================================================================
    def _read_mean(self, path) -> Optional[float]:
        p = Path(path)
        if not p.exists() or not HAS_RASTERIO:
            return None
        try:
            with rasterio.open(str(p)) as src:
                nd = src.nodata if src.nodata is not None else -9999.0
                total_sum = 0.0
                total_count = 0
                block_size = 512
                for r in range(0, src.height, block_size):
                    h = min(block_size, src.height - r)
                    for c in range(0, src.width, block_size):
                        w = min(block_size, src.width - c)
                        arr = src.read(
                            1, window=Window(c, r, w, h)).astype(np.float64)
                        arr[arr == nd] = np.nan
                        valid = arr[np.isfinite(arr)]
                        if valid.size:
                            total_sum += float(valid.sum())
                            total_count += int(valid.size)
                return total_sum / total_count if total_count else None
        except Exception:
            return None

    def _read_annual_precip(self, working_dir) -> Optional[float]:
        wd = Path(working_dir) if working_dir else None
        if wd is None or not wd.is_dir():
            return None
        for sub in ("inputs/maps/rain", "maps/rain"):
            rain_dir = wd / sub
            if rain_dir.is_dir():
                break
        else:
            return None
        total = 0.0
        found = 0
        for month in range(1, 13):
            for ext in (".tif", ".asc"):
                p = rain_dir / f"rain{month}{ext}"
                if p.exists():
                    m = self._read_mean(p)
                    if m is not None:
                        total += m
                        found += 1
                    break
        return total if found > 0 else None

    def _read_recharge_sources(self, out_dir: Path):
        p_diff = out_dir / "Recharge_diffusive_annual.tif"
        p_foc = out_dir / "Recharge_focused_annual.tif"
        p_tot = out_dir / "Recharge_Total_annual.tif"
        found = {
            "diffusive": p_diff.exists(),
            "focused": p_foc.exists(),
            "total": p_tot.exists(),
        }
        rd = self._read_mean(p_diff) if found["diffusive"] else None
        rf = self._read_mean(p_foc) if found["focused"] else None
        rt_explicit = self._read_mean(p_tot) if found["total"] else None
        return rd, rf, rt_explicit, found

    # =================================================================
    # Load / update
    # =================================================================
    def _load_from_output(self):
        out_dir = Path(self.get_output_dir() or "")
        if not out_dir.is_dir():
            QMessageBox.warning(self, "Water balance",
                                "Output directory is not set.")
            return
        wd = self.get_working_dir() or ""

        self.btn_load.setEnabled(False)
        self.btn_load.setText("Loading…")
        try:
            aet = self._read_mean(
                out_dir / "Cell_evapotranspiration_annual.tif")
            gw_dis = self._read_mean(
                out_dir / "Cell_gw_discharge_annual.tif")
            sr = self._read_mean(out_dir / "Cell_runoff_annual.tif")
            inter = self._read_mean(out_dir / "Interception_annual.tif")
            rd, rf, rt_explicit, found = self._read_recharge_sources(out_dir)
            ds = self._read_mean(
                out_dir / "soilwater_storage_delta_annual.tif")
            P = self._read_annual_precip(wd)
        finally:
            self.btn_load.setEnabled(True)
            self.btn_load.setText("Load from output folder")

        if all(v is None for v in (P, inter, aet, sr, rd)):
            QMessageBox.warning(
                self, "Water balance",
                "Could not find annual output rasters in the output "
                "folder. Run the main model first.")
            return

        P = P or 0.0
        inter = inter or 0.0
        aet = aet or 0.0
        gw_dis = gw_dis or 0.0
        sr = sr or 0.0
        rd = rd or 0.0
        rf = rf or 0.0
        ds = ds or 0.0
        rt = rt_explicit if rt_explicit is not None else (rd + rf)
        err = P - (aet + sr + rt + ds)

        self.values = {
            "P": P, "I": inter, "AET": aet, "GW_discharge": gw_dis,
            "SR": sr, "R_diff": rd, "R_foc": rf, "R_total": rt,
            "dS": ds, "err": err,
        }

        self._refresh_table()
        self._redraw_diagram()
        self.btn_export.setEnabled(True)
        self.btn_update.setEnabled(True)
        self.btn_copy.setEnabled(True)
        self._set_status(found, rt_explicit is not None, rt)

    def _update_recharge(self):
        out_dir = Path(self.get_output_dir() or "")
        if not out_dir.is_dir():
            QMessageBox.warning(self, "Water balance",
                                "Output directory is not set.")
            return
        if not self.values:
            self._load_from_output()
            return

        rd, rf, rt_explicit, found = self._read_recharge_sources(out_dir)
        rd = rd if rd is not None else self.values.get("R_diff", 0.0)
        rf = rf if rf is not None else self.values.get("R_foc", 0.0)
        rt = rt_explicit if rt_explicit is not None else (rd + rf)

        self.values["R_diff"] = rd
        self.values["R_foc"] = rf
        self.values["R_total"] = rt
        self.values["err"] = self.values.get("P", 0.0) - (
            self.values.get("AET", 0.0)
            + self.values.get("SR", 0.0)
            + rt
            + self.values.get("dS", 0.0))

        self._refresh_table()
        self._redraw_diagram()
        self._set_status(found, rt_explicit is not None, rt)

        import datetime as _dt
        self.lbl_status.setText(
            self.lbl_status.text()
            + "  ✔ Recharge refreshed at "
            + _dt.datetime.now().strftime("%H:%M:%S"))

    # =================================================================
    # Values table
    # =================================================================
    def _table_rows(self):
        v = self.values
        if not v:
            return [
                ("Precipitation", "P", "—"),
                ("Interception (included in AET)", "I", "—"),
                ("AET (precipitation-derived)", "AET", "—"),
                ("Groundwater + open-water discharge", "Q_gw", "—"),
                ("Surface runoff", "SR", "—"),
                ("Diffusive recharge", "R_diff", "—"),
                ("Focused recharge", "R_foc", "—"),
                ("Total recharge (annual mean)", "R_total", "—"),
                ("Soil-storage change (ΔS = S_end − S_start)", "ΔS", "—"),
                ("Mass balance error", "P − AET − SR − R − ΔS", "—"),
                ("Acceptability", "", "—"),
            ]

        def fmt(x):
            return f"{x:.2f}"

        err = v.get("err", 0.0)
        P = v.get("P", 0.0)
        verdict, _ = classify_closure(err, P)
        pct = (abs(err) / P * 100.0) if P > 0 else 0.0
        verdict_str = f"{verdict} ({pct:.2f} % of P)"

        return [
            ("Precipitation", "P", fmt(v.get("P", 0.0))),
            ("Interception (included in AET)", "I", fmt(v.get("I", 0.0))),
            ("AET (precipitation-derived)",
             "AET", fmt(v.get("AET", 0.0))),
            ("Groundwater + open-water discharge",
             "Q_gw", fmt(v.get("GW_discharge", 0.0))),
            ("Surface runoff", "SR", fmt(v.get("SR", 0.0))),
            ("Diffusive recharge", "R_diff", fmt(v.get("R_diff", 0.0))),
            ("Focused recharge", "R_foc", fmt(v.get("R_foc", 0.0))),
            ("Total recharge (annual mean)",
             "R_total", fmt(v.get("R_total", 0.0))),
            ("Soil-storage change (ΔS = S_end − S_start)",
             "ΔS", fmt(v.get("dS", 0.0))),
            ("Mass balance error",
             "P − AET − SR − R − ΔS", fmt(err)),
            ("Acceptability", "", verdict_str),
        ]

    def _refresh_table(self):
        rows = self._table_rows()
        self.table.setRowCount(len(rows))

        row_h = 20
        f = QFont("", 9)
        self.table.setFont(f)
        for i, (comp, sym, val) in enumerate(rows):
            self.table.setRowHeight(i, row_h)

            it_c = QTableWidgetItem(comp)
            it_s = QTableWidgetItem(sym)
            it_v = QTableWidgetItem(val)
            it_v.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            fv = QFont("", 9)
            fv.setBold(True)
            it_v.setFont(fv)

            self.table.setItem(i, 0, it_c)
            self.table.setItem(i, 1, it_s)
            self.table.setItem(i, 2, it_v)

            if comp == "Acceptability" and self.values:
                err = self.values.get("err", 0.0)
                P = self.values.get("P", 0.0)
                _, colour = classify_closure(err, P)
                c = QColor(colour)
                tint = QColor(int(c.red() * 0.15 + 255 * 0.85),
                              int(c.green() * 0.15 + 255 * 0.85),
                              int(c.blue() * 0.15 + 255 * 0.85))
                for col in range(3):
                    cell = self.table.item(i, col)
                    if cell:
                        cell.setBackground(tint)
                        cf = cell.font()
                        cf.setBold(True)
                        cell.setFont(cf)

        header_h = self.table.horizontalHeader().height()
        total_h = header_h + len(rows) * row_h + 4
        self.table.setFixedHeight(total_h)

        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)

    def _copy_table(self):
        rows = self._table_rows()
        header = ["Component", "Symbol", "Value (mm/yr)"]
        lines = ["\t".join(header)]
        for comp, sym, val in rows:
            lines.append(f"{comp}\t{sym}\t{val}")
        cb = QGuiApplication.clipboard()
        cb.setText("\n".join(lines), mode=cb.Clipboard)
        QMessageBox.information(
            self, "Copy Table",
            "Values table copied to the clipboard.\n\n"
            "Paste into Excel or Word with Ctrl+V.")

    # =================================================================
    # Diagram
    # =================================================================
    def _redraw_diagram(self):
        svg = self._svg_diagram(self.values)
        self.svg_widget.load(bytes(svg, "utf-8"))

    def _show_placeholder(self):
        self.svg_widget.load(bytes(self._svg_diagram(None), "utf-8"))

    def _set_status(self, found, used_explicit_total, rt):
        parts = [
            f"Diffusive: {'OK' if found['diffusive'] else 'missing'}",
            f"Focused: {'OK' if found['focused'] else 'missing'}",
            f"Total file: {'OK' if used_explicit_total else 'missing'}",
        ]
        if used_explicit_total:
            parts.append(f"R_total (annual mean) = {rt:.2f} mm/yr")
        else:
            parts.append(f"R_total (annual mean) = "
                         f"R_diff + R_foc = {rt:.2f} mm/yr")
        self.lbl_status.setText("   |   ".join(parts))

    # -----------------------------------------------------------------
    # Landscape SVG — background image + non-overlapping overlays
    # -----------------------------------------------------------------
    def _svg_diagram(self, values) -> str:
        W, H = 1000, 400
        c_text = "#1a1a1a"
        c_precip = "#1e5fa8"
        c_et = "#b83232"
        c_runoff = "#c25b04"
        c_rech = "#1b7a3e"
        c_gw = "#7a4fa8"
        c_ds = "#007aa8"

        def getval(k):
            return None if values is None else values.get(k)

        def fmtv(x, suffix=""):
            return "—" if x is None else f"{x:.1f}{suffix}"

        P = getval("P"); I = getval("I"); AET = getval("AET")
        GW_dis = getval("GW_discharge")
        SR = getval("SR"); Rdiff = getval("R_diff")
        Rfoc = getval("R_foc"); Rtot = getval("R_total")
        dS = getval("dS"); err = getval("err")

        parts = []
        parts.append(
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'viewBox="0 0 {W} {H}" '
            f'preserveAspectRatio="xMidYMid meet" '
            f'font-family="DejaVu Sans, Arial, sans-serif">')

        # ---- Background ------------------------------------------------
        img_uri = self._background_data_uri()
        if img_uri:
            parts.append(
                f'<image href="{img_uri}" xlink:href="{img_uri}" '
                f'x="0" y="0" width="{W}" height="{H}" '
                f'preserveAspectRatio="xMidYMid slice"/>')
        else:
            parts.append(f'<rect x="0" y="0" width="{W}" height="160" '
                         f'fill="#d6ecf7"/>')
            parts.append(f'<rect x="0" y="160" width="{W}" height="60" '
                         f'fill="#6b4423"/>')
            parts.append(f'<rect x="0" y="220" width="{W}" height="60" '
                         f'fill="#c9a66b"/>')
            parts.append(f'<rect x="0" y="280" width="{W}" height="80" '
                         f'fill="#8cc3e2"/>')
            parts.append(f'<rect x="0" y="360" width="{W}" height="40" '
                         f'fill="#b8b8b8"/>')
            parts.append(
                f'<text x="{W//2}" y="80" font-size="12" '
                f'fill="#333" text-anchor="middle">'
                f'Place water_balance_background.png in help/ for the '
                f'landscape diagram.</text>')

        # ---- Helper to draw a label box --------------------------------
        def label_box(x, y, lines, colour, anchor="start",
                      padding=7, box_alpha=0.85,
                      fsize_title=10, fsize_val=13):
            char_w = fsize_val * 0.55
            max_len = max(len(l[0]) for l in lines)
            w_est = max(130, int(max_len * char_w) + 2 * padding)
            h_est = (fsize_title * 1.25) * (len(lines) - 1) \
                    + fsize_val * 1.4 + 2 * padding
            if anchor == "end":
                x0 = x - w_est
            elif anchor == "middle":
                x0 = x - w_est / 2
            else:
                x0 = x
            out = []
            out.append(
                f'<rect x="{x0}" y="{y}" width="{w_est}" height="{h_est}" '
                f'rx="5" fill="#ffffff" opacity="{box_alpha}" '
                f'stroke="{colour}" stroke-width="1"/>')
            ty = y + padding + fsize_title
            for i, (txt, weight, fs) in enumerate(lines):
                tx = x0 + padding
                out.append(
                    f'<text x="{tx}" y="{ty}" font-size="{fs}" '
                    f'font-weight="{weight}" fill="{colour}">{txt}</text>')
                ty += fsize_title * 1.25 if fs == fsize_title \
                      else fsize_val * 1.5
            return "".join(out)

        # ---- Top sky labels: AET (left), P (right) --------------------
        parts.append(label_box(
            12, 10,
            [("Evaporation + Transpiration (AET)", "bold", 10),
             (f"{fmtv(AET, ' mm/yr')}", "bold", 14)],
            c_et, anchor="start"))
        parts.append(label_box(
            W - 12, 10,
            [("Precipitation (P)", "bold", 10),
             (f"{fmtv(P, ' mm/yr')}", "bold", 14)],
            c_precip, anchor="end"))

        # ---- Surface-level labels --------------------------------------
        # Interception — on vegetation, mid-left
        if I is not None:
            parts.append(label_box(
                20, 178,
                [("Interception (I) — included in AET", "bold", 9),
                 (f"{fmtv(I, ' mm/yr')}", "bold", 12)],
                c_et, anchor="start", box_alpha=0.80))

        # Surface runoff — on the river, right side
        if SR is not None:
            parts.append(label_box(
                W - 12, 178,
                [("Surface runoff (SR)", "bold", 9),
                 (f"{fmtv(SR, ' mm/yr')}", "bold", 12)],
                c_runoff, anchor="end", box_alpha=0.80))

        # ---- Root-zone label: ΔS ---------------------------------------
        if dS is not None:
            ds_sign = "+" if dS >= 0 else ""
            parts.append(label_box(
                20, 232,
                [("ΔS soil storage change", "bold", 9),
                 (f"{ds_sign}{dS:.1f} mm/yr", "bold", 12)],
                c_ds, anchor="start", box_alpha=0.80))

        # ---- Diffusive recharge — mid-vadose / shallow boundary -------
        if Rdiff is not None:
            parts.append(label_box(
                480, 292,
                [("Diffusive recharge (R_diff)", "bold", 9),
                 (f"{fmtv(Rdiff, ' mm/yr')}", "bold", 12)],
                c_rech, anchor="middle", box_alpha=0.82))

        # ---- Focused recharge — just below the river ------------------
        # The river runs across the middle of the scene; focused
        # recharge is a streambed process, so the label sits directly
        # under the visible river path.
        if Rfoc is not None and Rfoc > 0:
            parts.append(label_box(
                640, 225,
                [("Focused recharge (R_foc) — streambed", "bold", 9),
                 (f"{fmtv(Rfoc, ' mm/yr')}", "bold", 12)],
                c_rech, anchor="start", box_alpha=0.82))

        # ---- Total recharge — shallow aquifer right -------------------
        # Moved up to y=306 so the box does not collide with the mass
        # balance strip at the bottom (which begins at H-24 = 376).
        if Rtot is not None:
            parts.append(label_box(
                W - 12, 306,
                [("Total recharge (annual mean)", "bold", 9),
                 (f"{fmtv(Rtot, ' mm/yr')}", "bold", 12)],
                c_rech, anchor="end", box_alpha=0.85))

        # GW + open-water discharge — only if positive
        if GW_dis is not None and GW_dis > 0:
            parts.append(label_box(
                20, 306,
                [("Groundwater + open-water discharge", "bold", 9),
                 (f"{fmtv(GW_dis, ' mm/yr')}", "bold", 12)],
                c_gw, anchor="start", box_alpha=0.80))

        # ---- Mass balance strip at the very bottom --------------------
        if err is None or P is None or P <= 0:
            closure = "—"; cc = "#555555"; verdict_txt = ""
        else:
            verdict, cc = classify_closure(err, P)
            pct = (abs(err) / P * 100.0) if P > 0 else 0.0
            closure = f"{err:+.2f} mm/yr"
            verdict_txt = f"   —   {verdict}  ({pct:.2f} % of P)"

        parts.append(
            f'<rect x="0" y="{H-24}" width="{W}" height="24" '
            f'fill="#f7f8fa" opacity="0.94"/>')
        parts.append(
            f'<text x="12" y="{H-7}" font-size="11" fill="{c_text}">'
            f'<tspan font-weight="bold">Mass balance:</tspan> '
            f'P = AET + SR + R_total + ΔS '
            f'<tspan font-size="9" fill="#666">'
            f'(annual means; AET includes I)</tspan>'
            f'   <tspan font-weight="bold">Closure = </tspan>'
            f'<tspan font-weight="bold" fill="{cc}">{closure}</tspan>'
            f'<tspan font-weight="bold" fill="{cc}">{verdict_txt}</tspan>'
            f'</text>')

        parts.append('</svg>')
        return "".join(parts)

    # =================================================================
    # Export
    # =================================================================
    def _export_svg(self):
        if not self.values:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export water balance diagram",
            "water_balance.svg", "SVG (*.svg);;All files (*.*)")
        if not path:
            return
        try:
            svg = self._svg_diagram(self.values)
            Path(path).write_text(svg, encoding="utf-8")
            QMessageBox.information(self, "Export",
                                    f"Diagram saved to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))