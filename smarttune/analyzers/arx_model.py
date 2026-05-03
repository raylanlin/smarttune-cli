"""
ARX 模型系统辨识（适用于低采样率数据）。

对于 6-10 Hz 的 DataFlash RATE 日志，
ARX 模型比 FFT 方法更稳定，直接在离散时间域建模。
"""

import logging
from typing import Dict, Any, Tuple
import numpy as np

_log = logging.getLogger(__name__)

# 条件数告警阈值（P3-#7）：超过此值视为回归矩阵接近奇异，
# 此时 lstsq 解仍会返回但数值上不稳定，下游模型参数不可信。
_ARX_COND_WARN_THRESHOLD = 1e8


def estimate_delay(u: np.ndarray, y: np.ndarray, max_delay: int = 20) -> int:
    """
    用互相关估计纯延迟。

    Parameters
    ----------
    u : np.ndarray
        输入信号。
    y : np.ndarray
        输出信号。
    max_delay : int
        最大延迟（拍数）。

    Returns
    -------
    int
        估计的延迟（拍数）。
    """
    n = min(len(u), len(y))
    if n < max_delay * 2:
        return 0

    # 去均值
    u_centered = u[:n] - np.mean(u[:n])
    y_centered = y[:n] - np.mean(y[:n])

    # 互相关
    corr = np.correlate(y_centered, u_centered, mode='full')
    lags = np.arange(-n + 1, n)

    # 找正半轴第一个峰值
    pos_mask = (lags >= 0) & (lags <= max_delay)
    if not np.any(pos_mask):
        return 0

    pos_corr = corr[pos_mask]
    pos_lags = lags[pos_mask]

    peak_idx = np.argmax(pos_corr)
    return int(pos_lags[peak_idx])


