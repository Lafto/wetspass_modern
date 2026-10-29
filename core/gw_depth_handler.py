# -*- coding: utf-8 -*-
"""
Soil- and climate-aware groundwater depth capping for WetSpass-M.

Universality
------------
The cap is computed per-pixel from:
  * Soil class  →  capillary rise height  (from Soil.TBL tension_ht)
  * Climate     →  PET/P ratio            (from rain + pet rasters)

This makes the capping valid for any environment from hyper-arid sandy
aquifers to humid clay catchments, without a global single-value cap.
"""

import contextlib
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

try:
    import rasterio
    from rasterio.warp import reproject, Resampling
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False

logger = logging.getLogger("WetSpassModern")

MIN_CAP_M = 0.5
MAX_CAP_M = 50.0
DEFAULT_CAP_M = 20.0     # fallback when data is incomplete
# Default climate fallback is semi-arid — this is the environment
# where deep water tables most strongly bias recharge estimates.
DEFAULT_CLIMATE = "semi-arid"
AUTO_TRIGGER_M = 5.0     # only cap if a meaningful fraction is deeper


# =============================================================================
# Soil → effective depth
# =============================================================================

def _read_soil_table(tables_dir: Path) -> Dict[int, float]:
    """
    Read Soil.TBL and return {soil_code: D_eff_m}.

    Capillary rise height is looked up by soil texture NAME
    (Clay, Sandy Loam, Clay Loam, ...), which is robust against the
    variable number of words in the soil name field. The tension
    column is used only as a fallback.

    Standard capillary rise heights by USDA texture class
    (Source: USBR Water Measurement Manual; FAO 56):
        water_body       0.00 m
        sand             0.10 m
        loamy sand       0.20 m
        sandy loam       0.30 m
        loam             0.40 m
        silt loam        0.50 m
        silt             0.60 m
        sandy clay loam  0.25 m
        clay loam        0.40 m
        silty clay loam  0.50 m
        sandy clay       0.30 m
        silty clay       0.50 m
        clay             0.40 m
    """
    p = tables_dir / "Soil.TBL"
    if not p.exists():
        return {}

    lines = []
    with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            parts = [c.strip() for c in
                     line.replace(",", "\t").split("\t") if c.strip()]
            if parts:
                lines.append(parts)
    if len(lines) < 2:
        return {}

    # Locate the TENSIONHHT column in the header (for fallback only)
    header = [h.lower() for h in lines[0]]
    tension_idx = -1
    for name in ("tensionhht", "tension_ht", "tension", "bubbling"):
        for i, h in enumerate(header):
            if name in h:
                tension_idx = i
                break
        if tension_idx >= 0:
            break
    if tension_idx < 0 and len(header) >= 9:
        tension_idx = 8

    # Standard capillary rise heights by soil texture name (m)
    # NOTE: keys use SPACES to match the actual soil names in
    # Soil.TBL (e.g., "sandy clay" not "sandy_clay").
    CAPILLARY_BY_NAME = {
        "water body":        0.00,
        "sand":              0.10,
        "loamy sand":        0.20,
        "sandy loam":        0.30,
        "loam":              0.40,
        "silt loam":         0.50,
        "silt":              0.60,
        "sandy clay loam":   0.25,
        "clay loam":         0.40,
        "silty clay loam":   0.50,
        "sandy clay":        0.30,
        "silty clay":        0.50,
        "clay":              0.40,
    }

    def _is_number(s):
        try:
            float(s)
            return True
        except (TypeError, ValueError):
            return False

    result = {}
    for row in lines[1:]:
        if len(row) < 2:
            continue
        try:
            code = int(float(row[0]))
        except ValueError:
            continue

        # Detect where the numeric block starts in this row
        numeric_start = None
        for i in range(1, len(row)):
            if _is_number(row[i]):
                numeric_start = i
                break
        if numeric_start is None:
            continue

        # Extract soil texture name (1-3 words between code and numbers)
        name_tokens = [t for t in row[1:numeric_start]]
        # Join with SPACE (matches names in Soil.TBL)
        name = " ".join(t.lower() for t in name_tokens)

        # Prefer the name-based lookup
        d_eff = None
        if name in CAPILLARY_BY_NAME:
            d_eff = CAPILLARY_BY_NAME[name]
        else:
            # Longest partial match wins ("sandy_clay_loam" before "clay")
            for key in sorted(CAPILLARY_BY_NAME.keys(),
                              key=len, reverse=True):
                if key in name:
                    d_eff = CAPILLARY_BY_NAME[key]
                    break

        # Fallback: read the tension column at its shifted position
        if d_eff is None:
            row_tension_idx = (tension_idx - 1) + numeric_start
            if 0 <= row_tension_idx < len(row):
                with contextlib.suppress(ValueError):
                    t_cm = float(row[row_tension_idx])
                    if t_cm > 0:
                        d_eff = 2.0 * t_cm / 100.0

        # Last resort: mid-range default
        if d_eff is None:
            d_eff = 0.35

        result[code] = d_eff
    return result

