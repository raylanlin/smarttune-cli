"""
sysid_analyzer.py - 系统辨识分析模块

基于 ARX 模型进行系统辨识，计算自然频率、阻尼比，
并提供 PID 带宽调参建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy import signal

from smarttune.errors import InsufficientPIDDataError, AnalysisError
from smarttune.analyzers.arx_model import arx_identify, estimate_delay

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class SysIDResult:
    """单轴系统辨识结果。"""

    axis: str

    # ARX 模型参数
    na: int
    nb: int
    delay_samples: int
    a_coeffs: np.ndarray = field(repr=False)
    b_coeffs: np.ndarray = field(repr=False)

    # 连续系统参数（二阶近似）
    natural_freq_hz: float
    damping_ratio: float
    dc_gain: float

    # PID 带宽建议
    suggested_bandwidth_hz: float
    suggested_p_gain: float

    # 拟合质量
    fit_quality_percent: float

    # 原始数据信息
    sample_rate_hz: float
    data_points: int

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式（用于输出）。"""
        return {
            "axis": self.axis,
            "arx_model": {
                "na": self.na,
                "nb": self.nb,
                "delay_samples": self.delay_samples,
                "a_coeffs": self.a_coeffs.tolist(),
                "b_coeffs": self.b_coeffs.tolist(),
            },
            "continuous_approximation": {
                "natural_freq_hz": round(self.natural_freq_hz, 2),
                "damping_ratio": round(self.damping_ratio, 3),
                "dc_gain": round(self.dc_gain, 3),
            },
            "pid_recommendations": {
                "suggested_bandwidth_hz": round(self.suggested_bandwidth_hz, 1),
                "suggested_p_gain": round(self.suggested_p_gain, 4),
            },
            "fit_quality": {
                "fit_percent": round(self.fit_quality_percent, 1),
                "sample_rate_hz": round(self.sample_rate_hz, 1),
                "data_points": self.data_points,
            },
        }


# ---------------------------------------------------------------------------
# 系统辨识核心函数
# ---------------------------------------------------------------------------


def discrete_to_second_order(
    a: np.ndarray,
    b: np.ndarray,
    dt: float,
) -> Tuple[float, float, float]:
    """
    将离散 ARX 模型转换为连续二阶系统参数。

    使用双线性变换 (Tustin) s = (2/dt) * (z-1)/(z+1)
    将离散极点映射到 s 平面，然后从二阶特征方程提取 ωn 和 ζ。

    Parameters
    ----------
    a : np.ndarray
        A 多项式系数 [1, a1, a2, ...]
    b : np.ndarray
        B 多项式系数 [b0, b1, ...]
    dt : float
        采样周期（秒）

    Returns
    -------
    Tuple[wn, zeta, gain]
        wn: 自然频率 (rad/s)
        zeta: 阻尼比
        gain: 直流增益
    """
    # 计算直流增益 (z=1)
    dc_gain = float(np.sum(b) / np.sum(a))

    # 离散极点
    poles_discrete = np.roots(a)

    # 稳定极点: |z| < 1 且不在原点附近
    stable_mask = (np.abs(poles_discrete) < 1.0) & (np.abs(poles_discrete) > 0.01)
    stable_poles = poles_discrete[stable_mask]

    if len(stable_poles) == 0:
        return 10.0, 0.7, dc_gain

    # 主导极点 = 最接近单位圆的稳定极点（|z| 最接近 1）
    # 这些极点对应系统中最慢的动态（最低频率）
    sorted_idx = np.argsort(-np.abs(stable_poles))  # |z| 降序，最接近1在前
    dominant_poles = stable_poles[sorted_idx[:2]]

    if len(dominant_poles) < 2:
        # 只有一个主导极点，用一阶近似
        p = dominant_poles[0]
        s_plane = (2.0 / dt) * (p - 1.0) / (p + 1.0)
        wn = float(np.abs(s_plane))
        zeta = 1.0
    else:
        p1, p2 = dominant_poles

        # 双线性变换到 s 平面
        s1 = (2.0 / dt) * (p1 - 1.0) / (p1 + 1.0)
        s2 = (2.0 / dt) * (p2 - 1.0) / (p2 + 1.0)

        # 二阶等效仅对共轭复极点对成立：ωn² = s·conj(s) = |s|²。
        # 两个不相关的实极点是两个独立一阶环节，套用公式会产生
        # 物理上不存在的 ωn/ζ — 改用最慢（最主导）实极点的一阶近似。
        is_conjugate_pair = (
            abs(np.imag(s1)) > 1e-9
            and np.isclose(np.real(s1), np.real(s2), rtol=1e-6, atol=1e-9)
            and np.isclose(np.imag(s1), -np.imag(s2), rtol=1e-6, atol=1e-9)
        )

        if is_conjugate_pair:
            # ωn² = |s1 * conj(s2)| = |s1|²
            wn_sq = float(np.abs(s1 * np.conj(s2)))
            wn = float(np.sqrt(max(wn_sq, 1e-6)))
            zeta = float(-np.real(s1) / wn) if wn > 0 else 0.7
        else:
            # 两个实极点：取最接近虚轴（最慢）的极点做一阶近似
            s_dom = s1 if abs(np.real(s1)) <= abs(np.real(s2)) else s2
            wn = float(np.abs(s_dom))
            zeta = 1.0  # 过阻尼/非振荡系统

    # 限制合理范围
    wn = float(np.clip(wn, 1.0, 500.0))
    zeta = float(np.clip(zeta, 0.05, 2.0))

    return wn, zeta, dc_gain


