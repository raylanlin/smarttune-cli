"""
smarttune/output/formatter.py

统一输出格式化器 — 消费平台无关的 AnalysisResult，
通过 PlatformAdapter.map_param_to_platform() 翻译参数名。

支持输出模式：terminal (rich)、markdown、HTML。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from smarttune.models.analysis_result import (
    Assessment,
    AxisPIDResult,
    FFTAnalysisResult,
    FilterAnalysisResult,
    FullAnalysisResult,
    HardwareReport,
    MagFitResult,
    ParamRecommendation,
    PIDAnalysisResult,
    SysIDResult,
)
from smarttune.platform.base import PlatformAdapter

logger = logging.getLogger(__name__)

_ASSESSMENT_COLORS = {
    Assessment.EXCELLENT: "bold green",
    Assessment.GOOD: "green",
    Assessment.MARGINAL: "yellow",
    Assessment.POOR: "red",
    Assessment.UNUSABLE: "bold red",
}


class OutputFormatter:
    """统一输出格式化器。

    Parameters
    ----------
    adapter : PlatformAdapter
        用于翻译参数名
    output_file : Path, optional
        输出文件路径，None 则输出到终端
    """

    def __init__(
        self,
        adapter: PlatformAdapter,
        output_file: Optional[Path] = None,
        console: Optional[Console] = None,
    ) -> None:
        self._adapter = adapter
        self._output_file = output_file
        self._console = console or Console(stderr=True)

    # ------------------------------------------------------------------
    # 参数翻译
    # ------------------------------------------------------------------

    def _platform_param(self, generic_name: str) -> str:
        """通用参数名 → 平台参数名。"""
        return self._adapter.map_param_to_platform(generic_name)

    # ------------------------------------------------------------------
    # PID 输出
    # ------------------------------------------------------------------

    def format_pid(self, result: PIDAnalysisResult) -> None:
        """渲染 PID 分析结果到终端。"""
        self._console.print(Panel("PID Step Response Analysis", style="bold cyan"))

        for axis, ax_result in result.axes.items():
            self._render_pid_axis(ax_result)

        if result.axes:
            color = _ASSESSMENT_COLORS.get(result.overall_assessment, "white")
            self._console.print(
                f"\n  Overall: [{color}]{result.overall_assessment.value}[/{color}]"
            )

    def _render_pid_axis(self, ax: AxisPIDResult) -> None:
        color = _ASSESSMENT_COLORS.get(ax.assessment, "white")
        self._console.print(
            f"\n  [{color}]{ax.axis.upper()}: {ax.assessment.value}[/{color}]"
            f"  (steps: {ax.step_count})"
        )

        m = ax.metrics
        if m.rise_time_ms >= 0:
            table = Table(show_header=True, box=None, padding=(0, 2))
            table.add_column("Metric", style="dim")
            table.add_column("Value", justify="right")
            table.add_row("Rise time", f"{m.rise_time_ms:.0f} ms")
            table.add_row("Overshoot", f"{m.overshoot_percent:.1f}%")
            table.add_row("Settling time", f"{m.settling_time_ms:.0f} ms")
            table.add_row("Oscillations", f"{m.oscillation_count}")
            table.add_row("SS error", f"{m.steady_state_error_percent:.1f}%")
            self._console.print(table)

        self._render_recommendations(ax.recommendations)

    # ------------------------------------------------------------------
    # FFT 输出
    # ------------------------------------------------------------------

    def format_fft(self, result: Dict[str, Any]) -> None:
        """渲染 FFT 分析结果。"""
        self._console.print(Panel("FFT Vibration Analysis", style="bold cyan"))

        vib_level = result.get("vibration_level", "UNKNOWN")
        vib_mss = result.get("vibration_value_mss", 0)
        color = {"EXCELLENT": "green", "GOOD": "green", "MODERATE": "yellow",
                 "HIGH": "yellow", "SEVERE": "red", "CRITICAL": "bold red"}.get(vib_level, "white")
        self._console.print(f"  Vibration: [{color}]{vib_level}[/{color}] ({vib_mss:.1f} m/s²)")

        peaks = result.get("peak_frequencies", [])
        if peaks:
            table = Table(show_header=True, box=None, padding=(0, 2))
            table.add_column("Freq (Hz)", justify="right")
            table.add_column("Amplitude (dB)", justify="right")
            table.add_column("Source")
            for p in peaks[:5]:
                table.add_row(
                    f"{p.get('frequency_hz', 0):.1f}",
                    f"{p.get('amplitude_dbfs', p.get('amplitude', 0)):.1f}",
                    p.get("source", "unknown"),
                )
            self._console.print(table)

        # Notch recommendations
        recs = result.get("recommendations", {})
        if isinstance(recs, dict):
            for key, val in recs.items():
                native = self._platform_param(f"filter.{key}") if "." not in key else self._platform_param(key)
                self._console.print(f"    → [cyan]{native}[/cyan]: {val}")

        for w in result.get("warnings", []):
            self._console.print(f"  [yellow]⚠ {w}[/yellow]")

    # ------------------------------------------------------------------
    # SysID 输出
    # ------------------------------------------------------------------

    def format_sysid(self, results: Dict[str, Any]) -> None:
        """渲染系统辨识结果。"""
        self._console.print(Panel("System Identification (ARX)", style="bold cyan"))

        for axis, r in results.items():
            self._console.print(f"\n  {axis.upper()}:")
            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column("", style="dim")
            table.add_column("", justify="right")
            table.add_row("Natural freq", f"{r.natural_freq_hz:.1f} Hz")
            table.add_row("Damping ratio", f"{r.damping_ratio:.3f}")
            table.add_row("Fit quality", f"{r.fit_quality_percent:.1f}%")
            table.add_row("Bandwidth", f"{r.suggested_bandwidth_hz:.1f} Hz")
            self._console.print(table)

    # ------------------------------------------------------------------
    # MagFit 输出
    # ------------------------------------------------------------------

    def format_magfit(self, result: Any) -> None:
        """渲染磁力计分析结果。"""
        self._console.print(Panel("Magnetometer Calibration", style="bold cyan"))
        self._console.print(f"  Fitness: {result.fitness_mGauss:.1f} mGauss")
        self._console.print(f"  Assessment: {result.assessment}")

        if hasattr(result, 'ofs') and result.ofs:
            for i, axis in enumerate(["X", "Y", "Z"]):
                native = self._platform_param(f"mag.ofs.{axis.lower()}")
                self._console.print(f"    {native}: {result.ofs[i]:.1f}")

    # ------------------------------------------------------------------
    # Hardware 输出
    # ------------------------------------------------------------------

    def format_hardware(self, report: Dict[str, Any]) -> None:
        """渲染硬件配置报告。"""
        self._console.print(Panel("Hardware Configuration", style="bold cyan"))

        si = report.get("sys_info", {})
        if si:
            self._console.print(f"  Board: {si.get('board_name', 'Unknown')}")
            self._console.print(f"  Loop Rate: {si.get('sched_loop_rate', 400)} Hz")

        imu_configs = report.get("imu_configs", [])
        if imu_configs:
            table = Table(title="IMU Configuration", show_header=True, box=None)
            table.add_column("IMU")
            table.add_column("Gyro")
            table.add_column("Accel")
            for imu in imu_configs:
                gi = imu.get("gyro_info", {})
                ai = imu.get("accel_info", {})
                table.add_row(
                    f"IMU {imu.get('imu_index', '?')}",
                    gi.get("name", "?"),
                    ai.get("name", "?"),
                )
            self._console.print(table)

        pid_params = report.get("pid_params", {})
        if pid_params:
            table = Table(title="Rate PID Parameters", show_header=True, box=None)
            table.add_column("Axis", style="cyan")
            table.add_column("P", justify="right")
            table.add_column("I", justify="right")
            table.add_column("D", justify="right")
            table.add_column("FF", justify="right")
            for axis_key, p in pid_params.items():
                table.add_row(
                    axis_key.upper(),
                    f"{p.get('P', 0):.3f}",
                    f"{p.get('I', 0):.3f}",
                    f"{p.get('D', 0):.4f}",
                    f"{p.get('FF', 0):.3f}",
                )
            self._console.print(table)

    # ------------------------------------------------------------------
    # 通用建议渲染
    # ------------------------------------------------------------------

    def _render_recommendations(self, recs: List[ParamRecommendation]) -> None:
        """渲染参数修改建议列表。"""
        if not recs:
            return

        for rec in recs:
            native = self._platform_param(rec.param.generic_name)
            arrow = "↑" if rec.action == "increase" else "↓"
            change = rec.change_percent
            self._console.print(
                f"    {arrow} [cyan]{native}[/cyan]: "
                f"{rec.current:.4f} → {rec.suggested:.4f} "
                f"({change:+.1f}%)  "
                f"[dim]({rec.reason})[/dim]"
            )

    # ------------------------------------------------------------------
    # 综合输出
    # ------------------------------------------------------------------

    def format_full(self, result: FullAnalysisResult) -> None:
        """渲染完整分析结果。"""
        self._console.print(
            f"\n[bold]Platform:[/bold] {self._adapter.display_name}"
            f"  [bold]Log:[/bold] {result.log_file}"
        )

        if result.pid:
            self.format_pid(result.pid)
        if result.fft:
            self.format_fft(result.fft)
        if result.sysid:
            self.format_sysid({r.axis: r for r in result.sysid})
        if result.magfit:
            self.format_magfit(result.magfit)
        if result.hardware:
            self.format_hardware(vars(result.hardware))

        # 汇总所有建议
        all_recs = result.all_recommendations
        if all_recs:
            self._console.print(
                Panel(f"Total recommendations: {len(all_recs)}", style="bold cyan")
            )
            self._render_recommendations(all_recs)

    # ------------------------------------------------------------------
    # Markdown 输出
    # ------------------------------------------------------------------

    def to_markdown(self, result: FullAnalysisResult) -> str:
        """生成 Markdown 格式报告。"""
        lines = [
            f"# SmartTune Analysis Report",
            f"",
            f"**Platform:** {self._adapter.display_name}",
            f"**Log:** {result.log_file}",
            "",
        ]

        if result.pid:
            lines.append("## PID Analysis")
            lines.append("")
            for axis, ax in result.pid.axes.items():
                lines.append(f"### {axis.upper()}: {ax.assessment.value}")
                m = ax.metrics
                if m.rise_time_ms >= 0:
                    lines.append(f"- Rise time: {m.rise_time_ms:.0f} ms")
                    lines.append(f"- Overshoot: {m.overshoot_percent:.1f}%")
                    lines.append(f"- Settling time: {m.settling_time_ms:.0f} ms")
                    lines.append(f"- Oscillations: {m.oscillation_count}")
                    lines.append(f"- SS error: {m.steady_state_error_percent:.1f}%")
                for rec in ax.recommendations:
                    native = self._platform_param(rec.param.generic_name)
                    lines.append(f"- **{native}**: {rec.current:.4f} → {rec.suggested:.4f} ({rec.reason})")
                lines.append("")

        all_recs = result.all_recommendations
        if all_recs:
            lines.append("## Summary of Recommendations")
            lines.append("")
            lines.append("| Parameter | Current | Suggested | Reason |")
            lines.append("|---|---|---|---|")
            for rec in all_recs:
                native = self._platform_param(rec.param.generic_name)
                lines.append(f"| {native} | {rec.current:.4f} | {rec.suggested:.4f} | {rec.reason} |")

        return "\n".join(lines)
