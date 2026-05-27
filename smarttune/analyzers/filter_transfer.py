"""
滤波器传递函数模块 — 对齐 WebTools FilterReview.js

实现完整的 ArduPilot 滤波器栈：
- DigitalBiquadFilter (LPF)
- NotchFilter (单陷波，含衰减 + 最小频率 + spread)
- MultiNotch (双/三陷波，中心频率 ± spread)
- HarmonicNotchFilter (多谐波 × 多陷波)
- 相位分析 (get_phase / phase_scale)
- 从日志参数自动推导滤波器配置
"""

from __future__ import annotations

from typing import Dict, Any, Tuple, List, Optional

import numpy as np


# ---------------------------------------------------------------------------
# Z 变换辅助
# ---------------------------------------------------------------------------

def exp_jw(freqs: np.ndarray, sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
    """计算 e^(jω) 的实部/虚部数组。"""
    omega = 2.0 * np.pi * freqs / sample_rate
    return np.cos(omega), np.sin(omega)


def _cinv(r: np.ndarray, i: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    d = r * r + i * i
    d = np.where(np.abs(d) < 1e-30, 1e-30, d)
    return r / d, -i / d


def _cmul(ar, ai, br, bi):
    return ar * br - ai * bi, ar * bi + ai * br


def _cdiv(nr, ni, dr, di):
    d = dr * dr + di * di
    d = np.where(np.abs(d) < 1e-30, 1e-30, d)
    return (nr * dr + ni * di) / d, (ni * dr - nr * di) / d


# ---------------------------------------------------------------------------
# DigitalBiquadFilter (LPF) — 与 WebTools 对齐
# ---------------------------------------------------------------------------

class DigitalBiquadFilter:
    """二阶 Butterworth LPF。"""

    def __init__(self, cutoff_hz: float):
        self.cutoff_hz = cutoff_hz
        self._enabled = cutoff_hz > 0

    def transfer(self, Hn_r, Hn_i, Hd_r, Hd_i, sample_freq, Z1_r, Z1_i, Z2_r, Z2_i):
        if not self._enabled:
            return
        fr = sample_freq / self.cutoff_hz
        ohm = np.tan(np.pi / fr)
        c = 1.0 + 2.0 * np.cos(np.pi / 4.0) * ohm + ohm * ohm
        b0 = ohm * ohm / c
        b1 = 2.0 * b0
        b2 = b0
        a1 = 2.0 * (ohm * ohm - 1.0) / c
        a2 = (1.0 - 2.0 * np.cos(np.pi / 4.0) * ohm + ohm * ohm) / c

        nr = b0 + b1 * Z1_r + b2 * Z2_r
        ni = b1 * Z1_i + b2 * Z2_i
        dr = 1.0 + a1 * Z1_r + a2 * Z2_r
        di = a1 * Z1_i + a2 * Z2_i

        t1r, t1i = _cmul(Hn_r, Hn_i, nr, ni)
        t2r, t2i = _cmul(Hd_r, Hd_i, dr, di)
        Hn_r[:] = t1r; Hn_i[:] = t1i
        Hd_r[:] = t2r; Hd_i[:] = t2i


# ---------------------------------------------------------------------------
# NotchFilter — 与 WebTools NotchFilter 对齐
# ---------------------------------------------------------------------------

class NotchFilter:
    """单陷波，含衰减缩放(A)、最小频率、spread 偏移。"""

    def __init__(self, attenuation_dB, bandwidth_hz, harmonic_mul, min_freq_hz=0.0, spread_mul=1.0):
        self.A = 10.0 ** (-attenuation_dB / 40.0)
        self.bandwidth_hz = bandwidth_hz
        self.harmonic_mul = harmonic_mul
        self.min_freq_hz = min_freq_hz
        self.spread_mul = spread_mul

    def transfer(self, Hn_r, Hn_i, Hd_r, Hd_i, center, sample_freq, Z1_r, Z1_i, Z2_r, Z2_i):
        cf = center * self.harmonic_mul
        if cf <= 0.5 * self.bandwidth_hz or cf >= 0.5 * sample_freq:
            return

        A = self.A
        if self.min_freq_hz > 0 and cf < self.min_freq_hz:
            disable_freq = self.min_freq_hz * 0.25
            if cf < disable_freq:
                return
            ratio = (cf - disable_freq) / (self.min_freq_hz - disable_freq)
            A = 1.0 + (A - 1.0) * ratio

        cf = max(cf, self.min_freq_hz) * self.spread_mul
        bw = self.bandwidth_hz
        octaves = np.log2(cf / (cf - bw / 2.0)) * 2.0
        Q = ((2.0 ** octaves) ** 0.5) / ((2.0 ** octaves) - 1.0) if octaves > 0 else 10.0
        Asq = A ** 2

        omega = 2.0 * np.pi * cf / sample_freq
        alpha = np.sin(omega) / (2.0 * Q)
        b0 = 1.0 + alpha * Asq
        b1 = -2.0 * np.cos(omega)
        b2 = 1.0 - alpha * Asq
        a0 = 1.0 + alpha
        a1 = b1
        a2 = 1.0 - alpha

        nr = b0 + b1 * Z1_r + b2 * Z2_r
        ni = b1 * Z1_i + b2 * Z2_i
        dr = a0 + a1 * Z1_r + a2 * Z2_r
        di = a1 * Z1_i + a2 * Z2_i

        t1r, t1i = _cmul(Hn_r, Hn_i, nr, ni)
        t2r, t2i = _cmul(Hd_r, Hd_i, dr, di)
        Hn_r[:] = t1r; Hn_i[:] = t1i
        Hd_r[:] = t2r; Hd_i[:] = t2i


# ---------------------------------------------------------------------------
# MultiNotch (双/三陷波) — 与 WebTools MultiNotch 对齐
# ---------------------------------------------------------------------------

class MultiNotch:
    """2 或 3 个 NotchFilter 以不同 spread 覆盖更宽带宽。"""

    def __init__(self, attenuation_dB, bandwidth_hz, harmonic, min_freq_hz, num, center_freq):
        notch_spread = bandwidth_hz / (32.0 * center_freq) if center_freq > 0 else 0
        bw_scaled = (bandwidth_hz * harmonic) / num

        self.notches = [
            NotchFilter(attenuation_dB, bw_scaled, harmonic, min_freq_hz, 1.0 - notch_spread),
            NotchFilter(attenuation_dB, bw_scaled, harmonic, min_freq_hz, 1.0 + notch_spread),
        ]
        if num == 3:
            self.notches.append(NotchFilter(attenuation_dB, bw_scaled, harmonic, min_freq_hz, 1.0))

    def transfer(self, Hn_r, Hn_i, Hd_r, Hd_i, center, sample_freq, Z1_r, Z1_i, Z2_r, Z2_i):
        for n in self.notches:
            n.transfer(Hn_r, Hn_i, Hd_r, Hd_i, center, sample_freq, Z1_r, Z1_i, Z2_r, Z2_i)


# ---------------------------------------------------------------------------
# HarmonicNotchFilter — 与 WebTools 对齐
# ---------------------------------------------------------------------------

class HarmonicNotchFilter:
    """多谐波 × 单/双/三 陷波。"""

    def __init__(self, enable=0, freq=80.0, bandwidth=40.0, attenuation=40.0,
                 harmonics=3, options=0, min_ratio=1.0, mode=1, ref=0.0):
        self.enable = enable
        self.freq = freq
        self.bandwidth = bandwidth
        self.attenuation = attenuation
        self.harmonics_mask = harmonics
        self.options = options
        self.min_ratio = min_ratio
        self.mode = mode
        self.ref = ref
        self._enabled = enable > 0 and freq > 0

        if not self._enabled:
            self.filters: List = []
            return

        triple = (options & 16) != 0
        double = (options & 1) != 0
        single = not double and not triple
        treat_low_freq_as_min = (options & 32) != 0

        self.filters = []
        for n in range(16):
            if not (harmonics & (1 << n)):
                continue
            h = n + 1
            mf = freq * min_ratio
            if treat_low_freq_as_min:
                mf *= h
            if single:
                self.filters.append(NotchFilter(attenuation, bandwidth * h, h, mf, 1.0))
            else:
                self.filters.append(MultiNotch(attenuation, bandwidth, h, mf, 3 if triple else 2, freq))

    @property
    def enabled(self):
        return self._enabled

    def transfer_static(self, Hn_r, Hn_i, Hd_r, Hd_i, sample_freq, Z1_r, Z1_i, Z2_r, Z2_i):
        if not self._enabled:
            return
        for f in self.filters:
            f.transfer(Hn_r, Hn_i, Hd_r, Hd_i, self.freq, sample_freq, Z1_r, Z1_i, Z2_r, Z2_i)


# ---------------------------------------------------------------------------
# 相位分析 — 与 WebTools get_phase / phase_scale 对齐
# ---------------------------------------------------------------------------

def get_phase(h_r: np.ndarray, h_i: np.ndarray) -> np.ndarray:
    """偏置 unwrap 相位（度），陷波器友好。"""
    phase_raw = np.arctan2(h_i, h_r) * (180.0 / np.pi)
    n = len(phase_raw)
    if n == 0:
        return np.array([], dtype=np.float64)
    unwrapped = np.empty(n)
    unwrapped[0] = phase_raw[0]
    for i in range(1, n):
        diff = phase_raw[i] - phase_raw[i - 1]
        if diff >= 315.0:
            diff -= 360.0
        elif diff <= -45.0:
            diff += 360.0
        unwrapped[i] = unwrapped[i - 1] + diff
    return unwrapped


def phase_scale(phases: List[np.ndarray], wrap: bool = True) -> List[np.ndarray]:
    """同步 ±180° wrap。"""
    if not wrap or not phases:
        return phases
    result = [p.copy() for p in phases]
    n = len(result[0])
    for i in range(1, n):
        if result[0][i] > 180.0:
            for arr in result:
                arr[i] -= 360.0
        elif result[0][i] < -180.0:
            for arr in result:
                arr[i] += 360.0
    return result


# ---------------------------------------------------------------------------
# 计算完整传递函数
# ---------------------------------------------------------------------------

def _apply_filter_chain(
    freqs: np.ndarray,
    sample_rate: float,
    gyro_lpf: Optional[DigitalBiquadFilter] = None,
    notch_filters: Optional[List] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    应用滤波器对象链，返回 (mag_db, phase_deg)。

    平台无关的低级函数。各平台的 compute_filter_response 在解析完
    固件参数后调用此函数执行实际数学计算。
    """
    Z_r, Z_i = exp_jw(freqs, sample_rate)
    Z1_r, Z1_i = _cinv(Z_r, Z_i)
    Z2_r, Z2_i = _cinv(*_cmul(Z_r, Z_i, Z_r, Z_i))

    n = len(freqs)
    Hn_r, Hn_i = np.ones(n), np.zeros(n)
    Hd_r, Hd_i = np.ones(n), np.zeros(n)

    if gyro_lpf is not None:
        gyro_lpf.transfer(Hn_r, Hn_i, Hd_r, Hd_i, sample_rate, Z1_r, Z1_i, Z2_r, Z2_i)
    if notch_filters:
        for nf in notch_filters:
            if getattr(nf, "enabled", True):
                nf.transfer_static(Hn_r, Hn_i, Hd_r, Hd_i, sample_rate, Z1_r, Z1_i, Z2_r, Z2_i)

    H_r, H_i = _cdiv(Hn_r, Hn_i, Hd_r, Hd_i)
    mag = np.sqrt(H_r ** 2 + H_i ** 2)
    mag_db = 20.0 * np.log10(np.maximum(mag, 1e-12))
    phase_deg = get_phase(H_r, H_i)

    return mag_db, phase_deg


def compute_filter_response(
    freqs: np.ndarray,
    sample_rate: float,
    gyro_filter_hz: float = 0.0,
    notch_params: Optional[Dict[str, Any]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    计算滤波器幅度 (dB) + 相位 (度)。

    平台无关的纯数学函数。参数解析（含固件参数名映射）由各平台模块负责。

    Parameters
    ----------
    freqs : np.ndarray
        频率数组 (Hz)
    sample_rate : float
        采样率 (Hz)
    gyro_filter_hz : float
        LPF 截止频率 (Hz)，0 = 禁用
    notch_params : Dict | None
        {center_hz, bandwidth_hz, attenuation_db, harmonics} 或 None
    """
    lpf = DigitalBiquadFilter(gyro_filter_hz) if gyro_filter_hz > 0 else None
    notches = None
    if notch_params:
        notches = [HarmonicNotchFilter(
            enable=1, freq=notch_params.get("center_hz", 80.0),
            bandwidth=notch_params.get("bandwidth_hz", 40.0),
            attenuation=notch_params.get("attenuation_db", 40.0),
            harmonics=notch_params.get("harmonics", 3),
        )]
    return _apply_filter_chain(freqs, sample_rate, lpf, notches)


def simulate_filtered_spectrum(
    freqs: np.ndarray, magnitudes: np.ndarray, sample_rate: float,
    gyro_filter_hz: float = 0.0, notch_params: Optional[Dict[str, Any]] = None,
) -> np.ndarray:
    """模拟滤波后的频谱（dB 域相加）。平台无关。"""
    mag_db, _ = compute_filter_response(freqs, sample_rate, gyro_filter_hz, notch_params)
    return magnitudes + mag_db
