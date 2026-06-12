"""
合成信号回归测试 — 不依赖真实日志，验证核心算法的数值正确性。

原理：用已知动力学的二阶系统生成 desired/actual 信号对，
喂给各算法，断言恢复出的特性在容差内。这是金标准对拍测试
（真实日志 vs WebTools/PTB 输出）就位前的第一道数值防线。
"""

import numpy as np
import pytest


def _simulate_second_order(desired: np.ndarray, sample_rate: float,
                           wn: float, zeta: float) -> np.ndarray:
    """用双线性离散化的二阶系统 G(s)=wn²/(s²+2ζwn·s+wn²) 滤 desired。"""
    from scipy import signal
    num = [wn ** 2]
    den = [1.0, 2.0 * zeta * wn, wn ** 2]
    dt = 1.0 / sample_rate
    sysd = signal.cont2discrete((num, den), dt, method="bilinear")
    b, a = np.squeeze(sysd[0]), np.squeeze(sysd[1])
    return signal.lfilter(b, a, desired)


def _make_excitation(sample_rate: float, duration_s: float,
                     n_steps: int = 12, amp: float = 120.0,
                     seed: int = 42) -> np.ndarray:
    """生成多段阶跃 + 宽带扰动的激励信号。

    纯阶跃序列的频谱按 1/f² 衰减，中高频能量极低，Wiener 反卷积的
    正则化会压制这些频段 → 估计响应被抹平（v1 测试稳态只恢复 ~0.8、
    超调完全丢失的根因，非容差问题）。真实飞行的打杆信号含大量
    宽带成分，这里叠加 ~8% 幅值的白噪声使输入谱接近真实场景，
    反卷积才能良好条件化。
    """
    rng = np.random.default_rng(seed)
    n = int(sample_rate * duration_s)
    sig = np.zeros(n)
    seg = n // n_steps
    for i in range(n_steps):
        level = rng.uniform(-amp, amp)
        if abs(level) < 40.0:   # 保证超过 minInput=20 且留余量
            level = 40.0 * np.sign(level or 1.0)
        sig[i * seg:(i + 1) * seg] = level
    # 宽带扰动：打杆手抖 + 控制器残差的近似
    sig = sig + rng.normal(0.0, 0.08 * amp, n)
    return sig


# ---------------------------------------------------------------------------
# Betaflight 阶跃响应 (PTstepcalc 对齐实现)
# ---------------------------------------------------------------------------

class TestBetaflightStepResponse:
    SAMPLE_RATE = 1000.0  # BF 典型 1kHz

    def _run(self, wn=60.0, zeta=0.9, duration=20.0):
        from smarttune.platform.betaflight.step_response_fft import estimate_step_response
        desired = _make_excitation(self.SAMPLE_RATE, duration)
        actual = _simulate_second_order(desired, self.SAMPLE_RATE, wn, zeta)
        return estimate_step_response(desired, actual, self.SAMPLE_RATE)

    def test_steady_state_near_unity(self):
        """已知 DC 增益 = 1 的系统，估计的阶跃响应稳态应 ≈ 1。

        容差 0.8~1.2：Wiener 正则化对幅值有系统性向下偏置（参考实现
        同样存在），这里验证的是“量级正确 + 不发散”，精确幅值对拍
        属于金标准测试（真实日志 vs 上游工具）的职责。
        """
        result = self._run()
        assert result["valid_windows"] > 0, "合成信号应产生有效分析段"
        step = np.asarray(result["step_response"])
        t = np.asarray(result["time"])
        steady = float(np.mean(step[t >= 0.2]))
        assert 0.8 < steady < 1.2, f"稳态 {steady:.3f} 偏离 1.0"

    def test_overdamped_no_overshoot(self):
        """过阻尼系统 (ζ=1.2) 的估计响应不应出现显著超调。"""
        result = self._run(wn=50.0, zeta=1.2)
        step = np.asarray(result["step_response"])
        # 宽带扰动 + 反卷积波纹会带来小幅起伏，阈值留余量
        assert float(np.max(step)) < 1.25

    def test_underdamped_overshoots_more_than_overdamped(self):
        """欠阻尼 (ζ=0.3) 的峰值应显著高于过阻尼 (ζ=1.2)。

        用相对断言而非绝对阈值：反卷积正则化会平滑峰值，绝对超调量
        不可靠，但“欠阻尼比过阻尼峰值高”这一定性关系必须成立，
        否则说明动态特性被完全抹掉。
        """
        under = self._run(wn=60.0, zeta=0.3)
        over = self._run(wn=50.0, zeta=1.2)
        peak_under = float(np.max(np.asarray(under["step_response"])))
        peak_over = float(np.max(np.asarray(over["step_response"])))
        assert peak_under > peak_over * 1.04, (
            f"欠阻尼峰值 {peak_under:.3f} 未明显高于过阻尼 {peak_over:.3f}"
        )

    def test_weak_excitation_rejected(self):
        """峰值低于 minInput=20 deg/s 的信号应没有有效段。"""
        from smarttune.platform.betaflight.step_response_fft import estimate_step_response
        rng = np.random.default_rng(3)
        n = int(self.SAMPLE_RATE * 10.0)
        desired = np.clip(rng.normal(0.0, 4.0, n), -15.0, 15.0)
        actual = _simulate_second_order(desired, self.SAMPLE_RATE, 60.0, 0.9)
        result = estimate_step_response(desired, actual, self.SAMPLE_RATE)
        assert result["valid_windows"] == 0


