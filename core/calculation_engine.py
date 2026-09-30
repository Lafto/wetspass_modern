# -*- coding: utf-8 -*-
"""
WetSpass-M Modern — Core Calculation Engine.

Two modes:

  steady_state_mode = False  (default) — dynamic bucket-soil model
      S is a state carried month to month.
      P = AET + SR + R + ΔS   (ΔS ≠ 0 in general)

  steady_state_mode = True — Wetspass-M 2016 compatibility
      S is reset to zero every month.
      ET capped at rainfall (not rainfall − runoff).
      GW-fed ET folded into Cell_evapotranspiration.
      P = AET + SR + R + wb_error; wb_error ≈ 0 by the model's own
      recharge adjustment.

Defaults LP = 0.85, Alfa = 1.5 (original GUI).
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger("WetSpassModern")


@dataclass
class WetSpassParameters:
    a_interception: float = 4.5
    alfa: float = 1.5
    w_slope: float = 0.4
    w_landuse: float = 0.3
    w_soil: float = 0.3
    x_coef: float = 0.5
    lp: float = 0.85
    intensity: float = 4.0
    beta: float = 0.75
    contribution: float = 0.5
    base_temp: float = 0.0
    melt_factor: float = 0.02
    snow_density: float = 0.1
    area_km2: float = 10.0
    q0_surface: float = 0.0
    q0_base: float = 0.0
    steady_state_mode: bool = False   # Wetspass-M 2016 compatibility

    @property
    def intensity_coef(self) -> float:
        return 24.0 / max(self.intensity, 1e-6)

    def to_coefficients_list(self) -> List[str]:
        return [
            str(self.a_interception), str(self.alfa), str(self.w_slope),
            str(self.w_landuse), str(self.w_soil), str(self.x_coef),
            str(self.lp), str(self.intensity_coef), str(self.beta),
            str(self.contribution), str(self.base_temp), str(self.melt_factor),
            str(self.snow_density),
        ]


class WetSpassCalculator:

    def __init__(self, parameters: WetSpassParameters,
                 rainy_days: Optional[List[float]] = None):
        self.params = parameters
        self.rainy_days = rainy_days if rainy_days else [10.0] * 12
        self.coefficients = parameters.to_coefficients_list()
        self.snow_store: Optional[np.ndarray] = None
        self.qt_1 = 0.0
        self.qbt_1 = 0.0

    @staticmethod
    def _safe_div(num, den, fill=0.0):
        num = np.asarray(num, dtype=np.float64)
        den = np.asarray(den, dtype=np.float64)
        out = np.full(np.broadcast(num, den).shape, fill, dtype=np.float64)
        mask = np.abs(den) > 1e-12
        np.divide(num, den, out=out, where=mask)
        return out

    def calculate_gamma(self, dem):
        base = 0.5988 - (0.0000133 * dem)
        return np.power(np.maximum(base, 0.0), 5.23)

    def calculate_slope(self, dem, cellsize: float = 1.0):
        padded = np.pad(dem, 1, mode="edge")
        a = padded[:-2, :-2]; b = padded[:-2, 1:-1]; c = padded[:-2, 2:]
        d = padded[1:-1, :-2];                       f = padded[1:-1, 2:]
        g = padded[2:, :-2];  h = padded[2:, 1:-1];  k = padded[2:, 2:]
        dz_dx = ((c + 2 * f + k) - (a + 2 * d + g)) / (8.0 * cellsize)
        dz_dy = ((g + 2 * h + k) - (a + 2 * b + c)) / (8.0 * cellsize)
        return np.sqrt(dz_dx ** 2 + dz_dy ** 2) * 100.0

    def calculate_veg_ratios(self, vegarea, barearea):
        den = 0.01 + vegarea + barearea
        vegratio = self._safe_div(vegarea, den)
        bareratio = 1.0 - vegratio
        return vegratio, bareratio

    def calculate_snowmelt(self, ii, rainfall, temp, snowcover, dem,
                           degree_days, is_first_step=False):
        if (is_first_step or self.snow_store is None
                or self.snow_store.shape != dem.shape):
            self.snow_store = np.zeros_like(dem, dtype=np.float64)
        snow_fraction = 1.61 * np.power(1.35, temp) + 1.0
        swe = self.snow_store + self._safe_div(rainfall, snow_fraction)
        cm = np.where(temp < self.params.base_temp, 0.0, self.params.melt_factor)
        melt_condition = (snowcover > 0) & (swe > 0.5)
        snowmelt_a = np.where(melt_condition, cm * degree_days * temp, 0.0)
        snowmelt = np.minimum(snowmelt_a, swe)
        self.snow_store = swe - snowmelt
        return snowmelt, self.snow_store

    def calculate_interception(self, ii, rainfall, pet, lai, vegarea):
        nr = self.rainy_days[ii - 1]
        a_int = float(self.coefficients[0])
        alfa = float(self.coefficients[1])
        lp = float(self.coefficients[6])
        a_lai = np.where(lai <= 0, 0.001, a_int * lai)
        va = rainfall * (1.0 - 1.0 / np.exp(0.463 * lai))
        dit = a_lai - self._safe_div(a_lai, 1.0 + self._safe_div(va, a_lai))
        exp_term = np.exp(-dit * nr / np.maximum(rainfall, 1e-10))
        total_int = np.where(lai > 0, vegarea * rainfall * (1.0 - exp_term), 0.0)
        total_int = np.minimum(total_int, pet)
        evap_rate = np.where(
            rainfall > pet, 1.0,
            self._safe_div(rainfall,
                           np.power(np.power(rainfall, alfa) +
                                    np.power(pet, alfa), 1.0 / alfa)))
        interception = total_int * (0.04 + 0.067 * lai)
        ch = np.minimum(self._safe_div(evap_rate, lp), 1.0)
        return interception, total_int, evap_rate, ch

    def calculate_surface_runoff(self, ii, rainfall, total_int, lai,
                                 cper, imp_area, owarea, vegarea, barearea,
                                 vegratio, bareratio, ch, slope,
                                 soilfactor, landfactor):
        nr = self.rainy_days[ii - 1]
        intensity_coef = float(self.coefficients[7])
        imp_ow_area = imp_area + owarea
        cimp = 0.09 * np.exp(2.4 * imp_ow_area)
        cwp = ((1.0 - imp_ow_area) * cper) + (imp_ow_area * cimp)
        pdaily = self._safe_div(rainfall, nr)
        ch_mod = ((1.0 - imp_ow_area) * ch) + imp_ow_area
        num = cwp * pdaily
        den = (cwp * pdaily) - (intensity_coef * cwp) + intensity_coef
        csr = np.where(intensity_coef < pdaily,
                       self._safe_div(num, den), cwp)
        cell_runoff = ch_mod * csr * (rainfall - total_int)
        vegrunoff = vegarea * cper * cell_runoff * vegratio * (1.0 - cimp)
        barerunoff = barearea * cper * cell_runoff * bareratio * (1.0 - cimp)
        imperrunoff = imp_area * (1.0 - cper) * cell_runoff * (1.0 + cimp)
        owrunoff = owarea * 0.1 * cell_runoff
        return {
            "cper": cper, "cimp": cimp, "cwp": cwp,
            "imp_ow_area": imp_ow_area, "pdaily": pdaily,
            "ch": ch_mod, "csr": csr, "cell_runoff": cell_runoff,
            "vegrunoff": vegrunoff, "barerunoff": barerunoff,
            "imperrunoff": imperrunoff, "owrunoff": owrunoff,
        }

    def calculate_cper(self, landfactor, soilfactor, slope):
        w_lu = float(self.coefficients[3])
        w_so = float(self.coefficients[4])
        w_sl = float(self.coefficients[2])
        return (w_lu * landfactor) + (w_so * soilfactor) + \
               self._safe_div(w_sl * slope, 10.0 + slope)

    def calculate_et_factor(self, temp, wind, lai, gamma,
                            aero_resist_wind1ms, minstomata):
        temp_term = 2503.0 / np.power(temp + 237.3, 2)
        exp_term = np.power(2.71828, 17.3 * temp / (temp + 237.3))
        penman = self._safe_div(gamma, temp_term * exp_term)
        winda = np.where(wind <= 0.01, 0.01, wind)
        aero = self._safe_div(aero_resist_wind1ms, winda)
        laia = np.where(lai > 0, lai, 1.0)
        canopy = self._safe_div(minstomata, laia)
        et_factor = self._safe_div(
            1.0 + penman,
            1.0 + ((1.0 + self._safe_div(canopy, aero)) * penman))
        return et_factor, penman

    # ------------------------------------------------------------------
    # Dynamic bucket ET (default)
    # ------------------------------------------------------------------
    def _et_dynamic(self, ii, rainfall, pet, evap_rate,
                    et_factor, interception, lai, gwdepth,
                    rootdepth, fc, wp, tension_ht,
                    residual_wc, evapo_depth, a1,
                    vegarea, barearea, imp_area, owarea,
                    vegrunoff, barerunoff, imperrunoff,
                    cell_runoff, soil_storage_in):
        imp_evap_a = evap_rate * (rainfall - imperrunoff)
        imp_evap = np.minimum(imp_evap_a, pet)
        cell_imp = imp_evap * imp_area
        cell_ow = pet * owarea

        S_avail = np.maximum(
            soil_storage_in + rainfall - interception
            - cell_runoff - cell_imp, 0.0)

        rootwater = (fc - wp) * (rootdepth + tension_ht) * 10.0
        gwdepth_adj = np.where((gwdepth - tension_ht) >= 0,
                               gwdepth - tension_ht, 0.0)
        awc = S_avail + (12.0 * rootwater)
        pet_veg = et_factor * pet
        a1_pow_vega = self._safe_div(awc, np.maximum(pet_veg, 1e-10))
        a1_pow_veg = np.minimum(a1_pow_vega, 30.0)
        pot_act = 1.0 - np.power(a1, a1_pow_veg)
        act_tp_a = pot_act * evap_rate * pet_veg
        act_tp_b = np.minimum(act_tp_a, S_avail)
        act_tp_c = np.minimum(act_tp_b, pet - interception)
        act_tp = np.where(lai <= 0.05, 0.0, act_tp_c)
        gw_transp = np.where(gwdepth_adj <= rootdepth, pet_veg - act_tp, 0.0)
        transp = np.where(gwdepth_adj <= rootdepth, pet_veg, act_tp)

        a1_barea = self._safe_div(
            S_avail + 12.0 * (fc - residual_wc) *
            (evapo_depth + tension_ht) * 1000.0,
            np.maximum(pet, 1e-10))
        a1_bare = np.minimum(a1_barea, 30.0)
        pot_bare = 1.0 - np.power(a1, a1_bare)
        bare_a = pot_bare * evap_rate * pet
        bare_b = np.minimum(bare_a, S_avail)
        bare = np.minimum(bare_b, pet)
        gw_evapo = np.where(gwdepth_adj < evapo_depth, pet - bare, 0.0)
        bare = np.where(gwdepth_adj < evapo_depth, pet, bare_b)

        cell_tp = transp * vegarea
        cell_bare = bare * barearea
        cell_gwt = gw_transp * vegarea
        cell_gwe = gw_evapo * barearea

        soil_et_raw = cell_tp + cell_bare
        scale = np.where(
            soil_et_raw > 1e-10,
            np.minimum(S_avail / np.maximum(soil_et_raw, 1e-10), 1.0), 1.0)
        cell_tp_final = cell_tp * scale
        cell_bare_final = cell_bare * scale
        soil_et = cell_tp_final + cell_bare_final
        cell_et_precip = interception + cell_imp + soil_et
        cell_gw_discharge = cell_gwt + cell_gwe + cell_ow

        return {
            "cell_actualtranspiration": cell_tp_final,
            "cell_actual_baresoil_evapo": cell_bare_final,
            "cell_ow_evaporation": cell_ow,
            "cell_imper_evaporation": cell_imp,
            "cell_gw_transpiration": cell_gwt,
            "cell_gw_evaporation": cell_gwe,
            "cell_evapotranspiration": cell_et_precip,
            "cell_gw_discharge": cell_gw_discharge,
            "S_avail": S_avail,
            "soil_et": soil_et,
        }

    # ------------------------------------------------------------------
    # Steady-state ET (Wetspass-M 2016 compatibility)
    # ------------------------------------------------------------------
    def _et_steady_state(self, ii, rainfall, pet, evap_rate,
                         et_factor, interception, lai, gwdepth,
                         rootdepth, fc, wp, tension_ht,
                         residual_wc, evapo_depth, a1,
                         vegarea, barearea, imp_area, owarea,
                         vegrunoff, barerunoff, imperrunoff,
                         soilwater_storage):
        """Direct port of the original Wetspass-M 2016 ET routine.
        soilwater_storage is always zero in steady-state mode."""
        rootwater = (fc - wp) * (rootdepth + tension_ht) * 10.0
        gwdepth_adj = np.where((gwdepth - tension_ht) >= 0,
                               gwdepth - tension_ht, 0.0)
        awc = rainfall + (12.0 * rootwater)   # rainfall, not S_avail
        pet_veg = et_factor * pet
        a1_pow_vega = self._safe_div(awc, np.maximum(pet_veg, 1e-10))
        a1_pow_veg = np.minimum(a1_pow_vega, 30.0)
        pot_act = 1.0 - np.power(a1, a1_pow_veg)
        act_tp_a = pot_act * evap_rate * pet_veg
        act_tp_b = np.minimum(
            act_tp_a, rainfall - interception - vegrunoff + soilwater_storage)
        act_tp_c = np.minimum(act_tp_b, pet - interception)
        act_tp = np.where(lai <= 0.05, 0.0, act_tp_c)
        gw_transp = np.where(gwdepth_adj <= rootdepth, pet_veg - act_tp, 0.0)
        transp = np.where(gwdepth_adj <= rootdepth, pet_veg, act_tp)

        a1_barea = self._safe_div(
            rainfall + 12.0 * (fc - residual_wc) *
            (evapo_depth + tension_ht) * 1000.0,
            np.maximum(pet, 1e-10))
        a1_bare = np.minimum(a1_barea, 30.0)
        pot_bare = 1.0 - np.power(a1, a1_bare)
        bare_a = pot_bare * evap_rate * pet
        bare_b = np.minimum(bare_a, rainfall - barerunoff)
        bare = np.minimum(bare_b, pet)
        gw_evapo = np.where(gwdepth_adj < evapo_depth, pet - bare, 0.0)
        bare = np.where(gwdepth_adj < evapo_depth, pet, bare_b)

        imp_evap_a = evap_rate * (rainfall - imperrunoff)
        imp_evap = np.minimum(imp_evap_a, pet)

        cell_tp = transp * vegarea
        cell_bare = bare * barearea
        cell_ow = pet * owarea
        cell_imp = imp_evap * imp_area
        cell_gwt = gw_transp * vegarea
        cell_gwe = gw_evapo * barearea

        cell_et_precip = interception + cell_tp + cell_bare + cell_imp
        cell_et_precip = np.where(cell_et_precip > rainfall,
                                  rainfall, cell_et_precip)
        cell_et_precip = np.maximum(cell_et_precip, 0.0)

        cell_gw_discharge = cell_gwt + cell_gwe + cell_ow
        cell_et_total = cell_et_precip + cell_gw_discharge

        return {
            "cell_actualtranspiration": cell_tp,
            "cell_actual_baresoil_evapo": cell_bare,
            "cell_ow_evaporation": cell_ow,
            "cell_imper_evaporation": cell_imp,
            "cell_gw_transpiration": cell_gwt,
            "cell_gw_evaporation": cell_gwe,
            # In compat mode the mass balance uses the TOTAL (with GW).
            "cell_evapotranspiration": cell_et_total,
            "cell_gw_discharge": cell_gw_discharge,
        }

    # ------------------------------------------------------------------
    # Dynamic bucket recharge (default)
    # ------------------------------------------------------------------
    def _recharge_dynamic(self, S_avail, soil_et, fc, wp, rootdepth,
                          owarea, soil_storage_in):
        S_max = np.maximum((1.0 - owarea) * (fc - wp) * rootdepth * 1000.0, 0.0)
        dS = np.maximum(S_avail - soil_et, 0.0)
        excess = dS - S_max
        R = np.where(excess > 0, excess, 0.0)
        S_new = np.where(excess > 0, S_max, dS)
        cell_et_adj = np.zeros_like(S_avail)  # not used for output here
        wb_error = S_avail - soil_et - R - S_new
        return R, S_new, wb_error

    # ------------------------------------------------------------------
    # Steady-state recharge (Wetspass-M 2016 compatibility)
    # ------------------------------------------------------------------
    def _recharge_steady_state(self, rainfall, cell_et_total, cell_runoff,
                               owarea, gwdepth, rootdepth, soilwater_storage):
        """Direct port of the original Wetspass-M 2016 Recharge routine."""
        rech_a = rainfall - cell_et_total - cell_runoff
        rech_b = np.where(owarea > 0,
                          np.where(rech_a < 0, 0.0, rech_a), rech_a)
        rech = np.where(
            rech_b < (0.0 - soilwater_storage),
            np.where(gwdepth > rootdepth, 0.0 - soilwater_storage, rech_b),
            rech_b)
        cell_et_adj = cell_et_total + (rech_b - rech_a)
        wb_error = rainfall - cell_et_adj - cell_runoff - rech
        S_new = np.zeros_like(rainfall)
        return rech, cell_et_adj, S_new, wb_error

    def calculate_month(self, ii, inputs, is_first_step=False):
        rainfall = inputs["rainfall"]; pet = inputs["pet"]
        temp = inputs["temp"]; wind = inputs["wind"]
        gwdepth = inputs["gwdepth"]; lai = inputs["lai"]
        dem = inputs["dem"]; slope = inputs["slope"]; gamma = inputs["gamma"]
        vegarea = inputs["vegarea"]; barearea = inputs["barearea"]
        imp_area = inputs["imp_area"]; owarea = inputs["owarea"]
        rootdepth = inputs["rootdepth"]; minstomata = inputs["minstomata"]
        landfactor = inputs["landfactor"]; aero_resist = inputs["Aeroresit_wind1ms"]
        fc = inputs["fc"]; wp = inputs["wp"]
        residual_wc = inputs["residual_water_content"]
        a1 = inputs["a1"]; evapo_depth = inputs["evapo_depth"]
        tension_ht = inputs["tension_ht"]; soilfactor = inputs["soilfactor"]

        if self.params.steady_state_mode:
            soil_storage_in = np.zeros_like(rainfall)
        else:
            soil_storage_in = inputs.get("soil_storage")
            if soil_storage_in is None:
                soil_storage_in = np.zeros_like(rainfall)

        vegratio, bareratio = self.calculate_veg_ratios(vegarea, barearea)
        snowmelt = None
        if "snowcover" in inputs:
            degree_days = self.rainy_days[ii - 1]
            snowmelt, _ = self.calculate_snowmelt(
                ii, rainfall, temp, inputs["snowcover"], dem,
                degree_days, is_first_step)
        et_factor, penman = self.calculate_et_factor(
            temp, wind, lai, gamma, aero_resist, minstomata)
        interception, total_int, evap_rate, ch = self.calculate_interception(
            ii, rainfall, pet, lai, vegarea)
        cper = self.calculate_cper(landfactor, soilfactor, slope)
        runoff = self.calculate_surface_runoff(
            ii, rainfall, total_int, lai, cper, imp_area, owarea,
            vegarea, barearea, vegratio, bareratio, ch, slope,
            soilfactor, landfactor)

        if self.params.steady_state_mode:
            et = self._et_steady_state(
                ii, rainfall, pet, evap_rate, et_factor, interception,
                lai, gwdepth, rootdepth, fc, wp, tension_ht,
                residual_wc, evapo_depth, a1,
                vegarea, barearea, imp_area, owarea,
                runoff["vegrunoff"], runoff["barerunoff"],
                runoff["imperrunoff"], soil_storage_in)
            rech, cell_et_adj, S_new, wb_error = self._recharge_steady_state(
                rainfall, et["cell_evapotranspiration"],
                runoff["cell_runoff"], owarea, gwdepth, rootdepth,
                soil_storage_in)
        else:
            et = self._et_dynamic(
                ii, rainfall, pet, evap_rate, et_factor, interception,
                lai, gwdepth, rootdepth, fc, wp, tension_ht,
                residual_wc, evapo_depth, a1,
                vegarea, barearea, imp_area, owarea,
                runoff["vegrunoff"], runoff["barerunoff"],
                runoff["imperrunoff"], runoff["cell_runoff"],
                soil_storage_in)
            rech, S_new, wb_error = self._recharge_dynamic(
                et["S_avail"], et["soil_et"],
                fc, wp, rootdepth, owarea, soil_storage_in)
            cell_et_adj = et["cell_evapotranspiration"]

        outputs = {
            "Interception": interception,
            "total_Interception": total_int,
            "evap_rate": evap_rate,
            "Ch": ch,
            "Cell_runoff": runoff["cell_runoff"],
            "Csr": runoff["csr"],
            "vegrunoff": runoff["vegrunoff"],
            "barerunoff": runoff["barerunoff"],
            "imperrunoff": runoff["imperrunoff"],
            "owrunoff": runoff["owrunoff"],
            "Cell_evapotranspiration": cell_et_adj,
            "Cell_gw_discharge": et["cell_gw_discharge"],
            "Cell_actualtranspiration": et["cell_actualtranspiration"],
            "Cell_actual_baresoil_evapo": et["cell_actual_baresoil_evapo"],
            "Cell_ow_evaporation": et["cell_ow_evaporation"],
            "Cell_imper_evaporation": et["cell_imper_evaporation"],
            "Cell_gw_transpiration": et["cell_gw_transpiration"],
            "Cell_gw_evaporation": et["cell_gw_evaporation"],
            "recharge": rech,
            "soilwater_storage": S_new,
            "wb_error": wb_error,
            "penmann_coefficient": penman,
        }
        if snowmelt is not None:
            outputs["SnowMelt"] = snowmelt
            outputs["SnowStore"] = self.snow_store
        return outputs

    def calculate_statistics(self, outputs):
        stats = {}
        for key in ["Cell_evapotranspiration", "Cell_runoff",
                    "Interception", "recharge"]:
            arr = outputs.get(key)
            if arr is None:
                continue
            valid = arr[np.isfinite(arr)]
            stats[key] = float(np.mean(valid)) if valid.size else 0.0
        if self.params.area_km2 > 0:
            q_surf = (self.params.q0_surface * self.params.x_coef) + \
                     (1000.0 * (1.0 - self.params.x_coef) *
                      self.params.area_km2 * stats.get("Cell_runoff", 0.0))
            q_base = (self.params.q0_base * self.params.beta) + \
                     (1000.0 * (1.0 - self.params.beta) *
                      self.params.contribution * self.params.area_km2 *
                      stats.get("recharge", 0.0))
            stats["QSurf"] = q_surf
            stats["QBase"] = q_base
            self.qt_1 = q_surf
            self.qbt_1 = q_base
        return stats