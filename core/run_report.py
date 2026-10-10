# -*- coding: utf-8 -*-
"""
Run report and metadata writer.

Produces:
    RunSummary.txt        human-readable summary of a diffusive run
    _run_metadata.json    machine-readable metadata for the run
    RunSummary_focused.txt        human-readable summary of a focused run
    _run_metadata_focused.json    machine-readable metadata for a focused run

All writers are best-effort: a failure to write the report never blocks
the run. Failures are logged at debug level.
"""

import contextlib
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("WetSpassModern")


# =====================================================================
# Helpers
# =====================================================================
def _parse_meta_line(line):
    """
    Parse a 'key=value' line into its value.

    Returns "unknown" if the line is empty, has no '=', or fails to
    parse for any reason.
    """
    if "=" not in line:
        return "unknown"
    with contextlib.suppress(Exception):
        return line.split("=", 1)[1].strip()
    return "unknown"


def _safe_float(x, default=0.0):
    with contextlib.suppress(Exception):
        return float(x)
    return default


def _safe_int(x, default=0):
    with contextlib.suppress(Exception):
        return int(x)
    return default


def _parameters_dict(parameters):
    """Return a dict of the WetSpassParameters object, defensively."""
    out = {}
    if parameters is None:
        return out
    for name in (
        "a_interception", "alfa", "w_slope", "w_landuse", "w_soil",
        "x_coef", "lp", "intensity", "beta", "contribution",
        "base_temp", "melt_factor", "snow_density", "area_km2",
        "q0_surface", "q0_base", "steady_state_mode",
    ):
        with contextlib.suppress(Exception):
            out[name] = getattr(parameters, name)
    return out


