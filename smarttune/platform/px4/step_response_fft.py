"""
阶跃响应估计 — PX4 占位模块。

PX4 的阶跃响应 FFT 算法尚未实现（待 Phase 3）。
本模块提供与 ardupilot / betaflight 一致的公共接口，
返回空结果以避免 pid_reviewer 分派时抛出 ModuleNotFoundError。

接口签名参见: smarttune/platform/step_response_protocol.py
"""

from typing import Any, Dict, Optional

import numpy as np


def estimate_step_response(
    target: np.ndarray,
    actual: np.ndarray,
    sample_rate: float,
    window_size: Optional[int] = None,
    step_duration_s: float = 0.5,
    min_target_amplitude: float = 20.0,
    cutfreq: float = 25.0,
) -> Dict[str, Any]:
    """PX4 阶跃响应估计 — 尚未实现，返回空结果。"""
    return {
        "time": np.array([0.0]),
        "step_response": np.array([0.0]),
        "error": "PX4 step response FFT not yet implemented",
        "valid_windows": 0,
        "total_windows": 0,
        "method": "px4_stub",
    }


def compute_step_response_for_axis(
    pid_data: Dict[str, np.ndarray],
    axis: str = "roll",
    imu_data: Optional[Dict[str, np.ndarray]] = None,
) -> Dict[str, Any]:
    """PX4 轴阶跃响应 — 尚未实现，返回空结果。"""
    return {
        "axis": axis,
        "time_s": [],
        "step_response": [],
        "info": {"error": "PX4 step response FFT not yet implemented",
                 "method": "px4_stub"},
    }
