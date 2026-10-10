# -*- coding: utf-8 -*-
"""
Hydrological calibration / validation metrics.

Standard metrics used in the hydrology literature:
    NSE      Nash-Sutcliffe Efficiency
    KGE      Kling-Gupta Efficiency
    PBIAS    Percent bias
    R2       Coefficient of determination
    RMSE     Root mean square error
    MAE      Mean absolute error
    nRMSE    RMSE divided by the observed range
"""

import contextlib
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("WetSpassModern")


# ===========================================================================
# Observed data reader
# ===========================================================================
def read_observed_csv(path):
    """
    Read a CSV file with observed values.

    Supported column layouts (first non-comment line is the header):
        year, month, value
        month, value
        date, value
        year, value

    If no recognizable header is found, the file is assumed to have
    one value per row, representing month 1..12 in order.

    Returns
    -------
    dict with keys:
        months   : list of int (1..12), or None if only annual
        years    : list of int, or None if only monthly
        values   : list of float
        label    : human-readable description of what was read
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Observed-data file not found: {p}")

    lines = []
    with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            lines.append(s)
    if not lines:
        raise RuntimeError(f"Empty observed-data file: {p}")

    # Try to detect delimiter
    def split(line):
        for d in ("\t", ",", ";", " "):
            if d in line:
                return [c.strip() for c in line.split(d) if c.strip()]
        return [line]

    first = split(lines[0])
    header = [c.lower() for c in first]

    # Known column synonyms
    month_syn = {"month", "mon", "m", "mo"}
    year_syn = {"year", "yr", "y"}
    value_syn = {"value", "flow", "q", "streamflow", "discharge",
                 "gwl", "depth", "obs", "observed", "runoff",
                 "recharge", "aet", "et"}

    def find_col(syn):
        for i, c in enumerate(header):
            if c in syn:
                return i
        return -1

    idx_month = find_col(month_syn)
    idx_year = find_col(year_syn)
    idx_value = find_col(value_syn)

    # Case: unlabelled single column
    if len(first) == 1 and idx_month < 0 and idx_year < 0:
        values = []
        parse_ok = True
        for i in range(len(lines)):
            with contextlib.suppress(ValueError):
                values.append(float(lines[i]))
                continue
            # If any line fails, abandon this branch
            parse_ok = False
            break
        if parse_ok and values:
            return {
                "months": list(range(1, len(values) + 1)),
                "years": None,
                "values": values,
                "label": f"{p.name} (unlabelled, treated as month 1..N)",
            }
        # else fall through to the year/month/value fallback below

    has_header = (idx_value >= 0 and
                  (idx_month >= 0 or idx_year >= 0))

    if has_header:
        body = lines[1:]
        months, years, values = [], [], []
        for line in body:
            parts = split(line)
            if idx_value >= len(parts):
                continue
            try:
                v = float(parts[idx_value])
            except ValueError:
                continue
            m = None
            y = None
            if 0 <= idx_month < len(parts):
                with contextlib.suppress(ValueError):
                    m = int(float(parts[idx_month]))
            if 0 <= idx_year < len(parts):
                with contextlib.suppress(ValueError):
                    y = int(float(parts[idx_year]))
            months.append(m)
            years.append(y)
            values.append(v)
        label = f"{p.name} (header detected)"
        return {
            "months": months if any(m is not None for m in months)
                      else None,
            "years":  years  if any(y is not None for y in years)
                      else None,
            "values": values,
            "label":  label,
        }

    # Fallback: no header, assume year/month/value layout
    months, years, values = [], [], []
    for line in lines:
        parts = split(line)
        if len(parts) == 1:
            try:
                values.append(float(parts[0]))
                months.append(None)
                years.append(None)
            except ValueError:
                continue
        elif len(parts) == 2:
            try:
                months.append(int(float(parts[0])))
                values.append(float(parts[1]))
                years.append(None)
            except ValueError:
                continue
        elif len(parts) >= 3:
            try:
                years.append(int(float(parts[0])))
                months.append(int(float(parts[1])))
                values.append(float(parts[2]))
            except ValueError:
                continue

    return {
        "months": months if any(m is not None for m in months) else None,
        "years":  years  if any(y is not None for y in years)  else None,
        "values": values,
        "label":  f"{p.name} (no header)",
    }


def read_simulated_tbl(path, column="Runoff"):
    """
    Read one column from Simulated.tbl.

    Expected header: TimeStep  AET  Runoff  Interception  Recharge
    (with optional trailing columns)

    Returns dict {month: value}.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Simulated.tbl not found: {p}")

    lines = []
    with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.rstrip("\r\n")
            if not s or s.startswith("*"):
                continue
            lines.append(s)
    if len(lines) < 2:
        raise RuntimeError("Simulated.tbl has no data rows.")

    header = [h.strip().lower() for h in
              lines[0].replace(",", "\t").split("\t")]
    if "timestep" in header:
        idx_time = header.index("timestep")
    else:
        idx_time = 0

    col = column.lower()
    if col in header:
        idx_val = header.index(col)
    else:
        # Fall back to standard positions
        positions = {
            "aet": 1, "runoff": 2, "interception": 3, "recharge": 4,
        }
        if col not in positions:
            raise ValueError(
                f"Column '{column}' not found in Simulated.tbl. "
                f"Available: {header}")
        idx_val = positions[col]

    result = {}
    for line in lines[1:]:
        parts = [x.strip() for x in line.replace(",", "\t").split("\t")]
        if len(parts) <= max(idx_time, idx_val):
            continue
        try:
            t = int(float(parts[idx_time]))
            v = float(parts[idx_val])
        except ValueError:
            continue
        result[t] = v
    return result


