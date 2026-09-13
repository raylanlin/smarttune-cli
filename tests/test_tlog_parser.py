"""
tests/test_tlog_parser.py

MAVLink telemetry log (.tlog) adapter tests.

The risk in this feature is SmartTune's own mapping layer — unit conversions,
clock reconciliation, vehicle-vs-GCS heartbeat handling, desired-vs-actual
reconstruction, and the honesty of what it reports as missing — not pymavlink's
frame decoder. So the parser is driven with stub MAVLink messages through a
fake connection, while the structural sniff (`looks_like_tlog`) is tested
against real handcrafted bytes.

Several cases pin behaviour that was cross-checked against ArduPilot's own
UAVLogViewer (mavlinkParser.js / modeMaps.js / mavlinkDataExtractor.js):
GCS heartbeats are not the vehicle, mode maps are keyed by MAV_TYPE, the
vehicle clock (time_boot_ms) is the timeline, and param ids are sanitised the
same way.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from smarttune.errors import LogFormatError
from smarttune.platform.ardupilot import ArduPilotAdapter
from smarttune.platform.ardupilot import tlog_parser as tp

RAD2DEG = 180.0 / np.pi

#: wall clock (GCS) minus vehicle clock in the synthetic streams below
CLOCK_OFFSET = 990.0
BOOT0 = 10.0  # first vehicle timestamp, seconds since boot


class _Header:
    def __init__(self, seq: int):
        self.seq = seq


class _Msg:
    """Minimal stand-in for a pymavlink message object.

    ``sysid``/``compid``/``seq`` are optional so most tests stay terse; the
    system-filtering and drop-accounting tests set them explicitly.
    """

    def __init__(self, mtype: str, timestamp: float, sysid=1, compid=1, seq=None, **fields):
        self._type = mtype
        self._timestamp = timestamp
        self._sysid = sysid
        self._compid = compid
        if seq is not None:
            self._header = _Header(seq)
        for key, value in fields.items():
            setattr(self, key, value)

    def get_type(self) -> str:
        return self._type

    def get_srcSystem(self) -> int:
        return self._sysid

    def get_srcComponent(self) -> int:
        return self._compid


class _FakeLog:
    def __init__(self, messages):
        self._messages = list(messages)

    def recv_match(self, *args, **kwargs):
        return self._messages.pop(0) if self._messages else None


@pytest.fixture
def feed(monkeypatch):
    """Install a message stream that parse_tlog() will consume."""

    def _install(messages):
        from pymavlink import mavutil

        monkeypatch.setattr(
            mavutil, "mavlink_connection", lambda *a, **k: _FakeLog(messages), raising=True
        )

    return _install


def _wall(boot: float) -> float:
    return boot + CLOCK_OFFSET


def _heartbeat(boot, autopilot=3, custom_mode=0, mav_type=2, base_mode=0b10000000, seq=None):
    return _Msg(
        "HEARTBEAT",
        _wall(boot),
        seq=seq,
        autopilot=autopilot,
        type=mav_type,
        custom_mode=custom_mode,
        base_mode=base_mode,
    )


def _gcs_heartbeat(boot, custom_mode=99):
    """Mission Planner's own heartbeat — MAV_TYPE_GCS, invalid autopilot."""
    return _Msg("HEARTBEAT", _wall(boot), autopilot=8, type=6, custom_mode=custom_mode, base_mode=0)


def _attitude(boot, rollspeed=0.5, pitchspeed=-0.25, yawspeed=0.1):
    return _Msg(
        "ATTITUDE",
        _wall(boot),
        time_boot_ms=boot * 1000.0,
        roll=0.1,
        pitch=-0.05,
        yaw=1.0,
        rollspeed=rollspeed,
        pitchspeed=pitchspeed,
        yawspeed=yawspeed,
    )


def _target(boot, roll_rate=0.6, pitch_rate=-0.3, yaw_rate=0.2):
    return _Msg(
        "ATTITUDE_TARGET",
        _wall(boot),
        time_boot_ms=boot * 1000.0,
        body_roll_rate=roll_rate,
        body_pitch_rate=pitch_rate,
        body_yaw_rate=yaw_rate,
    )


