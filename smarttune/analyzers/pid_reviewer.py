"""
smarttune/analyzers/pid_reviewer.py

PID 阶跃响应分析引擎 — 平台无关。

消费 FlightData，输出 PIDAnalysisResult（含 ParamRef 参数引用）。
所有平台特有的参数名翻译由输出层通过 PlatformAdapter.map_param_to_platform() 完成。

核心算法（阶跃检测、指标计算、诊断规则）继承自 v1 ArduPilot-only 版，重构为多平台架构。
变更点仅为输入/输出接口。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from smarttune.models.flight_data import FlightData, AxisPIDSignal
from smarttune.models.analysis_result import (
    Assessment,
    Confidence,
    ParamRef,
    ParamRecommendation,
    StepMetrics,
    DiagnosisEntry,
    AxisPIDResult,
    PIDAnalysisResult,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 默认阈值（知识库可覆盖）
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLDS = {
    "roll": {
        "rise_time_ms":      {"min": 40,  "max": 120,  "ideal": 80},
        "overshoot_percent": {"min": 0,   "max": 15,   "ideal": 5},
        "settling_time_ms":  {"min": 100, "max": 350,  "ideal": 200},
        "oscillation_count": {"min": 0,   "max": 2,    "ideal": 0},
        "ss_error_percent":  {"min": 0,   "max": 5,    "ideal": 0},
    },
    "pitch": {
        "rise_time_ms":      {"min": 40,  "max": 150,  "ideal": 90},
        "overshoot_percent": {"min": 0,   "max": 20,   "ideal": 8},
        "settling_time_ms":  {"min": 100, "max": 400,  "ideal": 250},
        "oscillation_count": {"min": 0,   "max": 2,    "ideal": 0},
        "ss_error_percent":  {"min": 0,   "max": 5,    "ideal": 0},
    },
    "yaw": {
        "rise_time_ms":      {"min": 40,  "max": 200,  "ideal": 120},
        "overshoot_percent": {"min": 0,   "max": 25,   "ideal": 10},
        "settling_time_ms":  {"min": 150, "max": 600,  "ideal": 350},
        "oscillation_count": {"min": 0,   "max": 3,    "ideal": 1},
        "ss_error_percent":  {"min": 0,   "max": 8,    "ideal": 0},
    },
}

# 诊断规则：症状 → 参数调整（使用 generic param keys）
_TUNING_RULES: List[Dict[str, Any]] = [
    {
        "rule_id": "HIGH_OS",
        "symptom": "overshoot",
        "adjust": [{"param": "p", "action": "decrease", "factor": 0.85},
                   {"param": "d", "action": "decrease", "factor": 0.90}],
        "reason": "Overshoot exceeds threshold — reduce P gain",
        "confidence": "high",
    },
    {
        "rule_id": "SLOW_RISE",
        "symptom": "rise_time",
        "adjust": [{"param": "p", "action": "increase", "factor": 1.15},
                   {"param": "d", "action": "decrease", "factor": 0.95}],
        "reason": "Slow rise time — increase P gain for faster response",
        "confidence": "high",
    },
    {
        "rule_id": "OSCILLATION",
        "symptom": "oscillation_count",
        "adjust": [{"param": "p", "action": "decrease", "factor": 0.90},
                   {"param": "d", "action": "increase", "factor": 1.10}],
        "reason": "Excessive oscillation — increase D gain for damping",
        "confidence": "medium",
    },
    {
        "rule_id": "LONG_SETTLE",
        "symptom": "settling_time",
        "adjust": [{"param": "i", "action": "increase", "factor": 1.20}],
        "reason": "Long settling time — increase I gain for convergence",
        "confidence": "medium",
    },
    {
        "rule_id": "SS_ERROR",
        "symptom": "steady_state_error",
        "adjust": [{"param": "i", "action": "increase", "factor": 1.25}],
        "reason": "Steady-state error — increase I gain to eliminate offset",
        "confidence": "high",
    },
    {
        "rule_id": "FAST_OSCIL",
        "symptom": "fast_oscillation",
        "adjust": [{"param": "d", "action": "decrease", "factor": 0.80}],
        "reason": "High-frequency oscillation — reduce D gain",
        "confidence": "high",
    },
    {
        "rule_id": "WEAK_DAMPING",
        "symptom": "poor_damping",
        "adjust": [{"param": "d", "action": "increase", "factor": 1.15}],
        "reason": "Poor damping with overshoot — increase D gain",
        "confidence": "medium",
    },
]

# Generic PID param bounds (reasonable for most platforms)
_DEFAULT_BOUNDS = {
    "p":  (0.01, 0.5),
    "i":  (0.001, 0.2),
    "d":  (0.001, 0.05),
    "ff": (0.0, 0.2),
}


# ---------------------------------------------------------------------------
# 辅助函数（继承自 v1 ArduPilot-only 版）
# ---------------------------------------------------------------------------

def detect_steps(
    desired: np.ndarray,
    threshold_frac: float = 0.3,
    min_step_interval_ms: float = 200.0,
    dt_ms: float = 4.0,
) -> List[int]:
    """从 Desired 信号中检测阶跃跳变点。"""
    if desired.size < 3:
        return []
    diff = np.abs(np.diff(desired))
    max_val = np.max(np.abs(desired))
    threshold = max_val * threshold_frac if max_val > 1e-6 else 0.5
    step_indices: List[int] = []
    min_interval_samples = int(round(min_step_interval_ms / dt_ms)) if dt_ms > 0 else 10
    last_step = -min_interval_samples
    for i in range(len(diff)):
        if diff[i] >= threshold and (i - last_step) >= min_interval_samples:
            step_indices.append(i)
            last_step = i
    return step_indices


def _check_window_quality(
    actual: np.ndarray, desired: np.ndarray, step_idx: int,
    window_before: int = 5, window_after: int = 200,
) -> Tuple[bool, str]:
    """阶跃响应窗口数据质量预检。"""
    n = len(actual)
    start = max(0, step_idx - window_before)
    end = min(n, step_idx + window_after)
    actual_win = actual[start:end]

    if len(actual_win) < 20:
        return False, "window_too_short"
    if np.any(~np.isfinite(actual_win)):
        return False, "data_contains_nan_inf"
    if float(np.max(np.abs(actual_win))) > 2000.0:
        return False, "data_corrupt_extreme_value"

    before_end = step_idx
    after_start = step_idx
    before_len = min(before_end, 10)
    after_len = min(n - after_start, 30)
    step_amp = 0.0
    if before_len > 0 and after_len > 0:
        des_before = float(np.mean(desired[before_end - before_len:before_end]))
        des_after = float(np.mean(desired[after_start:after_start + after_len]))
        step_amp = abs(des_after - des_before)

    des_max = float(np.max(np.abs(desired[start:end]))) if end > start else 1.0
    max_actual = float(np.max(np.abs(actual_win)))
    if des_max > 1e-3 and max_actual > des_max * 5.0:
        return False, "actual_desired_ratio_anomaly"

    if step_amp > 0.5:
        post_start = min(step_idx + 2, n)
        post_end = min(step_idx + window_after, n)
        if post_end > post_start + 5:
            actual_post = actual[post_start:post_end]
            pre_start = max(0, step_idx - window_before)
            pre_end = step_idx
            baseline = float(np.mean(actual[pre_start:pre_end])) if pre_end > pre_start else float(actual_post[0])
            tail_len = max(5, len(actual_post) // 4)
            final_val = float(np.mean(actual_post[-tail_len:]))
            response_range = final_val - baseline
            if abs(response_range) > 0.1:
                peak_deviation = float(np.max(actual_post) - final_val)
                if (peak_deviation / abs(response_range)) * 100.0 > 300.0:
                    return False, "overshoot_exceeds_300pct"

    if step_amp > 0.3 and window_before >= 3:
        pre_seg = actual[max(0, step_idx - window_before):step_idx]
        if len(pre_seg) >= 3 and float(np.std(pre_seg)) > step_amp * 0.5:
            return False, "baseline_unstable"

    des_win = desired[start:end]
    if len(des_win) > 2 and step_amp > 0.3:
        des_diff = np.abs(np.diff(des_win))
        if int(np.sum(des_diff >= step_amp * 0.4)) >= 2:
            return False, "multiple_steps_in_window"

    return True, "ok"


def _extract_step_response(
    desired: np.ndarray, actual: np.ndarray, step_idx: int,
    dt_ms: float = 4.0, window_before: int = 5, window_after: int = 200,
) -> Tuple[np.ndarray, np.ndarray, float]:
    """提取阶跃前后的响应窗口。"""
    n = len(actual)
    start = max(0, step_idx - window_before)
    end = min(n, step_idx + window_after)
    window = actual[start:end]
    before_len = min(step_idx, 10)
    after_len = min(len(desired) - step_idx, 30)
    des_before = np.mean(desired[step_idx - before_len:step_idx]) if before_len > 0 else desired[step_idx]
    des_after = np.mean(desired[step_idx:step_idx + after_len]) if after_len > 0 else desired[step_idx]
    step_magnitude = abs(des_after - des_before)
    t = np.arange(len(window)) * dt_ms
    return t, window, step_magnitude


def _compute_metrics(
    response: np.ndarray, time_arr: np.ndarray, step_magnitude: float,
    dt_ms: float = 4.0, settle_band: float = 0.10,
) -> StepMetrics:
    """计算阶跃响应指标。"""
    m = StepMetrics()
    m.step_magnitude = step_magnitude

    if response.size < 5 or step_magnitude < 1e-6:
        return m

    baseline = np.mean(response[:min(5, len(response) // 4)])
    tail_len = max(5, len(response) // 4)
    final_value = np.mean(response[-tail_len:])
    m.final_value = final_value
    m.peak_value = np.max(np.abs(response - baseline))

    response_range = final_value - baseline
    if abs(response_range) < 1e-6:
        return m

    norm = (response - baseline) / step_magnitude
    norm = np.clip(norm, -0.5, 2.0)

    try:
        idx_10 = np.where(norm >= 0.10)[0][0]
        idx_90 = np.where(norm >= 0.90)[0][0]
        m.rise_time_ms = (idx_90 - idx_10) * dt_ms
    except (IndexError, ValueError):
        m.rise_time_ms = -1.0

    if response_range > 0:
        overshoot_val = np.max(response) - final_value
        m.overshoot_percent = max(0.0, (overshoot_val / step_magnitude) * 100.0)
    else:
        m.overshoot_percent = 0.0

    # Settling time
    settle_threshold = abs(step_magnitude) * settle_band
    direction = 1.0 if response_range >= 0 else -1.0
    expected_final = baseline + direction * abs(step_magnitude)
    in_band = np.abs(response - expected_final) <= settle_threshold

    m.settling_time_ms = -1.0
    if in_band.size > 0 and in_band[-1]:
        suffix_in_band = np.argmax(in_band[::-1].astype(np.int8) ^ 1)
        if suffix_in_band == 0 and in_band.all():
            first_idx = 0
        else:
            first_idx = len(in_band) - suffix_in_band
        m.settling_time_ms = float(time_arr[first_idx])
    elif len(response) > 0:
        m.settling_time_ms = float(time_arr[-1])

    # Oscillation count
    centered = response - final_value
    crossings = np.diff(np.sign(centered))
    start_skip = min(5, len(crossings) // 4)
    zero_crossings = int(np.sum(np.abs(crossings[start_skip:]) > 0))
    m.oscillation_count = zero_crossings // 2

    # Steady-state error
    if abs(step_magnitude) > 1e-6:
        actual_delta = final_value - baseline
        target_delta = direction * abs(step_magnitude)
        m.steady_state_error_percent = (abs(actual_delta - target_delta) / abs(step_magnitude)) * 100.0
    else:
        m.steady_state_error_percent = 0.0

    return m


def _assess_metrics(metrics: StepMetrics, thresholds: Dict[str, Dict]) -> Assessment:
    """根据阈值评估响应质量。"""
    score = 0.0
    max_score = 0.0
    checks = [
        ("rise_time_ms", metrics.rise_time_ms),
        ("overshoot_percent", metrics.overshoot_percent),
        ("settling_time_ms", metrics.settling_time_ms),
        ("oscillation_count", float(metrics.oscillation_count)),
        ("ss_error_percent", metrics.steady_state_error_percent),
    ]
    for key, value in checks:
        if key not in thresholds:
            continue
        th = thresholds[key]
        ideal, max_v, min_v = th["ideal"], th["max"], th["min"]
        max_score += 2
        if value < 0:
            score += 1
            continue
        if min_v <= value <= max_v:
            deviation = abs(value - ideal) / (max_v - min_v + 1e-9)
            score += 2 - deviation
        else:
            if value < min_v:
                edge_deviation = abs(min_v - ideal) / (max_v - min_v + 1e-9)
                denom = max(abs(min_v), abs(max_v), 1e-9)
                excess = (min_v - value) / denom
            else:
                edge_deviation = abs(max_v - ideal) / (max_v - min_v + 1e-9)
                denom = max(abs(max_v), 1e-9)
                excess = (value - max_v) / denom
            score += max(0.0, (2.0 - edge_deviation) * (1.0 - excess))

    if max_score == 0:
        return Assessment.MARGINAL
    ratio = score / max_score
    if ratio >= 0.85:
        return Assessment.EXCELLENT
    elif ratio >= 0.70:
        return Assessment.GOOD
    elif ratio >= 0.50:
        return Assessment.MARGINAL
    else:
        return Assessment.POOR


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class PIDReviewer:
    """PID 阶跃响应分析器 — 平台无关。

    Parameters
    ----------
    knowledge : dict, optional
        知识库规则，可覆盖默认阈值和调参规则。
    """

    def __init__(self, knowledge: Optional[Dict[str, Any]] = None) -> None:
        self._knowledge = knowledge or {}
        self._thresholds = self._knowledge.get("thresholds", {}) or _DEFAULT_THRESHOLDS
        tuning_rules_raw = self._knowledge.get("tuning_rules")
        self._rules = (
            tuning_rules_raw if isinstance(tuning_rules_raw, list) and tuning_rules_raw
            else _TUNING_RULES
        )
        self._bounds = self._knowledge.get("pid_bounds", {}) or _DEFAULT_BOUNDS
        self._settle_band = float(self._knowledge.get("settle_band", 0.10))

    def analyze(self, flight_data: FlightData, axis: Optional[str] = None) -> PIDAnalysisResult:
        """分析 PID 阶跃响应。

        Parameters
        ----------
        flight_data : FlightData
            统一飞行数据
        axis : str, optional
            指定轴，None 或 "all" 分析所有轴

        Returns
        -------
        PIDAnalysisResult
        """
        axes = flight_data.axes if axis in (None, "all") else [axis.lower()]
        result = PIDAnalysisResult()

        for ax in axes:
            if ax not in flight_data.pid:
                continue
            ax_result = self._analyze_axis(flight_data, ax)
            result.axes[ax] = ax_result

        # Overall assessment = worst axis
        if result.axes:
            assessments = [r.assessment for r in result.axes.values()]
            order = [Assessment.UNUSABLE, Assessment.POOR, Assessment.MARGINAL,
                     Assessment.GOOD, Assessment.EXCELLENT]
            result.overall_assessment = min(assessments, key=lambda a: order.index(a))

        return result

    def _analyze_axis(self, flight_data: FlightData, axis: str) -> AxisPIDResult:
        """单轴完整分析 — 包含时域指标 + 频域阶跃响应 + 原始数据。"""
        sig = flight_data.pid[axis]
        dt_ms = self._estimate_dt_ms(sig)

        # 1. 时域阶跃响应指标
        metrics = self._compute_avg_metrics(sig)
        thresholds = self._thresholds.get(axis, {})
        assessment = _assess_metrics(metrics, thresholds)

        # 2. 参数建议
        current_params = self._get_current_pid(flight_data.params, axis)
        recommendations = self._generate_recommendations(metrics, current_params, thresholds, axis)

        # 3. Step count
        step_indices = detect_steps(sig.desired, dt_ms=dt_ms)
        step_count = len(step_indices)

        # 4. FFT 频域阶跃响应（按平台动态分派）
        #    platform/ardupilot/step_response_fft.py  → WebTools 对齐
        #    platform/betaflight/step_response_fft.py  → Wiener 反卷积（PID Toolbox 对齐）
        #    platform/px4/step_response_fft.py         → 未来
        fft_step = {}
        _VALID_PLATFORMS = {"ardupilot", "betaflight", "px4"}
        platform_key = flight_data.platform.lower()
        if platform_key not in _VALID_PLATFORMS:
            _log.warning(
                "Unknown platform %r for FFT step response dispatch; "
                "expected one of %s", flight_data.platform, _VALID_PLATFORMS,
            )
        else:
            try:
                mod = importlib.import_module(
                    f"smarttune.platform.{platform_key}.step_response_fft"
                )
                compute_fn = getattr(mod, "compute_step_response_for_axis", None)
                if compute_fn is None:
                    _log.debug(
                        "Module smarttune.platform.%s.step_response_fft has no "
                        "compute_step_response_for_axis; skipping FFT step.",
                        platform_key,
                    )
                else:
                    # 构造旧版 get_pid_data 返回格式
                    pid_dict = {
                        "Desired": sig.desired,
                        "Actual":  sig.actual,
                        "time":    sig.timestamp_s,
                    }
                    if sig.p_term is not None:
                        pid_dict["P"] = sig.p_term
                    if sig.i_term is not None:
                        pid_dict["I"] = sig.i_term
                    if sig.d_term is not None:
                        pid_dict["D"] = sig.d_term
                    if sig.ff_term is not None:
                        pid_dict["FF"] = sig.ff_term
                    fft_step = compute_fn(pid_dict, axis, imu_data=None)
            except ModuleNotFoundError:
                _log.debug(
                    "No step_response_fft module for platform %s; "
                    "FFT step response skipped.", platform_key,
                )
            except Exception as exc:
                _log.debug("FFT step response failed for %s: %s", axis, exc)

        # 5. 原始时间序列（供绘图）
        time_ms = sig.timestamp_s * 1000.0
        raw_data = {
            "time_ms":  time_ms.tolist(),
            "desired":  sig.desired.tolist(),
            "actual":   sig.actual.tolist(),
            "P":        sig.p_term.tolist() if sig.p_term is not None else [],
            "I":        sig.i_term.tolist() if sig.i_term is not None else [],
            "D":        sig.d_term.tolist() if sig.d_term is not None else [],
        }

        # 6. 时域阶跃窗口提取（供每个阶跃单独绘图）
        step_responses = []
        for idx in step_indices:
            is_good, reason = _check_window_quality(sig.actual, sig.desired, idx)
            if not is_good:
                continue
            t_rel, act_win, magnitude = _extract_step_response(
                sig.desired, sig.actual, idx, dt_ms=dt_ms
            )
            t_global = time_ms[idx] + t_rel
            step_responses.append({
                "time_ms":   t_global.tolist(),
                "actual":    act_win.tolist(),
                "desired":   float(np.mean(sig.desired[max(0, idx):idx + 10])),
                "magnitude": magnitude,
            })

        return AxisPIDResult(
            axis=axis,
            metrics=metrics,
            assessment=assessment,
            recommendations=recommendations,
            step_count=step_count,
            fft_step=fft_step,
            raw_data=raw_data,
            step_responses=step_responses,
        )

    def _compute_avg_metrics(self, sig: AxisPIDSignal) -> StepMetrics:
        """计算平均阶跃响应指标。"""
        dt_ms = self._estimate_dt_ms(sig)
        step_indices = detect_steps(sig.desired, dt_ms=dt_ms)

        if not step_indices:
            return StepMetrics()

        metric_fields = ("rise_time_ms", "overshoot_percent", "settling_time_ms",
                         "oscillation_count", "steady_state_error_percent")
        totals = {f: 0.0 for f in metric_fields}
        counts = {f: 0 for f in metric_fields}
        skipped_quality = 0
        high_overshoot_count = 0

        for idx in step_indices:
            is_good, reason = _check_window_quality(sig.actual, sig.desired, idx)
            if not is_good:
                skipped_quality += 1
                _log.debug("Skipping step window idx=%d, quality: %s", idx, reason)
                continue
            t_rel, act_win, magnitude = _extract_step_response(
                sig.desired, sig.actual, idx, dt_ms=dt_ms
            )
            m = _compute_metrics(act_win, t_rel, magnitude, dt_ms=dt_ms,
                                 settle_band=self._settle_band)
            if m.overshoot_percent > 150.0:
                high_overshoot_count += 1
            for field in metric_fields:
                val = getattr(m, field)
                if val >= 0:
                    totals[field] += val
                    counts[field] += 1

        if skipped_quality > 0:
            _log.info(
                "Quality filter: %d/%d candidate windows skipped, %d valid.",
                skipped_quality, len(step_indices),
                len(step_indices) - skipped_quality,
            )
        if high_overshoot_count > 0:
            _log.warning(
                "%d windows had overshoot > 150%%, consider checking quality filter thresholds.",
                high_overshoot_count,
            )

        result = StepMetrics()
        for field in metric_fields:
            if counts[field] > 0:
                setattr(result, field, totals[field] / counts[field])
        return result

    def _estimate_dt_ms(self, sig: AxisPIDSignal) -> float:
        """估算采样周期。"""
        if sig.sample_count > 1:
            dt = float(np.median(np.diff(sig.timestamp_s)))
            return max(dt * 1000.0, 0.1)
        return 4.0

    def _get_current_pid(self, params: Dict[str, float], axis: str) -> Dict[str, float]:
        """从 FlightData.params 中提取当前 PID 值（通过 generic name 查找）。

        注意：params 里存的是平台原生参数名，这里需要反向查找。
        但为了保持平台无关，我们用 generic key 作为结果 key。
        具体值的获取由调用者负责（或通过 adapter 预处理）。
        """
        # 尝试直接用 generic name 查找（如果 params 已经被预处理过）
        result = {}
        for key in ("p", "i", "d", "ff"):
            generic = f"pid.{axis}.{key}"
            if generic in params:
                result[key] = params[generic]
            else:
                result[key] = 0.0
        return result

    def _check_symptom(
        self, symptom: str, metrics: StepMetrics, thresholds: Dict
    ) -> Tuple[str, str]:
        """检查症状是否触发。返回 (severity, affected_metric)。"""
        th_overshoot = thresholds.get("overshoot_percent", {}).get("max", 15)
        th_rise = thresholds.get("rise_time_ms", {}).get("max", 120)
        th_settle = thresholds.get("settling_time_ms", {}).get("max", 350)
        th_osc = thresholds.get("oscillation_count", {}).get("max", 2)
        th_ss = thresholds.get("ss_error_percent", {}).get("max", 5)

        checks = {
            "overshoot":           (metrics.overshoot_percent, th_overshoot, "overshoot_percent"),
            "rise_time":           (metrics.rise_time_ms, th_rise, "rise_time_ms"),
            "settling_time":       (metrics.settling_time_ms, th_settle, "settling_time_ms"),
            "oscillation_count":   (float(metrics.oscillation_count), th_osc, "oscillation_count"),
            "steady_state_error":  (metrics.steady_state_error_percent, th_ss, "ss_error_percent"),
        }

        if symptom not in checks:
            return "none", symptom

        val, threshold, metric = checks[symptom]
        if val < 0:
            return "none", metric
        if val > threshold * 1.5:
            return "high", metric
        elif val > threshold:
            return "medium", metric
        elif symptom == "overshoot" and val > threshold * 0.8:
            return "low", metric
        return "none", metric

    def _generate_recommendations(
        self, metrics: StepMetrics, current_params: Dict[str, float],
        thresholds: Dict, axis: str,
    ) -> List[ParamRecommendation]:
        """生成参数修改建议（平台无关，使用 ParamRef）。"""
        recommendations = []
        seen_params: Dict[str, Tuple[str, str]] = {}
        severity_rank = {"high": 2, "medium": 1, "low": 0, "none": -1}

        sorted_rules = sorted(
            self._rules,
            key=lambda r: severity_rank.get(
                self._check_symptom(r.get("symptom", ""), metrics, thresholds)[0], -1
            ),
            reverse=True,
        )

        for rule in sorted_rules:
            symptom_key = rule.get("symptom", "")
            severity, _ = self._check_symptom(symptom_key, metrics, thresholds)
            if severity == "none":
                continue

            rule_params = [a["param"] for a in rule.get("adjust", [])]
            skip = False
            for p in rule_params:
                if p in seen_params:
                    prev_sev, prev_sym = seen_params[p]
                    if prev_sym == symptom_key and severity_rank.get(prev_sev, 0) >= severity_rank.get(severity, 0):
                        skip = True
                        break
            if skip:
                continue

            for adj in rule.get("adjust", []):
                param_key = adj["param"]  # "p", "i", "d"
                generic_name = f"pid.{axis}.{param_key}"

                current_val = current_params.get(param_key, 0.0)
                factor = adj.get("factor", 1.0)
                new_val = current_val * factor

                # Bounds
                bounds = self._bounds.get(param_key, (0.0, 10.0))
                new_val = float(np.clip(new_val, bounds[0], bounds[1]))

                confidence = Confidence.HIGH if severity == "high" else (
                    Confidence.MEDIUM if severity == "medium" else Confidence.LOW
                )

                recommendations.append(ParamRecommendation(
                    param=ParamRef(generic_name, axis=axis, category="pid"),
                    current=round(current_val, 6),
                    suggested=round(new_val, 6),
                    reason=rule.get("reason", f"{symptom_key} issue"),
                    confidence=confidence,
                    action=adj["action"],
                ))

                seen_params[param_key] = (severity, symptom_key)

        return recommendations

    # ------------------------------------------------------------------
    # 公开便利方法（供可视化、Skill 层调用）
    # ------------------------------------------------------------------

    def extract_step_response(self, flight_data: FlightData, axis: str) -> Dict[str, Any]:
        """提取指定轴的阶跃响应数据（供可视化使用）。

        Returns
        -------
        Dict[str, Any]
            steps: List[Dict] — 每个阶跃窗口含 time_ms, actual, desired, magnitude
            axis: str
            dt_ms: float
        """
        ax = axis.lower()
        if ax not in flight_data.pid:
            return {"axis": ax, "steps": [], "dt_ms": 4.0}

        sig = flight_data.pid[ax]
        dt_ms = self._estimate_dt_ms(sig)
        step_indices = detect_steps(sig.desired, dt_ms=dt_ms)
        time_ms = sig.timestamp_s * 1000.0

        steps_out = []
        for idx in step_indices:
            is_good, _ = _check_window_quality(sig.actual, sig.desired, idx)
            if not is_good:
                continue
            t_rel, act_win, magnitude = _extract_step_response(
                sig.desired, sig.actual, idx, dt_ms=dt_ms
            )
            t_global = time_ms[idx] + t_rel
            steps_out.append({
                "time_ms":   t_global.tolist(),
                "actual":    act_win.tolist(),
                "desired":   float(np.mean(sig.desired[max(0, idx):idx + 10])),
                "magnitude": magnitude,
            })

        return {"axis": ax, "steps": steps_out, "dt_ms": dt_ms}

    def get_step_response_data(self, flight_data: FlightData, axis: str) -> Dict[str, Any]:
        """提取指定轴的阶跃响应时间序列数据（供可视化使用）。

        Convenience wrapper around extract_step_response.
        """
        return self.extract_step_response(flight_data, axis)