# ===========================================================================
# Metrics
# ===========================================================================
def _finite_pair(obs, sim):
    obs = np.asarray(obs, dtype=np.float64)
    sim = np.asarray(sim, dtype=np.float64)
    mask = np.isfinite(obs) & np.isfinite(sim)
    return obs[mask], sim[mask]


def nash_sutcliffe(obs, sim):
    o, s = _finite_pair(obs, sim)
    if o.size == 0:
        return None
    denom = np.sum((o - o.mean()) ** 2)
    if denom == 0:
        return None
    return 1.0 - np.sum((o - s) ** 2) / denom


def kling_gupta(obs, sim):
    o, s = _finite_pair(obs, sim)
    if o.size < 2:
        return None
    if o.std() == 0 or o.mean() == 0:
        return None
    r = np.corrcoef(o, s)[0, 1]
    alpha = s.std() / o.std()
    beta = s.mean() / o.mean()
    return 1.0 - np.sqrt((r - 1) ** 2 + (alpha - 1) ** 2
                          + (beta - 1) ** 2)


def percent_bias(obs, sim):
    o, s = _finite_pair(obs, sim)
    if o.size == 0 or o.sum() == 0:
        return None
    return 100.0 * (s.sum() - o.sum()) / o.sum()


def r_squared(obs, sim):
    o, s = _finite_pair(obs, sim)
    if o.size < 2:
        return None
    if o.std() == 0 or s.std() == 0:
        return None
    return float(np.corrcoef(o, s)[0, 1] ** 2)


def rmse(obs, sim):
    o, s = _finite_pair(obs, sim)
    if o.size == 0:
        return None
    return float(np.sqrt(np.mean((o - s) ** 2)))


def mae(obs, sim):
    o, s = _finite_pair(obs, sim)
    if o.size == 0:
        return None
    return float(np.mean(np.abs(o - s)))


def nrmse(obs, sim):
    r = rmse(obs, sim)
    o, _ = _finite_pair(obs, sim)
    if r is None or o.size == 0:
        return None
    rng = o.max() - o.min()
    if rng == 0:
        return None
    return 100.0 * r / rng


def compute_all_metrics(obs, sim):
    return {
        "NSE":   nash_sutcliffe(obs, sim),
        "KGE":   kling_gupta(obs, sim),
        "PBIAS": percent_bias(obs, sim),
        "R2":    r_squared(obs, sim),
        "RMSE":  rmse(obs, sim),
        "MAE":   mae(obs, sim),
        "nRMSE": nrmse(obs, sim),
        "n":     int(_finite_pair(obs, sim)[0].size),
    }


def grade_nse(v):
    if v is None:
        return ("—", "#666666")
    if v >= 0.75:
        return ("Very good", "#1b7a3e")
    if v >= 0.65:
        return ("Good", "#1b7a3e")
    if v >= 0.50:
        return ("Satisfactory", "#b06f00")
    if v >= 0.0:
        return ("Acceptable", "#b06f00")
    return ("Poor", "#b83232")


def grade_pbias(v):
    if v is None:
        return ("—", "#666666")
    a = abs(v)
    if a < 10:
        return ("Very good", "#1b7a3e")
    if a < 15:
        return ("Good", "#1b7a3e")
    if a < 25:
        return ("Satisfactory", "#b06f00")
    return ("Poor", "#b83232")