def _build_soil_depth_raster(
        soil_path: Path,
        soil_depths: Dict[int, float],
        ref_meta: dict) -> Optional[np.ndarray]:
    """
    Read the soil map, reproject to reference meta if needed, and build
    a per-pixel effective-depth array.
    """
    if not HAS_RASTERIO:
        return None
    try:
        with rasterio.open(str(soil_path)) as s:
            a = s.read(1)
            if (s.height, s.width) != (ref_meta["height"], ref_meta["width"]) \
               or s.crs != ref_meta["crs"] \
               or s.transform != ref_meta["transform"]:
                # Reproject into reference grid
                dst = np.full(
                    (ref_meta["height"], ref_meta["width"]),
                    np.nan, dtype=np.float32)
                reproject(
                    source=a, destination=dst,
                    src_transform=s.transform, src_crs=s.crs,
                    dst_transform=ref_meta["transform"], dst_crs=ref_meta["crs"],
                    resampling=Resampling.nearest)
                a = dst
            else:
                a = a.astype(np.float32)
    except Exception as exc:
        logger.warning("Could not read soil map: %s", exc)
        return None

    # Build depth raster
    # Start as NaN so NoData stays NaN (excluded from the mean later)
    depth = np.full(a.shape, np.nan, dtype=np.float32)
    for code, d_eff in soil_depths.items():
        depth[a == code] = d_eff
    # Any pixel that wasn't matched AND has no soil code (e.g. an
    # unexpected code) also stays NaN rather than defaulting to 20 m.
    return depth


# =============================================================================
# Climate factor
# =============================================================================



