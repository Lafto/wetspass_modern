# WetSpass-M Modern

**A spatially distributed monthly water balance model — modernized for QGIS with a dynamic soil-water bucket.**

[![QGIS](https://img.shields.io/badge/QGIS-3.28%2B-blue)](https://qgis.org)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green)](https://python.org)
[![License](https://img.shields.io/badge/License-Academic-orange)](LICENSE.txt)
[![Version](https://img.shields.io/badge/version-2.0.0-informational)](https://github.com/your-repo/wetspass-modern)

---

## Overview

WetSpass-M Modern is a modernized version of the original **Wetspass-M 2016** model (Abdollahi, Bashir & Batelaan, Vrije Universiteit Brussel) — a spatially distributed monthly water balance model for estimating groundwater recharge, evapotranspiration, surface runoff, and interception.

The modernization preserves the original physics but adds a **dynamic soil-water bucket**, replaces the original ASCII-grid I/O with **GeoTIFF**, and integrates the model as a **native QGIS plugin** with an interactive GUI. It ships with a fully documented **water balance panel**, automatic **spin-up convergence detection**, and a **Wetspass-M 2016 compatibility mode** for validation against legacy results.

The model runs on **any climate, land use, or soil type**, requiring only 12 monthly climate rasters and three static maps (DEM, land use, soil).

---

## Key improvements over Wetspass-M 2016

| Feature | Wetspass-M 2016 | WetSpass-M Modern |
|---|---|---|
| **Soil moisture** | Reset to zero every month (ΔS ≡ 0) | Dynamic bucket, carried forward |
| **ET cap** | At rainfall | At (rainfall − runoff) |
| **Groundwater-fed ET** | Folded into AET | Reported separately as `Cell_gw_discharge` |
| **Time integration** | Single pass | Spin-up + reported pass with automatic convergence |
| **I/O format** | ASCII grids | GeoTIFF (legacy `.asc` still supported) |
| **Aggregation** | Simple sum | Nodata-preserving, valid-cell-mask aligned |
| **Environment** | Standalone Windows GUI | QGIS plugin with tabbed interface |
| **Water balance** | Manual post-processing | Interactive panel with mass balance closure check |
| **Compatibility** | — | Toggle to reproduce 2016 physics exactly |

The dynamic soil-water bucket addresses a well-known limitation of the 2016 model: in seasonally-concentrated rainfall regimes (monsoonal, Mediterranean, semi-arid), the quasi-steady-state assumption (ΔS ≡ 0) biases annual recharge by 5–15 % and misplaces 30–60 % of the seasonal distribution. WetSpass-M Modern captures the seasonal memory of the soil column by carrying soil moisture as an explicit state variable.

---

## Features

### For hydrologists

- **Diffusive recharge** estimation at monthly, seasonal, and annual resolution
- **Focused (streambed) recharge** module for ephemeral stream networks
- **Soil-water bucket** with automatic spin-up and convergence detection
- **Mass balance closure check** with explicit ΔS term and acceptability verdict
- **Wetspass-M 2016 compatibility mode** for validation against legacy runs
- **Catchment-scale discharge** (surface runoff + baseflow) via linear routing

### For developers

- Pure Python 3.9+, no compiled extensions
- QGIS Processing integration — usable in scripts and batch workflows
- Block-streaming raster engine — scales to grids of 100+ million cells
- Automatic raster alignment — inputs at different CRS, resolution, or extent are warped on-the-fly
- Pause/resume with checkpoint files
- Nodata-preserving by design

---

## Installation

### Requirements

- QGIS 3.28 or newer
- Python 3.9+ (bundled with QGIS)
- `numpy`, `rasterio`

### Quick install

1. Copy the plugin folder to your QGIS plugins directory:

   ```
   Windows:  %APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\
   Linux:    ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/
   macOS:    ~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/
   ```

2. Restart QGIS. The plugin appears in the **Plugins** menu as *WetSpass-M Modern*.

3. Install Python dependencies if needed:

   ```bash
   pip install numpy rasterio
   ```

4. (Optional) Place the user guide PDF at `help/WetSpassM_UserGuide.pdf` to enable the **Help** tab's PDF launcher.

---

## Quick start

```
1. Organize inputs:
   MyProject/
   ├── inputs/
   │   ├── maps/
   │   │   ├── dem.tif
   │   │   ├── landuse.tif
   │   │   ├── soil.tif
   │   │   ├── rain/rain1.tif … rain12.tif
   │   │   ├── pet/pet1.tif … pet12.tif
   │   │   ├── temp/temp1.tif … temp12.tif
   │   │   ├── wind/wind1.tif … wind12.tif
   │   │   └── gwdepth/gwdepth1.tif … gwdepth12.tif
   │   └── tables/
   │       ├── Landuses.TBL
   │       ├── Soil.TBL
   │       └── RainyDaysPerMonth.TBL
   └── outputs/

2. In QGIS: WetSpass-M Modern → Open
3. Inputs tab: set working directory, click Auto-load
4. Check Inputs tab: verify every row is green
5. Parameters tab: defaults match Wetspass-M 2016 (LP=0.85, Alfa=1.5)
6. Run tab: set output directory, leave Spin-up cycles = 2
7. Press Run
8. Water Balance tab: click Load from output folder
```

Expected run time for a 100-million-cell grid on a modern workstation: **2–4 hours**, depending on spin-up convergence.

---

## Model overview

### Governing equations

Per grid cell, per month:

```
Interception          I     = f(LAI, rainfall, rainy days)
Surface runoff        SR    = f(rainfall, slope, land use, soil, I)
Available to soil     A     = S_prev + P − I − SR − ET_impervious
Soil-derived ET       E_s   = min(E_s_potential, A)
Storage capacity      S_max = (fc − wp) · rootdepth · 1000     [mm]
Recharge              R     = max(0, A − E_s − S_max)          [overflow]
Soil moisture         S     = clamp(A − E_s, 0, S_max)         [state t+1]
Actual ET to atm.     AET   = I + E_s + ET_impervious
Storage-fed ET        Q_gw  = ET_gw_transp + ET_gw_evapo + ET_open_water
```

### Mass balance

```
P = AET + SR + R_total + ΔS
```

Where `ΔS = S(t+1) − S(t)`. On a spun-up run, the annual mean ΔS ≈ 0 and the closure error collapses to numerical round-off.

### Spin-up

Because soil moisture is a state variable, the model runs the same 12 monthly input maps repeatedly until the bucket reaches a repeating annual cycle. After each cycle, the mean absolute change in soil moisture is computed:

```
mean |S_end − S_start|     [mm/yr]
```

If this falls below `max(1.0 mm, 0.5 % × mean annual P)`, the bucket has converged and the remaining warm-up cycles are skipped automatically. A permanent record is written to `spinup_convergence.csv`.

Recommended spin-up by climate:

| Climate | Cycles |
|---|---|
| Maritime temperate | 1 |
| Continental / Mediterranean | 2 |
| Monsoonal | 2–3 |
| Arid / semi-arid | 3–5 |

---

## Output

All rasters are GeoTIFF with LZW compression. Monthly, seasonal, and annual aggregations are produced for every flux. Key outputs:

| Raster | Meaning | Units |
|---|---|---|
| `Recharge_diffusive_annual.tif` | Diffuse recharge | mm/yr |
| `Cell_evapotranspiration_annual.tif` | AET (precipitation-derived, includes interception) | mm/yr |
| `Cell_runoff_annual.tif` | Surface runoff | mm/yr |
| `Interception_annual.tif` | Interception | mm/yr |
| `Cell_gw_discharge_annual.tif` | Groundwater + open-water ET (diagnostic) | mm/yr |
| `soilwater_storage_initial.tif` | Bucket state at end of run | mm |
| `spinup_convergence.csv` | Convergence history | — |
| `Simulated.tbl` | Per-month catchment means | — |

When the Focused Recharge module is run, additional rasters are produced:

| Raster | Meaning |
|---|---|
| `Recharge_focused_annual.tif` | Streambed infiltration |
| `Recharge_Total_annual.tif` | Diffusive + focused recharge |

---

## Compatibility mode

Tick **Wetspass-M 2016 compatibility mode** on the Run tab to reproduce the original 2016 physics exactly:

- Soil moisture reset to zero every month (ΔS ≡ 0 internally)
- Soil-derived ET capped at rainfall, not (P − runoff)
- Groundwater-fed ET folded into `Cell_evapotranspiration`
- Recharge-driven ET adjustment reapplied, so `wb_error` matches the original

Cell-by-cell differences against a legacy Wetspass-M 2016 installation should be at the level of floating-point round-off.

---

## Documentation

- **About tab** — full scientific justification, governing equations, references
- **Help tab** — quick reference, output folder structure, common errors and fixes
- **PDF user guide** — place at `help/WetSpassM_UserGuide.pdf`; opens from the Help tab

---

## Citation

If you use WetSpass-M Modern in a publication, please cite the original authors **and** acknowledge the modernization:

**Original model:**

> Abdollahi, K., Bashir, I., & Batelaan, O. (2016).
> *WetSpass-M: spatially distributed monthly water balance model.*
> Vrije Universiteit Brussel.

> Batelaan, O., & De Smedt, F. (2001).
> WetSpass: a flexible, GIS-based, distributed recharge methodology for
> regional groundwater modelling. *IAHS Publication*, 269, 11–18.

**Modernization:**

> Gurmu, M. G. (2026). *WetSpass-M Modern* (Version 2.0.0) [Computer software].
> https://github.com/Lafto/wetspass-modern

The license requires co-authorship to Abdollahi, Bashir, and Batelaan in any publication that uses the model.

---

## License

Academic-use license. See [LICENSE.txt](LICENSE.txt) for details. The original Wetspass-M model is available upon request for academic use. For commercial use, permission should be obtained from the original authors.

---

## Credits

**Original model (2016):**
- K. Abdollahi
- I. Bashir
- O. Batelaan

Dept. of Hydrology and Hydraulic Engineering,
Vrije Universiteit Brussel, Belgium

**Modernization (2026):**
- M. G. Gurmu — mggurmu@gmail.com/melese.geleta@ati.gov.et

---

## Contributing

Issues, bug reports, and pull requests are welcome on GitHub. When reporting a bug, please include:

- QGIS version and operating system
- The Progress log tail from the failed run
- The Water Balance panel screenshot (if applicable)
- The contents of `spinup_convergence.csv` (if applicable)

---

## Related work

- [SWAT](https://swat.tamu.edu/) — Soil and Water Assessment Tool, source of the bucket formulation
- [HBV](https://www.smhi.se/en/research/research-departments/hydrology/hbv-model-1.7994) — Hydrologiska Byråns Vattenbalansavdelning, soil-moisture routine
- [QGIS](https://qgis.org) — the GIS platform this plugin runs on

---

**Made for hydrologists, hydrogeologists, and graduate students working on regional water balance studies in any climate.**
