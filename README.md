# WetSpass-M Modern (QGIS Plugin)

Spatially distributed monthly water balance model — recharge, evapotranspiration,
runoff, interception — modernized from WetSpass-M 2016.

## Highlights

- **Same physics**, cell-by-cell identical results to WetSpass-M 2016
- **GeoTIFF I/O** (with legacy `.asc` fallback)
- **QGIS Processing** algorithm — batch-friendly, no GUI dialogs required
- **Cloud-Optimized GeoTIFF** ready output

## Install

1. Copy this whole folder to your QGIS Python plugins directory:
   - Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
   - Linux:   `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
   - macOS:   `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`

2. Install Python dependencies into QGIS's Python environment:

   ```bash
   pip install numpy rasterio