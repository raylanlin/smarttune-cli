"""
smarttune/cli.py

SmartTune CLI 入口 — 多平台飞行日志分析与调参顾问。
"""

import sys
from pathlib import Path
from typing import NoReturn, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.table import Table

from smarttune import __version__
from smarttune.errors import SmartTuneError
from smarttune.platform.registry import resolve_adapter, list_platforms

_console = Console(stderr=True)


def _print_error(exc: SmartTuneError) -> None:
    _console.print(exc.rich_render())


def _fail_in_progress(progress: Progress, exc: SmartTuneError) -> NoReturn:
    """在 Progress 上下文内遇到致命错误时的统一退出路径。"""
    progress.stop()
    _print_error(exc)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 入口组
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__, prog_name="smarttune", message="%(version)s")
def main():
    """SmartTune — Multi-platform flight log analysis & tuning advisor.

    \b
    Supported platforms:
      ArduPilot   (.bin / .log)   — Full support
      Betaflight  (.bbl / .bfl)   — Full support (v2.0)
      PX4         (.ulg)          — PID / FFT / SysID / Quality (pyulog required)

    \b
    Workflow:
      1. stune analyze -i log.bin           # Comprehensive analysis
      2. stune pid -i log.bin --visual      # PID step response
      3. stune fft -i log.bin --visual      # Vibration spectrum
      4. stune filter -i log.bin --visual   # Filter bode plot
      5. stune quality -i log.bin           # Log quality scoring
      6. stune platforms                    # List supported platforms

    \b
    Commands:
      analyze   Comprehensive analysis (PID + FFT + Mag)
      quality   Log quality scoring (data completeness / excitation / sample rate)
      pid       PID step response analysis
      fft       FFT vibration spectrum analysis
      filter    Filter transfer function analysis (Bode Plot)
      sysid     ARX system identification
      hardware  Hardware configuration report
      magfit    Magnetometer calibration analysis
      params    Query/validate firmware parameter tables

    \b
    Parameter query examples:
      stune params ap                  # List all ArduPilot parameters
      stune params ATC_RAT_RLL_P       # Show param details
      stune params --search notch       # Search across platforms
      stune params --validate ATC_RAT_RLL_P 0.15 -p ardupilot

    \b
    The platform is auto-detected from the log file format.
    Use --platform to override: stune analyze -i log.bin --platform ardupilot
    """
    pass


# ---------------------------------------------------------------------------
# platforms — 列出支持的平台
# ---------------------------------------------------------------------------

@main.command()
def platforms():
    """List all supported flight controller platforms."""
    table = Table(title="Supported Platforms")
    table.add_column("Platform", style="cyan")
    table.add_column("Display Name")
    table.add_column("Extensions")
    table.add_column("Capabilities")

    for p in list_platforms():
        table.add_row(p["name"], p["display_name"], p["extensions"], p["capabilities"])

    _console.print(table)


# ---------------------------------------------------------------------------
# analyze — 综合分析
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path), help="Flight log file")
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
@click.option("-o", "--output", "output_file", type=click.Path(path_type=Path),
              default=None, help="Output report file")
@click.option("--report", "report_format",
              type=click.Choice(["md", "html"], case_sensitive=False),
              default=None, help="Report format: md (Markdown) or html (self-contained HTML)")
@click.option("--visual/--no-visual", default=False, help="Generate plots")
@click.option("--axis", type=click.Choice(["roll", "pitch", "yaw", "all"], case_sensitive=False),
              default="all", help="Axis to analyze")
@click.option("--theme", type=click.Choice(["light", "dark"], case_sensitive=False),
              default="light", help="Plot theme: light (default) or dark")
