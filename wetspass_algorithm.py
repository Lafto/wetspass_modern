# -*- coding: utf-8 -*-
"""QGIS Processing algorithm wrapper for WetSpass-M Modern."""

import shutil
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingOutputString,
    QgsRasterLayer,
    QgsProject,
)
from qgis import processing as qgis_processing

from .core.calculation_engine import WetSpassParameters
from .core.workflow import WetSpassWorkflow
from .core.raster_io import RASTERIO_AVAILABLE


class WetSpassAlgorithm(QgsProcessingAlgorithm):

    # --- parameter IDs ---
    DEM = "DEM"
    LANDUSE = "LANDUSE"
    SOIL = "SOIL"
    WORKING_DIR = "WORKING_DIR"
    RAIN_FOLDER = "RAIN_FOLDER"
    PET_FOLDER = "PET_FOLDER"
    TEMP_FOLDER = "TEMP_FOLDER"
    WIND_FOLDER = "WIND_FOLDER"
    GWDEPTH_FOLDER = "GWDEPTH_FOLDER"
    SNOW_FOLDER = "SNOW_FOLDER"
    START_MONTH = "START_MONTH"
    END_MONTH = "END_MONTH"
    A_INTERCEPTION = "A_INTERCEPTION"
    ALFA = "ALFA"
    W_SLOPE = "W_SLOPE"
    W_LANDUSE = "W_LANDUSE"
    W_SOIL = "W_SOIL"
    X_COEF = "X_COEF"
    LP = "LP"
    INTENSITY = "INTENSITY"
    BETA = "BETA"
    CONTRIBUTION = "CONTRIBUTION"
    AREA_KM2 = "AREA_KM2"
    SIMULATE_SNOW = "SIMULATE_SNOW"
    CREATE_SIM_FILE = "CREATE_SIM_FILE"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    SIM_FILE_OUT = "SIM_FILE_OUT"

    def tr(self, s):
        return QCoreApplication.translate("WetSpassAlgorithm", s)

    def createInstance(self):
        return WetSpassAlgorithm()

    def name(self):
        return "wetspass_modern"

    def displayName(self):
        return self.tr("WetSpass-M Modern (Monthly Water Balance)")

    def group(self):
        return self.tr("Hydrology")

    def groupId(self):
        return "hydrology"

    def shortHelpString(self):
        return self.tr(
            "<h3>WetSpass-M Modern</h3>"
            "<p>Spatially distributed monthly water balance model "
            "(recharge, ET, runoff, interception).</p>"
            "<p><b>Calculation logic:</b> identical to WetSpass-M 2016.<br>"
            "<b>I/O:</b> GeoTIFF (modernized from ASCII grids).</p>"
            "<p><b>Note:</b> 'Cell_evapotranspiration' is the "
            "precipitation-derived AET (includes interception). "
            "Groundwater-fed ET and open-water evaporation are written "
            "separately as 'Cell_gw_discharge'. The correct mass balance is "
            "<b>P = AET + SR + R</b>.</p>"
            "<p><b>Original authors:</b> K. Abdollahi, I. Bashir, O. Batelaan "
            "(Vrije Universiteit Brussel).</p>"
            "<p><b>Modernization:</b> M. G. Gurmu, 2026.</p>"
        )

    # -----------------------------------------------------------------
    def initAlgorithm(self, config=None):
        # Rasters
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.DEM, self.tr("DEM")))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.LANDUSE, self.tr("Land Use map")))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.SOIL, self.tr("Soil map")))

        # Working directory
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.WORKING_DIR,
            self.tr("Working directory (must contain inputs/tables/*.TBL)")))

        # Monthly input folders
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.RAIN_FOLDER,
            self.tr("Rainfall folder (rain1.tif … rain12.tif)")))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.PET_FOLDER,
            self.tr("PET folder (pet1.tif … pet12.tif)")))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.TEMP_FOLDER,
            self.tr("Temperature folder (temp1.tif … temp12.tif)")))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.WIND_FOLDER,
            self.tr("Wind folder (wind1.tif … wind12.tif)")))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.GWDEPTH_FOLDER,
            self.tr("Groundwater depth folder (gwdepth1.tif … gwdepth12.tif)")))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.SNOW_FOLDER,
            self.tr("Snow cover folder (optional)"),
            optional=True))

        # Time range
        self.addParameter(QgsProcessingParameterNumber(
            self.START_MONTH, self.tr("Start month (1–12)"),
            defaultValue=1, minValue=1, maxValue=12))
        self.addParameter(QgsProcessingParameterNumber(
            self.END_MONTH, self.tr("End month (1–12)"),
            defaultValue=12, minValue=1, maxValue=12))

        # Model parameters — defaults match the original WetSpass-M 2016 GUI
        self.addParameter(QgsProcessingParameterNumber(
            self.A_INTERCEPTION, self.tr('"a" interception [mm/day]'),
            defaultValue=4.5, minValue=0.1, maxValue=10.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.ALFA, self.tr("Alfa coefficient"),
            defaultValue=1.26, minValue=0.1, maxValue=5.0))   # CORRECTED
        self.addParameter(QgsProcessingParameterNumber(
            self.W_SLOPE, self.tr("Slope weight"),
            defaultValue=0.4, minValue=0.0, maxValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.W_LANDUSE, self.tr("Land use weight"),
            defaultValue=0.3, minValue=0.0, maxValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.W_SOIL, self.tr("Soil weight"),
            defaultValue=0.3, minValue=0.0, maxValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.X_COEF, self.tr('"x" coefficient (0–1)'),
            defaultValue=0.5, minValue=0.0, maxValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.LP, self.tr("LP coefficient"),
            defaultValue=0.65, minValue=0.1, maxValue=1.0))   # CORRECTED
        self.addParameter(QgsProcessingParameterNumber(
            self.INTENSITY, self.tr("Average rainfall intensity [mm/h]"),
            defaultValue=4.0, minValue=0.1, maxValue=50.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.BETA, self.tr("Beta coefficient (0–1)"),
            defaultValue=0.75, minValue=0.0, maxValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.CONTRIBUTION, self.tr("Contribution factor"),
            defaultValue=0.5, minValue=0.0, maxValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.AREA_KM2, self.tr("Basin area [km²]"),
            defaultValue=10.0, minValue=0.0))

        # Options
        self.addParameter(QgsProcessingParameterBoolean(
            self.SIMULATE_SNOW, self.tr("Include snow processes"),
            defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CREATE_SIM_FILE,
            self.tr("Create simulation file for calibration"),
            defaultValue=True))

        # Output
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, self.tr("Output folder")))
        self.addOutput(QgsProcessingOutputString(
            self.SIM_FILE_OUT, self.tr("Simulation file path")))

    # -----------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        if not RASTERIO_AVAILABLE:
            raise ImportError(
                "rasterio is required for WetSpass-M Modern. "
                "Install with: pip install rasterio")

        dem_layer = self.parameterAsRasterLayer(parameters, self.DEM, context)
        lu_layer = self.parameterAsRasterLayer(parameters, self.LANDUSE, context)
        soil_layer = self.parameterAsRasterLayer(parameters, self.SOIL, context)

        working_dir = Path(self.parameterAsString(
            parameters, self.WORKING_DIR, context))
        output_dir = Path(self.parameterAsString(
            parameters, self.OUTPUT_FOLDER, context))

        p = WetSpassParameters(
            a_interception=self.parameterAsDouble(parameters, self.A_INTERCEPTION, context),
            alfa=self.parameterAsDouble(parameters, self.ALFA, context),
            w_slope=self.parameterAsDouble(parameters, self.W_SLOPE, context),
            w_landuse=self.parameterAsDouble(parameters, self.W_LANDUSE, context),
            w_soil=self.parameterAsDouble(parameters, self.W_SOIL, context),
            x_coef=self.parameterAsDouble(parameters, self.X_COEF, context),
            lp=self.parameterAsDouble(parameters, self.LP, context),
            intensity=self.parameterAsDouble(parameters, self.INTENSITY, context),
            beta=self.parameterAsDouble(parameters, self.BETA, context),
            contribution=self.parameterAsDouble(parameters, self.CONTRIBUTION, context),
            area_km2=self.parameterAsDouble(parameters, self.AREA_KM2, context),
        )

        inputs_dir = working_dir / "inputs" / "maps"
        inputs_dir.mkdir(parents=True, exist_ok=True)

        feedback.pushInfo("Copying input rasters to working directory...")

        dem_path = inputs_dir / "dem.tif"
        lu_path = inputs_dir / "landuse.tif"
        soil_path = inputs_dir / "soil.tif"

        for layer, out in [(dem_layer, dem_path),
                           (lu_layer, lu_path),
                           (soil_layer, soil_path)]:
            qgis_processing.run("gdal:translate", {
                "INPUT": layer,
                "OUTPUT": str(out),
                "EXTRA": "-co COMPRESS=DEFLATE -co TILED=YES",
            }, feedback=feedback)

        folder_map = {
            "rain": self.parameterAsString(parameters, self.RAIN_FOLDER, context),
            "pet": self.parameterAsString(parameters, self.PET_FOLDER, context),
            "temp": self.parameterAsString(parameters, self.TEMP_FOLDER, context),
            "wind": self.parameterAsString(parameters, self.WIND_FOLDER, context),
            "gwdepth": self.parameterAsString(parameters, self.GWDEPTH_FOLDER, context),
        }
        for sub, src in folder_map.items():
            if not src:
                continue
            dst = inputs_dir / sub
            dst.mkdir(parents=True, exist_ok=True)
            src_path = Path(src)
            if src_path.exists() and src_path.is_dir():
                for f in src_path.glob("*.tif"):
                    shutil.copy2(f, dst / f.name)
                for f in src_path.glob("*.asc"):
                    shutil.copy2(f, dst / f.name)

        if self.parameterAsBoolean(parameters, self.SIMULATE_SNOW, context):
            snow_src_str = self.parameterAsString(parameters, self.SNOW_FOLDER, context)
            if snow_src_str:
                snow_src = Path(snow_src_str)
                snow_dst = inputs_dir / "snow"
                snow_dst.mkdir(parents=True, exist_ok=True)
                if snow_src.exists():
                    for f in snow_src.glob("*.tif"):
                        shutil.copy2(f, snow_dst / f.name)

        wf = WetSpassWorkflow(
            working_dir=working_dir,
            output_dir=output_dir,
            parameters=p,
            start_step=self.parameterAsInt(parameters, self.START_MONTH, context),
            end_step=self.parameterAsInt(parameters, self.END_MONTH, context),
            simulate_snow=self.parameterAsBoolean(parameters, self.SIMULATE_SNOW, context),
            create_simulation_file=self.parameterAsBoolean(parameters, self.CREATE_SIM_FILE, context),
        )

        def progress(step, total, msg):
            feedback.setProgress(int(100 * step / max(total, 1)))
            feedback.pushInfo(msg)

        wf.progress_callback = progress

        feedback.pushInfo("Starting WetSpass-M calculation...")
        wf.run()
        feedback.pushInfo("Model run complete.")
        feedback.pushInfo(f"Output folder: {output_dir}")

        for month in range(wf.start_step, wf.end_step + 1):
            rp = output_dir / f"recharge_{month}.tif"
            if rp.exists():
                layer = QgsRasterLayer(str(rp), f"Recharge M{month}")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)

        sim_path = str(output_dir / wf.simulation_filename)
        return {
            self.SIM_FILE_OUT: sim_path,
            self.OUTPUT_FOLDER: str(output_dir),
        }