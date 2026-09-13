"""
smarttune/platform/ardupilot/tlog_parser.py

MAVLink telemetry log (.tlog) parser — ArduPilot ground-station recordings.

A ``.tlog`` is what Mission Planner / MAVProxy / QGroundControl write while
connected over a radio link: every received MAVLink frame prefixed with an
8-byte big-endian microsecond timestamp. It is a fundamentally different
source from the onboard DataFlash ``.bin``:

===============  ==================================  =============================
                 DataFlash ``.bin``                  Telemetry ``.tlog``
===============  ==================================  =============================
written by       the flight controller (SD card)     the ground station (PC)
rate             loop rate (400 Hz+), lossless       stream rates (1-50 Hz), lossy
rate controller  PIDR/PIDP/PIDY with P/I/D terms     not streamed at all
gyro             IMU at 400 Hz+                      RAW_IMU/ATTITUDE, 2-50 Hz
coverage         whole flight                        only while the link was up
===============  ==================================  =============================

What that means for analysis, and why this parser is explicit about it:

* **PID step response** is reconstructed from ``ATTITUDE_TARGET`` (desired body
  rates) against ``ATTITUDE`` (actual body rates). P/I/D term breakdown does
  not exist in telemetry, so those arrays are zero-filled — exactly like the
  existing legacy-``RATE`` fallback in the DataFlash path. If the vehicle never
  streamed ``ATTITUDE_TARGET`` (it is off by default on many ArduPilot setups),
  there is no desired-rate signal at all and PID analysis is skipped with a
  reason instead of being faked.
* **FFT** is band-limited by the stream rate: a 10 Hz ATTITUDE stream can only
  show 0-5 Hz, which is far below the 40-120 Hz range where prop/frame
  resonances live. The parser records the effective rate and a Nyquist warning
  in ``extras["telemetry_notes"]`` so the report can say so out loud.
* **Parameters** are only present if the GCS downloaded them during the
  recording (Mission Planner does this on connect). Without them, filter
  analysis has nothing to read.

Everything the parser cannot know it leaves absent — never invented.

Alignment with ArduPilot's own tooling
--------------------------------------
Cross-checked against UAVLogViewer (``src/tools/parsers/mavlinkParser.js``,
``src/tools/parsers/modeMaps.js``, ``src/tools/mavlinkDataExtractor.js``), the
reference web parser for these files:

* **Vehicle vs GCS heartbeats.** A tlog carries HEARTBEATs from *both* ends of
  the link; the ground station announces itself as ``MAV_TYPE_GCS``.
  UAVLogViewer filters mode/armed extraction to real vehicle types, and this
  parser does the same — autopilot id, frame type, flight modes and the armed
  timeline are read only from vehicle heartbeats.
* **Mode maps keyed by vehicle type**, not hardcoded to Copter: Copter (ACM),
  Plane (APM), Rover, Sub and Tracker maps mirror ``modeMaps.js``, with the
  same ``base_mode`` bit fallback (AUTO/GUIDED/STABILIZE) when a type has no
  map.
* **Vehicle clock as the timeline.** UAVLogViewer plots against
  ``time_boot_ms`` (the flight controller's own clock) rather than the tlog
  wrapper timestamp, which is the PC's wall clock and carries radio/buffering
  jitter. This parser resolves each sample to the vehicle clock where the
  message provides one (``time_boot_ms`` / boot-relative ``time_usec``), and
  maps wall-clock-only messages (SYS_STATUS, PARAM_VALUE, STATUSTEXT) onto it
  through the median clock offset.
* **Parameter id sanitising** uses the same rule as UAVLogViewer
  (strip anything outside ``[A-Za-z0-9_]``), and **AHRS2** is accepted as a
  position fallback exactly as it is there (and as ``AHR2`` is in our own
  DataFlash path).
* **One system per recording.** WebTools' StreamStats keys everything by
  ``srcSystem``/``srcComponent``; a tlog can contain the GCS, a companion
  computer and even a second vehicle. This parser locks onto the system id of
  the first vehicle heartbeat and ignores frames from other systems, reporting
  what it saw in ``log_source["systems"]``.
* **Dropped frames from sequence numbers**, the way StreamStats measures link
  loss (``dropped += seq - next_seq`` with 8-bit wrap) — a far better measure
  than a wall-clock gap heuristic, and reported as a percentage.

Deliberate differences: attitude/rate values are converted to degrees and
deg/s (UAVLogViewer keeps radians for its 3D view) because ``FlightData``
declares a degrees contract; and lat/lon keep our defensive "already scaled?"
check so a pre-scaled field cannot be divided twice.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from smarttune.errors import LogFileCorruptError, LogFileNotFoundError, LogFormatError
from smarttune.models.flight_data import AxisPIDSignal, FlightData, ModeChange

logger = logging.getLogger(__name__)

_RAD2DEG = 180.0 / np.pi
_MG_TO_MSS = 9.80665 / 1000.0  # milli-g → m/s²
_GAUSS_TO_MGAUSS = 1000.0

#: MAVLink frame start bytes: v1.0 (0xFE) and v2.0 (0xFD)
_MAVLINK_MAGIC = (0xFE, 0xFD)

#: MAV_AUTOPILOT
_AUTOPILOT_ARDUPILOTMEGA = 3
_AUTOPILOT_PX4 = 12
_AUTOPILOT_INVALID = 8

#: MAV_TYPE_GCS — the ground station's own heartbeat, never the vehicle
_MAV_TYPE_GCS = 6

#: MAV_TYPE → SmartTune frame label (mirrors UAVLogViewer's `vehicles` table,
#: kept at SmartTune's finer granularity: quad/hex/octo/tri rather than one
#: "quadcopter" bucket)
_FRAME_TYPES = {
    1: "fixedwing",
    2: "quad",
    3: "coaxial",
    4: "heli",
    5: "tracker",
    10: "rover",
    11: "boat",
    12: "sub",
    13: "hex",
    14: "octo",
    15: "tri",
    19: "vtol-tailsitter-duo",
    20: "vtol-tailsitter-quad",
    21: "vtol-tiltrotor",
    22: "vtol",
    23: "vtol",
    24: "vtol",
    29: "dodeca",
}

#: Vehicle types that own a flight mode. Mirrors UAVLogViewer's `validGCSs`
#: allow-list (MAV_TYPE_GCS = 6 is deliberately absent), plus the VTOL and
#: dodecarotor types its `vehicles` table knows about.
_VEHICLE_TYPES = frozenset(
    {1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20, 21, 22, 23, 24, 29}
)

# ── Mode maps: verbatim from UAVLogViewer src/tools/parsers/modeMaps.js ──────
_MODE_MAP_ACM = {
    0: "STABILIZE",
    1: "ACRO",
    2: "ALT_HOLD",
    3: "AUTO",
    4: "GUIDED",
    5: "LOITER",
    6: "RTL",
    7: "CIRCLE",
    9: "LAND",
    11: "DRIFT",
    13: "SPORT",
    14: "FLIP",
    15: "AUTOTUNE",
    16: "POSHOLD",
    17: "BRAKE",
    18: "THROW",
    19: "AVOID_ADSB",
    20: "GUIDED_NOGPS",
    21: "SMART_RTL",
    22: "FLOWHOLD",
    23: "FOLLOW",
    24: "ZIGZAG",
    25: "SYSTEMID",
    26: "AUTOROTATE",
    27: "AUTO_RTL",  # added after the webtool's table
}
_MODE_MAP_APM = {
    0: "MANUAL",
    1: "CIRCLE",
    2: "STABILIZE",
    3: "TRAINING",
    4: "ACRO",
    5: "FBWA",
    6: "FBWB",
    7: "CRUISE",
    8: "AUTOTUNE",
    10: "AUTO",
    11: "RTL",
    12: "LOITER",
    13: "TAKEOFF",
    14: "AVOID_ADSB",
    15: "GUIDED",
    16: "INITIALISING",
    17: "QSTABILIZE",
    18: "QHOVER",
    19: "QLOITER",
    20: "QLAND",
    21: "QRTL",
    22: "QAUTOTUNE",
    23: "QACRO",
    24: "THERMAL",
}
_MODE_MAP_ROVER = {
    0: "MANUAL",
    1: "ACRO",
    3: "STEERING",
    4: "HOLD",
    5: "LOITER",
    6: "FOLLOW",
    7: "SIMPLE",
    8: "DOCK",
    9: "CIRCLE",
    10: "AUTO",
    11: "RTL",
    12: "SMART_RTL",
    15: "GUIDED",
    16: "INITIALISING",
}
_MODE_MAP_TRACKER = {
    0: "MANUAL",
    1: "STOP",
    2: "SCAN",
    3: "SERVO_TEST",
    10: "AUTO",
    16: "INITIALISING",
}
_MODE_MAP_SUB = {
    0: "STABILIZE",
    1: "ACRO",
    2: "ALT_HOLD",
    3: "AUTO",
    4: "GUIDED",
    7: "CIRCLE",
    9: "SURFACE",
    16: "POSHOLD",
    19: "MANUAL",
    20: "MOTOR_DETECT",
}

_ACM_TYPES = frozenset({2, 3, 4, 13, 14, 15})  # quad, coaxial, heli, hex, octo, tri


def _mode_map_for(mav_type: Optional[int]) -> Optional[Dict[int, str]]:
    """Mode table for a vehicle type — same dispatch as UAVLogViewer.getModeMap."""
    if mav_type in _ACM_TYPES:
        return _MODE_MAP_ACM
    if mav_type == 1:
        return _MODE_MAP_APM
    if mav_type in (10, 11):
        return _MODE_MAP_ROVER
    if mav_type == 5:
        return _MODE_MAP_TRACKER
    if mav_type == 12:
        return _MODE_MAP_SUB
    return None


def _mode_string(mav_type: Optional[int], custom_mode: int, base_mode: int) -> str:
    """custom_mode → firmware mode name, with UAVLogViewer's base_mode fallback."""
    table = _mode_map_for(mav_type)
    if table is None:
        if base_mode & 4:
            return "AUTO"
        if base_mode & 8:
            return "GUIDED"
        if base_mode & 16:
            return "STABILIZE"
        return f"MODE{custom_mode}"
    return table.get(custom_mode, f"MODE{custom_mode}")