def _raw_imu(boot):
    # MAVLink units: xacc mG, xgyro mrad/s, xmag mgauss
    return _Msg(
        "RAW_IMU",
        _wall(boot),
        time_usec=boot * 1e6,
        xacc=100,
        yacc=-50,
        zacc=-1000,
        xgyro=500,
        ygyro=-250,
        zgyro=100,
        xmag=180,
        ymag=-60,
        zmag=400,
    )


def _stream(n=40, dt=0.1, with_target=True, with_imu=True, autopilot=3, mav_type=2):
    msgs = [
        _heartbeat(BOOT0, autopilot=autopilot, mav_type=mav_type),
        _Msg(
            "AUTOPILOT_VERSION",
            _wall(BOOT0),
            flight_sw_version=(4 << 24) | (5 << 16) | (7 << 8),
        ),
    ]
    for i in range(n):
        boot = BOOT0 + i * dt
        msgs.append(_attitude(boot))
        if with_target:
            msgs.append(_target(boot + dt / 4))
        if with_imu:
            msgs.append(_raw_imu(boot))
    msgs += [
        # wall-clock-only messages: placed on the vehicle clock via the offset
        _Msg("SYS_STATUS", _wall(BOOT0 + 1.0), voltage_battery=16800, current_battery=1250),
        _Msg("SYS_STATUS", _wall(BOOT0 + 2.0), voltage_battery=16500, current_battery=1300),
        _Msg(
            "GPS_RAW_INT",
            _wall(BOOT0 + 1.0),
            time_usec=(BOOT0 + 1.0) * 1e6,
            fix_type=3,
            lat=473977418,
            lon=85455938,
            alt=500000,
            satellites_visible=14,
            eph=80,
        ),
        _Msg("PARAM_VALUE", _wall(BOOT0 + 0.5), param_id="ATC_RAT_RLL_P", param_value=0.135),
        _Msg("PARAM_VALUE", _wall(BOOT0 + 0.5), param_id="INS_GYRO_FILTER", param_value=20.0),
        _Msg("STATUSTEXT", _wall(BOOT0 + 1.5), severity=6, text="EKF3 IMU0 is using GPS"),
        _Msg(
            "SERVO_OUTPUT_RAW",
            _wall(BOOT0 + 1.0),
            time_usec=(BOOT0 + 1.0) * 1e6,
            **{f"servo{i}_raw": 1500 for i in range(1, 9)},
        ),
        _Msg(
            "SERVO_OUTPUT_RAW",
            _wall(BOOT0 + 1.1),
            time_usec=(BOOT0 + 1.1) * 1e6,
            **{f"servo{i}_raw": 1600 for i in range(1, 9)},
        ),
        _Msg("VFR_HUD", _wall(BOOT0 + 1.0), throttle=45, alt=12.5, climb=0.3),
        _Msg("BAD_DATA", _wall(BOOT0 + 1.0)),
    ]
    return msgs


@pytest.fixture
def tlog_file(tmp_path):
    p = tmp_path / "2026-09-13 10-00-00.tlog"
    p.write_bytes(struct.pack(">Q", 1_757_000_000_000_000) + b"\xfd" + b"\x00" * 32)
    return p


# ---------------------------------------------------------------------------
# Structural detection
# ---------------------------------------------------------------------------


def test_looks_like_tlog_accepts_timestamped_mavlink_frame(tlog_file):
    assert tp.looks_like_tlog(tlog_file) is True


def test_looks_like_tlog_rejects_implausible_timestamp(tmp_path):
    bogus = tmp_path / "b.tlog"
    bogus.write_bytes(struct.pack(">Q", 42) + b"\xfe" + b"\x00" * 32)
    assert tp.looks_like_tlog(bogus) is False


def test_looks_like_tlog_rejects_dataflash_bin(tmp_path):
    binlog = tmp_path / "flight.bin"
    binlog.write_bytes(b"\xa3\x95\x80" + b"\x00" * 64)
    assert tp.looks_like_tlog(binlog) is False


