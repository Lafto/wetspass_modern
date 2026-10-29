# -*- coding: utf-8 -*-
"""
Tools for the WetSpass-M Modern plugin.
"""

from pathlib import Path

from qgis.PyQt.QtWidgets import (
    QAction, QMessageBox, QProgressDialog, QApplication,
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QPushButton,
)
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QFont
from qgis.core import QgsRasterLayer, QgsProject


try:
    from ..core.raster_io import (
        mask_diffusive_outputs_to_dem,
        mask_rasters_to_boundary,
    )
    _HAS_RASTER_IO = True
except Exception as _exc:  # noqa: BLE001
    import traceback
    print(f"[tools_menu] raster_io import failed: {_exc}")
    print(traceback.format_exc())
    mask_diffusive_outputs_to_dem = None
    mask_rasters_to_boundary = None
    _HAS_RASTER_IO = False


try:
    from ..core.gwdepth_tools import analyse_gwdepth
    _HAS_GWDEPTH = True
except Exception as _exc:  # noqa: BLE001
    import traceback
    print(f"[tools_menu] gwdepth_tools import failed: {_exc}")
    print(traceback.format_exc())
    analyse_gwdepth = None
    _HAS_GWDEPTH = False


try:
    from ..core.per_cell_closure import (
        compute_per_cell_closure, write_closure_report,
    )
    _HAS_CLOSURE = True
except Exception as _exc:  # noqa: BLE001
    import traceback
    print(f"[tools_menu] per_cell_closure import failed: {_exc}")
    print(traceback.format_exc())
    compute_per_cell_closure = None
    write_closure_report = None
    _HAS_CLOSURE = False


