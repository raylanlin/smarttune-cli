"""
时域阶跃响应估计（适用于低采样率数据）。

对于低采样率（< 20 Hz）的 DataFlash RATE 日志，FFT 方法（Wiener 反卷积）
需要足够多的窗口才能平均出稳定估计，采样率太低时窗口数不足、噪声放大显著。
本模块改用**真实时域阶跃提取**：

算法：
1. 用一阶差分在 Desired 信号上检测阶跃跳变点（与 pid_reviewer._detect_steps 等价）
2. 在每个跳变点提取 [step_idx - before, step_idx + after] 窗口
3. 对多个窗口的响应按阶跃幅值归一化后平均
4. 输出归一化平均阶跃响应（0 = pre-step baseline，1 = 期望稳态）

相比旧实现的改进：
- 不再使用硬编码的 1 - exp(-t/0.05) 曲线
- 响应形状直接来自实际飞行数据
- 低采样率下（10-20 Hz）仍可得到有意义的平均阶跃响应
"""

from __future__ import annotations

import logging
from typing import Dict, Any, List, Optional, Tuple

import numpy as np

_log = logging.getLogger(__name__)

# 最小阶跃幅值阈值（rad/s），低于此的跳变视为噪声
_MIN_STEP_AMP = 0.3  # rad/s ≈ 17 deg/s

# 窗口参数（秒）
_WINDOW_BEFORE_S = 0.1   # 阶跃前保留时长
_WINDOW_AFTER_S = 1.5    # 阶跃后保留时长（低采样率响应更慢）

# 数据质量过滤常量
_MAX_ACTUAL_RADS = 26.2  # 最大合法角速率 ≈ 1500 deg/s
_MIN_STD_RADS = 0.05     # Actual 最小标准差（静止段过滤）


def _detect_steps_time_domain(
    desired: np.ndarray,
    sample_rate: float,
    threshold_frac: float = 0.3,
    min_step_interval_s: float = 0.5,
) -> List[int]:
    """
    在 Desired 信号中用一阶差分检测阶跃跳变点。

    Parameters
    ----------
    desired : np.ndarray
        期望值序列（rad/s）。
    sample_rate : float
        采样率（Hz）。
    threshold_frac : float
        相对于信号峰值的阶跃检测阈值（默认 30%）。
    min_step_interval_s : float
        两次阶跃之间的最小间隔（秒）。

    Returns
    -------
    List[int]
        阶跃起始点索引列表。
    """
    if len(desired) < 3:
        return []

    diff = np.abs(np.diff(desired))
    max_val = float(np.max(np.abs(desired)))
    threshold = max_val * threshold_frac if max_val > 1e-6 else _MIN_STEP_AMP

    min_interval = max(1, int(min_step_interval_s * sample_rate))
    step_indices: List[int] = []
    last_step = -min_interval

    for i in range(len(diff)):
        if diff[i] >= threshold and (i - last_step) >= min_interval:
            step_indices.append(i)
            last_step = i

    return step_indices


