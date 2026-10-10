# -*- coding: utf-8 -*-
"""
Calibration / validation tab.

Load an observed-data CSV, match it against Simulated.tbl, compute
standard metrics (NSE, KGE, PBIAS, R2, RMSE, MAE, nRMSE), and plot
observed vs simulated monthly values.
"""

from pathlib import Path

import numpy as np

from qgis.PyQt.QtCore import Qt, QPointF, QRectF
from qgis.PyQt.QtGui import (
    QPainter, QPen, QColor, QBrush, QFont, QPolygonF,
)
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QFileDialog, QMessageBox, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSizePolicy,
)

from ..core.calibration import (
    read_observed_csv, read_simulated_tbl, compute_all_metrics,
    grade_nse, grade_pbias,
)


SIM_COLUMNS = [
    ("AET",           "aet"),
    ("Runoff",        "runoff"),
    ("Interception",  "interception"),
    ("Recharge",      "recharge"),
]

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ===========================================================================
# Simple plot widget (no matplotlib dependency)
# ===========================================================================
class _LinePlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(280)
        self._obs = None
        self._sim = None
        self._x_labels = None
        self._y_label = ""

    def set_data(self, obs, sim, x_labels=None, y_label=""):
        self._obs = np.asarray(obs, dtype=np.float64) if obs is not None \
            else None
        self._sim = np.asarray(sim, dtype=np.float64) if sim is not None \
            else None
        self._x_labels = x_labels
        self._y_label = y_label
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        if self._obs is None and self._sim is None:
            painter.setPen(QColor("#888"))
            f = QFont(); f.setItalic(True)
            painter.setFont(f)
            painter.drawText(self.rect(),
                             Qt.AlignmentFlag.AlignCenter,
                             "No data yet — load a CSV and press Compare.")
            painter.end()
            return

        pad_l, pad_r, pad_t, pad_b = 60, 20, 20, 50
        plot_w = max(self.width() - pad_l - pad_r, 10)
        plot_h = max(self.height() - pad_t - pad_b, 10)

        all_y = []
        for arr in (self._obs, self._sim):
            if arr is not None:
                v = arr[np.isfinite(arr)]
                if v.size:
                    all_y.append(v)
        if not all_y:
            painter.end()
            return
        y_all = np.concatenate(all_y)
        y_min = float(y_all.min())
        y_max = float(y_all.max())
        if y_max == y_min:
            y_max += 1.0
        margin = 0.08 * (y_max - y_min)
        y_min -= margin
        y_max += margin

        n = max(
            self._obs.size if self._obs is not None else 0,
            self._sim.size if self._sim is not None else 0)
        if n == 0:
            painter.end()
            return

        def x_px(i):
            if n == 1:
                return pad_l + plot_w / 2
            return pad_l + (i / (n - 1)) * plot_w

        def y_px(v):
            return pad_t + (1.0 - (v - y_min) / (y_max - y_min)) * plot_h

        # Grid lines
        painter.setPen(QPen(QColor("#e0e0e0"), 1))
        for i in range(5):
            yv = y_min + (i / 4) * (y_max - y_min)
            yp = y_px(yv)
            painter.drawLine(pad_l, int(yp), pad_l + plot_w, int(yp))
            painter.setPen(QColor("#444"))
            f = QFont(); f.setPointSize(8)
            painter.setFont(f)
            painter.drawText(4, int(yp) + 4, f"{yv:.2f}")
            painter.setPen(QPen(QColor("#e0e0e0"), 1))

        # X axis
        painter.setPen(QPen(QColor("#666"), 1))
        painter.drawLine(pad_l, pad_t + plot_h,
                         pad_l + plot_w, pad_t + plot_h)

        # X labels
        if self._x_labels and len(self._x_labels) == n:
            painter.setPen(QColor("#444"))
            f = QFont(); f.setPointSize(8)
            painter.setFont(f)
            for i, lab in enumerate(self._x_labels):
                xp = x_px(i)
                painter.drawText(int(xp) - 12, pad_t + plot_h + 16, str(lab))
        else:
            painter.setPen(QColor("#444"))
            f = QFont(); f.setPointSize(8)
            painter.setFont(f)
            for i in range(n):
                xp = x_px(i)
                painter.drawText(int(xp) - 6, pad_t + plot_h + 16, str(i + 1))

        # Y label
        if self._y_label:
            painter.save()
            painter.translate(14, pad_t + plot_h / 2)
            painter.rotate(-90)
            painter.drawText(-70, 0, self._y_label)
            painter.restore()

        # Draw lines
        def draw_series(arr, color, width, marker):
            if arr is None:
                return
            pts = []
            for i, v in enumerate(arr):
                if np.isfinite(v):
                    pts.append(QPointF(x_px(i), y_px(v)))
            if len(pts) < 2:
                return
            painter.setPen(QPen(color, width))
            poly = QPolygonF(pts)
            painter.drawPolyline(poly)
            if marker:
                painter.setBrush(QBrush(color))
                r = 3
                for p in pts:
                    painter.drawEllipse(p, r, r)

        draw_series(self._sim, QColor("#1e5fa8"), 2.0, True)
        draw_series(self._obs, QColor("#b83232"), 2.0, True)

        # Legend
        painter.setPen(QColor("#b83232"))
        painter.drawText(self.width() - 160, 20, "— Observed")
        painter.setPen(QColor("#1e5fa8"))
        painter.drawText(self.width() - 160, 36, "— Simulated")

        painter.end()


