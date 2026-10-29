# Changelog

All notable changes to the **WetSpass-M Modern** project will be documented in this file.

## [2.4.1] - 2026-10-25

### Added
- **River-Aquifer Conductance Module:** New tab for MODFLOW 6 RIV package integration.
- **Field-Anchored Geomorphic Scaling:** Propagates outlet field measurements upstream.
- **Outlet Expansion Index (OEI):** Handles delta widening at lakes to prevent overestimation.
- **Stream Typology & Geology Inputs:** Optional multipliers for rock permeability and stream flow duration.
- **MODFLOW CSV Export:** Generates a ready-to-import `MODFLOW_Stream_Table.csv`.
- **Multi-Flow Direction (MFD) Engine:** Solves flat-area straight-line stream artifacts.
- **Instant "Skip to Final Cycle":** Aborts the current month/block immediately rather than waiting for the cycle to finish.
- **Smart GWD Cache Clearing:** Automatically deletes old capped rasters if UI toggles change.

### Changed
- Default maximum spin-up cycles increased from 7 to 30 (safe due to early convergence exit).
- The "Run" button is now always active, prompting the user to discard or resume if a checkpoint exists.
- UI updated with a Math Formula reference box, styled progress bars, and green Run button.

### Fixed
- **Mass Balance in Lake Catchments:** Resolved the negative closure error (e.g., -48 mm) in humid/highland areas by allowing raw GWD overrides.
- **Grid Alignment:** Fixed the `operands could not be broadcast together` error when combining Diffusive and Focused recharge.
- **Annual Recharge Units:** Fixed Focused Recharge math to output `mm/year` instead of `mm/day`.
- **Streambed Depth Bug:** Fixed an issue where GRASS segment IDs caused depth to explode to 43+ meters.
- **Bandit Security Warnings:** Refactored `try/except/pass` blocks to log errors properly.
- **Metadata Parsing:** Rewrote `metadata.txt` to fix QGIS ZIP installation errors.

## [2.2.0] - 2026-10-24
### Added
- UNEP 5-Zone Climate Capping (prevents infinite evaporation in arid zones).
- Intelligent Auto-Cropping to prevent RAM crashes.
- Resumable Operations (smart caching) for cropping and model math.
- Spin-up convergence tracking with an early exit tolerance.
- Global NaN & NoData Guard.

## [2.1.0] - 2026-10-XX
### Added
- Crop outputs to boundary tool.
- Physical cap on total recharge based on mean annual precipitation.
- User-selectable combine method for diffusive + focused recharge.
- Focused recharge module for streambed infiltration.
