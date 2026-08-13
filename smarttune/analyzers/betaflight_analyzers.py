"""
smarttune/analyzers/betaflight_analyzers.py

Betaflight 特有分析器:
  1. FeedforwardAnalyzer — FF strength + smoothing 评估
  2. RPMFilterAnalyzer  — RPM 滤波器效果评估
  3. DTermNoiseAnalyzer — D-term 噪声 / d_min boost 行为分析

这些分析器通过 BetaflightAdapter.extra_analyzers() 注册，
仅在 platform == "betaflight" 时运行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from smarttune.models.flight_data import FlightData
from smarttune.models.analysis_result import (
    Assessment,
    Confidence,
    ParamRef,
    ParamRecommendation,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class FeedforwardResult:
    """Feedforward 分析结果。"""

    axis: str
    ff_contribution_percent: float = 0.0  # FF 在总 PID 输出中的占比
    ff_overshoot_detected: bool = False  # FF 是否导致过冲
    ff_tracking_error_rms: float = 0.0  # setpoint → actual 的 RMS 追踪误差
    assessment: Assessment = Assessment.GOOD
    recommendations: List[ParamRecommendation] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RPMFilterResult:
    """RPM 滤波器效果评估结果。"""

    rpm_filter_detected: bool = False
    noise_reduction_db: float = 0.0  # 估计的噪声衰减 (dB)
    motor_noise_peaks_hz: List[float] = field(default_factory=list)
    residual_noise_level: float = 0.0
    assessment: Assessment = Assessment.GOOD
    recommendations: List[ParamRecommendation] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DTermNoiseResult:
    """D-term 噪声分析结果。"""

    axis: str
    d_noise_rms: float = 0.0  # D-term 信号的 RMS
    d_to_output_ratio: float = 0.0  # D-term RMS / P-term RMS
    d_min_active_percent: float = 0.0  # d_min 激活时间占比
    high_freq_energy_ratio: float = 0.0  # 高频噪声能量占比
    assessment: Assessment = Assessment.GOOD
    recommendations: List[ParamRecommendation] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1. Feedforward Analyzer
# ---------------------------------------------------------------------------


class FeedforwardAnalyzer:
    """分析 Betaflight Feedforward (FF) 项的效果。

    FF 在 BF 中是独立于 PID 的第四项，直接将 RC 指令前馈到输出。
    合理的 FF 可以加速响应并减轻 P 项的负担。

    分析维度:
      - FF 在总 PID 输出中的占比 (正常 20-40%)
      - FF 是否导致 setpoint 瞬间过冲
      - setpoint → actual 的追踪误差
    """

    def analyze(self, fd: FlightData, axis: str = "all") -> Dict[str, FeedforwardResult]:
        """分析 feedforward 效果。

        Parameters
        ----------
        fd : FlightData
        axis : str
            "roll", "pitch", "yaw", 或 "all"

        Returns
        -------
        Dict[str, FeedforwardResult]
            每轴的 FF 分析结果
        """
        axes = ["roll", "pitch", "yaw"] if axis == "all" else [axis]
        results: Dict[str, FeedforwardResult] = {}

        for ax in axes:
            if ax not in fd.pid:
                continue

            sig = fd.pid[ax]
            result = FeedforwardResult(axis=ax)

            # 需要 ff_term 数据
            if sig.ff_term is None or np.all(sig.ff_term == 0):
                result.details["note"] = "No FF data available"
                result.assessment = Assessment.GOOD
                results[ax] = result
                continue

            ff = sig.ff_term.astype(np.float64)
            n = len(ff)

            # ── FF 贡献度 ──
            # 计算 FF / (|P| + |I| + |D| + |FF|) 的平均值
            p = sig.p_term.astype(np.float64) if sig.p_term is not None else np.zeros(n)
            i = sig.i_term.astype(np.float64) if sig.i_term is not None else np.zeros(n)
            d = sig.d_term.astype(np.float64) if sig.d_term is not None else np.zeros(n)

            total_abs = np.abs(p) + np.abs(i) + np.abs(d) + np.abs(ff)
            # 避免除零
            mask = total_abs > 1e-6
            if np.sum(mask) > 0:
                ff_ratio = np.mean(np.abs(ff[mask]) / total_abs[mask]) * 100
            else:
                ff_ratio = 0.0
            result.ff_contribution_percent = ff_ratio

            # ── FF 过冲检测 ──
            # 当 setpoint 突变时，如果 actual 的峰值 > desired 且 FF 很大
            desired = sig.desired.astype(np.float64)
            actual = sig.actual.astype(np.float64)

            # 检测 setpoint 突变 (diff > 30% of range)
            sp_range = np.ptp(desired)
            if sp_range > 1e-6:
                sp_diff = np.abs(np.diff(desired))
                threshold = sp_range * 0.3
                step_indices = np.where(sp_diff > threshold)[0]

                overshoot_events = 0
                for idx in step_indices:
                    # 看阶跃后 20 个样本内是否有过冲
                    window_end = min(idx + 20, n)
                    window = actual[idx:window_end]
                    target = desired[min(idx + 1, n - 1)]

                    if len(window) > 2 and target != 0:
                        peak = np.max(np.abs(window))
                        if peak > abs(target) * 1.15:  # 15% 过冲
                            overshoot_events += 1

                result.ff_overshoot_detected = overshoot_events > len(step_indices) * 0.3

            # ── 追踪误差 ──
            tracking_error = actual - desired
            result.ff_tracking_error_rms = float(np.sqrt(np.mean(tracking_error**2)))

            # ── 评估 ──
            if result.ff_overshoot_detected:
                result.assessment = Assessment.MARGINAL
                result.recommendations.append(
                    ParamRecommendation(
                        param=ParamRef(f"pid.{ax}.ff", axis=ax),
                        current=fd.params.get(f"pid_{ax}_f", 0),
                        suggested=fd.params.get(f"pid_{ax}_f", 120) * 0.85,
                        reason=f"FF causes overshoot on {ax} — reduce FF by ~15%",
                        confidence=Confidence.MEDIUM,
                        action="decrease",
                    )
                )
            elif ff_ratio < 10 and sig.ff_term is not None:
                result.assessment = Assessment.MARGINAL
                result.recommendations.append(
                    ParamRecommendation(
                        param=ParamRef(f"pid.{ax}.ff", axis=ax),
                        current=fd.params.get(f"pid_{ax}_f", 0),
                        suggested=fd.params.get(f"pid_{ax}_f", 120) * 1.2,
                        reason=f"FF contribution very low ({ff_ratio:.0f}%) — consider increasing FF",
                        confidence=Confidence.LOW,
                        action="increase",
                    )
                )
            elif ff_ratio > 50:
                result.assessment = Assessment.MARGINAL
                result.recommendations.append(
                    ParamRecommendation(
                        param=ParamRef(f"pid.{ax}.ff", axis=ax),
                        current=fd.params.get(f"pid_{ax}_f", 0),
                        suggested=fd.params.get(f"pid_{ax}_f", 120) * 0.8,
                        reason=f"FF dominates output ({ff_ratio:.0f}%) — P term may be too low or FF too high",
                        confidence=Confidence.MEDIUM,
                        action="decrease",
                    )
                )

            result.details = {
                "ff_contribution_percent": round(ff_ratio, 1),
                "tracking_error_rms": round(result.ff_tracking_error_rms, 2),
                "ff_overshoot_events": overshoot_events if sp_range > 1e-6 else 0,
                "total_step_events": len(step_indices) if sp_range > 1e-6 else 0,
            }

            results[ax] = result

        return results


# ---------------------------------------------------------------------------
# 2. RPM Filter Analyzer
# ---------------------------------------------------------------------------


class RPMFilterAnalyzer:
    """评估 RPM 滤波器的效果。

    通过分析电机频率处的噪声水平来推断 RPM filter 是否有效。
    需要 gyro 数据和电机输出数据。

    分析方法:
      - 从 gyro 频谱中找到电机噪声峰值
      - 检查这些峰值是否被有效衰减
      - 对比有/无 RPM filter 的典型噪声特征
    """

    def analyze(self, fd: FlightData) -> RPMFilterResult:
        """分析 RPM 滤波器效果。"""
        result = RPMFilterResult()

        # 检查是否启用了 RPM filter
        rpm_enabled = fd.params.get("rpm_filter", 0)
        dshot_bidir = fd.params.get("dshot_bidir", 0)
        result.rpm_filter_detected = bool(rpm_enabled) and bool(dshot_bidir)

        if fd.gyro is None or len(fd.gyro) < 256:
            result.details["note"] = "Insufficient gyro data for RPM filter analysis"
            return result

        # 估计采样率
        if fd.imu_timestamp_s is not None and len(fd.imu_timestamp_s) > 1:
            dt = np.median(np.diff(fd.imu_timestamp_s))
            fs = 1.0 / dt if dt > 0 else fd.sample_rate_hz
        else:
            fs = fd.sample_rate_hz

        if fs <= 0:
            result.details["note"] = "Cannot determine sample rate"
            return result

        # 对 gyro roll 轴做 FFT
        gyro_signal = fd.gyro[:, 0].astype(np.float64)
        gyro_signal = gyro_signal - np.mean(gyro_signal)
        n = len(gyro_signal)

        # 加窗
        window = np.hanning(n)
        windowed = gyro_signal * window

        fft_result = np.fft.rfft(windowed)
        freqs = np.fft.rfftfreq(n, d=1.0 / fs)
        power = np.abs(fft_result) ** 2

        # 归一化
        power_db = 10 * np.log10(power + 1e-12)

        # 噪声底限 (取 20-50Hz 的中位数作为 baseline)
        baseline_mask = (freqs >= 20) & (freqs <= 50)
        if np.sum(baseline_mask) > 0:
            noise_floor_db = float(np.median(power_db[baseline_mask]))
        else:
            noise_floor_db = float(np.percentile(power_db, 25))

        # 在电机噪声典型范围内找峰值 (80-400Hz)
        motor_band = (freqs >= 80) & (freqs <= 400)
        motor_power = power_db[motor_band]
        motor_freqs = freqs[motor_band]

        if len(motor_power) > 0:
            # 简单峰值检测
            peaks = []
            for i in range(1, len(motor_power) - 1):
                if (
                    motor_power[i] > motor_power[i - 1]
                    and motor_power[i] > motor_power[i + 1]
                    and motor_power[i] > noise_floor_db + 10
                ):
                    peaks.append(
                        {
                            "freq": float(motor_freqs[i]),
                            "power_db": float(motor_power[i]),
                            "above_floor_db": float(motor_power[i] - noise_floor_db),
                        }
                    )

            # 按功率排序
            peaks.sort(key=lambda x: x["power_db"], reverse=True)
            result.motor_noise_peaks_hz = [p["freq"] for p in peaks[:5]]

            if peaks:
                max_peak_above_floor = peaks[0]["above_floor_db"]
                result.noise_reduction_db = max_peak_above_floor

                # 评估
                if result.rpm_filter_detected:
                    if max_peak_above_floor < 15:
                        result.assessment = Assessment.EXCELLENT
                    elif max_peak_above_floor < 25:
                        result.assessment = Assessment.GOOD
                    else:
                        result.assessment = Assessment.MARGINAL
                        result.recommendations.append(
                            ParamRecommendation(
                                param=ParamRef("filter.gyro_lpf"),
                                current=fd.params.get("gyro_lowpass_hz", 200),
                                suggested=max(100, fd.params.get("gyro_lowpass_hz", 200) - 50),
                                reason="Residual motor noise high despite RPM filter — lower gyro LPF",
                                confidence=Confidence.MEDIUM,
                                action="decrease",
                            )
                        )
                else:
                    if max_peak_above_floor > 20:
                        result.assessment = Assessment.MARGINAL
                        result.recommendations.append(
                            ParamRecommendation(
                                param=ParamRef("filter.gyro_lpf"),
                                current=fd.params.get("gyro_lowpass_hz", 200),
                                suggested=max(80, fd.params.get("gyro_lowpass_hz", 200) * 0.7),
                                reason="Significant motor noise without RPM filter — consider enabling RPM filter or lowering gyro LPF",
                                confidence=Confidence.MEDIUM,
                                action="decrease",
                            )
                        )
            else:
                result.assessment = Assessment.EXCELLENT
                result.details["note"] = "No significant motor noise peaks detected"

        # 总体残留噪声水平
        high_freq_mask = (freqs >= 100) & (freqs <= fs / 2 - 10)
        if np.sum(high_freq_mask) > 0:
            result.residual_noise_level = float(np.sqrt(np.mean(power[high_freq_mask])))

        result.details.update(
            {
                "sample_rate_hz": float(fs),
                "noise_floor_db": round(noise_floor_db, 1),
                "peak_count": len(result.motor_noise_peaks_hz),
                "rpm_filter_enabled": result.rpm_filter_detected,
            }
        )

        return result


# ---------------------------------------------------------------------------
# 3. D-Term Noise Analyzer
# ---------------------------------------------------------------------------


class DTermNoiseAnalyzer:
    """分析 D-term 噪声和 d_min/d_max 行为。

    BF 4.x 的 d_min 系统在低 setpoint 活动时降低 D 增益，
    在高 setpoint 活动时使用完整 D 增益 (d_max = pid_x_d)。

    分析维度:
      - D-term 整体噪声水平
      - D/P 比率（D 项相对 P 项的大小）
      - d_min 激活占比（低 D 时间 vs 高 D 时间）
      - 高频噪声能量占比
    """

    def analyze(self, fd: FlightData, axis: str = "all") -> Dict[str, DTermNoiseResult]:
        """分析 D-term 噪声。"""
        axes = ["roll", "pitch", "yaw"] if axis == "all" else [axis]
        results: Dict[str, DTermNoiseResult] = {}

        for ax in axes:
            if ax not in fd.pid:
                continue

            sig = fd.pid[ax]
            result = DTermNoiseResult(axis=ax)

            if sig.d_term is None or np.all(sig.d_term == 0):
                result.details["note"] = "No D-term data available"
                results[ax] = result
                continue

            d = sig.d_term.astype(np.float64)
            n = len(d)

            # ── D-term RMS ──
            result.d_noise_rms = float(np.sqrt(np.mean(d**2)))

            # ── D/P 比率 ──
            if sig.p_term is not None:
                p = sig.p_term.astype(np.float64)
                p_rms = np.sqrt(np.mean(p**2))
                if p_rms > 1e-6:
                    result.d_to_output_ratio = result.d_noise_rms / p_rms
                else:
                    result.d_to_output_ratio = 0.0

            # ── d_min 激活占比 ──
            # d_min 模式下 |D| 较小；检测 D 值分布是否双峰
            d_abs = np.abs(d)
            d_median = np.median(d_abs)
            if d_median > 1e-6:
                # d_min 激活 ≈ D 值低于中位数的 60% 的时间
                d_min_threshold = d_median * 0.6
                result.d_min_active_percent = float(np.mean(d_abs < d_min_threshold) * 100)
            else:
                result.d_min_active_percent = 100.0  # D 几乎为零

            # ── 高频噪声能量占比 ──
            if n >= 64:
                fs = fd.sample_rate_hz if fd.sample_rate_hz > 0 else 4000
                fft_d = np.fft.rfft(d * np.hanning(n))
                freqs = np.fft.rfftfreq(n, d=1.0 / fs)
                power = np.abs(fft_d) ** 2

                # 高频 = > 采样率/8 (500Hz for 4kHz loop)
                high_freq_cutoff = fs / 8
                hf_mask = freqs >= high_freq_cutoff
                total_power = np.sum(power)
                hf_power = np.sum(power[hf_mask])

                if total_power > 1e-12:
                    result.high_freq_energy_ratio = float(hf_power / total_power)
                else:
                    result.high_freq_energy_ratio = 0.0

            # ── 评估 ──
            if result.d_to_output_ratio > 0.5:
                result.assessment = Assessment.POOR
                d_min_param = f"pid.{ax}.d_min"
                result.recommendations.append(
                    ParamRecommendation(
                        param=ParamRef(d_min_param, axis=ax),
                        current=fd.params.get(f"d_min_{ax}", 0),
                        suggested=max(0, fd.params.get(f"d_min_{ax}", 25) * 0.7),
                        reason=f"D-term noise very high on {ax} (D/P ratio = {result.d_to_output_ratio:.2f}) — reduce d_min",
                        confidence=Confidence.HIGH,
                        action="decrease",
                    )
                )
            elif result.d_to_output_ratio > 0.3:
                result.assessment = Assessment.MARGINAL
                result.recommendations.append(
                    ParamRecommendation(
                        param=ParamRef(f"filter.dterm_lpf"),
                        current=fd.params.get("dterm_lowpass_hz", 150),
                        suggested=max(80, fd.params.get("dterm_lowpass_hz", 150) * 0.8),
                        reason=f"D-term noise elevated on {ax} — consider lowering D-term LPF",
                        confidence=Confidence.MEDIUM,
                        action="decrease",
                    )
                )
            elif result.high_freq_energy_ratio > 0.3:
                result.assessment = Assessment.MARGINAL
                result.recommendations.append(
                    ParamRecommendation(
                        param=ParamRef(f"filter.dterm_lpf"),
                        current=fd.params.get("dterm_lowpass_hz", 150),
                        suggested=max(80, fd.params.get("dterm_lowpass_hz", 150) * 0.85),
                        reason=f"High-frequency energy in D-term on {ax} ({result.high_freq_energy_ratio*100:.0f}%) — lower D-term LPF",
                        confidence=Confidence.MEDIUM,
                        action="decrease",
                    )
                )
            else:
                result.assessment = Assessment.GOOD

            result.details = {
                "d_noise_rms": round(result.d_noise_rms, 2),
                "d_to_p_ratio": round(result.d_to_output_ratio, 3),
                "d_min_active_percent": round(result.d_min_active_percent, 1),
                "high_freq_energy_ratio": round(result.high_freq_energy_ratio, 3),
            }

            results[ax] = result

        return results