# ===========================================================================
# Tab
# ===========================================================================
class CalibrationTab(QWidget):

    def __init__(self, get_output_dir, parent=None):
        super().__init__(parent)
        self.get_output_dir = get_output_dir
        self._obs_cache = None
        self._sim_cache = None
        self._metrics = None
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        outer.addWidget(QLabel(
            "Load an observed-data CSV and compare it against "
            "<b>Simulated.tbl</b> produced by the diffusive run. "
            "Standard hydrology metrics (NSE, KGE, PBIAS, R², RMSE, MAE) "
            "are computed and shown below."))

        # ---- File row ----
        g_file = QGroupBox("Observed data")
        fv = QVBoxLayout(g_file)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Observed CSV:"))
        self.txt_observed = QLineEdit()
        row1.addWidget(self.txt_observed, 1)
        btn_browse = QPushButton("...")
        btn_browse.setFixedWidth(32)
        btn_browse.clicked.connect(self._browse_observed)
        row1.addWidget(btn_browse)
        fv.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Simulated column:"))
        self.combo_sim = QComboBox()
        for label, key in SIM_COLUMNS:
            self.combo_sim.addItem(label, key)
        self.combo_sim.setCurrentIndex(1)  # Runoff by default
        row2.addWidget(self.combo_sim)
        row2.addStretch(1)

        self.btn_compare = QPushButton("Compare")
        self.btn_compare.clicked.connect(self._compare)
        row2.addWidget(self.btn_compare)

        fv.addLayout(row2)
        outer.addWidget(g_file)

        # ---- Metrics table ----
        g_metrics = QGroupBox("Metrics")
        mv = QVBoxLayout(g_metrics)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["Metric", "Value", "Grade"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setMaximumHeight(220)
        mv.addWidget(self.table)
        outer.addWidget(g_metrics)

        # ---- Plot ----
        g_plot = QGroupBox("Observed vs Simulated")
        pv = QVBoxLayout(g_plot)
        self.plot = _LinePlot()
        self.plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        pv.addWidget(self.plot)
        outer.addWidget(g_plot, 1)

        # ---- Export ----
        row3 = QHBoxLayout()
        row3.addStretch(1)
        btn_export = QPushButton("Export metrics CSV")
        btn_export.clicked.connect(self._export_metrics)
        btn_export.setEnabled(False)
        self.btn_export = btn_export
        row3.addWidget(btn_export)
        outer.addLayout(row3)

    # =================================================================
    def _browse_observed(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select observed-data CSV", "",
            "CSV (*.csv *.txt *.tbl);;All files (*.*)")
        if path:
            self.txt_observed.setText(path)

    # =================================================================
    def _compare(self):
        csv_path = self.txt_observed.text().strip()
        if not csv_path:
            QMessageBox.warning(self, "Compare",
                                "Pick an observed-data CSV first.")
            return

        out_dir = Path(self.get_output_dir() or "")
        if not out_dir.is_dir():
            QMessageBox.warning(self, "Compare",
                                "Set the Output Directory first.")
            return
        sim_path = out_dir / "Simulated.tbl"
        if not sim_path.exists():
            QMessageBox.warning(
                self, "Compare",
                f"Simulated.tbl not found in:\n{out_dir}\n\n"
                "Run the diffusive model first.")
            return

        try:
            obs = read_observed_csv(csv_path)
        except Exception as exc:
            QMessageBox.critical(self, "Observed data", str(exc))
            return

        col_key = self.combo_sim.currentData()
        try:
            sim_map = read_simulated_tbl(sim_path, column=col_key)
        except Exception as exc:
            QMessageBox.critical(self, "Simulated.tbl", str(exc))
            return

        # ---- Align observed and simulated on month ----
        obs_months = obs.get("months")
        obs_values = obs.get("values")

        if obs_months is not None and len(obs_months) == len(obs_values):
            # Group observed by month: take the mean of all observations
            # for each month 1..12
            buckets = {m: [] for m in range(1, 13)}
            for m, v in zip(obs_months, obs_values):
                if m in buckets and v is not None:
                    buckets[m].append(v)
            obs_monthly = {}
            for m, vals in buckets.items():
                if vals:
                    obs_monthly[m] = float(np.mean(vals))
            # If only one obs point per month, use it directly
            if len(obs_monthly) == 0:
                QMessageBox.warning(
                    self, "Compare",
                    "No month information could be read from the CSV. "
                    "Expected columns: month, value.")
                return
        else:
            # No month info → assume rows are month 1..12
            obs_monthly = {}
            for i, v in enumerate(obs_values):
                m = i + 1
                if 1 <= m <= 12:
                    obs_monthly[m] = v

        # Now align to sim months present in both
        common_months = sorted(set(obs_monthly.keys()) & set(sim_map.keys()))
        if not common_months:
            QMessageBox.warning(
                self, "Compare",
                "No overlapping months between observed data and "
                "Simulated.tbl.")
            return

        obs_vec = [obs_monthly[m] for m in common_months]
        sim_vec = [sim_map[m] for m in common_months]

        metrics = compute_all_metrics(obs_vec, sim_vec)
        self._metrics = metrics
        self._obs_cache = (common_months, obs_vec)
        self._sim_cache = (common_months, sim_vec)

        self._fill_table(metrics, obs, col_key)
        self._update_plot(common_months, obs_vec, sim_vec, col_key)

        self.btn_export.setEnabled(True)

    # =================================================================
    def _fill_table(self, metrics, obs, col_key):
        self.table.setRowCount(0)
        rows = [
            ("Observed source", obs.get("label", "—"), "—", "#333333"),
            ("Months compared", str(metrics.get("n", 0)), "—", "#333333"),
            ("", "", "", "#333333"),
            ("NSE",  self._fmt(metrics["NSE"], 4), "nse", "#333333"),
            ("KGE",  self._fmt(metrics["KGE"], 4), "kge", "#333333"),
            ("PBIAS", self._fmt(metrics["PBIAS"], 2, "%"),
             "pbias", "#333333"),
            ("R²",   self._fmt(metrics["R2"], 4), "r2", "#333333"),
            ("RMSE", self._fmt(metrics["RMSE"], 4), "rmse", "#333333"),
            ("MAE",  self._fmt(metrics["MAE"], 4), "mae", "#333333"),
            ("nRMSE", self._fmt(metrics["nRMSE"], 2, "%"),
             "nrmse", "#333333"),
        ]
        self.table.setRowCount(len(rows))
        for i, (name, val, kind, color) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(name))
            it_v = QTableWidgetItem(val)
            it_v.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, 1, it_v)

            grade = "—"
            grade_color = QColor("#666666")
            if kind == "nse":
                grade, c = grade_nse(metrics["NSE"])
                grade_color = QColor(c)
            elif kind == "kge":
                if metrics["KGE"] is not None:
                    g, c = grade_nse(metrics["KGE"])
                    grade = g
                    grade_color = QColor(c)
            elif kind == "pbias":
                grade, c = grade_pbias(metrics["PBIAS"])
                grade_color = QColor(c)
            elif kind == "r2":
                if metrics["R2"] is not None:
                    if metrics["R2"] >= 0.75:
                        grade = "Very good"
                        grade_color = QColor("#1b7a3e")
                    elif metrics["R2"] >= 0.5:
                        grade = "Good"
                        grade_color = QColor("#1b7a3e")
                    elif metrics["R2"] >= 0.3:
                        grade = "Acceptable"
                        grade_color = QColor("#b06f00")
                    else:
                        grade = "Poor"
                        grade_color = QColor("#b83232")
            it_g = QTableWidgetItem(grade)
            it_g.setForeground(grade_color)
            self.table.setItem(i, 2, it_g)

    @staticmethod
    def _fmt(v, dec, suffix=""):
        if v is None:
            return "—"
        return f"{v:.{dec}f}{suffix}"

    def _update_plot(self, months, obs_vec, sim_vec, col_key):
        x_labels = [MONTH_NAMES[m - 1] for m in months]
        label = self.combo_sim.currentText()
        self.plot.set_data(obs_vec, sim_vec,
                            x_labels=x_labels,
                            y_label=label)

    # =================================================================
    def _export_metrics(self):
        if not self._metrics:
            return
        out_dir = Path(self.get_output_dir() or "")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save metrics as CSV",
            str(out_dir / "calibration_metrics.csv"),
            "CSV (*.csv);;All files (*.*)")
        if not path:
            return
        lines = ["metric,value"]
        for k in ("NSE", "KGE", "PBIAS", "R2", "RMSE", "MAE", "nRMSE", "n"):
            v = self._metrics.get(k)
            lines.append(f"{k},{'' if v is None else v}")
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8")
            QMessageBox.information(
                self, "Export metrics", f"Saved:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))