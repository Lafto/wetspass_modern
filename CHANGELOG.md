# Changelog

All notable changes to **WetSpass-M Modern** are documented in this
file.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

Nothing yet.

---

## [2.1.0] — 2026-10-10

### Added

#### Crop outputs to boundary (new tool)
- Replaces the failing QGIS `gdal:rasterize` "Mask rasters"
  workflow, which had become unusable on recent QGIS builds:
  - `ERROR 1: Duplicate argument -ot` on some builds.
  - Silent no-output when the boundary CRS differed from the
    raster's.
  - Silent no-output when the boundary did not spatially overlap
    the raster bounding box.
- Uses `rasterio` + `geopandas` instead of the GDAL command-line
  layer, so CRS reprojection and boundary-pixel retention work
  correctly.
- File-selection dialog listing every `.tif` in the output folder,
  grouped by variable and period, with:
  - A live **Filter** box.
  - **Select all / Select none / Invert** buttons.
  - **Only annual** — ticks only `*_annual.tif`.
  - **Only Recharge** — ticks only `Recharge_*.tif`.
  - A running counter: *"N of M files selected"*.
- Auto-reprojects the boundary vector to each raster's CRS. Cached
  per unique CRS, so reprojection runs once per projection, not
  once per file.
- Uses `all_touched=True` to retain edge pixels. Critical for
  hydrological water-balance closure.
- Writes cropped copies to `output_dir/cropped_to_boundary/`.
  **Original rasters are never modified.**
- Runs on a background `QThread`. Live progress and per-file
  logging. A failed file does not stop the rest of the queue.

#### Climate-zone-based physical cap on total recharge
- **Recompute from inputs** button reads `rain1.tif` … `rain12.tif`
  from the working directory, computes mean annual precipitation,
  and classifies it against the standard reference table:

  | P (mm/yr) | Recharge fraction | Max plausible recharge |
  |---|---|---|
  | 100 – 300 | 1 – 5 % | 5 – 15 mm/yr |
  | 300 – 700 | 5 – 15 % | 15 – 100 mm/yr |
  | 700 – 1500 | 20 – 35 % | 150 – 500 mm/yr |
  | 1500 – 3000 | 30 – 50 % | 450 – 1500 mm/yr |
  | 3000 – 5000 | 40 – 60 % | 1200 – 3000 mm/yr |

- Recommended cap = upper bound of the plausible recharge fraction
  for the matched zone (`frac_max × mean_annual_P`). Set at the
  upper bound rather than the midpoint so the cap only clips
  genuinely implausible values.
- **Optional override** with a user-entered custom value.
- **Preview cap effect** button runs a read-only scan over the
  combine inputs and reports, without writing any file:
  - Number of valid cells.
  - **Before cap**: min / mean / max.
  - **After cap**: min / mean / max.
  - **Clipping summary**: cells clipped and mean drop across all
    cells.
  - **Distribution of clipped values** binned by ratio to the cap
    (1–1.5×, 1.5–2×, 2–5×, 5–10×, 10–50×, 50–100×, 100×+).

#### User-selectable combine method
- **Addition (default)** — `R_total = R_diffusive + R_focused`.
  Correct when the two mechanisms are physically independent
  pathways, which is the standard WetSpass-M assumption.
- **Replace-by-max (conservative)** —
  `R_total = max(R_diffusive, R_focused)`. This was the previous
  fixed behavior; preserved as an option.
- **Focused-over-diffusive** —
  `R_total = R_focused` on stream cells, `R_diffusive` elsewhere.
  Only for users who believe the diffusive model already includes
  some streambed loss.

#### Optional capped focused raster output
- New checkbox **"Also write capped focused raster"** writes
  `Recharge_focused_annual_capped.tif` alongside the total when
  the cap is enabled.
- The original `Recharge_focused_annual.tif` is never overwritten.

#### Rainfall-driven climate-regime auto-selection
- The climate-regime radio group is auto-selected after
  **Recompute from inputs**:

  | Mean annual P | Regime selected |
  |---|---|
  | < 300 mm/yr | Arid |
  | 300 – 700 | Semi-arid |
  | 700 – 1200 | Mixed / transitional |
  | ≥ 1200 | Humid |