def analyze(log_file: Path, platform_name: str, output_file: Optional[Path],
            report_format: Optional[str], visual: bool, axis: str, theme: str):
    """Comprehensive log analysis — PID + FFT + filter + mag recommendations."""
    try:
        adapter = resolve_adapter(platform_name, log_file)
    except SmartTuneError as exc:
        _print_error(exc)
        sys.exit(1)

    # 收集各模块的失败信息
    module_failures: list[tuple[str, Exception]] = []

    from smarttune.knowledge import KnowledgeBase
    from smarttune.output.formatter import OutputFormatter
    from smarttune.models.analysis_result import FullAnalysisResult

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=_console,
        transient=False,
    ) as progress:
        # Phase 1: Parse log
        p_parse = progress.add_task("[cyan]Parsing log...", total=None)
        try:
            flight_data = adapter.parse(log_file)
            progress.update(p_parse, completed=True,
                            description=f"[green]✓ Parsed {flight_data.duration_s:.0f}s ({adapter.display_name})")
        except SmartTuneError as exc:
            _fail_in_progress(progress, exc)

        capabilities = adapter.capabilities()
        kb = KnowledgeBase(platform=adapter.name)
        fmt = OutputFormatter(adapter=adapter, output_file=output_file, theme=theme)
        full_result = FullAnalysisResult(platform=adapter.name, log_file=str(log_file))

        # Phase 2: PID Analysis
        # A1 fix: analyzer wiring now lives ONLY in services.run_module —
        # the CLI just renders. (Was: duplicate PIDReviewer/FFTAnalyzer/MAGFit
        # instantiation here that drifted from the services layer.)
        from smarttune.services.analysis import run_module

        pid_result = None
        if "pid" in capabilities and flight_data.pid:
            p_pid = progress.add_task("[cyan]PID analysis...", total=None)
            try:
                pid_result = run_module("pid", adapter, flight_data, kb=kb, axis=axis)
                full_result.pid = pid_result
                progress.update(p_pid, completed=True, description="[green]✓ PID analysis complete")
            except Exception as exc:
                module_failures.append(("PID", exc))
                progress.update(p_pid, completed=True, description=f"[yellow]! PID skipped: {exc}")

        # Phase 3: FFT Analysis
        fft_result = None
        if "fft" in capabilities and flight_data.gyro is not None:
            p_fft = progress.add_task("[cyan]FFT analysis...", total=None)
            try:
                fft_result = run_module("fft", adapter, flight_data, kb=kb)
                full_result.fft = fft_result  # B2 fix: was never assigned
                progress.update(p_fft, completed=True, description="[green]✓ FFT analysis complete")
            except Exception as exc:
                module_failures.append(("FFT", exc))
                progress.update(p_fft, completed=True, description=f"[yellow]! FFT skipped: {exc}")

        # Phase 4: MagFit
        magfit_result = None
        if "magfit" in capabilities and flight_data.has_mag:
            p_mag = progress.add_task("[cyan]Magnetometer analysis...", total=None)
            try:
                magfit_result = run_module("magfit", adapter, flight_data, kb=kb)
                full_result.magfit = magfit_result
                progress.update(p_mag, completed=True, description="[green]✓ Magnetometer analysis complete")
            except Exception as exc:
                module_failures.append(("Magnetometer", exc))
                progress.update(p_mag, completed=True, description=f"[yellow]! Magnetometer skipped: {exc}")

        # Check: at least one module must succeed
        if pid_result is None and fft_result is None and magfit_result is None:
            progress.stop()
            for mod_name, exc in module_failures:
                _console.print(f"\n[bold red]✗ {mod_name} failed:[/bold red]")
                if isinstance(exc, SmartTuneError):
                    _print_error(exc)
                else:
                    _console.print(f"  {exc}")
            _console.print("\n[bold red]✗ Analysis failed: all modules unable to process this log[/bold red]")
            sys.exit(1)

        # ── Determine report format ──────────────────────────────────────
        effective_report_format = report_format
        if effective_report_format is None and output_file is not None:
            if output_file.suffix.lower() == ".html":
                effective_report_format = "html"
            elif output_file.suffix.lower() in (".md", ".txt"):
                effective_report_format = "md"

        # ── HTML report path ─────────────────────────────────────────────
        if effective_report_format == "html":
            p_html = progress.add_task("[cyan]Generating HTML report...", total=None)
            try:
                from smarttune.output.html_report import generate_html_report, save_html_report

                html_out = generate_html_report(
                    pid_results=pid_result,
                    fft_results=fft_result,
                    magfit_results=magfit_result,
                    log_path=str(log_file),
                )

                html_path = output_file if output_file else Path(log_file.stem + "_report.html")
                if html_path.suffix.lower() != ".html":
                    html_path = html_path.with_suffix(".html")

                save_html_report(html_out, str(html_path))
                progress.update(p_html, completed=True, description="[green]✓ HTML report generated")
                _console.print(f"\n[bold green]✓[/bold green] HTML report saved: [cyan]{html_path}[/cyan]")
            except Exception as exc:
                progress.update(p_html, completed=True,
                                description=f"[yellow]! HTML report failed: {exc}")
        else:
            # Terminal + optional markdown output
            if pid_result is not None:
                fmt.format_pid(pid_result)
            if fft_result is not None:
                fmt.format_fft(fft_result)
            if magfit_result is not None:
                fmt.format_magfit(magfit_result)

            # ── Markdown report ──
            if effective_report_format == "md" and output_file:
                md = fmt.to_markdown(full_result)
                output_file.write_text(md, encoding="utf-8")
                _console.print(f"\n[green]✓[/green] Report saved: [cyan]{output_file}[/cyan]")

        # ── Visual plots ─────────────────────────────────────────────────
        if visual:
            p_vis = progress.add_task("[cyan]Generating plots...", total=None)
            try:
                fmt.generate_all_plots(pid_result, fft_result)
                progress.update(p_vis, completed=True, description="[green]✓ Plots generated")
            except Exception as exc:
                progress.update(p_vis, completed=True,
                                description=f"[yellow]! Plot generation failed: {exc}")

    # ── Post-progress: render failures + summary ─────────────────────────
    if module_failures:
        _console.print()
        for mod_name, exc in module_failures:
            _console.print(f"[bold yellow]! {mod_name} skipped:[/bold yellow]")
            if isinstance(exc, SmartTuneError):
                _print_error(exc)
            else:
                _console.print(f"  {exc}")

        succeeded = []
        if pid_result is not None:
            succeeded.append("PID")
        if fft_result is not None:
            succeeded.append("FFT")
        if magfit_result is not None:
            succeeded.append("Magnetometer")
        failed = [name for name, _ in module_failures]
        _console.print(
            f"\n[bold yellow]✓ Analysis partially complete[/bold yellow] — "
            f"Success: {', '.join(succeeded) or 'none'} | "
            f"Failed: {', '.join(failed)}"
        )
    else:
        _console.print("\n[bold green]✓ Analysis complete![/bold green]")


