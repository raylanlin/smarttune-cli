"""
阶跃响应估计（ArduPilot 专用 — 对齐 WebTools PIDReview.js / Plasmatree PID-Analyzer）。

本模块在 pid_reviewer.py 中按 platform 动态分派：
  smarttune.platform.ardupilot.step_response_fft

算法流程（完全复现 WebTools redraw_step + Libraries/Array_Math.js + Libraries/fft.js；
2026-09-13 对着 raylanlin/WebTools@cb46984f 的 PIDReview.js:1031-1160 重核一遍）：

1. 分窗：Hanning 窗，window_size 点，spacing = round(window_size / 16)
2. 每窗做 fft.js realTransform（单边 FFT，DC/Nyquist 乘 1/N，其余乘 2/N）
3. to_double_sided：正频率×0.5，负频率共轭×(-0.5)
4. 构造 SNR 正则化：累积高斯积分 → 归一化 → 镜像 → (1 - sn + eps) → ×10 → 倒数
5. Wiener 反卷积：H = Pyx / (Pxx + sn) 其中 sn 加到 Pxx 实部
6. IFFT → 脉冲响应 → 累积和 → 阶跃响应
7. 跨窗口平均（算术平均，与上游一致）

逐项核对结果（对应 PIDReview.js 行号）：
  hanning(window_size)                 :1064  ✓
  window_spacing = round(N/16)         :1068  ✓
  step_end = min(ceil(0.5/dt), N)      :1072  ✓
  cutfreq = 25                         :1082  ✓（低采样率时夹到 0.8×Nyquist，见下）
  len_lpf = bins.findIndex(x >= cut)   :1083  ✓（v3.4.0 修正 off-by-one）
  len_lpf += len_lpf - 2               :1084  ✓
  radius = ceil(len_lpf*0.5)           :1085  ✓
  sigma  = len_lpf / 6                 :1086  ✓
  累积高斯 + 归一化                     :1091-1098  ✓
  镜像 sn[1:real_len-1].reverse()      :1100  ✓
  (×-1, +1+1e-9, ×10, 取倒)            :1103-1106 ✓
  TarMax < 20 跳过（加窗后）            :1136  ✓
  Pxx.real += sn                       :1157  ✓

有意偏离（上游不适用的场景，不是不一致）：
  · cutfreq ≥ Nyquist 时夹到 0.8×Nyquist。上游 findIndex 返回 -1 → len_lpf 负值
    → sn 全 1 → H≈0，退化为平坦零响应。.bin（400 Hz+）永远碰不到，
    但 .tlog（1-50 Hz）每次都碰 —— 夹住并在 info.cutfreq_clamped 标注。
  · 额外的窗口数据质量预检（NaN / >1500 deg/s / act≫4×tar / 静止段 /
    超调 >300%）—— 上游无此步，这些窗口在 info.skipped_quality 计数。

数据源（与 WebTools 一致）：
- input  = Tar（PIDx.Tar / RATE.*Des）
- output = Act（PIDx.Act / RATE.R,P,Y）—— **不**替换为 IMU.Gyr；
  PIDReview.js:1600/1608 构造 test set 时取的就是 Act，Readme 的说法也是
  "between the target and actual signals"。
  IMU 陀螺仍可通过 prefer_imu=True 手动开启（需插值重采样，与上游不同源）。
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

    single: complex ndarray, shape (real_len,)
        rfft 结果（已缩放：DC/Nyq *1/N, 其余 *2/N）
    returns: complex128 ndarray, shape (2*real_len - 2,)
        full_len = 2 * (real_len - 1)
        DC / Nyquist 原样保留
        正频率 *0.5；负频率位置存放正频率的共轭 *0.5

        精度说明：JS 的 Number 是 float64，这里用 complex128（双 float64）
        保持与 WebTools 同精度；旧实现用 complex64 反而低于参考实现。
    """
    real_len = len(single)
    full_len = 2 * (real_len - 1)
    ret = np.zeros(full_len, dtype=np.complex128)

    # DC
    ret[0] = single[0]
    # Nyquist
    ret[real_len - 1] = single[real_len - 1]

    # 正/负频率向量化（P3-#5）：原 Python 循环逐元素填充，对典型 ~400 点窗口
    # 执行数百次。numpy slicing + 共轭批量赋值，性能提升一到两个数量级。
    if real_len > 2:
        pos = single[1 : real_len - 1] * 0.5
        ret[1 : real_len - 1] = pos
        # 负频率位置 full_len-1 .. real_len，对应正频率 1..real_len-2 的反向共轭
        ret[full_len - 1 : real_len - 1 : -1] = np.conj(pos)

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

    # SNR 正则化截止频率不能超过 Nyquist。WebTools 只处理 400 Hz+ 的 .bin，
    # cutfreq=25 永远在带内；findIndex 对 25 Hz 超出频带时返回 -1，导致
    # len_lpf 为负、sn 全为 1、H≈0（退化为平坦零响应）。遥测日志（.tlog，
    # 1-50 Hz）会直接落进这个退化分枝，所以这里显式夹到 0.8×Nyquist
    # 并在结果里标注 —— 这是相对参考实现的**有意偏离**，不是不一致。
    nyquist = sample_rate * 0.5
    cutfreq_used = cutfreq
    cutfreq_clamped = False
    if cutfreq >= nyquist:
        cutfreq_used = max(nyquist * 0.8, 1e-6)
        cutfreq_clamped = True

    # ------------------------------------------------------------
    # 窗口参数（与 WebTools 一致）
    # ------------------------------------------------------------
    window = _hanning(window_size)
    window_spacing = int(np.round(window_size / 16))  # Math.round
    num_windows = (n - window_size) // window_spacing + 1

    # WebTools: Math.min(Math.ceil(0.5 / sample_time), window_size)  (PIDReview.js:1072)
    # （源码核对 2026-09-13：用 ceil 而非 floor）
    step_end = min(int(np.ceil(step_duration_s / dt)), window_size)
    time_arr = np.arange(step_end) * dt
    full_len = 2 * (real_len - 1)  # 双边谱长度

    # ------------------------------------------------------------
    # 向量化（P3-opt，已 bit-identical 核验）：原标量循环逐元素填充。
    # FFT 缩放因子（与 WebTools run_fft 一致）
    # DC/Nyquist: 1/N，其余: 2/N
    # ------------------------------------------------------------
    scale = np.full(real_len, 2.0 / window_size, dtype=np.float64)
    scale[0] = 1.0 / window_size
    scale[real_len - 1] = 1.0 / window_size

    # ------------------------------------------------------------
    # 构造 SNR 正则化（复现 WebTools redraw_step 精确流程）
    # ------------------------------------------------------------
    # bins 对应频率 (Hz)
    bins = np.fft.rfftfreq(window_size, d=dt)
    # WebTools: bins.findIndex((x) => x >= cutfreq) —— 第一个 >= cutfreq 的 bin。
    # side="left" 才等价于 >=；旧实现用 side="right"，当某个 bin 恰好等于
    # cutfreq 时会多算一个 bin（off-by-one）。
    bin_idx = int(np.searchsorted(bins, cutfreq_used, side="left"))
    len_lpf = bin_idx
    len_lpf += len_lpf - 2  # account for double sided, DC and Nyquist not copied
    len_lpf = max(len_lpf, 1)

    radius = int(np.ceil(len_lpf * 0.5))
    sigma = len_lpf / 6.0

    # 累积高斯积分（fft.js 逐元素循环 → 向量化 cumsum，P3-opt 已 bit-identical 核验）
    sn = np.ones(real_len, dtype=np.float64)
    m = min(len_lpf, real_len)
    if m > 0:
        j_arr = np.arange(m, dtype=np.float64)
        gauss = np.exp((-0.5 / sigma**2) * (j_arr - radius) ** 2)
        csum = np.cumsum(gauss)
        total = csum[-1]
        sn[:m] = csum / total if total > 0 else csum

    # 镜像拼接（精确复现 WebTools 语法）
    sn = np.concatenate([sn, sn[1 : real_len - 1][::-1]])

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
            tail_val = float(np.mean(step[-max(5, step_end // 4) :]))
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
        "cutfreq_hz": cutfreq_used,
        "method": "webtools_fft",
    }
    if cutfreq_clamped:
        info["cutfreq_clamped"] = True
        info["note"] = (
            f"SNR regularisation cutoff clamped from {cutfreq:.0f} Hz to "
            f"{cutfreq_used:.1f} Hz (0.8x Nyquist at {sample_rate:.0f} Hz sampling)"
        )

    return {
        "time": time_arr,
        "step_response": step_out,
        **info,
    }


def compute_step_response_for_axis(
    pid_data: Dict[str, np.ndarray],
    axis: str = "roll",
    imu_data: Optional[Dict[str, np.ndarray]] = None,
    prefer_imu: bool = False,
) -> Dict[str, Any]:
    """
    为指定轴计算阶跃响应（对齐 WebTools PIDReview 的数据源选择）。

    输出信号默认用 **PID 消息的 Act**（PIDR/PIDP/PIDY.Act，无则 RATE.R/P/Y）——
    与 WebTools 一致（PIDReview.js:1600/1608 构造 test set 时取的就是
    log_msg.Act / log_msg[axis_prefix]，全程未替换为 IMU 陀螺）。

    prefer_imu=True 才用 IMU.Gyr 作为输出。那条路径需要把 Tar 插值到陀螺时基
    并重新均匀重采样，会引入插值自身的动态，且陀螺是未经控制器滤波的信号 ——
    与参考实现不同源，仅作为对比手段保留。
    （v3.4.0 之前这是默认行为，并被错标为"与 WebTools 一致"。）
    """
    desired = pid_data.get("Desired", np.array([]))
    actual_rate = pid_data.get("Actual", np.array([]))
    time_rate = pid_data.get("time", np.array([]))

    use_imu = prefer_imu and imu_data is not None and len(imu_data.get("GyrX", [])) > 0
    source = "imu_gyro" if use_imu else "pid_act"

    if use_imu:
        axis_idx = {"roll": 0, "pitch": 1, "yaw": 2}.get(axis.lower(), 0)
        gyr_keys = ["GyrX", "GyrY", "GyrZ"]
        gyr_key = gyr_keys[axis_idx]

        actual_imu = imu_data[gyr_key]
        time_imu = imu_data["time"]

        if len(time_rate) > 1 and len(time_imu) > 1:
            from scipy import interpolate

            interp_desired = interpolate.interp1d(
                time_rate,
                desired,
                kind="linear",
                bounds_error=False,
                fill_value=(desired[0], desired[-1]),
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

    # 阈值：对齐 WebTools PIDReview 的 TarMax < 20 窗口筛选（deg/s）。
    # 旧实现用 3.0 增加窗口数，但会把弱激励的噪声窗口平均进来，
    # 与参考实现偏离 — 现恢复 20.0 严格对齐。
    min_amp = 20.0  # deg/s

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
        "info": {
            "output_signal": source,
            **{k: v for k, v in result.items() if k not in ("time", "step_response")},
        },
    }
