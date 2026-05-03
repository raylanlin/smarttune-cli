"""
smarttune/cli.py

SmartTune CLI 入口 — 多平台飞行日志分析与调参顾问。
"""

import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from smarttune import __version__
from smarttune.errors import SmartTuneError
from smarttune.platform.registry import resolve_adapter, list_platforms

_console = Console(stderr=True)


def _print_error(exc: SmartTuneError) -> None:
    _console.print(exc.rich_render())


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
      Betaflight  (.bbl / .bfl)   — Planned v2.0
      PX4         (.ulg)          — Planned v2.x

    \b
    Workflow:
      1. stune analyze -i log.bin           # Comprehensive analysis
      2. stune pid -i log.bin --visual      # PID step response
      3. stune fft -i log.bin --visual      # Vibration spectrum
      4. stune platforms                    # List supported platforms

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
@click.option("--visual/--no-visual", default=False, help="Generate plots")
@click.option("--axis", type=click.Choice(["roll", "pitch", "yaw", "all"], case_sensitive=False),
              default="all", help="Axis to analyze")
def analyze(log_file: Path, platform_name: str, output_file: Optional[Path],
            visual: bool, axis: str):
    """Comprehensive log analysis — PID + FFT + filter + mag recommendations."""
    try:
        adapter = resolve_adapter(platform_name, log_file)
    except SmartTuneError as exc:
        _print_error(exc)
        sys.exit(1)

    _console.print(f"[cyan]Platform:[/cyan] {adapter.display_name}")
    _console.print(f"[cyan]Capabilities:[/cyan] {', '.join(sorted(adapter.capabilities()))}")

    try:
        flight_data = adapter.parse(log_file)
    except SmartTuneError as exc:
        _print_error(exc)
        sys.exit(1)

    issues = flight_data.validate()
    if issues:
        _console.print("[yellow]Data quality warnings:[/yellow]")
        for issue in issues:
            _console.print(f"  ⚠ {issue}")

    _console.print(f"\n[green]✓[/green] Parsed {flight_data.duration_s:.0f}s of flight data")
    _console.print(f"  Axes: {', '.join(flight_data.axes)}")
    _console.print(f"  Sample rate: {flight_data.sample_rate_hz:.0f} Hz")
    _console.print(f"  Mag data: {'yes' if flight_data.has_mag else 'no'}")

    capabilities = adapter.capabilities()
    from smarttune.knowledge import KnowledgeBase
    from smarttune.output.formatter import OutputFormatter
    kb = KnowledgeBase(platform=adapter.name)
    fmt = OutputFormatter(adapter=adapter, output_file=output_file)

    from smarttune.models.analysis_result import FullAnalysisResult
    full_result = FullAnalysisResult(platform=adapter.name, log_file=str(log_file))

    # ── PID Analysis ──
    if "pid" in capabilities and flight_data.pid:
        from smarttune.analyzers.pid_reviewer import PIDReviewer
        _console.print("\n[cyan]Running PID analysis...[/cyan]")
        reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))
        full_result.pid = reviewer.analyze(flight_data, axis=axis if axis != "all" else None)
        fmt.format_pid(full_result.pid)

    # ── FFT Analysis ──
    if "fft" in capabilities and flight_data.gyro is not None:
        try:
            from smarttune.analyzers.fft_analyzer import FFTAnalyzer
            _console.print("\n[cyan]Running FFT analysis...[/cyan]")
            fft_analyzer = FFTAnalyzer(knowledge=kb.get("filter_rules", {}))
            fft_dict = fft_analyzer.analyze(flight_data)
            fmt.format_fft(fft_dict)
        except Exception as exc:
            _console.print(f"[yellow]FFT analysis skipped: {exc}[/yellow]")

    # ── MagFit ──
    if "magfit" in capabilities and flight_data.has_mag:
        try:
            from smarttune.analyzers.magfit import MAGFit
            _console.print("\n[cyan]Running magnetometer analysis...[/cyan]")
            magfit = MAGFit(knowledge=kb.get("magfit_rules", {}))
            magfit_result = magfit.analyze(flight_data)
            fmt.format_magfit(magfit_result)
        except Exception as exc:
            _console.print(f"[yellow]MagFit analysis skipped: {exc}[/yellow]")

    # ── Markdown report ──
    if output_file and output_file.suffix.lower() == ".md":
        md = fmt.to_markdown(full_result)
        output_file.write_text(md, encoding="utf-8")
        _console.print(f"\n[green]✓[/green] Report saved: [cyan]{output_file}[/cyan]")

    _console.print("\n[bold green]✓ Analysis complete[/bold green]")


