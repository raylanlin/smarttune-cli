"""
PX4 滤波器传递函数 — 占位实现（Phase 3）

PX4 使用 IMU_GYRO_CUTOFF (gyro LPF) 和 IMU_DGYRO_CUTOFF (D 导数 LPF)，
没有与 ArduPilot INS_HNTCH 等价的多谐波陷波结构。
精确实现待 Phase 3 ULog 解析完成后补充。

对外接口（与其他平台一致）：
    derive_filters_from_params(params)  -> Dict
    compute_filter_response(...)        -> (mag_db, phase_deg)
    simulate_filtered_spectrum(...)     -> np.ndarray
    build_filter_display_lines(params)  -> List[str]
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from smarttune.analyzers.filter_transfer import (
    compute_filter_response as _compute_filter_response,
    simulate_filtered_spectrum as _simulate_filtered_spectrum,
)


def derive_filters_from_params(params: Dict[str, float]) -> Dict[str, Any]:
    """
    从 PX4 参数构建近似滤波器描述。

    PX4 相关参数：
        IMU_GYRO_CUTOFF    — 陀螺 LPF 截止（Hz）
        IMU_DGYRO_CUTOFF   — D 导数 LPF 截止（Hz）
    """
    lpf_hz = float(params.get("IMU_GYRO_CUTOFF", 0.0))
    d_lpf = float(params.get("IMU_DGYRO_CUTOFF", 0.0))

    parts = []
    if lpf_hz > 0:
        parts.append(f"Gyro LPF={lpf_hz:.0f} Hz")
    if d_lpf > 0:
        parts.append(f"D LPF={d_lpf:.0f} Hz")
    if not parts:
        parts.append("(no filters parsed)")

    return {
        "gyro_lpf_hz": lpf_hz,
        "notch_filters_approx": [],
        "config_summary": ", ".join(parts),
        "_px4_raw": {"IMU_GYRO_CUTOFF": lpf_hz, "IMU_DGYRO_CUTOFF": d_lpf},
    }


def compute_filter_response(
    freqs: np.ndarray,
    sample_rate: float,
    gyro_filter_hz: float = 0.0,
    notch_params: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, float]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """计算 PX4 近似滤波器响应（仅 gyro LPF）。"""
    if params is not None:
        cfg = derive_filters_from_params(params)
        return _compute_filter_response(freqs, sample_rate, cfg["gyro_lpf_hz"], None)
    return _compute_filter_response(freqs, sample_rate, gyro_filter_hz, notch_params)


def simulate_filtered_spectrum(
    freqs: np.ndarray,
    magnitudes: np.ndarray,
    sample_rate: float,
    gyro_filter_hz: float = 0.0,
    notch_params: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, float]] = None,
) -> np.ndarray:
    """模拟 PX4 近似滤波后的频谱。"""
    mag_db, _ = compute_filter_response(freqs, sample_rate, gyro_filter_hz, notch_params, params)
    return magnitudes + mag_db


def build_filter_display_lines(params: Dict[str, float]) -> List[str]:
    """构建 CLI 显示用的 PX4 滤波器配置文本行。"""
    lines: List[str] = []
    lpf_hz = params.get("IMU_GYRO_CUTOFF", 0.0)
    d_lpf = params.get("IMU_DGYRO_CUTOFF", 0.0)
    if lpf_hz > 0:
        lines.append(f"  Gyro LPF: IMU_GYRO_CUTOFF = {lpf_hz:.0f} Hz")
    if d_lpf > 0:
        lines.append(f"  D LPF: IMU_DGYRO_CUTOFF = {d_lpf:.0f} Hz")
    if not lines:
        lines.append("  (no PX4 filter parameters found — ULog parse not yet implemented)")
    return lines


def get_fallback_gyro_filter_hz(params: Dict[str, float]) -> float:
    return float(params.get("IMU_GYRO_CUTOFF", 0.0))


def get_notch_bandwidth_hz(params: Dict[str, float], notch_index: int = 1) -> float:
    return 20.0  # PX4 无固定陷波，返回默认值
