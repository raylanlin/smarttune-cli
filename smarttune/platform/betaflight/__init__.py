"""
smarttune/platform/betaflight/__init__.py

Betaflight Blackbox 日志适配器 — BBL/BFL 格式。

Phase 2: 完整 BBL 解析器实现。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Set

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
    # PID gains (BF 4.5+ parameter names)
    "pid.roll.p":    "p_roll",
    "pid.roll.i":    "i_roll",
    "pid.roll.d":    "d_roll",
    "pid.roll.ff":   "f_roll",
    "pid.pitch.p":   "p_pitch",
    "pid.pitch.i":   "i_pitch",
    "pid.pitch.d":   "d_pitch",
    "pid.pitch.ff":  "f_pitch",
    "pid.yaw.p":     "p_yaw",
    "pid.yaw.i":     "i_yaw",
    "pid.yaw.d":     "d_yaw",
    "pid.yaw.ff":    "f_yaw",
    # BF-specific (BF 4.5+ renamed d_min → d_max)
    "pid.roll.d_min":    "d_max_roll",
    "pid.pitch.d_min":   "d_max_pitch",
    "pid.yaw.d_min":     "d_max_yaw",
    "pid.anti_gravity":  "anti_gravity_gain",
    "pid.feedforward_trans": "feedforward_transition",
    "pid.iterm_relax":   "iterm_relax_cutoff",
    # Filters (BF 4.5+ naming: gyro_lowpass_hz → gyro_lpf1_static_hz)
    "filter.gyro_lpf":      "gyro_lpf1_static_hz",
    "filter.gyro_lpf2":     "gyro_lpf2_static_hz",
    "filter.dterm_lpf":     "dterm_lpf1_static_hz",
    "filter.dterm_lpf2":    "dterm_lpf2_static_hz",
    "filter.notch1.freq":   "gyro_notch1_hz",
    "filter.notch1.bw":     "gyro_notch1_cutoff",
    "filter.notch2.freq":   "gyro_notch2_hz",
    "filter.notch2.bw":     "gyro_notch2_cutoff",
    # Dynamic notch / RPM filter
    "filter.dyn_notch_count": "dyn_notch_count",
    "filter.dyn_notch_q":    "dyn_notch_q",
    "filter.dyn_notch_min":  "dyn_notch_min_hz",
    "filter.dyn_notch_max":  "dyn_notch_max_hz",
    "filter.rpm_harmonics":  "rpm_filter_harmonics",
    "filter.rpm_min":        "rpm_filter_min_hz",
}

# Old-format fallback names (for reading logs with older BF firmware)
_PARAM_MAP_TO_PLATFORM_LEGACY = {
    "pid.roll.p": "pid_roll_p", "pid.roll.i": "pid_roll_i",
    "pid.roll.d": "pid_roll_d", "pid.roll.ff": "pid_roll_f",
    "pid.pitch.p": "pid_pitch_p", "pid.pitch.i": "pid_pitch_i",
    "pid.pitch.d": "pid_pitch_d", "pid.pitch.ff": "pid_pitch_f",
    "pid.yaw.p": "pid_yaw_p", "pid.yaw.i": "pid_yaw_i",
    "pid.yaw.d": "pid_yaw_d", "pid.yaw.ff": "pid_yaw_f",
    "filter.gyro_lpf": "gyro_lowpass_hz",
    "filter.gyro_lpf2": "gyro_lowpass2_hz",
    "filter.dterm_lpf": "dterm_lowpass_hz",
}

_PARAM_MAP_TO_GENERIC = {v: k for k, v in _PARAM_MAP_TO_PLATFORM.items()}
# Merge legacy map into reverse for backward compat
_PARAM_MAP_TO_GENERIC.update({v: k for k, v in _PARAM_MAP_TO_PLATFORM_LEGACY.items()})

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


def _sanitize_signal(arr: np.ndarray, max_abs: float = 2000.0) -> np.ndarray:
    """清洗 BBL 解析异常值。

    BBL P-frame 差值编码在帧丢失/损坏时会产生极端跳变值。
    将超出 ±max_abs 范围的点替换为前后邻值的线性插值。

    Parameters
    ----------
    arr : np.ndarray
        原始信号（deg/s 或其他物理量）。
    max_abs : float
        合理范围上界（默认 2000，适用于陀螺仪/setpoint deg/s）。

    Returns
    -------
    np.ndarray
        清洗后的信号（原数组不被修改）。
    """
    out = arr.copy()
    bad_mask = np.abs(out) > max_abs
    n_bad = int(np.sum(bad_mask))
    if n_bad == 0:
        return out

    # 用线性插值替换异常点
    good_mask = ~bad_mask
    good_idx = np.where(good_mask)[0]
    if len(good_idx) < 2:
        # 几乎全坏，返回零
        out[bad_mask] = 0.0
        return out

    bad_idx = np.where(bad_mask)[0]
    out[bad_idx] = np.interp(bad_idx, good_idx, out[good_idx])

    logger.debug(
        "Sanitized %d outlier samples (%.2f%%) from signal (max_abs=%.0f)",
        n_bad, n_bad / len(arr) * 100, max_abs,
    )
    return out


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
            parse_bbl_columnar, get_primary_mode,
            EVENT_FLIGHT_MODE, BBLColumnarSegment,
            FRAME_TYPE_I, FRAME_TYPE_P,
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

        # 解析 BBL 数据 — memory-efficient columnar path
        try:
            segments = parse_bbl_columnar(data, max_segments=10)
        except Exception as exc:
            raise ParseError(
                message=f"BBL parse failed: {exc}",
                hint="The log file may be corrupted or use an unsupported format version.",
            )

        # Free the raw bytes now that parsing is done
        del data

        if not segments:
            raise ParseError(
                message="No valid log segment found",
                hint="The log file may be empty or corrupted.",
            )

        # Merge columns from all segments
        header = segments[0].header
        all_events = []
        column_chunks: List[BBLColumnarSegment] = []
        total_frames = 0

        for seg in segments:
            if seg.n_frames > 0:
                column_chunks.append(seg)
                total_frames += seg.n_frames
            all_events.extend(seg.events)

        if total_frames < 10:
            raise InsufficientPIDDataError(
                message=f"Too few frames in log ({total_frames})",
                hint="The flight recording may have been too short.",
            )

        # Concatenate columns from all segments into single arrays
        if len(column_chunks) == 1:
            merged_columns = column_chunks[0].columns
            merged_ft = column_chunks[0].frame_types
            available_fields = column_chunks[0].available_fields
        else:
            available_fields = column_chunks[0].available_fields
            merged_columns = {}
            for name in column_chunks[0].field_names:
                merged_columns[name] = np.concatenate(
                    [seg.columns[name] for seg in column_chunks if name in seg.columns]
                )
            merged_ft = np.concatenate([seg.frame_types for seg in column_chunks])

        n_frames = total_frames

        # ── 提取参数 (从头信息) ────────────────────
        params: Dict[str, float] = {}
        for key, value in header.properties.items():
            try:
                params[key] = float(value)
            except (ValueError, TypeError):
                pass

        # A2 契约：注入 generic key（pid.roll.p 等）供平台无关分析器读取当前值。
        # 兼顾 BF 4.5+ 新名（p_roll）与旧固件名（pid_roll_p）。旧实现只存原生名，
        # 导致 PIDReviewer._get_current_pid 恒返回 0.0，叠加 C4 后 PID 建议被全丢弃。
        for _gmap in (_PARAM_MAP_TO_PLATFORM, _PARAM_MAP_TO_PLATFORM_LEGACY):
            for _generic, _plat in _gmap.items():
                if _plat in params and _generic not in params:
                    params[_generic] = params[_plat]

        # ── 计算时间序列 ────────────────────────────
        loop_rate_hz = params.get("looptime", 250)
        if loop_rate_hz > 100:
            sample_rate_hz = 1_000_000.0 / loop_rate_hz
        else:
            sample_rate_hz = loop_rate_hz

        # Account for P interval
        p_interval_str = header.properties.get("P interval", "")
        if '/' in p_interval_str:
            try:
                parts = p_interval_str.split('/')
                p_num = int(parts[0])
                p_denom = int(parts[1])
                if p_denom > 0 and p_num > 0:
                    sample_rate_hz = sample_rate_hz * p_num / p_denom
            except (ValueError, IndexError):
                pass
        elif p_interval_str.strip():
            try:
                p_denom = int(p_interval_str.strip())
                if p_denom > 1:
                    sample_rate_hz = sample_rate_hz / p_denom
            except ValueError:
                pass

        dt_s = 1.0 / sample_rate_hz
        timestamps_s = np.arange(n_frames, dtype=np.float64) * dt_s

        # ── Helper to get a column as float64 ──────────
        def _col_f64(name: str) -> Optional[np.ndarray]:
            arr = merged_columns.get(name)  # noqa: F821  # closure over parse()'s local; del'd only after use
            if arr is not None:
                return arr.astype(np.float64)
            return None

        # ── 提取 PID 信号 ──────────────────────────
        pid_data: Dict[str, AxisPIDSignal] = {}

        for axis in ["roll", "pitch", "yaw"]:
            gyro_name = _resolve_field_name(available_fields, _GYRO_FIELD_NAMES[axis])
            sp_name = _resolve_field_name(available_fields, _SETPOINT_FIELD_NAMES[axis])

            if gyro_name is None and sp_name is None:
                continue

            actual = _col_f64(gyro_name) if gyro_name else np.zeros(n_frames)
            desired = _col_f64(sp_name) if sp_name else np.zeros(n_frames)

            rate_limit = 2000.0
            try:
                rl_str = header.properties.get("rate_limits", "1998,1998,1998")
                rl_vals = [int(x) for x in rl_str.split(',')]
                axis_idx = {"roll": 0, "pitch": 1, "yaw": 2}[axis]
                if axis_idx < len(rl_vals):
                    rate_limit = float(rl_vals[axis_idx]) * 1.1
            except (ValueError, IndexError):
                pass
            actual = _sanitize_signal(actual, max_abs=rate_limit)
            desired = _sanitize_signal(desired, max_abs=rate_limit)

            p_name = _resolve_field_name(available_fields, _PID_P_FIELDS[axis])
            i_name = _resolve_field_name(available_fields, _PID_I_FIELDS[axis])
            d_name = _resolve_field_name(available_fields, _PID_D_FIELDS[axis])
            f_name = _resolve_field_name(available_fields, _PID_F_FIELDS[axis])

            p_term = _sanitize_signal(_col_f64(p_name), max_abs=2000.0) if p_name else None
            i_term = _sanitize_signal(_col_f64(i_name), max_abs=2000.0) if i_name else None
            d_term = _sanitize_signal(_col_f64(d_name), max_abs=2000.0) if d_name else None
            ff_term = _sanitize_signal(_col_f64(f_name), max_abs=2000.0) if f_name else None

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
            gx = _sanitize_signal(_col_f64(gyro_x_name), max_abs=2000.0)
            gy = _sanitize_signal(_col_f64(gyro_y_name), max_abs=2000.0)
            gz = _sanitize_signal(_col_f64(gyro_z_name), max_abs=2000.0)
            gyro = np.column_stack([gx, gy, gz])
            del gx, gy, gz  # free intermediates

        # ── 提取加速度 ─────────────────────────────
        acc_x_name = _resolve_field_name(available_fields, _ACCEL_FIELDS["x"])
        acc_y_name = _resolve_field_name(available_fields, _ACCEL_FIELDS["y"])
        acc_z_name = _resolve_field_name(available_fields, _ACCEL_FIELDS["z"])

        accel = None
        if acc_x_name and acc_y_name and acc_z_name:
            ax = _col_f64(acc_x_name)
            ay = _col_f64(acc_y_name)
            az = _col_f64(acc_z_name)
            acc_1g_raw = params.get("acc_1G", 256)
            try:
                acc_1g_raw = int(acc_1g_raw)
            except (ValueError, TypeError):
                acc_1g_raw = 256
            accel = np.column_stack([ax, ay, az]) * (9.80665 / acc_1g_raw)
            del ax, ay, az

        # ── 提取电机输出 ────────────────────────────
        motor_output = None
        motor_names = [m for m in _MOTOR_FIELDS if m in available_fields]
        if motor_names:
            motor_cols = [_col_f64(mn) for mn in motor_names]
            motor_raw = np.column_stack(motor_cols)
            del motor_cols
            minthrottle = params.get("minthrottle", 1070)
            maxthrottle = params.get("maxthrottle", 2000)
            throttle_range = maxthrottle - minthrottle
            if throttle_range > 0:
                motor_output = np.clip(
                    (motor_raw - minthrottle) / throttle_range, 0.0, 1.0)
            else:
                motor_output = motor_raw
            del motor_raw

        # Free the merged columns now that we've extracted everything
        i_frame_count = int(np.sum(merged_ft == FRAME_TYPE_I))
        p_frame_count = int(np.sum(merged_ft == FRAME_TYPE_P))
        del merged_columns, merged_ft

        # ── 提取飞行模式 ──────────────────────────
        mode_changes: List[ModeChange] = []
        for event in all_events:
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
                "i_frame_count": i_frame_count,
                "p_frame_count": p_frame_count,
                "event_count": len(all_events),
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

    def param_table(self):
        from smarttune.platform.params import ParamTable
        return ParamTable.from_knowledge("betaflight")
