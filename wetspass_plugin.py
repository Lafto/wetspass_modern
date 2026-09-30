# -*- coding: utf-8 -*-
"""WetSpass-M Modern — QGIS plugin main class."""

from pathlib import Path

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from qgis.core import QgsApplication

PLUGIN_MENU = "&WetSpass-M Modern"


class WetSpassPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None
        self.dialog = None
        self.plugin_dir = Path(__file__).parent

    def initProcessing(self):
        from .wetspass_provider import WetSpassProvider
        self.provider = WetSpassProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()

        icon_path = self.plugin_dir / "icon.png"
        icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()

        self.action = QAction(
            icon, "Open WetSpass-M Modern…", self.iface.mainWindow())
        self.action.setObjectName("wetspassModernOpenAction")
        self.action.triggered.connect(self.open_dialog)

        self.iface.addPluginToMenu(PLUGIN_MENU, self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action is not None:
            self.iface.removePluginMenu(PLUGIN_MENU, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
        # Clean up the dialog's event filter and any running worker.
        if self.dialog is not None:
            try:
                self.dialog.cleanup()
            except Exception:
                pass
            self.dialog = None

    def open_dialog(self):
        """Open the WetSpass-M Modern GUI."""
        from .ui.main_dialog import WetSpassMainDialog

        if self.dialog is None:
            self.dialog = WetSpassMainDialog(self.iface,
                                             self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()