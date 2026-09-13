"""
阶跃响应估计（Betaflight）— 对齐 Plasmatree PID-Analyzer。

本模块在 pid_reviewer.py 中按 platform 动态分派：
  smarttune.platform.betaflight.step_response_fft

参考实现的选择
--------------
BF 社区有两套同源实现：

* **Plasmatree/PID-Analyzer**（Python，开源可核）—— 这一族算法的源头；
  ArduPilot WebTools 的 PIDReview step response 就是从它来的。
* **bw1129/PIDtoolbox**（MATLAB，`PTstepcalc.m`）—— 社区最常用的 GUI，
  但仓库已下架，无法逐行核对。

v3.5.0 之前本模块按 PTstepcalc.m 的**公开描述**重写过一版（2 秒段、
不加窗、常数 λ 正则化、段步长 segment/4）。逐行核对 PID-Analyzer 源码后
发现那版有一个实质缺陷：**完全不加窗**。PID-Analyzer.py:64 明确用
``np.hanning(self.flen)``，WebTools 同样用 Hanning —— 不加窗的 2 秒段
在反卷积前会带入频谱泄漏，直接污染传递函数估计。

因此现在统一走已核验的共享内核（``platform/ardupilot/step_response_fft``
的 ``estimate_step_response``，对齐 WebTools PIDReview.js 逐行），只保留
BF 侧特有的输入门控。

对齐结果（PID-Analyzer.py 行号）
--------------------------------
  framelen = 1.0 s                    :32   ✅ 共享内核默认 1 秒窗
  resplen  = 0.5 s                    :33   ✅
  cutfreq  = 25 Hz                    :34   ✅
  superpos = 16 → shift = flen/16     :36,201 ✅ 共享内核 spacing = N/16
  np.hanning(flen)                    :64   ✅（旧实现缺失，已修）
  Wiener 反卷积 + cumsum              :212-232 ✅
  threshold = 500 deg/s               :37   ✅ 见下
  跨窗平均                             :227+ ✅

输入门控（与 PID-Analyzer 的 low/high 拆分等价）
------------------------------------------------
PID-Analyzer 用 ``low_high_mask(max_in, 500)`` 把响应拆成
"≤500 deg/s" 和 ">500 deg/s" 两条曲线分别平均，而**不是**丢弃。
SmartTune 只产出一条曲线，取的是调参实际看的那条 —— low-input
（峰值 ≤ 500 deg/s），实现方式是给共享内核传 ``max_target_amplitude=500``。
下限 20 deg/s 与 WebTools 的 ``TarMax < 20`` 门控一致。

有意偏离
--------
* **窗口长度取 2 的幂**（共享内核沿用 WebTools 的 fft.js 约束），
  PID-Analyzer 直接用 1 秒对应的样本数。频率分辨率略有差异。
* **正则化形式**：PID-Analyzer 用 cutfreq 处的硬掩码
  （``to_mask(clip(|freq|, cutfreq-1e-9, cutfreq))``，:219），
  共享内核用 WebTools 的高斯 CDF 平滑掩码。两者都是 25 Hz 处的
  SNR 正则化，后者过渡更平缓。
* 额外的窗口数据质量预检（NaN / >1500 deg/s / 静止段 / 超调 >300%），
  上游无此步，计入 ``info.skipped_quality``。

数据源
------
- input  = setpoint[axis]（BF Blackbox 的 SP）
- output = gyroADC[axis]（滤波后陀螺，即控制环的反馈信号）
  PID-Analyzer 同样用 gyro 作为 output（:63 stacks 的 'gyro'）。
  注意：``gyroUnfilt``（未滤波陀螺）适合做陷波定位，不适合做闭环阶跃响应。

References
----------
- https://github.com/Plasmatree/PID-Analyzer  (PID-Analyzer.py)
- https://github.com/bw1129/PIDtoolbox  (PTstepcalc.m，仓库已下架)
"""

from typing import Any, Dict, Optional

import numpy as np

from smarttune.platform.ardupilot.step_response_fft import (
    estimate_step_response as _shared_estimate,
)

# PID-Analyzer 常数（PID-Analyzer.py:32-37）
_FRAME_DURATION_S = 1.0  # framelen
_RESPONSE_DURATION_S = 0.5  # resplen
_CUTFREQ_HZ = 25.0  # cutfreq
_HIGH_INPUT_THRESHOLD = 500.0  # threshold — low/high input 分界
_MIN_INPUT_DEG_S = 20.0  # 下限门控（与 WebTools TarMax 一致）

