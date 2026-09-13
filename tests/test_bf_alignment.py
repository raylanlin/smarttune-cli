"""
tests/test_bf_alignment.py

Betaflight alignment regressions (v3.5.0).

Cross-checked against the firmware itself (betaflight `src/main/blackbox/
blackbox.c`) and the two reference consumers — `betaflight/blackbox-log-viewer`
and `Plasmatree/PID-Analyzer`. See docs/ALIGNMENT_BETAFLIGHT.md.

The two behaviours pinned here are the ones that were actually wrong:

1. `blackbox_high_resolution` (BF 4.4+) makes the firmware log gyroADC /
   gyroUnfilt / rcCommand / setpoint multiplied by 10 (blackbox.c:2313). We
   ignored it, so those signals read 10x too large — and the outlier sanitiser
   then "repaired" every sample above ~220 deg/s, destroying the trace.
2. The step response ran with **no window function** at all, while
   PID-Analyzer uses `np.hanning(flen)` (PID-Analyzer.py:64).
"""

from __future__ import annotations

import numpy as np
import pytest

from smarttune.platform.betaflight import BetaflightAdapter
from smarttune.platform.betaflight import step_response_fft as bf_step
from smarttune.platform.betaflight.bbl_parser import BBLColumnarSegment, BBLHeader, FRAME_TYPE_I

_N = 600
_LOOPTIME_US = 125  # 8 kHz


def _segment(high_resolution: bool, gyro_raw: float = 1000.0, with_unfilt: bool = True):
    """A synthetic columnar segment with flat, known field values."""
    props = {
        "looptime": str(_LOOPTIME_US),
        "rate_limits": "1998,1998,1998",
        "minthrottle": "1070",
        "maxthrottle": "2000",
        "acc_1G": "2048",
        "gyro_scale": "0x3f800000",  # 1.0f — what modern firmware writes
        "blackbox_high_resolution": "1" if high_resolution else "0",
        "p_roll": "45",
    }
    header = BBLHeader(
        product="Blackbox flight data recorder by Nicholas Sherlock",
        data_version=2,
        firmware_type="Betaflight",
        firmware_revision="Betaflight 4.5.1",
        board_info="MAMBAF722",
        craft_name="test",
        properties=props,
    )

    names = []
    cols = {}

    def put(name, value):
        names.append(name)
        cols[name] = np.full(_N, value, dtype=np.int64)

    for i in range(3):
        put(f"gyroADC[{i}]", gyro_raw)
        put(f"setpoint[{i}]", gyro_raw)
        put(f"axisP[{i}]", 50)
        put(f"axisI[{i}]", 10)
        put(f"axisD[{i}]", 5)
        put(f"axisF[{i}]", 20)
        put(f"rcCommand[{i}]", 200)
        if with_unfilt:
            put(f"gyroUnfilt[{i}]", gyro_raw)
    for i in range(4):
        put(f"motor[{i}]", 1500)
    put("accSmooth[0]", 0)
    put("accSmooth[1]", 0)
    put("accSmooth[2]", 2048)

    return BBLColumnarSegment(
        header=header,
        field_names=names,
        columns=cols,
        frame_types=np.full(_N, FRAME_TYPE_I, dtype=np.uint8),
        n_frames=_N,
        events=[],
    )


@pytest.fixture
def parse_with(monkeypatch, tmp_path):
    """Parse a fake .bbl by stubbing the columnar parser."""
    log = tmp_path / "flight.bbl"
    log.write_bytes(b"H Product:Blackbox flight data recorder by Nicholas Sherlock\n")

    def _run(segment):
        from smarttune.platform.betaflight import bbl_parser

        monkeypatch.setattr(
            bbl_parser, "parse_bbl_columnar", lambda *a, **k: [segment], raising=True
        )
        return BetaflightAdapter().parse(log)

    return _run


# ---------------------------------------------------------------------------
# blackbox_high_resolution
# ---------------------------------------------------------------------------


def test_high_resolution_signals_are_descaled(parse_with):
    """gyroADC 1000 with high resolution on means 100 deg/s, not 1000."""
    fd = parse_with(_segment(high_resolution=True, gyro_raw=1000.0))

    assert fd.extras["blackbox_info"]["high_resolution"] is True
    assert fd.extras["blackbox_info"]["high_resolution_scale"] == 10.0
    assert fd.gyro[0][0] == pytest.approx(100.0)
    assert fd.pid["roll"].actual[0] == pytest.approx(100.0)
    assert fd.pid["roll"].desired[0] == pytest.approx(100.0)


def test_high_resolution_off_leaves_values_alone(parse_with):
    fd = parse_with(_segment(high_resolution=False, gyro_raw=1000.0))

    assert fd.extras["blackbox_info"]["high_resolution"] is False
    assert fd.gyro[0][0] == pytest.approx(1000.0)
    assert fd.pid["roll"].actual[0] == pytest.approx(1000.0)


