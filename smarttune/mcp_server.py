"""
smarttune/mcp_server.py

Read-only MCP (Model Context Protocol) server for SmartTune.

Exposes flight log analysis tools for LLM agents (e.g. OpenClaw customer
service) without shell execution, arbitrary file writes, or parameter mutation.

Tool parity with CLI:
  smarttune_list_platforms     ↔  stune platforms
  smarttune_log_quality        ↔  stune quality
  smarttune_analyze_log        ↔  stune analyze
  smarttune_analyze_pid        ↔  stune pid
  smarttune_analyze_fft        ↔  stune fft
  smarttune_analyze_magfit     ↔  stune magfit
  smarttune_analyze_sysid      ↔  stune sysid
  smarttune_analyze_filter     ↔  stune filter
  smarttune_analyze_hardware   ↔  stune hardware
  smarttune_generate_plot      ↔  stune <cmd> --visual
  smarttune_list_param_groups  ↔  stune params <platform> --groups
  smarttune_list_params        ↔  stune params <platform> --group X / -c pid
  smarttune_get_param          ↔  stune params <NAME>
  smarttune_search_params      ↔  stune params --search
  smarttune_validate_param     ↔  stune params --validate

Response contract (v3.2):
  Success  {"ok": true,  ...payload}
  Failure  {"ok": false, "error_code", "message", "hint", "retryable"}
  One shape for every tool, so clients branch on fields instead of prose.
  A rejected parameter value is a successful call with valid=false — not an error.

Security boundaries:
  - No subprocess / os.system / shell commands
  - No arbitrary output paths
  - Path validation: allowed roots, extensions, file size, symlink resolution
  - All tools are read-only and idempotent
  - stdout carries JSON-RPC only: service calls run inside _quiet_stdout()
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from smarttune.errors import SmartTuneError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWED_ROOTS = [
    Path.cwd(),
    Path.home() / ".openclaw" / "workspace" / "files" / "inbox",
    Path.home() / ".openclaw" / "workspace" / "files" / "output",
    Path.home() / ".openclaw" / "media",
    Path("/tmp"),
]

_ALLOWED_EXTENSIONS = {".bin", ".log", ".bbl", ".bfl", ".ulg"}


def _get_max_file_mb() -> float:
    """Read max file size from env at call time (not import time)."""
    try:
        return float(os.environ.get("SMARTTUNE_MCP_MAX_FILE_MB", "300"))
    except (ValueError, TypeError):
        return 300.0


def _get_allowed_roots() -> List[Path]:
    """Return allowed root directories from env or defaults."""
    env_val = os.environ.get("SMARTTUNE_MCP_ALLOWED_ROOTS", "")
    if env_val.strip():
        return [Path(p).expanduser().resolve() for p in env_val.split(":") if p.strip()]
    return [
        p.expanduser().resolve() for p in _DEFAULT_ALLOWED_ROOTS if _safe_resolve(p) is not None
    ]


def _safe_resolve(p: Path) -> Optional[Path]:
    """Resolve a path, returning None if it doesn't exist."""
    try:
        return p.expanduser().resolve()
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


class PathValidationError(Exception):
    """Raised when a log path fails security validation."""


def validate_log_path(raw_path: str) -> Path:
    """Validate and resolve a log file path.

    Steps:
      1. Expand ~ and resolve symlinks (strict=True → must exist)
      2. Verify it is a regular file
      3. Check extension is in allowed set
      4. Check resolved path is under an allowed root
      5. Check file size <= configured limit

    Returns the resolved Path on success; raises PathValidationError otherwise.
    """
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise PathValidationError(f"Cannot resolve path: {raw_path} ({exc})")

    # Must be a file
    if not resolved.is_file():
        raise PathValidationError(f"Not a regular file: {raw_path}")

    # Extension check
    suffix = resolved.suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise PathValidationError(
            f"Disallowed file extension '{suffix}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
        )

    # Allowed root check
    allowed_roots = _get_allowed_roots()
    if not any(_is_relative_to(resolved, root) for root in allowed_roots):
        raise PathValidationError(
            f"Path is outside allowed directories. "
            f"Allowed roots: {[str(r) for r in allowed_roots]}"
        )

    # Size check
    max_file_mb = _get_max_file_mb()
    size_mb = resolved.stat().st_size / (1024 * 1024)
    if size_mb > max_file_mb:
        raise PathValidationError(f"File too large: {size_mb:.1f} MB (limit: {max_file_mb:.0f} MB)")

    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    """Check if path is relative to root (compatible with Python 3.9+)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Helper: wrap service calls with path validation and error handling
# ---------------------------------------------------------------------------

#: Error codes worth retrying. Everything else is deterministic — a retry
#: returns the identical failure, so clients should surface it instead.
_RETRYABLE_CODES = {"E9999"}


_PLATFORM_KEYS = {
    "ap": "ardupilot",
    "apm": "ardupilot",
    "ardupilot": "ardupilot",
    "bf": "betaflight",
    "betaflight": "betaflight",
    "px4": "px4",
    "pixhawk": "px4",
}


def _resolve_platform_key(value: str) -> str:
    """Normalise a platform argument ("ap" / "APM" / "ArduPilot" → "ardupilot")."""
    return _PLATFORM_KEYS.get(str(value).strip().lower(), str(value).strip().lower())


def _dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _ok(payload: Dict[str, Any]) -> str:
    """Success envelope: ok=True plus the payload's own fields."""
    body: Dict[str, Any] = {"ok": True}
    body.update(payload)
    return _dumps(body)


