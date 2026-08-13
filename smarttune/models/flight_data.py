"""
smarttune/models/flight_data.py

统一飞行数据中间表示 — 所有平台的日志解析器都输出这个结构，
所有分析引擎都只消费这个结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class AxisPIDSignal:
    """单轴 PID 信号 — 最大公约数抽象。

    所有平台（ArduPilot / Betaflight / PX4）都有 desired 和 actual，
    P/I/D/FF 项可选（部分平台日志不一定全部记录）。
    """

    timestamp_s: np.ndarray  # 秒，从日志起始计
    desired: np.ndarray  # 目标角速率, deg/s
    actual: np.ndarray  # 实际角速率, deg/s
    p_term: Optional[np.ndarray] = None
    i_term: Optional[np.ndarray] = None
    d_term: Optional[np.ndarray] = None
    ff_term: Optional[np.ndarray] = None  # Betaflight feedforward / ArduPilot FF
    output: Optional[np.ndarray] = None  # 控制器总输出

    @property
    def sample_count(self) -> int:
        return len(self.timestamp_s)

    @property
    def duration_s(self) -> float:
        if self.sample_count < 2:
            return 0.0
        return float(self.timestamp_s[-1] - self.timestamp_s[0])


@dataclass
class ModeChange:
    """飞行模式切换事件。

    raw_mode 保留平台原始名称（如 ArduPilot "STABILIZE"、BF "ANGLE"、PX4 "Position"），
    mode_name 映射到统一命名，用于分析引擎切分飞行段。
    """

    timestamp_s: float
    mode_name: str  # 统一: "stabilize", "acro", "althold", "loiter", "auto", "land", ...
    raw_mode: str  # 平台原始名


@dataclass
class FlightData:
    """统一飞行数据结构 — 分析引擎的唯一输入。

    设计原则：
    - 必选字段 = 所有平台都能提供的最大公约数
    - 可选字段 = 部分平台可能没有（如 BF 无磁力计）
    - extras = 平台特有数据的逃逸舱，由平台特化规则消费

    单位约定：
    - 角度/角速率: deg, deg/s
    - 加速度: m/s²
    - 磁场: mGauss
    - 电压: V
    - 电流: A
    - 电机输出: 0.0 ~ 1.0 归一化
    """

    # ── 元信息 ──────────────────────────────────────────────
    platform: str  # "ardupilot" | "betaflight" | "px4"
    firmware_version: str = ""
    frame_type: Optional[str] = None  # "quad", "hex", "octo", "tri", "heli", ...
    board_name: Optional[str] = None  # 飞控板型号
    log_file: str = ""  # 原始日志路径

    # ── 采样信息 ────────────────────────────────────────────
    sample_rate_hz: float = 0.0  # 主循环采样率
    duration_s: float = 0.0  # 总时长

    # ── PID 信号（必选）──────────────────────────────────────
    pid: Dict[str, AxisPIDSignal] = field(default_factory=dict)
    # keys: "roll", "pitch", "yaw"

    # ── IMU 原始数据（必选）──────────────────────────────────
    gyro: Optional[np.ndarray] = None  # (N, 3), deg/s
    accel: Optional[np.ndarray] = None  # (N, 3), m/s²
    imu_timestamp_s: Optional[np.ndarray] = None  # (N,)

    # ── 可选信号 ────────────────────────────────────────────
    mag: Optional[np.ndarray] = None  # (N, 3), mGauss
    mag_timestamp_s: Optional[np.ndarray] = None
    baro_alt: Optional[np.ndarray] = None  # (N,), meters
    motor_output: Optional[np.ndarray] = None  # (N, num_motors), 0-1
    motor_timestamp_s: Optional[np.ndarray] = None
    battery_voltage: Optional[np.ndarray] = None  # (N,), Volts
    battery_current: Optional[np.ndarray] = None  # (N,), Amps
    battery_timestamp_s: Optional[np.ndarray] = None

    # ── 飞行模式 ────────────────────────────────────────────
    mode_changes: List[ModeChange] = field(default_factory=list)

    # ── 参数快照（日志中通常有当前参数值）─────────────────────
    params: Dict[str, float] = field(default_factory=dict)

    # ── 平台特有扩展 ────────────────────────────────────────
    extras: Dict[str, Any] = field(default_factory=dict)

    # ── 便利方法 ────────────────────────────────────────────

    @property
    def axes(self) -> List[str]:
        """已有 PID 数据的轴列表。"""
        return sorted(self.pid.keys())

    @property
    def has_mag(self) -> bool:
        return self.mag is not None and len(self.mag) > 0

    @property
    def has_motor(self) -> bool:
        return self.motor_output is not None and len(self.motor_output) > 0

    @property
    def has_battery(self) -> bool:
        return self.battery_voltage is not None and len(self.battery_voltage) > 0

    def validate(self) -> List[str]:
        """验证数据完整性，返回问题列表（空列表 = 通过）。"""
        issues = []
        if not self.pid:
            issues.append("No PID data available")
        for axis, sig in self.pid.items():
            if sig.sample_count < 100:
                issues.append(f"PID {axis}: insufficient samples ({sig.sample_count})")
        if self.gyro is None or len(self.gyro) < 100:
            issues.append("Insufficient gyro data")
        if self.accel is None or len(self.accel) < 100:
            issues.append("Insufficient accel data")
        return issues