def test_adapter_detects_tlog_and_advertises_extension(tlog_file):
    assert ".tlog" in ArduPilotAdapter().supported_extensions
    assert ArduPilotAdapter.detect(tlog_file) is True


def test_registry_auto_detects_tlog_as_ardupilot(tlog_file):
    from smarttune.platform.registry import detect_platform

    assert detect_platform(tlog_file).name == "ardupilot"


# ---------------------------------------------------------------------------
# Clock reconciliation (UAVLogViewer plots against time_boot_ms)
# ---------------------------------------------------------------------------


def test_vehicle_clock_is_the_timeline(feed, tlog_file):
    feed(_stream(n=20, dt=0.1))
    fd = tp.parse_tlog(tlog_file)

    src = fd.extras["log_source"]
    assert src["clock"] == "vehicle_boot"
    assert src["clock_offset_s"] == pytest.approx(CLOCK_OFFSET, abs=0.05)
    # timeline starts at zero and spans the vehicle-clock range, not wall clock
    assert fd.pid["roll"].timestamp_s[0] == pytest.approx(0.0, abs=1e-6)
    assert fd.duration_s == pytest.approx(2.0, abs=0.3)


def test_wall_clock_only_messages_are_placed_on_the_vehicle_timeline(feed, tlog_file):
    feed(_stream(n=40, dt=0.1))
    fd = tp.parse_tlog(tlog_file)

    # SYS_STATUS carries no vehicle timestamp; it was sent at boot+1.0 / +2.0
    assert fd.battery_timestamp_s.tolist() == pytest.approx([1.0, 2.0], abs=0.05)


def test_falls_back_to_gcs_clock_when_no_vehicle_timestamp(feed, tlog_file):
    msgs = [
        _Msg("HEARTBEAT", _wall(BOOT0), autopilot=3, type=2, custom_mode=0, base_mode=128),
        *[
            _Msg(
                "ATTITUDE",
                _wall(BOOT0 + i * 0.1),
                roll=0.0,
                pitch=0.0,
                yaw=0.0,
                rollspeed=0.3,
                pitchspeed=0.0,
                yawspeed=0.0,
            )
            for i in range(20)
        ],
    ]
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    assert fd.extras["log_source"]["clock"] == "gcs_wall"
    assert "receive clock" in " ".join(fd.extras["telemetry_notes"])
    assert fd.duration_s == pytest.approx(1.9, abs=0.05)


# ---------------------------------------------------------------------------
# Heartbeats: vehicle vs ground station
# ---------------------------------------------------------------------------


def test_gcs_heartbeats_are_ignored(feed, tlog_file):
    msgs = _stream(n=12)
    # Mission Planner interleaves its own heartbeats with custom_mode 99
    msgs.insert(1, _gcs_heartbeat(BOOT0 + 0.05))
    msgs.insert(4, _gcs_heartbeat(BOOT0 + 0.15))
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    assert fd.extras["log_source"]["gcs_heartbeats_ignored"] == 2
    assert fd.extras["log_source"]["mav_type"] == 2  # quad, not GCS
    assert fd.frame_type == "quad"
    # GCS custom_mode 99 must not appear as a flight mode
    assert [mc.raw_mode for mc in fd.mode_changes] == ["STABILIZE"]


def test_gcs_heartbeat_first_does_not_poison_autopilot_detection(feed, tlog_file):
    msgs = _stream(n=12)
    msgs.insert(0, _gcs_heartbeat(BOOT0 - 0.1))
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    assert fd.extras["log_source"]["autopilot"] == 3  # ArduPilot, from the vehicle


def test_armed_events_are_tracked(feed, tlog_file):
    msgs = _stream(n=12)
    msgs.insert(1, _heartbeat(BOOT0 + 0.2, base_mode=0))  # disarmed
    msgs.insert(2, _heartbeat(BOOT0 + 0.4, base_mode=0b10000000))  # armed again
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    states = [e["armed"] for e in fd.extras["armed_events"]]
    assert states == [True, False, True]
    assert fd.extras["armed_events"][1]["time"] == pytest.approx(0.2, abs=0.05)


