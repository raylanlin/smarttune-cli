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
      PX4         (.ulg)          — Partial support (v2.1)

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
        pid_result = None
        if "pid" in capabilities and flight_data.pid:
            p_pid = progress.add_task("[cyan]PID analysis...", total=None)
            try:
                from smarttune.analyzers.pid_reviewer import PIDReviewer
                reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))
                pid_result = reviewer.analyze(flight_data, axis=axis if axis != "all" else None)
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
                from smarttune.analyzers.fft_analyzer import FFTAnalyzer
                fft_analyzer = FFTAnalyzer(knowledge=kb.get("filter_rules", {}))
                fft_result = fft_analyzer.analyze(flight_data)
                progress.update(p_fft, completed=True, description="[green]✓ FFT analysis complete")
            except Exception as exc:
                module_failures.append(("FFT", exc))
                progress.update(p_fft, completed=True, description=f"[yellow]! FFT skipped: {exc}")

        # Phase 4: MagFit
        magfit_result = None
        if "magfit" in capabilities and flight_data.has_mag:
            p_mag = progress.add_task("[cyan]Magnetometer analysis...", total=None)
            try:
                from smarttune.analyzers.magfit import MAGFit
                magfit = MAGFit(knowledge=kb.get("magfit_rules", {}))
                magfit_result = magfit.analyze(flight_data)
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
              type=click.Path(exists=True, path_type=Path))
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
@click.option("-a", "--axis", type=click.Choice(["roll", "pitch", "yaw", "all"],
              case_sensitive=False), default="all")
@click.option("--visual/--no-visual", default=False,
              help="Generate step response plots")
@click.option("--theme", type=click.Choice(["light", "dark"], case_sensitive=False),
              default="light", help="Plot theme: light (default) or dark")
def pid(log_file: Path, platform_name: str, axis: str, visual: bool, theme: str):
    """PID step response analysis."""
    _run_single_analysis("pid", log_file, platform_name, axis, visual, theme=theme)


# ---------------------------------------------------------------------------
# fft — FFT 分析
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
@click.option("--visual/--no-visual", default=False,
              help="Generate FFT spectrum plot")
@click.option("--theme", type=click.Choice(["light", "dark"], case_sensitive=False),
              default="light", help="Plot theme: light (default) or dark")
def fft(log_file: Path, platform_name: str, visual: bool, theme: str):
    """FFT vibration spectrum analysis."""
    _run_single_analysis("fft", log_file, platform_name, "all", visual, theme=theme)


# ---------------------------------------------------------------------------
# magfit — 磁力计
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
def magfit(log_file: Path, platform_name: str):
    """Magnetometer calibration analysis."""
    _run_single_analysis("magfit", log_file, platform_name, "all", False)


# ---------------------------------------------------------------------------
# sysid — 系统辨识
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
@click.option("-a", "--axis", type=click.Choice(["roll", "pitch", "yaw", "all"],
              case_sensitive=False), default="all")
@click.option("--na", type=int, default=3, help="ARX model A polynomial order")
@click.option("--nb", type=int, default=2, help="ARX model B polynomial order")
def sysid(log_file: Path, platform_name: str, axis: str, na: int, nb: int):
    """System identification — ARX model parameter estimation."""
    _run_single_analysis("sysid", log_file, platform_name, axis, False)


# ---------------------------------------------------------------------------
# hardware — 硬件报告
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--platform", "platform_name", default="auto",
              help="Platform: auto, ardupilot, betaflight, px4 (default: auto)")
def hardware(log_file: Path, platform_name: str):
    """Hardware configuration report."""
    _run_single_analysis("hardware", log_file, platform_name, "all", False)


