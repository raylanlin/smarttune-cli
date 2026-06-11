"""
阶跃响应估计 — 对齐 Betaflight 社区 PID Toolbox（bw1129/PIDtoolbox, PTstepcalc.m）。

本模块在 pid_reviewer.py 中按 platform 动态分派：
  smarttune.platform.betaflight.step_response_fft

与 ArduPilot 版（platform/ardupilot/step_response_fft.py，对齐 WebTools
PIDReview.js：Hanning 窗 + 93.75% 重叠 + 高斯 CDF 固定正则化）是两套
不同的社区参考实现，分别忠实对齐，互不混用。

PTstepcalc.m 对齐要点
---------------------
1. 分段：2 秒段（segment_length = sample_rate × 2），不加窗（无 Hanning）
2. 段筛选：max(|SP|) ≥ minInput（20 deg/s）且 ≤ maxInput（500 deg/s）
3. 反卷积：对 SP/GY 段做零填充 FFT，
       G = (fft(GY) · conj(fft(SP))) / (fft(SP) · conj(fft(SP)) + λ)
   λ 为相对小量正则化（λ = 1e-4 × max(Pxx)，量级无关）
4. 阶跃响应：cumsum(real(ifft(G))) 取前 500 ms
5. 段级质量控制：稳态均值（t ∈ [200, 500] ms）落在 (0.5, 3.0) 内才保留
   （SP 与 GY 同单位 deg/s，理想稳态 = 1.0；越界视为反卷积失败段）
6. 跨段平均：mean

已确认对齐 PTstepcalc.m 的部分：2s 段长 / 500ms 响应窗 / minInput=20 /
常数正则化 Wiener 反卷积 / cumsum / 稳态 QC / 跨段平均。
近似处理（上游精确常数待金标准对拍验证）：段步长（本实现 segment/4）、
QC 带 (0.5, 3.0)、零填充长度（100 点）。

References
----------
- https://github.com/bw1129/PIDtoolbox  (PTstepcalc.m，仓库已下架；
  本实现依据其公开算法描述及 WebTools PIDReview.js 源码注释中对
  PID-Analyzer/PIDtoolbox 同源算法的引用编写)
"""

from typing import Dict, Any, List, Optional
import numpy as np

# PTstepcalc.m 常数
_MIN_INPUT_DEG_S = 20.0      # minInput — 段内 SP 峰值下限
_MAX_INPUT_DEG_S = 500.0     # maxInput — 段内 SP 峰值上限
_SEGMENT_DURATION_S = 2.0    # 2 秒分析段
_RESPONSE_DURATION_S = 0.5   # 500 ms 阶跃响应窗
_PAD_SAMPLES = 100           # FFT 前零填充
_QC_STEADY_LO = 0.5          # 稳态 QC 下限（理想值 1.0）
_QC_STEADY_HI = 3.0          # 稳态 QC 上限
_REG_FACTOR = 1e-4           # Wiener 正则化 λ = _REG_FACTOR × max(Pxx)