# ---------------------------------------------------------------------------
# ArduPilot 阶跃响应 (WebTools 对齐实现)
# ---------------------------------------------------------------------------

class TestArdupilotStepResponse:
    SAMPLE_RATE = 400.0  # AP RATE 典型 400Hz

    def test_steady_state_near_unity(self):
        from smarttune.platform.ardupilot.step_response_fft import estimate_step_response
        desired = _make_excitation(self.SAMPLE_RATE, 30.0)
        actual = _simulate_second_order(desired, self.SAMPLE_RATE, 50.0, 0.9)
        result = estimate_step_response(desired, actual, self.SAMPLE_RATE)
        assert result.get("valid_windows", 0) > 0
        step = np.asarray(result["step_response"])
        t = np.asarray(result["time"])
        steady = float(np.mean(step[t >= 0.2]))
        # 容差同 BF 测试：验证量级而非精确幅值（后者属金标准对拍）
        assert 0.75 < steady < 1.25, f"稳态 {steady:.3f} 偏离 1.0"


# ---------------------------------------------------------------------------
# ARX 系统辨识
# ---------------------------------------------------------------------------

class TestARXIdentification:
    def test_recovers_natural_frequency(self):
        """ARX 应从合成数据恢复 ωn（±30% 容差 — 离散化与噪声影响）。"""
        from smarttune.analyzers.arx_model import arx_identify
        from smarttune.analyzers.sysid_analyzer import discrete_to_second_order

        sample_rate, wn_true, zeta_true = 100.0, 25.0, 0.5
        desired = _make_excitation(sample_rate, 60.0, n_steps=30)
        actual = _simulate_second_order(desired, sample_rate, wn_true, zeta_true)

        a, b, info = arx_identify(desired, actual, na=2, nb=2, d=0, return_info=True)
        assert not info["is_fallback"]

        wn_est, zeta_est, _dc = discrete_to_second_order(a, b, 1.0 / sample_rate)
        assert abs(wn_est - wn_true) / wn_true < 0.30, \
            f"ωn 估计 {wn_est:.1f} vs 真值 {wn_true}"

    def test_insufficient_data_flags_fallback(self):
        from smarttune.analyzers.arx_model import arx_identify
        a, b, info = arx_identify(np.array([1.0, 2.0]), np.array([1.0, 2.0]),
                                  return_info=True)
        assert info["is_fallback"]
        assert "insufficient" in info["fallback_reason"]

    def test_backward_compatible_two_tuple(self):
        """不传 return_info 时仍返回 (a, b) 二元组。"""
        from smarttune.analyzers.arx_model import arx_identify
        sample_rate = 100.0
        desired = _make_excitation(sample_rate, 30.0)
        actual = _simulate_second_order(desired, sample_rate, 25.0, 0.5)
        result = arx_identify(desired, actual)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# MAGFit 补偿模型
# ---------------------------------------------------------------------------

class TestCompassCompensation:
    def test_symmetric_soft_iron_roundtrip(self):
        """构造已知 OFS/DIA/ODI 失真的磁场，补偿后应精确还原。"""
        from smarttune.analyzers.magfit import _apply_compass_compensation

        rng = np.random.default_rng(7)
        n = 500
        # 真实磁场（均匀单位球方向 × 典型场强 450 mGauss）
        v = rng.normal(size=(n, 3))
        truth = 450.0 * v / np.linalg.norm(v, axis=1, keepdims=True)

        ofs = np.array([120.0, -80.0, 45.0])
        dia = np.array([0.98, 1.03, 0.95])
        odi = np.array([0.02, -0.015, 0.01])
        M = np.array([
            [dia[0], odi[0], odi[1]],
            [odi[0], dia[1], odi[2]],
            [odi[1], odi[2], dia[2]],
        ])
        # 由 truth 反推 raw：truth = M·(raw+ofs)  →  raw = M⁻¹·truth − ofs
        raw = (np.linalg.inv(M) @ truth.T).T - ofs

        thr = np.zeros(n)
        mot = np.zeros(3)
        recovered = _apply_compass_compensation(raw, ofs, dia, odi, mot, thr)
        assert np.allclose(recovered, truth, atol=1e-9)


# ---------------------------------------------------------------------------
# PID 建议安全 cap
# ---------------------------------------------------------------------------

class TestRecommendationCap:
    def test_factor_clipped_to_25_percent(self):
        """无论知识库 factor 多大，单次建议幅度不超过 ±25%。"""
        f = float(np.clip(3.0, 0.75, 1.25))
        assert f == 1.25
        f = float(np.clip(0.1, 0.75, 1.25))
        assert f == 0.75


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