- The **Auto-detect from gwdepth** button is relabelled
  **"Auto-detect from gwdepth (secondary)"** and remains available
  as a cross-check for users who trust their interpolated
  depth-to-water-table rasters.

### Fixed

- **Focused recharge** — `operands could not be broadcast together`
  error when the soil-derived Ks raster had a different shape from
  the D8 hydrology grid. The soil raster is now resampled to the
  D8 grid before the Ks lookup.
- **Focused recharge** — mass-balance overflow. A streambed cell
  can no longer infiltrate more water than is physically available
  at that cell. This removes the 12,000+ mm/yr outliers that
  previously contaminated `Recharge_Total_annual.tif` and drove
  `wb_error_annual.tif` to values as extreme as −12,000 mm/yr.
- **Focused recharge** — annual depth cap. The annual focused
  raster now respects `max_daily_depth_mm × flow_days` per month
  before summing.
- **Channel-width rasterization** — removed the duplicate `-ot`
  flag from the `gdal:rasterize` call. Eliminates
  `ERROR 1: Duplicate argument -ot` on all QGIS builds.
- **All modules** — replaced every remaining `try/except/pass` and
  `try/except/continue` block with `contextlib.suppress` or an
  explicit early return. Full Bandit scan reports
  **No issues identified** across 11,184 lines of code.
- **Water balance tab** — the closure note now explicitly states
  that `Cell_gw_discharge`, `Cell_ow_evaporation`, and
  `Cell_imper_evaporation` are components *inside* AET and are not
  subtracted again in the closure equation.

### Documentation

- User guide updated to version 2.1.0:
  - New chapter **"Crop outputs to boundary"**.
  - Rewritten **Focused Recharge** chapter covering the three
    combine methods, the climate-based cap, and the preview tool.
  - New troubleshooting entries for the crop tool and the
    climate-cap workflow.
  - Updated file listing to include `core/cropper.py`.
- `CHANGELOG.md` (this file) added to the plugin folder.
- `README.md` refreshed for 2.1.0.

### Changed

- The default combine method changed from **replace-by-max** to
  **addition**. To reproduce 2.0.5 behavior, select
  **Replace-by-max** in the Focused Recharge tab before clicking
  **Combine → Total recharge**.
- The `LICENSE` file was renamed from `LICENSE.txt` to `LICENSE`
  (no extension) to satisfy the QGIS plugin repository validator.

### Removed

- `convert_icon.py` and `convert_png_to_icon.py` — one-off
  development scripts, not part of the plugin runtime.

### Notes

- No changes to the diffusive model physics. Diffusive outputs
  for a given input dataset and starting state are identical to
  version 2.0.5.
- Checkpoints from 2.0.5 are not compatible with 2.1.0 and will be
  discarded on resume. Run a fresh simulation or delete
  `wetspass_checkpoint.json` from the output folder.

---

## [2.0.5] — 2026-10-06

### Fixed

- A disk error (external drive disconnect, power loss, or
  unexpected shutdown) could overwrite the good checkpoint with a
  stale one that said "resume from month 1", forcing a full
  restart on Resume. The workflow now preserves the checkpoint
  written after the last completed month, so Resume picks up at
  the exact month and block where it stopped.
- Duplicate `[Check Inputs]` log lines. Auto-checks triggered by
  switching to the Check Inputs tab or changing the working
  directory no longer write to the run log; only an explicit
  **Check Inputs** click does.
- Cancel button was inactive during a run. Cancel and Pause are
  now enabled as soon as the worker starts.
- Pause checks the pause flag between individual block writes as
  well as at the start of each block, so the run stops within one
  write instead of waiting for a whole block to complete.
- Pass 1 (static lookups) no longer rebuilds on every resume when
  the user supplies their own slope raster on the Inputs tab. The
  workflow now correctly recognises the supplied slope raster and
  skips the redundant Pass 1 rebuild.