# ---------------------------------------------------------------------------
# pid — PID 分析
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--platform", "platform_name", default="auto")
@click.option("-a", "--axis", type=click.Choice(["roll", "pitch", "yaw", "all"],
              case_sensitive=False), default="all")
@click.option("--visual/--no-visual", default=False)
def pid(log_file: Path, platform_name: str, axis: str, visual: bool):
    """PID step response analysis."""
    _run_single_analysis("pid", log_file, platform_name, axis, visual)


# ---------------------------------------------------------------------------
# fft — FFT 分析
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--platform", "platform_name", default="auto")
@click.option("--visual/--no-visual", default=False)
def fft(log_file: Path, platform_name: str, visual: bool):
    """FFT vibration spectrum analysis."""
    _run_single_analysis("fft", log_file, platform_name, "all", visual)


# ---------------------------------------------------------------------------
# magfit — 磁力计
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--platform", "platform_name", default="auto")
def magfit(log_file: Path, platform_name: str):
    """Magnetometer calibration analysis."""
    _run_single_analysis("magfit", log_file, platform_name, "all", False)


# ---------------------------------------------------------------------------
# sysid — 系统辨识
# ---------------------------------------------------------------------------

@main.command()
@click.option("-i", "--input", "log_file", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--platform", "platform_name", default="auto")
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
@click.option("--platform", "platform_name", default="auto")
def hardware(log_file: Path, platform_name: str):
    """Hardware configuration report."""
    _run_single_analysis("hardware", log_file, platform_name, "all", False)


# ---------------------------------------------------------------------------
# 通用单项分析流程
# ---------------------------------------------------------------------------

def _run_single_analysis(capability: str, log_file: Path, platform_name: str,
                         axis: str, visual: bool):
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

    try:
        flight_data = adapter.parse(log_file)
    except SmartTuneError as exc:
        _print_error(exc)
        sys.exit(1)

    _console.print(f"[green]✓[/green] Parsed {flight_data.duration_s:.0f}s of flight data")

    from smarttune.knowledge import KnowledgeBase
    from smarttune.output.formatter import OutputFormatter
    kb = KnowledgeBase(platform=adapter.name)
    fmt = OutputFormatter(adapter=adapter)

    if capability == "pid":
        from smarttune.analyzers.pid_reviewer import PIDReviewer
        reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))
        pid_result = reviewer.analyze(flight_data, axis=axis if axis != "all" else None)
        fmt.format_pid(pid_result)
    elif capability == "fft":
        from smarttune.analyzers.fft_analyzer import FFTAnalyzer
        fft_analyzer = FFTAnalyzer(knowledge=kb.get("filter_rules", {}))
        fft_result = fft_analyzer.analyze(flight_data)
        fmt.format_fft(fft_result)
    elif capability == "magfit":
        from smarttune.analyzers.magfit import MAGFit
        magfit = MAGFit(knowledge=kb.get("magfit_rules", {}))
        result = magfit.analyze(flight_data)
        fmt.format_magfit(result)
    elif capability == "sysid":
        from smarttune.analyzers.sysid_analyzer import SysIDAnalyzer
        analyzer = SysIDAnalyzer()
        results = analyzer.analyze(flight_data, axis=axis if axis != "all" else None)
        fmt.format_sysid(results)
    elif capability == "hardware":
        from smarttune.analyzers.hardware_report import generate_hardware_report
        report = generate_hardware_report(flight_data.params, flight_data=flight_data)
        fmt.format_hardware(report)
    else:
        _console.print(f"[yellow]{capability} analyzer integration pending[/yellow]")

    _console.print(f"\n[bold green]✓ {capability.upper()} analysis complete[/bold green]")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