# ---------------------------------------------------------------------------
# filter — 滤波器传递函数分析 (#8)
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
    try:
        adapter = resolve_adapter(platform_name, log_file)
    except SmartTuneError as exc:
        _print_error(exc)
        sys.exit(1)

    import numpy as np

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

        task2 = progress.add_task("[cyan]FFT analysis...", total=None)
        try:
            from smarttune.analyzers.fft_analyzer import FFTAnalyzer
            from smarttune.knowledge import KnowledgeBase
            kb = KnowledgeBase(platform=adapter.name)
            analyzer = FFTAnalyzer(knowledge=kb.get("filter_rules", {}))
            fft_results = analyzer.analyze(flight_data)
            raw_spectrum = analyzer.get_spectrum_data() if hasattr(analyzer, 'get_spectrum_data') else {}
            progress.update(task2, completed=True, description="[green]✓ FFT analysis complete")
        except SmartTuneError as exc:
            _fail_in_progress(progress, exc)

    import importlib
    _ft_mod = importlib.import_module(
        f"smarttune.platform.{adapter.name}.filter_transfer"
    )
    compute_filter_response  = _ft_mod.compute_filter_response
    derive_filters_from_params = _ft_mod.derive_filters_from_params
    get_fallback_gyro_filter_hz = _ft_mod.get_fallback_gyro_filter_hz
    get_notch_bandwidth_hz = _ft_mod.get_notch_bandwidth_hz
    build_filter_display_lines = _ft_mod.build_filter_display_lines

    params = flight_data.params or {}
    sample_rate = flight_data.sample_rate_hz or 400
    freqs = np.linspace(1, sample_rate / 2, 500)

    # Determine mode: manual params take priority, otherwise auto-derive
    use_manual = (gyro_filter is not None or notch_freq is not None) or not auto
    config_summary = ""

    if use_manual:
        current_gyro = gyro_filter if gyro_filter is not None else get_fallback_gyro_filter_hz(params)
        notch_params = None
        if notch_freq is not None and notch_freq > 0:
            notch_params = {
                "center_hz": notch_freq,
                "bandwidth_hz": get_notch_bandwidth_hz(params),
                "attenuation_db": 30,
                "harmonics": 3,
            }
        mag_db, phase_deg = compute_filter_response(
            freqs, sample_rate, current_gyro, notch_params
        )
        config_summary = f"GYRO_FILTER={current_gyro:.0f}Hz"
        if notch_freq:
            config_summary += f", Notch={notch_freq:.0f}Hz"
    else:
        cfg = derive_filters_from_params(params)
        config_summary = cfg.get("config_summary", "auto")
        mag_db, phase_deg = compute_filter_response(freqs, sample_rate, params=params)

    # ── Text report ──
    _console.print(Panel("Filter Transfer Function Analysis", style="bold cyan"))
    mode_label = "[yellow]Manual[/yellow]" if use_manual else "[green]Auto-derived[/green]"
    _console.print(f"\n[bold]Mode:[/bold] {mode_label}")
    _console.print(f"[bold]Config:[/bold] {config_summary}")

    # Key frequency points
    key_freqs = [1, 5, 10, 20, 40, 80, 120, 200]
    table = Table(title="Key Frequency Response")
    table.add_column("Freq (Hz)", style="cyan")
    table.add_column("Magnitude (dB)", justify="right")
    table.add_column("Phase (°)", justify="right")

    for fk in key_freqs:
        if fk >= freqs[-1]:
            break
        idx = int(np.argmin(np.abs(freqs - fk)))
        table.add_row(str(fk), f"{mag_db[idx]:.1f}", f"{phase_deg[idx]:.1f}")
    _console.print(table)

    # -3dB cutoff
    idx_3db = np.where(mag_db < -3)[0]
    if idx_3db.size > 0:
        f_3db = freqs[idx_3db[0]]
        _console.print(f"\n[yellow]-3dB cutoff ≈ {f_3db:.1f} Hz[/yellow]")

    # Auto mode: show full filter chain info
    if not use_manual:
        _console.print("\n[bold]Filter chain:[/bold]")
        for line in build_filter_display_lines(params):
            _console.print(line)

    # ── Visualization ──
    if visual:
        try:
            from smarttune.output.filter_visualization import plot_bode
            out_path = Path.cwd() / "output"
            out_path.mkdir(parents=True, exist_ok=True)
            img_path = out_path / "filter_bode.png"
            plot_bode(
                freqs, mag_db, phase_deg,
                str(img_path),
                title=f"Filter Bode Plot ({config_summary})",
            )
            _console.print(f"\n[green]✓[/green] Bode Plot saved: {img_path}")
        except Exception as exc:
            _console.print(f"[yellow]Visualization failed: {exc}[/yellow]")

    _console.print("\n[bold green]✓ Filter analysis complete![/bold green]")