# ---------------------------------------------------------------------------
# pid — PID 分析
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path), help="Flight log file")
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
@click.option("-a", "--axis", type=click.Choice(["roll", "pitch", "yaw", "all"],
              case_sensitive=False), default="all",
              help="Axis to analyze (default: all)")
@click.option("--visual/--no-visual", default=False,
              help="Generate step response plots")
@click.option("--theme", type=click.Choice(["light", "dark"], case_sensitive=False),
              default="light", help="Plot theme: light (default) or dark")
def pid(log_file: Path, platform_name: str, axis: str, visual: bool, theme: str):
    """PID step response analysis.

    \b
    Detects stick-input step responses from flight data and evaluates:
      · Rise time, overshoot, settling time, oscillation count
      · Per-axis diagnostics with tuning recommendations

    \b
    Examples:
      stune pid -i flight.bin                  # All axes
      stune pid -i flight.bin -a roll          # Roll only
      stune pid -i flight.bin -a roll --visual # Roll with plots
    """
    _run_single_analysis("pid", log_file, platform_name, axis, visual, theme=theme)


# ---------------------------------------------------------------------------
# fft — FFT 分析
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path), help="Flight log file")
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
@click.option("--visual/--no-visual", default=False,
              help="Generate FFT spectrum plot")
@click.option("--theme", type=click.Choice(["light", "dark"], case_sensitive=False),
              default="light", help="Plot theme: light (default) or dark")