def test_high_resolution_no_longer_clipped_by_the_sanitiser(parse_with):
    """1500 deg/s logs as 15000 raw — the old path interpolated it away."""
    fd = parse_with(_segment(high_resolution=True, gyro_raw=15000.0))

    # descaled to 1500 deg/s, inside the 1998*1.1 rate limit → survives intact
    assert fd.pid["roll"].actual[0] == pytest.approx(1500.0)
    assert fd.gyro[0][0] == pytest.approx(1500.0)
    assert float(np.std(fd.gyro[:, 0])) == pytest.approx(0.0, abs=1e-9)


def test_pid_terms_are_not_descaled(parse_with):
    """Firmware scales only gyro/rcCommand/setpoint — PID terms stay raw."""
    fd = parse_with(_segment(high_resolution=True))

    assert fd.pid["roll"].p_term[0] == pytest.approx(50.0)
    assert fd.pid["roll"].d_term[0] == pytest.approx(5.0)


def test_unfiltered_gyro_is_exposed_and_descaled(parse_with):
    fd = parse_with(_segment(high_resolution=True, gyro_raw=1000.0))

    assert fd.extras["blackbox_info"]["has_unfiltered_gyro"] is True
    unfilt = fd.extras["gyro_unfiltered"]
    assert unfilt.shape == (_N, 3)
    assert unfilt[0][0] == pytest.approx(100.0)
    # FlightData.gyro stays the filtered trace (assessment calibration)
    assert fd.extras["blackbox_info"]["gyro_source"] == "gyroADC (filtered)"


def test_missing_unfiltered_gyro_is_reported(parse_with):
    fd = parse_with(_segment(high_resolution=False, with_unfilt=False))

    assert fd.extras["blackbox_info"]["has_unfiltered_gyro"] is False
    assert "gyro_unfiltered" not in fd.extras


def test_gyro_scale_header_is_recorded(parse_with):
    """Firmware writes gyro_scale = 1.0f, so raw gyroADC already is deg/s."""
    fd = parse_with(_segment(high_resolution=False))

    assert fd.extras["blackbox_info"]["gyro_scale_header"] == "0x3f800000"


# ---------------------------------------------------------------------------
# Step response — PID-Analyzer alignment
# ---------------------------------------------------------------------------


def _ramp_signals(sample_rate=1000.0, seconds=6.0, amplitude=200.0):
    """Square-wave setpoint with a first-order-ish gyro response."""
    n = int(sample_rate * seconds)
    t = np.arange(n) / sample_rate
    sp = amplitude * np.sign(np.sin(2 * np.pi * 0.8 * t))
    gy = np.zeros(n)
    tau = 0.05
    alpha = 1.0 / (tau * sample_rate)
    for i in range(1, n):
        gy[i] = gy[i - 1] + alpha * (sp[i] - gy[i - 1])
    return sp, gy


def test_step_response_uses_the_shared_windowed_kernel():
    sp, gy = _ramp_signals()
    out = bf_step.estimate_step_response(sp, gy, sample_rate=1000.0)

    assert out["method"] == "pid_analyzer_wiener"
    assert out["valid_windows"] > 0
    assert out["input_window_deg_s"] == [20.0, 500.0]
    # 0.5 s response window at 1 kHz
    assert len(out["step_response"]) == pytest.approx(500, abs=1)
    # a first-order response settles near 1.0
    assert out["steady_state"] == pytest.approx(1.0, abs=0.35)
    assert out["steady_state_ok"] is True


def test_high_input_windows_are_excluded():
    """PID-Analyzer splits at 500 deg/s; we keep the low-input response."""
    sp, gy = _ramp_signals(amplitude=900.0)
    out = bf_step.estimate_step_response(sp, gy, sample_rate=1000.0)

    assert out["valid_windows"] == 0
    assert out.get("error")


def test_below_min_input_windows_are_excluded():
    sp, gy = _ramp_signals(amplitude=5.0)
    out = bf_step.estimate_step_response(sp, gy, sample_rate=1000.0)

    assert out["valid_windows"] == 0


def test_compute_for_axis_defaults_to_gyro_adc():
    sp, gy = _ramp_signals()
    t = np.arange(len(sp)) / 1000.0
    pid_data = {"Desired": sp, "Actual": gy, "time": t}
    imu = {"time": t, "GyrX": gy * 2, "GyrY": gy, "GyrZ": gy}

    out = bf_step.compute_step_response_for_axis(pid_data, "roll", imu_data=imu)
    assert out["info"]["output_signal"] == "gyro_adc"

    out_imu = bf_step.compute_step_response_for_axis(
        pid_data, "roll", imu_data=imu, prefer_imu=True
    )
    assert out_imu["info"]["output_signal"] == "imu_gyro"


def test_shared_kernel_max_gate_is_opt_in():
    """ArduPilot must keep WebTools behaviour: no upper amplitude gate."""
    from smarttune.platform.ardupilot.step_response_fft import estimate_step_response

    sp, gy = _ramp_signals(amplitude=900.0)
    out = estimate_step_response(sp, gy, sample_rate=1000.0)
    assert out["valid_windows"] > 0