# 稳态合理性检查（SmartTune 附加；SP 与 GY 同单位，理想稳态 = 1.0）
_QC_STEADY_LO = 0.5
_QC_STEADY_HI = 3.0


def estimate_step_response(
    target: np.ndarray,
    actual: np.ndarray,
    sample_rate: float,
    window_size: Optional[int] = None,
    step_duration_s: float = _RESPONSE_DURATION_S,
    min_target_amplitude: float = _MIN_INPUT_DEG_S,
    max_target_amplitude: float = _HIGH_INPUT_THRESHOLD,
) -> Dict[str, Any]:
    """
    估计阶跃响应（PID-Analyzer 口径，走共享 Hanning/Wiener 内核）。

    Parameters
    ----------
    target : np.ndarray
        Setpoint（SP）序列，deg/s。
    actual : np.ndarray
        滤波后陀螺仪（gyroADC）序列，deg/s。
    sample_rate : float
        采样率（Hz）。
    window_size : int, optional
        覆盖默认 1 秒窗（点数）。仅用于测试。
    step_duration_s : float
        阶跃响应窗时长（秒，默认 0.5 = PID-Analyzer resplen）。
    min_target_amplitude : float
        窗内 SP 峰值下限（默认 20 deg/s）。
    max_target_amplitude : float
        窗内 SP 峰值上限（默认 500 deg/s = PID-Analyzer threshold，
        即只取 low-input 响应）。

    Returns
    -------
    Dict with keys: time, step_response, valid_windows, total_windows,
    skipped_quality, window_size, sample_rate, method
    """
    result = _shared_estimate(
        target=target,
        actual=actual,
        sample_rate=sample_rate,
        window_size=window_size,
        step_duration_s=step_duration_s,
        min_target_amplitude=min_target_amplitude,
        cutfreq=_CUTFREQ_HZ,
        max_target_amplitude=max_target_amplitude,
    )
    result["method"] = "pid_analyzer_wiener"
    result["input_window_deg_s"] = [min_target_amplitude, max_target_amplitude]

    # 稳态合理性：SP 与 GY 同单位，收敛值应接近 1.0。偏离过大说明反卷积
    # 没收敛（数据太脏或激励不足），标注而不是静默给出一条错曲线。
    step = result.get("step_response")
    time_arr = result.get("time")
    if isinstance(step, np.ndarray) and isinstance(time_arr, np.ndarray) and step.size:
        qc_mask = (time_arr >= 0.2) & (time_arr <= step_duration_s)
        if bool(np.any(qc_mask)):
            steady = float(np.mean(step[qc_mask]))
            result["steady_state"] = round(steady, 3)
            result["steady_state_ok"] = bool(_QC_STEADY_LO < steady < _QC_STEADY_HI)
    return result


def compute_step_response_for_axis(
    pid_data: Dict[str, np.ndarray],
    axis: str = "roll",
    imu_data: Optional[Dict[str, np.ndarray]] = None,
    prefer_imu: bool = False,
) -> Dict[str, Any]:
    """
    为指定轴计算阶跃响应。

    Betaflight Blackbox 的 ``Actual`` **就是** gyroADC（滤波后陀螺），与
    PID-Analyzer 的 output 同源，所以默认不走 imu_data 路径 —— 那条路径
    会把同一份数据插值重采样一遍，白白引入插值动态。
    ``prefer_imu=True`` 仅在 ``Actual`` 来自其他低采样率源时有意义。
    （v3.5.0 前默认走 IMU 路径，对 BF 是纯粹的冗余重采样。）
    """
    desired = pid_data.get("Desired", np.array([]))
    actual_rate = pid_data.get("Actual", np.array([]))
    time_rate = pid_data.get("time", np.array([]))

    use_imu = prefer_imu and imu_data is not None and len(imu_data.get("GyrX", [])) > 0
    source = "imu_gyro" if use_imu else "gyro_adc"

    if use_imu:
        axis_idx = {"roll": 0, "pitch": 1, "yaw": 2}.get(axis.lower(), 0)
        gyr_key = ["GyrX", "GyrY", "GyrZ"][axis_idx]

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
        min_target_amplitude=_MIN_INPUT_DEG_S,
        max_target_amplitude=_HIGH_INPUT_THRESHOLD,
        step_duration_s=_RESPONSE_DURATION_S,
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