# ===========================================================================
# 1. Mask outputs to boundary
# ===========================================================================
def make_mask_to_dem_action(parent_widget,
                            get_working_dir,
                            get_output_dir,
                            get_boundary_path=None):
    action = QAction("Mask outputs to boundary", parent_widget)
    action.setStatusTip(
        "Set nodata (-9999) outside the project boundary on every "
        "Recharge_diffusive_*.tif and related outputs.")

    def _run():
        if not _HAS_RASTER_IO:
            QMessageBox.critical(
                parent_widget, "Mask rasters",
                "The core raster_io module could not be imported.")
            return

        wd = get_working_dir() or ""
        od = get_output_dir() or ""
        if not wd or not od:
            QMessageBox.warning(parent_widget, "Mask rasters",
                                "Set Working Directory and Output "
                                "Directory first.")
            return

        boundary = ""
        if get_boundary_path is not None:
            boundary = (get_boundary_path() or "").strip()

        use_boundary = bool(boundary)
        if use_boundary and not Path(boundary).exists():
            QMessageBox.warning(parent_widget, "Mask rasters",
                                f"Boundary shapefile not found:\n"
                                f"{boundary}")
            return

        if use_boundary:
            scope = f"Boundary shapefile:\n  {boundary}"
            method = "boundary"
        else:
            scope = ("No project boundary shapefile is set.\n\n"
                     "The DEM valid mask will be used instead.")
            method = "dem"

        if QMessageBox.question(
                parent_widget, "Mask rasters",
                f"Rewriting:\n"
                f"  • Recharge_diffusive_*.tif (13 files)\n"
                f"  • Recharge_Total_annual.tif (if present)\n"
                f"  • Recharge_focused_annual.tif (if present)\n\n"
                f"{scope}\n\nProceed?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                ) != QMessageBox.StandardButton.Yes:
            return

        dlg = QProgressDialog("Preparing…", "Cancel", 0, 100,
                              parent_widget)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        cancelled = {"v": False}

        def _progress(step, total, msg):
            if cancelled["v"]:
                return
            total = max(total, 1)
            dlg.setValue(int(100 * step / total))
            dlg.setLabelText(msg)
            QApplication.processEvents()
            if dlg.wasCanceled():
                cancelled["v"] = True

        try:
            if method == "boundary":
                ref = Path(od) / "Recharge_diffusive_annual.tif"
                if ref.exists() and mask_rasters_to_boundary is not None:
                    result = mask_rasters_to_boundary(
                        output_dir=od, boundary_path=boundary,
                        reference_raster=ref,
                        progress_callback=_progress)
                elif mask_diffusive_outputs_to_dem is not None:
                    result = mask_diffusive_outputs_to_dem(
                        output_dir=od, working_dir=wd,
                        progress_callback=_progress)
                else:
                    raise RuntimeError(
                        "Neither masking function is available.")
            else:
                if mask_diffusive_outputs_to_dem is None:
                    raise RuntimeError(
                        "mask_diffusive_outputs_to_dem is not "
                        "available.")
                result = mask_diffusive_outputs_to_dem(
                    output_dir=od, working_dir=wd,
                    progress_callback=_progress)
        except Exception as exc:
            import traceback
            dlg.close()
            QMessageBox.critical(parent_widget, "Mask failed",
                                 f"{exc}\n\n"
                                 f"{traceback.format_exc()[:1200]}")
            return
        finally:
            dlg.close()

        ok = result.get("ok", [])
        skip = result.get("skip", [])
        fail = result.get("fail", [])

        lines = [f"Masked:  {len(ok)}",
                 f"Skipped: {len(skip)}",
                 f"Failed:  {len(fail)}"]

        if skip:
            lines.append("")
            lines.append("Skipped files:")
            for n in skip[:20]:
                lines.append(f"  – {n}")
            if len(skip) > 20:
                lines.append(f"  … and {len(skip) - 20} more")

        if fail:
            lines.append("")
            lines.append("Failure details:")
            for n in fail[:20]:
                lines.append(f"  – {n}")
            if len(fail) > 20:
                lines.append(f"  … and {len(fail) - 20} more")

        box = QMessageBox(parent_widget)
        box.setWindowTitle("Mask rasters")
        box.setIcon(QMessageBox.Information if not fail
                    else QMessageBox.Warning)
        box.setText("\n".join(lines[:3]))
        if len(lines) > 3:
            box.setDetailedText("\n".join(lines[3:]))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec_()

    action.triggered.connect(_run)
    return action


# ===========================================================================
# 2. Analyse gwdepth
# ===========================================================================
def make_gwdepth_analyse_action(parent_widget,
                                get_working_dir,
                                get_output_dir):
    action = QAction("Analyse gwdepth", parent_widget)
    action.setStatusTip(
        "Per-month statistics of the gwdepth rasters.")

    def _run():
        if analyse_gwdepth is None:
            QMessageBox.critical(
                parent_widget, "Analyse gwdepth",
                "The core gwdepth_tools module could not be imported.")
            return

        wd = get_working_dir() or ""
        od = get_output_dir() or wd
        if not wd:
            QMessageBox.warning(parent_widget, "Analyse gwdepth",
                                "Set Working Directory first.")
            return
        try:
            stats = analyse_gwdepth(od, wd)
        except Exception as exc:
            QMessageBox.critical(parent_widget, "Analyse gwdepth",
                                 str(exc))
            return
        if not stats:
            QMessageBox.warning(
                parent_widget, "Analyse gwdepth",
                "No gwdepth rasters found under "
                "Inputs/maps/gwdepth.")
            return

        header = ("Month   mean      min       max      <5m%    "
                  "<10m%   >50m%")
        lines = [header, "-" * len(header)]
        for m in range(1, 13):
            if m in stats:
                s = stats[m]
                lines.append(
                    f"{m:>5d}  {s['mean']:8.2f} {s['min']:9.2f} "
                    f"{s['max']:9.2f}  {s['pct_lt_5m']:6.2f}  "
                    f"{s['pct_lt_10m']:6.2f}  {s['pct_gt_50m']:6.2f}")
        text = "\n".join(lines)

        months_present = sorted(stats.keys())
        if months_present:
            means = [stats[m]["mean"] for m in months_present]
            shallow = [stats[m]["pct_lt_5m"] for m in months_present]
            summary = (
                f"\nSummary across months 1–12:\n"
                f"  Mean gwdepth             : "
                f"{sum(means)/len(means):8.2f} m\n"
                f"  Cells shallower than 5 m : "
                f"{sum(shallow)/len(shallow):6.2f} %\n"
                f"  Values in metres below the land surface."
            )
        else:
            summary = ""

        dlg = QDialog(parent_widget)
        dlg.setWindowTitle("gwdepth summary")
        dlg.resize(760, 520)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        title = QLabel(
            "<b>Per-month gwdepth statistics</b>  "
            "<i>(metres below land surface)</i>")
        lay.addWidget(title)

        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setFont(QFont("Courier New", 10))
        edit.setLineWrapMode(QTextEdit.NoWrap)
        edit.setPlainText(text + summary)
        lay.addWidget(edit, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_copy = QPushButton("Copy to clipboard")

        def _copy():
            QApplication.clipboard().setText(text + summary)

        btn_copy.clicked.connect(_copy)
        btn_row.addWidget(btn_copy)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

        dlg.exec_()

    action.triggered.connect(_run)
    return action


# ===========================================================================
# 3. Per-cell closure
# ===========================================================================
def make_percell_closure_action(parent_widget,
                                get_working_dir,
                                get_output_dir):
    action = QAction("Per-cell closure", parent_widget)

    def _run():
        if compute_per_cell_closure is None:
            QMessageBox.critical(
                parent_widget, "Per-cell closure",
                "The core per_cell_closure module could not be "
                "imported.")
            return

        wd = get_working_dir() or ""
        od = get_output_dir() or wd
        if not wd or not od:
            QMessageBox.warning(
                parent_widget, "Per-cell closure",
                "Set Working Directory and Output Directory first.")
            return

        dlg = QProgressDialog("Computing per-cell residual…",
                              "Cancel", 0, 0, parent_widget)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        try:
            summary = compute_per_cell_closure(od, wd)
            if write_closure_report is not None:
                write_closure_report(od, summary)
        except Exception as exc:
            dlg.close()
            QMessageBox.critical(parent_widget, "Per-cell closure",
                                 str(exc))
            return
        dlg.close()

        out_path = Path(summary["output_raster"])
        if out_path.exists():
            layer = QgsRasterLayer(str(out_path), "wb_error_annual")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)

        QMessageBox.information(
            parent_widget, "Per-cell water balance residual",
            f"Wrote {out_path.name}\n\n"
            f"Cells          : {summary['n_cells']:,}\n"
            f"Mean           : {summary['mean_residual']:+.4f} mm/yr\n"
            f"Mean |value|   : "
            f"{summary['mean_abs_residual']:.4f} mm/yr\n"
            f"Cells > ±5     : {summary['pct_abs_gt_5']:.2f} %\n"
            f"Cells > ±10    : {summary['pct_abs_gt_10']:.2f} %\n"
            f"Cells > ±20    : {summary['pct_abs_gt_20']:.2f} %")

    action.triggered.connect(_run)
    return action


# ===========================================================================
# 4. Open diffusive run report
# ===========================================================================
def make_open_diffusive_report_action(parent_widget, get_output_dir):
    action = QAction("Diffusive report", parent_widget)

    def _run():
        od = get_output_dir() or ""
        if not od:
            QMessageBox.warning(parent_widget, "Run report",
                                "Set Output Directory first.")
            return
        p = Path(od) / "RunSummary.txt"
        if not p.exists():
            QMessageBox.warning(
                parent_widget, "Run report",
                f"RunSummary.txt not found in:\n{od}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    action.triggered.connect(_run)
    return action


# ===========================================================================
# 5. Open focused recharge run report
# ===========================================================================
def make_open_focused_report_action(parent_widget, get_output_dir):
    action = QAction("Focused report", parent_widget)

    def _run():
        od = get_output_dir() or ""
        if not od:
            QMessageBox.warning(parent_widget, "Run report",
                                "Set Output Directory first.")
            return
        p = Path(od) / "RunSummary_focused.txt"
        if not p.exists():
            QMessageBox.warning(
                parent_widget, "Run report",
                f"RunSummary_focused.txt not found in:\n{od}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    action.triggered.connect(_run)
    return action