def _extract_window(
    desired: np.ndarray,
    actual: np.ndarray,
    step_idx: int,
    before: int,
    after: int,
) -> Optional[Tuple[np.ndarray, float]]:
    """
    提取并验证单个阶跃响应窗口，返回归一化响应。

    Returns
    -------
    (normalized_response, step_amplitude) 或 None（质量不合格）。

    归一化约定：0 = pre-step baseline，1 = 期望稳态幅值，方向与阶跃方向一致。
    """
    n = len(actual)
    start = max(0, step_idx - before)
    end = min(n, step_idx + after)

    act_win = actual[start:end]
    des_win = desired[start:end]  # 窗口内的 desired 切片

    if len(act_win) < before + 5:
        return None  # 窗口太短

    # NaN / Inf
    if not (np.all(np.isfinite(act_win)) and np.all(np.isfinite(des_win))):
        return None

    # 极端值过滤
    if float(np.max(np.abs(act_win))) > _MAX_ACTUAL_RADS:
        return None

    # 静止段过滤
    if float(np.std(act_win)) < _MIN_STD_RADS:
        return None

    # 计算阶跃幅值：用 des_win 的局部索引
    local_before = step_idx - start          # window 内阶跃点位置
    pre_len = min(local_before, max(2, before // 2))
    # np.diff 返回 diff[i] = x[i+1]-x[i]，所以 step_idx 是跳变前最后一点，
    # 跳变后的第一个点在 step_idx+1，即 des_win 的 local_before+1 处
    post_start = local_before + 1             # 跳变后首个采样点（窗口内索引）
    post_len = max(2, before // 2)

    if pre_len < 1 or post_start >= len(des_win):
        return None

    des_pre = float(np.mean(des_win[:pre_len]))
    des_post = float(np.mean(des_win[post_start: post_start + post_len]))
    step_amp = abs(des_post - des_pre)

    if step_amp < _MIN_STEP_AMP:
        return None

    # 归一化：以 actual 的 pre-baseline 为基点，step_amp 为单位，方向对齐 desired
    baseline = float(np.mean(act_win[:pre_len]))
    step_sign = 1.0 if (des_post - des_pre) >= 0 else -1.0

    norm = (act_win - baseline) * step_sign / step_amp
    norm = np.clip(norm, -0.5, 2.0)  # 最多 200% 超调，过大的视为数据异常

    return norm, step_amp


def estimate_step_response_time_domain(
    target: np.ndarray,
    actual: np.ndarray,
    sample_rate: Optional[float] = None,
    step_duration_s: float = 1.5,
) -> Dict[str, Any]:
    """
    时域阶跃响应估计（真实数据驱动）。

    通过检测 target 中的阶跃跳变点，提取每个阶跃后的 actual 响应窗口，
    按阶跃幅值归一化后跨窗口平均。

    Parameters
    ----------
    target : np.ndarray
        期望值序列（rad/s）。
    actual : np.ndarray
        实际值序列（rad/s）。
    sample_rate : float, optional
        采样率（Hz）。
    step_duration_s : float
        阶跃后观察时长（秒），默认 1.5 s。

    Returns
    -------
    Dict with keys:
        time           np.ndarray  相对时间（秒，0 = 阶跃时刻）
        step_response  np.ndarray  归一化平均阶跃响应
        valid_windows  int         参与平均的有效窗口数
        total_windows  int         检测到的候选窗口总数
        method         str         "time_domain_avg"
        error          str         仅失败时存在
    """
    n = len(target)
    if n < 20 or len(actual) < 20:
        return {
            "time": np.array([0.0]),
            "step_response": np.array([0.0]),
            "error": "数据不足（< 20 点）",
            "valid_windows": 0,
            "total_windows": 0,
            "method": "time_domain_avg",
        }

    if len(target) != len(actual):
        min_n = min(len(target), len(actual))
        target = target[:min_n]
        actual = actual[:min_n]
        n = min_n

    if sample_rate is None or sample_rate <= 0:
        sample_rate = 10.0
        _log.debug("time_domain: sample_rate 未知，使用默认 10 Hz")

    before = max(2, int(_WINDOW_BEFORE_S * sample_rate))
    after = max(5, int(step_duration_s * sample_rate))
    step_len = after

    time_arr = np.arange(step_len) * (1.0 / sample_rate)

    # 检测阶跃点
    step_indices = _detect_steps_time_domain(target, sample_rate)
    total_windows = len(step_indices)

    if total_windows == 0:
        return {
            "time": time_arr,
            "step_response": np.zeros(step_len),
            "error": "未检测到阶跃（target 变化量不足）",
            "valid_windows": 0,
            "total_windows": 0,
            "method": "time_domain_avg",
        }

    # 提取、归一化每个窗口
    normed_list: List[np.ndarray] = []
    for idx in step_indices:
        result = _extract_window(target, actual, idx, before, after)
        if result is None:
            continue
        norm, _ = result

        # 对齐到 step_len（末尾填充稳态值）
        if len(norm) < step_len:
            pad_val = float(norm[-1]) if len(norm) > 0 else 0.0
            norm = np.concatenate([norm, np.full(step_len - len(norm), pad_val)])
        else:
            norm = norm[:step_len]

        normed_list.append(norm)

    valid_windows = len(normed_list)

    if valid_windows == 0:
        return {
            "time": time_arr,
            "step_response": np.zeros(step_len),
            "error": "所有候选窗口数据质量不合格",
            "valid_windows": 0,
            "total_windows": total_windows,
            "method": "time_domain_avg",
        }

    step_out = np.mean(np.array(normed_list), axis=0)

    _log.debug(
        "time_domain: %d / %d 窗口有效，平均稳态值 %.3f",
        valid_windows, total_windows, float(np.mean(step_out[-max(1, step_len // 5):])),
    )

    return {
        "time": time_arr,
        "step_response": step_out,
        "valid_windows": valid_windows,
        "total_windows": total_windows,
        "method": "time_domain_avg",
    }


def compute_step_response_time_domain_for_axis(
    pid_data: Dict[str, Any],
    axis: str = "roll",
) -> Dict[str, Any]:
    """
    为指定轴计算时域阶跃响应（供 PIDReviewer 低采样率回退路径调用）。

    Parameters
    ----------
    pid_data : Dict
        LogParser.get_pid_data(axis) 返回值（含 Desired / Actual / time）。
    axis : str
        轴名（仅用于日志标识）。

    Returns
    -------
    Dict 含 axis, time_s, step_response, info（与 FFT 路径格式对齐）。
    """
    desired = pid_data.get("Desired", np.array([]))
    actual = pid_data.get("Actual", np.array([]))
    time_arr = pid_data.get("time", np.array([]))

    if len(desired) < 20 or len(actual) < 20:
        return {
            "axis": axis,
            "time_s": [],
            "step_response": [],
            "info": {"error": "数据不足", "method": "time_domain_avg"},
        }

    # 估算采样率
    if len(time_arr) > 1:
        diffs = np.diff(time_arr)
        diffs_valid = diffs[diffs > 1e-6]
        dt = float(np.median(diffs_valid)) if len(diffs_valid) > 0 else 0.1
        sample_rate = 1.0 / dt if dt > 0 else 10.0
    else:
        sample_rate = 10.0

    _log.debug("time_domain axis=%s sr=%.1f Hz n=%d", axis, sample_rate, len(desired))

    result = estimate_step_response_time_domain(
        target=np.asarray(desired, dtype=np.float64),
        actual=np.asarray(actual, dtype=np.float64),
        sample_rate=sample_rate,
        step_duration_s=1.5,
    )

    return {
        "axis": axis,
        "time_s": result["time"].tolist(),
        "step_response": result["step_response"].tolist(),
        "info": {k: v for k, v in result.items() if k not in ("time", "step_response")},
    }