def _compute_climate_factor(maps_dir: Path, ref_meta: Optional[dict], months: list) -> Tuple[float, str, float]:
    """
    Computes the Aridity Index (P / PET) and classifies it into 5 UNEP zones:
    Hyper-Arid, Arid, Semi-Arid, Dry Sub-Humid, and Humid.
    """
    if not HAS_RASTERIO:
        return 1.0, "Unknown (no rasterio)", 1.0

    total_p = 0.0
    total_pet = 0.0
    valid_count = 0

    for m in months:
        rain_path = next((maps_dir / "rain" / f"rain{m}{ext}" for ext in (".tif", ".asc") if (maps_dir / "rain" / f"rain{m}{ext}").exists()), None)
        pet_path = next((maps_dir / "pet" / f"pet{m}{ext}" for ext in (".tif", ".asc") if (maps_dir / "pet" / f"pet{m}{ext}").exists()), None)

        if not rain_path or not pet_path:
            continue

        with rasterio.open(str(rain_path)) as src:
            rain = src.read(1).astype(np.float64)
            if src.nodata is not None:
                rain[rain == src.nodata] = np.nan
        with rasterio.open(str(pet_path)) as src:
            pet = src.read(1).astype(np.float64)
            if src.nodata is not None:
                pet[pet == src.nodata] = np.nan

        valid = np.isfinite(rain) & np.isfinite(pet) & (pet > 0)
        total_p += np.nansum(rain[valid])
        total_pet += np.nansum(pet[valid])
        valid_count += np.count_nonzero(valid)

    if valid_count == 0 or total_pet == 0:
        return 1.0, "Unknown (no data)", 1.0

    # Aridity Index (AI) = Precipitation / PET
    ai = total_p / total_pet

    # UNEP Aridity Zones
    if ai < 0.05:
        factor, label = 0.1, "Hyper-Arid"
    elif ai < 0.20:
        factor, label = 0.3, "Arid"
    elif ai < 0.50:
        factor, label = 0.6, "Semi-Arid"
    elif ai < 0.65:
        factor, label = 1.0, "Dry Sub-Humid"
    else:
        factor, label = 1.5, "Humid"

    return factor, label, (1.0 / ai if ai > 0 else 999.0)

    total_p = 0.0
    total_pet = 0.0
    valid_count = 0

    for m in months:
        rain_path = next((maps_dir / "rain" / f"rain{m}{ext}" for ext in (".tif", ".asc") if (maps_dir / "rain" / f"rain{m}{ext}").exists()), None)
        pet_path = next((maps_dir / "pet" / f"pet{m}{ext}" for ext in (".tif", ".asc") if (maps_dir / "pet" / f"pet{m}{ext}").exists()), None)

        if not rain_path or not pet_path:
            continue

        with rasterio.open(str(rain_path)) as src:
            rain = src.read(1).astype(np.float64)
            if src.nodata is not None:
                rain[rain == src.nodata] = np.nan
        with rasterio.open(str(pet_path)) as src:
            pet = src.read(1).astype(np.float64)
            if src.nodata is not None:
                pet[pet == src.nodata] = np.nan

        valid = np.isfinite(rain) & np.isfinite(pet) & (pet > 0)
        total_p += np.nansum(rain[valid])
        total_pet += np.nansum(pet[valid])
        valid_count += np.count_nonzero(valid)

    if valid_count == 0 or total_pet == 0:
        return 1.0, "Unknown (no data)", 1.0

    ai = total_p / total_pet

    if ai < 0.05:
        factor, label = 0.1, "Hyper-Arid"
    elif ai < 0.20:
        factor, label = 0.3, "Arid"
    elif ai < 0.50:
        factor, label = 0.6, "Semi-Arid"
    elif ai < 0.65:
        factor, label = 1.0, "Dry Sub-Humid"
    else:
        factor, label = 1.5, "Humid"

    return factor, label, (1.0 / ai if ai > 0 else 999.0)

    def _mean_decimated(path: Path, ref_meta: dict) -> Optional[float]:
        try:
            with rasterio.open(str(path)) as s:
                h = max(1, s.height // 8)
                w = max(1, s.width // 8)
                arr = s.read(1, out_shape=(h, w)).astype(np.float64)
                nd = s.nodata if s.nodata is not None else -9999.0
                arr[arr == nd] = np.nan
                valid = arr[np.isfinite(arr)]
                return float(valid.mean()) if valid.size else None
        except Exception:
            return None

    p_annual = 0.0
    n_p = 0
    for m in months:
        for sub, pre in (("rain", "rain"),):
            f = maps_dir / sub / f"{pre}{m}.tif"
            if not f.exists():
                f = maps_dir / sub / f"{pre}{m}.asc"
            if f.exists():
                v = _mean_decimated(f, ref_meta)
                if v is not None:
                    p_annual += v
                    n_p += 1
                    break
    if n_p == 0 or p_annual <= 0:
        return 1.0, "semi-arid (assumed)", 2.0

    pet_annual = 0.0
    n_pet = 0
    for m in months:
        for sub, pre in (("pet", "pet"),):
            f = maps_dir / sub / f"{pre}{m}.tif"
            if not f.exists():
                f = maps_dir / sub / f"{pre}{m}.asc"
            if f.exists():
                v = _mean_decimated(f, ref_meta)
                if v is not None:
                    pet_annual += v
                    n_pet += 1
                    break

    if n_pet == 0 or pet_annual <= 0:
        return 1.0, "semi-arid (assumed)", 2.0

    ratio = pet_annual / max(p_annual, 1.0)
    if ratio > 10.0:
        return 0.5, "hyper-arid", ratio
    if ratio > 5.0:
        return 0.7, "arid", ratio
    if ratio > 2.0:
        return 1.0, "semi-arid", ratio
    if ratio > 1.0:
        return 1.2, "dry sub-humid", ratio
    return 1.5, "humid", ratio


# =============================================================================
# Capping
# =============================================================================

def _cap_one_with_raster(src: Path, dst: Path,
                         cap_raster: Optional[np.ndarray],
                         cap_scalar: Optional[float]) -> bool:
    """
    Apply either a per-pixel cap raster (preferred) or a scalar cap.
    """
    try:
        with rasterio.open(str(src)) as s:
            a = s.read(1).astype(np.float32)
            nd = s.nodata
            mask = (a == nd) if nd is not None else np.zeros_like(a, dtype=bool)

            if cap_raster is not None and cap_raster.shape == a.shape:
                # Where cap is NaN, do not modify (keep raw gwdepth)
                finite_cap = np.isfinite(cap_raster)
                capped = a.copy()
                capped[finite_cap] = np.minimum(
                    a[finite_cap], cap_raster[finite_cap])
            elif cap_scalar is not None:
                capped = np.minimum(a, cap_scalar)
            else:
                capped = a.copy()

            capped = np.where(capped < MIN_CAP_M, MIN_CAP_M, capped)
            if nd is not None:
                capped[mask] = nd

            profile = s.profile.copy()
            profile.update(dtype="float32", compress="lzw")

        with rasterio.open(str(dst), "w", **profile) as d:
            d.write(capped, 1)
        return True
    except Exception as exc:
        logger.error("Failed to cap %s: %s", src.name, exc)
        return False


# =============================================================================
# Public entry point
# =============================================================================

def prepare_effective_gwdepth(working_dir: Path,
                              output_dir: Path,
                              user_cap: Optional[float] = None,
                              auto_cap: bool = True,
                              report_callback=None,
                              maps_dir: Optional[Path] = None,
                              tables_dir: Optional[Path] = None,
                              ref_meta: Optional[dict] = None,
                              months: Optional[list] = None) -> Optional[Path]:
    """
    Universal, soil- and climate-aware capping.

    If `user_cap` > 0 → single-value cap (override).
    Otherwise → per-pixel cap from soil map × climate factor.
    """
    def report(step, total, msg):
        logger.info("[%d/%d] %s", step, total, msg)
        if report_callback:
            try:
                report_callback(step, total, str(msg))
            except Exception as exc:
                logger.debug("report_callback failed: %s", exc)

    # Locate source folder
    candidates = [
        (maps_dir / "gwdepth") if maps_dir else None,
        working_dir / "inputs" / "maps" / "gwdepth",
        working_dir / "maps" / "gwdepth",
        working_dir / "Inputs" / "Maps" / "gwdepth",
        working_dir / "Maps" / "gwdepth",
    ]
    src_dir = next((c for c in candidates if c and c.is_dir()), None)
    if src_dir is None:
        return None
    src_maps = src_dir.parent
    src_tables = tables_dir or (working_dir / "inputs" / "tables")
    months = months or list(range(1, 13))

    # Reuse existing folder if complete
    dst_dir = output_dir / "effective_gwdepth"
    
    import json
    import shutil
    config_file = dst_dir / "_cap_config.json"
    expected_config = {"auto_cap": auto_cap, "user_cap": user_cap}
    
    # If settings have changed, delete the old cache
    if dst_dir.exists():
        config_mismatch = True
        if config_file.exists():
            try:
                with open(config_file, "r") as cf:
                    if json.load(cf) == expected_config:
                        config_mismatch = False
            except Exception as exc:
                logger.debug("Could not read cap config: %s", exc)
        
        if config_mismatch:
            report(0, 1, "GW depth: settings changed. Clearing old capped rasters...")
            shutil.rmtree(dst_dir)
        else:
            existing = sorted(dst_dir.glob("gwdepth*.tif"))
            if len(existing) == len(months):
                report(0, 1, f"GW depth: reusing {len(existing)} capped rasters.")
                return dst_dir

    # If capping is completely turned off, return None so the model uses the original maps
    if not auto_cap and (user_cap is None or user_cap <= 0):
        report(0, 1, "GW depth: auto-capping disabled — using raw rasters.")
        return None

    # Create the folder and save the new config
    dst_dir.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w") as cf:
        json.dump(expected_config, cf)

    # User override wins
    if user_cap is not None and user_cap > 0:
        cap_scalar = float(user_cap)
        cap_raster = None
        report(0, 1, f"GW depth: manual cap {cap_scalar:.1f} m.")
    elif not auto_cap:
        report(0, 1, "GW depth: auto-capping disabled — using raw rasters.")
        return None
    else:
        # Build per-pixel cap
        cap_raster, cap_scalar, explanation = _build_universal_cap(
            src_maps, src_tables, ref_meta, months, report)
        if cap_raster is None and cap_scalar is None:
            return None
        report(0, 1, explanation)

    # Apply
    rasters = sorted(
        list(src_dir.glob("gwdepth*.tif"))
        + list(src_dir.glob("gwdepth*.asc"))
    )
    dst_dir.mkdir(parents=True, exist_ok=True)
    n_ok = 0
    for r in rasters:
        if _cap_one_with_raster(r, dst_dir / r.name, cap_raster, cap_scalar):
            n_ok += 1
    if n_ok == 0:
        report(0, 1, "GW depth: capping failed — using raw rasters.")
        return None

    mode = ("per-pixel (soil × climate)"
            if cap_raster is not None
            else f"scalar {cap_scalar:.1f} m")
    report(0, 1, f"GW depth: {n_ok} raster(s) capped using {mode}.")
    return dst_dir


# =============================================================================
# Build the universal per-pixel cap
# =============================================================================

def _build_universal_cap(maps_dir: Path, tables_dir: Path,
                         ref_meta: Optional[dict], months: list,
                         report) -> Tuple[Optional[np.ndarray],
                                          Optional[float],
                                          str]:
    """
    Returns (cap_raster, cap_scalar, explanation).
    Only one of raster/scalar is non-None.
    """
    # Need reference grid — read from DEM if not provided
    if ref_meta is None:
        dem = None
        for ext in (".tif", ".asc"):
            c = maps_dir / f"dem{ext}"
            if c.exists():
                dem = c
                break
        if dem is None or not HAS_RASTERIO:
            return None, DEFAULT_CAP_M, \
                   "GW depth: no DEM available — falling back to 20 m cap."
        with rasterio.open(str(dem)) as s:
            ref_meta = {
                "height": s.height, "width": s.width,
                "crs": s.crs, "transform": s.transform,
            }

    # 1. Soil map → per-pixel effective depth
    soil_table = _read_soil_table(tables_dir)
    if not soil_table:
        return None, DEFAULT_CAP_M, \
               "GW depth: soil table unavailable — falling back to 20 m cap."

    soil_map = None
    for ext in (".tif", ".asc"):
        c = maps_dir / f"soil{ext}"
        if c.exists():
            soil_map = c
            break
    if soil_map is None:
        return None, DEFAULT_CAP_M, \
               "GW depth: soil map missing — falling back to 20 m cap."

    report(0, 1, "GW depth: reading soil map for per-pixel effective depth...")
    soil_depth = _build_soil_depth_raster(soil_map, soil_table, ref_meta)
    if soil_depth is None:
        return None, DEFAULT_CAP_M, \
               "GW depth: soil map read failed — falling back to 20 m cap."

    d_min = float(np.nanmin(soil_depth))
    d_max = float(np.nanmax(soil_depth))
    d_mean = float(np.nanmean(soil_depth))
    report(0, 1,
           f"GW depth: soil-based effective depth = "
           f"{d_mean:.2f} m mean "
           f"(range {d_min:.2f}–{d_max:.2f} m)")

    # 2. Climate factor is passed in as 'ai' and 'c_label'

    

    # 2. Climate factor (Moved inside to fix NameError)
    report(0, 1, "GW depth: computing PET/P climate factor...")
    c_factor, c_label, c_ratio = _compute_climate_factor(maps_dir, ref_meta, months)
    ai = 1.0 / c_ratio if c_ratio > 0 else 0.0  # Aridity Index (P/PET)
    report(0, 1, f"GW depth: climate = {c_label} (PET/P = {c_ratio:.2f}, factor = {c_factor:.2f}, AI = {ai:.2f})")

    # 3. Combine: per-pixel cap raster based on UNEP Aridity Zones

    if ai < 0.05:  # Hyper-Arid
        base_min_depth, base_max_depth = 100.0, 300.0
    elif ai < 0.20:  # Arid
        base_min_depth, base_max_depth = 30.0, 100.0
    elif ai < 0.50:  # Semi-Arid
        base_min_depth, base_max_depth = 10.0, 30.0
    elif ai < 0.65:  # Dry Sub-Humid
        base_min_depth, base_max_depth = 3.0, 10.0
    elif ai < 1.0:   # Humid
        base_min_depth, base_max_depth = 1.0, 5.0
    else:            # Very Humid / Per-Humid
        base_min_depth, base_max_depth = 0.5, 2.0

    # Enforce the climate limits, while respecting the soil's physical capillary limit
    cap_raster = np.maximum(soil_depth, base_min_depth)
    finite = np.isfinite(cap_raster)
    cap_raster[finite] = np.clip(cap_raster[finite], base_min_depth, base_max_depth)

    # Preserve NoData (NaN) values
    nan_mask = np.isnan(soil_depth)
    cap_raster[nan_mask] = np.nan

    mean_cap = float(np.nanmean(cap_raster))
    explanation = (
        f"GW depth: UNEP climate cap applied ({c_label}, AI={ai:.2f}). "
        f"Mean cap = {mean_cap:.1f} m."
    )
    return cap_raster.astype(np.float32), None, explanation
