"""
smarttune/output/formatter.py

统一输出格式化器 — 消费平台无关的 AnalysisResult，
通过 PlatformAdapter.map_param_to_platform() 翻译参数名。

支持输出模式：terminal (rich)、markdown、matplotlib 图表。
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

    def format_pid(self, result: PIDAnalysisResult, visual: bool = False) -> None:
        """渲染 PID 分析结果到终端。"""
        self._console.print(Panel("PID Step Response Analysis", style="bold cyan"))

        for axis, ax_result in result.axes.items():
            self._render_pid_axis(ax_result)

        if result.axes:
            color = _ASSESSMENT_COLORS.get(result.overall_assessment, "white")
            self._console.print(
                f"\n  Overall: [{color}]{result.overall_assessment.value}[/{color}]"
            )

        if visual:
            path = self._generate_pid_plot(result)
            if path:
                self._console.print(f"\n[green]✓[/green] PID plot saved: [cyan]{path}[/cyan]")

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

    def format_fft(self, result: Dict[str, Any], visual: bool = False) -> None:
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
                    f"{p.get('frequency_hz', p.get('freq', 0)):.1f}",
                    f"{p.get('amplitude_dbfs', p.get('amplitude', p.get('magnitude_db', 0))):.1f}",
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

        if visual:
            path = self._generate_fft_plot(result)
            if path:
                self._console.print(f"\n[green]✓[/green] FFT plot saved: [cyan]{path}[/cyan]")

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
            nf = getattr(r, "natural_freq_hz", r.get("natural_freq_hz", 0) if isinstance(r, dict) else 0)
            dr = getattr(r, "damping_ratio", r.get("damping_ratio", 0) if isinstance(r, dict) else 0)
            fq = getattr(r, "fit_quality", r.get("fit_quality_percent", r.get("fit_quality", 0)) if isinstance(r, dict) else 0)
            bw = getattr(r, "bandwidth_hz", r.get("suggested_bandwidth_hz", r.get("bandwidth_hz", 0)) if isinstance(r, dict) else 0)
            table.add_row("Natural freq", f"{nf:.1f} Hz")
            table.add_row("Damping ratio", f"{dr:.3f}")
            table.add_row("Fit quality", f"{fq:.1f}%")
            table.add_row("Bandwidth", f"{bw:.1f} Hz")
            self._console.print(table)

    # ------------------------------------------------------------------
    # MagFit 输出
    # ------------------------------------------------------------------

    def format_magfit(self, result: Any) -> None:
        """渲染磁力计分析结果。"""
        self._console.print(Panel("Magnetometer Calibration", style="bold cyan"))

        # Support both old attr and new dataclass
        fitness = getattr(result, "fitness_mGauss", None) or getattr(result, "fitness_mgauss", 0)
        assessment = getattr(result, "assessment", "UNKNOWN")
        self._console.print(f"  Fitness: {fitness:.1f} mGauss")
        self._console.print(f"  Assessment: {assessment}")

        if hasattr(result, 'ofs') and result.ofs:
            for i, axis in enumerate(["X", "Y", "Z"]):
                native = self._platform_param(f"mag.ofs.{axis.lower()}")
                self._console.print(f"    {native}: {result.ofs[i]:.1f}")
        elif hasattr(result, 'offsets') and result.offsets:
            for axis, val in result.offsets.items():
                native = self._platform_param(f"mag.ofs.{axis}")
                self._console.print(f"    {native}: {val:.1f}")

    # ------------------------------------------------------------------
    # Hardware 输出
    # ------------------------------------------------------------------

    def format_hardware(self, report: Dict[str, Any], visual: bool = False) -> None:
        """渲染硬件配置报告。"""
        self._console.print(Panel("Hardware Configuration", style="bold cyan"))

        # Version info
        vi = report.get("version_info", {})
        if vi:
            self._console.print(f"\n[bold]Firmware:[/bold]")
            for k, v in vi.items():
                if k != "time" and v:
                    self._console.print(f"  {k}: {v}")

        si = report.get("sys_info", {})
        if si:
            board = si.get('board_name', 'Unknown')
            if board and board != 'Unknown':
                board_id = si.get('board_id', 0)
                self._console.print(f"  Board: {board} (ID={board_id})")
            self._console.print(f"  Loop Rate: {si.get('sched_loop_rate', 400)} Hz")
            self._console.print(f"  EKF Type: {si.get('ahrs_ekf_type', 3)}")

        imu_configs = report.get("imu_configs", [])
        if imu_configs:
            table = Table(title="IMU Configuration", show_header=True, box=None)
            table.add_column("IMU", style="cyan")
            table.add_column("Gyro")
            table.add_column("Accel")
            table.add_column("Bus")
            table.add_column("Gyro Cal", justify="center")
            table.add_column("Accel Cal", justify="center")
            for imu in imu_configs:
                gi = imu.get("gyro_info", {})
                ai = imu.get("accel_info", {})
                table.add_row(
                    f"IMU {imu.get('imu_index', '?')}",
                    gi.get("name", "?"),
                    ai.get("name", "?"),
                    gi.get("bus_type", "N/A"),
                    "✓" if imu.get("gyro_calibrated") else "✗",
                    "✓" if imu.get("accel_calibrated") else "✗",
                )
            self._console.print(table)

        # Compass configs
        compass_configs = report.get("compass_configs", [])
        if compass_configs:
            table = Table(title="Compass Configuration", show_header=True, box=None)
            table.add_column("ID", style="cyan")
            table.add_column("Chip")
            table.add_column("Bus")
            table.add_column("External", justify="center")
            table.add_column("Enabled", justify="center")
            for cc in compass_configs:
                info = cc.get("info", {})
                table.add_row(
                    str(cc.get("index", "?")),
                    info.get("name", "Unknown"),
                    info.get("bus_type", "N/A"),
                    "✓" if cc.get("external") else "✗",
                    "✓" if cc.get("use") else "✗",
                )
            self._console.print(table)

        # Filter config
        filter_cfg = report.get("filter_config", {})
        if filter_cfg:
            self._console.print(f"\n[bold]Filter Configuration:[/bold]")
            self._console.print(f"  GYRO_FILTER: {filter_cfg.get('gyro_filter', 0):.1f} Hz")
            self._console.print(f"  ACCEL_FILTER: {filter_cfg.get('accel_filter', 0):.1f} Hz")
            for nf in filter_cfg.get("notch_filters", []):
                if nf.get("enable"):
                    pfx = nf.get("prefix", "Notch")
                    self._console.print(
                        f"  {pfx}: freq={nf.get('freq', 0):.0f}Hz, BW={nf.get('bw', 0):.0f}, "
                        f"ATT={nf.get('att', 0):.0f}dB, Mode={nf.get('mode', 0)}, "
                        f"Harmonics={nf.get('harmonics', 0)}"
                    )
            if not any(nf.get("enable") for nf in filter_cfg.get("notch_filters", [])):
                self._console.print("  Notch: disabled")

        pid_params = report.get("pid_params", {})
        if pid_params:
            table = Table(title="Rate PID Parameters", show_header=True, box=None)
            table.add_column("Axis", style="cyan")
            table.add_column("P", justify="right")
            table.add_column("I", justify="right")
            table.add_column("D", justify="right")
            table.add_column("FF", justify="right")
            table.add_column("FLTT", justify="right")
            table.add_column("FLTD", justify="right")
            for axis_key, p in pid_params.items():
                table.add_row(
                    axis_key.upper(),
                    f"{p.get('P', 0):.3f}",
                    f"{p.get('I', 0):.3f}",
                    f"{p.get('D', 0):.4f}",
                    f"{p.get('FF', 0):.3f}",
                    f"{p.get('FLTT', 0):.0f}",
                    f"{p.get('FLTD', 0):.0f}",
                )
            self._console.print(table)

        # Battery reports
        for bat in report.get("battery_reports", []):
            self._console.print(f"\n[bold]Battery {bat.get('id', 0)}:[/bold]")
            self._console.print(
                f"  Voltage: {bat.get('voltage_min', 0):.1f}~{bat.get('voltage_max', 0):.1f}V "
                f"(avg {bat.get('voltage_mean', 0):.1f}V)"
            )
            self._console.print(
                f"  Current: max {bat.get('current_max', 0):.1f}A, avg {bat.get('current_mean', 0):.1f}A"
            )
            if bat.get("consumed_mah"):
                self._console.print(f"  Consumed: {bat['consumed_mah']:.0f} mAh")

        # Integrity issues
        integrity = report.get("integrity_issues", [])
        if integrity:
            self._console.print(f"\n[bold yellow]Log Integrity Issues:[/bold yellow]")
            for issue in integrity:
                self._console.print(f"  [yellow]⚠[/yellow] {issue}")

        if visual:
            path = self._generate_hardware_report_plot(report)
            if path:
                self._console.print(f"\n[green]✓[/green] Hardware report plot saved: [cyan]{path}[/cyan]")

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

    # ================================================================
    # matplotlib 可视化 (#10, #11)
    # ================================================================

    def _plot_save(self, filename: str) -> str:
        """返回图片保存路径。"""
        output_dir = Path.cwd() / "output"
        output_dir.mkdir(exist_ok=True)
        return str(output_dir / filename)

    def generate_all_plots(
        self,
        pid_results: Any = None,
        fft_results: Any = None,
    ) -> None:
        """生成所有可用的 matplotlib 图表。"""
        if pid_results is not None:
            self._generate_pid_plot(pid_results)
        if fft_results is not None:
            self._generate_fft_plot(fft_results)

    def _generate_pid_plot(self, results: Any) -> Optional[str]:
        """
        绘制 PID 阶跃响应曲线。

        支持 PIDAnalysisResult (dataclass) 和旧版 dict 格式。

        Returns
        -------
        Optional[str]
            保存的图片路径，失败返回 None。
        """
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError:
            return None

        # 统一提取 axes 数据
        axes_data: Dict[str, Any] = {}
        if isinstance(results, PIDAnalysisResult):
            for axis_name, ax_result in results.axes.items():
                axes_data[axis_name] = {
                    "fft_step": ax_result.fft_step,
                    "assessment": ax_result.assessment.value,
                    "step_count": ax_result.step_count,
                    "metrics": ax_result.metrics.to_dict(),
                }
        elif isinstance(results, dict):
            if "axis" in results:
                axes_data[results["axis"]] = results
            else:
                for ax in ["roll", "pitch", "yaw"]:
                    if ax in results:
                        axes_data[ax] = results[ax]

        if not axes_data:
            return None

        n = len(axes_data)
        fig, axes_plots = plt.subplots(n, 1, figsize=(10, 4 * n), squeeze=False)
        fig.patch.set_facecolor("#1a1a2e")
        plt.rcParams.update({
            "text.color": "#e0e0e0",
            "axes.labelcolor": "#e0e0e0",
            "axes.facecolor": "#16213e",
            "axes.edgecolor": "#4a4a6a",
            "xtick.color": "#e0e0e0",
            "ytick.color": "#e0e0e0",
            "grid.color": "#3a3a5a",
        })

        for i, (axis_name, axis_data) in enumerate(axes_data.items()):
            fft_step = axis_data.get("fft_step", {})
            assessment = axis_data.get("assessment", "?")
            ax = axes_plots[i, 0]

            time_s = fft_step.get("time_s", [])
            step_resp = fft_step.get("step_response", [])
            info = fft_step.get("info", {})

            if not time_s or not step_resp:
                ax.text(0.5, 0.5, "No FFT step response data", ha="center", va="center",
                        transform=ax.transAxes)
                ax.set_title(f"{axis_name.capitalize()} Step Response ({assessment})")
                continue

            t = np.array(time_s) * 1000  # s → ms
            resp = np.array(step_resp)

            # Plot step response
            ax.plot(t, resp, color="#4fc3f7", linewidth=1.5, alpha=0.9, label="Response")

            # Reference lines
            ax.axhline(0, color="#888888", linestyle="-", linewidth=0.5, alpha=0.5)
            ax.axhline(1, color="#ff7043", linestyle="--", linewidth=1.0, alpha=0.8, label="Target")
            ax.axhline(0.1, color="#666666", linestyle=":", linewidth=0.8, alpha=0.5)
            ax.axhline(0.9, color="#666666", linestyle=":", linewidth=0.8, alpha=0.5)

            # Axes limits
            max_time_ms = max(t) if len(t) > 0 else 500
            ax.set_xlim(0, max(500, min(2000, max_time_ms * 1.1)))
            ax.set_ylim(0, 2)
            ax.set_xlabel("Time (ms)", fontsize=9)
            ax.set_ylabel("Response", fontsize=9)

            valid_win = info.get("valid_windows", axis_data.get("step_count", 0))
            title = f"{axis_name.capitalize()} Step Response ({assessment}) | {valid_win} windows"
            ax.set_title(title, fontsize=10)
            ax.legend(fontsize=8, loc="lower right")
            ax.grid(True, alpha=0.3)

        plt.tight_layout()
        out_path = self._plot_save("pid_analysis.png")
        plt.savefig(out_path, dpi=150, facecolor=fig.get_facecolor())
        plt.close(fig)
        return out_path

    def _generate_fft_plot(
        self,
        results: Dict[str, Any],
        raw_spectrum: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        绘制 FFT 频谱曲线。

        Returns
        -------
        Optional[str]
            保存的图片路径，失败返回 None。
        """
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError:
            return None

        fig, ax = plt.subplots(figsize=(10, 5))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#16213e")
        plt.rcParams.update({
            "text.color": "#e0e0e0",
            "axes.labelcolor": "#e0e0e0",
            "axes.edgecolor": "#4a4a6a",
            "xtick.color": "#e0e0e0",
            "ytick.color": "#e0e0e0",
            "grid.color": "#3a3a5a",
        })

        # Full spectrum curve
        if raw_spectrum and raw_spectrum.get("freqs"):
            freqs = np.array(raw_spectrum["freqs"])
            mags = np.array(raw_spectrum["magnitudes"])
            ax.plot(freqs, mags, "b-", linewidth=1.0, alpha=0.8, label="Spectrum")

            if mags.size > 0:
                sorted_mag = np.sort(mags)
                noise_floor = float(np.mean(sorted_mag[:max(1, mags.size // 5)]))
                ax.axhline(noise_floor, color="gray", linestyle="--",
                           linewidth=0.8, alpha=0.5, label=f"Noise floor ({noise_floor:.0f}dB)")
        else:
            # Fallback to bar chart (peaks only)
            peaks = results.get("peak_frequencies", [])
            if peaks:
                freqs_bar = [p.get("freq", p.get("frequency_hz", 0)) for p in peaks]
                mags_bar = [p.get("magnitude_db", p.get("amplitude_dbfs", p.get("amplitude", 0))) for p in peaks]
                ax.bar(freqs_bar, mags_bar, color="steelblue", alpha=0.8, width=10)

        # Annotate peaks
        peaks = results.get("peak_frequencies", [])
        for peak in peaks:
            f = peak.get("freq", peak.get("frequency_hz", 0))
            m = peak.get("magnitude_db", peak.get("amplitude_dbfs", peak.get("amplitude", 0)))
            src = peak.get("source", "unknown")
            is_harm = peak.get("is_harmonic", False)

            label = f"{f:.0f}Hz"
            if src != "unknown":
                label += f" ({src})"
            if is_harm:
                label += "*"

            ax.annotate(label, (f, m), textcoords="offset points",
                        xytext=(5, 8), ha="left", fontsize=8,
                        color="darkred" if is_harm else "black")
            ax.plot(f, m, "r*" if is_harm else "ro", markersize=8 if is_harm else 6, alpha=0.7)

        # Notch center frequency line
        recs = results.get("recommendations", {})
        notch_freq = 0
        if isinstance(recs, dict):
            notch_freq = recs.get("INS_HNTCH_FREQ", 0)
        if notch_freq > 0:
            ax.axvline(notch_freq, color="green", linestyle="--",
                       linewidth=1.5, alpha=0.7, label=f"Notch center ({notch_freq:.0f}Hz)")
            notch_bw = recs.get("INS_HNTCH_BW", 0) if isinstance(recs, dict) else 0
            if notch_bw > 0:
                ax.axvspan(notch_freq - notch_bw / 2, notch_freq + notch_bw / 2,
                           color="green", alpha=0.15, label=f"Notch BW ({notch_bw:.0f}Hz)")

        # Title & axes
        vib = results.get("vibration_level", "?")
        vib_val = results.get("vibration_value_mss", 0)
        ax.set_title(f"FFT Spectrum — Vibration: {vib} ({vib_val:.1f} m/s²)")
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("Magnitude (dBFS)")
        ax.legend(fontsize=8, loc="best")
        ax.grid(True, alpha=0.3)

        max_freq = 500
        if peaks:
            max_freq = max(max_freq, max(p.get("freq", p.get("frequency_hz", 0)) for p in peaks) * 1.2)
        if notch_freq > 0:
            max_freq = max(max_freq, notch_freq * 1.2)
        ax.set_xlim(0, max_freq)

        plt.tight_layout()
        out_path = self._plot_save("fft_spectrum.png")
        plt.savefig(out_path, dpi=150, facecolor=fig.get_facecolor())
        plt.close(fig)
        return out_path

    def _generate_hardware_report_plot(self, hw_report: Dict[str, Any]) -> Optional[str]:
        """
        绘制硬件配置摘要图。

        Returns
        -------
        Optional[str]
            保存的图片路径。
        """
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            return None

        fig, axes = plt.subplots(2, 2, figsize=(12, 10))

        # IMU table
        ax1 = axes[0, 0]
        ax1.axis("off")
        imu_configs = hw_report.get("imu_configs", [])
        if imu_configs:
            table_data = []
            for imu in imu_configs:
                gi = imu.get("gyro_info", {})
                ai = imu.get("accel_info", {})
                table_data.append([
                    f"IMU {imu.get('imu_index', '?')}",
                    gi.get("name", str(imu.get("gyro_id", "?"))),
                    ai.get("name", str(imu.get("accel_id", "?"))),
                    "✓" if imu.get("gyro_calibrated") else "✗",
                    "✓" if imu.get("accel_calibrated") else "✗",
                ])
            ax1.table(
                cellText=table_data,
                colLabels=["IMU", "Gyro", "Accel", "Gyro Cal", "Accel Cal"],
                loc="center", cellLoc="center",
            )
            ax1.set_title("IMU Configuration", fontsize=12, fontweight="bold")

        # Filter config
        ax2 = axes[0, 1]
        filter_cfg = hw_report.get("filter_config", {})
        filter_items = [
            f"GYRO_FILTER: {filter_cfg.get('gyro_filter', 0):.1f} Hz",
            f"ACCEL_FILTER: {filter_cfg.get('accel_filter', 0):.1f} Hz",
            f"NOTCH_ENABLE: {'Yes' if filter_cfg.get('notch_enable') else 'No'}",
            f"NOTCH_FREQ: {filter_cfg.get('notch_freq', 0):.1f} Hz",
            f"NOTCH_BW: {filter_cfg.get('notch_bw', 0):.1f} Hz",
        ]
        ax2.axis("off")
        ax2.text(0.1, 0.5, "\n".join(filter_items), fontsize=10, family="monospace",
                 verticalalignment="center", transform=ax2.transAxes)
        ax2.set_title("Filter Configuration", fontsize=12, fontweight="bold")

        # PID params
        ax3 = axes[1, 0]
        pid_params = hw_report.get("pid_params", {})
        if pid_params:
            table_data = []
            for axis_key, params in pid_params.items():
                table_data.append([
                    axis_key.upper(),
                    f"{params.get('P', 0):.3f}",
                    f"{params.get('I', 0):.3f}",
                    f"{params.get('D', 0):.4f}",
                    f"{params.get('FF', 0):.3f}",
                ])
            ax3.axis("off")
            ax3.table(
                cellText=table_data,
                colLabels=["Axis", "P", "I", "D", "FF"],
                loc="center", cellLoc="center",
            )
            ax3.set_title("Rate PID Parameters", fontsize=12, fontweight="bold")
        else:
            ax3.axis("off")

        # System info
        ax4 = axes[1, 1]
        sys_info = hw_report.get("sys_info", {})
        sys_items = [
            f"Board: {sys_info.get('board_name', 'Unknown')}",
            f"Loop Rate: {sys_info.get('sched_loop_rate', 400)} Hz",
            f"Total Params: {hw_report.get('total_params', 0)}",
        ]
        ax4.axis("off")
        ax4.text(0.1, 0.5, "\n".join(sys_items), fontsize=10, family="monospace",
                 verticalalignment="center", transform=ax4.transAxes)
        ax4.set_title("System Information", fontsize=12, fontweight="bold")

        plt.tight_layout()
        out_path = self._plot_save("hardware_report.png")
        plt.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path
