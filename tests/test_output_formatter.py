"""
tests/test_output_formatter.py

Test that OutputFormatter correctly translates ParamRef to platform names.
"""

import io
import numpy as np
import pytest
from rich.console import Console

from smarttune.models.flight_data import FlightData, AxisPIDSignal
from smarttune.models.analysis_result import (
    Assessment,
    ParamRef,
    ParamRecommendation,
    Confidence,
    StepMetrics,
    AxisPIDResult,
    PIDAnalysisResult,
    FullAnalysisResult,
)
from smarttune.output.formatter import OutputFormatter
from smarttune.platform.registry import get_adapter


def _make_pid_result():
    """Create a PID result with recommendations."""
    rec = ParamRecommendation(
        param=ParamRef("pid.roll.p", axis="roll", category="pid"),
        current=0.135,
        suggested=0.115,
        reason="Overshoot exceeds threshold",
        confidence=Confidence.HIGH,
        action="decrease",
    )
    ax = AxisPIDResult(
        axis="roll",
        metrics=StepMetrics(
            rise_time_ms=60.0,
            overshoot_percent=22.0,
            settling_time_ms=250.0,
            oscillation_count=1,
            steady_state_error_percent=2.5,
        ),
        assessment=Assessment.MARGINAL,
        recommendations=[rec],
        step_count=4,
    )
    return PIDAnalysisResult(
        axes={"roll": ax},
        overall_assessment=Assessment.MARGINAL,
    )


class TestParamTranslation:
    """Verify that OutputFormatter translates generic → platform param names."""

    def test_ardupilot_translation(self):
        adapter = get_adapter("ardupilot")
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        fmt = OutputFormatter(adapter=adapter, console=console)

        pid_result = _make_pid_result()
        fmt.format_pid(pid_result)

        output = buf.getvalue()
        assert "ATC_RAT_RLL_P" in output
        assert "pid.roll.p" not in output  # generic name should NOT appear

    def test_betaflight_translation(self):
        adapter = get_adapter("betaflight")
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        fmt = OutputFormatter(adapter=adapter, console=console)

        pid_result = _make_pid_result()
        fmt.format_pid(pid_result)

        output = buf.getvalue()
        assert "p_roll" in output

    def test_px4_translation(self):
        adapter = get_adapter("px4")
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        fmt = OutputFormatter(adapter=adapter, console=console)

        pid_result = _make_pid_result()
        fmt.format_pid(pid_result)

        output = buf.getvalue()
        assert "MC_ROLLRATE_P" in output


class TestMarkdownOutput:
    def test_markdown_generation(self):
        adapter = get_adapter("ardupilot")
        fmt = OutputFormatter(adapter=adapter)

        full = FullAnalysisResult(
            platform="ardupilot",
            log_file="test.bin",
            pid=_make_pid_result(),
        )
        md = fmt.to_markdown(full)
        assert "# SmartTune Analysis Report" in md
        assert "ATC_RAT_RLL_P" in md
        assert "MARGINAL" in md
        assert "0.1350" in md
        assert "0.1150" in md


class TestFullPipeline:
    def test_format_full(self):
        adapter = get_adapter("ardupilot")
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        fmt = OutputFormatter(adapter=adapter, console=console)

        full = FullAnalysisResult(
            platform="ardupilot",
            log_file="test.bin",
            pid=_make_pid_result(),
        )
        fmt.format_full(full)
        output = buf.getvalue()
        assert "ATC_RAT_RLL_P" in output
        assert "Total recommendations: 1" in output
