"""
smarttune/platform/betaflight/__init__.py

Betaflight Blackbox 日志适配器 — BBL/BFL 格式。

Phase 2: 完整 BBL 解析器实现。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Set

import numpy as np

from smarttune.platform.base import PlatformAdapter
from smarttune.platform.registry import register
from smarttune.models.flight_data import AxisPIDSignal, FlightData, ModeChange
from smarttune.errors import (
    LogFileNotFoundError, LogFileCorruptError, ParseError,
    InsufficientPIDDataError, SmartTuneError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Betaflight 参数映射表
# ---------------------------------------------------------------------------

_PARAM_MAP_TO_PLATFORM = {
    # PID gains
    "pid.roll.p":    "pid_roll_p",
    "pid.roll.i":    "pid_roll_i",
    "pid.roll.d":    "pid_roll_d",
    "pid.roll.ff":   "pid_roll_f",       # BF 4.x: feedforward
    "pid.pitch.p":   "pid_pitch_p",
    "pid.pitch.i":   "pid_pitch_i",
    "pid.pitch.d":   "pid_pitch_d",
    "pid.pitch.ff":  "pid_pitch_f",
    "pid.yaw.p":     "pid_yaw_p",
    "pid.yaw.i":     "pid_yaw_i",
    "pid.yaw.d":     "pid_yaw_d",
    "pid.yaw.ff":    "pid_yaw_f",
    # Filters
    "filter.gyro_lpf":     "gyro_lowpass_hz",
    "filter.gyro_lpf2":    "gyro_lowpass2_hz",
    "filter.dterm_lpf":    "dterm_lowpass_hz",
    "filter.notch1.freq":  "gyro_notch1_hz",
    "filter.notch1.bw":    "gyro_notch1_cutoff",
    "filter.notch2.freq":  "gyro_notch2_hz",
    "filter.notch2.bw":    "gyro_notch2_cutoff",
    # Betaflight-specific
    "pid.roll.d_min":    "d_min_roll",
    "pid.pitch.d_min":   "d_min_pitch",
    "pid.yaw.d_min":     "d_min_yaw",
    "pid.anti_gravity":  "anti_gravity_gain",
}

_PARAM_MAP_TO_GENERIC = {v: k for k, v in _PARAM_MAP_TO_PLATFORM.items()}

# Betaflight 模式映射
_MODE_MAP = {
    "ANGLE": "stabilize",
    "HORIZON": "horizon",
    "ACRO": "acro",
    "AIR": "acro",
    "FAILSAFE": "failsafe",
    "DISARMED": "disarmed",
}


# ---------------------------------------------------------------------------
# Blackbox 日志 magic bytes
# ---------------------------------------------------------------------------

_BBL_MAGIC = b"H Product:Blackbox"


# ---------------------------------------------------------------------------
# BBL field name → FlightData mapping
# ---------------------------------------------------------------------------

_GYRO_FIELD_NAMES = {
    "roll":  ["gyroADC[0]", "gyroADC_0", "gyroData[0]"],
    "pitch": ["gyroADC[1]", "gyroADC_1", "gyroData[1]"],
    "yaw":   ["gyroADC[2]", "gyroADC_2", "gyroData[2]"],
}

_SETPOINT_FIELD_NAMES = {
    "roll":  ["setpoint[0]", "rcCommand[0]"],
    "pitch": ["setpoint[1]", "rcCommand[1]"],
    "yaw":   ["setpoint[2]", "rcCommand[2]"],
}

_PID_P_FIELDS = {
    "roll": ["axisP[0]"], "pitch": ["axisP[1]"], "yaw": ["axisP[2]"],
}
_PID_I_FIELDS = {
    "roll": ["axisI[0]"], "pitch": ["axisI[1]"], "yaw": ["axisI[2]"],
}
_PID_D_FIELDS = {
    "roll": ["axisD[0]"], "pitch": ["axisD[1]"], "yaw": ["axisD[2]"],
}
_PID_F_FIELDS = {
    "roll": ["axisF[0]"], "pitch": ["axisF[1]"], "yaw": ["axisF[2]"],
}

_MOTOR_FIELDS = ["motor[0]", "motor[1]", "motor[2]", "motor[3]",
                 "motor[4]", "motor[5]", "motor[6]", "motor[7]"]

_ACCEL_FIELDS = {
    "x": ["accSmooth[0]", "accData[0]"],
    "y": ["accSmooth[1]", "accData[1]"],
    "z": ["accSmooth[2]", "accData[2]"],
}

_TIME_FIELD = "time"


def _resolve_field_name(field_names, candidates):
    """在字段名列表中找到第一个匹配项。"""
    for name in candidates:
        if name in field_names:
            return name
    return None


# ---------------------------------------------------------------------------
# BetaflightAdapter
# ---------------------------------------------------------------------------

@register
class BetaflightAdapter(PlatformAdapter):
    """Betaflight Blackbox 日志适配器。"""

    @property
    def name(self) -> str:
        return "betaflight"

    @property
    def display_name(self) -> str:
        return "Betaflight"

    @property
    def supported_extensions(self) -> list[str]:
        return [".bbl", ".bfl", ".txt"]

    # ── 检测 ────────────────────────────────────────────────

    @classmethod
    def detect(cls, path: Path) -> bool:
        """检测是否为 Betaflight Blackbox 日志。"""
        if not path.is_file():
            return False

        suffix = path.suffix.lower()
        if suffix not in (".bbl", ".bfl", ".txt"):
            return False

        try:
            with open(path, "rb") as f:
                header = f.read(64)
            return _BBL_MAGIC in header
        except (OSError, IOError):
            return False

    # ── 解析 ────────────────────────────────────────────────

    def parse(self, path: Path, segment_index: int = 0) -> FlightData:
        """解析 Betaflight Blackbox 日志 → FlightData。

        Parameters
        ----------
        path : Path
            BBL 日志文件路径
        segment_index : int
            要解析的飞行段索引 (BBL 文件可含多段飞行, 默认第 0 段)

        Returns
        -------
        FlightData
            填充了 BF 日志数据的统一飞行数据结构
        """
        from smarttune.platform.betaflight.bbl_parser import (
            parse_bbl, get_primary_mode,
            EVENT_FLIGHT_MODE,
        )

        if not path.is_file():
            raise LogFileNotFoundError(
                message=f"Log file not found: {path}",
                hint="Check the file path and ensure the file exists.",
            )

        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LogFileCorruptError(
                message=f"Cannot read log file: {exc}",
                hint="Ensure the file is readable and not locked.",
            )

        if _BBL_MAGIC not in data[:128]:
            raise LogFileCorruptError(
                message="Not a valid Betaflight Blackbox log",
                hint="Ensure this is a .bbl/.bfl file from Betaflight.",
            )

        # 解析 BBL 数据
        try:
            segments = parse_bbl(data, max_segments=segment_index + 1)
        except Exception as exc:
            raise ParseError(
                message=f"BBL parse failed: {exc}",
                hint="The log file may be corrupted or use an unsupported format version.",
            )

        if not segments or segment_index >= len(segments):
            raise ParseError(
                message=f"No valid log segment found (requested index {segment_index})",
                hint="The log file may be empty or corrupted.",
            )

        segment = segments[segment_index]
        header = segment.header
        frames = segment.frames

        if len(frames) < 10:
            raise InsufficientPIDDataError(
                message=f"Too few frames in log ({len(frames)})",
                hint="The flight recording may have been too short.",
            )

        # ── 确定可用字段 ────────────────────────────
        available_fields = set()
        if frames:
            available_fields = set(frames[0].values.keys())

        # ── 提取参数 (从头信息) ────────────────────
        params: Dict[str, float] = {}
        for key, value in header.properties.items():
            try:
                params[key] = float(value)
            except (ValueError, TypeError):
                pass

        # ── 计算时间序列 ────────────────────────────
        loop_rate_hz = params.get("looptime", 250)
        if loop_rate_hz > 100:
            sample_rate_hz = 1_000_000.0 / loop_rate_hz
        else:
            sample_rate_hz = loop_rate_hz

        time_field_available = _TIME_FIELD in available_fields
        timestamps_us = []
        for f in frames:
            if time_field_available:
                timestamps_us.append(f.values.get(_TIME_FIELD, 0))
            else:
                timestamps_us.append(f.values.get("loopIteration", 0))

        timestamps_us = np.array(timestamps_us, dtype=np.float64)

        if time_field_available:
            timestamps_s = timestamps_us / 1_000_000.0
        else:
            looptime_us = params.get("looptime", 250)
            timestamps_s = timestamps_us * looptime_us / 1_000_000.0

        if len(timestamps_s) > 0:
            timestamps_s = timestamps_s - timestamps_s[0]

        n_frames = len(frames)

        # ── 提取 PID 信号 ──────────────────────────
        pid_data: Dict[str, AxisPIDSignal] = {}

        for axis in ["roll", "pitch", "yaw"]:
            gyro_name = _resolve_field_name(available_fields, _GYRO_FIELD_NAMES[axis])
            sp_name = _resolve_field_name(available_fields, _SETPOINT_FIELD_NAMES[axis])

            if gyro_name is None and sp_name is None:
                continue

            actual = (np.array([f.values.get(gyro_name, 0) for f in frames], dtype=np.float64)
                      if gyro_name else np.zeros(n_frames))
            desired = (np.array([f.values.get(sp_name, 0) for f in frames], dtype=np.float64)
                       if sp_name else np.zeros(n_frames))

            p_name = _resolve_field_name(available_fields, _PID_P_FIELDS[axis])
            i_name = _resolve_field_name(available_fields, _PID_I_FIELDS[axis])
            d_name = _resolve_field_name(available_fields, _PID_D_FIELDS[axis])
            f_name = _resolve_field_name(available_fields, _PID_F_FIELDS[axis])

            p_term = (np.array([f.values.get(p_name, 0) for f in frames], dtype=np.float64)
                       if p_name else None)
            i_term = (np.array([f.values.get(i_name, 0) for f in frames], dtype=np.float64)
                       if i_name else None)
            d_term = (np.array([f.values.get(d_name, 0) for f in frames], dtype=np.float64)
                       if d_name else None)
            ff_term = (np.array([f.values.get(f_name, 0) for f in frames], dtype=np.float64)
                        if f_name else None)

            pid_data[axis] = AxisPIDSignal(
                timestamp_s=timestamps_s.copy(),
                desired=desired,
                actual=actual,
                p_term=p_term,
                i_term=i_term,
                d_term=d_term,
                ff_term=ff_term,
            )

        # ── 提取 gyro IMU 数据 ─────────────────────
        gyro_x_name = _resolve_field_name(available_fields, _GYRO_FIELD_NAMES["roll"])
        gyro_y_name = _resolve_field_name(available_fields, _GYRO_FIELD_NAMES["pitch"])
        gyro_z_name = _resolve_field_name(available_fields, _GYRO_FIELD_NAMES["yaw"])

        gyro = None
        if gyro_x_name and gyro_y_name and gyro_z_name:
            gx = np.array([f.values.get(gyro_x_name, 0) for f in frames], dtype=np.float64)
            gy = np.array([f.values.get(gyro_y_name, 0) for f in frames], dtype=np.float64)
            gz = np.array([f.values.get(gyro_z_name, 0) for f in frames], dtype=np.float64)
            gyro = np.column_stack([gx, gy, gz])

        # ── 提取加速度 ─────────────────────────────
        acc_x_name = _resolve_field_name(available_fields, _ACCEL_FIELDS["x"])
        acc_y_name = _resolve_field_name(available_fields, _ACCEL_FIELDS["y"])
        acc_z_name = _resolve_field_name(available_fields, _ACCEL_FIELDS["z"])

        accel = None
        if acc_x_name and acc_y_name and acc_z_name:
            ax = np.array([f.values.get(acc_x_name, 0) for f in frames], dtype=np.float64)
            ay = np.array([f.values.get(acc_y_name, 0) for f in frames], dtype=np.float64)
            az = np.array([f.values.get(acc_z_name, 0) for f in frames], dtype=np.float64)
            accel = np.column_stack([ax, ay, az]) * (9.80665 / 512.0)

        # ── 提取电机输出 ────────────────────────────
        motor_output = None
        motor_names = [m for m in _MOTOR_FIELDS if m in available_fields]
        if motor_names:
            motor_cols = []
            for mn in motor_names:
                motor_cols.append(
                    np.array([f.values.get(mn, 0) for f in frames], dtype=np.float64))
            motor_raw = np.column_stack(motor_cols)
            minthrottle = params.get("minthrottle", 1070)
            maxthrottle = params.get("maxthrottle", 2000)
            throttle_range = maxthrottle - minthrottle
            if throttle_range > 0:
                motor_output = np.clip(
                    (motor_raw - minthrottle) / throttle_range, 0.0, 1.0)
            else:
                motor_output = motor_raw

        # ── 提取飞行模式 ──────────────────────────
        mode_changes: List[ModeChange] = []
        for event in segment.events:
            if event.event_type == EVENT_FLIGHT_MODE:
                flags = event.data.get("flags", 0)
                raw_mode = get_primary_mode(flags)
                unified = _MODE_MAP.get(raw_mode, raw_mode.lower())
                mode_changes.append(ModeChange(
                    timestamp_s=0.0,
                    mode_name=unified,
                    raw_mode=raw_mode,
                ))

        # ── 组装 FlightData ─────────────────────────
        duration_s = float(timestamps_s[-1]) if len(timestamps_s) > 1 else 0.0

        flight_data = FlightData(
            platform="betaflight",
            firmware_version=header.firmware_revision,
            board_name=header.board_info or "",
            log_file=str(path),
            sample_rate_hz=sample_rate_hz,
            duration_s=duration_s,
            pid=pid_data,
            gyro=gyro,
            accel=accel,
            imu_timestamp_s=timestamps_s,
            motor_output=motor_output,
            motor_timestamp_s=timestamps_s if motor_output is not None else None,
            mode_changes=mode_changes,
            params=params,
            extras={
                "craft_name": header.craft_name,
                "firmware_type": header.firmware_type,
                "data_version": header.data_version,
                "frame_count": n_frames,
                "i_frame_count": sum(1 for f in frames if f.frame_type == 'I'),
                "p_frame_count": sum(1 for f in frames if f.frame_type == 'P'),
                "event_count": len(segment.events),
            },
        )

        if motor_output is not None:
            n_motors = motor_output.shape[1]
            if n_motors <= 4:
                flight_data.frame_type = "quad"
            elif n_motors == 6:
                flight_data.frame_type = "hex"
            elif n_motors == 8:
                flight_data.frame_type = "octo"

        return flight_data

    # ── 参数映射 ────────────────────────────────────────────

    def map_param_to_platform(self, generic_name: str) -> str:
        return _PARAM_MAP_TO_PLATFORM.get(generic_name, generic_name)

    def map_param_to_generic(self, platform_name: str) -> str:
        return _PARAM_MAP_TO_GENERIC.get(platform_name, platform_name)

    # ── 能力 ────────────────────────────────────────────────

    def capabilities(self) -> Set[str]:
        return {"pid", "fft", "filter", "hardware", "quality"}