# =====================================================================
# Diffusive run report
# =====================================================================
def write_diffusive_run_report(
        output_dir,
        working_dir,
        parameters,
        start_step,
        end_step,
        simulate_snow,
        output_periods,
        spin_up_cycles,
        keep_outputs,
        months_processed,
        status="completed"):
    """
    Write RunSummary.txt and _run_metadata.json into the output folder.
    Never raises.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now().isoformat(timespec="seconds")

    # ---------------- JSON metadata ----------------
    meta = {
        "run_type": "diffusive",
        "status": status,
        "finished_at": now,
        "working_dir": str(working_dir),
        "output_dir": str(output_dir),
        "start_step": _safe_int(start_step),
        "end_step": _safe_int(end_step),
        "simulate_snow": bool(simulate_snow),
        "output_periods": list(output_periods or []),
        "spin_up_cycles": _safe_int(spin_up_cycles),
        "keep_outputs": sorted(list(keep_outputs or [])),
        "months_processed": _safe_int(months_processed),
        "parameters": _parameters_dict(parameters),
    }

    json_path = output_dir / "_run_metadata.json"
    with contextlib.suppress(Exception):
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)

    # ---------------- Human-readable summary ----------------
    lines = []
    lines.append("=" * 68)
    lines.append("  WetSpass-M Modern — Run Summary (diffusive)")
    lines.append("=" * 68)
    lines.append(f"  Status            : {status}")
    lines.append(f"  Finished at       : {now}")
    lines.append(f"  Working directory : {working_dir}")
    lines.append(f"  Output directory  : {output_dir}")
    lines.append("")
    lines.append("  RUN CONFIGURATION")
    lines.append("  " + "-" * 60)
    lines.append(f"  Start step        : {start_step}")
    lines.append(f"  End step          : {end_step}")
    lines.append(f"  Months processed  : {months_processed}")
    lines.append(f"  Spin-up cycles    : {spin_up_cycles}")
    lines.append(f"  Snow processes    : {simulate_snow}")
    lines.append(f"  Output periods    : {', '.join(output_periods or []) or '—'}")
    lines.append("")

    lines.append("  PARAMETERS")
    lines.append("  " + "-" * 60)
    for k, v in _parameters_dict(parameters).items():
        lines.append(f"  {k:<24}: {v}")
    lines.append("")

    if keep_outputs:
        lines.append("  OUTPUTS KEPT")
        lines.append("  " + "-" * 60)
        for name in sorted(keep_outputs):
            lines.append(f"  - {name}")
        lines.append("")

    lines.append("=" * 68)

    txt_path = output_dir / "RunSummary.txt"
    with contextlib.suppress(Exception):
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    logger.debug("Wrote diffusive run report: %s", txt_path)


# =====================================================================
# Focused run report
# =====================================================================
def write_focused_run_report(
        output_dir,
        working_dir,
        engine,
        results):
    """
    Write RunSummary_focused.txt and _run_metadata_focused.json.
    Never raises.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now().isoformat(timespec="seconds")

    # ---------------- JSON metadata ----------------
    meta = {
        "run_type": "focused",
        "finished_at": now,
        "working_dir": str(working_dir),
        "output_dir": str(output_dir),
    }
    if engine is not None:
        for name in (
            "start_step", "end_step", "stream_source",
            "dem_stream_threshold", "Ks_fixed", "W_fixed",
            "gwdepth_threshold", "environment", "ks_mode",
            "width_mode", "delta_factor", "max_daily_depth_mm",
            "flow_duration_fraction",
        ):
            with contextlib.suppress(Exception):
                meta[name] = getattr(engine, name)

    for k, v in (results or {}).items():
        if k == "monthly_results":
            continue
        with contextlib.suppress(Exception):
            meta[k] = v

    json_path = output_dir / "_run_metadata_focused.json"
    with contextlib.suppress(Exception):
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)

    # ---------------- Human-readable summary ----------------
    r = results or {}
    lines = []
    lines.append("=" * 68)
    lines.append("  WetSpass-M Modern — Run Summary (focused recharge)")
    lines.append("=" * 68)
    lines.append(f"  Finished at       : {now}")
    lines.append(f"  Working directory : {working_dir}")
    lines.append(f"  Output directory  : {output_dir}")
    lines.append("")

    if engine is not None:
        lines.append("  ENGINE CONFIGURATION")
        lines.append("  " + "-" * 60)
        for name in (
            "start_step", "end_step", "stream_source",
            "dem_stream_threshold", "Ks_fixed", "W_fixed",
            "gwdepth_threshold", "environment", "ks_mode",
            "width_mode", "delta_factor", "max_daily_depth_mm",
            "flow_duration_fraction",
        ):
            with contextlib.suppress(Exception):
                lines.append(f"  {name:<22}: {getattr(engine, name)}")
        lines.append("")

    lines.append("  RESULTS")
    lines.append("  " + "-" * 60)
    for k in (
        "catchment_area_m2", "n_stream_cells", "losing_fraction",
        "total_focused_m3", "total_available_m3", "total_outlet_m3",
        "annual_avg_mm", "annual_loss_fraction", "streambed_avg_mm",
    ):
        if k in r:
            lines.append(f"  {k:<22}: {r[k]}")
    lines.append("")

    monthly = r.get("monthly_results") or []
    if monthly:
        lines.append("  MONTHLY RESULTS")
        lines.append("  " + "-" * 60)
        lines.append(
            f"  {'Month':>5}  {'Losing':>8}  {'Stream':>8}  "
            f"{'V_avail (m3)':>14}  {'V_focus (m3)':>14}  "
            f"{'Catch-avg':>10}"
        )
        for m in monthly:
            with contextlib.suppress(Exception):
                lines.append(
                    f"  {m.get('month', '?'):>5}  "
                    f"{m.get('n_losing', 0):>8}  "
                    f"{m.get('n_stream_valid', 0):>8}  "
                    f"{m.get('V_available', 0.0):>14.4e}  "
                    f"{m.get('V_focused', 0.0):>14.4e}  "
                    f"{m.get('R_focused_avg_mm', 0.0):>10.4f}"
                )
        lines.append("")

    lines.append("=" * 68)

    txt_path = output_dir / "RunSummary_focused.txt"
    with contextlib.suppress(Exception):
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    logger.debug("Wrote focused run report: %s", txt_path)