"""
smarttune/platform/px4/__init__.py

PX4 ULog 日志适配器 — 完整实现（基于 pyulog）。

uORB 主题映射：
  vehicle_angular_velocity  → PID actual (rad/s → deg/s) + gyro
  vehicle_rates_setpoint    → PID desired (rad/s → deg/s)
  sensor_combined           → gyro/accel 回退源
  vehicle_acceleration      → accel (m/s²)
  sensor_mag                → mag (Gauss → mGauss)
  actuator_motors           → motor_output (0~1，PX4 v1.14+)
  actuator_outputs          → motor_output 回退（PWM → 归一化）
  battery_status            → voltage / current
  vehicle_gps_position /
  sensor_gps                → extras["gps_position"]（lat/lon 1e7 缩放）
  vehicle_attitude          → extras["attitude_quat"]（四元数）

参数契约（A2）：parse() 会把 MC_ROLLRATE_P 等平台参数同时以
generic key（"pid.roll.p"）注入 FlightData.params，
供 PIDReviewer._get_current_pid 直接消费。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import numpy as np

from smarttune.platform.base import PlatformAdapter
from smarttune.platform.registry import register
from smarttune.models.flight_data import AxisPIDSignal, FlightData
from smarttune.errors import (
    LogFileNotFoundError,
    LogFileCorruptError,
    ParseError,
    InsufficientPIDDataError,
    SmartTuneError,
)

logger = logging.getLogger(__name__)

_PARAM_MAP_TO_PLATFORM = {
    "pid.roll.p": "MC_ROLLRATE_P",
    "pid.roll.i": "MC_ROLLRATE_I",
    "pid.roll.d": "MC_ROLLRATE_D",
    "pid.roll.ff": "MC_ROLLRATE_FF",
    "pid.pitch.p": "MC_PITCHRATE_P",
    "pid.pitch.i": "MC_PITCHRATE_I",
    "pid.pitch.d": "MC_PITCHRATE_D",
    "pid.pitch.ff": "MC_PITCHRATE_FF",
    "pid.yaw.p": "MC_YAWRATE_P",
    "pid.yaw.i": "MC_YAWRATE_I",
    "pid.yaw.d": "MC_YAWRATE_D",
    "pid.yaw.ff": "MC_YAWRATE_FF",
    "filter.gyro_lpf": "IMU_GYRO_CUTOFF",
    "filter.dterm_lpf": "IMU_DGYRO_CUTOFF",
    "filter.accel_lpf": "IMU_ACCEL_CUTOFF",
    # 静态陷波（PX4 无 mode/REF/HMC/ATT 概念，FFT 分析器已按平台分支
    # 只输出 freq/bw；enable 无独立参数 — NF0_FRQ=0 即禁用）
    "filter.notch1.freq": "IMU_GYRO_NF0_FRQ",
    "filter.notch1.bw": "IMU_GYRO_NF0_BW",
    "filter.notch2.freq": "IMU_GYRO_NF1_FRQ",
    "filter.notch2.bw": "IMU_GYRO_NF1_BW",
}

_PARAM_MAP_TO_GENERIC = {v: k for k, v in _PARAM_MAP_TO_PLATFORM.items()}

# ULog magic: "ULog" 0x01 0x12 0x35
_ULOG_MAGIC = b"ULog\x01\x12\x35"

_RAD2DEG = 180.0 / np.pi


def _import_pyulog():
    try:
        from pyulog import ULog  # type: ignore

        return ULog
    except ImportError:
        raise SmartTuneError(
            code="E9002",
            message="pyulog is required for PX4 ULog parsing but is not installed",
            hint="Install it with: pip install pyulog",
        )


def _get_dataset(ulog, name: str, multi_id: int = 0):
    """从 ULog 取指定主题的数据集，不存在返回 None。"""
    for d in ulog.data_list:
        if d.name == name and d.multi_id == multi_id:
            return d
    return None


def _ts_seconds(data: Dict[str, np.ndarray], t0_us: float) -> np.ndarray:
    """timestamp(µs) → 相对秒。"""
    return (data["timestamp"].astype(np.float64) - t0_us) * 1e-6


@register
class PX4Adapter(PlatformAdapter):
    """PX4 ULog 日志适配器（pyulog 后端）。"""

    @property
    def name(self) -> str:
        return "px4"

    @property
    def display_name(self) -> str:
        return "PX4"

    @property
    def supported_extensions(self) -> list[str]:
        return [".ulg", ".ulog"]

    @classmethod
    def detect(cls, path: Path) -> bool:
        if not path.is_file():
            return False
        if path.suffix.lower() not in (".ulg", ".ulog"):
            return False
        try:
            with open(path, "rb") as f:
                header = f.read(7)
            return header == _ULOG_MAGIC
        except (OSError, IOError):
            return False

    # ── 解析 ────────────────────────────────────────────────

    def parse(self, path: Path) -> FlightData:
        ULog = _import_pyulog()

        if not path.is_file():
            raise LogFileNotFoundError(
                message=f"Log file not found: {path}",
                hint="Check the file path and ensure the file exists.",
            )

        try:
            ulog = ULog(str(path))
        except Exception as exc:
            raise LogFileCorruptError(
                message=f"ULog parse failed: {exc}",
                hint="The file may be corrupted or not a valid ULog.",
            )

        # ── 参数：平台名 + generic key 双写（A2 契约）─────────
        params: Dict[str, float] = {}
        for k, v in (ulog.initial_parameters or {}).items():
            try:
                params[k] = float(v)
            except (TypeError, ValueError):
                pass
        for generic, plat in _PARAM_MAP_TO_PLATFORM.items():
            if plat in params:
                params[generic] = params[plat]

        # ── 角速度（actual / gyro）────────────────────────────
        d_angvel = _get_dataset(ulog, "vehicle_angular_velocity")
        d_sensor = _get_dataset(ulog, "sensor_combined")

        if d_angvel is not None and "xyz[0]" in d_angvel.data:
            src = d_angvel.data
            gyro_keys = ("xyz[0]", "xyz[1]", "xyz[2]")
        elif d_sensor is not None and "gyro_rad[0]" in d_sensor.data:
            src = d_sensor.data
            gyro_keys = ("gyro_rad[0]", "gyro_rad[1]", "gyro_rad[2]")
        else:
            raise ParseError(
                message="No angular velocity topic in ULog "
                "(vehicle_angular_velocity / sensor_combined)",
                hint="Ensure the log was recorded with default PX4 logger topics.",
            )

        t0_us = float(src["timestamp"][0])
        t_gyro = _ts_seconds(src, t0_us)
        gyro = np.column_stack(
            [
                src[gyro_keys[0]].astype(np.float64) * _RAD2DEG,
                src[gyro_keys[1]].astype(np.float64) * _RAD2DEG,
                src[gyro_keys[2]].astype(np.float64) * _RAD2DEG,
            ]
        )

        if len(t_gyro) < 100:
            raise InsufficientPIDDataError(
                message=f"Too few angular velocity samples ({len(t_gyro)})",
                hint="The flight recording may have been too short.",
            )

        # ── 角速率 setpoint（desired）→ 插值到 gyro 时间轴 ────
        d_sp = _get_dataset(ulog, "vehicle_rates_setpoint")
        pid_data: Dict[str, AxisPIDSignal] = {}
        if d_sp is not None and "roll" in d_sp.data:
            t_sp = _ts_seconds(d_sp.data, t0_us)
            for axis, key, col in (("roll", "roll", 0), ("pitch", "pitch", 1), ("yaw", "yaw", 2)):
                sp_deg = d_sp.data[key].astype(np.float64) * _RAD2DEG
                desired = np.interp(t_gyro, t_sp, sp_deg)
                pid_data[axis] = AxisPIDSignal(
                    timestamp_s=t_gyro.copy(),
                    desired=desired,
                    actual=gyro[:, col].copy(),
                )
        else:
            logger.warning(
                "vehicle_rates_setpoint not logged — PID step analysis unavailable "
                "(FFT/quality still work)"
            )

        # ── 加速度 ───────────────────────────────────────────
        accel = None
        d_acc = _get_dataset(ulog, "vehicle_acceleration")
        if d_acc is not None and "xyz[0]" in d_acc.data:
            accel = np.column_stack(
                [
                    d_acc.data["xyz[0]"],
                    d_acc.data["xyz[1]"],
                    d_acc.data["xyz[2]"],
                ]
            ).astype(np.float64)
        elif d_sensor is not None and "accelerometer_m_s2[0]" in d_sensor.data:
            accel = np.column_stack(
                [
                    d_sensor.data["accelerometer_m_s2[0]"],
                    d_sensor.data["accelerometer_m_s2[1]"],
                    d_sensor.data["accelerometer_m_s2[2]"],
                ]
            ).astype(np.float64)

        # ── 磁力计（Gauss → mGauss，对齐 FlightData 单位契约）──
        mag = None
        mag_ts = None
        d_mag = _get_dataset(ulog, "sensor_mag")
        if d_mag is not None and "x" in d_mag.data:
            mag = (
                np.column_stack(
                    [
                        d_mag.data["x"],
                        d_mag.data["y"],
                        d_mag.data["z"],
                    ]
                ).astype(np.float64)
                * 1000.0
            )
            mag_ts = _ts_seconds(d_mag.data, t0_us)
        elif d_sensor is not None and "magnetometer_ga[0]" in d_sensor.data:
            # 老固件（≤v1.8）无独立 sensor_mag 主题，磁力计在
            # sensor_combined.magnetometer_ga（已用 pyulog sample.ulg 验证）
            mag = (
                np.column_stack(
                    [
                        d_sensor.data["magnetometer_ga[0]"],
                        d_sensor.data["magnetometer_ga[1]"],
                        d_sensor.data["magnetometer_ga[2]"],
                    ]
                ).astype(np.float64)
                * 1000.0
            )
            mag_ts = _ts_seconds(d_sensor.data, t0_us)

        # ── 电机输出 ─────────────────────────────────────────
        motor_output = None
        motor_ts = None
        d_motors = _get_dataset(ulog, "actuator_motors")
        if d_motors is not None:
            cols = [k for k in sorted(d_motors.data.keys()) if k.startswith("control[")]
            arrs = [d_motors.data[c].astype(np.float64) for c in cols]
            arrs = [a for a in arrs if np.any(np.isfinite(a)) and np.nanmax(np.abs(a)) > 0]
            if arrs:
                motor_output = np.clip(np.nan_to_num(np.column_stack(arrs)), 0.0, 1.0)
                motor_ts = _ts_seconds(d_motors.data, t0_us)
        if motor_output is None:
            d_out = _get_dataset(ulog, "actuator_outputs")
            if d_out is not None:
                cols = [k for k in sorted(d_out.data.keys()) if k.startswith("output[")]
                arrs = []
                for c in cols:
                    a = d_out.data[c].astype(np.float64)
                    if np.nanmax(a) > 800:  # 活跃 PWM 通道
                        arrs.append(np.clip((a - 1000.0) / 1000.0, 0.0, 1.0))
                if arrs:
                    motor_output = np.column_stack(arrs)
                    motor_ts = _ts_seconds(d_out.data, t0_us)

        # ── 电池 ─────────────────────────────────────────────
        batt_v = batt_a = batt_ts = None
        d_batt = _get_dataset(ulog, "battery_status")
        if d_batt is not None and "voltage_v" in d_batt.data:
            batt_v = d_batt.data["voltage_v"].astype(np.float64)
            if "current_a" in d_batt.data:
                batt_a = d_batt.data["current_a"].astype(np.float64)
            batt_ts = _ts_seconds(d_batt.data, t0_us)

        # ── GPS / 姿态 → extras ──────────────────────────────
        extras: Dict[str, Any] = {"ulog_msg_count": len(ulog.data_list)}

        for gps_topic in ("vehicle_gps_position", "sensor_gps"):
            d_gps = _get_dataset(ulog, gps_topic)
            if d_gps is not None and "lat" in d_gps.data and len(d_gps.data["lat"]) > 0:
                lat_arr = d_gps.data["lat"].astype(np.float64)
                lon_arr = d_gps.data["lon"].astype(np.float64)
                scale = 1e-7 if np.nanmax(np.abs(lat_arr)) > 1000 else 1.0
                mid = len(lat_arr) // 2
                alt_m = 0.0
                if "alt" in d_gps.data:
                    alt_raw = float(d_gps.data["alt"][mid])
                    alt_m = alt_raw * 1e-3 if abs(alt_raw) > 100000 else alt_raw
                extras["gps_position"] = {
                    "lat": float(lat_arr[mid]) * scale,
                    "lon": float(lon_arr[mid]) * scale,
                    "alt": alt_m,
                }
                break

        d_att = _get_dataset(ulog, "vehicle_attitude")
        if d_att is not None and "q[0]" in d_att.data:
            extras["attitude_quat"] = {
                "time": _ts_seconds(d_att.data, t0_us),
                "q": np.column_stack(
                    [
                        d_att.data["q[0]"],
                        d_att.data["q[1]"],
                        d_att.data["q[2]"],
                        d_att.data["q[3]"],
                    ]
                ).astype(np.float64),
            }

        # ── 采样率 / 时长 ─────────────────────────────────────
        dts = np.diff(t_gyro)
        dts = dts[dts > 1e-6]
        sample_rate_hz = 1.0 / float(np.median(dts)) if len(dts) else 0.0
        duration_s = float(t_gyro[-1] - t_gyro[0]) if len(t_gyro) > 1 else 0.0

        sys_name = ""
        try:
            sys_name = ulog.msg_info_dict.get("sys_name", "")
            fw = ulog.msg_info_dict.get("ver_sw", "")
        except Exception:
            fw = ""

        return FlightData(
            platform="px4",
            firmware_version=str(fw),
            board_name=str(sys_name) or None,
            log_file=str(path),
            sample_rate_hz=sample_rate_hz,
            duration_s=duration_s,
            pid=pid_data,
            gyro=gyro,
            accel=accel,
            imu_timestamp_s=t_gyro,
            mag=mag,
            mag_timestamp_s=mag_ts,
            motor_output=motor_output,
            motor_timestamp_s=motor_ts,
            battery_voltage=batt_v,
            battery_current=batt_a,
            battery_timestamp_s=batt_ts,
            params=params,
            extras=extras,
        )

    # ── 参数映射 ────────────────────────────────────────────

    def map_param_to_platform(self, generic_name: str) -> str:
        return _PARAM_MAP_TO_PLATFORM.get(generic_name, generic_name)

    def map_param_to_generic(self, platform_name: str) -> str:
        return _PARAM_MAP_TO_GENERIC.get(platform_name, platform_name)

    # ── 能力（诚实集合：filter/magfit/hardware 需平台特化模块，未实现）──

    def capabilities(self) -> Set[str]:
        return {"pid", "fft", "sysid", "quality"}

    def param_table(self):
        from smarttune.platform.params import ParamTable

        return ParamTable.from_knowledge("px4")