def calculate_fit_quality(
    y_actual: np.ndarray,
    y_predicted: np.ndarray,
) -> float:
    """
    计算模型拟合质量（百分比）。

    使用 R² 决定系数，转换为百分比。

    Parameters
    ----------
    y_actual : np.ndarray
        实际输出
    y_predicted : np.ndarray
        模型预测输出

    Returns
    -------
    float
        拟合质量百分比 (0-100)
    """
    ss_res = np.sum((y_actual - y_predicted) ** 2)
    ss_tot = np.sum((y_actual - np.mean(y_actual)) ** 2)

    if ss_tot < 1e-10:
        return 0.0

    r_squared = 1 - (ss_res / ss_tot)
    return max(0.0, min(100.0, r_squared * 100))


def predict_arx_output(
    u: np.ndarray,
    a: np.ndarray,
    b: np.ndarray,
    d: int,
) -> np.ndarray:
    """
    用 ARX 模型预测输出。

    Parameters
    ----------
    u : np.ndarray
        输入信号
    a : np.ndarray
        A 多项式系数 [1, a1, a2, ...]
    b : np.ndarray
        B 多项式系数 [b0, b1, ...]
    d : int
        纯延迟（拍数）

    Returns
    -------
    np.ndarray
        预测输出
    """
    N = len(u)
    y_pred = np.zeros(N)

    na = len(a) - 1
    nb = len(b)

    start_idx = max(na, nb + d)

    for k in range(start_idx, N):
        # AR 部分
        for i in range(1, na + 1):
            y_pred[k] -= a[i] * y_pred[k - i]

        # X 部分（外生输入）
        for j in range(nb):
            idx = k - d - j
            if idx >= 0:
                y_pred[k] += b[j] * u[idx]

    return y_pred


def suggest_pid_bandwidth(
    natural_freq_hz: float,
    damping_ratio: float,
    sample_rate_hz: float,
) -> Tuple[float, float]:
    """
    根据系统特性建议 PID 带宽和 P 增益。

    Parameters
    ----------
    natural_freq_hz : float
        自然频率 (Hz)
    damping_ratio : float
        阻尼比
    sample_rate_hz : float
        采样率 (Hz)

    Returns
    -------
    Tuple[bandwidth_hz, p_gain]
        建议带宽 (Hz) 和建议 P 增益
    """
    # 带宽建议：通常为自然频率的 0.5 ~ 1.0 倍
    # 阻尼比低时，带宽应更低以避免振荡

    if damping_ratio < 0.3:
        # 欠阻尼，保守带宽
        bandwidth_factor = 0.3
    elif damping_ratio < 0.7:
        # 中等阻尼
        bandwidth_factor = 0.5
    else:
        # 良好阻尼
        bandwidth_factor = 0.7

    suggested_bw = natural_freq_hz * bandwidth_factor

    # 限制带宽不超过采样率的 1/10（避免混叠）
    max_bw = sample_rate_hz / 10
    suggested_bw = min(suggested_bw, max_bw)

    # P 增益建议：基于带宽和直流增益
    # 简化的经验公式：P ≈ 2π * bw / (10 * gain)
    suggested_p = 2 * np.pi * suggested_bw / 10
    suggested_p = np.clip(suggested_p, 0.01, 0.5)

    return suggested_bw, suggested_p


# ---------------------------------------------------------------------------
# 主分析类
# ---------------------------------------------------------------------------