def fft(log_file: Path, platform_name: str, visual: bool, theme: str):
    """FFT vibration spectrum analysis.

    \b
    Analyzes gyro data to identify vibration frequencies and suggests:
      · Vibration severity rating (EXCELLENT/GOOD/MARGINAL/POOR)
      · Notch filter parameters (INS_HNTCH_FREQ, INS_HNTCH_BW)

    \b
    Examples:
      stune fft -i flight.bin          # Basic analysis
      stune fft -i flight.bin --visual # With spectrum plot
    """
    _run_single_analysis("fft", log_file, platform_name, "all", visual, theme=theme)


# ---------------------------------------------------------------------------
# magfit — 磁力计
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path), help="Flight log file")
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
def magfit(log_file: Path, platform_name: str):
    """Magnetometer calibration analysis.

    \b
    Evaluates compass calibration quality:
      · Fitness score (mGauss) — lower is better
      · Hard iron / soft iron interference diagnosis
      · Flight coverage check (yaw/pitch/roll range)

    \b
    Example:
      stune magfit -i flight.bin
    """
    _run_single_analysis("magfit", log_file, platform_name, "all", False)


# ---------------------------------------------------------------------------
# sysid — 系统辨识
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path), help="Flight log file")
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
@click.option("-a", "--axis", type=click.Choice(["roll", "pitch", "yaw", "all"],
              case_sensitive=False), default="all",
              help="Axis to analyze (default: all)")
@click.option("--na", type=int, default=3, help="ARX model A polynomial order (default: 3)")
@click.option("--nb", type=int, default=2, help="ARX model B polynomial order (default: 2)")
def sysid(log_file: Path, platform_name: str, axis: str, na: int, nb: int):
    """System identification — ARX model parameter estimation.

    \b
    Estimates transfer function from flight data:
      · Natural frequency, damping ratio, time constant
      · PID bandwidth recommendations

    \b
    Examples:
      stune sysid -i flight.bin                  # All axes (na=3, nb=2)
      stune sysid -i flight.bin -a roll --na 4   # Custom ARX order
    """
    _run_single_analysis("sysid", log_file, platform_name, axis, False, na=na, nb=nb)


# ---------------------------------------------------------------------------
# hardware — 硬件报告
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path), help="Flight log file")
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
def hardware(log_file: Path, platform_name: str):
    """Hardware configuration report.

    \b
    Displays sensor configuration and active parameters:
      · IMU setup (gyro/accel IDs, calibration status)
      · Compass configuration
      · Active filter settings
      · Rate PID parameters

    \b
    Example:
      stune hardware -i flight.bin
    """
    _run_single_analysis("hardware", log_file, platform_name, "all", False)


# ---------------------------------------------------------------------------
# filter — 滤波器传递函数分析 (#8)  [D1 fix: now calls services layer]
# ---------------------------------------------------------------------------

@main.command("filter")
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path), help="Flight log file")
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
@click.option("--gyro-filter", type=float, default=None,
              help="Override GYRO_FILTER cutoff frequency (Hz)")
@click.option("--notch-freq", type=float, default=None,
              help="Specify Notch center frequency (Hz)")
@click.option("--auto/--no-auto", default=True,
              help="Auto-derive filter config from log parameters (default: on)")
@click.option("--visual/--no-visual", default=False, help="Generate Bode Plot visualization")
@click.option("--theme", type=click.Choice(["light", "dark"], case_sensitive=False),
              default="light", help="Plot theme: light (default) or dark")