# ---------------------------------------------------------------------------
# Mode maps keyed by MAV_TYPE (modeMaps.js)
# ---------------------------------------------------------------------------


def test_copter_mode_map(feed, tlog_file):
    msgs = _stream(n=12)
    msgs.insert(1, _heartbeat(BOOT0 + 0.2, custom_mode=2))  # ALT_HOLD
    msgs.insert(2, _heartbeat(BOOT0 + 0.4, custom_mode=5))  # LOITER
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    assert [mc.raw_mode for mc in fd.mode_changes][:3] == ["STABILIZE", "ALT_HOLD", "LOITER"]
    # canonical names match the DataFlash path's vocabulary
    assert [mc.mode_name for mc in fd.mode_changes][:3] == ["stabilize", "althold", "loiter"]


def test_plane_mode_map_is_used_for_fixed_wing(feed, tlog_file):
    msgs = _stream(n=12, mav_type=1)
    msgs.insert(1, _heartbeat(BOOT0 + 0.2, custom_mode=5, mav_type=1))  # FBWA on plane
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    # custom_mode 5 is LOITER on Copter but FBWA on Plane — the map must follow MAV_TYPE
    assert [mc.raw_mode for mc in fd.mode_changes][:2] == ["MANUAL", "FBWA"]
    assert fd.frame_type == "fixedwing"
    assert "MAV_TYPE 1" in " ".join(fd.extras["telemetry_notes"])


def test_base_mode_fallback_for_unmapped_vehicle_type(feed, tlog_file):
    msgs = _stream(n=12, mav_type=29)  # dodecarotor: no mode table
    msgs.insert(1, _heartbeat(BOOT0 + 0.2, custom_mode=77, mav_type=29, base_mode=0b10000100))
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    assert "AUTO" in [mc.raw_mode for mc in fd.mode_changes]


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def test_pid_reconstructed_from_target_vs_attitude(feed, tlog_file):
    feed(_stream())
    fd = tp.parse_tlog(tlog_file, {"pid.roll.p": "ATC_RAT_RLL_P"})

    assert sorted(fd.pid) == ["pitch", "roll", "yaw"]
    roll = fd.pid["roll"]
    assert roll.actual[5] == pytest.approx(0.5 * RAD2DEG, rel=1e-6)
    assert roll.desired[5] == pytest.approx(0.6 * RAD2DEG, rel=1e-3)
    # P/I/D terms do not exist in telemetry — zero-filled, never invented
    assert np.all(roll.p_term == 0) and np.all(roll.d_term == 0)
    assert roll.sample_count == 40


def test_no_attitude_target_means_no_pid_and_a_stated_reason(feed, tlog_file):
    feed(_stream(with_target=False))
    fd = tp.parse_tlog(tlog_file)

    assert fd.pid == {}
    notes = " ".join(fd.extras["telemetry_notes"])
    assert "ATTITUDE_TARGET" in notes
    assert ".bin" in notes


def test_gyro_accel_mag_units(feed, tlog_file):
    feed(_stream())
    fd = tp.parse_tlog(tlog_file)

    assert fd.gyro[0][0] == pytest.approx(0.5 * RAD2DEG, rel=1e-6)  # 500 mrad/s
    assert fd.accel[0][0] == pytest.approx(100 * 9.80665 / 1000.0, rel=1e-6)  # 100 mG
    assert fd.mag[0].tolist() == [180.0, -60.0, 400.0]  # mgauss passthrough
    assert fd.extras["log_source"]["gyro_source"] == "RAW_IMU"


def test_attitude_rates_are_the_gyro_fallback(feed, tlog_file):
    feed(_stream(with_imu=False))
    fd = tp.parse_tlog(tlog_file)

    assert fd.gyro is not None
    assert fd.extras["log_source"]["gyro_source"] == "ATTITUDE"
    assert fd.accel is None
    notes = " ".join(fd.extras["telemetry_notes"])
    assert "EKF" in notes and "accelerometer" in notes.lower()