#: Below this gyro sample rate an FFT cannot reach the prop/frame resonance band
_FFT_USABLE_RATE_HZ = 100.0

#: UAVLogViewer strips everything outside this set from param ids
_PARAM_ID_STRIP = re.compile(r"[^A-Za-z0-9_]")

#: A boot-relative time_usec cannot plausibly exceed ~30 days; anything larger
#: is a UNIX epoch stamp and must not be mistaken for uptime.
_MAX_BOOT_USEC = 30 * 24 * 3600 * 1_000_000


def looks_like_tlog(path: Path) -> bool:
    """Cheap structural sniff: 8-byte timestamp followed by a MAVLink frame.

    Checks the first record's wrapper: a plausible GCS epoch in microseconds
    followed by a MAVLink v1/v2 start byte — enough to reject a mis-named file
    (e.g. a DataFlash .bin) without parsing it.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    if len(head) < 12:
        return False
    if head[8] not in _MAVLINK_MAGIC:
        return False
    stamp_us = int.from_bytes(head[:8], "big")
    return 1_104_537_600_000_000 < stamp_us < 4_102_444_800_000_000


class _Series:
    """Append-only column bundle backed by plain lists (tlog volumes are small).

    Every series carries two clocks per sample: ``t`` (vehicle clock, NaN when
    the message has none) and ``w`` (GCS wall clock). They are reconciled once,
    after parsing, by :func:`_resolve_clock`.
    """

    __slots__ = ("cols",)

    def __init__(self, *names: str) -> None:
        self.cols: Dict[str, List[float]] = {n: [] for n in ("t", "w", *names)}

    def add(self, t: float, w: float, **values: float) -> None:
        self.cols["t"].append(t)
        self.cols["w"].append(w)
        for name, value in values.items():
            self.cols[name].append(value)

    def arr(self, name: str) -> np.ndarray:
        return np.asarray(self.cols[name], dtype=np.float64)

    def time(self, offset: float, t0: float) -> np.ndarray:
        """Resolved timeline, seconds from log start."""
        t = self.arr("t")
        w = self.arr("w")
        resolved = np.where(np.isfinite(t), t, w - offset)
        return resolved - t0

    def raw_time(self, offset: float) -> np.ndarray:
        t = self.arr("t")
        w = self.arr("w")
        return np.where(np.isfinite(t), t, w - offset)

    def __len__(self) -> int:
        return len(self.cols["w"])


def _median_rate(t: np.ndarray) -> float:
    if t.size < 2:
        return 0.0
    dts = np.diff(t)
    dts = dts[(dts > 1e-9) & (dts < 10.0)]
    if dts.size == 0:
        return 0.0
    dt = float(np.median(dts))
    return 1.0 / dt if dt > 0 else 0.0


def _vehicle_time(msg: Any) -> float:
    """Seconds on the flight controller's clock, or NaN when not provided.

    ``time_boot_ms`` is authoritative (this is the field UAVLogViewer plots
    against). ``time_usec`` is accepted only when it is plausibly uptime —
    GPS_RAW_INT and HIGHRES_IMU may carry a UNIX epoch instead.
    """
    boot_ms = getattr(msg, "time_boot_ms", None)
    if boot_ms:
        try:
            return float(boot_ms) / 1000.0
        except (TypeError, ValueError):
            return float("nan")
    usec = getattr(msg, "time_usec", None)
    if usec:
        try:
            usec_f = float(usec)
        except (TypeError, ValueError):
            return float("nan")
        if 0 < usec_f < _MAX_BOOT_USEC:
            return usec_f / 1_000_000.0
    return float("nan")


def parse_tlog(path: Path, param_map: Optional[Dict[str, str]] = None) -> FlightData:
    """Parse a MAVLink telemetry log into :class:`FlightData`.

    Parameters
    ----------
    path
        Path to the ``.tlog`` file.
    param_map
        Optional generic→platform parameter map. Matching platform parameters
        found in ``PARAM_VALUE`` messages are mirrored under their generic key
        (``pid.roll.p``), the same contract the DataFlash path honours.

    Raises
    ------
    LogFileNotFoundError, LogFileCorruptError, LogFormatError
    """
    from pymavlink import mavutil

    if not path.is_file():
        raise LogFileNotFoundError(message=f"Log file not found: {path}")

    try:
        mlog = mavutil.mavlink_connection(str(path), robust_parsing=True, dialect="ardupilotmega")
    except Exception as exc:
        raise LogFileCorruptError(
            message=f"Cannot open telemetry log: {exc}",
            hint="Check the file is a Mission Planner / MAVProxy .tlog recording",
        ) from exc

    att = _Series("roll", "pitch", "yaw", "rollspeed", "pitchspeed", "yawspeed")
    tgt = _Series("roll_rate", "pitch_rate", "yaw_rate")
    imu = _Series("gx", "gy", "gz", "ax", "ay", "az", "mx", "my", "mz", "has_mag", "has_accel")
    gps = _Series("fix", "lat", "lon", "alt", "sats", "eph")
    bat = _Series("volt", "curr")
    servo = _Series("m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8")
    vfr = _Series("throttle", "alt", "climb")
    ahrs2 = _Series("lat", "lon", "alt")

    params: Dict[str, float] = {}
    msg_log: List[Dict[str, Any]] = []
    mode_events: List[Tuple[float, float, str]] = []  # (t, wall, mode name)
    armed_events: List[Tuple[float, float, bool]] = []
    imu_sources: Dict[str, int] = {}
    clock_pairs: List[float] = []  # wall − vehicle, for messages carrying both
    systems: Dict[int, Dict[str, Any]] = {}  # srcSystem → {count, type, is_vehicle}
    seq_state: Dict[Tuple[int, int], int] = {}  # (sys, comp) → next expected seq
    frames_received = 0
    frames_dropped = 0
    frames_other_system = 0
    vehicle_sysid: Optional[int] = None
    autopilot: Optional[int] = None
    mav_type: Optional[int] = None
    gcs_heartbeats = 0
    firmware_version = ""
    last_mode: Optional[str] = None
    last_armed: Optional[bool] = None
    w_min = w_max = 0.0
    started = False
    msg_count = 0
    link_gaps: List[float] = []
    prev_w: Optional[float] = None

    try:
        while True:
            msg = mlog.recv_match()
            if msg is None:
                break
            mtype = msg.get_type()
            if mtype == "BAD_DATA":
                continue
            wall = getattr(msg, "_timestamp", None)
            if wall is None:
                continue

            # ── Per-system accounting (StreamStats keys on srcSystem/srcComponent) ──
            try:
                sysid = int(msg.get_srcSystem())
                compid = int(msg.get_srcComponent())
            except Exception:
                sysid, compid = 0, 0
            entry = systems.setdefault(sysid, {"count": 0, "type": None, "is_vehicle": False})
            entry["count"] += 1

            # Sequence-based drop count, per (system, component)
            seq = getattr(getattr(msg, "_header", None), "seq", None)
            if seq is not None:
                key = (sysid, compid)
                expected = seq_state.get(key)
                if expected is not None:
                    observed = seq if seq >= expected else seq + 256
                    frames_dropped += observed - expected
                seq_state[key] = (int(seq) + 1) % 256
                frames_received += 1

            if mtype == "HEARTBEAT":
                htype = int(getattr(msg, "type", 0))
                entry["type"] = htype
                entry["is_vehicle"] = htype in _VEHICLE_TYPES
                if vehicle_sysid is None and htype in _VEHICLE_TYPES:
                    vehicle_sysid = sysid

            # Frames from another system (GCS, companion computer, second vehicle)
            # are not this vehicle's flight data.
            if vehicle_sysid is not None and sysid != vehicle_sysid:
                frames_other_system += 1
                continue

            msg_count += 1
            if not started:
                w_min = wall
                started = True
            if prev_w is not None and wall - prev_w > 3.0:
                link_gaps.append(round(wall - prev_w, 1))
            prev_w = wall
            w_max = wall

            t = _vehicle_time(msg)
            if np.isfinite(t):
                clock_pairs.append(wall - t)

            if mtype == "HEARTBEAT":
                htype = int(getattr(msg, "type", 0))
                hap = int(getattr(msg, "autopilot", _AUTOPILOT_INVALID))
                # A tlog carries the ground station's heartbeat too — it is not
                # the vehicle, and reading autopilot/type/mode from it is wrong.
                if htype == _MAV_TYPE_GCS or htype not in _VEHICLE_TYPES:
                    gcs_heartbeats += 1
                    continue
                if autopilot is None or autopilot == _AUTOPILOT_INVALID:
                    autopilot = hap
                if mav_type is None:
                    mav_type = htype
                base = int(getattr(msg, "base_mode", 0))
                name = _mode_string(htype, int(getattr(msg, "custom_mode", 0)), base)
                if name != last_mode:
                    mode_events.append((t, wall, name))
                    last_mode = name
                armed = bool(base & 0b10000000)  # MAV_MODE_FLAG_SAFETY_ARMED
                if armed != last_armed:
                    armed_events.append((t, wall, armed))
                    last_armed = armed

            elif mtype == "ATTITUDE":
                att.add(
                    t,
                    wall,
                    roll=getattr(msg, "roll", 0.0) * _RAD2DEG,
                    pitch=getattr(msg, "pitch", 0.0) * _RAD2DEG,
                    yaw=getattr(msg, "yaw", 0.0) * _RAD2DEG,
                    rollspeed=getattr(msg, "rollspeed", 0.0) * _RAD2DEG,
                    pitchspeed=getattr(msg, "pitchspeed", 0.0) * _RAD2DEG,
                    yawspeed=getattr(msg, "yawspeed", 0.0) * _RAD2DEG,
                )

            elif mtype == "ATTITUDE_TARGET":
                tgt.add(
                    t,
                    wall,
                    roll_rate=getattr(msg, "body_roll_rate", 0.0) * _RAD2DEG,
                    pitch_rate=getattr(msg, "body_pitch_rate", 0.0) * _RAD2DEG,
                    yaw_rate=getattr(msg, "body_yaw_rate", 0.0) * _RAD2DEG,
                )

            elif mtype in ("RAW_IMU", "SCALED_IMU2", "SCALED_IMU3"):
                # MAVLink spec: xacc mG, xgyro mrad/s, xmag mgauss
                imu_sources[mtype] = imu_sources.get(mtype, 0) + 1
                imu.add(
                    t,
                    wall,
                    gx=getattr(msg, "xgyro", 0) * 1e-3 * _RAD2DEG,
                    gy=getattr(msg, "ygyro", 0) * 1e-3 * _RAD2DEG,
                    gz=getattr(msg, "zgyro", 0) * 1e-3 * _RAD2DEG,
                    ax=getattr(msg, "xacc", 0) * _MG_TO_MSS,
                    ay=getattr(msg, "yacc", 0) * _MG_TO_MSS,
                    az=getattr(msg, "zacc", 0) * _MG_TO_MSS,
                    mx=float(getattr(msg, "xmag", 0)),
                    my=float(getattr(msg, "ymag", 0)),
                    mz=float(getattr(msg, "zmag", 0)),
                    has_mag=1.0,
                    has_accel=1.0,
                )

            elif mtype == "HIGHRES_IMU":
                # SI units: xacc m/s², xgyro rad/s, xmag Gauss
                imu_sources[mtype] = imu_sources.get(mtype, 0) + 1
                imu.add(
                    t,
                    wall,
                    gx=getattr(msg, "xgyro", 0.0) * _RAD2DEG,
                    gy=getattr(msg, "ygyro", 0.0) * _RAD2DEG,
                    gz=getattr(msg, "zgyro", 0.0) * _RAD2DEG,
                    ax=getattr(msg, "xacc", 0.0),
                    ay=getattr(msg, "yacc", 0.0),
                    az=getattr(msg, "zacc", 0.0),
                    mx=getattr(msg, "xmag", 0.0) * _GAUSS_TO_MGAUSS,
                    my=getattr(msg, "ymag", 0.0) * _GAUSS_TO_MGAUSS,
                    mz=getattr(msg, "zmag", 0.0) * _GAUSS_TO_MGAUSS,
                    has_mag=1.0,
                    has_accel=1.0,
                )

            elif mtype == "GPS_RAW_INT":
                lat = getattr(msg, "lat", 0)
                lon = getattr(msg, "lon", 0)
                gps.add(
                    t,
                    wall,
                    fix=int(getattr(msg, "fix_type", 0)),
                    lat=lat * 1e-7 if abs(lat) > 1000 else float(lat),
                    lon=lon * 1e-7 if abs(lon) > 1000 else float(lon),
                    alt=getattr(msg, "alt", 0) * 1e-3,
                    sats=int(getattr(msg, "satellites_visible", 0)),
                    eph=getattr(msg, "eph", 0) * 1e-2,
                )

            elif mtype == "GLOBAL_POSITION_INT" and len(gps) == 0:
                lat = getattr(msg, "lat", 0)
                lon = getattr(msg, "lon", 0)
                gps.add(
                    t,
                    wall,
                    fix=3,
                    lat=lat * 1e-7 if abs(lat) > 1000 else float(lat),
                    lon=lon * 1e-7 if abs(lon) > 1000 else float(lon),
                    alt=getattr(msg, "alt", 0) * 1e-3,
                    sats=0,
                    eph=0.0,
                )

            elif mtype == "AHRS2":
                # Position fallback — UAVLogViewer offers AHRS2 as a trajectory
                # source, and our DataFlash path uses the equivalent AHR2.
                lat = getattr(msg, "lat", 0)
                lon = getattr(msg, "lng", getattr(msg, "lon", 0))
                ahrs2.add(
                    t,
                    wall,
                    lat=lat * 1e-7 if abs(lat) > 1000 else float(lat),
                    lon=lon * 1e-7 if abs(lon) > 1000 else float(lon),
                    alt=float(getattr(msg, "altitude", 0.0)),
                )

            elif mtype == "SYS_STATUS":
                volt = getattr(msg, "voltage_battery", 0)
                curr = getattr(msg, "current_battery", -1)
                if volt not in (0, 65535):
                    bat.add(t, wall, volt=volt * 1e-3, curr=max(curr, 0) * 1e-2)

            elif mtype == "BATTERY_STATUS":
                voltages = getattr(msg, "voltages", None) or []
                cells = [v for v in voltages if v not in (0, 65535)]
                if cells:
                    bat.add(
                        t,
                        wall,
                        volt=sum(cells) * 1e-3,
                        curr=max(getattr(msg, "current_battery", 0), 0) * 1e-2,
                    )

            elif mtype == "SERVO_OUTPUT_RAW":

                def _norm(raw: int) -> float:
                    if raw <= 0:
                        return 0.0
                    return float(np.clip((raw - 1000.0) / 1000.0, 0.0, 1.0))

                servo.add(
                    t,
                    wall,
                    **{f"m{i}": _norm(int(getattr(msg, f"servo{i}_raw", 0))) for i in range(1, 9)},
                )

            elif mtype == "VFR_HUD":
                vfr.add(
                    t,
                    wall,
                    throttle=float(getattr(msg, "throttle", 0)),
                    alt=float(getattr(msg, "alt", 0.0)),
                    climb=float(getattr(msg, "climb", 0.0)),
                )

            elif mtype == "PARAM_VALUE":
                raw_id = getattr(msg, "param_id", "")
                if isinstance(raw_id, bytes):
                    raw_id = raw_id.decode("utf-8", errors="replace")
                # Same sanitising as UAVLogViewer: keep [A-Za-z0-9_] only
                name = _PARAM_ID_STRIP.sub("", str(raw_id))
                if name:
                    try:
                        params[name] = float(getattr(msg, "param_value", 0.0))
                    except (TypeError, ValueError):
                        pass

            elif mtype == "STATUSTEXT":
                text = getattr(msg, "text", "")
                if isinstance(text, bytes):
                    text = text.decode("utf-8", errors="replace")
                msg_log.append(
                    {
                        "time": wall,
                        "severity": int(getattr(msg, "severity", 6)),
                        "text": str(text).strip("\x00"),
                    }
                )

            elif mtype == "AUTOPILOT_VERSION":
                sw = int(getattr(msg, "flight_sw_version", 0))
                if sw:
                    firmware_version = f"{(sw >> 24) & 0xFF}.{(sw >> 16) & 0xFF}.{(sw >> 8) & 0xFF}"

    except (LogFileCorruptError, LogFormatError):
        raise
    except Exception as exc:
        raise LogFileCorruptError(
            message=f"Telemetry parsing error after {msg_count} messages: {exc}",
            hint="The recording may have been cut off mid-frame.",
        ) from exc

    # Nothing analysable? Say precisely why. A recording can be non-empty and
    # still carry no vehicle data — e.g. the GCS was logging before it ever
    # connected, so every frame is its own heartbeat.
    has_signal = bool(params) or any(
        len(series) for series in (att, tgt, imu, gps, bat, servo, vfr, ahrs2)
    )
    if msg_count == 0 or not has_signal:
        if systems and not any(meta.get("is_vehicle") for meta in systems.values()):
            raise LogFormatError(
                message=f"No vehicle frames in {path.name}",
                hint=(
                    "The recording only contains ground-station / companion traffic "
                    f"(system ids seen: {sorted(systems)}). Record while connected to "
                    "the vehicle."
                ),
            )
        raise LogFormatError(
            message=f"No usable flight data in {path.name}",
            hint="The file may be empty, truncated, or not a telemetry log.",
        )

    if autopilot == _AUTOPILOT_PX4:
        raise LogFormatError(
            message="This .tlog was recorded from a PX4 vehicle",
            hint=(
                "SmartTune analyses PX4 from the onboard ULog (.ulg), which carries the "
                "rate-controller data telemetry does not. Export the .ulg from the vehicle."
            ),
        )
    if autopilot not in (None, _AUTOPILOT_ARDUPILOTMEGA, _AUTOPILOT_INVALID):
        logger.warning(
            "tlog vehicle HEARTBEAT reports autopilot id %s (not ArduPilot) — parsing anyway",
            autopilot,
        )

    notes: List[str] = []

    # ── Reconcile the two clocks ─────────────────────────────────────────
    # Vehicle clock (time_boot_ms) is the measurement timestamp; the tlog
    # wrapper is when the PC received the frame. Prefer the former (as
    # UAVLogViewer does) and shift wall-clock-only messages onto it.
    if clock_pairs:
        offset = float(np.median(np.asarray(clock_pairs, dtype=np.float64)))
        clock_kind = "vehicle_boot"
    else:
        offset = 0.0
        clock_kind = "gcs_wall"
        notes.append(
            "No message carried a vehicle timestamp (time_boot_ms): timing is based on the "
            "ground station's receive clock, which includes radio and buffering jitter."
        )

    all_times: List[np.ndarray] = []
    for series in (att, tgt, imu, gps, bat, servo, vfr, ahrs2):
        if len(series):
            all_times.append(series.raw_time(offset))
    mode_times = np.asarray(
        [(t if np.isfinite(t) else w - offset) for t, w, _ in mode_events], dtype=np.float64
    )
    if mode_times.size:
        all_times.append(mode_times)
    if all_times:
        t0 = float(min(float(np.min(a)) for a in all_times))
        t_end = float(max(float(np.max(a)) for a in all_times))
    else:
        t0, t_end = w_min - offset, w_max - offset

    # ── PID: ATTITUDE_TARGET (desired) vs ATTITUDE (actual) ──────────────
    pid_axes: Dict[str, AxisPIDSignal] = {}
    n_att, n_tgt = len(att), len(tgt)
    if n_att >= 10 and n_tgt >= 10:
        t_act = att.time(offset, t0)
        t_des = tgt.time(offset, t0)
        order = np.argsort(t_des)
        t_des_sorted = t_des[order]
        for axis, act_col, des_col in (
            ("roll", "rollspeed", "roll_rate"),
            ("pitch", "pitchspeed", "pitch_rate"),
            ("yaw", "yawspeed", "yaw_rate"),
        ):
            actual = att.arr(act_col)
            desired = np.interp(t_act, t_des_sorted, tgt.arr(des_col)[order])
            zeros = np.zeros_like(actual)
            pid_axes[axis] = AxisPIDSignal(
                timestamp_s=t_act,
                desired=desired,
                actual=actual,
                p_term=zeros,
                i_term=zeros.copy(),
                d_term=zeros.copy(),
                ff_term=zeros.copy(),
            )
        notes.append(
            "PID signals reconstructed from ATTITUDE_TARGET vs ATTITUDE body rates; "
            "P/I/D term breakdown is not streamed over telemetry and reads as zero."
        )
        rate_hz = _median_rate(t_act)
        if rate_hz and rate_hz < 50:
            notes.append(
                f"Rate signals sampled at only ~{rate_hz:.0f} Hz (telemetry stream rate) — "
                "step-response timing metrics are coarse; use the onboard .bin for tuning."
            )
    elif n_att >= 10:
        notes.append(
            "No ATTITUDE_TARGET messages: telemetry carries actual rates but no desired "
            "rates, so PID step-response analysis is unavailable. Raise SR*_EXTRA1 / enable "
            "ATTITUDE_TARGET streaming, or analyse the onboard .bin log."
        )
    else:
        notes.append("No ATTITUDE messages: attitude/rate data unavailable in this recording.")

    fd = FlightData(
        platform="ardupilot",
        log_file=str(path),
        params=params,
        firmware_version=firmware_version,
    )
    fd.pid.update(pid_axes)
    if mav_type is not None:
        fd.frame_type = _FRAME_TYPES.get(mav_type)

    # ── Gyro / accel / mag: pick the richest IMU source ──────────────────
    n_imu = len(imu)
    gyro_source = ""
    if n_imu >= 10:
        t_imu = imu.time(offset, t0)
        fd.imu_timestamp_s = t_imu
        fd.gyro = np.column_stack([imu.arr("gx"), imu.arr("gy"), imu.arr("gz")])
        if float(np.max(imu.arr("has_accel"))) > 0:
            fd.accel = np.column_stack([imu.arr("ax"), imu.arr("ay"), imu.arr("az")])
        mag_mask = imu.arr("has_mag") > 0
        if int(np.sum(mag_mask)) >= 10:
            mx, my, mz = imu.arr("mx"), imu.arr("my"), imu.arr("mz")
            # A vehicle with no compass still streams RAW_IMU with zeroed mag
            # fields — all-zero rows are absence, not a measurement.
            nonzero = (mx != 0) | (my != 0) | (mz != 0)
            mag_mask = mag_mask & nonzero
        if int(np.sum(mag_mask)) >= 10:
            fd.mag_timestamp_s = t_imu[mag_mask]
            fd.mag = np.column_stack(
                [imu.arr("mx")[mag_mask], imu.arr("my")[mag_mask], imu.arr("mz")[mag_mask]]
            )
        gyro_source = "+".join(sorted(imu_sources)) or "RAW_IMU"
    elif n_att >= 10:
        # No IMU stream at all — ATTITUDE body rates are the only gyro-like signal
        fd.imu_timestamp_s = att.time(offset, t0)
        fd.gyro = np.column_stack(
            [att.arr("rollspeed"), att.arr("pitchspeed"), att.arr("yawspeed")]
        )
        gyro_source = "ATTITUDE"
        notes.append(
            "No RAW_IMU/SCALED_IMU stream: gyro traces are ATTITUDE body rates "
            "(already filtered by the EKF), not raw sensor data."
        )

    if fd.gyro is not None and fd.imu_timestamp_s is not None:
        gyro_rate = _median_rate(fd.imu_timestamp_s)
        fd.sample_rate_hz = gyro_rate
        if 0 < gyro_rate < _FFT_USABLE_RATE_HZ:
            notes.append(
                f"Gyro source {gyro_source} sampled at ~{gyro_rate:.0f} Hz → FFT is limited to "
                f"{gyro_rate / 2:.0f} Hz, below the 40-120 Hz band where prop and frame "
                "resonances appear. Vibration findings from this log are indicative only."
            )
    elif fd.pid:
        fd.sample_rate_hz = _median_rate(next(iter(fd.pid.values())).timestamp_s)

    if fd.accel is None:
        notes.append("No accelerometer stream in this recording (RAW_IMU not requested).")

    # ── Battery / motors ────────────────────────────────────────────────
    if len(bat) >= 2:
        fd.battery_timestamp_s = bat.time(offset, t0)
        fd.battery_voltage = bat.arr("volt")
        fd.battery_current = bat.arr("curr")
    if len(servo) >= 2:
        fd.motor_timestamp_s = servo.time(offset, t0)
        fd.motor_output = np.column_stack([servo.arr(f"m{i}") for i in range(1, 9)])

    # ── Mode changes (vehicle heartbeats only) ──────────────────────────
    from smarttune.platform.ardupilot import _MODE_MAP as _CANONICAL_MODES

    for raw_t, raw_w, name in mode_events:
        stamp = (raw_t if np.isfinite(raw_t) else raw_w - offset) - t0
        fd.mode_changes.append(
            ModeChange(
                timestamp_s=stamp,
                mode_name=_CANONICAL_MODES.get(name.upper(), name.lower()),
                raw_mode=name,
            )
        )

    # ── Extras (same keys the DataFlash path publishes) ─────────────────
    if n_att >= 1:
        t_att = att.time(offset, t0)
        fd.extras["attitude"] = {
            "time": t_att,
            "Roll": att.arr("roll"),
            "Pitch": att.arr("pitch"),
            "Yaw": att.arr("yaw"),
            "RollIn": np.zeros_like(t_att),
            "PitchIn": np.zeros_like(t_att),
            "YawIn": np.zeros_like(t_att),
        }

    gps_lat = gps_lon = gps_alt = 0.0
    if len(gps) > 0:
        fixes, lats, lons, alts = (gps.arr("fix"), gps.arr("lat"), gps.arr("lon"), gps.arr("alt"))
        for i in range(len(gps) - 1, -1, -1):
            if fixes[i] >= 3 and (lats[i] != 0 or lons[i] != 0):
                gps_lat, gps_lon, gps_alt = float(lats[i]), float(lons[i]), float(alts[i])
                break
    if gps_lat == 0 and gps_lon == 0 and len(ahrs2) > 0:
        lats, lons, alts = ahrs2.arr("lat"), ahrs2.arr("lon"), ahrs2.arr("alt")
        for i in range(len(ahrs2) - 1, -1, -1):
            if lats[i] != 0 or lons[i] != 0:
                gps_lat, gps_lon, gps_alt = float(lats[i]), float(lons[i]), float(alts[i])
                break
    fd.extras["gps_position"] = {"lat": gps_lat, "lon": gps_lon, "alt": gps_alt}
    fd.extras["msg_log"] = msg_log
    fd.extras["compass_raw"] = []  # MagFit needs per-compass offsets; telemetry has none
    fd.extras["autotune"] = {"ATUN": [], "ATDE": []}
    fd.extras["version_info"] = {"FWVer": firmware_version}
    fd.extras["armed_events"] = [
        {
            "time": (rt if np.isfinite(rt) else rw - offset) - t0,
            "armed": armed,
        }
        for rt, rw, armed in armed_events
    ]
    if len(vfr) > 0:
        fd.extras["vfr_hud"] = {
            "time": vfr.time(offset, t0),
            "throttle": vfr.arr("throttle"),
            "alt": vfr.arr("alt"),
            "climb": vfr.arr("climb"),
        }

    if not params:
        notes.append(
            "No PARAM_VALUE messages: the ground station did not download parameters during "
            "this recording, so current-value comparisons and filter analysis are unavailable."
        )
    elif param_map:
        # Same contract as the DataFlash path: mirror platform parameters under
        # their generic key so platform-agnostic analyzers can read current values.
        for generic, platform_name in param_map.items():
            if platform_name in params and generic not in params:
                params[generic] = params[platform_name]

    if link_gaps:
        notes.append(
            f"{len(link_gaps)} telemetry gap(s) over 3 s (worst {max(link_gaps):.0f} s) — "
            "radio dropouts leave holes in the data."
        )
    total_frames = frames_received + frames_dropped
    if total_frames > 0 and frames_dropped > 0:
        drop_pct = frames_dropped / total_frames * 100
        if drop_pct >= 5.0:
            notes.append(
                f"{drop_pct:.1f}% of MAVLink frames were lost in transit "
                f"({frames_dropped} of {total_frames}, from sequence numbers) — "
                "the recording is missing data the vehicle did send."
            )
    other_vehicles = [
        sid for sid, meta in systems.items() if meta.get("is_vehicle") and sid != vehicle_sysid
    ]
    if other_vehicles:
        notes.append(
            f"Recording contains {len(other_vehicles)} other vehicle system id(s) "
            f"{other_vehicles}: only system {vehicle_sysid} was analysed."
        )
    if mav_type is not None and mav_type not in _ACM_TYPES:
        notes.append(
            f"Vehicle reports MAV_TYPE {mav_type} ({_FRAME_TYPES.get(mav_type, 'unknown')}): "
            "SmartTune's tuning knowledge targets multirotors."
        )

    fd.duration_s = max(t_end - t0, 0.0)
    fd.extras["log_source"] = {
        "kind": "mavlink_telemetry",
        "extension": path.suffix.lower(),
        "message_count": msg_count,
        "clock": clock_kind,
        "clock_offset_s": round(offset, 3),
        "gyro_source": gyro_source,
        "imu_sources": imu_sources,
        "attitude_samples": n_att,
        "attitude_target_samples": n_tgt,
        "param_count": len(params),
        "link_gaps_s": link_gaps,
        "frames_received": frames_received,
        "frames_dropped": frames_dropped,
        "drop_percent": (
            round(frames_dropped / (frames_received + frames_dropped) * 100, 2)
            if (frames_received + frames_dropped) > 0
            else 0.0
        ),
        "vehicle_sysid": vehicle_sysid,
        "systems": {str(k): v for k, v in systems.items()},
        "frames_from_other_systems": frames_other_system,
        "mav_type": mav_type,
        "autopilot": autopilot,
        "gcs_heartbeats_ignored": gcs_heartbeats,
    }
    fd.extras["telemetry_notes"] = notes

    logger.info(
        "Parsed %s: %d messages, %.0fs, clock=%s, gyro=%s @ %.0f Hz, pid_axes=%s, params=%d",
        path.name,
        msg_count,
        fd.duration_s,
        clock_kind,
        gyro_source or "none",
        fd.sample_rate_hz,
        sorted(fd.pid),
        len(params),
    )
    return fd
