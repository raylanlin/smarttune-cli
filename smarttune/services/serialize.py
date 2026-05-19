"""
smarttune/services/serialize.py

JSON-safe serialization of SmartTune analysis results.

Converts dataclasses, enums, NumPy scalars/arrays, and recommendation
objects into plain dicts/lists suitable for JSON encoding and LLM consumption.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any, Dict, List, Optional

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

from smarttune.models.analysis_result import (
    Assessment,
    AxisPIDResult,
    Confidence,
    FFTAnalysisResult,
    FilterAnalysisResult,
    FullAnalysisResult,
    HardwareReport,
    MagFitResult,
    ParamRecommendation,
    PIDAnalysisResult,
    StepMetrics,
    VibrationPeak,
)
from smarttune.platform.base import PlatformAdapter


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def to_jsonable(value: Any) -> Any:
    """Recursively convert a value to a JSON-serializable form.

    Handles:
    - None, str, int, float, bool → pass through
    - Enum → .value
    - NumPy scalar → .item()
    - NumPy array → summarized (length + first few values for small, stats for large)
    - dataclass → dict of fields
    - dict → recurse values
    - list/tuple → recurse elements
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if _HAS_NUMPY:
        if isinstance(value, (np.integer,)):
            return int(value)
        if isinstance(value, (np.floating,)):
            return _safe_float(float(value))
        if isinstance(value, np.bool_):
            return bool(value)
        if isinstance(value, np.ndarray):
            return _summarize_array(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    # Fallback
    return str(value)


def _safe_float(v: float) -> float | None:
    """Replace NaN/Inf with None for clean JSON."""
    if _HAS_NUMPY and (np.isnan(v) or np.isinf(v)):
        return None
    return round(v, 6) if abs(v) < 1e9 else v


def _summarize_array(arr: "np.ndarray") -> Dict[str, Any]:
    """Produce a compact summary of a NumPy array instead of dumping raw data."""
    if arr.size == 0:
        return {"shape": list(arr.shape), "values": []}
    if arr.size <= 10:
        return {"shape": list(arr.shape), "values": [to_jsonable(x) for x in arr.flat]}
    # Large arrays: return stats only
    summary: Dict[str, Any] = {
        "shape": list(arr.shape),
        "length": int(arr.size),
    }
    try:
        summary["min"] = _safe_float(float(np.nanmin(arr)))
        summary["max"] = _safe_float(float(np.nanmax(arr)))
        summary["mean"] = _safe_float(float(np.nanmean(arr)))
        summary["std"] = _safe_float(float(np.nanstd(arr)))
    except (TypeError, ValueError):
        pass
    return summary


# ---------------------------------------------------------------------------
# Domain-specific serializers
# ---------------------------------------------------------------------------

def serialize_param_recommendation(
    rec: ParamRecommendation,
    adapter: Optional[PlatformAdapter] = None,
) -> Dict[str, Any]:
    """Serialize a single parameter recommendation."""
    d: Dict[str, Any] = {
        "generic_param": rec.param.generic_name,
        "current": _safe_float(rec.current),
        "suggested": _safe_float(rec.suggested),
        "change_percent": _safe_float(rec.change_percent),
        "action": rec.action or ("increase" if rec.suggested > rec.current else "decrease"),
        "confidence": rec.confidence.value if isinstance(rec.confidence, Enum) else str(rec.confidence),
        "reason": rec.reason,
    }
    if adapter is not None:
        try:
            d["param"] = adapter.map_param_to_platform(rec.param.generic_name)
        except Exception:
            d["param"] = rec.param.generic_name
    else:
        d["param"] = rec.param.generic_name
    return d


def serialize_step_metrics(metrics: StepMetrics) -> Dict[str, Any]:
    """Serialize PID step response metrics."""
    return {
        "rise_time_ms": _safe_float(metrics.rise_time_ms),
        "overshoot_percent": _safe_float(metrics.overshoot_percent),
        "settling_time_ms": _safe_float(metrics.settling_time_ms),
        "oscillation_count": metrics.oscillation_count,
        "steady_state_error_percent": _safe_float(metrics.steady_state_error_percent),
    }


def serialize_pid_result(
    result: PIDAnalysisResult,
    adapter: Optional[PlatformAdapter] = None,
    max_recommendations: int = 20,
) -> Dict[str, Any]:
    """Serialize full PID analysis result (compact, no raw_data/step_responses)."""
    axes: Dict[str, Any] = {}
    total_recs = 0
    for axis_name, axis_result in result.axes.items():
        recs = axis_result.recommendations
        remaining = max(0, max_recommendations - total_recs)
        serialized_recs = [
            serialize_param_recommendation(r, adapter)
            for r in recs[:remaining]
        ]
        total_recs += len(serialized_recs)
        axes[axis_name] = {
            "assessment": axis_result.assessment.value if isinstance(axis_result.assessment, Enum) else str(axis_result.assessment),
            "step_count": axis_result.step_count,
            "metrics": serialize_step_metrics(axis_result.metrics),
            "recommendations": serialized_recs,
        }
    return {
        "overall_assessment": result.overall_assessment.value if isinstance(result.overall_assessment, Enum) else str(result.overall_assessment),
        "axes": axes,
    }


def serialize_fft_result(
    result,
    adapter: Optional[PlatformAdapter] = None,
    max_recommendations: int = 20,
) -> Dict[str, Any]:
    """Serialize FFT analysis result (no raw spectrum data)."""
    # Handle both dataclass (old) and dict (new optimized) formats
    if isinstance(result, dict):
        # Dict format from optimized analyzers
        peaks_raw = result.get("peaks", [])
        peaks = [
            {
                "frequency_hz": _safe_float(p.get("freq", p.get("frequency_hz", 0))),
                "amplitude": _safe_float(p.get("power_db", p.get("amplitude", 0))),
                "source_guess": p.get("source", p.get("source_guess", "")),
            }
            for p in peaks_raw[:20]
        ]
        recs_raw = result.get("recommendations", [])
        recs = [
            serialize_param_recommendation(r, adapter)
            for r in recs_raw[:max_recommendations]
        ] if isinstance(recs_raw, list) else []
        return {
            "vibration_level": result.get("vibration_level", ""),
            "noise_floor": _safe_float(result.get("noise_floor", 0)),
            "peaks": peaks,
            "recommendations": recs,
        }

    # Dataclass format (original)
    peaks = [
        {
            "frequency_hz": _safe_float(p.frequency_hz),
            "amplitude": _safe_float(p.amplitude),
            "source_guess": p.source_guess,
        }
        for p in result.peaks[:20]  # Cap peaks
    ]
    recs = [
        serialize_param_recommendation(r, adapter)
        for r in result.recommendations[:max_recommendations]
    ]
    return {
        "vibration_level": result.vibration_level.value if isinstance(result.vibration_level, Enum) else str(result.vibration_level),
        "noise_floor": _safe_float(result.noise_floor),
        "peaks": peaks,
        "recommendations": recs,
    }


def serialize_magfit_result(
    result: MagFitResult,
    adapter: Optional[PlatformAdapter] = None,
    max_recommendations: int = 20,
) -> Dict[str, Any]:
    """Serialize magnetometer calibration result."""
    recs = [
        serialize_param_recommendation(r, adapter)
        for r in result.recommendations[:max_recommendations]
    ]
    return {
        "assessment": result.assessment.value if isinstance(result.assessment, Enum) else str(result.assessment),
        "fitness_mgauss": _safe_float(result.fitness_mgauss),
        "offsets": {k: _safe_float(v) for k, v in result.offsets.items()},
        "recommendations": recs,
    }


def serialize_hardware_report(report: HardwareReport) -> Dict[str, Any]:
    """Serialize hardware configuration report."""
    return {
        "firmware_version": report.firmware_version,
        "board_name": report.board_name,
        "imu_configs": to_jsonable(report.imu_configs),
        "compass_configs": to_jsonable(report.compass_configs),
        "filter_config": to_jsonable(report.filter_config),
        "pid_params": to_jsonable(report.pid_params),
        "integrity_issues": report.integrity_issues,
    }


def serialize_sysid_results(results: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize system identification results.

    Accepts a dict of {axis_name: SysIDResult} from SysIDAnalyzer.analyze().
    """
    axes: Dict[str, Any] = {}
    for axis_name, result in results.items():
        # SysIDResult has a .to_dict() method
        if hasattr(result, 'to_dict'):
            axes[axis_name] = result.to_dict()
        elif isinstance(result, dict):
            axes[axis_name] = to_jsonable(result)
        else:
            axes[axis_name] = to_jsonable(result)
    return {"axes": axes}


def serialize_filter_result(
    cutoff_3db_hz: Optional[float],
    config_summary: str,
    freqs: Any,
    mag_db: Any,
    phase_deg: Any,
    sample_rate_hz: float,
) -> Dict[str, Any]:
    """Serialize filter transfer function analysis to a compact dict.

    Returns key frequency points instead of full arrays.
    """
    key_freqs_list = [1, 5, 10, 20, 40, 80, 120, 200]
    key_points = []
    if _HAS_NUMPY and isinstance(freqs, np.ndarray):
        for fk in key_freqs_list:
            if fk >= freqs[-1]:
                break
            idx = int(np.argmin(np.abs(freqs - fk)))
            key_points.append({
                "frequency_hz": fk,
                "magnitude_db": round(float(mag_db[idx]), 1),
                "phase_deg": round(float(phase_deg[idx]), 1),
            })

    return {
        "config_summary": config_summary,
        "cutoff_3db_hz": round(cutoff_3db_hz, 1) if cutoff_3db_hz is not None else None,
        "key_frequency_response": key_points,
        "sample_rate_hz": round(sample_rate_hz, 1),
    }


def serialize_extra_analyzers_results(results: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize results from platform extra analyzers (e.g. Betaflight FF/RPM/DTerm)."""
    out: Dict[str, Any] = {}
    for name, result in results.items():
        if hasattr(result, 'to_dict'):
            out[name] = result.to_dict()
        elif isinstance(result, dict):
            out[name] = to_jsonable(result)
        else:
            out[name] = to_jsonable(result)
    return out


def serialize_full_result(
    result: FullAnalysisResult,
    adapter: Optional[PlatformAdapter] = None,
    max_recommendations: int = 20,
) -> Dict[str, Any]:
    """Serialize a complete FullAnalysisResult to a JSON-safe dict."""
    modules: Dict[str, Any] = {}
    if result.pid is not None:
        modules["pid"] = serialize_pid_result(result.pid, adapter, max_recommendations)
    if result.fft is not None:
        modules["fft"] = serialize_fft_result(result.fft, adapter, max_recommendations)
    if result.magfit is not None:
        modules["magfit"] = serialize_magfit_result(result.magfit, adapter, max_recommendations)
    if result.hardware is not None:
        modules["hardware"] = serialize_hardware_report(result.hardware)
    return {
        "platform": result.platform,
        "log_file": result.log_file,
        "modules": modules,
    }