- `Simulated.tbl` is now appended to, rather than truncated, when
  a run is resumed. Months written before the pause are preserved
  in the final file.
- The status bar no longer duplicates the full log line; it shows
  a short summary of the most recent message.

### Added

- Read-side retries. A transient input-drive disconnect during a
  read is now retried up to three times before the run pauses
  cleanly.
- **Skip to final cycle** button on the Progress tab. Once the
  soil-water bucket has essentially converged, press it to
  abandon the remaining spin-up cycles and jump straight to the
  final output cycle.
- A 5 % tolerance margin to the convergence test, so residuals
  sitting on the numerical noise floor are recognised as
  converged. Prevents the workflow from running an extra spin-up
  cycle when the bucket is already at equilibrium.
- **Resuming from checkpoint** line to the Progress log so the
  resume position is visible.

### Changed

- QGIS 4 readiness: `qgisMaximumVersion` raised to 4.99, so the
  plugin is listed on the QGIS 4 Ready Plugins page.
- Full Qt6 compatibility: all Qt enums are fully scoped. Loads
  cleanly in Qt5 and Qt6 without deprecation warnings.

### Cleanup

- Replaced all remaining `try/except/pass` and
  `try/except/continue` blocks with `contextlib.suppress` or a
  small helper function across `wetspass_plugin.py`,
  `ui/main_dialog.py`, `ui/water_balance_tab.py`, `ui/worker.py`,
  `core/raster_io.py`, `core/stream_io.py`, and
  `core/workflow.py`. Fixes Flake8 B110 and B112.
- Renamed the ambiguous loop variable `l` to `line` in
  `ui/water_balance_tab.py`. Fixes Flake8 E741.
- Replaced `subprocess.Popen` with `QDesktopServices.openUrl` for
  the "Show in folder" button. Fixes Bandit B603/B607 without
  needing suppression comments.
- Fixed `metadata.txt` continuation-line indentation and escaped
  literal percent signs so the repository parser accepts the file.

### Notes

- No functional or numerical changes to the model physics. Model
  results are identical to 2.0.2 for the same inputs and starting
  state.

---

## [2.0.0] — 2026-08-XX

### Added

- First public release.
- **Dynamic soil-water bucket** replacing the quasi-steady-state
  assumption of the original 2016 model. Soil moisture is carried
  as an explicit state variable from one month to the next, so the
  seasonal redistribution of water through the root zone is
  preserved.
- **ET cap** consistent with mass balance: soil-derived ET is
  capped at `(P − SR)` rather than `P`. Prevents negative
  recharge on high-runoff cells.
- **Cell_gw_discharge** reported separately from AET, so
  groundwater-fed ET is not double-counted in the closure test.
- **QGIS plugin** with GeoTIFF I/O. Legacy ASCII `.asc` grids are
  still accepted.
- **Robust checkpointing and resume.** The workflow saves state
  after every completed month and every completed spin-up cycle.
- **Sample data** shipped with the plugin.
- **Focused recharge module** for ephemeral and intermittent
  stream networks (D8 routing, transmission losses, losing vs
  gaining classification).
- **Compatibility mode** to reproduce Wetspass-M 2016 results
  exactly for validation against legacy runs.
- **Water Balance tab** with explicit ΔS and an acceptability
  verdict.
- **Calibration tab** computing NSE, KGE, PBIAS, R², RMSE, and
  MAE between observed data and `Simulated.tbl`.

---

## Versioning policy

- **MAJOR** — incompatible changes to input requirements, output
  variable names, or the water-balance formulation.
- **MINOR** — new features that are backward compatible.
- **PATCH** — backward-compatible bug fixes and documentation
  updates.

---

[Unreleased]: https://github.com/Lafto/wetspass_modern/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/Lafto/wetspass_modern/compare/v2.0.5...v2.1.0
[2.0.5]: https://github.com/Lafto/wetspass_modern/compare/v2.0.0...v2.0.5
[2.0.0]: https://github.com/Lafto/wetspass_modern/releases/tag/v2.0.0