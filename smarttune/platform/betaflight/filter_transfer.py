"""
Betaflight 滤波器传递函数 — 占位实现（Phase 3）

当前状态：
  BF 的滤波器栈（gyro_lowpass / dterm_lowpass / RPM filter / 动态陷波）
  与 ArduPilot 的 INS_HNTCH 架构有本质差异。精确渲染待 Phase 3 实现。

当前策略：
  1. 从 BF 参数名（gyro_lowpass_hz / gyro_notch1_hz 等）映射到
     等价的 LPF + 单陷波模型，再调用通用 compute_filter_response
  2. build_filter_display_lines 显示 BF 原生参数名
  3. 所有入口与 ardupilot/filter_transfer.py 签名一致

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


# ---------------------------------------------------------------------------
# BF 参数名 → 通用滤波器模型映射
# ---------------------------------------------------------------------------

def derive_filters_from_params(params: Dict[str, float]) -> Dict[str, Any]:
    """
    从 Betaflight 参数构建近似滤波器描述（供 compute_filter_response 使用）。

    支持 BF 4.3 旧参数名和 BF 4.5+ 新参数名（兼容不同版本的 BBL 日志）。

    BF 4.5+ 参数名:
        gyro_lpf1_static_hz   — 陀螺一阶 LPF（替代 gyro_lowpass_hz）
        gyro_lpf2_static_hz   — 陀螺二阶 LPF（替代 gyro_lowpass2_hz）
        dterm_lpf1_static_hz  — D-term LPF（替代 dterm_lowpass_hz）
        gyro_notch1_hz        — 固定陷波中心频率
        gyro_notch1_cutoff    — 陷波截止（近似带宽）

    Returns
    -------
    Dict with keys: gyro_lpf, notch_filters, config_summary, _bf_raw
    """
    # Support both old (BF <4.5) and new (BF 4.5+) param names
    lpf1 = float(params.get("gyro_lpf1_static_hz",
                           params.get("gyro_lowpass_hz", 0.0)))
    lpf2 = float(params.get("gyro_lpf2_static_hz",
                           params.get("gyro_lowpass2_hz", 0.0)))
    dterm_lpf = float(params.get("dterm_lpf1_static_hz",
                               params.get("dterm_lowpass_hz", 0.0)))

    # 等效 LPF：取两级中的较低（更保守）截止频率
    effective_lpf = min(f for f in [lpf1, lpf2] if f > 0) if any(f > 0 for f in [lpf1, lpf2]) else 0.0

    # 固定陷波：gyro_notch1
    notch1_hz     = float(params.get("gyro_notch1_hz",     0.0))
    notch1_cutoff = float(params.get("gyro_notch1_cutoff", 0.0))
    notch1_bw = abs(notch1_hz - notch1_cutoff) * 2.0 if notch1_cutoff > 0 and notch1_hz > 0 else 20.0

    # gyro_notch2（可选）
    notch2_hz     = float(params.get("gyro_notch2_hz",     0.0))
    notch2_cutoff = float(params.get("gyro_notch2_cutoff", 0.0))
    notch2_bw = abs(notch2_hz - notch2_cutoff) * 2.0 if notch2_cutoff > 0 and notch2_hz > 0 else 20.0

    parts = []
    if effective_lpf > 0:
        parts.append(f"Gyro LPF={effective_lpf:.0f} Hz")
    if notch1_hz > 0:
        parts.append(f"Notch1={notch1_hz:.0f} Hz")
    if notch2_hz > 0:
        parts.append(f"Notch2={notch2_hz:.0f} Hz")
    if not parts:
        parts.append("(no filters parsed)")

    # Use original names found in params for display
    display_lpf1 = "gyro_lpf1_static_hz" if "gyro_lpf1_static_hz" in params else "gyro_lowpass_hz"
    display_lpf2 = "gyro_lpf2_static_hz" if "gyro_lpf2_static_hz" in params else "gyro_lowpass2_hz"
    display_dlpf = "dterm_lpf1_static_hz" if "dterm_lpf1_static_hz" in params else "dterm_lowpass_hz"

    # 构造近似 notch_filters 列表
    notch_filters_approx = []
    if notch1_hz > 0:
        notch_filters_approx.append({
            "enabled": True, "freq": notch1_hz, "bw": notch1_bw,
            "att": 40.0, "mode": 1,
        })
    if notch2_hz > 0:
        notch_filters_approx.append({
            "enabled": True, "freq": notch2_hz, "bw": notch2_bw,
            "att": 40.0, "mode": 1,
        })

    return {
        "gyro_lpf_hz": effective_lpf,
        "notch_filters_approx": notch_filters_approx,
        "config_summary": ", ".join(parts),
        "_bf_raw": {
            display_lpf1:  lpf1,
            display_lpf2: lpf2,
            "notch1_hz": notch1_hz, "notch1_bw": notch1_bw,
            "notch2_hz": notch2_hz, "notch2_bw": notch2_bw,
            display_dlpf: dterm_lpf,
        },
    }


def compute_filter_response(
    freqs: np.ndarray,
    sample_rate: float,
    gyro_filter_hz: float = 0.0,
    notch_params: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, float]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    计算 Betaflight 近似滤波器响应（幅度 dB + 相位 °）。

    若提供 params，自动从 BF 参数推导；否则使用 gyro_filter_hz / notch_params。

    注意：当前实现为近似模型（单级 LPF + 固定陷波），
    不含 RPM filter 动态跟踪和 D-term 专用滤波器栈。
    """
    if params is not None:
        cfg = derive_filters_from_params(params)
        effective_lpf = cfg["gyro_lpf_hz"]
        # 取第一个陷波近似（TODO: Phase 3 支持多陷波）
        nf_list = cfg["notch_filters_approx"]
        first_notch = None
        if nf_list:
            nf = nf_list[0]
            first_notch = {
                "center_hz": nf["freq"],
                "bandwidth_hz": nf["bw"],
                "attenuation_db": nf.get("att", 40.0),
                "harmonics": 1,  # BF 固定陷波无谐波
            }
        return _compute_filter_response(freqs, sample_rate, effective_lpf, first_notch)

    return _compute_filter_response(freqs, sample_rate, gyro_filter_hz, notch_params)