def test_low_rate_triggers_fft_band_warning(feed, tlog_file):
    feed(_stream(dt=0.1))  # 10 Hz
    fd = tp.parse_tlog(tlog_file)

    assert fd.sample_rate_hz == pytest.approx(10.0, abs=0.5)
    notes = " ".join(fd.extras["telemetry_notes"])
    assert "FFT is limited" in notes and "resonance" in notes


def test_epoch_time_usec_is_not_mistaken_for_uptime(feed, tlog_file):
    """GPS_RAW_INT may carry a UNIX epoch in time_usec — it is not boot time."""
    msgs = _stream(n=12)
    msgs.append(
        _Msg(
            "GPS_RAW_INT",
            _wall(BOOT0 + 3.0),
            time_usec=1_757_000_000_000_000,  # epoch µs
            fix_type=3,
            lat=473977418,
            lon=85455938,
            alt=500000,
            satellites_visible=12,
            eph=90,
        )
    )
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    # a 55-year duration would mean the epoch stamp leaked into the timeline
    assert fd.duration_s < 60


# ---------------------------------------------------------------------------
# Parameters / metadata
# ---------------------------------------------------------------------------


def test_params_mirror_generic_keys(feed, tlog_file):
    feed(_stream())
    fd = tp.parse_tlog(
        tlog_file, {"pid.roll.p": "ATC_RAT_RLL_P", "filter.gyro_lpf": "INS_GYRO_FILTER"}
    )

    assert fd.params["ATC_RAT_RLL_P"] == pytest.approx(0.135)
    assert fd.params["pid.roll.p"] == pytest.approx(0.135)
    assert fd.params["filter.gyro_lpf"] == pytest.approx(20.0)


def test_param_ids_are_sanitised_like_the_webtool(feed, tlog_file):
    msgs = _stream(n=12)
    msgs.append(
        _Msg("PARAM_VALUE", _wall(BOOT0 + 0.6), param_id=b"ATC_RAT_PIT_P\x00\x03", param_value=0.14)
    )
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    assert "ATC_RAT_PIT_P" in fd.params
    assert fd.params["ATC_RAT_PIT_P"] == pytest.approx(0.14)


def test_battery_motors_gps_statustext_and_metadata(feed, tlog_file):
    feed(_stream())
    fd = tp.parse_tlog(tlog_file)

    assert fd.battery_voltage.tolist() == pytest.approx([16.8, 16.5])
    assert fd.battery_current.tolist() == pytest.approx([12.5, 13.0])
    assert fd.motor_output.shape[1] == 8
    assert fd.motor_output[0][0] == pytest.approx(0.5)
    assert fd.extras["gps_position"]["lat"] == pytest.approx(47.3977418)
    assert fd.extras["msg_log"][0]["text"] == "EKF3 IMU0 is using GPS"
    assert fd.extras["msg_log"][0]["severity"] == 6
    assert fd.firmware_version == "4.5.7"
    assert fd.frame_type == "quad"
    assert fd.platform == "ardupilot"


def test_ahrs2_is_a_position_fallback(feed, tlog_file):
    msgs = [m for m in _stream(n=12) if m.get_type() != "GPS_RAW_INT"]
    msgs.append(
        _Msg(
            "AHRS2",
            _wall(BOOT0 + 1.0),
            lat=473977000,
            lng=85455000,
            altitude=48.5,
        )
    )
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    assert fd.extras["gps_position"]["lat"] == pytest.approx(47.3977, abs=1e-4)
    assert fd.extras["gps_position"]["alt"] == pytest.approx(48.5)


def test_link_gap_is_reported(feed, tlog_file):
    msgs = _stream(n=10)
    msgs.append(_attitude(BOOT0 + 60.0))
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    assert fd.extras["log_source"]["link_gaps_s"]
    assert "telemetry gap" in " ".join(fd.extras["telemetry_notes"])


