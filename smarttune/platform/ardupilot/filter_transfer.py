"""
ArduPilot 滤波器传递函数 — 对齐 WebTools FilterReview.js

从日志 PARM 参数（INS_GYRO_FILTER / INS_HNTCH_* / INS_HNTC2_*）
自动推导完整的 AP 滤波器栈传递函数。

对外接口（与其他平台一致）：
    derive_filters_from_params(params)  -> Dict
    compute_filter_response(...)        -> (mag_db, phase_deg)
    simulate_filtered_spectrum(...)     -> np.ndarray
    build_filter_display_lines(params)  -> List[str]
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# 重用通用滤波器数学
from smarttune.analyzers.filter_transfer import (
    DigitalBiquadFilter,
    HarmonicNotchFilter,
    _apply_filter_chain,
    compute_filter_response as _compute_filter_response,
)


def derive_filters_from_params(params: Dict[str, float]) -> Dict[str, Any]:
    """
    从 ArduPilot PARM 参数构建滤波器对象链。

    读取 INS_GYRO_FILTER, INS_HNTCH_*, INS_HNTC2_*。
    """
    gyro_hz = params.get("INS_GYRO_FILTER", 20.0)
    lpf = DigitalBiquadFilter(gyro_hz)
    notches = []
    for pfx in ["INS_HNTCH_", "INS_HNTC2_"]:
        en = int(params.get(f"{pfx}ENABLE", 0))
        hnf = HarmonicNotchFilter(
            enable=en, freq=params.get(f"{pfx}FREQ", 80.0),
            bandwidth=params.get(f"{pfx}BW", 40.0),
            attenuation=params.get(f"{pfx}ATT", 40.0),
            harmonics=int(params.get(f"{pfx}HMNCS", 3)),
            options=int(params.get(f"{pfx}OPTS", 0)),
            min_ratio=params.get(f"{pfx}FM_RAT", 1.0),
            mode=int(params.get(f"{pfx}MODE", 1)),
            ref=params.get(f"{pfx}REF", 0.0),
        )
        notches.append(hnf)
    parts = [f"LPF={gyro_hz:.0f}Hz"]
    for i, nf in enumerate(notches):
        if nf.enabled:
            parts.append(f"Notch{i+1}={nf.freq:.0f}Hz")
    return {"gyro_lpf": lpf, "notch_filters": notches, "config_summary": ", ".join(parts)}


def compute_filter_response(
    freqs: np.ndarray,
    sample_rate: float,
    gyro_filter_hz: float = 0.0,
    notch_params: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, float]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    计算 ArduPilot 滤波器幅度 (dB) + 相位 (度)。

    支持两种模式：
    1. 手动模式：gyro_filter_hz + notch_params → 走核心 compute_filter_response
    2. 参数模式：params (INS_GYRO_FILTER, INS_HNTCH_* 等) →
       解析为滤波器对象链，走核心 _apply_filter_chain
    """
    if params is not None:
        cfg = derive_filters_from_params(params)
        return _apply_filter_chain(freqs, sample_rate, cfg["gyro_lpf"], cfg["notch_filters"])
    return _compute_filter_response(freqs, sample_rate, gyro_filter_hz, notch_params)


def simulate_filtered_spectrum(
    freqs: np.ndarray,
    magnitudes: np.ndarray,
    sample_rate: float,
    gyro_filter_hz: float = 0.0,
    notch_params: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, float]] = None,
) -> np.ndarray:
    """模拟 ArduPilot 滤波后的频谱（dB 域相加）。"""
    mag_db, _ = compute_filter_response(freqs, sample_rate, gyro_filter_hz, notch_params, params)
    return magnitudes + mag_db


def build_filter_display_lines(params: Dict[str, float]) -> List[str]:
    """
    构建 CLI 显示用的滤波器配置文本行（ArduPilot 格式）。

    Returns
    -------
    List[str]
        Rich markup 格式的行列表，供 cli.py 直接打印。
    """
    lines: List[str] = []
    lpf_hz = params.get("INS_GYRO_FILTER", 0.0)
    lines.append(f"  LPF: INS_GYRO_FILTER = {lpf_hz:.0f} Hz")

    for i, pfx in enumerate(["INS_HNTCH_", "INS_HNTC2_"]):
        en = int(params.get(f"{pfx}ENABLE", 0))
        if not en:
            continue
        freq_val = params.get(f"{pfx}FREQ", 0.0)
        bw_val   = params.get(f"{pfx}BW",   0.0)
        att_val  = params.get(f"{pfx}ATT",  0.0)
        hmncs    = int(params.get(f"{pfx}HMNCS", 0))
        opts     = int(params.get(f"{pfx}OPTS",  0))
        mode_val = int(params.get(f"{pfx}MODE",  0))
        double   = "Double" if (opts & 1)  else ""
        triple   = "Triple" if (opts & 16) else ""
        notch_type = triple or double or "Single"
        lines.append(
            f"  Notch{i + 1}: {freq_val:.0f} Hz, BW={bw_val:.0f}, "
            f"ATT={att_val:.0f} dB, Harmonics={bin(hmncs)}, "
            f"{notch_type}, Mode={mode_val}"
        )
    return lines


def get_fallback_gyro_filter_hz(params: Dict[str, float]) -> float:
    """从参数中读取 LPF 截止频率，带 0 回退。"""
    return float(params.get("INS_GYRO_FILTER", 0.0))


def get_notch_bandwidth_hz(params: Dict[str, float], notch_index: int = 1) -> float:
    """读取指定陷波器的带宽（Hz）。notch_index: 1 → HNTCH, 2 → HNTC2。"""
    pfx = "INS_HNTCH_" if notch_index == 1 else "INS_HNTC2_"
    return float(params.get(f"{pfx}BW", 10.0))