def _err(
    message: str,
    error_code: str = "E4000",
    hint: str = "",
    retryable: Optional[bool] = None,
    **extra: Any,
) -> str:
    """Failure envelope — one shape for every tool.

    {ok: false, error_code, message, hint, retryable}. Clients branch on
    retryable instead of pattern-matching prose. (Was: three different shapes —
    bare {"error": str}, {"error","code","hint"}, and {"valid": false,"error"} —
    with no retry signal at all.)
    """
    body: Dict[str, Any] = {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "hint": hint,
        "retryable": bool(error_code in _RETRYABLE_CODES if retryable is None else retryable),
        # legacy key, kept one release for existing prompts/clients
        "error": message,
    }
    body.update(extra)
    return _dumps(body)


@contextlib.contextmanager
def _quiet_stdout():
    """Redirect stdout to stderr for the duration of a service call.

    stdio MCP transport allows ONLY JSON-RPC frames on stdout. SmartTune itself
    logs to stderr, but third-party parsers on the analysis path (pyulog,
    pymavlink, matplotlib backends) may print — one stray line corrupts the
    stream and the client sees a hang. Anything printed inside this block lands
    on stderr instead.
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


def _call_service(func, log_path: str, **kwargs) -> str:
    """Validate path, call service function, return a JSON envelope."""
    try:
        resolved = validate_log_path(log_path)
    except PathValidationError as exc:
        return _err(
            str(exc),
            error_code="E1001",
            hint="Pass a path inside SMARTTUNE_MCP_ALLOWED_ROOTS with a supported extension",
        )

    try:
        with _quiet_stdout():
            result = func(log_path=resolved, **kwargs)
        return _ok(result if isinstance(result, dict) else {"result": result})
    except SmartTuneError as exc:
        return _err(exc.message, error_code=exc.code, hint=exc.hint)
    except Exception as exc:
        logger.exception("Unexpected error in %s", func.__name__)
        return _err(
            f"Unexpected error: {exc}",
            error_code="E9999",
            hint="This is an internal failure; retrying may help",
        )


# ---------------------------------------------------------------------------
# Markdown report renderer
# ---------------------------------------------------------------------------


def _render_markdown(result: Dict[str, Any]) -> str:
    """Render analysis result dict as a compact Markdown report."""
    lines: List[str] = []
    lines.append("# SmartTune Analysis Report")
    lines.append("")
    lines.append(f"**Platform:** {result.get('display_name', result.get('platform', 'unknown'))}")
    lines.append(f"**Log file:** {result.get('log_file', 'unknown')}")
    if "duration_s" in result:
        lines.append(f"**Duration:** {result['duration_s']}s")
    lines.append("")

    modules = result.get("modules", {})

    # PID
    if "pid" in modules:
        pid = modules["pid"]
        lines.append(f"## PID Analysis — {pid.get('overall_assessment', 'N/A')}")
        lines.append("")
        for axis_name, axis_data in pid.get("axes", {}).items():
            lines.append(
                f"### {axis_name.capitalize()} Axis — {axis_data.get('assessment', 'N/A')}"
            )
            metrics = axis_data.get("metrics", {})
            lines.append(f"- Steps detected: {axis_data.get('step_count', 0)}")
            if metrics.get("rise_time_ms") is not None:
                lines.append(f"- Rise time: {metrics['rise_time_ms']} ms")
            if metrics.get("overshoot_percent") is not None:
                lines.append(f"- Overshoot: {metrics['overshoot_percent']}%")
            if metrics.get("settling_time_ms") is not None:
                lines.append(f"- Settling time: {metrics['settling_time_ms']} ms")
            recs = axis_data.get("recommendations", [])
            if recs:
                lines.append("")
                lines.append("| Parameter | Current | Suggested | Change | Confidence | Reason |")
                lines.append("|-----------|---------|-----------|--------|------------|--------|")
                for r in recs:
                    lines.append(
                        f"| {r.get('param', '')} | {r.get('current', '')} | "
                        f"{r.get('suggested', '')} | {r.get('change_percent', '')}% | "
                        f"{r.get('confidence', '')} | {r.get('reason', '')} |"
                    )
            lines.append("")

    # FFT
    if "fft" in modules:
        fft = modules["fft"]
        lines.append(f"## FFT Vibration Analysis — {fft.get('vibration_level', 'N/A')}")
        lines.append("")
        peaks = fft.get("peaks", [])
        if peaks:
            lines.append("| Frequency (Hz) | Amplitude | Source |")
            lines.append("|----------------|-----------|--------|")
            for p in peaks:
                lines.append(
                    f"| {p.get('frequency_hz', '')} | {p.get('amplitude', '')} | {p.get('source_guess', '')} |"
                )
            lines.append("")
        recs = fft.get("recommendations", [])
        if recs:
            for r in recs:
                lines.append(
                    f"- **{r.get('param', '')}**: {r.get('current', '')} → {r.get('suggested', '')} ({r.get('reason', '')})"
                )
            lines.append("")

    # MagFit
    if "magfit" in modules:
        mag = modules["magfit"]
        lines.append(f"## Magnetometer Calibration — {mag.get('assessment', 'N/A')}")
        lines.append("")
        lines.append(f"- Fitness: {mag.get('fitness_mgauss', '')} mGauss")
        offsets = mag.get("offsets", {})
        if offsets:
            lines.append(
                f"- Offsets: X={offsets.get('x', '?')}, Y={offsets.get('y', '?')}, Z={offsets.get('z', '?')}"
            )
        lines.append("")

    # Hardware
    if "hardware" in modules:
        hw = modules["hardware"]
        lines.append("## Hardware Configuration")
        lines.append("")
        if hw.get("firmware"):
            lines.append(f"- Firmware: {hw['firmware']}")
        elif hw.get("version_info", {}).get("firmware"):
            lines.append(f"- Firmware: {hw['version_info']['firmware']}")
        if hw.get("board_name"):
            lines.append(f"- Board: {hw['board_name']}")
        elif hw.get("sys_info", {}).get("board_name"):
            lines.append(f"- Board: {hw['sys_info']['board_name']}")
        issues = hw.get("integrity_issues", [])
        if issues:
            lines.append("- Issues:")
            for issue in issues:
                lines.append(f"  - {issue}")
        lines.append("")

    # SysID
    if "sysid" in modules:
        sysid = modules["sysid"]
        lines.append("## System Identification (ARX)")
        lines.append("")
        for axis_name, axis_data in sysid.get("axes", {}).items():
            lines.append(f"### {axis_name.capitalize()} Axis")
            cont = axis_data.get("continuous_approximation", {})
            pid_rec = axis_data.get("pid_recommendations", {})
            fit = axis_data.get("fit_quality", {})
            if cont:
                lines.append(f"- Natural frequency: {cont.get('natural_freq_hz', '?')} Hz")
                lines.append(f"- Damping ratio: {cont.get('damping_ratio', '?')}")
            if pid_rec:
                lines.append(
                    f"- Suggested bandwidth: {pid_rec.get('suggested_bandwidth_hz', '?')} Hz"
                )
                lines.append(f"- Suggested P gain: {pid_rec.get('suggested_p_gain', '?')}")
            if fit:
                lines.append(f"- Fit quality: {fit.get('fit_percent', '?')}%")
            lines.append("")

    # Filter
    if "filter" in modules:
        filt = modules["filter"]
        lines.append("## Filter Transfer Function")
        lines.append("")
        lines.append(f"- Config: {filt.get('config_summary', 'N/A')}")
        if filt.get("cutoff_3db_hz"):
            lines.append(f"- -3dB cutoff: {filt['cutoff_3db_hz']} Hz")
        kfr = filt.get("key_frequency_response", [])
        if kfr:
            lines.append("")
            lines.append("| Freq (Hz) | Magnitude (dB) | Phase (°) |")
            lines.append("|-----------|----------------|-----------|")
            for pt in kfr:
                lines.append(f"| {pt['frequency_hz']} | {pt['magnitude_db']} | {pt['phase_deg']} |")
        lines.append("")

    # Extra analyzers
    if "extra" in modules:
        lines.append("## Platform-Specific Analysis")
        lines.append("")
        for name, data in modules["extra"].items():
            lines.append(f"### {name}")
            if isinstance(data, dict):
                for k, v in data.items():
                    lines.append(f"- {k}: {v}")
            else:
                lines.append(str(data))
            lines.append("")

    # Module failures
    failures = result.get("module_failures", [])
    if failures:
        lines.append("## Module Failures")
        lines.append("")
        for f in failures:
            lines.append(f"- **{f.get('module', '?')}**: {f.get('error', 'unknown error')}")
        lines.append("")

    # Safety footer
    lines.append("---")
    lines.append("*Read-only analysis. No parameters were written to the flight controller.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool annotations — shared across all tools
# ---------------------------------------------------------------------------

_READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "smarttune_mcp",
    instructions=(
        "SmartTune MCP — Read-only flight log analysis tools for multi-rotor drones. "
        "Supports ArduPilot (.bin/.log), Betaflight (.bbl/.bfl), and PX4 (.ulg) logs. "
        "All tools are safe, idempotent, and never write parameters to the flight controller.\n\n"
        "Available tools:\n"
        "  smarttune_list_platforms    — List supported platforms and capabilities\n"
        "  smarttune_log_quality      — Assess log data quality before analysis\n"
        "  smarttune_analyze_log      — Comprehensive analysis (all modules)\n"
        "  smarttune_analyze_pid      — PID step response analysis\n"
        "  smarttune_analyze_fft      — FFT vibration spectrum analysis\n"
        "  smarttune_analyze_magfit   — Magnetometer calibration analysis\n"
        "  smarttune_analyze_sysid    — ARX system identification\n"
        "  smarttune_analyze_filter   — Filter transfer function (Bode plot)\n"
        "  smarttune_analyze_hardware — Hardware configuration report\n"
        "  smarttune_generate_plot    — Generate base64 PNG chart (pid/fft/filter)\n"
        "  smarttune_list_param_groups — Browse a platform's parameter groups (start here)\n"
        "  smarttune_list_params      — List parameters in one group or category (compact rows)\n"
        "  smarttune_get_param        — Full definition of one parameter, incl. enum meanings\n"
        "  smarttune_search_params    — Ranked keyword search across names/descriptions/enums\n"
        "  smarttune_validate_param   — Validate a parameter name + value before recommending\n\n"
        "Every tool returns {ok: true, ...} or {ok: false, error_code, message, hint, retryable}.\n\n"
        "Recommended workflow: list_platforms → log_quality → analyze_log (or individual tools)\n"
        "Parameter lookup: list_param_groups → list_params(group=…) → get_param(name) for detail.\n"
        "  Never list a whole table: ArduPilot alone is ~2,800 parameters.\n"
        "Parameter validation: BEFORE recommending parameter changes, ALWAYS call\n"
        "  smarttune_validate_param. It checks enum membership as well as numeric range;\n"
        "  status='unverifiable' means the table cannot confirm the value — do NOT proceed\n"
        "  as if it were valid. Enum meanings come from get_param (e.g. BATT_MONITOR 4 =\n"
        "  'Analog Voltage and Current').\n"
        "For visual reports: analyze first, then generate_plot for the relevant chart type."
    ),
)


# ── 1. List Platforms ──────────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_list_platforms() -> str:
    """List all supported flight controller platforms, their log file extensions, and analysis capabilities.

    Returns JSON with platform names, display names, extensions, and capabilities.
    Use this to determine which platforms SmartTune supports before analyzing a log.
    """
    from smarttune.platform.registry import list_platforms

    platforms = list_platforms()
    # Convert capabilities from comma-separated string to list
    for p in platforms:
        if isinstance(p.get("capabilities"), str):
            p["capabilities"] = [c.strip() for c in p["capabilities"].split(",") if c.strip()]
        if isinstance(p.get("extensions"), str):
            cli_exts = [e.strip() for e in p["extensions"].split(",") if e.strip()]
        else:
            cli_exts = p.get("extensions", [])
        p["mcp_accepted_extensions"] = [e for e in cli_exts if e in _ALLOWED_EXTENSIONS]
    return _ok(
        {
            "platforms": platforms,
            "mcp_allowed_extensions": sorted(_ALLOWED_EXTENSIONS),
        }
    )


# ── 2. Log Quality ────────────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_log_quality(
    log_path: str,
    platform: str = "auto",
) -> str:
    """Assess flight log data quality for analysis.

    Evaluates data completeness (PID, gyro, mag, motor, battery), flight duration,
    stick excitation (step response windows per axis), and sample rate consistency
    (jitter, drop rate). Returns a quality score (0-100) with rating and advice.

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        platform: Platform override — "auto" (default), "ardupilot", "betaflight", or "px4".
    """
    from smarttune.services.analysis import get_log_quality

    return _call_service(get_log_quality, log_path, platform=platform)


# ── 3. Comprehensive Analysis ─────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_analyze_log(
    log_path: str,
    platform: str = "auto",
    axis: str = "all",
    include_modules: Optional[List[str]] = None,
    response_format: str = "json",
    max_recommendations: int = 20,
) -> str:
    """Run comprehensive flight log analysis and return structured recommendations.

    Runs all available analysis modules: PID tuning, vibration (FFT), magnetometer
    calibration, hardware config, system identification (ARX), and filter transfer
    function. Returns compact results suitable for explaining to users.

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        platform: Platform override — "auto" (default), "ardupilot", "betaflight", or "px4".
        axis: Axis to analyze — "all" (default), "roll", "pitch", or "yaw".
        include_modules: Subset of ["pid", "fft", "magfit", "hardware", "filter", "sysid"]. None = all available.
        response_format: Output format — "json" (default) or "markdown".
        max_recommendations: Maximum number of parameter recommendations (1–100, default 20).
    """
    # Clamp max_recommendations
    max_recommendations = max(1, min(100, max_recommendations))

    # Validate axis
    if axis not in ("all", "roll", "pitch", "yaw"):
        return _err(
            f"Invalid axis: {axis!r}", error_code="E4001", hint="Use all, roll, pitch, or yaw"
        )

    # Validate response_format
    if response_format not in ("json", "markdown"):
        return _err(
            f"Invalid response_format: {response_format!r}",
            error_code="E4000",
            hint="Use json or markdown",
        )

    # Validate include_modules
    valid_modules = {"pid", "fft", "magfit", "hardware", "filter", "sysid"}
    if include_modules is not None:
        invalid = set(include_modules) - valid_modules
        if invalid:
            return _err(
                f"Invalid modules: {sorted(invalid)}",
                error_code="E4000",
                hint=f"Valid modules: {sorted(valid_modules)}",
            )

    try:
        resolved = validate_log_path(log_path)
    except PathValidationError as exc:
        return _err(
            str(exc),
            error_code="E1001",
            hint="Pass a path inside SMARTTUNE_MCP_ALLOWED_ROOTS with a supported extension",
        )

    try:
        from smarttune.services.analysis import analyze_log

        with _quiet_stdout():
            result = analyze_log(
                log_path=resolved,
                platform=platform,
                axis=axis,
                include_modules=include_modules,
                max_recommendations=max_recommendations,
            )

        if response_format == "markdown":
            return _render_markdown(result)

        return _ok(result)

    except SmartTuneError as exc:
        return _err(exc.message, error_code=exc.code, hint=exc.hint)
    except Exception as exc:
        logger.exception("Unexpected error in smarttune_analyze_log")
        return _err(
            f"Unexpected error: {exc}",
            error_code="E9999",
            hint="This is an internal failure; retrying may help",
        )


# ── 4. PID Analysis ───────────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_analyze_pid(
    log_path: str,
    platform: str = "auto",
    axis: str = "all",
    max_recommendations: int = 20,
) -> str:
    """PID step response analysis — detect step responses and evaluate tuning quality.

    Analyzes stick-input step responses and evaluates rise time, overshoot,
    settling time, oscillation count. Provides per-axis diagnostics with
    specific parameter tuning recommendations.

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        platform: Platform override — "auto", "ardupilot", "betaflight", or "px4".
        axis: Axis to analyze — "all" (default), "roll", "pitch", or "yaw".
        max_recommendations: Maximum parameter recommendations (1–100, default 20).
    """
    if axis not in ("all", "roll", "pitch", "yaw"):
        return _err(
            f"Invalid axis: {axis!r}", error_code="E4001", hint="Use all, roll, pitch, or yaw"
        )

    from smarttune.services.analysis import analyze_pid

    return _call_service(
        analyze_pid,
        log_path,
        platform=platform,
        axis=axis,
        max_recommendations=max(1, min(100, max_recommendations)),
    )


# ── 5. FFT Analysis ───────────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_analyze_fft(
    log_path: str,
    platform: str = "auto",
    max_recommendations: int = 20,
) -> str:
    """FFT vibration spectrum analysis — identify vibration frequencies and severity.

    Analyzes gyro data to identify vibration peaks, noise floor, and suggests
    notch filter parameters (e.g. INS_HNTCH_FREQ, INS_HNTCH_BW for ArduPilot).
    Returns vibration severity rating (EXCELLENT/GOOD/MARGINAL/POOR).

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        platform: Platform override — "auto", "ardupilot", "betaflight", or "px4".
        max_recommendations: Maximum parameter recommendations (1–100, default 20).
    """
    from smarttune.services.analysis import analyze_fft

    return _call_service(
        analyze_fft,
        log_path,
        platform=platform,
        max_recommendations=max(1, min(100, max_recommendations)),
    )


# ── 6. MagFit Analysis ────────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_analyze_magfit(
    log_path: str,
    platform: str = "auto",
    max_recommendations: int = 20,
) -> str:
    """Magnetometer calibration analysis — evaluate compass calibration quality.

    Evaluates compass fitness score (mGauss), hard/soft iron interference,
    flight coverage (yaw/pitch/roll range), and provides calibration
    offset recommendations.

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        platform: Platform override — "auto", "ardupilot", "betaflight", or "px4".
        max_recommendations: Maximum parameter recommendations (1–100, default 20).
    """
    from smarttune.services.analysis import analyze_magfit

    return _call_service(
        analyze_magfit,
        log_path,
        platform=platform,
        max_recommendations=max(1, min(100, max_recommendations)),
    )


# ── 7. System Identification ──────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_analyze_sysid(
    log_path: str,
    platform: str = "auto",
    axis: str = "all",
    na: int = 3,
    nb: int = 2,
) -> str:
    """ARX system identification — estimate transfer function from flight data.

    Fits an ARX(na,nb) model to PID rate data, extracts natural frequency,
    damping ratio, DC gain, and provides PID bandwidth recommendations.

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        platform: Platform override — "auto", "ardupilot", "betaflight", or "px4".
        axis: Axis to analyze — "all" (default), "roll", "pitch", or "yaw".
        na: ARX model A polynomial order (default 3).
        nb: ARX model B polynomial order (default 2).
    """
    if axis not in ("all", "roll", "pitch", "yaw"):
        return _err(
            f"Invalid axis: {axis!r}", error_code="E4001", hint="Use all, roll, pitch, or yaw"
        )
    na = max(1, min(10, na))
    nb = max(1, min(10, nb))

    from smarttune.services.analysis import analyze_sysid

    return _call_service(
        analyze_sysid,
        log_path,
        platform=platform,
        axis=axis,
        na=na,
        nb=nb,
    )


# ── 8. Filter Analysis ────────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_analyze_filter(
    log_path: str,
    platform: str = "auto",
    gyro_filter_hz: Optional[float] = None,
    notch_freq_hz: Optional[float] = None,
    auto_derive: bool = True,
) -> str:
    """Filter transfer function analysis (Bode plot data).

    Two modes:
      - Auto mode (default): derive filter config from log parameters
      - Manual mode: specify gyro_filter_hz / notch_freq_hz directly

    Returns key frequency response points, -3dB cutoff, config summary,
    and filter chain details (auto mode).

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        platform: Platform override — "auto", "ardupilot", "betaflight", or "px4".
        gyro_filter_hz: Override GYRO_FILTER cutoff frequency (Hz). Switches to manual mode.
        notch_freq_hz: Specify Notch center frequency (Hz). Switches to manual mode.
        auto_derive: Auto-derive filter config from log parameters (default: true).
    """
    from smarttune.services.analysis import analyze_filter

    return _call_service(
        analyze_filter,
        log_path,
        platform=platform,
        gyro_filter_hz=gyro_filter_hz,
        notch_freq_hz=notch_freq_hz,
        auto_derive=auto_derive,
    )


# ── 9. Hardware Report ─────────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_analyze_hardware(
    log_path: str,
    platform: str = "auto",
) -> str:
    """Hardware configuration report — sensor setup, filter config, PID parameters.

    Displays IMU setup (gyro/accel IDs, calibration status), compass configuration,
    active filter settings, rate PID parameters, battery report, and firmware/board info.

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        platform: Platform override — "auto", "ardupilot", "betaflight", or "px4".
    """
    from smarttune.services.analysis import analyze_hardware

    return _call_service(analyze_hardware, log_path, platform=platform)


# ── 10. Plot Generation ──────────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_generate_plot(
    log_path: str,
    plot_type: str = "pid",
    platform: str = "auto",
    axis: str = "all",
    theme: str = "light",
) -> str:
    """Generate an analysis chart as a base64 PNG image.

    Returns a data URL (data:image/png;base64,...) that can be displayed
    directly in an <img> tag or rendered by an agent's image viewer.

    Available plot types:
      - "pid"    — PID step response curve (per axis)
      - "fft"    — FFT vibration spectrum with peak annotations
      - "filter" — Filter Bode plot (magnitude + phase)

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        plot_type: Chart type — "pid" (default), "fft", or "filter".
        platform: Platform override — "auto", "ardupilot", "betaflight", or "px4".
        axis: Axis for PID plot — "all" (default), "roll", "pitch", or "yaw".
        theme: Color theme — "light" (default) or "dark".
    """
    if plot_type not in ("pid", "fft", "filter"):
        return _err(
            f"Invalid plot_type: {plot_type!r}", error_code="E4000", hint="Use pid, fft, or filter"
        )
    if axis not in ("all", "roll", "pitch", "yaw"):
        return _err(
            f"Invalid axis: {axis!r}", error_code="E4001", hint="Use all, roll, pitch, or yaw"
        )
    if theme not in ("light", "dark"):
        return _err(f"Invalid theme: {theme!r}", error_code="E4000", hint="Use light or dark")

    from smarttune.services.plot import generate_plot

    return _call_service(
        generate_plot,
        log_path,
        platform=platform,
        plot_type=plot_type,
        axis=axis,
        theme=theme,
    )


# ── 11. Parameter groups ─────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_list_param_groups(platform: str = "ardupilot") -> str:
    """List a platform's firmware parameter GROUPS — the cheap way to explore a parameter table.

    A full table is thousands of parameters; this returns ~80-200 groups with a
    count, category and sample members each. Pick a group, then call
    smarttune_list_params(group=...) to see its members.

    Args:
        platform: "ardupilot", "betaflight", or "px4".
    """
    from smarttune.platform.params import ParamTable

    platform = _resolve_platform_key(platform)
    available = ParamTable.available_platforms()
    if platform not in available:
        return _err(
            f"Unknown platform: {platform!r}", error_code="E4010", hint=f"Available: {available}"
        )

    tbl = ParamTable.from_knowledge(platform)
    return _ok(
        {
            "platform": tbl.platform,
            "source": tbl.meta.get("source", {}),
            "parameter_count": len(tbl),
            "categories": tbl.categories(),
            "groups": tbl.groups(),
        }
    )


# ── 12. List Parameters ──────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_list_params(
    platform: str = "ardupilot",
    group: str = "",
    category: str = "all",
    limit: int = 100,
    offset: int = 0,
    verbose: bool = False,
) -> str:
    """List parameters for a platform, filtered by group or category.

    Returns COMPACT rows (name, type, range, unit, one-line summary) — full
    descriptions and enum member tables come from smarttune_get_param. Listing a
    whole table without a filter is refused: ArduPilot alone is ~2,800
    parameters, and dumping them all costs hundreds of KB per call.

    Args:
        platform: "ardupilot", "betaflight", or "px4".
        group: Firmware parameter group, e.g. "ATC_" / "GYRO_CONFIG" / "Multicopter Rate Control".
        category: Topic filter, e.g. "pid", "filter", "battery". "all" needs a group.
        limit: Max rows to return (1-500, default 100).
        offset: Row offset for paging.
        verbose: Include full descriptions and enum members (use sparingly).
    """
    from smarttune.platform.params import ParamTable, to_full_dict, to_slim_dict

    platform = _resolve_platform_key(platform)
    available = ParamTable.available_platforms()
    if platform not in available:
        return _err(
            f"Unknown platform: {platform!r}", error_code="E4010", hint=f"Available: {available}"
        )

    limit = max(1, min(500, limit))
    offset = max(0, offset)
    tbl = ParamTable.from_knowledge(platform)

    if group:
        rows = tbl.list_by_group(group)
        if not rows:
            return _err(
                f"No group matching {group!r} in {tbl.platform}",
                error_code="E4000",
                hint="Call smarttune_list_param_groups first",
            )
        scope = {"group": group}
    elif category != "all":
        rows = tbl.list_by_category(category)
        if not rows:
            return _err(
                f"No parameters in category {category!r}",
                error_code="E4000",
                hint=f"Available categories: {tbl.categories()}",
            )
        scope = {"category": category}
    else:
        return _err(
            f"Refusing to list all {len(tbl)} parameters at once",
            error_code="E4000",
            hint="Pass group= or category=, or call smarttune_list_param_groups to explore",
            group_count=len(tbl.groups()),
            categories=tbl.categories(),
        )

    page = rows[offset : offset + limit]
    shape = to_full_dict if verbose else to_slim_dict
    return _ok(
        {
            "platform": tbl.platform,
            **scope,
            "count": len(rows),
            "offset": offset,
            "returned": len(page),
            "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
            "params": [shape(p) for p in page],
        }
    )


# ── 13. Get one parameter ────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_get_param(param_name: str, platform: str = "all") -> str:
    """Get the FULL definition of one parameter — description, range, default, enum members.

    This is the tool to call before explaining or recommending a parameter: it
    carries the upstream description and, for enum/bitmask parameters, what each
    value actually means (e.g. BATT_MONITOR 4 = "Analog Voltage and Current").

    Args:
        param_name: Exact firmware parameter name, e.g. "BATT_MONITOR", "MC_ROLLRATE_P", "gyro_lpf1_static_hz".
        platform: "ardupilot" | "betaflight" | "px4" | "all" (default: search every table).
    """
    from smarttune.platform.params import ParamTable, to_full_dict

    available = ParamTable.available_platforms()
    if platform == "all":
        targets = available
    else:
        key = _resolve_platform_key(platform)
        if key not in available:
            return _err(
                f"Unknown platform: {platform!r}",
                error_code="E4010",
                hint=f"Available: {available}",
            )
        targets = [key]

    matches = []
    for plat in targets:
        tbl = ParamTable.from_knowledge(plat)
        pd = tbl.query(param_name)
        if pd:
            matches.append({"platform": tbl.platform, **to_full_dict(pd)})

    if not matches:
        return _err(
            f"Parameter {param_name!r} not found",
            error_code="E4002",
            hint="Use smarttune_search_params for partial matches",
        )
    return _ok({"param_name": param_name, "matches": matches})


# ── 14. Search Parameters ────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_search_params(
    keyword: str,
    platform: str = "all",
    limit: int = 40,
) -> str:
    """Search parameters by keyword, ranked by match quality.

    Matches names, groups, categories, display names, descriptions and enum
    labels — so "analog voltage" finds BATT_MONITOR. Exact and prefix name
    matches rank first. Returns compact rows; follow up with smarttune_get_param.

    Args:
        keyword: Search text, e.g. "notch", "rate p gain", "analog voltage".
        platform: "ardupilot" | "betaflight" | "px4" | "all" (default).
        limit: Max hits per platform (1-200, default 40).
    """
    from smarttune.platform.params import ParamTable, to_slim_dict

    if not keyword.strip():
        return _err("keyword is empty", error_code="E4000", hint="Pass a search term")

    limit = max(1, min(200, limit))
    available = ParamTable.available_platforms()
    if platform == "all":
        targets = available
    else:
        key = _resolve_platform_key(platform)
        if key not in available:
            return _err(
                f"Unknown platform: {platform!r}",
                error_code="E4010",
                hint=f"Available: {available}",
            )
        targets = [key]

    results = {}
    total = 0
    for plat in targets:
        tbl = ParamTable.from_knowledge(plat)
        hits = tbl.search(keyword)
        total += len(hits)
        if hits:
            results[tbl.platform] = {
                "count": len(hits),
                "returned": min(len(hits), limit),
                "params": [to_slim_dict(p) for p in hits[:limit]],
            }

    return _ok({"keyword": keyword, "total": total, "platforms": results})


# ── 15. Validate Parameter ───────────────────────────────────


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def smarttune_validate_param(
    param_name: str,
    param_value: float,
    platform: str,
) -> str:
    """Validate that a parameter exists AND the proposed value is legal. Call before recommending anything.

    Checks, in order:
      1. the name exists in the platform's parameter table
      2. for enum/bitmask parameters, the value is a defined member / bit combination
      3. otherwise, the value sits inside the published [min, max] range

    Returns ok/valid plus a status field:
      ok | not_found | out_of_range | not_a_member | not_an_integer | unverifiable

    "unverifiable" means the table has no members and no range for a discrete
    parameter — the value is NOT accepted; say so instead of guessing.

    Args:
        param_name: Firmware parameter name, e.g. "ATC_RAT_RLL_P", "p_roll", "MC_ROLLRATE_P".
        param_value: The proposed value.
        platform: "ardupilot", "betaflight", or "px4".
    """
    from smarttune.platform.params import ParamTable, to_full_dict

    platform = _resolve_platform_key(platform)
    available = ParamTable.available_platforms()
    if platform not in available:
        return _err(
            f"Unknown platform: {platform!r}",
            error_code="E4010",
            hint=f"Available: {available}",
            valid=False,
            status="not_found",
        )

    tbl = ParamTable.from_knowledge(platform)
    verdict = tbl.validate_detail(param_name, param_value)
    pd = tbl.query(param_name)

    body = {
        "param_name": param_name,
        "param_value": param_value,
        "platform": tbl.platform,
        "valid": verdict["valid"],
        "status": verdict["status"],
        "message": verdict["message"],
    }
    if verdict.get("options"):
        body["options"] = verdict["options"]
    if verdict.get("hint"):
        body["hint"] = verdict["hint"]
    if pd is not None:
        body["parameter"] = to_full_dict(pd)

    # A rejected value is a legitimate answer, not a tool failure — ok stays true
    # so clients do not treat it as retryable transport trouble.
    return _ok(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the SmartTune MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