def arx_identify(
    u: np.ndarray,
    y: np.ndarray,
    na: int = 2,
    nb: int = 2,
    d: int = 0,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    ARX 模型辨识（最小二乘法）。

    ARX 模型：
        y[k] + a1*y[k-1] + ... + an*y[k-n] = b0*u[k-d] + ... + bm*u[k-d-m]

    Parameters
    ----------
    u : np.ndarray
        输入信号。
    y : np.ndarray
        输出信号。
    na : int
        自回归阶数（A 多项式阶数）。
    nb : int
        外生输入阶数（B 多项式阶数）。
    d : int
        纯延迟（拍数）。

    Returns
    -------
    Tuple[a, b]
        a: A 多项式系数 [1, a1, a2, ...]
        b: B 多项式系数 [b0, b1, ..., bm]
    """
    N = len(y)
    M = na + nb  # 待估参数数量
    start_idx = max(na, nb + d) + 1

    if N < start_idx + M:
        # 数据不足，返回默认模型
        return np.array([1.0, -0.5, 0.0]), np.array([0.5, 0.0])

    # 构建回归矩阵
    Phi = np.zeros((N - start_idx, na + nb))
    Y = np.zeros(N - start_idx)

    for k in range(start_idx, N):
        row = k - start_idx
        Y[row] = y[k]

        # 填入 y 的历史值（负号）
        for i in range(1, na + 1):
            Phi[row, i - 1] = -y[k - i]

        # 填入 u 的历史值
        for i in range(nb):
            idx = k - d - i
            Phi[row, na + i] = u[idx] if idx >= 0 else 0

    # 最小二乘求解
    # P3-#7: lstsq 即使在回归矩阵接近奇异时仍会返回结果，但解的数值不稳定。
    # 对于低采样率（6-10Hz）+ 较大阶数（na=3, nb=2）的组合，Phi 容易病态，
    # 这里用条件数做一次诊断，超阈值时记录警告（不阻断流程，因为下游 fit
    # quality 检查会进一步过滤；告警用于让用户知道结果可能不可信）。
    try:
        cond = float(np.linalg.cond(Phi))
    except (np.linalg.LinAlgError, ValueError):
        cond = float("inf")
    if cond > _ARX_COND_WARN_THRESHOLD:
        _log.warning(
            "ARX 回归矩阵条件数 %.2e > %.0e，模型参数可能不可信。"
            "建议：增加采集时长、减小阶数（na/nb）、或检查输入是否充分激励。",
            cond, _ARX_COND_WARN_THRESHOLD,
        )

    try:
        theta, _residual, _rank, _sv = np.linalg.lstsq(Phi, Y, rcond=None)
    except Exception:
        # 求解失败，返回默认模型
        return np.array([1.0, -0.5, 0.0]), np.array([0.5, 0.0])

    a = np.concatenate([[1.0], theta[:na]])
    b = theta[na:na + nb]

    return a, b


def arx_step_response(
    a: np.ndarray,
    b: np.ndarray,
    d: int = 0,
    N: int = 100,
    oversample: int = 10,
    n_steps: int = None,
) -> np.ndarray:
    """
    从 ARX 模型计算阶跃响应（递推仿真 + 插值平滑）。

    Parameters
    ----------
    a : np.ndarray
        A 多项式系数。
    b : np.ndarray
        B 多项式系数。
    d : int
        纯延迟（拍数）。
    N : int
        响应长度（原始采样点数）。
    oversample : int
        过采样倍数（使曲线平滑）。

    Returns
    -------
    np.ndarray
        阶跃响应（归一化到稳态值，过采样后长度 N*oversample）。
    """
    if n_steps is not None:
        N = n_steps
        oversample = 1  # 不插值，直接返回 N 点
    from scipy import interpolate
    
    # 1. 在原始采样率下计算
    y = np.zeros(N)
    u_step = np.ones(N)

    na = len(a) - 1
    nb = len(b)

    for k in range(1, N):
        for i in range(1, na + 1):
            if k - i >= 0:
                y[k] -= a[i] * y[k - i]

        for j in range(nb):
            idx = k - d - j
            if idx >= 0:
                y[k] += b[j] * u_step[idx]

    # 归一化
    steady_state = y[-1] if abs(y[-1]) > 0.01 else 1.0
    if steady_state < 0:
        y = -y
        steady_state = -steady_state

    if abs(steady_state) > 0.01:
        y = y / abs(steady_state)

    # 2. 用三次样条插值平滑
    x_coarse = np.arange(N)
    x_fine = np.linspace(0, N - 1, N * oversample)
    
    # 三次样条插值
    cs = interpolate.CubicSpline(x_coarse, y)
    y_fine = cs(x_fine)
    
    # 限制范围（避免插值产生异常值）
    y_fine = np.clip(y_fine, -0.5, 2.0)

    return y_fine


def estimate_step_response_arx(
    target: np.ndarray,
    actual: np.ndarray,
    sample_rate: float,
    na: int = 2,
    nb: int = 2,
    step_duration_s: float = 2.0,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    """
    用 ARX 模型估计阶跃响应。

    Parameters
    ----------
    target : np.ndarray
        目标值序列（输入）。
    actual : np.ndarray
        实际值序列（输出）。
    sample_rate : float
        采样率（Hz）。
    na : int
        自回归阶数。
    nb : int
        外生输入阶数。
    step_duration_s : float
        阶跃响应时长（秒）。

    Returns
    -------
    Tuple[time, response, info]
    """
    dt = 1.0 / sample_rate
    N = int(step_duration_s / dt)
    N = min(N, len(target) // 2)

    if len(target) < 20:
        return np.array([0.0]), np.array([0.0]), {"error": "数据不足"}

    # 去均值
    u = target - np.mean(target[:10])
    y = actual - np.mean(actual[:10])

    # 估计延迟
    d = estimate_delay(u, y, max_delay=min(10, len(u) // 4))

    # ARX 辨识
    a, b = arx_identify(u, y, na, nb, d)

    # 计算阶跃响应（过采样 10 倍使曲线平滑）
    step_resp = arx_step_response(a, b, d, N=N, oversample=10)

    # 时间数组（对应过采样）
    dt_fine = dt / 10  # 过采样后的时间间隔
    time_arr = np.arange(len(step_resp)) * dt_fine

    info = {
        "method": "arx",
        "sample_rate": sample_rate,
        "delay_samples": d,
        "delay_time": d * dt,
        "a": a.tolist(),
        "b": b.tolist(),
        "na": na,
        "nb": nb,
    }

    return time_arr, step_resp, info