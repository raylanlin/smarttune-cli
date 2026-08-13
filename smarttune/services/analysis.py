"""
smarttune/services/analysis.py

Pure library layer for flight log analysis.

No Rich output, no shell execution, no arbitrary writes.
Returns structured dataclasses and dicts suitable for JSON serialization.

Provides full parity with CLI commands:
  - get_log_quality()     ↔  stune quality
  - analyze_log()         ↔  stune analyze  (pid + fft + magfit + hardware + filter + sysid)
  - analyze_pid()         ↔  stune pid
  - analyze_fft()         ↔  stune fft
  - analyze_magfit()      ↔  stune magfit
  - analyze_sysid()       ↔  stune sysid
  - analyze_filter()      ↔  stune filter
  - analyze_hardware()    ↔  stune hardware
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from smarttune.errors import SmartTuneError
from smarttune.knowledge import KnowledgeBase
from smarttune.models.analysis_result import FullAnalysisResult
from smarttune.models.flight_data import FlightData
from smarttune.platform.base import PlatformAdapter
from smarttune.platform.registry import resolve_adapter
from smarttune.services.serialize import (
    serialize_full_result,
    serialize_pid_result,
    serialize_fft_result,
    serialize_magfit_result,
    serialize_sysid_results,
    serialize_filter_result,
    serialize_extra_analyzers_results,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Load / parse
# ---------------------------------------------------------------------------


def load_flight_data(
    log_path: Path,
    platform: str = "auto",
) -> Tuple[PlatformAdapter, FlightData]:
    """Parse a flight log and return the adapter + unified FlightData.

    Raises SmartTuneError on detection or parse failure.
    """
    _lp = Path(log_path) if isinstance(log_path, str) else log_path
    adapter = resolve_adapter(platform, _lp)
    flight_data = adapter.parse(_lp)
    return adapter, flight_data


# ---------------------------------------------------------------------------
# Log quality — full parity with CLI quality command
# ---------------------------------------------------------------------------


def get_log_quality(
    log_path: Path,
    platform: str = "auto",
) -> Dict[str, Any]:
    """Parse a log and return a quality assessment dict.

    Mirrors the CLI ``stune quality`` command exactly:
      1. Data completeness (PID/Gyro/Mag/Motor/Battery)
      2. Duration check
      3. Excitation (step response windows per axis)
      4. Sample rate consistency (jitter, drop rate)
    """
    _lp = Path(log_path) if isinstance(log_path, str) else log_path
    adapter, fd = load_flight_data(_lp, platform)

    issues: List[str] = []
    score = 100

    # ── 1. Data completeness ──────────────────────────────────
    has_pid = bool(fd.pid)
    has_gyro = fd.gyro is not None and len(fd.gyro) > 0
    has_mag = fd.has_mag
    has_motor = fd.has_motor
    has_battery = fd.has_battery

    completeness: List[Dict[str, Any]] = []

    n_pid = 0
    if has_pid:
        for sig in fd.pid.values():
            n_pid = max(n_pid, sig.sample_count)
    completeness.append({"name": "PID/RATE", "samples": n_pid, "ok": n_pid > 0, "required": True})

    n_gyro = len(fd.gyro) if has_gyro else 0
    completeness.append({"name": "IMU/Gyro", "samples": n_gyro, "ok": n_gyro > 0, "required": True})

    completeness.append(
        {"name": "Magnetometer", "samples": 1 if has_mag else 0, "ok": has_mag, "required": False}
    )
    completeness.append(
        {
            "name": "Motor",
            "samples": len(fd.motor_output) if has_motor else 0,
            "ok": has_motor,
            "required": False,
        }
    )
    completeness.append(
        {
            "name": "Battery",
            "samples": len(fd.battery_voltage) if has_battery else 0,
            "ok": has_battery,
            "required": False,
        }
    )

    for item in completeness:
        if not item["ok"] and item["required"]:
            issues.append(f"Missing required data: {item['name']}")
            score -= 20
        elif not item["ok"] and not item["required"]:
            issues.append(f"Optional data missing: {item['name']}")
            score -= 5

    # ── 2. Duration check ─────────────────────────────────────
    duration_s = fd.duration_s
    duration_min = duration_s / 60.0

    if duration_s < 30:
        issues.append(f"Log duration only {duration_s:.0f}s — far too short (recommend >= 3 min)")
        score -= 30
    elif duration_s < 120:
        issues.append(f"Log duration {duration_s:.0f}s is short — recommend at least 3 minutes")
        score -= 10
    elif duration_s < 300:
        issues.append(f"Log duration {duration_min:.1f} min — adequate for basic analysis")

    # ── 3. Excitation (step response windows) ─────────────────
    # C9 fix: use the same detect_steps() as PIDReviewer so the
    # "step window" counts shown by `stune quality` and `stune pid`
    # can no longer contradict each other (old code used an ad-hoc
    # std-based threshold here vs. max-based threshold in the analyzer).
    step_counts: Dict[str, int] = {}
    if has_pid and n_pid > 10:
        try:
            from smarttune.analyzers.pid_reviewer import detect_steps

            for ax in fd.axes:
                sig = fd.pid.get(ax)
                if sig is None or sig.desired is None or len(sig.desired) <= 10:
                    step_counts[ax] = 0
                    continue
                dt_ms = 4.0
                t = sig.timestamp_s
                if t is not None and len(t) > 1:
                    dts = np.diff(t)
                    dts_valid = dts[dts > 1e-6]
                    if len(dts_valid) > 0:
                        dt_ms = max(float(np.median(dts_valid)) * 1000.0, 0.1)
                step_counts[ax] = len(detect_steps(sig.desired, dt_ms=dt_ms))
        except Exception:
            pass

    if step_counts:
        min_steps = min(step_counts.values()) if step_counts else 0
        total_steps = sum(step_counts.values())

        if total_steps < 3:
            ax_str = " / ".join(f"{a.capitalize()}:{step_counts.get(a, 0)}" for a in step_counts)
            issues.append(
                f"Insufficient step response windows ({ax_str}) "
                "— perform quick stick inputs in Stabilize/AltHold mode"
            )
            score -= 25
        elif min_steps < 3:
            weak_axis = min(step_counts, key=step_counts.get)
            issues.append(
                f"{weak_axis.capitalize()} axis has few step windows ({step_counts[weak_axis]}), "
                "PID analysis may be unreliable"
            )
            score -= 8

    # ── 4. Sample rate consistency ────────────────────────────
    rate_consistency: List[Dict[str, Any]] = []

    if has_pid and n_pid > 2:
        for sig in fd.pid.values():
            t = sig.timestamp_s
            if t is not None and len(t) > 2:
                dts = np.diff(t)
                dts_valid = dts[(dts > 0) & (dts < 1.0)]
                if len(dts_valid) > 0:
                    median_dt = float(np.median(dts_valid))
                    sr = 1.0 / median_dt if median_dt > 0 else 0
                    std_dt = float(np.std(dts_valid))
                    jitter_pct = std_dt / median_dt * 100 if median_dt > 0 else 0
                    drop_count = int(np.sum(dts_valid > median_dt * 1.5))
                    drop_rate = drop_count / len(dts_valid) * 100
                    rate_consistency.append(
                        {
                            "source": "RATE/PID",
                            "sample_rate_hz": round(sr, 1),
                            "jitter_percent": round(jitter_pct, 1),
                            "drop_rate_percent": round(drop_rate, 1),
                        }
                    )
                    if drop_rate > 5:
                        issues.append(f"RATE message drop rate is high ({drop_rate:.1f}%)")
                        score -= 8
                    if jitter_pct > 20:
                        issues.append(f"RATE message timing jitter is high ({jitter_pct:.1f}%)")
                        score -= 5
                break  # Only check first axis

    score = max(0, min(100, score))

    # Rating
    if score >= 90:
        rating, advice = "EXCELLENT", "Proceed with full analysis"
    elif score >= 75:
        rating, advice = "GOOD", "Proceed with analysis; some data may be limited"
    elif score >= 55:
        rating, advice = "MARGINAL", "Analysis possible but results may be incomplete"
    else:
        rating, advice = (
            "POOR",
            "Log quality is low; consider re-flying with better logging settings",
        )

    file_size_mb = _lp.stat().st_size / (1024 * 1024)

    return {
        "platform": adapter.name,
        "display_name": adapter.display_name,
        "log_file": _lp.name,
        "file_size_mb": round(file_size_mb, 2),
        "duration_s": round(fd.duration_s, 1),
        "sample_rate_hz": round(fd.sample_rate_hz, 1),
        "axes": fd.axes,
        "has_gyro": has_gyro,
        "has_accel": fd.accel is not None and len(fd.accel) > 0,
        "has_mag": has_mag,
        "has_motor": has_motor,
        "has_battery": has_battery,
        "data_completeness": completeness,
        "step_counts": step_counts if step_counts else None,
        "rate_consistency": rate_consistency if rate_consistency else None,
        "validation_issues": fd.validate(),
        "quality": {
            "score": score,
            "rating": rating,
            "advice": advice,
        },
    }


# ---------------------------------------------------------------------------
# Shared module runner — single source of truth for analyzer wiring (A1 fix)
#
# Both the CLI (Rich rendering) and this services layer (JSON serialization)
# call run_module(); analyzer instantiation / knowledge wiring / capability
# and data guards live ONLY here, so the two entry points can no longer drift.
# ---------------------------------------------------------------------------

_MODULE_ERROR_CODES = {
    "pid": ("E5010", "E5011"),
    "fft": ("E5020", "E5021"),
    "magfit": ("E5030", "E5031"),
    "sysid": ("E5040", "E5041"),
    "filter": ("E5050", "E5051"),
    "hardware": ("E5060", "E5061"),
}


def _require_capability(module: str, adapter: PlatformAdapter) -> None:
    if module not in adapter.capabilities():
        cap_code = _MODULE_ERROR_CODES.get(module, ("E5000",))[0]
        raise SmartTuneError(
            message=f"{module} analysis is not supported on {adapter.display_name}",
            hint=f"Supported capabilities: {', '.join(sorted(adapter.capabilities()))}",
            code=cap_code,
        )


def run_module(
    module: str,
    adapter: PlatformAdapter,
    fd: FlightData,
    kb: Optional[KnowledgeBase] = None,
    axis: str = "all",
    na: int = 3,
    nb: int = 2,
) -> Any:
    """Run ONE analysis module on already-parsed FlightData.

    Returns the RAW result object (PIDAnalysisResult / FFTAnalysisResult /
    MagFitResult / sysid list / hardware dict) — callers decide whether to
    serialize (services) or render (CLI).

    Raises SmartTuneError on missing capability or missing required data.
    """
    _require_capability(module, adapter)
    data_code = _MODULE_ERROR_CODES.get(module, ("E5000", "E5001"))[1]
    _axis = axis if axis != "all" else None
    if kb is None:
        kb = KnowledgeBase(platform=adapter.name)

    if module == "pid":
        if not fd.pid:
            raise SmartTuneError(
                message="No PID data found in log",
                hint="The log may lack RATE or PID controller data",
                code=data_code,
            )
        from smarttune.analyzers.pid_reviewer import PIDReviewer

        reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))
        return reviewer.analyze(fd, axis=_axis)

    if module == "fft":
        if fd.gyro is None or len(fd.gyro) == 0:
            raise SmartTuneError(
                message="No gyro data found in log",
                hint="FFT analysis requires gyroscope data",
                code=data_code,
            )
        from smarttune.analyzers.fft_analyzer import FFTAnalyzer

        analyzer = FFTAnalyzer(knowledge=kb.get("filter_rules", {}))
        return analyzer.analyze(fd)

    if module == "magfit":
        if not fd.has_mag:
            raise SmartTuneError(
                message="No magnetometer data found in log",
                hint="MagFit analysis requires compass data",
                code=data_code,
            )
        from smarttune.analyzers.magfit import MAGFit

        magfit = MAGFit(knowledge=kb.get("magfit_rules", {}))
        return magfit.analyze(fd)

    if module == "sysid":
        if not fd.pid:
            raise SmartTuneError(
                message="No PID data found in log",
                hint="System identification requires PID rate data",
                code=data_code,
            )
        from smarttune.analyzers.sysid_analyzer import SysIDAnalyzer

        analyzer = SysIDAnalyzer(na=na, nb=nb)
        return analyzer.analyze(fd, axis=_axis)

    if module == "hardware":
        try:
            _hr_mod = importlib.import_module(f"smarttune.platform.{adapter.name}.hardware_report")
        except ImportError:
            raise SmartTuneError(
                message=f"Hardware report module not available for {adapter.display_name}",
                code=data_code,
            )
        return _hr_mod.generate_hardware_report(fd.params, flight_data=fd)

    raise ValueError(f"Unknown analysis module: {module!r}")


# ---------------------------------------------------------------------------
# Individual analysis functions (matching each CLI command)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inline recommendation validation (v3.2.1)
# ---------------------------------------------------------------------------


def _attach_validation(payload: Dict[str, Any], adapter: PlatformAdapter) -> Dict[str, Any]:
    """Annotate every serialized recommendation with a validation verdict.

    Walks the payload for ``recommendations`` lists and attaches
    ``validated`` / ``validation_status`` (and ``validation_message`` on
    rejection) to each entry, checked against the platform's parameter table.

    This turns the "always validate before recommending" discipline into a
    mechanism: agents no longer need one ``validate_param`` round-trip per
    recommendation — the analysis result arrives pre-validated. Explicit
    validation stays available for values the agent adjusts afterwards.

    Fails open as annotation only: if the parameter table cannot be loaded,
    the payload is returned untouched (recommendations simply carry no
    ``validated`` field — absence means "not checked", never "approved").
    """
    try:
        table = adapter.param_table()
    except Exception:  # missing table must never break analysis
        return payload

    def _annotate(rec: Dict[str, Any]) -> None:
        name = rec.get("param") or rec.get("generic_param")
        value = rec.get("suggested")
        if not name:
            return
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            rec["validated"] = False
            rec["validation_status"] = "unverifiable"
            rec["validation_message"] = "suggested value is not numeric"
            return
        verdict = table.validate_detail(str(name), float(value))
        rec["validated"] = bool(verdict["valid"])
        rec["validation_status"] = verdict["status"]
        if not verdict["valid"]:
            rec["validation_message"] = verdict["message"]

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            recs = node.get("recommendations")
            if isinstance(recs, list):
                for rec in recs:
                    if isinstance(rec, dict):
                        _annotate(rec)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(payload)
    return payload


def analyze_pid(
    log_path: Path,
    platform: str = "auto",
    axis: str = "all",
    max_recommendations: int = 20,
) -> Dict[str, Any]:
    """Run PID step response analysis. Matches ``stune pid``."""
    _lp = Path(log_path) if isinstance(log_path, str) else log_path
    adapter, fd = load_flight_data(_lp, platform)
    result = run_module("pid", adapter, fd, axis=axis)

    return _attach_validation(
        {
            "platform": adapter.name,
            "display_name": adapter.display_name,
            "log_file": _lp.name,
            "duration_s": round(fd.duration_s, 1),
            **serialize_pid_result(result, adapter, max_recommendations),
        },
        adapter,
    )


def analyze_fft(
    log_path: Path,
    platform: str = "auto",
    max_recommendations: int = 20,
) -> Dict[str, Any]:
    """Run FFT vibration spectrum analysis. Matches ``stune fft``."""
    _lp = Path(log_path) if isinstance(log_path, str) else log_path
    adapter, fd = load_flight_data(_lp, platform)
    result = run_module("fft", adapter, fd)

    return _attach_validation(
        {
            "platform": adapter.name,
            "display_name": adapter.display_name,
            "log_file": _lp.name,
            "duration_s": round(fd.duration_s, 1),
            **serialize_fft_result(result, adapter, max_recommendations),
        },
        adapter,
    )


def analyze_magfit(
    log_path: Path,
    platform: str = "auto",
    max_recommendations: int = 20,
) -> Dict[str, Any]:
    """Run magnetometer calibration analysis. Matches ``stune magfit``."""
    _lp = Path(log_path) if isinstance(log_path, str) else log_path
    adapter, fd = load_flight_data(_lp, platform)
    result = run_module("magfit", adapter, fd)

    return _attach_validation(
        {
            "platform": adapter.name,
            "display_name": adapter.display_name,
            "log_file": _lp.name,
            "duration_s": round(fd.duration_s, 1),
            **serialize_magfit_result(result, adapter, max_recommendations),
        },
        adapter,
    )


def analyze_sysid(
    log_path: Path,
    platform: str = "auto",
    axis: str = "all",
    na: int = 3,
    nb: int = 2,
) -> Dict[str, Any]:
    """Run ARX system identification. Matches ``stune sysid``."""
    _lp = Path(log_path) if isinstance(log_path, str) else log_path
    adapter, fd = load_flight_data(_lp, platform)
    results = run_module("sysid", adapter, fd, axis=axis, na=na, nb=nb)

    if not results:
        raise SmartTuneError(
            message="System identification produced no results",
            hint="The log may lack sufficient data or excitation",
            code="E5042",
        )

    return {
        "platform": adapter.name,
        "display_name": adapter.display_name,
        "log_file": _lp.name,
        "duration_s": round(fd.duration_s, 1),
        **serialize_sysid_results(results),
    }


def analyze_filter(
    log_path: Path,
    platform: str = "auto",
    gyro_filter_hz: Optional[float] = None,
    notch_freq_hz: Optional[float] = None,
    auto_derive: bool = True,
    _include_bode_data: bool = False,
) -> Dict[str, Any]:
    """Run filter transfer function analysis (Bode plot data). Matches ``stune filter``.

    Parameters
    ----------
    _include_bode_data : bool
        Internal flag — when True, includes full ``bode_data`` dict
        (freqs, magnitude_db, phase_deg arrays) for plot generation.
        Not exposed to MCP callers directly.
    """
    _lp = Path(log_path) if isinstance(log_path, str) else log_path
    adapter, fd = load_flight_data(_lp, platform)

    if "filter" not in adapter.capabilities():
        raise SmartTuneError(
            message=f"Filter analysis is not supported on {adapter.display_name}",
            hint=f"Supported capabilities: {', '.join(sorted(adapter.capabilities()))}",
            code="E5050",
        )

    try:
        _ft_mod = importlib.import_module(f"smarttune.platform.{adapter.name}.filter_transfer")
    except ImportError:
        raise SmartTuneError(
            message=f"Filter transfer module not available for {adapter.display_name}",
            code="E5051",
        )

    compute_filter_response = _ft_mod.compute_filter_response
    derive_filters_from_params = _ft_mod.derive_filters_from_params
    get_fallback_gyro_filter_hz = _ft_mod.get_fallback_gyro_filter_hz
    get_notch_bandwidth_hz = _ft_mod.get_notch_bandwidth_hz
    build_filter_display_lines = _ft_mod.build_filter_display_lines

    params = fd.params or {}
    sample_rate = fd.sample_rate_hz or 400
    freqs = np.linspace(1, sample_rate / 2, 500)

    use_manual = (gyro_filter_hz is not None or notch_freq_hz is not None) or not auto_derive

    if use_manual:
        current_gyro = (
            gyro_filter_hz if gyro_filter_hz is not None else get_fallback_gyro_filter_hz(params)
        )
        notch_params = None
        if notch_freq_hz is not None and notch_freq_hz > 0:
            notch_params = {
                "center_hz": notch_freq_hz,
                "bandwidth_hz": get_notch_bandwidth_hz(params),
                "attenuation_db": 30,
                "harmonics": 3,
            }
        mag_db, phase_deg = compute_filter_response(freqs, sample_rate, current_gyro, notch_params)
        config_summary = f"GYRO_FILTER={current_gyro:.0f}Hz"
        if notch_freq_hz:
            config_summary += f", Notch={notch_freq_hz:.0f}Hz"
    else:
        cfg = derive_filters_from_params(params)
        config_summary = cfg.get("config_summary", "auto")
        mag_db, phase_deg = compute_filter_response(freqs, sample_rate, params=params)

    # -3dB cutoff
    idx_3db = np.where(mag_db < -3)[0]
    cutoff_3db_hz = float(freqs[idx_3db[0]]) if idx_3db.size > 0 else None

    # Key frequency points for summary
    key_freqs_list = [1, 5, 10, 20, 40, 80, 120, 200]
    key_points = []
    for fk in key_freqs_list:
        if fk >= freqs[-1]:
            break
        idx = int(np.argmin(np.abs(freqs - fk)))
        key_points.append(
            {
                "frequency_hz": fk,
                "magnitude_db": round(float(mag_db[idx]), 1),
                "phase_deg": round(float(phase_deg[idx]), 1),
            }
        )

    # Filter chain info (auto mode)
    filter_chain = None
    if not use_manual:
        try:
            filter_chain = build_filter_display_lines(params)
        except Exception:
            pass

    result = {
        "platform": adapter.name,
        "display_name": adapter.display_name,
        "log_file": _lp.name,
        "mode": "manual" if use_manual else "auto",
        "config_summary": config_summary,
        "cutoff_3db_hz": round(cutoff_3db_hz, 1) if cutoff_3db_hz else None,
        "key_frequency_response": key_points,
        "filter_chain": filter_chain,
        "sample_rate_hz": round(sample_rate, 1),
    }

    if _include_bode_data:
        result["bode_data"] = {
            "freqs": freqs.tolist(),
            "magnitude_db": mag_db.tolist(),
            "phase_deg": phase_deg.tolist(),
        }

    return result


def analyze_hardware(
    log_path: Path,
    platform: str = "auto",
) -> Dict[str, Any]:
    """Run hardware configuration report. Matches ``stune hardware``."""
    _lp = Path(log_path) if isinstance(log_path, str) else log_path
    adapter, fd = load_flight_data(_lp, platform)
    report = run_module("hardware", adapter, fd)

    return {
        "platform": adapter.name,
        "display_name": adapter.display_name,
        "log_file": _lp.name,
        "duration_s": round(fd.duration_s, 1),
        **report,
    }


# ---------------------------------------------------------------------------
# Full analysis — comprehensive (matches `stune analyze`)
# ---------------------------------------------------------------------------


def analyze_log(
    log_path: Path,
    platform: str = "auto",
    axis: str = "all",
    include_modules: Optional[List[str]] = None,
    max_recommendations: int = 20,
) -> Dict[str, Any]:
    """Run comprehensive analysis and return a structured result dict.

    Parameters
    ----------
    log_path : Path
        Path to flight log file.
    platform : str
        "auto" or explicit platform name.
    axis : str
        "all", "roll", "pitch", or "yaw".
    include_modules : list[str] | None
        Subset of ["pid", "fft", "magfit", "hardware", "filter", "sysid"].
        None means run all available modules.
    max_recommendations : int
        Maximum number of parameter recommendations to include.

    Returns
    -------
    dict
        JSON-serializable analysis result with modules, module_failures,
        and safety metadata.
    """
    _lp = Path(log_path) if isinstance(log_path, str) else log_path
    adapter, fd = load_flight_data(_lp, platform)
    capabilities = adapter.capabilities()
    kb = KnowledgeBase(platform=adapter.name)

    full_result = FullAnalysisResult(platform=adapter.name, log_file=_lp.name)
    module_failures: List[Dict[str, str]] = []

    # Determine which modules to run
    all_modules = {"pid", "fft", "magfit", "hardware", "filter", "sysid"}
    if include_modules is not None:
        requested = set(include_modules) & all_modules
    else:
        requested = all_modules

    # --- PID ---
    if "pid" in requested and "pid" in capabilities and fd.pid:
        try:
            full_result.pid = run_module("pid", adapter, fd, kb=kb, axis=axis)
        except Exception as exc:
            logger.warning("PID analysis failed: %s", exc)
            module_failures.append({"module": "pid", "error": str(exc)})

    # --- FFT ---
    if "fft" in requested and "fft" in capabilities and fd.gyro is not None:
        try:
            full_result.fft = run_module("fft", adapter, fd, kb=kb)
        except Exception as exc:
            logger.warning("FFT analysis failed: %s", exc)
            module_failures.append({"module": "fft", "error": str(exc)})

    # --- MagFit ---
    if "magfit" in requested and "magfit" in capabilities and fd.has_mag:
        try:
            full_result.magfit = run_module("magfit", adapter, fd, kb=kb)
        except Exception as exc:
            logger.warning("MagFit analysis failed: %s", exc)
            module_failures.append({"module": "magfit", "error": str(exc)})

    # --- Hardware (platform-specific import, not generic) ---
    hw_dict = None
    if "hardware" in requested and "hardware" in capabilities:
        try:
            hw_dict = run_module("hardware", adapter, fd, kb=kb)
        except Exception as exc:
            logger.warning("Hardware report failed: %s", exc)
            module_failures.append({"module": "hardware", "error": str(exc)})

    # --- SysID ---
    sysid_dict = None
    if "sysid" in requested and "sysid" in capabilities and fd.pid:
        try:
            sysid_results = run_module("sysid", adapter, fd, kb=kb, axis=axis)
            if sysid_results:
                sysid_dict = serialize_sysid_results(sysid_results)
        except Exception as exc:
            logger.warning("SysID analysis failed: %s", exc)
            module_failures.append({"module": "sysid", "error": str(exc)})

    # --- Filter ---
    filter_dict = None
    if "filter" in requested and "filter" in capabilities:
        try:
            _ft_mod = importlib.import_module(f"smarttune.platform.{adapter.name}.filter_transfer")
            params = fd.params or {}
            sample_rate = fd.sample_rate_hz or 400
            freqs = np.linspace(1, sample_rate / 2, 500)

            cfg = _ft_mod.derive_filters_from_params(params)
            config_summary = cfg.get("config_summary", "auto")
            mag_db, phase_deg = _ft_mod.compute_filter_response(freqs, sample_rate, params=params)

            idx_3db = np.where(mag_db < -3)[0]
            cutoff_3db = float(freqs[idx_3db[0]]) if idx_3db.size > 0 else None

            filter_dict = serialize_filter_result(
                cutoff_3db_hz=cutoff_3db,
                config_summary=config_summary,
                freqs=freqs,
                mag_db=mag_db,
                phase_deg=phase_deg,
                sample_rate_hz=sample_rate,
            )
        except Exception as exc:
            logger.warning("Filter analysis failed: %s", exc)
            module_failures.append({"module": "filter", "error": str(exc)})

    # --- Extra analyzers (platform-specific, e.g. Betaflight FF/RPM/DTerm) ---
    extra_results = None
    extra_analyzers = adapter.extra_analyzers()
    if extra_analyzers:
        extra_results = {}
        for ea in extra_analyzers:
            name = getattr(ea, "name", type(ea).__name__)
            try:
                ea_result = ea.analyze(fd)
                extra_results[name] = ea_result
            except Exception as exc:
                logger.warning("Extra analyzer %s failed: %s", name, exc)
                module_failures.append({"module": name, "error": str(exc)})
        if not extra_results:
            extra_results = None

    # Check that at least one module succeeded
    has_any = any(
        [
            full_result.pid,
            full_result.fft,
            full_result.magfit,
            hw_dict,
            sysid_dict,
            filter_dict,
            extra_results,
        ]
    )
    if not has_any:
        if module_failures:
            raise SmartTuneError(
                message="All requested analysis modules failed",
                hint="; ".join(f"{mf['module']}: {mf['error']}" for mf in module_failures),
                code="E5099",
            )
        raise SmartTuneError(
            message="No analysis modules could run on this log",
            hint="The log may lack the required data (PID signals, gyro, mag) for the requested analyses.",
            code="E5098",
        )

    # Serialize
    result = serialize_full_result(full_result, adapter, max_recommendations)

    # Add modules that aren't in FullAnalysisResult dataclass
    if hw_dict is not None:
        result["modules"]["hardware"] = hw_dict
    if sysid_dict is not None:
        result["modules"]["sysid"] = sysid_dict
    if filter_dict is not None:
        result["modules"]["filter"] = filter_dict
    if extra_results is not None:
        result["modules"]["extra"] = serialize_extra_analyzers_results(extra_results)

    result["display_name"] = adapter.display_name
    result["duration_s"] = round(fd.duration_s, 1)
    result["module_failures"] = module_failures
    result["safety"] = {
        "read_only": True,
        "path_validated": True,
        "parameter_write_performed": False,
    }
    # v3.2.1: every recommendation ships pre-validated against the parameter table
    return _attach_validation(result, adapter)