def filter_cmd(log_file: Path, platform_name: str, gyro_filter: Optional[float],
               notch_freq: Optional[float], auto: bool, visual: bool, theme: str):
    """Filter transfer function analysis (Bode Plot).

    \b
    Two modes:
      - Auto mode (default): derive filter config from log parameters
        (platform-specific: INS_HNTCH_* for ArduPilot, gyro_lowpass_hz / notch for BF)
      - Manual mode: specify --gyro-filter/--notch-freq directly

    \b
    Examples:
      stune filter -i flight.bin                         # auto-derive
      stune filter -i flight.bin --no-auto --gyro-filter 20 --visual
      stune filter -i flight.bin --notch-freq 80 --visual
    """
    from smarttune.services.analysis import analyze_filter  # D1 fix: call services layer

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task("[cyan]Analyzing filter...", total=None)
        try:
            result = analyze_filter(
                log_file,
                platform=platform_name,
                gyro_filter_hz=gyro_filter,
                notch_freq_hz=notch_freq,
                auto_derive=auto,
                _include_bode_data=visual,
            )
            progress.update(task, completed=True, description="[green]✓ Filter analysis complete")
        except SmartTuneError as exc:
            progress.stop()
            _print_error(exc)
            sys.exit(1)

    # ── Text report ──
    _console.print(Panel("Filter Transfer Function Analysis", style="bold cyan"))
    mode_label = "[yellow]Manual[/yellow]" if result["mode"] == "manual" else "[green]Auto-derived[/green]"
    _console.print(f"\n[bold]Mode:[/bold] {mode_label}")
    _console.print(f"[bold]Config:[/bold] {result['config_summary']}")

    key_pts = result.get("key_frequency_response", [])
    if key_pts:
        table = Table(title="Key Frequency Response")
        table.add_column("Freq (Hz)", style="cyan")
        table.add_column("Magnitude (dB)", justify="right")
        table.add_column("Phase (°)", justify="right")
        for pt in key_pts:
            table.add_row(
                str(pt["frequency_hz"]),
                f"{pt['magnitude_db']:.1f}",
                f"{pt['phase_deg']:.1f}",
            )
        _console.print(table)

    if result.get("cutoff_3db_hz"):
        _console.print(f"\n[yellow]-3dB cutoff ≈ {result['cutoff_3db_hz']:.1f} Hz[/yellow]")

    filter_chain = result.get("filter_chain")
    if filter_chain and result["mode"] == "auto":
        _console.print("\n[bold]Filter chain:[/bold]")
        for line in filter_chain:
            _console.print(line)

    # ── Visualization ──
    if visual:
        bode = result.get("bode_data")
        if bode:
            try:
                import numpy as np
                from smarttune.output.filter_visualization import plot_bode
                out_path = Path.cwd() / "output"
                out_path.mkdir(parents=True, exist_ok=True)
                img_path = out_path / "filter_bode.png"
                plot_bode(
                    np.array(bode["freqs"]),
                    np.array(bode["magnitude_db"]),
                    np.array(bode["phase_deg"]),
                    str(img_path),
                    title=f"Filter Bode Plot ({result['config_summary']})",
                )
                _console.print(f"\n[green]✓[/green] Bode Plot saved: {img_path}")
            except Exception as exc:
                _console.print(f"[yellow]Visualization failed: {exc}[/yellow]")

    _console.print("\n[bold green]✓ Filter analysis complete![/bold green]")


# ---------------------------------------------------------------------------
# quality — 日志质量评分 (#9)  [B3 fix: calls services layer, removed dup logic]
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path), help="Flight log file")
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
@click.option("-o", "--output", "output_file", type=click.Path(path_type=Path),
              default=None, help="Output quality report file (optional)")
