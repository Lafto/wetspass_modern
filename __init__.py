# -*- coding: utf-8 -*-
"""
WetSpass-M Modern — QGIS Processing Plugin
===========================================
Modernized WetSpass-M 2016 with GeoTIFF I/O and QGIS Processing integration.

Original authors (2016): K. Abdollahi, I. Bashir, O. Batelaan (VUB).
Modernization (2026):    M. G. Gurmu <mggurmu@gmail.com>
"""

__version__ = "2.0.0"
__author__ = "M. G. Gurmu"
__email__ = "mggurmu@gmail.com"


def classFactory(iface):
    """QGIS Plugin entry point — called by QGIS Plugin Manager."""
    from .wetspass_plugin import WetSpassPlugin
    return WetSpassPlugin(iface)