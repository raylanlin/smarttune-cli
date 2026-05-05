"""
阶跃响应估计（对齐 ArduPilot WebTools PIDReview.js / Plasmatree PID-Analyzer）。

算法流程（完全复现 WebTools redraw_step + Libraries/Array_Math.js + Libraries/fft.js）：

1. 分窗：Hanning 窗，window_size 点，spacing = round(window_size / 16)
2. 每窗做 fft.js realTransform（单边 FFT，DC/Nyquist 乘 1/N，其余乘 2/N）
3. to_double_sided：正频率×0.5，负频率共轭×(-0.5)
4. 构造 SNR 正则化：累积高斯积分 → 归一化 → 镜像 → (1 - sn + eps) → ×10 → 倒数
5. Wiener 反卷积：H = Pyx / (Pxx + sn) 其中 sn 加到 Pxx 实部
6. IFFT → 脉冲响应 → 累积和 → 阶跃响应
7. 跨窗口平均

数据源（与 WebTools 一致）：
- input = Tar（RATE.Des，单位与日志一致，无额外转换）
- output = IMU.Gyr（优先）→ 回退 PIDR.Act
"""

from typing import Dict, Any, List, Tuple, Optional
import numpy as np


def _hanning(n: int) -> np.ndarray:
    """Hanning 窗，与 fft.js/hanning 一致。"""
    scale = 2.0 * np.pi / (n - 1)
    return 0.5 - 0.5 * np.cos(scale * np.arange(n))


def _real_length(n: int) -> int:
    """单边 FFT 长度，与 fft.js/real_length 一致。"""
    return n // 2 + 1


def _to_double_sided(single: np.ndarray) -> np.ndarray:
    """
    单边复谱转双边复谱（复现 WebTools to_double_sided）。

    single: complex64 ndarray, shape (real_len,)
        rfft 结果（已缩放：DC/Nyq *1/N, 其余 *2/N）
    returns: complex64 ndarray, shape (2*real_len - 2,)
        full_len = 2 * (real_len - 1)
        DC / Nyquist 原样保留
        正频率 *0.5；负频率位置存放正频率的共轭 *0.5
    """
    real_len = len(single)
    full_len = 2 * (real_len - 1)
    ret = np.zeros(full_len, dtype=np.complex64)

    # DC
    ret[0] = single[0]
    # Nyquist
    ret[real_len - 1] = single[real_len - 1]

    # 正/负频率向量化（P3-#5）：原 Python 循环逐元素填充，对典型 ~400 点窗口
    # 执行数百次。numpy slicing + 共轭批量赋值，性能提升一到两个数量级。
    if real_len > 2:
        pos = single[1:real_len - 1] * 0.5
        ret[1:real_len - 1] = pos
        # 负频率位置 full_len-1 .. real_len，对应正频率 1..real_len-2 的反向共轭
        ret[full_len - 1:real_len - 1:-1] = np.conj(pos)

    return ret