# ---------------------------------------------------------------------------
# quality — 日志质量评分 (#9)
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
    try:
        adapter = resolve_adapter(platform_name, log_file)
    except SmartTuneError as exc:
        _print_error(exc)
        sys.exit(1)

    import numpy as np

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=_console,
        transient=False,
    ) as progress:
        p_parse = progress.add_task("[cyan]Parsing log...", total=None)
        try:
            flight_data = adapter.parse(log_file)
            progress.update(p_parse, completed=True, description="[green]✓ Log parsed")
        except SmartTuneError as exc:
            _fail_in_progress(progress, exc)

        p_quality = progress.add_task("[cyan]Evaluating log quality...", total=None)

        issues: list[str] = []
        score = 100

        # ── 1. Data completeness ──
        has_pid = flight_data.pid is not None and len(flight_data.pid) > 0
        has_gyro = flight_data.gyro is not None and len(flight_data.gyro) > 0
        has_mag = flight_data.has_mag

        completeness_rows: list[tuple[str, int, bool, bool]] = []

        n_pid = 0
        if has_pid:
            for axis_data in flight_data.pid.values():
                if isinstance(axis_data, dict) and "time" in axis_data:
                    n_pid = max(n_pid, len(axis_data["time"]))
                elif isinstance(axis_data, dict) and "Desired" in axis_data:
                    n_pid = max(n_pid, len(axis_data["Desired"]))
        completeness_rows.append(("PID/RATE", n_pid, n_pid > 0, True))

        n_gyro = len(flight_data.gyro) if has_gyro else 0
        completeness_rows.append(("IMU/Gyro", n_gyro, n_gyro > 0, True))

        completeness_rows.append(("Magnetometer", 1 if has_mag else 0, has_mag, False))

        for name, count, ok, required in completeness_rows:
            if not ok and required:
                issues.append(f"❌ Missing required data: {name}")
                score -= 20
            elif not ok and not required:
                issues.append(f"⚠ Optional data missing: {name}")
                score -= 5

        # ── 2. Duration check ──
        duration_s = flight_data.duration_s
        duration_min = duration_s / 60.0

        if duration_s < 30:
            issues.append(f"❌ Log duration only {duration_s:.0f}s — far too short (recommend ≥ 3 min)")
            score -= 30
        elif duration_s < 120:
            issues.append(f"⚠ Log duration {duration_s:.0f}s is short — recommend at least 3 minutes")
            score -= 10
        elif duration_s < 300:
            issues.append(f"ℹ Log duration {duration_min:.1f} min — adequate for basic analysis")

        # ── 3. Excitation (step response windows) ──
        step_counts: dict[str, int] = {}
        if has_pid and n_pid > 10:
            try:
                from smarttune.analyzers.pid_reviewer import PIDReviewer
                from smarttune.knowledge import KnowledgeBase
                kb = KnowledgeBase(platform=adapter.name)
                reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))

                for ax in flight_data.axes:
                    try:
                        ax_data = flight_data.pid.get(ax, {})
                        desired = ax_data.get("Desired", ax_data.get("desired", np.array([])))
                        if hasattr(desired, '__len__') and len(desired) > 10:
                            # Detect steps via threshold
                            diff = np.abs(np.diff(desired))
                            threshold = np.std(desired) * 0.5 if np.std(desired) > 0 else 1.0
                            step_indices = np.where(diff > threshold)[0]
                            # Group nearby indices
                            if len(step_indices) > 0:
                                groups = 1
                                for j in range(1, len(step_indices)):
                                    if step_indices[j] - step_indices[j-1] > 10:
                                        groups += 1
                                step_counts[ax] = groups
                            else:
                                step_counts[ax] = 0
                        else:
                            step_counts[ax] = 0
                    except Exception:
                        step_counts[ax] = 0
            except Exception:
                pass

        if step_counts:
            min_steps = min(step_counts.values())
            total_steps = sum(step_counts.values())

            if total_steps < 3:
                ax_str = " / ".join(f"{a.capitalize()}:{step_counts.get(a, 0)}" for a in step_counts)
                issues.append(
                    f"❌ Insufficient step response windows ({ax_str})\n"
                    "  → Perform quick stick inputs in Stabilize/AltHold mode"
                )
                score -= 25
            elif min_steps < 3:
                weak_axis = min(step_counts, key=step_counts.get)
                issues.append(
                    f"⚠ {weak_axis.capitalize()} axis has few step windows ({step_counts[weak_axis]}), "
                    "PID analysis may be unreliable"
                )
                score -= 8
            elif min_steps >= 10:
                ax_str = " / ".join(f"{a.capitalize()}:{step_counts.get(a, 0)}" for a in step_counts)
                issues.append(f"✓ Excitation adequate: step windows sufficient ({ax_str})")

        # ── 4. Sample rate consistency ──
        rate_consistency_rows: list[tuple[str, float, float, float]] = []

        if has_pid and n_pid > 2:
            # Try to get time from first PID axis
            for ax_data in flight_data.pid.values():
                if isinstance(ax_data, dict) and "time" in ax_data:
                    t = np.array(ax_data["time"])
                    if len(t) > 2:
                        dts = np.diff(t)
                        dts_valid = dts[(dts > 0) & (dts < 1.0)]
                        if len(dts_valid) > 0:
                            median_dt = float(np.median(dts_valid))
                            sr = 1.0 / median_dt if median_dt > 0 else 0
                            std_dt = float(np.std(dts_valid))
                            jitter_pct = std_dt / median_dt * 100 if median_dt > 0 else 0
                            drop_count = int(np.sum(dts_valid > median_dt * 1.5))
                            drop_rate = drop_count / len(dts_valid) * 100
                            rate_consistency_rows.append(("RATE/PID", sr, jitter_pct, drop_rate))

                            if drop_rate > 5:
                                issues.append(f"⚠ RATE message drop rate is high ({drop_rate:.1f}%)")
                                score -= 8
                            if jitter_pct > 20:
                                issues.append(f"⚠ RATE message timing jitter is high ({jitter_pct:.1f}%)")
                                score -= 5
                    break

        score = max(0, min(100, score))

        # Rating
        if score >= 90:
            overall = "EXCELLENT"
            ov_color = "bold green"
        elif score >= 75:
            overall = "GOOD"
            ov_color = "green"
        elif score >= 55:
            overall = "MARGINAL"
            ov_color = "yellow"
        else:
            overall = "POOR"
            ov_color = "bold red"

        progress.update(p_quality, completed=True, description="[green]✓ Quality evaluation complete")

    # ── Build report ──
    lines = [
        "=" * 60,
        "  Log Quality Report",
        "=" * 60,
        f"  Log file: {log_file.name}",
        f"  File size: {log_file.stat().st_size / 1024 / 1024:.1f} MB",
        f"  Duration: {duration_min:.1f} min ({duration_s:.0f}s)",
        "",
        f"  Score: {score}/100  [{overall}]",
        "",
        "── Data Completeness ────────────────────────────────────",
    ]

    for name, count, ok, required in completeness_rows:
        status = "✓" if ok else ("❌" if required else "⚠")
        lines.append(f"  {status} {name:<15} {count:>8} samples")

    if step_counts:
        lines += ["", "── Excitation (Step Windows) ─────────────────────────────"]
        for ax, cnt in step_counts.items():
            bar = "█" * min(cnt, 20) + "░" * max(0, 20 - cnt)
            qual = "✓" if cnt >= 5 else ("⚠" if cnt >= 2 else "❌")
            lines.append(f"  {qual} {ax.capitalize():<8} {bar} {cnt:>3}")

    if rate_consistency_rows:
        lines += ["", "── Sample Rate Consistency ───────────────────────────────"]
        for name, sr, jitter, drop in rate_consistency_rows:
            lines.append(
                f"  {name:<12} Rate: {sr:.0f} Hz  Jitter: {jitter:.1f}%  Drops: {drop:.1f}%"
            )

    lines += ["", "── Issues & Recommendations ─────────────────────────────"]
    for issue in issues:
        lines.append(f"  {issue}")
    if not issues:
        lines.append("  ✓ No quality issues found — log quality is good")

    advice = "Proceed with analysis" if overall in ("EXCELLENT", "GOOD") else "Address issues above before analysis"
    lines += ["", "=" * 60, f"  Recommendation: {advice}", "=" * 60]

    report_text = "\n".join(lines)

    _console.print(f"\n[{ov_color}]Score: {score}/100 [{overall}][/{ov_color}]")
    for line in lines:
        _console.print(line)

    if output_file:
        output_file.write_text(report_text + "\n", encoding="utf-8")
        _console.print(f"\n[green]✓[/green] Quality report saved: [cyan]{output_file}[/cyan]")