def quality(log_file: Path, platform_name: str, output_file: Optional[Path]):
    """Evaluate log quality — data completeness, excitation, and sample rate scoring.

    \b
    Dimensions:
      · Data completeness — critical message types present
      · Duration          — sufficient for analysis
      · Excitation        — enough PID step response windows
      · Sample rate       — RATE/IMU sampling consistency & drop rate

    \b
    Examples:
      stune quality -i flight.bin
      stune quality -i flight.bin -o quality_report.txt
    """
    from smarttune.services.analysis import get_log_quality

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task("[cyan]Evaluating log quality...", total=None)
        try:
            result = get_log_quality(log_file, platform=platform_name)
            progress.update(task, completed=True, description="[green]✓ Quality evaluation complete")
        except SmartTuneError as exc:
            progress.stop()
            _print_error(exc)
            sys.exit(1)

    q = result["quality"]
    score = q["score"]
    rating = q["rating"]
    advice = q["advice"]
    duration_s = result["duration_s"]
    duration_min = duration_s / 60.0

    if score >= 90:
        ov_color = "bold green"
    elif score >= 75:
        ov_color = "green"
    elif score >= 55:
        ov_color = "yellow"
    else:
        ov_color = "bold red"

    lines = [
        "=" * 60,
        "  Log Quality Report",
        "=" * 60,
        f"  Log file:  {result['log_file']}",
        f"  Platform:  {result['display_name']}",
        f"  File size: {result['file_size_mb']:.1f} MB",
        f"  Duration:  {duration_min:.1f} min ({duration_s:.0f}s)",
        "",
        f"  Score: {score}/100  [{rating}]",
        "",
        "── Data Completeness ────────────────────────────────────",
    ]

    for item in result.get("data_completeness", []):
        status = "✓" if item["ok"] else ("❌" if item["required"] else "⚠")
        lines.append(f"  {status} {item['name']:<15} {item['samples']:>8} samples")

    step_counts = result.get("step_counts")
    if step_counts:
        lines += ["", "── Excitation (Step Windows) ─────────────────────────────"]
        for ax, cnt in step_counts.items():
            bar = "█" * min(cnt, 20) + "░" * max(0, 20 - cnt)
            qual = "✓" if cnt >= 5 else ("⚠" if cnt >= 2 else "❌")
            lines.append(f"  {qual} {ax.capitalize():<8} {bar} {cnt:>3}")

    rate_consistency = result.get("rate_consistency")
    if rate_consistency:
        lines += ["", "── Sample Rate Consistency ───────────────────────────────"]
        for rc in rate_consistency:
            lines.append(
                f"  {rc['source']:<12} Rate: {rc['sample_rate_hz']:.0f} Hz  "
                f"Jitter: {rc['jitter_percent']:.1f}%  Drops: {rc['drop_rate_percent']:.1f}%"
            )

    issues = result.get("validation_issues", [])
    if issues:
        lines += ["", "── Validation Issues ────────────────────────────────────"]
        for issue in issues:
            lines.append(f"  ⚠ {issue}")

    lines += [
        "",
        "=" * 60,
        f"  Recommendation: {advice}",
        "=" * 60,
    ]

    report_text = "\n".join(lines)

    _console.print(f"\n[{ov_color}]Score: {score}/100 [{rating}][/{ov_color}]")
    for line in lines:
        _console.print(line)

    if output_file:
        output_file.write_text(report_text + "\n", encoding="utf-8")
        _console.print(f"\n[green]✓[/green] Quality report saved: [cyan]{output_file}[/cyan]")


# ---------------------------------------------------------------------------
# 通用单项分析流程
# ---------------------------------------------------------------------------

def _run_single_analysis(capability: str, log_file: Path, platform_name: str,
                         axis: str, visual: bool, theme: str = "light",
                         na: int = 3, nb: int = 2):
    try:
        adapter = resolve_adapter(platform_name, log_file)
    except SmartTuneError as exc:
        _print_error(exc)
        sys.exit(1)

    if capability not in adapter.capabilities():
        from smarttune.errors import CapabilityNotSupportedError
        _print_error(CapabilityNotSupportedError(
            message=f"'{capability}' is not supported on {adapter.display_name}",
            hint=f"Supported: {', '.join(sorted(adapter.capabilities()))}",
        ))
        sys.exit(1)

    _console.print(f"[cyan]Platform:[/cyan] {adapter.display_name}")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task("[cyan]Parsing log...", total=None)
        try:
            flight_data = adapter.parse(log_file)
            progress.update(task, completed=True, description="[green]✓ Log parsed")
        except SmartTuneError as exc:
            _fail_in_progress(progress, exc)

        task2 = progress.add_task(f"[cyan]{capability.upper()} analysis...", total=None)
        from smarttune.knowledge import KnowledgeBase
        from smarttune.output.formatter import OutputFormatter
        from smarttune.services.analysis import run_module  # A1 fix: shared wiring
        kb = KnowledgeBase(platform=adapter.name)
        fmt = OutputFormatter(adapter=adapter, theme=theme)

        try:
            if capability == "pid":
                pid_result = run_module("pid", adapter, flight_data, kb=kb, axis=axis)
                progress.update(task2, completed=True, description=f"[green]✓ {capability.upper()} complete")
                fmt.format_pid(pid_result, visual=visual)
            elif capability == "fft":
                fft_result = run_module("fft", adapter, flight_data, kb=kb)
                progress.update(task2, completed=True, description=f"[green]✓ {capability.upper()} complete")
                fmt.format_fft(fft_result, visual=visual)
            elif capability == "magfit":
                result = run_module("magfit", adapter, flight_data, kb=kb)
                progress.update(task2, completed=True, description=f"[green]✓ {capability.upper()} complete")
                fmt.format_magfit(result)
            elif capability == "sysid":
                # (既有 bug 修复：--na/--nb 选项之前从未被传递，恒用默认阶数)
                results = run_module("sysid", adapter, flight_data, kb=kb, axis=axis, na=na, nb=nb)
                progress.update(task2, completed=True, description=f"[green]✓ {capability.upper()} complete")
                fmt.format_sysid(results)
            elif capability == "hardware":
                report = run_module("hardware", adapter, flight_data, kb=kb)
                progress.update(task2, completed=True, description=f"[green]✓ {capability.upper()} complete")
                fmt.format_hardware(report, visual=visual)
            else:
                progress.update(task2, completed=True,
                                description=f"[yellow]{capability} integration pending")
        except SmartTuneError as exc:
            _fail_in_progress(progress, exc)

    _console.print(f"\n[bold green]✓ {capability.upper()} analysis complete[/bold green]")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

