# -*- coding: utf-8 -*-
"""
Small widget that renders a shapefile's polygon on a plain background.

Used by the Check Inputs tab to show a preview of the project boundary
without needing a full QGIS map canvas.
"""

from qgis.PyQt.QtCore import Qt, QPointF
from qgis.PyQt.QtGui import (
    QPainter, QPen, QColor, QBrush, QPolygonF, QFont,
)
from qgis.PyQt.QtWidgets import QWidget


class BoundaryPreview(QWidget):
    """
    Draws the first exterior ring(s) of the polygon(s) in a vector
    layer, fit to the widget with a fixed margin.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(240, 200)
        self._polygons = []       # list of [(x, y), ...]
        self._extent = None
        self._placeholder = "No boundary loaded."
        self._info_lines = []

    # -----------------------------------------------------------------
    def set_boundary(self, layer_or_path):
        self._polygons = []
        self._extent = None
        self._placeholder = ""
        self._info_lines = []

        if layer_or_path is None:
            self._placeholder = "No boundary loaded."
            self.update()
            return

        try:
            from qgis.core import QgsVectorLayer
            if isinstance(layer_or_path, str):
                layer = QgsVectorLayer(layer_or_path, "boundary", "ogr")
            else:
                layer = layer_or_path

            if not layer.isValid():
                self._placeholder = "Boundary file could not be opened."
                self.update()
                return

            self._info_lines.append(f"Features: {layer.featureCount()}")
            crs = layer.crs()
            self._info_lines.append(
                f"CRS: {crs.authid() or 'unknown'}")

            xmin = ymin = float("inf")
            xmax = ymax = -float("inf")
            n_geom = 0

            for f in layer.getFeatures():
                geom = f.geometry()
                if geom is None or geom.isEmpty():
                    continue
                for ring in self._iter_exterior_rings(geom):
                    pts = []
                    for pt in ring:
                        pts.append((pt.x(), pt.y()))
                        xmin = min(xmin, pt.x())
                        xmax = max(xmax, pt.x())
                        ymin = min(ymin, pt.y())
                        ymax = max(ymax, pt.y())
                    if pts:
                        self._polygons.append(pts)
                        n_geom += 1
                    if n_geom >= 64:
                        break
                if n_geom >= 64:
                    break

            if not self._polygons:
                self._placeholder = "No polygon geometry in layer."
            else:
                self._extent = (xmin, ymin, xmax, ymax)
        except Exception as exc:  # noqa: BLE001
            self._placeholder = f"Preview failed: {exc}"

        self.update()

    # -----------------------------------------------------------------
    @staticmethod
    def _iter_exterior_rings(geom):
        """Yield exterior rings as lists of QgsPointXY, handling single
        and multi polygon geometry."""
        try:
            if geom.isMultipart():
                for poly in geom.asMultiPolygon():
                    if poly:
                        yield poly[0]
            else:
                poly = geom.asPolygon()
                if poly:
                    yield poly[0]
        except Exception:
            return

    # -----------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fafafa"))

        if self._placeholder or not self._polygons or self._extent is None:
            painter.setPen(QColor("#888"))
            font = QFont()
            font.setItalic(True)
            painter.setFont(font)
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._placeholder or "No boundary loaded.")
            painter.end()
            return

        xmin, ymin, xmax, ymax = self._extent
        dx = max(xmax - xmin, 1e-9)
        dy = max(ymax - ymin, 1e-9)

        pad = 10
        avail_w = max(self.width() - 2 * pad, 10)
        avail_h = max(self.height() - 2 * pad - 32, 10)

        scale = min(avail_w / dx, avail_h / dy)
        new_w = dx * scale
        new_h = dy * scale
        x0 = pad + (avail_w - new_w) / 2
        y0 = pad + (avail_h - new_h) / 2

        def to_px(x, y):
            px = x0 + (x - xmin) * scale
            # raster Y grows downward, so flip
            py = y0 + (ymax - y) * scale
            return px, py

        painter.setPen(QPen(QColor("#1e5fa8"), 1.6))
        painter.setBrush(QBrush(QColor(30, 95, 168, 40)))

        for pts in self._polygons:
            poly = QPolygonF()
            for (x, y) in pts:
                px, py = to_px(x, y)
                poly.append(QPointF(px, py))
            painter.drawPolygon(poly)

        # Info footer
        if self._info_lines:
            painter.setPen(QColor("#555"))
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            y = self.height() - 8 - (len(self._info_lines) - 1) * 14
            for line in self._info_lines:
                painter.drawText(8, y, line)
                y += 14

        painter.end()