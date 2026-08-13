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
# 输出格式 (--format text|json)
# ---------------------------------------------------------------------------


def format_option(func):
    """复用的 ``-f/--format text|json`` 选项。

    json 模式下 payload 由 services 层生成（与 MCP server 同源），只写 stdout；
    进度/错误/提示始终走 stderr，因此管道里的 JSON 永远干净。
    """
    return click.option(
        "-f",
        "--format",
        "output_format",
        type=click.Choice(["text", "json"], case_sensitive=False),
        default="text",
        show_default=True,
        help="Output format: text (Rich terminal) or json (machine-readable, stdout)",
    )(func)


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
      params    Browse/query/validate firmware parameter tables

    \b
    Parameter query examples:
      stune params                      # Available tables
      stune params ap --groups          # ArduPilot parameter groups
      stune params ap --group ATC_      # Parameters in a group
      stune params BATT_MONITOR         # One parameter: description + enum values
      stune params --search notch       # Ranked search across platforms
      stune params --validate BATT_MONITOR 4 -p ap
      stune params --lint               # Parameter-table health check

    \b
    The platform is auto-detected from the log file format.
    Use --platform to override: stune analyze -i log.bin --platform ardupilot
    """
    pass


# ---------------------------------------------------------------------------
# platforms — 列出支持的平台
# ---------------------------------------------------------------------------


@main.command()
@format_option
def platforms(output_format: str):
    """List all supported flight controller platforms."""
    if output_format == "json":
        from smarttune.output.json_output import emit_result

        sys.exit(emit_result("platforms", {"platforms": list_platforms()}))

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
@click.option(
    "-i",
    "--input",
    "log_file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Flight log file",
)
@click.option(
    "--platform",
    "platform_name",
    default="auto",
    help="Platform: auto, ardupilot, betaflight, px4 (default: auto)",
)
@click.option(
    "-o",
    "--output",
    "output_file",
    type=click.Path(path_type=Path),
    default=None,
    help="Output report file",
)
@click.option(
    "--report",
    "report_format",
    type=click.Choice(["md", "html"], case_sensitive=False),
    default=None,
    help="Report format: md (Markdown) or html (self-contained HTML)",
)
@click.option("--visual/--no-visual", default=False, help="Generate plots")
@click.option(
    "--axis",
    type=click.Choice(["roll", "pitch", "yaw", "all"], case_sensitive=False),
    default="all",
    help="Axis to analyze",
)
@click.option(
    "--theme",
    type=click.Choice(["light", "dark"], case_sensitive=False),
    default="light",
    help="Plot theme: light (default) or dark",
)
@format_option
def analyze(
    log_file: Path,
    platform_name: str,
    output_file: Optional[Path],
    report_format: Optional[str],
    visual: bool,
    axis: str,
    theme: str,
    output_format: str,
):
    """Comprehensive log analysis — PID + FFT + filter + mag recommendations."""
    # ── JSON path: single call into the services layer (same code path as MCP) ──
    if output_format == "json":
        from smarttune.output.json_output import emit_result, fail
        from smarttune.services.analysis import analyze_log

        if visual:
            _console.print("[dim]note: --visual is ignored in json mode[/dim]")
        if report_format:
            _console.print(f"[dim]note: --report {report_format} is ignored in json mode[/dim]")
        try:
            payload = analyze_log(log_file, platform=platform_name, axis=axis)
        except SmartTuneError as exc:
            sys.exit(fail("analyze", exc, output_file))
        sys.exit(emit_result("analyze", payload, output_file))

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
            progress.update(
                p_parse,
                completed=True,
                description=f"[green]✓ Parsed {flight_data.duration_s:.0f}s ({adapter.display_name})",
            )
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
                progress.update(
                    p_mag, completed=True, description="[green]✓ Magnetometer analysis complete"
                )
            except Exception as exc:
                module_failures.append(("Magnetometer", exc))
                progress.update(
                    p_mag, completed=True, description=f"[yellow]! Magnetometer skipped: {exc}"
                )

        # Check: at least one module must succeed
        if pid_result is None and fft_result is None and magfit_result is None:
            progress.stop()
            for mod_name, exc in module_failures:
                _console.print(f"\n[bold red]✗ {mod_name} failed:[/bold red]")
                if isinstance(exc, SmartTuneError):
                    _print_error(exc)
                else:
                    _console.print(f"  {exc}")
            _console.print(
                "\n[bold red]✗ Analysis failed: all modules unable to process this log[/bold red]"
            )
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
                progress.update(
                    p_html, completed=True, description="[green]✓ HTML report generated"
                )
                _console.print(
                    f"\n[bold green]✓[/bold green] HTML report saved: [cyan]{html_path}[/cyan]"
                )
            except Exception as exc:
                progress.update(
                    p_html, completed=True, description=f"[yellow]! HTML report failed: {exc}"
                )
        else:
            # Terminal + optional markdown output
            if pid_result is not None:
                fmt.format_pid(pid_result)
            if fft_result is not None:
                fmt.format_fft(fft_result)
            if magfit_result is not None:
                fmt.format_magfit(magfit_result)

            # ── Markdown report ──
            # (was: silently produced nothing when --report md was passed without -o;
            #  now mirrors the HTML path and derives a default filename)
            if effective_report_format == "md":
                md_path = output_file if output_file else Path(log_file.stem + "_report.md")
                md_path.write_text(fmt.to_markdown(full_result), encoding="utf-8")
                _console.print(f"\n[green]✓[/green] Report saved: [cyan]{md_path}[/cyan]")

        # ── Visual plots ─────────────────────────────────────────────────
        if visual:
            p_vis = progress.add_task("[cyan]Generating plots...", total=None)
            try:
                fmt.generate_all_plots(pid_result, fft_result)
                progress.update(p_vis, completed=True, description="[green]✓ Plots generated")
            except Exception as exc:
                progress.update(
                    p_vis, completed=True, description=f"[yellow]! Plot generation failed: {exc}"
                )

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
@click.option(
    "-i",
    "--input",
    "log_file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Flight log file",
)
@click.option(
    "--platform",
    "platform_name",
    default="auto",
    help="Platform: auto, ardupilot, betaflight, px4 (default: auto)",
)
@click.option(
    "-a",
    "--axis",
    type=click.Choice(["roll", "pitch", "yaw", "all"], case_sensitive=False),
    default="all",
    help="Axis to analyze (default: all)",
)
@click.option("--visual/--no-visual", default=False, help="Generate step response plots")
@click.option(
    "--theme",
    type=click.Choice(["light", "dark"], case_sensitive=False),
    default="light",
    help="Plot theme: light (default) or dark",
)
@format_option
def pid(
    log_file: Path, platform_name: str, axis: str, visual: bool, theme: str, output_format: str
):
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
    _run_single_analysis(
        "pid", log_file, platform_name, axis, visual, theme=theme, output_format=output_format
    )


# ---------------------------------------------------------------------------
# fft — FFT 分析
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "-i",
    "--input",
    "log_file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Flight log file",
)
@click.option(
    "--platform",
    "platform_name",
    default="auto",
    help="Platform: auto, ardupilot, betaflight, px4 (default: auto)",
)
@click.option("--visual/--no-visual", default=False, help="Generate FFT spectrum plot")
@click.option(
    "--theme",
    type=click.Choice(["light", "dark"], case_sensitive=False),
    default="light",
    help="Plot theme: light (default) or dark",
)
@format_option
def fft(log_file: Path, platform_name: str, visual: bool, theme: str, output_format: str):
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
    _run_single_analysis(
        "fft", log_file, platform_name, "all", visual, theme=theme, output_format=output_format
    )


# ---------------------------------------------------------------------------
# magfit — 磁力计
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "-i",
    "--input",
    "log_file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Flight log file",
)
@click.option(
    "--platform",
    "platform_name",
    default="auto",
    help="Platform: auto, ardupilot, betaflight, px4 (default: auto)",
)
@format_option
def magfit(log_file: Path, platform_name: str, output_format: str):
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
    _run_single_analysis(
        "magfit", log_file, platform_name, "all", False, output_format=output_format
    )


# ---------------------------------------------------------------------------
# sysid — 系统辨识
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "-i",
    "--input",
    "log_file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Flight log file",
)
@click.option(
    "--platform",
    "platform_name",
    default="auto",
    help="Platform: auto, ardupilot, betaflight, px4 (default: auto)",
)
@click.option(
    "-a",
    "--axis",
    type=click.Choice(["roll", "pitch", "yaw", "all"], case_sensitive=False),
    default="all",
    help="Axis to analyze (default: all)",
)
@click.option("--na", type=int, default=3, help="ARX model A polynomial order (default: 3)")
@click.option("--nb", type=int, default=2, help="ARX model B polynomial order (default: 2)")
@format_option
def sysid(log_file: Path, platform_name: str, axis: str, na: int, nb: int, output_format: str):
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
    _run_single_analysis(
        "sysid", log_file, platform_name, axis, False, na=na, nb=nb, output_format=output_format
    )


# ---------------------------------------------------------------------------
# hardware — 硬件报告
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "-i",
    "--input",
    "log_file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Flight log file",
)
@click.option(
    "--platform",
    "platform_name",
    default="auto",
    help="Platform: auto, ardupilot, betaflight, px4 (default: auto)",
)
@format_option
def hardware(log_file: Path, platform_name: str, output_format: str):
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
    _run_single_analysis(
        "hardware", log_file, platform_name, "all", False, output_format=output_format
    )


# ---------------------------------------------------------------------------
# filter — 滤波器传递函数分析 (#8)  [D1 fix: now calls services layer]
# ---------------------------------------------------------------------------


@main.command("filter")
@click.option(
    "-i",
    "--input",
    "log_file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Flight log file",
)
@click.option(
    "--platform",
    "platform_name",
    default="auto",
    help="Platform: auto, ardupilot, betaflight, px4 (default: auto)",
)
@click.option(
    "--gyro-filter", type=float, default=None, help="Override GYRO_FILTER cutoff frequency (Hz)"
)
@click.option("--notch-freq", type=float, default=None, help="Specify Notch center frequency (Hz)")
@click.option(
    "--auto/--no-auto",
    default=True,
    help="Auto-derive filter config from log parameters (default: on)",
)
@click.option("--visual/--no-visual", default=False, help="Generate Bode Plot visualization")
@click.option(
    "--theme",
    type=click.Choice(["light", "dark"], case_sensitive=False),
    default="light",
    help="Plot theme: light (default) or dark",
)
@format_option
def filter_cmd(
    log_file: Path,
    platform_name: str,
    gyro_filter: Optional[float],
    notch_freq: Optional[float],
    auto: bool,
    visual: bool,
    theme: str,
    output_format: str,
):
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

    # ── JSON path (bode arrays stay out of the payload — key points only) ──
    if output_format == "json":
        from smarttune.output.json_output import emit_result, fail

        try:
            result = analyze_filter(
                log_file,
                platform=platform_name,
                gyro_filter_hz=gyro_filter,
                notch_freq_hz=notch_freq,
                auto_derive=auto,
            )
        except SmartTuneError as exc:
            sys.exit(fail("filter", exc))
        result.pop("bode_data", None)
        sys.exit(emit_result("filter", result))

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
    mode_label = (
        "[yellow]Manual[/yellow]" if result["mode"] == "manual" else "[green]Auto-derived[/green]"
    )
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
@click.option(
    "-i",
    "--input",
    "log_file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Flight log file",
)
@click.option(
    "--platform",
    "platform_name",
    default="auto",
    help="Platform: auto, ardupilot, betaflight, px4 (default: auto)",
)
@click.option(
    "-o",
    "--output",
    "output_file",
    type=click.Path(path_type=Path),
    default=None,
    help="Output quality report file (optional)",
)
@format_option
def quality(log_file: Path, platform_name: str, output_file: Optional[Path], output_format: str):
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

    if output_format == "json":
        from smarttune.output.json_output import emit_result, fail

        try:
            payload = get_log_quality(log_file, platform=platform_name)
        except SmartTuneError as exc:
            sys.exit(fail("quality", exc, output_file))
        sys.exit(emit_result("quality", payload, output_file))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task("[cyan]Evaluating log quality...", total=None)
        try:
            result = get_log_quality(log_file, platform=platform_name)
            progress.update(
                task, completed=True, description="[green]✓ Quality evaluation complete"
            )
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


def _run_single_analysis(
    capability: str,
    log_file: Path,
    platform_name: str,
    axis: str,
    visual: bool,
    theme: str = "light",
    na: int = 3,
    nb: int = 2,
    output_format: str = "text",
):
    # ── JSON path: delegate to the services layer, emit one envelope, exit ──
    if output_format == "json":
        from smarttune.output.json_output import emit_result, fail
        from smarttune.services import analysis as svc

        if visual:
            _console.print("[dim]note: --visual is ignored in json mode[/dim]")
        try:
            if capability == "pid":
                payload = svc.analyze_pid(log_file, platform=platform_name, axis=axis)
            elif capability == "fft":
                payload = svc.analyze_fft(log_file, platform=platform_name)
            elif capability == "magfit":
                payload = svc.analyze_magfit(log_file, platform=platform_name)
            elif capability == "sysid":
                payload = svc.analyze_sysid(
                    log_file, platform=platform_name, axis=axis, na=na, nb=nb
                )
            elif capability == "hardware":
                payload = svc.analyze_hardware(log_file, platform=platform_name)
            else:
                from smarttune.errors import CapabilityNotSupportedError

                raise CapabilityNotSupportedError(
                    message=f"'{capability}' has no json serializer",
                    hint="Supported: pid, fft, magfit, sysid, hardware",
                )
        except SmartTuneError as exc:
            sys.exit(fail(capability, exc))
        sys.exit(emit_result(capability, payload))

    try:
        adapter = resolve_adapter(platform_name, log_file)
    except SmartTuneError as exc:
        _print_error(exc)
        sys.exit(1)

    if capability not in adapter.capabilities():
        from smarttune.errors import CapabilityNotSupportedError

        _print_error(
            CapabilityNotSupportedError(
                message=f"'{capability}' is not supported on {adapter.display_name}",
                hint=f"Supported: {', '.join(sorted(adapter.capabilities()))}",
            )
        )
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
                progress.update(
                    task2, completed=True, description=f"[green]✓ {capability.upper()} complete"
                )
                fmt.format_pid(pid_result, visual=visual)
            elif capability == "fft":
                fft_result = run_module("fft", adapter, flight_data, kb=kb)
                progress.update(
                    task2, completed=True, description=f"[green]✓ {capability.upper()} complete"
                )
                fmt.format_fft(fft_result, visual=visual)
            elif capability == "magfit":
                result = run_module("magfit", adapter, flight_data, kb=kb)
                progress.update(
                    task2, completed=True, description=f"[green]✓ {capability.upper()} complete"
                )
                fmt.format_magfit(result)
            elif capability == "sysid":
                # (既有 bug 修复：--na/--nb 选项之前从未被传递，恒用默认阶数)
                results = run_module("sysid", adapter, flight_data, kb=kb, axis=axis, na=na, nb=nb)
                progress.update(
                    task2, completed=True, description=f"[green]✓ {capability.upper()} complete"
                )
                fmt.format_sysid(results)
            elif capability == "hardware":
                report = run_module("hardware", adapter, flight_data, kb=kb)
                progress.update(
                    task2, completed=True, description=f"[green]✓ {capability.upper()} complete"
                )
                fmt.format_hardware(report, visual=visual)
            else:
                progress.update(
                    task2, completed=True, description=f"[yellow]{capability} integration pending"
                )
        except SmartTuneError as exc:
            _fail_in_progress(progress, exc)

    _console.print(f"\n[bold green]✓ {capability.upper()} analysis complete[/bold green]")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


@main.command()
@click.argument("query", required=False)
@click.option(
    "--platform", "-p", default=None, help="Target platform: ardupilot (ap), betaflight (bf), px4"
)
@click.option(
    "--search",
    "-s",
    "search_term",
    default=None,
    help="Keyword search across names, descriptions and enum labels",
)
@click.option(
    "--group",
    "-g",
    "group_name",
    default=None,
    help="List parameters in a firmware parameter group, e.g. ATC_ / GYRO_CONFIG",
)
@click.option(
    "--groups",
    "list_groups",
    is_flag=True,
    default=False,
    help="List the platform's parameter groups",
)
@click.option("--category", "-c", default="all", help="Filter by category, e.g. pid / filter")
@click.option(
    "--validate",
    "-v",
    "validate_pair",
    nargs=2,
    default=None,
    metavar="NAME VALUE",
    help="Validate a parameter name and value",
)
@click.option(
    "--lint",
    "run_lint",
    is_flag=True,
    default=False,
    help="Check the parameter table for data-integrity defects",
)
@click.option(
    "--limit", type=int, default=60, show_default=True, help="Max rows per listing (0 = no limit)"
)
@format_option
def params(
    query,
    platform,
    search_term,
    group_name,
    list_groups,
    category,
    validate_pair,
    run_lint,
    limit,
    output_format,
):
    """Query and validate flight controller parameters.

    \b
    Parameter tables are generated from official firmware metadata into
    smarttune/knowledge/params/<platform>.json (see tools/build_param_tables.py):
      ArduPilot   apm.pdef.json  — full names, @Values, @Bitmask, ranges
      PX4         px4params JSON — defaults, ranges, values, bitmasks
      Betaflight  cli/settings.c — names, PG_ groups, ranges, lookup tables

    \b
    Browse by group:
      stune params ap --groups              # 194 parameter groups
      stune params ap --group ATC_          # everything in the ATC_ group
      stune params bf --group PID_PROFILE
      stune params px4 --category pid

    \b
    Look up one parameter (description + enum members):
      stune params BATT_MONITOR
      stune params MC_ROLLRATE_P

    \b
    Search and validate:
      stune params --search notch                       # ranked, cross-platform
      stune params --validate BATT_MONITOR 4 -p ap      # enum member check
      stune params --validate p_roll 999 -p bf          # range check (exit 1)

    \b
    Data health:
      stune params --lint -p ap            # exit 1 if the table has defects
    """
    from smarttune.platform.params import ParamTable, to_full_dict, to_slim_dict

    _json = output_format == "json"
    if _json:
        from smarttune.output.json_output import emit_result, fail

    _ALIASES = {
        "ap": "ardupilot",
        "ardupilot": "ardupilot",
        "apm": "ardupilot",
        "bf": "betaflight",
        "betaflight": "betaflight",
        "px4": "px4",
        "pixhawk": "px4",
    }

    def _resolve_platform(value):
        return _ALIASES.get(str(value).lower(), str(value).lower()) if value else None

    def _load(plat):
        try:
            return ParamTable.from_knowledge(plat)
        except FileNotFoundError as exc:
            if _json:
                from smarttune.errors import UnsupportedPlatformError

                sys.exit(fail("params", UnsupportedPlatformError(message=str(exc))))
            _console.print(f"[red]{exc}[/red]")
            sys.exit(1)

    def _slim_table(rows, title):
        t = Table(title=title, show_header=True, box=None, title_justify="left")
        t.add_column("Parameter", style="cyan", no_wrap=True)
        t.add_column("Type")
        t.add_column("Range", justify="right")
        t.add_column("Unit")
        t.add_column("Summary")
        for p in rows:
            enum_note = (
                f"enum[{len(p.values)}]"
                if p.values
                else (f"bits[{len(p.bitmask)}]" if p.bitmask else p.type)
            )
            t.add_row(p.name, enum_note, p.range_str() or "—", p.unit or "", p.summary(70))
        return t

    def _emit_rows(command, rows, payload_extra):
        capped = rows if limit in (0, None) else rows[:limit]
        if _json:
            body = dict(payload_extra)
            body["count"] = len(rows)
            body["returned"] = len(capped)
            if len(capped) < len(rows):
                body["truncated"] = True
            body["params"] = [to_slim_dict(p) for p in capped]
            sys.exit(emit_result(command, body))
        title = payload_extra.get("title", "")
        _console.print(_slim_table(capped, title))
        if len(capped) < len(rows):
            _console.print(
                f"[dim]showing {len(capped)} of {len(rows)} — raise --limit for more[/dim]"
            )

    platform = _resolve_platform(platform)

    # ── lint ────────────────────────────────────────────────
    if run_lint:
        from smarttune.platform.param_lint import lint_table

        targets = [platform] if platform else ParamTable.available_platforms()
        reports = [lint_table(_load(p)) for p in targets]
        if _json:
            ok = all(r["ok"] for r in reports)
            emit_result("params.lint", {"ok": ok, "reports": reports})
            sys.exit(0 if ok else 1)
        bad = False
        for rep in reports:
            colour = "green" if rep["ok"] else "red"
            _console.print(
                f"\n[bold]{rep['platform']}[/bold] schema v{rep['schema_version']} — "
                f"{rep['parameter_count']} params — "
                f"[{colour}]{rep['error_count']} errors[/{colour}], "
                f"{rep['warning_count']} warnings"
            )
            for check, count in sorted(rep["by_check"].items(), key=lambda kv: -kv[1]):
                _console.print(f"  {check:<28} {count}")
            for f in rep["findings"][:10]:
                mark = "[red]✗[/red]" if f["severity"] == "error" else "[yellow]![/yellow]"
                _console.print(f"  {mark} {f['param'] or '(table)'}: {f['detail']}")
            if len(rep["findings"]) > 10:
                _console.print(f"  [dim]… {len(rep['findings']) - 10} more[/dim]")
            bad = bad or not rep["ok"]
        sys.exit(1 if bad else 0)

    # ── validate ────────────────────────────────────────────
    if validate_pair:
        name, val_str = validate_pair
        try:
            value = float(val_str)
        except ValueError:
            if _json:
                from smarttune.errors import InvalidParameterError

                sys.exit(
                    fail(
                        "params.validate",
                        InvalidParameterError(
                            message=f"Invalid value: {val_str}", hint="Pass a number"
                        ),
                    )
                )
            _console.print(f"[red]Invalid value: {val_str}[/red]")
            sys.exit(1)

        if not platform:
            if _json:
                from smarttune.errors import InvalidParameterError

                sys.exit(
                    fail(
                        "params.validate",
                        InvalidParameterError(
                            message="--platform is required for --validate",
                            hint="e.g. -p ap | -p bf | -p px4",
                        ),
                    )
                )
            _console.print("[red]--platform required for --validate[/red]")
            sys.exit(1)

        tbl = _load(platform)
        verdict = tbl.validate_detail(name, value)
        pd = tbl.query(name)
        if _json:
            body = {"platform": tbl.platform, "param": name, "value": value, **verdict}
            if pd is not None:
                body["parameter"] = to_full_dict(pd)
            emit_result("params.validate", body, status=verdict["status"])
            sys.exit(0 if verdict["valid"] else 1)

        if verdict["valid"]:
            _console.print(f"[green]✓ {verdict['message']}[/green]")
        else:
            _console.print(f"[red]✗ {verdict['message']}[/red]")
            if verdict.get("hint"):
                _console.print(f"[dim]  hint: {verdict['hint']}[/dim]")
        opts = verdict.get("options")
        if opts:
            _console.print(
                "[dim]  allowed:[/dim] "
                + ", ".join(
                    f"{k}={v}"
                    for k, v in sorted(
                        opts.items(),
                        key=lambda kv: int(kv[0]) if kv[0].lstrip("-").isdigit() else 0,
                    )
                )
            )
        sys.exit(0 if verdict["valid"] else 1)

    # ── search ──────────────────────────────────────────────
    if search_term:
        targets = [platform] if platform else ParamTable.available_platforms()
        hits = []
        for plat in targets:
            tbl = ParamTable.from_knowledge(plat)
            for p in tbl.search(search_term):
                hits.append((tbl.platform, p))
        if _json:
            capped = hits if limit in (0, None) else hits[:limit]
            emit_result(
                "params.search",
                {
                    "keyword": search_term,
                    "count": len(hits),
                    "returned": len(capped),
                    "matches": [{"platform": plat, **to_slim_dict(p)} for plat, p in capped],
                },
            )
            sys.exit(0)
        if not hits:
            _console.print(f"[yellow]No parameters matching '{search_term}'[/yellow]")
            sys.exit(0)
        by_platform = {}
        for plat, p in hits:
            by_platform.setdefault(plat, []).append(p)
        for plat, rows in by_platform.items():
            capped = rows if limit in (0, None) else rows[:limit]
            _console.print(
                _slim_table(capped, f"{plat} — {len(rows)} match(es) for '{search_term}'")
            )
            if len(capped) < len(rows):
                _console.print(
                    f"[dim]showing {len(capped)} of {len(rows)} — raise --limit for more[/dim]"
                )
        return

    # ── a bare query: platform alias, group name, or parameter name ──
    if query and not platform:
        alias = _ALIASES.get(query.lower())
        if alias:
            platform = alias
            query = None

    if query:
        targets = [platform] if platform else ParamTable.available_platforms()
        found = []
        for plat in targets:
            tbl = ParamTable.from_knowledge(plat)
            pd = tbl.query(query)
            if pd:
                found.append((tbl.platform, pd))
        if found:
            if _json:
                sys.exit(
                    emit_result(
                        "params.get",
                        {
                            "query": query,
                            "matches": [
                                {"platform": plat, **to_full_dict(pd)} for plat, pd in found
                            ],
                        },
                    )
                )
            for plat, pd in found:
                lines = [
                    f"[bold cyan]{pd.name}[/bold cyan]  [dim]{plat}[/dim]",
                    f"[dim]{pd.display_name or ''}[/dim]",
                    "",
                    f"[dim]group:[/dim] {pd.group or '—'}    [dim]category:[/dim] {pd.category}"
                    f"    [dim]type:[/dim] {pd.type}",
                    f"[dim]default:[/dim] {'unknown' if pd.default is None else pd.default}"
                    f"    [dim]range:[/dim] {pd.range_str() or '—'}"
                    + (f" {pd.unit}" if pd.unit else "")
                    + (f"    [dim]step:[/dim] {pd.increment}" if pd.increment is not None else ""),
                ]
                flags = []
                if pd.user:
                    flags.append(pd.user)
                if pd.reboot_required:
                    flags.append("reboot required")
                if pd.read_only:
                    flags.append("read-only")
                if flags:
                    lines.append(f"[dim]flags:[/dim] {', '.join(flags)}")
                if pd.description:
                    lines += ["", pd.description]
                if pd.values:
                    lines += ["", "[bold]values:[/bold]"]
                    for k in sorted(
                        pd.values, key=lambda x: int(x) if x.lstrip("-").isdigit() else 0
                    ):
                        lines.append(f"  {k:>4} = {pd.values[k]}")
                if pd.bitmask:
                    lines += ["", "[bold]bits:[/bold]"]
                    for k in sorted(pd.bitmask, key=lambda x: int(x) if x.isdigit() else 0):
                        lines.append(f"  bit {k:>2} = {pd.bitmask[k]}")
                if pd.unresolved_ref:
                    lines += ["", f"[yellow]note:[/yellow] {pd.unresolved_ref}"]
                _console.print(Panel("\n".join(lines), title=f"Parameter: {pd.name}", expand=False))
            return

        # not a parameter — maybe a group name
        for plat in targets:
            tbl = ParamTable.from_knowledge(plat)
            rows = tbl.list_by_group(query)
            if rows:
                _emit_rows(
                    "params.group",
                    rows,
                    {
                        "platform": tbl.platform,
                        "group": query,
                        "title": f"{tbl.platform} — group {query} ({len(rows)} params)",
                    },
                )

        if _json:
            from smarttune.errors import InvalidParameterError

            sys.exit(
                fail(
                    "params.get",
                    InvalidParameterError(
                        message=f"'{query}' is not a known parameter or group",
                        hint="Use --search for partial matches, or --groups to browse",
                        code="E4002",
                    ),
                )
            )
        _console.print(f"[yellow]'{query}' is not a known parameter or group[/yellow]")
        _console.print(
            "[dim]Try: stune params --search <keyword> | stune params <platform> --groups[/dim]"
        )
        sys.exit(1)

    # ── no platform: list available tables ──────────────────
    if not platform:
        available = ParamTable.available_platforms()
        tables = []
        for plat in available:
            tbl = ParamTable.from_knowledge(plat)
            tables.append(
                {
                    "platform": tbl.platform,
                    "key": plat,
                    "schema_version": tbl.schema_version,
                    "count": len(tbl),
                    "group_count": len(tbl.groups()),
                    "firmware": (tbl.meta.get("source") or {}).get("firmware", ""),
                }
            )
        if _json:
            sys.exit(emit_result("params.tables", {"tables": tables}))
        if not tables:
            _console.print("[yellow]No parameter tables found in knowledge base[/yellow]")
            return
        t = Table(title="Parameter tables", show_header=True, box=None, title_justify="left")
        t.add_column("Platform", style="cyan")
        t.add_column("Key")
        t.add_column("Params", justify="right")
        t.add_column("Groups", justify="right")
        t.add_column("Schema", justify="right")
        t.add_column("Firmware")
        for e in tables:
            t.add_row(
                e["platform"],
                e["key"],
                str(e["count"]),
                str(e["group_count"]),
                f"v{e['schema_version']}",
                e["firmware"] or "—",
            )
        _console.print(t)
        _console.print(
            "\n[dim]Browse: stune params ap --groups | stune params ap --group ATC_[/dim]"
        )
        return

    tbl = _load(platform)

    # ── group listing ───────────────────────────────────────
    if group_name:
        rows = tbl.list_by_group(group_name)
        if not rows:
            if _json:
                from smarttune.errors import InvalidParameterError

                sys.exit(
                    fail(
                        "params.group",
                        InvalidParameterError(
                            message=f"No group matching {group_name!r} in {tbl.platform}",
                            hint="List groups with --groups",
                        ),
                    )
                )
            _console.print(f"[yellow]No group matching '{group_name}' in {tbl.platform}[/yellow]")
            sys.exit(1)
        _emit_rows(
            "params.group",
            rows,
            {
                "platform": tbl.platform,
                "group": group_name,
                "title": f"{tbl.platform} — group {group_name} ({len(rows)} params)",
            },
        )

    # ── group index ─────────────────────────────────────────
    if list_groups or category == "all":
        groups = tbl.groups()
        if _json:
            sys.exit(
                emit_result(
                    "params.groups",
                    {
                        "platform": tbl.platform,
                        "parameter_count": len(tbl),
                        "group_count": len(groups),
                        "categories": tbl.categories(),
                        "groups": groups,
                    },
                )
            )
        t = Table(
            title=f"{tbl.platform} — {len(groups)} groups, {len(tbl)} parameters",
            show_header=True,
            box=None,
            title_justify="left",
        )
        t.add_column("Group", style="cyan")
        t.add_column("Params", justify="right")
        t.add_column("Category")
        t.add_column("Examples")
        for g in groups:
            t.add_row(g["group"], str(g["count"]), g["category"], ", ".join(g["sample"]))
        _console.print(t)
        _console.print(
            f"\n[dim]Drill in: stune params {platform} --group {groups[0]['group'] if groups else 'ATC_'}"
            f"  |  by topic: stune params {platform} -c pid[/dim]"
        )
        return

    # ── category listing ────────────────────────────────────
    rows = tbl.list_by_category(category)
    if not rows:
        if _json:
            from smarttune.errors import InvalidParameterError

            sys.exit(
                fail(
                    "params.list",
                    InvalidParameterError(
                        message=f"No parameters in category {category!r}",
                        hint=f"Available: {', '.join(tbl.categories())}",
                    ),
                )
            )
        _console.print(f"[yellow]No parameters in category '{category}'[/yellow]")
        _console.print(f"[dim]Available: {', '.join(tbl.categories())}[/dim]")
        sys.exit(1)
    _emit_rows(
        "params.list",
        rows,
        {
            "platform": tbl.platform,
            "category": category,
            "title": f"{tbl.platform} — category {category} ({len(rows)} params)",
        },
    )


if __name__ == "__main__":
    main()
