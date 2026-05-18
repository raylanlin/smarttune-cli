"""
tests/test_services_analysis.py

Tests for the smarttune.services.analysis module.

Uses synthetic FlightData fixtures rather than real log files.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from smarttune.errors import SmartTuneError
from smarttune.models.analysis_result import (
    Assessment,
    AxisPIDResult,
    FFTAnalysisResult,
    MagFitResult,
    PIDAnalysisResult,
    StepMetrics,
    VibrationPeak,
)
from smarttune.models.flight_data import AxisPIDSignal, FlightData


def _make_synthetic_flight_data(platform: str = "ardupilot") -> FlightData:
    """Create a minimal synthetic FlightData for testing."""
    n = 2000
    t = np.linspace(0, 10, n)
    desired = np.sin(2 * np.pi * 0.5 * t) * 50
    actual = desired + np.random.randn(n) * 2

    pid_signal = AxisPIDSignal(
        timestamp_s=t,
        desired=desired,
        actual=actual,
        p_term=np.random.randn(n) * 0.1,
        i_term=np.random.randn(n) * 0.01,
        d_term=np.random.randn(n) * 0.05,
    )

    return FlightData(
        platform=platform,
        firmware_version="4.5.0",
        sample_rate_hz=200.0,
        duration_s=10.0,
        pid={"roll": pid_signal, "pitch": pid_signal, "yaw": pid_signal},
        gyro=np.random.randn(n, 3),
        accel=np.random.randn(n, 3) + np.array([0, 0, 9.81]),
        imu_timestamp_s=t,
        mag=np.random.randn(n, 3) * 300,
        mag_timestamp_s=t,
        motor_output=np.random.rand(n, 4),
        motor_timestamp_s=t,
        battery_voltage=np.ones(n) * 12.6,
        battery_current=np.ones(n) * 5.0,
        battery_timestamp_s=t,
    )


class TestGetLogQuality:
    """Test the get_log_quality service function."""

    @patch("smarttune.services.analysis.load_flight_data")
    def test_returns_expected_keys(self, mock_load, tmp_path):
        fd = _make_synthetic_flight_data()
        mock_adapter = MagicMock()
        mock_adapter.name = "ardupilot"
        mock_adapter.display_name = "ArduPilot"
        mock_load.return_value = (mock_adapter, fd)

        log_file = tmp_path / "test.bin"
        log_file.write_bytes(b"\x00" * 1024)

        from smarttune.services.analysis import get_log_quality
        result = get_log_quality(log_file)

        assert "platform" in result
        assert "quality" in result
        assert "score" in result["quality"]
        assert "rating" in result["quality"]
        assert result["platform"] == "ardupilot"
        assert result["has_gyro"] is True
        assert result["has_mag"] is True

    @patch("smarttune.services.analysis.load_flight_data")
    def test_poor_quality_for_empty_data(self, mock_load, tmp_path):
        fd = FlightData(platform="ardupilot")
        mock_adapter = MagicMock()
        mock_adapter.name = "ardupilot"
        mock_adapter.display_name = "ArduPilot"
        mock_load.return_value = (mock_adapter, fd)

        log_file = tmp_path / "empty.bin"
        log_file.write_bytes(b"\x00" * 512)

        from smarttune.services.analysis import get_log_quality
        result = get_log_quality(log_file)

        assert result["quality"]["score"] < 50
        assert result["quality"]["rating"] == "POOR"


class TestAnalyzeLog:
    """Test the analyze_log service function."""

    @patch("smarttune.services.analysis.load_flight_data")
    def test_returns_expected_structure(self, mock_load, tmp_path):
        fd = _make_synthetic_flight_data()
        mock_adapter = MagicMock()
        mock_adapter.name = "ardupilot"
        mock_adapter.display_name = "ArduPilot"
        mock_adapter.capabilities.return_value = {"pid", "fft", "magfit", "hardware"}
        mock_adapter.map_param_to_platform.side_effect = lambda g: g
        mock_load.return_value = (mock_adapter, fd)

        # Mock the analyzers to return simple results
        mock_pid = PIDAnalysisResult(
            overall_assessment=Assessment.GOOD,
            axes={
                "roll": AxisPIDResult(
                    axis="roll",
                    metrics=StepMetrics(rise_time_ms=85.0),
                    assessment=Assessment.GOOD,
                    step_count=5,
                ),
            },
        )
        mock_fft = FFTAnalysisResult(
            vibration_level=Assessment.GOOD,
            peaks=[VibrationPeak(frequency_hz=47.5, amplitude=2.1, source_guess="propeller")],
        )
        mock_mag = MagFitResult(
            assessment=Assessment.GOOD,
            fitness_mgauss=23.4,
            offsets={"x": 1.0, "y": -3.0, "z": 5.0},
        )

        log_file = tmp_path / "test.bin"
        log_file.write_bytes(b"\x00" * 1024)

        with patch("smarttune.services.analysis.KnowledgeBase") as mock_kb:
            mock_kb.return_value.get.return_value = {}
            with patch("smarttune.analyzers.pid_reviewer.PIDReviewer") as MockPID:
                MockPID.return_value.analyze.return_value = mock_pid
                with patch("smarttune.analyzers.fft_analyzer.FFTAnalyzer") as MockFFT:
                    MockFFT.return_value.analyze.return_value = mock_fft
                    with patch("smarttune.analyzers.magfit.MAGFit") as MockMag:
                        MockMag.return_value.analyze.return_value = mock_mag
                        with patch("smarttune.analyzers.hardware_report.generate_hardware_report") as MockHW:
                            MockHW.return_value = {
                                "imu_configs": [],
                                "compass_configs": [],
                                "filter_config": {},
                                "pid_params": {},
                                "sys_info": {
                                    "board_name": "CubeOrange",
                                    "board_id": 140,
                                    "sched_loop_rate": 400,
                                },
                                "version_info": {"firmware": "4.5.0"},
                                "battery_reports": [],
                                "integrity_issues": [],
                                "total_params": 0,
                            }

                            from smarttune.services.analysis import analyze_log
                            result = analyze_log(log_file)

        assert result["platform"] == "ardupilot"
        assert "modules" in result
        assert "pid" in result["modules"]
        assert "fft" in result["modules"]
        assert "magfit" in result["modules"]
        assert "hardware" in result["modules"]
        assert result["modules"]["hardware"]["sys_info"]["board_name"] == "CubeOrange"
        assert result["safety"]["read_only"] is True
        assert result["safety"]["parameter_write_performed"] is False

        # Verify JSON serializable
        serialized = json.dumps(result)
        assert isinstance(serialized, str)

    @patch("smarttune.services.analysis.load_flight_data")
    def test_module_failure_recorded(self, mock_load, tmp_path):
        """When a module raises, it should appear in module_failures, not crash."""
        fd = _make_synthetic_flight_data()
        mock_adapter = MagicMock()
        mock_adapter.name = "ardupilot"
        mock_adapter.display_name = "ArduPilot"
        mock_adapter.capabilities.return_value = {"pid", "fft"}
        mock_adapter.map_param_to_platform.side_effect = lambda g: g
        mock_load.return_value = (mock_adapter, fd)

        mock_fft = FFTAnalysisResult(vibration_level=Assessment.GOOD)

        log_file = tmp_path / "test.bin"
        log_file.write_bytes(b"\x00" * 1024)

        with patch("smarttune.services.analysis.KnowledgeBase") as mock_kb:
            mock_kb.return_value.get.return_value = {}
            with patch("smarttune.analyzers.pid_reviewer.PIDReviewer") as MockPID:
                MockPID.return_value.analyze.side_effect = RuntimeError("PID boom")
                with patch("smarttune.analyzers.fft_analyzer.FFTAnalyzer") as MockFFT:
                    MockFFT.return_value.analyze.return_value = mock_fft

                    from smarttune.services.analysis import analyze_log
                    result = analyze_log(log_file, include_modules=["pid", "fft"])

        assert "pid" not in result["modules"]
        assert "fft" in result["modules"]
        assert len(result["module_failures"]) == 1
        assert result["module_failures"][0]["module"] == "pid"

    @patch("smarttune.services.analysis.load_flight_data")
    def test_all_modules_fail_raises(self, mock_load, tmp_path):
        """When every module fails, analyze_log should raise SmartTuneError."""
        fd = _make_synthetic_flight_data()
        mock_adapter = MagicMock()
        mock_adapter.name = "ardupilot"
        mock_adapter.display_name = "ArduPilot"
        mock_adapter.capabilities.return_value = {"pid"}
        mock_load.return_value = (mock_adapter, fd)

        log_file = tmp_path / "test.bin"
        log_file.write_bytes(b"\x00" * 1024)

        with patch("smarttune.services.analysis.KnowledgeBase") as mock_kb:
            mock_kb.return_value.get.return_value = {}
            with patch("smarttune.analyzers.pid_reviewer.PIDReviewer") as MockPID:
                MockPID.return_value.analyze.side_effect = RuntimeError("PID boom")

                from smarttune.services.analysis import analyze_log
                with pytest.raises(SmartTuneError, match="All requested analysis modules failed"):
                    analyze_log(log_file, include_modules=["pid"])