# ---------------------------------------------------------------------------
# 通用单项分析流程
# ---------------------------------------------------------------------------

def _run_single_analysis(capability: str, log_file: Path, platform_name: str,
                         axis: str, visual: bool, theme: str = "light"):
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
        kb = KnowledgeBase(platform=adapter.name)
        fmt = OutputFormatter(adapter=adapter, theme=theme)

        try:
            if capability == "pid":
                from smarttune.analyzers.pid_reviewer import PIDReviewer
                reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))
                pid_result = reviewer.analyze(flight_data, axis=axis if axis != "all" else None)
                progress.update(task2, completed=True, description=f"[green]✓ {capability.upper()} complete")
                fmt.format_pid(pid_result, visual=visual)
            elif capability == "fft":
                from smarttune.analyzers.fft_analyzer import FFTAnalyzer
                fft_analyzer = FFTAnalyzer(knowledge=kb.get("filter_rules", {}))
                fft_result = fft_analyzer.analyze(flight_data)
                progress.update(task2, completed=True, description=f"[green]✓ {capability.upper()} complete")
                fmt.format_fft(fft_result, visual=visual)
            elif capability == "magfit":
                from smarttune.analyzers.magfit import MAGFit
                magfit = MAGFit(knowledge=kb.get("magfit_rules", {}))
                result = magfit.analyze(flight_data)
                progress.update(task2, completed=True, description=f"[green]✓ {capability.upper()} complete")
                fmt.format_magfit(result)
            elif capability == "sysid":
                from smarttune.analyzers.sysid_analyzer import SysIDAnalyzer
                analyzer = SysIDAnalyzer()
                results = analyzer.analyze(flight_data, axis=axis if axis != "all" else None)
                progress.update(task2, completed=True, description=f"[green]✓ {capability.upper()} complete")
                fmt.format_sysid(results)
            elif capability == "hardware":
                import importlib as _il
                _hr_mod = _il.import_module(
                    f"smarttune.platform.{adapter.name}.hardware_report"
                )
                report = _hr_mod.generate_hardware_report(flight_data.params, flight_data=flight_data)
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

if __name__ == "__main__":
    main()