def test_missing_params_is_stated(feed, tlog_file):
    feed([m for m in _stream() if m.get_type() != "PARAM_VALUE"])
    fd = tp.parse_tlog(tlog_file)

    assert fd.params == {}
    assert "No PARAM_VALUE" in " ".join(fd.extras["telemetry_notes"])


# ---------------------------------------------------------------------------
# System filtering + drop accounting (StreamStats)
# ---------------------------------------------------------------------------


def test_frames_from_other_systems_are_excluded(feed, tlog_file):
    """A tlog can carry a companion computer or a second vehicle on another sysid."""
    msgs = _stream(n=12)
    # sysid 2: another vehicle streaming nonsense rates
    for i in range(8):
        other = _attitude(BOOT0 + i * 0.1, rollspeed=5.0)
        other._sysid = 2
        msgs.append(other)
    msgs.append(_heartbeat(BOOT0 + 0.05))  # vehicle heartbeat is sysid 1
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    src = fd.extras["log_source"]
    assert src["vehicle_sysid"] == 1
    assert src["frames_from_other_systems"] == 8
    # the intruder's 5 rad/s never reaches the signal
    assert float(np.max(np.abs(fd.pid["roll"].actual))) == pytest.approx(0.5 * RAD2DEG, rel=1e-6)


def test_second_vehicle_is_reported(feed, tlog_file):
    msgs = _stream(n=12)
    other_hb = _heartbeat(BOOT0 + 0.3)
    other_hb._sysid = 7
    msgs.append(other_hb)
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    assert "other vehicle system id" in " ".join(fd.extras["telemetry_notes"])
    assert fd.extras["log_source"]["systems"]["7"]["is_vehicle"] is True


def test_dropped_frames_are_counted_from_sequence_numbers(feed, tlog_file):
    seqs = [0, 1, 2, 13, 14]  # 10 frames lost between 2 and 13
    msgs = [_heartbeat(BOOT0, seq=s) for s in seqs]
    msgs += [_attitude(BOOT0 + i * 0.1) for i in range(12)]
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    src = fd.extras["log_source"]
    assert src["frames_dropped"] == 10
    assert src["drop_percent"] > 5
    assert "frames were lost in transit" in " ".join(fd.extras["telemetry_notes"])


def test_sequence_wrap_is_not_a_drop(feed, tlog_file):
    msgs = [_heartbeat(BOOT0, seq=s) for s in (254, 255, 0, 1)]
    msgs += [_attitude(BOOT0 + i * 0.1) for i in range(12)]
    feed(msgs)
    fd = tp.parse_tlog(tlog_file)

    assert fd.extras["log_source"]["frames_dropped"] == 0


def test_gcs_only_recording_is_refused_with_a_precise_reason(feed, tlog_file):
    msgs = [_gcs_heartbeat(BOOT0 + i * 0.5) for i in range(4)]
    for m in msgs:
        m._sysid = 255
    feed(msgs)
    with pytest.raises(LogFormatError) as exc:
        tp.parse_tlog(tlog_file)
    assert "ground-station" in (exc.value.hint or "")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_px4_sourced_tlog_is_refused_with_ulg_hint(feed, tlog_file):
    feed(_stream(autopilot=12))  # MAV_AUTOPILOT_PX4
    with pytest.raises(LogFormatError) as exc:
        tp.parse_tlog(tlog_file)
    assert ".ulg" in (exc.value.hint or "")


def test_empty_recording_is_refused(feed, tlog_file):
    feed([])
    with pytest.raises(LogFormatError):
        tp.parse_tlog(tlog_file)


# ---------------------------------------------------------------------------
# Quality integration
# ---------------------------------------------------------------------------


def test_quality_surfaces_telemetry_caveats(feed, tlog_file):
    feed(_stream())
    from smarttune.services import analysis as svc

    payload = svc.get_log_quality(tlog_file, platform="ardupilot")

    assert payload["log_source"]["kind"] == "mavlink_telemetry"
    assert payload["telemetry_notes"]
    assert "onboard DataFlash" in payload["quality"]["advice"]
    assert payload["quality"]["score"] <= 85
