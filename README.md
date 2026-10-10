# WetSpass-M Modern

A modernized, QGIS-integrated implementation of the WetSpass-M
spatially distributed monthly water-balance model.

WetSpass-M Modern estimates diffuse and focused groundwater
recharge, evapotranspiration, surface runoff, and interception at
monthly, seasonal, and annual time steps. It preserves the physics
of the original Wetspass-M 2016 model (Abdollahi, Bashir & Batelaan,
Vrije Universiteit Brussel) while replacing the quasi-steady-state
soil assumption with a dynamic soil-water bucket, adding focused
recharge along ephemeral streams, and delivering the whole workflow
inside QGIS.

**Current version:** 2.1.0
**Author:** M. G. Gurmu &lt;mggurmu@gmail.com&gt;
**License:** Academic use — see [LICENSE.txt](LICENSE.txt)
**Repository:** https://github.com/Lafto/wetspass_modern

---

## What's new in 2.1.0

- **Crop outputs to boundary** — a robust replacement for the failing
  QGIS `gdal:rasterize` "Mask rasters" workflow, with a
  file-selection dialog, background-thread execution, and originals
  preserved.
- **Climate-zone-based physical cap** on total recharge, derived
  automatically from mean annual precipitation.
- **User-selectable combine method** for diffusive + focused
  recharge: Addition (default), Replace-by-max (conservative), or
  Focused-over-diffusive.
- **Preview cap effect** button that reports, without writing any
  file, how many cells a given cap would clip.
- **Rainfall-driven climate-regime auto-selection**.
- Fixes for three focused-recharge defects (broadcast shape
  mismatch, mass-balance overflow, duplicate `-ot` flag).

See [CHANGELOG.md](CHANGELOG.md) for the full history.

---

## Installation

### From the QGIS plugin repository

1. Open QGIS.
2. Go to **Plugins → Manage and Install Plugins → All**.
3. Search for *WetSpass-M Modern*.
4. Click **Install Plugin**.

### From source (for development or pre-release testing)

Copy the entire `wetspass_modern` folder into your QGIS plugin
directory:

- **Windows:** `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
- **Linux:** `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
- **macOS:** `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`

Then open QGIS, go to **Plugins → Manage and Install Plugins →
Installed**, and tick **WetSpass-M Modern**.

### Dependencies

- QGIS 3.28 or newer (tested through 3.44; listed for QGIS 4.x).
- Python 3.9 or newer (bundled with QGIS).
- Python packages:
  - `numpy`
  - `rasterio`
  - `geopandas` (required by the crop tool)

`numpy` and `rasterio` are commonly available in QGIS's Python
environment. `geopandas` may need to be installed separately. On
Windows, open the **OSGeo4W Shell** (installed with QGIS) and run:
