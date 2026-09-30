# -*- coding: utf-8 -*-
"""QGIS Processing provider — exposes WetSpass-M algorithms."""

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .wetspass_algorithm import WetSpassAlgorithm


class WetSpassProvider(QgsProcessingProvider):

    def id(self):
        return "wetspass"

    def name(self):
        return "WetSpass-M Modern"

    def longName(self):
        return "WetSpass-M Modern — Spatially Distributed Water Balance"

    def icon(self):
        return QIcon()

    def loadAlgorithms(self):
        self.addAlgorithm(WetSpassAlgorithm())