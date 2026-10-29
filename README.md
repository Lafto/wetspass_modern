# WetSpass-M Modern (v2.4.1)

A spatially distributed monthly water balance model integrated into QGIS. It partitions rainfall into interception, surface runoff, actual evapotranspiration (AET), and groundwater recharge on a per-pixel basis, with advanced tools for MODFLOW river conductance estimation.

## Key Features

### 1. Diffusive Water Balance
*   **UNEP 5-Zone Climate Capping:** Automatically adjusts groundwater depth limits based on the Aridity Index (P/PET) to prevent infinite evaporation in arid regions. Includes a Smart Cache that clears automatically when settings change.
*   **Intelligent Auto-Cropping:** Clips input rasters to your project boundary before the math begins to prevent RAM crashes on country-scale grids.
*   **Resumable Operations:** Automatically resumes cropping and modeling from the exact block where it stopped. The "Run" button remains active during a paused state to allow starting fresh runs.
*   **Spin-Up Convergence:** Automatically stabilizes soil moisture. Default is set to 30 cycles (exits early on convergence). Includes a "Skip to Final Cycle" button that instantly aborts the current cycle for time-sensitive runs.

### 2. River-Aquifer Conductance (MODFLOW 6)
*   **Field-Anchored Geomorphic Scaling:** Uses field measurements from a single outlet to estimate riverbed conductance, depth, width, and stage for the entire river network.
*   **Outlet Expansion Index (OEI):** Prevents artificial over-estimation of upstream conductance by isolating the delta/lake expansion width.
*   **Stream Typology:** Applies a physical multiplier based on whether the stream is Perennial (1.0), Intermittent (0.8), or Ephemeral (0.5) to both Conductance and Stage.
*   **Gaining vs. Losing Classification:** Compares the calculated streambed elevation against the (smoothed) water table elevation to determine where focused recharge occurs.
*   **Geological Factor (\(G_i\)):** Allows reading a Geology raster (loaded in the Inputs tab) to adjust conductance based on underlying rock permeability.
*   **MODFLOW-Ready Exports:** Automatically generates a CSV table with Layer, Row, Column, Stage, Conductance, and River Bottom Elevation—ready for direct import into ModelMuse.

### 3. Focused Recharge
*   **Multi-Flow Direction (MFD) Engine:** Solves flat-area routing artifacts to ensure realistic, meandering stream networks.
*   **Total Recharge Combination:** Combines diffusive recharge and stream focused recharge into a single `Recharge_Total_Annual.tif` for Groundwater Potential (GWP) mapping.

## Requirements
This plugin requires the following Python libraries and external software, which are usually included in the standard OSGeo4W/QGIS installer:
*   `rasterio` (Raster I/O and resampling)
*   `geopandas` (Boundary polygon handling)
*   `scipy` (GWD smoothing and filters)
*   `numpy` (Matrix math)
*   **GRASS GIS** (Must be installed and activated inside QGIS for the River Conductance routing)

## Important Folders
*   `inputs/`: Your original raw data (including the optional Geology Map for the River Conductance module).
*   `outputs/`: The main diffusive model results.
*   `outputs/cropped_inputs/`: Auto-generated smaller maps used for the math.
*   `outputs/effective_gwdepth/`: The capped groundwater maps (if auto-capping is used).
*   `outputs/River_Conductance/`: The MODFLOW CSV tables and focused recharge rasters.

## Credits
*   **Original Physics:** K. Abdollahi, I. Bashir, and O. Batelaan.
*   **Modernization & QGIS Integration:** M. G. Gurmu (2026).
*   **AI Programming Assistant:** DeepSeek AI (used for programming the current scripts under careful human guidance).
