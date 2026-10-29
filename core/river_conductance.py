# -*- coding: utf-8 -*-
import logging
import csv
import time
import numpy as np
import rasterio
from pathlib import Path
from qgis.PyQt.QtWidgets import QApplication

logger = logging.getLogger("WetSpassModern")

def calculate_conductance_and_recharge(dem_path, gwd_path, out_dir, params, boundary_path=None, stream_source='dem', stream_path=None, geology_path=None, progress_callback=None, max_daily_depth_mm=5.0):
    base_out_dir = Path(out_dir)
    out_dir = base_out_dir / "River_Conductance"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    def _report(step, total, msg):
        if progress_callback:
            progress_callback(step, total, msg)
            QApplication.processEvents()
            time.sleep(0.1) # Force UI update
        logger.info(f"[{step}/{total}] {msg}")
    
    _report(1, 5, "Reading inputs and calculating base parameters...")
    
    K0 = params['K0']; W0 = params['W0']; L0 = params['L0']; M0 = params['M0']
    D0 = params['D0']; Stage = params['Stage']
    W_out = params['W_out']; W_expected = params['W_expected']
    A0 = params['A0']; S0 = params['S0']; I0 = params['I0']
    a = params['a']; b = params['b']; c = params['c']; d = params['d']
    f = params['f']; g = params['g']
    type_multiplier_val = params.get('type_multiplier', 1.0)
    I_min = 1e-4
    
    OEI = W_out / W_expected if W_expected > 0 else 1.0
    C0 = (K0 * W0 * L0) / M0 if M0 > 0 else 0.0
    C0_star = C0 * (OEI ** c)
    
    _report(2, 5, "Computing hydrology (GRASS). QGIS may freeze for 1-2 mins...")
    
    # --- GRASS HYDROLOGY ---
    import processing
    temp_dir = out_dir / "_temp_conductance"
    temp_dir.mkdir(exist_ok=True)
    grass_alg = "grass7:r.watershed"
    try:
        processing.algorithmHelp(grass_alg)
    except:
        grass_alg = "grass8:r.watershed"

    filled_path = str(temp_dir / "filled.tif")
    acc_path = str(temp_dir / "acc.tif")
    
    processing.run("grass7:r.fill.dir", {
        'input': str(dem_path), 'format': 1, 'output': filled_path,
        'direction': str(temp_dir / "dir.tif"), 'areas': str(temp_dir / "areas.tif")
    })
    
    processing.run(grass_alg, {
        'elevation': filled_path, 'memory': 300,
        'accumulation': acc_path,
        'drainage': str(temp_dir / "fdir.tif"),
        'basin': str(temp_dir / "basin.tif"),
        'threshold': 500,
        'min_size': 1, 'max_slope_length': 0
    })
    
    _report(3, 5, "Applying geomorphic scaling and conducting boundary checks...")
    
    # --- READ RASTERS ---
    with rasterio.open(dem_path) as src:
        dem = src.read(1).astype(np.float32)
        meta = src.meta.copy()
        dem[dem == src.nodata] = np.nan
        
    with rasterio.open(gwd_path) as src:
        gwd = src.read(1).astype(np.float32)
        gwd[gwd == src.nodata] = np.nan
        
    with rasterio.open(acc_path) as src:
        acc = src.read(1)
        acc[acc == src.nodata] = 0

    # --- STRAHLER ORDER PROXY ---
    from .stream_derivation import order_proxy_from_accumulation
    stream_mask = acc >= 500
    S_i = order_proxy_from_accumulation(acc, stream_mask, 500).astype(np.float32)

    # --- GEOMORPHIC CALCS ---
    cell_size = abs(meta['transform'][0])
    cell_area_km2 = (cell_size * cell_size) / 1e6
    A_i = acc * cell_area_km2
    
    grad_y, grad_x = np.gradient(dem, cell_size)
    slope = np.sqrt(grad_x**2 + grad_y**2)
    I_i = np.maximum(np.where(np.isfinite(slope), slope, I_min), I_min)
    
    S0_safe = max(S0, 1.0)
    D_i = D0 * ((S_i / S0_safe) ** f)
    Stage_i = Stage * ((S_i / S0_safe) ** g) * type_multiplier_val
    
    # --- ELEVATION CHECKS ---
    Z_bed = dem - D_i
    Z_wt = dem - gwd
    Z_stage = Z_bed + Stage_i
    
    # Diagnostic log for Gaining/Losing
    mean_bed = np.nanmean(Z_bed[stream_mask])
    mean_wt = np.nanmean(Z_wt[stream_mask])
    logger.info(f"Diagnostic: Mean Bed Elev: {mean_bed:.2f}m | Mean Water Table Elev: {mean_wt:.2f}m")
    if mean_wt > mean_bed:
        logger.info("WARNING: Water table is ABOVE the streambed. Streams are Gaining (Recharge=0).")
    else:
        logger.info("Water table is BELOW streambed. Streams are Losing (Recharge > 0).")

    is_losing = (Z_bed > Z_wt) & stream_mask
    
    # --- GEOLOGY FACTOR ---
    G_i = np.ones_like(A_i, dtype=np.float32)
    if geology_path and Path(geology_path).exists():
        _report(3, 5, "Reading and applying Geological Factor raster...")
        with rasterio.open(geology_path) as src:
            # Assuming geology raster is already aligned to DEM
            geo_arr = src.read(1).astype(np.float32)
            geo_arr[geo_arr == src.nodata] = 1.0
            if geo_arr.shape == G_i.shape:
                G_i = geo_arr
            else:
                logger.warning("Geology raster shape doesn't match DEM. Using default G_i=1.0")

    # --- CONDUCTANCE ---
    A0_safe = max(A0, 1e-6); I0_safe = max(I0, I_min)
    C_i = np.zeros_like(A_i, dtype=np.float64)
    
    term_L = L_i = np.full_like(A_i, cell_size) / L0
    term_A = (A_i / A0_safe) ** b
    term_S = (S_i / S0_safe) ** a
    term_I = (I0_safe / I_i) ** d
    
    valid_stream = stream_mask & np.isfinite(dem)
    C_i[valid_stream] = C0_star * term_L[valid_stream] * term_A[valid_stream] * term_S[valid_stream] * term_I[valid_stream] * type_multiplier_val * G_i[valid_stream]
    
    
    
    # 1. Calculate Daily Recharge
    
    # 2. Convert to ANNUAL Recharge using Flow Duration (Days)
    flow_days = params.get('flow_days', 30.0)
    
    # Correct physics: Volume (m3) -> Depth (mm)
    # Volume = Conductance (m3/d) * Head Difference (m) * Flow Days
    # Depth (mm) = (Volume / Cell Area m2) * 1000
    cell_area_m2 = params.get('cell_size', 1000.0) ** 2
    
    # Calculate Head Difference (m)
    head_diff = np.maximum(0, Z_stage - Z_wt)

    # Calculate Recharge Volume (m3/day) on losing stream cells
    Q_recharge = np.zeros_like(C_i)
    Q_recharge[is_losing] = C_i[is_losing] * head_diff[is_losing]

    volume_m3 = Q_recharge * flow_days
    Recharge_mm_year = (volume_m3 / cell_area_m2) * 1000.0
    
    # Optional: Apply a safety cap (e.g., 50 mm/day max) to prevent infinite values
    safety_cap_mm = 50.0 * flow_days
    Recharge_mm_year = np.minimum(Recharge_mm_year, safety_cap_mm)
    
    # Prepare DataFrame for output
    
    # Recharge (m3/d) = Conductance * Head Difference
    _report(4, 5, "Cropping to boundary and saving spatial rasters...")
    
    # --- CROP TO BOUNDARY (Set -9999 NoData) ---
    if boundary_path and Path(boundary_path).exists():
        import geopandas as gpd
        from rasterio.mask import mask as rio_mask
        gdf = gpd.read_file(boundary_path)
        with rasterio.open(dem_path) as src:
            if gdf.crs != src.crs:
                gdf = gdf.to_crs(src.crs)
            out_image, _ = rio_mask(src, gdf.geometry, crop=False, nodata=-9999.0, all_touched=True)
            valid_mask = (out_image[0] != -9999.0)
            
            C_i = np.where(valid_mask, C_i, -9999.0)
            D_i = np.where(valid_mask, D_i, -9999.0)
            is_losing_out = np.where(valid_mask, is_losing.astype(np.float32), -9999.0)
            stream_mask = stream_mask & valid_mask
    else:
        is_losing_out = is_losing.astype(np.float32)

    meta.update({"dtype": "float32", "nodata": -9999.0})
    
    def save_raster(data, name):
        with rasterio.open(out_dir / name, "w", **meta) as dst:
            dst.write(data.astype(np.float32), 1)
            
    save_raster(C_i, "Stream_Conductance.tif")
    save_raster(D_i, "Stream_Depth.tif")
    save_raster(is_losing_out, "Stream_Status_Losing.tif")
    save_raster(Recharge_mm_year, "Stream_Focused_Recharge_Annual.tif")
    
    _report(5, 5, "Writing MODFLOW CSV table...")
    
    # --- SAVE CSV ---
    csv_out = out_dir / "MODFLOW_Stream_Table.csv"
    with open(csv_out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Layer", "Row", "Col", "Stage_Elev_m", "Cond_m2_day", "Rbot_Elev_m", "Gaining_Losing", "Recharge_mm_year", "Strahler", "Area_km2", "Slope", "Width_m", "Depth_m"])
        rr, cc = np.where(stream_mask)
        for r, c in zip(rr, cc):
            status = "Gaining" if not is_losing[r,c] else "Losing"
            writer.writerow([
                1, r, c, f"{Z_stage[r,c]:.3f}", f"{C_i[r,c]:.3f}", f"{Z_bed[r,c]:.3f}",
                status, f"{Recharge_mm_year[r,c]:.4f}", int(S_i[r,c]), f"{A_i[r,c]:.3f}", 
                f"{I_i[r,c]:.5f}", f"{W_out * ((S_i[r,c]/S0_safe)**a):.2f}", f"{D_i[r,c]:.3f}"
            ])

    # Cleanup temp
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)
    _report(5, 5, "Finished!")
    return str(out_dir)