@main.command()
@click.argument("query", required=False)
@click.option("--platform", "-p", default=None, help="Target platform: ardupilot, betaflight, px4")
@click.option("--search", "-s", "search_term", default=None, help="Search keyword across all platforms")
@click.option("--category", "-c", default="all", help="Filter by category")
@click.option("--validate", "-v", "validate_pair", nargs=2, default=None, metavar="NAME VALUE",
              help="Validate a parameter name and value")
def params(query, platform, search_term, category, validate_pair):
    """Query and validate flight controller parameters.

    \b
    Query parameter tables loaded from knowledge base JSON files
    (smarttune/knowledge/params/). Data scraped from official firmware repos.

    \b
    Examples:
      stune params ap                  # List all ArduPilot parameters
      stune params bf                  # List all Betaflight parameters
      stune params px4                 # List all PX4 parameters
      stune params ATC_RAT_RLL_P       # Show detail for a specific param
      stune params --search notch       # Search for "notch" across all platforms
      stune params --validate ATC_RAT_RLL_P 0.15 --platform ardupilot
      stune params --category pid --platform betaflight
    """
    from smarttune.platform.params import ParamTable
    from rich.table import Table
    from rich.panel import Panel
    import json

    _PLATFORM_ALIASES = {
        "ap": "ardupilot", "ardupilot": "ardupilot",
        "bf": "betaflight", "betaflight": "betaflight",
        "px4": "px4",
    }

    # ── validate mode ──────────────────────────────────────
    if validate_pair:
        name, val_str = validate_pair
        try:
            value = float(val_str)
        except ValueError:
            _console.print(f"[red]Invalid value: {val_str}[/red]")
            sys.exit(1)

        if not platform:
            _console.print("[red]--platform required for --validate[/red]")
            sys.exit(1)

        platform = _PLATFORM_ALIASES.get(platform, platform)
        try:
            tbl = ParamTable.from_knowledge(platform)
        except FileNotFoundError as e:
            _console.print(f"[red]{e}[/red]")
            sys.exit(1)

        ok, msg = tbl.validate(name, value)
        if ok:
            _console.print(f"[green]✓ {msg}[/green]")
            sys.exit(0)
        else:
            _console.print(f"[red]✗ {msg}[/red]")
            sys.exit(1)

    # ── search mode ────────────────────────────────────────
    if search_term:
        available = ParamTable.available_platforms()
        if platform:
            platform = _PLATFORM_ALIASES.get(platform, platform)
            available = [platform] if platform in available else []

        if not available:
            _console.print("[red]No platforms available[/red]")
            sys.exit(1)

        total = 0
        for plat in available:
            tbl = ParamTable.from_knowledge(plat)
            results = tbl.search(search_term)
            if not results:
                continue
            total += len(results)
            _console.print(f"\n[bold cyan]{tbl.platform}[/bold cyan] — {len(results)} match(es)")
            t = Table(show_header=True, box=None)
            t.add_column("Parameter", style="cyan")
            t.add_column("Category")
            t.add_column("Type")
            t.add_column("Range")
            t.add_column("Description")
            for r in results:
                rng = ""
                if r.min is not None or r.max is not None:
                    rng = f"[{r.min}, {r.max}]"
                if r.unit:
                    rng += f" {r.unit}"
                t.add_row(r.name, r.category, r.type, rng, r.description[:60])
            _console.print(t)

        if total == 0:
            _console.print(f"[yellow]No parameters matching '{search_term}'[/yellow]")
        return

    # ── query mode (specific param or platform list) ───────
    if query:
        # Try as platform alias first
        plat_guess = _PLATFORM_ALIASES.get(query.lower())
        if plat_guess:
            # List all for this platform
            platform = plat_guess
        else:
            # Treat as parameter name — try all platforms
            available = ParamTable.available_platforms()
            found = []
            for plat in available:
                tbl = ParamTable.from_knowledge(plat)
                pd = tbl.query(query)
                if pd:
                    found.append((plat, pd))

            if found:
                for plat, pd in found:
                    _console.print(Panel.fit(
                        f"[bold cyan]{pd.name}[/bold cyan]\n"
                        f"[dim]Platform:[/dim] {plat}  |  "
                        f"[dim]Category:[/dim] {pd.category}  |  "
                        f"[dim]Type:[/dim] {pd.type}\n"
                        f"[dim]Default:[/dim] {pd.default}\n"
                        f"[dim]Range:[/dim] [{pd.min}, {pd.max}]"
                        + (f" {pd.unit}" if pd.unit else "") + "\n"
                        f"[dim]Description:[/dim] {pd.description or '—'}",
                        title=f"Parameter: {pd.name}"
                    ))
                return
            else:
                _console.print(f"[yellow]Parameter '{query}' not found in any platform[/yellow]")
                _console.print(f"[dim]Try --search for partial matches[/dim]")
                sys.exit(1)

    # ── list mode (platform specified or no args) ──────────
    if not platform:
        # No args at all — show available platforms
        available = ParamTable.available_platforms()
        if not available:
            _console.print("[yellow]No parameter tables found in knowledge base[/yellow]")
            return
        _console.print("[bold]Available parameter tables:[/bold]")
        for plat in available:
            tbl = ParamTable.from_knowledge(plat)
            cats = ", ".join(tbl.categories())
            _console.print(f"  [cyan]{tbl.platform}[/cyan] — {len(tbl)} params ({cats})")
        _console.print("\n[dim]Usage: stune params ap | stune params bf | stune params px4[/dim]")
        return

    platform = _PLATFORM_ALIASES.get(platform, platform)
    try:
        tbl = ParamTable.from_knowledge(platform)
    except FileNotFoundError as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)

    params_list = tbl.list_all()
    if category != "all":
        params_list = tbl.list_by_category(category)

    cat_counts = {}
    for p in params_list:
        cat_counts[p.category] = cat_counts.get(p.category, 0) + 1

    _console.print(f"\n[bold]{tbl.platform}[/bold] — {len(params_list)} parameters"
                   + (f" (category: {category})" if category != "all" else "")
                   + (f"  [dim]categories: {', '.join(f'{k}({v})' for k,v in sorted(cat_counts.items()))}[/dim]" if category == "all" else ""))

    t = Table(show_header=True, box=None)
    t.add_column("Parameter", style="cyan")
    t.add_column("Category")
    t.add_column("Type")
    t.add_column("Default")
    t.add_column("Range")
    t.add_column("Description")
    for p in sorted(params_list, key=lambda x: (x.category, x.name)):
        rng = ""
        if p.min is not None or p.max is not None:
            rng = f"[{p.min}, {p.max}]"
        if p.unit:
            rng += f" {p.unit}"
        t.add_row(p.name, p.category, p.type, str(p.default), rng, (p.description or "")[:80])
    _console.print(t)


if __name__ == "__main__":
    main()
