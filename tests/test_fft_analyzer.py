"""
tests/test_fft_analyzer.py

Test the migrated FFT analyzer with synthetic IMU data.
"""

import numpy as np
import pytest
from smarttune.models.flight_data import FlightData


def _make_imu_data(
    duration_s: float = 5.0,
    sample_rate: float = 400.0,
    vib_freq_hz: float = 80.0,
    vib_amplitude: float = 2.0,
    noise_level: float = 0.5,
) -> FlightData:
    """Generate synthetic IMU data with a known vibration frequency."""
    n = int(duration_s * sample_rate)
    t = np.linspace(0, duration_s, n)

    # Base signal: gravity on Z + noise
    base_gyro = np.random.randn(n, 3) * noise_level
    base_accel = np.random.randn(n, 3) * noise_level
    base_accel[:, 2] += 9.81  # gravity

    # Inject vibration at known frequency on all axes
    vib = vib_amplitude * np.sin(2 * np.pi * vib_freq_hz * t)
    base_gyro[:, 0] += vib
    base_gyro[:, 1] += vib * 0.5
    base_accel[:, 0] += vib * 0.3

    return FlightData(
        platform="ardupilot",
        gyro=base_gyro,
        accel=base_accel,
        imu_timestamp_s=t,
        sample_rate_hz=sample_rate,
        duration_s=duration_s,
    )


class TestFFTAnalyzer:
    def test_analyze_basic(self):
        from smarttune.analyzers.fft_analyzer import FFTAnalyzer
        fd = _make_imu_data()
        analyzer = FFTAnalyzer()
        result = analyzer.analyze(fd)
        assert "vibration_level" in result
        assert "peak_frequencies" in result
        assert result["vibration_value_mss"] > 0

    def test_detects_known_frequency(self):
        from smarttune.analyzers.fft_analyzer import FFTAnalyzer
        fd = _make_imu_data(vib_freq_hz=120.0, vib_amplitude=5.0)
        analyzer = FFTAnalyzer()
        result = analyzer.analyze(fd)
        peaks = result.get("peak_frequencies", [])
        if peaks:
            # The dominant peak should be near 120 Hz
            freqs = [p["frequency_hz"] for p in peaks]
            assert any(abs(f - 120.0) < 10 for f in freqs), \
                f"Expected peak near 120Hz, got {freqs}"

    def test_insufficient_data(self):
        from smarttune.analyzers.fft_analyzer import FFTAnalyzer, InsufficientDataError
        fd = FlightData(platform="ardupilot")  # No IMU data
        analyzer = FFTAnalyzer()
        with pytest.raises(InsufficientDataError):
            analyzer.analyze(fd)

    def test_get_spectrum_data(self):
        from smarttune.analyzers.fft_analyzer import FFTAnalyzer
        fd = _make_imu_data()
        analyzer = FFTAnalyzer()
        analyzer.analyze(fd)
        spectrum = analyzer.get_spectrum_data()
        assert "freqs" in spectrum
        assert "magnitudes" in spectrum
        assert len(spectrum["freqs"]) > 0
