"""
tests/test_pid_reviewer.py

Test the migrated PID reviewer with synthetic flight data.
"""

import numpy as np
import pytest
from smarttune.models.flight_data import FlightData, AxisPIDSignal
from smarttune.models.analysis_result import Assessment, ParamRef
from smarttune.analyzers.pid_reviewer import PIDReviewer, detect_steps


def _make_step_signal(
    duration_s: float = 10.0,
    sample_rate: float = 250.0,
    step_times: list = None,
    overshoot: float = 0.05,
    rise_time_s: float = 0.08,
    settling_time_s: float = 0.2,
) -> AxisPIDSignal:
    """Generate a synthetic PID signal with step responses."""
    if step_times is None:
        step_times = [2.0, 4.0, 6.0, 8.0]

    n = int(duration_s * sample_rate)
    dt = 1.0 / sample_rate
    t = np.arange(n) * dt

    desired = np.zeros(n)
    actual = np.zeros(n)

    # Non-cumulative steps: alternate between 0 and 5 deg/s
    for i, st in enumerate(step_times):
        idx = int(st * sample_rate)
        target_val = 5.0 if i % 2 == 0 else 0.0
        desired[idx:] = target_val

    # Simulate second-order response for actual
    for i, st in enumerate(step_times):
        idx = int(st * sample_rate)
        target_val = 5.0 if i % 2 == 0 else 0.0
        prev_val = 0.0 if i % 2 == 0 else 5.0
        step_amp = target_val - prev_val
        for j in range(idx, n):
            tau = (j - idx) * dt
            wn = 1.0 / rise_time_s * 1.8
            zeta = 0.5 if overshoot > 0.1 else 0.7
            wd = wn * np.sqrt(max(1e-6, 1 - zeta**2))
            env = np.exp(-zeta * wn * tau)
            resp = 1.0 - env * (
                np.cos(wd * tau) + (zeta / np.sqrt(max(1e-6, 1 - zeta**2))) * np.sin(wd * tau)
            )
            actual[j] = prev_val + step_amp * resp

    # Add some noise
    actual += np.random.randn(n) * 0.02

    return AxisPIDSignal(
        timestamp_s=t,
        desired=desired,
        actual=actual,
    )


class TestDetectSteps:
    def test_detects_steps(self):
        sig = _make_step_signal()
        steps = detect_steps(sig.desired, dt_ms=4.0)
        assert len(steps) == 4

    def test_empty_signal(self):
        steps = detect_steps(np.array([]))
        assert steps == []

    def test_no_steps(self):
        steps = detect_steps(np.ones(1000))
        assert steps == []


class TestPIDReviewer:
    def test_analyze_single_axis(self):
        sig = _make_step_signal(overshoot=0.05)
        fd = FlightData(
            platform="ardupilot",
            pid={"roll": sig},
        )
        reviewer = PIDReviewer()
        result = reviewer.analyze(fd, axis="roll")
        assert "roll" in result.axes
        ax = result.axes["roll"]
        assert ax.step_count == 4
        assert ax.metrics.rise_time_ms > 0
        assert ax.metrics.overshoot_percent >= 0

    def test_analyze_all_axes(self):
        sig = _make_step_signal()
        fd = FlightData(
            platform="betaflight",
            pid={"roll": sig, "pitch": sig, "yaw": sig},
        )
        reviewer = PIDReviewer()
        result = reviewer.analyze(fd)
        assert len(result.axes) == 3
        assert result.overall_assessment is not None

    def test_recommendations_use_paramref(self):
        """Verify recommendations use generic ParamRef, not platform-specific names."""
        # Create a signal with overshoot to trigger recommendations
        sig = _make_step_signal(overshoot=0.3, rise_time_s=0.03)
        fd = FlightData(
            platform="ardupilot",
            pid={"roll": sig},
            params={"pid.roll.p": 0.2, "pid.roll.i": 0.1, "pid.roll.d": 0.01},
        )
        reviewer = PIDReviewer()
        result = reviewer.analyze(fd, axis="roll")
        ax = result.axes["roll"]

        # Any recommendations should use ParamRef
        for rec in ax.recommendations:
            assert isinstance(rec.param, ParamRef)
            assert rec.param.generic_name.startswith("pid.roll.")
            assert rec.param.category == "pid"

    def test_empty_data(self):
        fd = FlightData(platform="ardupilot")
        reviewer = PIDReviewer()
        result = reviewer.analyze(fd)
        assert len(result.axes) == 0

    def test_knowledge_override(self):
        """Custom thresholds from knowledge base."""
        custom = {
            "thresholds": {
                "roll": {
                    "rise_time_ms": {"min": 10, "max": 50, "ideal": 30},
                    "overshoot_percent": {"min": 0, "max": 5, "ideal": 2},
                    "settling_time_ms": {"min": 50, "max": 150, "ideal": 80},
                    "oscillation_count": {"min": 0, "max": 1, "ideal": 0},
                    "ss_error_percent": {"min": 0, "max": 3, "ideal": 0},
                },
            }
        }
        sig = _make_step_signal()
        fd = FlightData(platform="ardupilot", pid={"roll": sig})
        reviewer = PIDReviewer(knowledge=custom)
        result = reviewer.analyze(fd, axis="roll")
        assert "roll" in result.axes


class TestParamMapIntegration:
    def test_param_translation(self):
        """End-to-end: generic ParamRef → platform-specific name."""
        from smarttune.platform.registry import get_adapter

        ref = ParamRef("pid.roll.p", axis="roll", category="pid")

        ap = get_adapter("ardupilot")
        assert ap.map_param_to_platform(ref.generic_name) == "ATC_RAT_RLL_P"

        bf = get_adapter("betaflight")
        assert bf.map_param_to_platform(ref.generic_name) == "p_roll"

        px4 = get_adapter("px4")
        assert px4.map_param_to_platform(ref.generic_name) == "MC_ROLLRATE_P"
