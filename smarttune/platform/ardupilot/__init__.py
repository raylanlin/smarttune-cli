"""
smarttune/platform/ardupilot/__init__.py

ArduPilot 平台适配器 — 将现有 ArduPilot 日志解析和参数映射
封装为 PlatformAdapter 接口。
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Set

import numpy as np

from smarttune.platform.base import PlatformAdapter
from smarttune.platform.registry import register
from smarttune.models.flight_data import AxisPIDSignal, FlightData, ModeChange
from smarttune.errors import LogFileNotFoundError, LogFileCorruptError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ArduPilot 参数映射表
# ---------------------------------------------------------------------------

_PARAM_MAP_TO_PLATFORM = {
    # PID gains
    "pid.roll.p":       "ATC_RAT_RLL_P",
    "pid.roll.i":       "ATC_RAT_RLL_I",
    "pid.roll.d":       "ATC_RAT_RLL_D",
    "pid.roll.ff":      "ATC_RAT_RLL_FF",
    "pid.roll.filt":    "ATC_RAT_RLL_FLTD",
    "pid.roll.filt_t":  "ATC_RAT_RLL_FLTT",
    "pid.pitch.p":      "ATC_RAT_PIT_P",
    "pid.pitch.i":      "ATC_RAT_PIT_I",
    "pid.pitch.d":      "ATC_RAT_PIT_D",
    "pid.pitch.ff":     "ATC_RAT_PIT_FF",
    "pid.pitch.filt":   "ATC_RAT_PIT_FLTD",
    "pid.pitch.filt_t": "ATC_RAT_PIT_FLTT",
    "pid.yaw.p":        "ATC_RAT_YAW_P",
    "pid.yaw.i":        "ATC_RAT_YAW_I",
    "pid.yaw.d":        "ATC_RAT_YAW_D",
    "pid.yaw.ff":       "ATC_RAT_YAW_FF",
    "pid.yaw.filt":     "ATC_RAT_YAW_FLTD",
    "pid.yaw.filt_t":   "ATC_RAT_YAW_FLTT",
    # Filters
    "filter.gyro_lpf":      "INS_GYRO_FILTER",
    "filter.accel_lpf":     "INS_ACCEL_FILTER",
    "filter.dterm_lpf":     "INS_ACCEL_FILTER",
    "filter.notch1.enable": "INS_HNTCH_ENABLE",
    "filter.notch1.freq":   "INS_HNTCH_FREQ",
    "filter.notch1.bw":     "INS_HNTCH_BW",
    "filter.notch1.att":    "INS_HNTCH_ATT",
    "filter.notch1.mode":   "INS_HNTCH_MODE",
    "filter.notch1.ref":    "INS_HNTCH_REF",
    "filter.notch1.hmc":    "INS_HNTCH_HMC",
    "filter.notch2.enable": "INS_HNTC2_ENABLE",
    "filter.notch2.freq":   "INS_HNTC2_FREQ",
    "filter.notch2.bw":     "INS_HNTC2_BW",
    "filter.notch2.att":    "INS_HNTC2_ATT",
    "filter.notch2.mode":   "INS_HNTC2_MODE",
    "filter.notch2.ref":    "INS_HNTC2_REF",
    "filter.notch2.hmc":    "INS_HNTC2_HMC",
    # Magnetometer
    "mag.ofs.x": "COMPASS_OFS_X",
    "mag.ofs.y": "COMPASS_OFS_Y",
    "mag.ofs.z": "COMPASS_OFS_Z",
}

# 反向映射
_PARAM_MAP_TO_GENERIC = {v: k for k, v in _PARAM_MAP_TO_PLATFORM.items()}

# ArduPilot 飞行模式 → 统一模式名
_MODE_MAP = {
    "STABILIZE": "stabilize",
    "ALT_HOLD":  "althold",
    "LOITER":    "loiter",
    "AUTO":      "auto",
    "GUIDED":    "guided",
    "LAND":      "land",
    "RTL":       "rtl",
    "ACRO":      "acro",
    "POSHOLD":   "poshold",
    "AUTOTUNE":  "autotune",
    "FLOWHOLD":  "flowhold",
}


# ---------------------------------------------------------------------------
# ArduPilot 日志检测 — magic bytes
# ---------------------------------------------------------------------------

# ArduPilot DataFlash binary log starts with 0xA3 0x95 (FMT header)
_ARDUPILOT_MAGIC = b"\xa3\x95"


@register
class ArduPilotAdapter(PlatformAdapter):
    """ArduPilot DataFlash 日志适配器。"""

    @property
    def name(self) -> str:
        return "ardupilot"

    @property
    def display_name(self) -> str:
        return "ArduPilot"

    @property
    def supported_extensions(self) -> list[str]:
        return [".bin", ".log"]

    # ── 检测 ────────────────────────────────────────────────

    @classmethod
    def detect(cls, path: Path) -> bool:
        """检测是否为 ArduPilot DataFlash 日志。"""
        if not path.is_file():
            return False

        suffix = path.suffix.lower()

        # .bin 文件: 检查 magic bytes
        if suffix == ".bin":
            try:
                with open(path, "rb") as f:
                    header = f.read(4)
                return len(header) >= 2 and header[:2] == _ARDUPILOT_MAGIC
            except (OSError, IOError):
                return False

        # .log 文件: 检查是否有 ArduPilot 文本日志特征
        if suffix == ".log":
            try:
                with open(path, "r", errors="ignore") as f:
                    head = f.read(1024)
                # ArduPilot text logs typically start with FMT or contain GPS/IMU lines
                return "FMT" in head or "IMU" in head or "PARM" in head
            except (OSError, IOError):
                return False

        return False

    # ── 解析 ────────────────────────────────────────────────

    def parse(self, path: Path) -> FlightData:
        """解析 ArduPilot DataFlash 日志 → FlightData。

        内部使用 pymavlink 进行单次遍历解析。
        """
        from pymavlink import mavutil

        if not path.is_file():
            raise LogFileNotFoundError(
                message=f"Log file not found: {path}",
                hint="Check the file path and ensure the file exists.",
            )

        try:
            mlog = mavutil.mavlink_connection(str(path))
        except Exception as exc:
            raise LogFileCorruptError(
                message=f"Cannot open log file: {exc}",
                hint="Ensure this is a valid ArduPilot DataFlash .bin file.",
            )

        # ── 单次遍历收集所有数据 ────────────────────────────
        pid_data = {"roll": [], "pitch": [], "yaw": []}
        imu_data = []
        mag_data = []
        mode_data = []
        params = {}

        # PID 消息类型映射
        pid_msg_map = {
            "PIDR": "roll",
            "PIDP": "pitch",
            "PIDY": "yaw",
        }

        while True:
            msg = mlog.recv_match()
            if msg is None:
                break

            msg_type = msg.get_type()

            # 参数
            if msg_type == "PARM":
                try:
                    params[msg.Name] = float(msg.Value)
                except (AttributeError, ValueError):
                    pass

            # PID 数据
            elif msg_type in pid_msg_map:
                axis = pid_msg_map[msg_type]
                try:
                    pid_data[axis].append({
                        "time": msg._timestamp,
                        "desired": getattr(msg, "Tar", getattr(msg, "Des", 0)),
                        "actual": getattr(msg, "Act", 0),
                        "p": getattr(msg, "P", 0),
                        "i": getattr(msg, "I", 0),
                        "d": getattr(msg, "D", 0),
                        "ff": getattr(msg, "FF", 0),
                    })
                except AttributeError:
                    pass

            # 如果没有 PIDx 消息，尝试 RATE 消息
            elif msg_type == "RATE":
                try:
                    t = msg._timestamp
                    for axis, des_field, act_field in [
                        ("roll",  "RDes", "R"),
                        ("pitch", "PDes", "P"),
                        ("yaw",   "YDes", "Y"),
                    ]:
                        pid_data[axis].append({
                            "time": t,
                            "desired": getattr(msg, des_field, 0),
                            "actual": getattr(msg, act_field, 0),
                        })
                except AttributeError:
                    pass

            # IMU 数据
            elif msg_type == "IMU":
                try:
                    imu_data.append({
                        "time": msg._timestamp,
                        "gx": msg.GyrX,
                        "gy": msg.GyrY,
                        "gz": msg.GyrZ,
                        "ax": msg.AccX,
                        "ay": msg.AccY,
                        "az": msg.AccZ,
                    })
                except AttributeError:
                    pass

            # 磁力计
            elif msg_type == "MAG":
                try:
                    mag_data.append({
                        "time": msg._timestamp,
                        "x": msg.MagX,
                        "y": msg.MagY,
                        "z": msg.MagZ,
                    })
                except AttributeError:
                    pass

            # 飞行模式
            elif msg_type == "MODE":
                try:
                    raw_mode = getattr(msg, "Mode", str(getattr(msg, "ModeNum", 0)))
                    mode_data.append({
                        "time": msg._timestamp,
                        "raw_mode": raw_mode,
                    })
                except AttributeError:
                    pass

        # ── 构建 FlightData ─────────────────────────────────

        flight_data = FlightData(
            platform="ardupilot",
            log_file=str(path),
            params=params,
        )

        # 固件版本
        flight_data.firmware_version = params.get("SYSID_SW_MREV", "")
        flight_data.board_name = ""

        # PID 信号
        for axis, records in pid_data.items():
            if len(records) < 10:
                continue
            ts = np.array([r["time"] for r in records])
            ts = ts - ts[0]  # 从 0 开始
            flight_data.pid[axis] = AxisPIDSignal(
                timestamp_s=ts,
                desired=np.array([r["desired"] for r in records]),
                actual=np.array([r["actual"] for r in records]),
                p_term=np.array([r.get("p", 0) for r in records]),
                i_term=np.array([r.get("i", 0) for r in records]),
                d_term=np.array([r.get("d", 0) for r in records]),
                ff_term=np.array([r.get("ff", 0) for r in records]),
            )

        # IMU 数据
        if len(imu_data) > 10:
            ts = np.array([r["time"] for r in imu_data])
            flight_data.imu_timestamp_s = ts - ts[0]
            flight_data.gyro = np.column_stack([
                [r["gx"] for r in imu_data],
                [r["gy"] for r in imu_data],
                [r["gz"] for r in imu_data],
            ])
            flight_data.accel = np.column_stack([
                [r["ax"] for r in imu_data],
                [r["ay"] for r in imu_data],
                [r["az"] for r in imu_data],
            ])

        # 磁力计
        if len(mag_data) > 10:
            ts = np.array([r["time"] for r in mag_data])
            flight_data.mag_timestamp_s = ts - ts[0]
            flight_data.mag = np.column_stack([
                [r["x"] for r in mag_data],
                [r["y"] for r in mag_data],
                [r["z"] for r in mag_data],
            ])

        # 飞行模式
        for md in mode_data:
            raw = str(md["raw_mode"])
            unified = _MODE_MAP.get(raw.upper(), raw.lower())
            flight_data.mode_changes.append(ModeChange(
                timestamp_s=md["time"],
                mode_name=unified,
                raw_mode=raw,
            ))

        # 采样率和时长
        if flight_data.pid:
            first_axis = next(iter(flight_data.pid.values()))
            flight_data.duration_s = first_axis.duration_s
            if first_axis.sample_count > 1:
                dt = np.median(np.diff(first_axis.timestamp_s))
                flight_data.sample_rate_hz = 1.0 / dt if dt > 0 else 0
        elif flight_data.gyro is not None and flight_data.imu_timestamp_s is not None:
            flight_data.duration_s = float(
                flight_data.imu_timestamp_s[-1] - flight_data.imu_timestamp_s[0]
            )
            dt = np.median(np.diff(flight_data.imu_timestamp_s))
            flight_data.sample_rate_hz = 1.0 / dt if dt > 0 else 0

        return flight_data

    # ── 参数映射 ────────────────────────────────────────────

    def map_param_to_platform(self, generic_name: str) -> str:
        return _PARAM_MAP_TO_PLATFORM.get(generic_name, generic_name)

    def map_param_to_generic(self, platform_name: str) -> str:
        return _PARAM_MAP_TO_GENERIC.get(platform_name, platform_name)

    # ── 能力 ────────────────────────────────────────────────

    def capabilities(self) -> Set[str]:
        return {"pid", "fft", "filter", "sysid", "magfit", "hardware", "quality"}