def _complex_mul(a: np.ndarray, b: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    复向量逐元素乘法 a * b（复现 WebTools complex_mul）。

    返回 (real_part, imag_part)，各自为独立 ndarray。
    """
    ra = a.real
    ia = a.imag
    rb = b.real
    ib = b.imag
    return (ra * rb - ia * ib, ra * ib + ia * rb)


def _complex_div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    复向量逐元素除法 a / b（复现 WebTools complex_div）。

    返回 complex64 ndarray。
    """
    ra = a.real
    ia = a.imag
    rb = b.real
    ib = b.imag
    denom = rb * rb + ib * ib
    real_part = (ra * rb + ia * ib) / denom
    imag_part = (ia * rb - ra * ib) / denom  # (bc - ad) / denom
    return real_part + 1j * imag_part


def estimate_step_response(
    target: np.ndarray,
    actual: np.ndarray,
    sample_rate: float,
    window_size: Optional[int] = None,
    step_duration_s: float = 0.5,
    min_target_amplitude: float = 20.0,  # 20 deg/s（对齐 WebTools 阈值）
    cutfreq: float = 25.0,
) -> Dict[str, Any]:
    """
    估计阶跃响应（复现 WebTools redraw_step 核心算法）。

    Parameters
    ----------
    target : np.ndarray
        目标值序列（单位与 min_target_amplitude 一致）。
    actual : np.ndarray
        实际值序列（与 target 同单位）。
    sample_rate : float
        采样率（Hz）。
    window_size : int, optional
        FFT 窗口大小（点数），默认基于采样率：1s 窗口，2 的幂，最小 64。
    step_duration_s : float
        阶跃响应可视时长（秒，默认 0.5）。
    min_target_amplitude : float
        窗口最小目标幅值阈值（默认 20 deg/s，对齐 WebTools 阈值）。
        数据单位为 deg/s（BF gyroADC / AP RATE 解析后均为 deg/s）。
    cutfreq : float
        SNR 正则化截止频率（Hz，默认 25）。

    Returns
    -------
    Dict with keys: time, step_response, valid_windows, total_windows, window_size, sample_rate, method
    """
    n = len(target)

    # 窗口大小：1 秒，2 的幂，最小 64
    if window_size is None:
        win = int(sample_rate)
        # 向上取到 2 的幂
        window_size = 1
        while window_size < win:
            window_size *= 2
        window_size = min(window_size, n // 2)
        window_size = max(window_size, 64)

    if n < window_size:
        return {
            "time": np.array([0.0]),
            "step_response": np.array([0.0]),
            "error": "数据太短",
            "valid_windows": 0,
            "total_windows": 0,
        }

    dt = 1.0 / sample_rate
    real_len = _real_length(window_size)  # window_size//2 + 1

    # ------------------------------------------------------------
    # 窗口参数（与 WebTools 一致）
    # ------------------------------------------------------------
    window = _hanning(window_size)
    window_spacing = int(np.round(window_size / 16))  # Math.round
    num_windows = (n - window_size) // window_spacing + 1

    step_end = min(int(step_duration_s / dt), window_size)
    time_arr = np.arange(step_end) * dt
    full_len = 2 * (real_len - 1)  # 双边谱长度

    # ------------------------------------------------------------
    # FFT 缩放因子（与 WebTools run_fft 一致）
    # DC/Nyquist: 1/N，其余: 2/N
    # ------------------------------------------------------------
    scale = np.ones(real_len, dtype=np.float64)
    scale[0] = 1.0 / window_size
    for j in range(1, real_len - 1):
        scale[j] = 2.0 / window_size
    scale[real_len - 1] = 1.0 / window_size

    # ------------------------------------------------------------
    # 构造 SNR 正则化（复现 WebTools redraw_step 精确流程）
    # ------------------------------------------------------------
    # bins 对应频率 (Hz)
    bins = np.fft.rfftfreq(window_size, d=dt)
    # 找到 cutfreq 所在 bin 索引
    bin_idx = int(np.searchsorted(bins, cutfreq, side="right"))
    len_lpf = bin_idx
    len_lpf += len_lpf - 2  # account for double sided, DC and Nyquist not copied
    len_lpf = max(len_lpf, 1)

    radius = int(np.ceil(len_lpf * 0.5))
    sigma = len_lpf / 6.0

    # 累积高斯积分（完整兑现 fft.js 逐元素循环）
    sn = np.ones(real_len, dtype=np.float64)
    last_sn = 0.0
    for j in range(min(len_lpf, real_len)):
        sn[j] = last_sn + np.exp((-0.5 / sigma ** 2) * (j - radius) ** 2)
        last_sn = sn[j]

    # 归一化到 1
    if last_sn > 0:
        sn[:min(len_lpf, real_len)] /= last_sn

    # 镜像拼接（精确复现 WebTools 语法）
    sn = np.concatenate([sn, sn[1:real_len - 1][::-1]])

    # Scale: -1 → offset 1 + 1e-9 → ×10 → inverse
    sn = 1.0 / (10.0 * (1.0 - sn + 1e-9))

    # ------------------------------------------------------------
    # 分窗 FFT → Wiener 反卷积 → 阶跃响应
    # ------------------------------------------------------------
    all_steps: List[np.ndarray] = []
    skipped_quality = 0

    for i in range(num_windows):
        start = i * window_spacing
        end = start + window_size

        raw_tar = target[start:end]
        raw_act = actual[start:end]

        # ── 数据质量预检（在加窗之前检查原始信号） ──────────
        # 1. NaN/Inf 检测
        if np.any(~np.isfinite(raw_act)) or np.any(~np.isfinite(raw_tar)):
            skipped_quality += 1
            continue

        # 2. Actual 极端值检测：飞行中角速度 > 1500 deg/s 异常
        #    数据单位为 deg/s（BF gyroADC / AP RATE 均为 deg/s）
        act_max = float(np.max(np.abs(raw_act)))
        if act_max > 1500.0:
            skipped_quality += 1
            continue

        # 3. Actual/Target 比例异常检测
        tar_peak = float(np.max(np.abs(raw_tar)))
        if tar_peak > 1e-3 and act_max > tar_peak * 4.0:
            skipped_quality += 1
            continue

        # 4. Actual 标准差过小（静止段，无有效响应）
        #    单位 deg/s，5 deg/s 标准差以下视为静止
        if float(np.std(raw_act)) < 5.0:
            skipped_quality += 1
            continue

        tar_win = raw_tar * window
        act_win = raw_act * window

        # 幅值阈值（与 WebTools TarMax < 20.0 一致）
        tar_max = np.max(np.abs(tar_win))
        if tar_max < min_target_amplitude:
            continue

        # FFT（fft.js realTransform → rfft）
        tar_fft = np.fft.rfft(tar_win) * scale
        act_fft = np.fft.rfft(act_win) * scale

        # 转双边谱
        X = _to_double_sided(tar_fft)
        Y = _to_double_sided(act_fft)

        # 共轭
        Xcon = np.conj(X)

        # Pyx = Y * conj(X), Pxx = X * conj(X)
        # 使用 _complex_mul 得到 (real, imag) 元组
        Pyx_real, Pyx_imag = _complex_mul(Y, Xcon)
        Pxx_real, Pxx_imag = _complex_mul(X, Xcon)

        # 加 SNR 正则化到 Pxx 实部（与 WebTools Pxx[0] = array_add(Pxx[0], sn) 一致）
        Pxx_real += sn

        # 组装复数组
        Pyx = Pyx_real + 1j * Pyx_imag
        Pxx = Pxx_real + 1j * Pxx_imag

        # 传递函数 H = Pyx / Pxx
        H = _complex_div(Pyx, Pxx)

        # IFFT → 脉冲响应（取实部）
        impulse = np.fft.ifft(H).real

        # 累积和 → 阶跃响应
        step = np.cumsum(impulse[:step_end])

        # 5. 阶跃响应质量检查：超调 > 300% 视为异常窗口
        if step_end > 5:
            tail_val = float(np.mean(step[-max(5, step_end // 4):]))
            if abs(tail_val) > 1e-3:
                peak_dev = float(np.max(step) - tail_val)
                overshoot_est = peak_dev / abs(tail_val) * 100.0
                if overshoot_est > 300.0:
                    skipped_quality += 1
                    continue

        all_steps.append(step)

    if not all_steps:
        return {
            "time": time_arr,
            "step_response": np.zeros(step_end),
            "error": "无有效窗口",
            "valid_windows": 0,
            "total_windows": num_windows,
        }

    # 均值平均（与 WebTools 一致）
    step_array = np.array(all_steps)
    step_out = np.mean(step_array, axis=0)

    info = {
        "valid_windows": len(all_steps),
        "total_windows": num_windows,
        "skipped_quality": skipped_quality,
        "window_size": window_size,
        "sample_rate": sample_rate,
        "method": "webtools_fft",
    }

    return {
        "time": time_arr,
        "step_response": step_out,
        **info,
    }


def compute_step_response_for_axis(
    pid_data: Dict[str, np.ndarray],
    axis: str = "roll",
    imu_data: Optional[Dict[str, np.ndarray]] = None,
) -> Dict[str, Any]:
    """
    为指定轴计算阶跃响应（对齐 WebTools 数据源逻辑）。

    优先使用 IMU 陀螺仪数据（高采样率）作为实际响应。
    """
    desired = pid_data.get("Desired", np.array([]))
    actual_rate = pid_data.get("Actual", np.array([]))
    time_rate = pid_data.get("time", np.array([]))

    use_imu = imu_data is not None and len(imu_data.get("GyrX", [])) > 0

    if use_imu:
        axis_idx = {"roll": 0, "pitch": 1, "yaw": 2}.get(axis.lower(), 0)
        gyr_keys = ["GyrX", "GyrY", "GyrZ"]
        gyr_key = gyr_keys[axis_idx]

        actual_imu = imu_data[gyr_key]
        time_imu = imu_data["time"]

        if len(time_rate) > 1 and len(time_imu) > 1:
            from scipy import interpolate

            interp_desired = interpolate.interp1d(
                time_rate, desired, kind="linear",
                bounds_error=False, fill_value=(desired[0], desired[-1]),
            )
            desired_resampled = interp_desired(time_imu)

            # 时间均匀化（DataFlash 时间戳可能不均匀，FFT 要求均匀采样）
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

    # 阈值：数据单位为 deg/s（BF gyroADC / AP RATE 解析后均为 deg/s）
    # 使用 3.0 deg/s 作为最小目标幅值，在窗口数量和 SNR 之间取得平衡
    min_amp = 3.0  # deg/s

    result = estimate_step_response(
        target=desired,
        actual=actual,
        sample_rate=sample_rate,
        min_target_amplitude=min_amp,
        step_duration_s=0.5,
        cutfreq=25.0,
    )

    time_out = result.get("time", np.array([]))
    step_resp = result.get("step_response", np.array([]))

    return {
        "axis": axis,
        "time_s": time_out.tolist(),
        "step_response": step_resp.tolist(),
        "info": {k: v for k, v in result.items() if k not in ("time", "step_response")},
    }
