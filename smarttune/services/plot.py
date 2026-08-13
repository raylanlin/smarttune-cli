"""
smarttune/services/plot.py

Generate base64-encoded PNG plots from analysis results.

Used by the MCP server to return images that agents can display inline.
All functions accept the same structured dicts returned by analysis.py,
plus optional raw data (fft_step, spectrum, bode) when available.

Dependencies: matplotlib (optional — graceful degradation if absent).
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _check_matplotlib():
    """Import matplotlib with Agg backend; return (plt, np) or raise ImportError."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    return plt, np


def _fig_to_base64(fig) -> str:
    """Convert a matplotlib Figure to a base64 PNG data-URL string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    data = base64.b64encode(buf.read()).decode("ascii")
    return f"data:image/png;base64,{data}"


# ---------------------------------------------------------------------------
# PID step response plot
# ---------------------------------------------------------------------------


def generate_pid_plot(
    pid_result,  # PIDAnalysisResult dataclass
    *,
    theme: str = "light",
) -> Dict[str, Any]:
    """Generate PID step response plot from a PIDAnalysisResult.

    Returns
    -------
    Dict with keys:
      - ``image_base64``: str — data:image/png;base64,... (ready for <img src>)
      - ``axes_plotted``: List[str] — which axes had data
      - ``format``: "png"
    """
    plt, np = _check_matplotlib()

    if theme == "dark":
        plt.style.use("dark_background")
    else:
        plt.style.use("default")

    # Extract per-axis fft_step data
    axes_data: Dict[str, Any] = {}
    if hasattr(pid_result, "axes"):
        # PIDAnalysisResult dataclass
        for axis_name, ax_result in pid_result.axes.items():
            axes_data[axis_name] = {
                "fft_step": ax_result.fft_step,
                "assessment": (
                    ax_result.assessment.value
                    if hasattr(ax_result.assessment, "value")
                    else str(ax_result.assessment)
                ),
                "step_count": ax_result.step_count,
                "metrics": (
                    ax_result.metrics.to_dict() if hasattr(ax_result.metrics, "to_dict") else {}
                ),
            }
    elif isinstance(pid_result, dict):
        # Serialized dict format
        for ax in ["roll", "pitch", "yaw"]:
            if ax in pid_result.get("axes", {}):
                axes_data[ax] = pid_result["axes"][ax]

    if not axes_data:
        return {"image_base64": "", "axes_plotted": [], "format": "png", "error": "No axis data"}

    n = len(axes_data)
    fig, axes_plots = plt.subplots(n, 1, figsize=(10, 4 * n), squeeze=False)

    axes_plotted = []
    for i, (axis_name, axis_data) in enumerate(axes_data.items()):
        fft_step = axis_data.get("fft_step", {})
        assessment = axis_data.get("assessment", "?")
        ax = axes_plots[i, 0]

        time_s = fft_step.get("time_s", [])
        step_resp = fft_step.get("step_response", [])

        if not time_s or not step_resp:
            ax.text(
                0.5,
                0.5,
                "No FFT step response data",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=11,
                color="#888888",
            )
            ax.set_title(f"{axis_name.capitalize()} Step Response ({assessment})")
            continue

        axes_plotted.append(axis_name)
        t = np.array(time_s) * 1000  # s → ms
        resp = np.array(step_resp)

        if theme == "dark":
            ax.plot(t, resp, color="#e63946", linewidth=1.5, alpha=0.9, label="Response")
            ax.axhline(0, color="#555555", linestyle="-", linewidth=0.5, alpha=0.5)
            ax.axhline(1, color="#ffffff", linestyle="--", linewidth=1.0, alpha=0.8, label="Target")
            ax.axhline(0.1, color="#444444", linestyle=":", linewidth=0.8, alpha=0.5)
            ax.axhline(0.9, color="#444444", linestyle=":", linewidth=0.8, alpha=0.5)
        else:
            ax.plot(t, resp, "b-", linewidth=1.5, alpha=0.9, label="Response")
            ax.axhline(0, color="#cccccc", linestyle="-", linewidth=0.5, alpha=0.5)
            ax.axhline(1, color="#999999", linestyle="--", linewidth=1.0, alpha=0.8, label="Target")
            ax.axhline(0.1, color="#dddddd", linestyle=":", linewidth=0.8, alpha=0.5)
            ax.axhline(0.9, color="#dddddd", linestyle=":", linewidth=0.8, alpha=0.5)
            for spine in ax.spines.values():
                spine.set_color("#e0e0e0")

        # Metrics annotation
        metrics = axis_data.get("metrics", {})
        info_parts = []
        if metrics.get("rise_time_ms", -1) >= 0:
            info_parts.append(f"Rise: {metrics['rise_time_ms']:.0f}ms")
        if metrics.get("overshoot_percent", -1) >= 0:
            info_parts.append(f"OS: {metrics['overshoot_percent']:.1f}%")
        if metrics.get("settling_time_ms", -1) >= 0:
            info_parts.append(f"Settle: {metrics['settling_time_ms']:.0f}ms")
        if info_parts:
            info_text = "  |  ".join(info_parts)
            ax.text(
                0.02,
                0.95,
                info_text,
                transform=ax.transAxes,
                fontsize=8,
                verticalalignment="top",
                bbox=dict(
                    boxstyle="round,pad=0.3",
                    facecolor="white" if theme == "light" else "#333333",
                    alpha=0.8,
                    edgecolor="#cccccc",
                ),
            )

        max_time_ms = max(t) if len(t) > 0 else 500
        ax.set_xlim(0, max(500, min(2000, max_time_ms * 1.1)))
        ax.set_ylim(0, 2)
        ax.set_xlabel("Time (ms)", fontsize=9)
        ax.set_ylabel("Response", fontsize=9)

        step_count = axis_data.get("step_count", 0)
        valid_win = fft_step.get("info", {}).get("valid_windows", step_count)
        title = f"{axis_name.capitalize()} Step Response ({assessment}) | {valid_win} windows"
        ax.set_title(title, fontsize=10)
        ax.legend(fontsize=8, loc="lower right")
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    b64 = _fig_to_base64(fig)
    plt.close(fig)

    return {"image_base64": b64, "axes_plotted": axes_plotted, "format": "png"}


# ---------------------------------------------------------------------------
# FFT spectrum plot
# ---------------------------------------------------------------------------


def generate_fft_plot(
    fft_result: Dict[str, Any],
    *,
    spectrum_data: Optional[Dict[str, Any]] = None,
    theme: str = "light",
) -> Dict[str, Any]:
    """Generate FFT vibration spectrum plot.

    Parameters
    ----------
    fft_result : Dict
        Serialized FFT result (from serialize_fft_result or analyze_fft).
    spectrum_data : Dict, optional
        Full spectrum from FFTAnalyzer.get_spectrum_data() — if provided,
        draws the continuous spectrum curve instead of bar-chart peaks.
    theme : str
        "light" or "dark".

    Returns
    -------
    Dict with image_base64, format.
    """
    plt, np = _check_matplotlib()

    if theme == "dark":
        plt.style.use("dark_background")
    else:
        plt.style.use("default")

    fig, ax = plt.subplots(figsize=(10, 5))

    # Full spectrum curve
    if spectrum_data and spectrum_data.get("freqs"):
        freqs = np.array(spectrum_data["freqs"])
        mags = np.array(spectrum_data["magnitudes"])
        spectrum_color = "#ffd60a" if theme == "dark" else "steelblue"
        ax.plot(freqs, mags, color=spectrum_color, linewidth=1.0, alpha=0.8, label="Spectrum")

        if mags.size > 0:
            sorted_mag = np.sort(mags)
            noise_floor = float(np.mean(sorted_mag[: max(1, mags.size // 5)]))
            nf_color = "#888888" if theme == "dark" else "gray"
            ax.axhline(
                noise_floor,
                color=nf_color,
                linestyle="--",
                linewidth=0.8,
                alpha=0.5,
                label=f"Noise floor ({noise_floor:.0f}dB)",
            )
    else:
        # Fallback: bar chart from peaks
        peaks = fft_result.get("peaks", [])
        if peaks:
            freqs_bar = [p.get("frequency_hz", 0) for p in peaks]
            mags_bar = [p.get("amplitude", 0) for p in peaks]
            bar_color = "#ff6b35" if theme == "dark" else "steelblue"
            ax.bar(freqs_bar, mags_bar, color=bar_color, alpha=0.8, width=10)

    # Annotate peaks
    peaks = fft_result.get("peaks", [])
    for peak in peaks:
        f = peak.get("frequency_hz", 0)
        m = peak.get("amplitude", 0)
        src = peak.get("source_guess", "")
        label = f"{f:.0f}Hz"
        if src:
            label += f" ({src})"

        ax.annotate(
            label,
            (f, m),
            textcoords="offset points",
            xytext=(5, 8),
            ha="left",
            fontsize=8,
            color="darkred" if theme == "light" else "#ff6b6b",
        )
        marker_color = "#ff0000" if theme == "dark" else "red"
        ax.plot(f, m, "o", color=marker_color, markersize=6, alpha=0.7)

    # Title
    vib = fft_result.get("vibration_level", "?")
    ax.set_title(f"FFT Spectrum — Vibration: {vib}", fontsize=11)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Magnitude (dBFS)")
    ax.legend(fontsize=8, loc="best")
    ax.grid(True, alpha=0.3)

    if theme != "dark":
        for spine in ax.spines.values():
            spine.set_color("#e0e0e0")

    max_freq = 500
    if peaks:
        max_freq = max(max_freq, max(p.get("frequency_hz", 0) for p in peaks) * 1.2)
    ax.set_xlim(0, max_freq)

    plt.tight_layout()
    b64 = _fig_to_base64(fig)
    plt.close(fig)

    return {"image_base64": b64, "format": "png"}


# ---------------------------------------------------------------------------
# Filter Bode plot
# ---------------------------------------------------------------------------


def generate_filter_bode_plot(
    filter_result: Dict[str, Any],
    *,
    theme: str = "light",
) -> Dict[str, Any]:
    """Generate filter Bode plot (magnitude + phase) from filter analysis result.

    The filter_result must contain ``bode_data`` with keys:
    ``freqs``, ``magnitude_db``, ``phase_deg``.

    Returns
    -------
    Dict with image_base64, format.
    """
    plt, np = _check_matplotlib()

    if theme == "dark":
        plt.style.use("dark_background")
    else:
        plt.style.use("default")

    bode = filter_result.get("bode_data", {})
    freqs = bode.get("freqs", [])
    mag_db = bode.get("magnitude_db", [])
    phase_deg = bode.get("phase_deg", [])

    if not freqs or not mag_db:
        return {"image_base64": "", "format": "png", "error": "No Bode data available"}

    freqs = np.array(freqs)
    mag_db = np.array(mag_db)
    phase_deg = np.array(phase_deg) if phase_deg else np.zeros_like(freqs)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)

    # Magnitude
    mag_color = "#e63946" if theme == "dark" else "blue"
    ax1.plot(freqs, mag_db, color=mag_color, linewidth=1.2)
    ax1.set_ylabel("Magnitude (dB)")
    config_summary = filter_result.get("config_summary", "Filter Bode Plot")
    ax1.set_title(config_summary, fontsize=11)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(-60, 5)
    ax1.axhline(-3, color="red", linestyle="--", alpha=0.5, label="-3dB")
    ax1.legend(fontsize=8)

    # Phase
    phase_color = "#22c55e" if theme == "dark" else "green"
    ax2.plot(freqs, phase_deg, color=phase_color, linewidth=1.2)
    ax2.set_xlabel("Frequency (Hz)")
    ax2.set_ylabel("Phase (deg)")
    ax2.grid(True, alpha=0.3)
    ax2.axhspan(-45, 45, alpha=0.1, color="green", label="±45°")

    if theme != "dark":
        for a in (ax1, ax2):
            for spine in a.spines.values():
                spine.set_color("#e0e0e0")

    plt.tight_layout()
    b64 = _fig_to_base64(fig)
    plt.close(fig)

    return {"image_base64": b64, "format": "png"}


# ---------------------------------------------------------------------------
# Unified plot dispatcher
# ---------------------------------------------------------------------------


def generate_plot(
    log_path: Path,
    platform: str = "auto",
    plot_type: str = "pid",
    axis: str = "all",
    theme: str = "light",
) -> Dict[str, Any]:
    """High-level: parse log → run analysis → generate plot → return base64.

    Parameters
    ----------
    log_path : Path
        Validated log file path.
    platform : str
        Platform override.
    plot_type : str
        One of "pid", "fft", "filter".
    axis : str
        Axis for PID ("all", "roll", "pitch", "yaw").
    theme : str
        "light" or "dark".

    Returns
    -------
    Dict with ``image_base64``, ``plot_type``, ``format``, and metadata.
    """
    from smarttune.services.analysis import load_flight_data

    _lp = Path(log_path) if isinstance(log_path, str) else log_path
    adapter, fd = load_flight_data(_lp, platform)

    if plot_type == "pid":
        if "pid" not in adapter.capabilities():
            raise SmartTuneError(
                message=f"PID analysis not supported on {adapter.display_name}",
                code="E6010",
            )
        from smarttune.analyzers.pid_reviewer import PIDReviewer
        from smarttune.knowledge import KnowledgeBase

        kb = KnowledgeBase(platform=adapter.name)
        reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))
        # Get raw dataclass for plot (not serialized dict)
        pid_result = reviewer.analyze(fd, axis=axis if axis != "all" else None)
        plot_data = generate_pid_plot(pid_result, theme=theme)
        plot_data["plot_type"] = "pid"
        plot_data["platform"] = adapter.display_name
        plot_data["log_file"] = _lp.name
        return plot_data

    elif plot_type == "fft":
        if "fft" not in adapter.capabilities():
            raise SmartTuneError(
                message=f"FFT analysis not supported on {adapter.display_name}",
                code="E6020",
            )
        from smarttune.analyzers.fft_analyzer import FFTAnalyzer
        from smarttune.services.serialize import serialize_fft_result
        from smarttune.knowledge import KnowledgeBase

        kb = KnowledgeBase(platform=adapter.name)
        analyzer = FFTAnalyzer(knowledge=kb.get("filter_rules", {}))
        result = analyzer.analyze(fd)
        serialized = serialize_fft_result(result, adapter)
        # Get full spectrum for continuous curve
        spectrum_data = analyzer.get_spectrum_data()
        plot_data = generate_fft_plot(serialized, spectrum_data=spectrum_data, theme=theme)
        plot_data["plot_type"] = "fft"
        plot_data["platform"] = adapter.display_name
        plot_data["log_file"] = _lp.name
        return plot_data

    elif plot_type == "filter":
        if "filter" not in adapter.capabilities():
            raise SmartTuneError(
                message=f"Filter analysis not supported on {adapter.display_name}",
                code="E6030",
            )
        # Run filter analysis with bode_data included
        from smarttune.services.analysis import analyze_filter as _analyze_filter

        result = _analyze_filter(
            log_path=_lp,
            platform=platform,
            auto_derive=True,
            _include_bode_data=True,
        )
        plot_data = generate_filter_bode_plot(result, theme=theme)
        plot_data["plot_type"] = "filter"
        plot_data["platform"] = adapter.display_name
        plot_data["log_file"] = _lp.name
        return plot_data

    else:
        return {"error": f"Unknown plot_type: {plot_type!r}. Must be pid, fft, or filter."}


# Import here to avoid circular import at module level
from smarttune.errors import SmartTuneError
from smarttune.knowledge import KnowledgeBase
