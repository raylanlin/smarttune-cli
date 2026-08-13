"""
tests/test_integration.py

End-to-end integration tests — synthetic data through the full analysis pipeline.
"""

import numpy as np
import pytest
from smarttune.models.flight_data import FlightData, AxisPIDSignal


def _make_full_flight_data(duration_s=10.0, sample_rate=250.0):
    """Create a realistic synthetic FlightData with PID + IMU + mag."""
    n_pid = int(duration_s * sample_rate)
    n_imu = int(duration_s * 400)  # IMU at 400Hz
    t_pid = np.linspace(0, duration_s, n_pid)
    t_imu = np.linspace(0, duration_s, n_imu)

    # PID signals with step responses
    step_times = [2.0, 4.0, 6.0, 8.0]
    pid = {}
    for axis in ["roll", "pitch", "yaw"]:
        desired = np.zeros(n_pid)
        actual = np.zeros(n_pid)
        for i, st in enumerate(step_times):
            idx = int(st * sample_rate)
            val = 5.0 if i % 2 == 0 else 0.0
            desired[idx:] = val
        # Simple first-order response
        tau = 0.05
        for i in range(1, n_pid):
            dt = t_pid[i] - t_pid[i - 1]
            alpha = dt / (tau + dt)
            actual[i] = actual[i - 1] + alpha * (desired[i] - actual[i - 1])
        actual += np.random.randn(n_pid) * 0.1

        pid[axis] = AxisPIDSignal(
            timestamp_s=t_pid,
            desired=desired,
            actual=actual,
        )

    # IMU data with vibration at 80Hz
    gyro = np.random.randn(n_imu, 3) * 0.5
    gyro[:, 0] += 2.0 * np.sin(2 * np.pi * 80 * t_imu)
    accel = np.random.randn(n_imu, 3) * 0.3
    accel[:, 2] += 9.81

    # Mag data
    n_mag = int(duration_s * 50)
    t_mag = np.linspace(0, duration_s, n_mag)
    mag = np.column_stack(
        [
            200 + np.random.randn(n_mag) * 5,
            -100 + np.random.randn(n_mag) * 5,
            300 + np.random.randn(n_mag) * 5,
        ]
    )

    return FlightData(
        platform="ardupilot",
        firmware_version="4.5.0",
        sample_rate_hz=sample_rate,
        duration_s=duration_s,
        pid=pid,
        gyro=gyro,
        accel=accel,
        imu_timestamp_s=t_imu,
        mag=mag,
        mag_timestamp_s=t_mag,
        params={
            "ATC_RAT_RLL_P": 0.135,
            "ATC_RAT_RLL_I": 0.135,
            "ATC_RAT_RLL_D": 0.0036,
            "ATC_RAT_PIT_P": 0.135,
            "ATC_RAT_PIT_I": 0.135,
            "ATC_RAT_PIT_D": 0.0036,
            "ATC_RAT_YAW_P": 0.180,
            "ATC_RAT_YAW_I": 0.018,
            "ATC_RAT_YAW_D": 0.0,
            "INS_GYRO_FILTER": 20.0,
            "INS_HNTCH_ENABLE": 1,
            "INS_HNTCH_FREQ": 80.0,
            "INS_HNTCH_BW": 40.0,
            "INS_HNTCH_ATT": 40.0,
            "pid.roll.p": 0.135,
            "pid.roll.i": 0.135,
            "pid.roll.d": 0.0036,
            "pid.pitch.p": 0.135,
            "pid.pitch.i": 0.135,
            "pid.pitch.d": 0.0036,
            "pid.yaw.p": 0.180,
            "pid.yaw.i": 0.018,
            "pid.yaw.d": 0.0,
        },
    )


class TestFullPipeline:
    """End-to-end tests through the full analysis pipeline."""

    def test_pid_analysis(self):
        from smarttune.analyzers.pid_reviewer import PIDReviewer

        fd = _make_full_flight_data()
        reviewer = PIDReviewer()
        result = reviewer.analyze(fd)
        assert len(result.axes) == 3
        for axis in ["roll", "pitch", "yaw"]:
            assert axis in result.axes
            assert result.axes[axis].step_count > 0

    def test_fft_analysis(self):
        from smarttune.analyzers.fft_analyzer import FFTAnalyzer

        fd = _make_full_flight_data()
        analyzer = FFTAnalyzer()
        result = analyzer.analyze(fd)
        assert result["vibration_value_mss"] > 0
        # Should detect the 80Hz vibration
        peaks = result.get("peak_frequencies", [])
        if peaks:
            freqs = [p["frequency_hz"] for p in peaks]
            assert any(abs(f - 80) < 15 for f in freqs)

    def test_sysid_analysis(self):
        from smarttune.analyzers.sysid_analyzer import SysIDAnalyzer

        fd = _make_full_flight_data()
        analyzer = SysIDAnalyzer(na=2, nb=1)
        results = analyzer.analyze(fd, axis="roll")
        assert "roll" in results
        assert results["roll"].natural_freq_hz > 0
        assert results["roll"].fit_quality_percent > 0

    def test_platform_param_translation(self):
        """Verify the full chain: analyze → ParamRef → platform name."""
        from smarttune.analyzers.pid_reviewer import PIDReviewer
        from smarttune.platform.registry import get_adapter

        fd = _make_full_flight_data()
        reviewer = PIDReviewer()
        result = reviewer.analyze(fd, axis="roll")

        # Translate recommendations to all platforms
        for platform_name in ["ardupilot", "betaflight", "px4"]:
            adapter = get_adapter(platform_name)
            for rec in result.axes["roll"].recommendations:
                native = adapter.map_param_to_platform(rec.param.generic_name)
                assert (
                    native != rec.param.generic_name or platform_name != "ardupilot"
                ), f"Failed to translate {rec.param.generic_name} for {platform_name}"

    def test_knowledge_layers(self):
        """Verify knowledge base loads common + platform rules."""
        from smarttune.knowledge import KnowledgeBase

        for platform in ["ardupilot", "betaflight"]:
            kb = KnowledgeBase(platform=platform)
            # Common rules
            assert "vibration_rules" in kb.rules
            # Platform rules
            assert "pid_rules" in kb.rules

    def test_validate_flight_data(self):
        fd = _make_full_flight_data()
        issues = fd.validate()
        assert len(issues) == 0  # Full data should pass

    def test_validate_empty(self):
        fd = FlightData(platform="ardupilot")
        issues = fd.validate()
        assert len(issues) > 0
