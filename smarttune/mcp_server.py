"""
smarttune/mcp_server.py

Read-only MCP (Model Context Protocol) server for SmartTune.

Exposes flight log analysis tools for LLM agents (e.g. OpenClaw customer
service) without shell execution, arbitrary file writes, or parameter mutation.

Security boundaries:
  - No subprocess / os.system / shell commands
  - No arbitrary output paths
  - Path validation: allowed roots, extensions, file size, symlink resolution
  - All tools are read-only and idempotent
"""

from __future__ import annotations

import json
import logging
import os
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
    return [p.expanduser().resolve() for p in _DEFAULT_ALLOWED_ROOTS if _safe_resolve(p) is not None]


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
        raise PathValidationError(
            f"File too large: {size_mb:.1f} MB (limit: {max_file_mb:.0f} MB)"
        )

    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    """Check if path is relative to root (compatible with Python 3.9+)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Markdown report renderer
# ---------------------------------------------------------------------------

def _render_markdown(result: Dict[str, Any]) -> str:
    """Render analysis result dict as a compact Markdown report."""
    lines: List[str] = []
    lines.append(f"# SmartTune Analysis Report")
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
            lines.append(f"### {axis_name.capitalize()} Axis — {axis_data.get('assessment', 'N/A')}")
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
                lines.append(f"| {p.get('frequency_hz', '')} | {p.get('amplitude', '')} | {p.get('source_guess', '')} |")
            lines.append("")
        recs = fft.get("recommendations", [])
        if recs:
            for r in recs:
                lines.append(f"- **{r.get('param', '')}**: {r.get('current', '')} → {r.get('suggested', '')} ({r.get('reason', '')})")
            lines.append("")

    # MagFit
    if "magfit" in modules:
        mag = modules["magfit"]
        lines.append(f"## Magnetometer Calibration — {mag.get('assessment', 'N/A')}")
        lines.append("")
        lines.append(f"- Fitness: {mag.get('fitness_mgauss', '')} mGauss")
        offsets = mag.get("offsets", {})
        if offsets:
            lines.append(f"- Offsets: X={offsets.get('x', '?')}, Y={offsets.get('y', '?')}, Z={offsets.get('z', '?')}")
        lines.append("")

    # Hardware
    if "hardware" in modules:
        hw = modules["hardware"]
        lines.append("## Hardware Configuration")
        lines.append("")
        version_info = hw.get("version_info", {})
        sys_info = hw.get("sys_info", {})
        if version_info.get("firmware"):
            lines.append(f"- Firmware: {version_info['firmware']}")
        if sys_info.get("board_name"):
            lines.append(f"- Board: {sys_info['board_name']}")
        if sys_info.get("sched_loop_rate"):
            lines.append(f"- Loop rate: {sys_info['sched_loop_rate']} Hz")
        issues = hw.get("integrity_issues", [])
        if issues:
            lines.append("- Issues:")
            for issue in issues:
                lines.append(f"  - {issue}")
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
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "smarttune_mcp",
    instructions=(
        "SmartTune MCP — Read-only flight log analysis tools. "
        "Supports ArduPilot, Betaflight, and PX4 logs. "
        "All tools are safe, idempotent, and never write parameters."
    ),
)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
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
        # Mark which extensions the MCP server actually accepts
        if isinstance(p.get("extensions"), str):
            cli_exts = [e.strip() for e in p["extensions"].split(",") if e.strip()]
        else:
            cli_exts = p.get("extensions", [])
        p["mcp_accepted_extensions"] = [
            e for e in cli_exts if e in _ALLOWED_EXTENSIONS
        ]
    result = {
        "platforms": platforms,
        "mcp_allowed_extensions": sorted(_ALLOWED_EXTENSIONS),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def smarttune_log_quality(
    log_path: str,
    platform: str = "auto",
) -> str:
    """Parse a flight log and assess its quality for analysis.

    Returns JSON with platform info, data availability flags, duration,
    sample rate, validation issues, and a quality score/rating.

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        platform: Platform override — "auto" (default), "ardupilot", "betaflight", or "px4".
    """
    try:
        resolved = validate_log_path(log_path)
    except PathValidationError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2)

    try:
        from smarttune.services.analysis import get_log_quality
        result = get_log_quality(resolved, platform=platform)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except SmartTuneError as exc:
        return json.dumps({
            "error": exc.message,
            "code": exc.code,
            "hint": exc.hint,
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.exception("Unexpected error in smarttune_log_quality")
        return json.dumps({"error": f"Unexpected error: {exc}"}, ensure_ascii=False, indent=2)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def smarttune_analyze_log(
    log_path: str,
    platform: str = "auto",
    axis: str = "all",
    include_modules: Optional[List[str]] = None,
    response_format: str = "json",
    max_recommendations: int = 20,
) -> str:
    """Run comprehensive flight log analysis and return structured recommendations.

    Analyzes PID tuning, vibration (FFT), magnetometer calibration, and hardware
    configuration. Returns compact results suitable for explaining to users.

    Args:
        log_path: Path to a flight log file (.bin, .log, .bbl, .bfl, .ulg).
        platform: Platform override — "auto" (default), "ardupilot", "betaflight", or "px4".
        axis: Axis to analyze — "all" (default), "roll", "pitch", or "yaw".
        include_modules: Subset of ["pid", "fft", "magfit", "hardware", "filter"]. None = all available.
        response_format: Output format — "json" (default) or "markdown".
        max_recommendations: Maximum number of parameter recommendations (1–100, default 20).
    """
    # Clamp max_recommendations
    max_recommendations = max(1, min(100, max_recommendations))

    # Validate axis
    if axis not in ("all", "roll", "pitch", "yaw"):
        return json.dumps({"error": f"Invalid axis: {axis!r}. Must be all, roll, pitch, or yaw."})

    # Validate response_format
    if response_format not in ("json", "markdown"):
        return json.dumps({"error": f"Invalid response_format: {response_format!r}. Must be json or markdown."})

    # Validate include_modules
    valid_modules = {"pid", "fft", "magfit", "hardware", "filter"}
    if include_modules is not None:
        invalid = set(include_modules) - valid_modules
        if invalid:
            return json.dumps({"error": f"Invalid modules: {invalid}. Valid: {sorted(valid_modules)}"})

    try:
        resolved = validate_log_path(log_path)
    except PathValidationError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2)

    try:
        from smarttune.services.analysis import analyze_log
        result = analyze_log(
            log_path=resolved,
            platform=platform,
            axis=axis,
            include_modules=include_modules,
            max_recommendations=max_recommendations,
        )

        if response_format == "markdown":
            return _render_markdown(result)

        return json.dumps(result, ensure_ascii=False, indent=2)

    except SmartTuneError as exc:
        return json.dumps({
            "error": exc.message,
            "code": exc.code,
            "hint": exc.hint,
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.exception("Unexpected error in smarttune_analyze_log")
        return json.dumps({"error": f"Unexpected error: {exc}"}, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the SmartTune MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