def simulate_filtered_spectrum(
    freqs: np.ndarray,
    magnitudes: np.ndarray,
    sample_rate: float,
    gyro_filter_hz: float = 0.0,
    notch_params: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, float]] = None,
) -> np.ndarray:
    """模拟 Betaflight 近似滤波后的频谱（dB 域相加）。"""
    mag_db, _ = compute_filter_response(freqs, sample_rate, gyro_filter_hz, notch_params, params)
    return magnitudes + mag_db


def build_filter_display_lines(params: Dict[str, float]) -> List[str]:
    """
    构建 CLI 显示用的 BF 滤波器配置文本行。
    支持 BF 4.3 旧名和 BF 4.5+ 新名。
    """
    lines: List[str] = []

    # Try new names first, fall back to old names
    lpf1 = params.get("gyro_lpf1_static_hz", params.get("gyro_lowpass_hz", 0.0))
    lpf2 = params.get("gyro_lpf2_static_hz", params.get("gyro_lowpass2_hz", 0.0))
    dterm_lpf = params.get("dterm_lpf1_static_hz", params.get("dterm_lowpass_hz", 0.0))

    lpf1_name = "gyro_lpf1_static_hz" if "gyro_lpf1_static_hz" in params else "gyro_lowpass_hz"
    lpf2_name = "gyro_lpf2_static_hz" if "gyro_lpf2_static_hz" in params else "gyro_lowpass2_hz"
    dlpf_name = "dterm_lpf1_static_hz" if "dterm_lpf1_static_hz" in params else "dterm_lowpass_hz"

    if lpf1 > 0:
        lines.append(f"  Gyro LPF1: {lpf1_name} = {lpf1:.0f} Hz")
    if lpf2 > 0:
        lines.append(f"  Gyro LPF2: {lpf2_name} = {lpf2:.0f} Hz")

    if dterm_lpf > 0:
        lines.append(f"  D-term LPF: {dlpf_name} = {dterm_lpf:.0f} Hz")

    for idx in [1, 2]:
        hz  = params.get(f"gyro_notch{idx}_hz",     0.0)
        cut = params.get(f"gyro_notch{idx}_cutoff", 0.0)
        if hz > 0:
            lines.append(f"  Notch{idx}: gyro_notch{idx}_hz = {hz:.0f} Hz, cutoff = {cut:.0f} Hz")

    rpm_hz = params.get("rpm_filter_min_hz", 0.0)
    if rpm_hz > 0:
        lines.append(f"  RPM Filter: min_hz = {rpm_hz:.0f} Hz (dynamic, not rendered)")

    if not lines:
        lines.append("  (no filter parameters found in log header)")

    return lines


def get_fallback_gyro_filter_hz(params: Dict[str, float]) -> float:
    """返回有效的 LPF 截止频率（取两级最低非零值，兼容新旧参数名）。"""
    lpf1 = float(params.get("gyro_lpf1_static_hz", params.get("gyro_lowpass_hz", 0.0)))
    lpf2 = float(params.get("gyro_lpf2_static_hz", params.get("gyro_lowpass2_hz", 0.0)))
    valid = [f for f in [lpf1, lpf2] if f > 0]
    return min(valid) if valid else 0.0


def get_notch_bandwidth_hz(params: Dict[str, float], notch_index: int = 1) -> float:
    """返回指定固定陷波的近似带宽（Hz）。"""
    hz  = float(params.get(f"gyro_notch{notch_index}_hz",     0.0))
    cut = float(params.get(f"gyro_notch{notch_index}_cutoff", 0.0))
    if hz > 0 and cut > 0:
        return abs(hz - cut) * 2.0
    return 20.0
