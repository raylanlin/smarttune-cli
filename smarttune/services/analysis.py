"""
smarttune/services/analysis.py

Pure library layer for flight log analysis.

No Rich output, no shell execution, no arbitrary writes.
Returns structured dataclasses and dicts suitable for JSON serialization.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from smarttune.errors import SmartTuneError
from smarttune.knowledge import KnowledgeBase
from smarttune.models.analysis_result import FullAnalysisResult
from smarttune.models.flight_data import FlightData
from smarttune.platform.base import PlatformAdapter
from smarttune.platform.registry import resolve_adapter
from smarttune.services.serialize import serialize_full_result

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
# Log quality
# ---------------------------------------------------------------------------

def get_log_quality(
    log_path: Path,
    platform: str = "auto",
) -> Dict[str, Any]:
    """Parse a log and return a quality assessment dict.

    The dict contains platform info, data availability flags, duration,
    sample rate, validation issues, and a quality score.
    """
    adapter, fd = load_flight_data(log_path, platform)

    validation_issues = fd.validate()

    # Compute a simple quality score based on data completeness
    score = 100
    deductions = {
        "No PID data available": 40,
        "Insufficient gyro data": 20,
        "Insufficient accel data": 15,
    }
    for issue in validation_issues:
        for pattern, penalty in deductions.items():
            if pattern.lower() in issue.lower():
                score -= penalty
                break
        else:
            # Generic deduction for unrecognized issues
            score -= 5

    if not fd.has_mag:
        score -= 5
    if not fd.has_motor:
        score -= 5
    if not fd.has_battery:
        score -= 3

    score = max(0, min(100, score))

    if score >= 90:
        rating, advice = "EXCELLENT", "Proceed with full analysis"
    elif score >= 70:
        rating, advice = "GOOD", "Proceed with analysis; some data may be limited"
    elif score >= 50:
        rating, advice = "MARGINAL", "Analysis possible but results may be incomplete"
    else:
        rating, advice = "POOR", "Log quality is low; consider re-flying with better logging settings"

    file_size_mb = log_path.stat().st_size / (1024 * 1024)

    return {
        "platform": adapter.name,
        "display_name": adapter.display_name,
        "log_file": log_path.name,
        "file_size_mb": round(file_size_mb, 2),
        "duration_s": round(fd.duration_s, 1),
        "sample_rate_hz": round(fd.sample_rate_hz, 1),
        "axes": fd.axes,
        "has_gyro": fd.gyro is not None and len(fd.gyro) > 0,
        "has_accel": fd.accel is not None and len(fd.accel) > 0,
        "has_mag": fd.has_mag,
        "has_motor": fd.has_motor,
        "has_battery": fd.has_battery,
        "validation_issues": validation_issues,
        "quality": {
            "score": score,
            "rating": rating,
            "advice": advice,
        },
    }


# ---------------------------------------------------------------------------
# Full analysis
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
        Subset of ["pid", "fft", "magfit", "hardware", "filter"].
        None means run all available modules.
    max_recommendations : int
        Maximum number of parameter recommendations to include.

    Returns
    -------
    dict
        JSON-serializable analysis result with modules, module_failures,
        and safety metadata.
    """
    adapter, fd = load_flight_data(log_path, platform)
    capabilities = adapter.capabilities()
    kb = KnowledgeBase(platform=adapter.name)

    full_result = FullAnalysisResult(platform=adapter.name, log_file=log_path.name)
    module_failures: List[Dict[str, str]] = []

    # Determine which modules to run
    available_modules = {"pid", "fft", "magfit", "hardware", "filter"}
    if include_modules is not None:
        requested = set(include_modules) & available_modules
    else:
        requested = available_modules

    # --- PID ---
    if "pid" in requested and "pid" in capabilities and fd.pid:
        try:
            from smarttune.analyzers.pid_reviewer import PIDReviewer
            reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))
            pid_result = reviewer.analyze(fd, axis=axis if axis != "all" else None)
            full_result.pid = pid_result
        except Exception as exc:
            logger.warning("PID analysis failed: %s", exc)
            module_failures.append({"module": "pid", "error": str(exc)})

    # --- FFT ---
    if "fft" in requested and "fft" in capabilities and fd.gyro is not None:
        try:
            from smarttune.analyzers.fft_analyzer import FFTAnalyzer
            fft_analyzer = FFTAnalyzer(knowledge=kb.get("filter_rules", {}))
            fft_result = fft_analyzer.analyze(fd)
            full_result.fft = fft_result
        except Exception as exc:
            logger.warning("FFT analysis failed: %s", exc)
            module_failures.append({"module": "fft", "error": str(exc)})

    # --- MagFit ---
    if "magfit" in requested and "magfit" in capabilities and fd.has_mag:
        try:
            from smarttune.analyzers.magfit import MAGFit
            magfit = MAGFit(knowledge=kb.get("magfit_rules", {}))
            magfit_result = magfit.analyze(fd)
            full_result.magfit = magfit_result
        except Exception as exc:
            logger.warning("MagFit analysis failed: %s", exc)
            module_failures.append({"module": "magfit", "error": str(exc)})

    # --- Hardware ---
    hw_dict = None
    if "hardware" in requested and "hardware" in capabilities:
        try:
            from smarttune.analyzers.hardware_report import generate_hardware_report
            hw_dict = generate_hardware_report(fd.params, flight_data=fd)
        except Exception as exc:
            logger.warning("Hardware report failed: %s", exc)
            module_failures.append({"module": "hardware", "error": str(exc)})

    # Check that at least one module succeeded
    has_any = any([
        full_result.pid, full_result.fft, full_result.magfit, hw_dict
    ])
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
    # Hardware returns a plain dict, so add it directly to modules
    if hw_dict is not None:
        result["modules"]["hardware"] = hw_dict
    result["display_name"] = adapter.display_name
    result["duration_s"] = round(fd.duration_s, 1)
    result["module_failures"] = module_failures
    result["safety"] = {
        "read_only": True,
        "path_validated": True,
        "parameter_write_performed": False,
    }
    return result