def estimate_step_response(
    target: np.ndarray,
    actual: np.ndarray,
    sample_rate: float,
    window_size: Optional[int] = None,
    step_duration_s: float = _RESPONSE_DURATION_S,
    min_target_amplitude: float = _MIN_INPUT_DEG_S,
    max_target_amplitude: float = _MAX_INPUT_DEG_S,
) -> Dict[str, Any]:
    """
    估计阶跃响应（对齐 PID Toolbox PTstepcalc.m）。

    Parameters
    ----------
    target : np.ndarray
        Setpoint（SP）序列，deg/s。
    actual : np.ndarray
        滤波后陀螺仪（GY / gyroADC）序列，deg/s。
    sample_rate : float
        采样率（Hz）。
    window_size : int, optional
        覆盖默认 2 秒段长（点数）。仅用于测试；常规调用勿传。
    step_duration_s : float
        阶跃响应窗时长（秒，默认 0.5 = PTB 的 500 ms）。
    min_target_amplitude : float
        段内 SP 峰值下限（PTB minInput，默认 20 deg/s）。
    max_target_amplitude : float
        段内 SP 峰值上限（PTB maxInput，默认 500 deg/s）。

    Returns
    -------
    Dict with keys: time, step_response, valid_windows, total_windows,
    skipped_quality, window_size, sample_rate, method
    """
    n = len(target)

    seg_len = int(round(sample_rate * _SEGMENT_DURATION_S))
    if window_size is not None:
        seg_len = int(window_size)
    seg_len = max(seg_len, 8)

    wnd = int(round(sample_rate * step_duration_s))
    wnd = max(min(wnd, seg_len), 2)
    time_arr = np.arange(wnd) / sample_rate

    if n < seg_len:
        return {
            "time": np.array([0.0]),
            "step_response": np.array([0.0]),
            "error": "数据太短",
            "valid_windows": 0,
            "total_windows": 0,
        }

    # 段步长：segment/4（75% 重叠）。PTB 上游的精确步长以
    # PTstepcalc.m 为准 — 只影响平均段数，不影响单段数学。
    seg_step = max(seg_len // 4, 1)
    num_segments = (n - seg_len) // seg_step + 1

    # 稳态 QC 区间掩码（t ∈ [200, 500] ms）
    qc_mask = (time_arr >= 0.2) & (time_arr <= step_duration_s)
    has_qc = bool(np.any(qc_mask))

    all_steps: List[np.ndarray] = []
    skipped_quality = 0

    for i in range(num_segments):
        start = i * seg_step
        end = start + seg_len
        sp = target[start:end]
        gy = actual[start:end]

        # ── SmartTune 附加数据预检（PTB 之外的脏数据防御）──────
        if np.any(~np.isfinite(sp)) or np.any(~np.isfinite(gy)):
            skipped_quality += 1
            continue
        if float(np.max(np.abs(gy))) > 1500.0:  # deg/s，物理极端值
            skipped_quality += 1
            continue

        # ── PTB 段筛选：minInput ≤ max(|SP|) ≤ maxInput ─────────
        sp_max = float(np.max(np.abs(sp)))
        if sp_max < min_target_amplitude or sp_max > max_target_amplitude:
            continue

        # ── 零填充 FFT + 常数正则化 Wiener 反卷积（PTB 核心）────
        a = np.concatenate([sp, np.zeros(_PAD_SAMPLES)])
        b = np.concatenate([gy, np.zeros(_PAD_SAMPLES)])
        fa = np.fft.fft(a)
        fb = np.fft.fft(b)

        pxx = (fa * np.conj(fa)).real  # 自谱，纯实数
        lam = _REG_FACTOR * float(np.max(pxx)) if pxx.size else 1e-9
        lam = max(lam, 1e-30)

        G = (fb * np.conj(fa)) / (pxx + lam)

        impulse = np.fft.ifft(G).real
        step = np.cumsum(impulse[:wnd])

        # ── PTB 段级 QC：稳态均值必须接近 1 ─────────────────────
        if has_qc:
            steady = float(np.mean(step[qc_mask]))
            if not (_QC_STEADY_LO < steady < _QC_STEADY_HI):
                skipped_quality += 1
                continue

        all_steps.append(step)

    if not all_steps:
        return {
            "time": time_arr,
            "step_response": np.zeros(wnd),
            "error": "无有效窗口",
            "valid_windows": 0,
            "total_windows": num_segments,
        }

    step_out = np.mean(np.array(all_steps), axis=0)

    return {
        "time": time_arr,
        "step_response": step_out,
        "valid_windows": len(all_steps),
        "total_windows": num_segments,
        "skipped_quality": skipped_quality,
        "window_size": seg_len,
        "sample_rate": sample_rate,
        "method": "pidtoolbox_ptstepcalc",
    }


def compute_step_response_for_axis(
    pid_data: Dict[str, np.ndarray],
    axis: str = "roll",
    imu_data: Optional[Dict[str, np.ndarray]] = None,
) -> Dict[str, Any]:
    """
    为指定轴计算阶跃响应。

    Betaflight Blackbox 的 Actual 即 gyroADC（滤波后陀螺仪），与 PTB 的
    GY 输入一致；imu_data 路径仅在 Actual 来自其他低采样率源时启用
    （提供更高采样率的陀螺仪 + 时间均匀化重采样）。
    """
    desired = pid_data.get("Desired", np.array([]))
    actual_rate = pid_data.get("Actual", np.array([]))
    time_rate = pid_data.get("time", np.array([]))

    use_imu = imu_data is not None and len(imu_data.get("GyrX", [])) > 0

    if use_imu:
        axis_idx = {"roll": 0, "pitch": 1, "yaw": 2}.get(axis.lower(), 0)
        gyr_key = ["GyrX", "GyrY", "GyrZ"][axis_idx]

        actual_imu = imu_data[gyr_key]
        time_imu = imu_data["time"]

        if len(time_rate) > 1 and len(time_imu) > 1:
            from scipy import interpolate

            interp_desired = interpolate.interp1d(
                time_rate, desired, kind="linear",
                bounds_error=False, fill_value=(desired[0], desired[-1]),
            )
            desired_resampled = interp_desired(time_imu)

            # 时间均匀化（FFT 要求均匀采样）
            new_time = np.linspace(time_imu[0], time_imu[-1], len(time_imu))
            desired_resampled = np.interp(new_time, time_imu, desired_resampled)
            actual_imu = np.interp(new_time, time_imu, actual_imu)

            actual = actual_imu
            desired = desired_resampled
            time_arr = new_time
        else:
            actual = actual_rate
            time_arr = time_rate
    else:
        actual = actual_rate
        time_arr = time_rate

    if len(desired) < 100 or len(actual) < 100:
        return {
            "axis": axis,
            "time_s": [],
            "step_response": [],
            "info": {"error": "数据不足"},
        }

    # 估计采样率
    if len(time_arr) > 1:
        dt = np.median(np.diff(time_arr))
        sample_rate = 1.0 / dt if dt > 0 else 400.0
    else:
        sample_rate = 400.0

    result = estimate_step_response(
        target=desired,
        actual=actual,
        sample_rate=sample_rate,
        # PTB minInput = 20 deg/s（不再用旧实现的 3.0 妥协值；
        # 段太少时宁可报告 valid_windows=0 也不引入弱激励噪声段）
        min_target_amplitude=_MIN_INPUT_DEG_S,
        step_duration_s=_RESPONSE_DURATION_S,
    )

    time_out = result.get("time", np.array([]))
    step_resp = result.get("step_response", np.array([]))

    return {
        "axis": axis,
        "time_s": time_out.tolist(),
        "step_response": step_resp.tolist(),
        "info": {k: v for k, v in result.items() if k not in ("time", "step_response")},
    }