class SysIDAnalyzer:
    """
    系统辨识分析器。

    基于 ARX 模型对 Rate PID 数据进行系统辨识，
    提取系统动态特性并提供调参建议。

    Parameters
    ----------
    na : int, default 3
        ARX 模型 A 多项式阶数。
    nb : int, default 2
        ARX 模型 B 多项式阶数。
    """

    def __init__(
        self,
        na: int = 3,
        nb: int = 2,
    ) -> None:
        self._na = na
        self._nb = nb

    def analyze(
        self,
        flight_data,
        axis: Optional[str] = None,
    ) -> Dict[str, SysIDResult]:
        """
        执行系统辨识分析。

        Parameters
        ----------
        flight_data : FlightData
            统一飞行数据结构。
        axis : str, optional
            指定轴，默认分析所有轴。

        Returns
        -------
        Dict[str, SysIDResult]
        """
        axes = flight_data.axes if axis is None else [axis.lower()]
        results: Dict[str, SysIDResult] = {}

        for ax in axes:
            if ax not in flight_data.pid:
                continue
            result = self._analyze_axis(flight_data, ax)
            if result is not None:
                results[ax] = result

        return results

    def _analyze_axis(self, flight_data, axis: str) -> Optional[SysIDResult]:
        """分析单个轴的系统特性。"""
        sig = flight_data.pid[axis]

        if sig.sample_count < 50:
            raise InsufficientPIDDataError(
                message=f"{axis} axis PID data insufficient ({sig.sample_count} samples)",
                hint="Use a log with sufficient flight data for system identification",
            )

        desired = sig.desired
        actual = sig.actual
        time_arr = sig.timestamp_s

        # 计算采样率
        dt = np.median(np.diff(time_arr))
        if dt <= 0:
            dt = 0.004  # 默认 250 Hz
        sample_rate_hz = 1.0 / dt

        # 去均值（使用前10个点作为基线）
        u = desired - np.mean(desired[:10])
        y = actual - np.mean(actual[:10])

        # 估计延迟
        d = estimate_delay(u, y, max_delay=min(20, len(u) // 4))

        # ARX 辨识（C14 修复：fallback 占位模型显式拒绝，不再产出
        # 看似合理实则虚构的 ωn/ζ 结论）
        try:
            a, b, arx_info = arx_identify(
                u,
                y,
                self._na,
                self._nb,
                d,
                return_info=True,
            )
        except Exception as exc:
            raise AnalysisError(
                message=f"{axis} 轴 ARX 辨识失败: {exc}",
                hint="数据质量可能不足，尝试使用更长的飞行记录",
            )

        if arx_info["is_fallback"]:
            raise AnalysisError(
                message=(
                    f"{axis} 轴 ARX 辨识失败（{arx_info['fallback_reason']}），"
                    "无法产出可信的系统参数"
                ),
                hint="增加采集时长或检查输入激励是否充分",
            )

        # 计算拟合质量
        y_pred = predict_arx_output(u, a, b, d)
        fit_quality = calculate_fit_quality(y, y_pred)

        # 转换为连续系统参数
        wn_rad_s, zeta, dc_gain = discrete_to_second_order(a, b, dt)
        natural_freq_hz = wn_rad_s / (2 * np.pi)

        # PID 带宽建议
        suggested_bw, suggested_p = suggest_pid_bandwidth(natural_freq_hz, zeta, sample_rate_hz)

        return SysIDResult(
            axis=axis,
            na=self._na,
            nb=self._nb,
            delay_samples=d,
            a_coeffs=a,
            b_coeffs=b,
            natural_freq_hz=natural_freq_hz,
            damping_ratio=zeta,
            dc_gain=dc_gain,
            suggested_bandwidth_hz=suggested_bw,
            suggested_p_gain=suggested_p,
            fit_quality_percent=fit_quality,
            sample_rate_hz=sample_rate_hz,
            data_points=sig.sample_count,
        )


def format_sysid_report(results: Dict[str, SysIDResult]) -> str:
    """
    格式化系统辨识报告为字符串。

    Parameters
    ----------
    results : Dict[str, SysIDResult]
        各轴的辨识结果。

    Returns
    -------
    str
        格式化的报告文本。
    """
    lines = []
    lines.append("=" * 60)
    lines.append("系统辨识分析报告 (ARX 模型)")
    lines.append("=" * 60)
    lines.append("")

    for axis in ["roll", "pitch", "yaw"]:
        if axis not in results:
            continue

        r = results[axis]
        lines.append(f"\n【{axis.upper()} 轴】")
        lines.append("-" * 40)

        # ARX 模型信息
        lines.append(f"ARX 模型: na={r.na}, nb={r.nb}, 延迟={r.delay_samples} 拍")
        lines.append(f"  A(z) = 1 + {r.a_coeffs[1]:.4f}z⁻¹ + {r.a_coeffs[2]:.4f}z⁻² + ...")
        lines.append(f"  B(z) = {r.b_coeffs[0]:.4f} + {r.b_coeffs[1]:.4f}z⁻¹ + ...")
        lines.append("")

        # 连续系统参数
        lines.append("连续系统近似（二阶）:")
        lines.append(
            f"  自然频率 ωn = {r.natural_freq_hz:.2f} Hz ({r.natural_freq_hz*2*np.pi:.1f} rad/s)"
        )
        lines.append(f"  阻尼比 ζ = {r.damping_ratio:.3f}")

        if r.damping_ratio < 0.3:
            damp_desc = "欠阻尼（易振荡）"
        elif r.damping_ratio < 0.7:
            damp_desc = "中等阻尼"
        elif r.damping_ratio < 1.0:
            damp_desc = "良好阻尼"
        else:
            damp_desc = "过阻尼（响应慢）"
        lines.append(f"  阻尼状态: {damp_desc}")
        lines.append(f"  直流增益 = {r.dc_gain:.3f}")
        lines.append("")

        # PID 建议
        lines.append("PID 带宽建议:")
        lines.append(f"  建议带宽: {r.suggested_bandwidth_hz:.1f} Hz")
        lines.append(f"  建议 P 增益: {r.suggested_p_gain:.4f}")
        lines.append("")

        # 拟合质量
        lines.append(f"模型拟合质量: {r.fit_quality_percent:.1f}%")
        lines.append(f"数据点数: {r.data_points}, 采样率: {r.sample_rate_hz:.1f} Hz")

    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)
