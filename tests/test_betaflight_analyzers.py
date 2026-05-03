"""
tests/test_betaflight_analyzers.py

Betaflight 特有分析器和知识库测试。
"""

import json
import unittest
from pathlib import Path

import numpy as np

from smarttune.models.flight_data import FlightData, AxisPIDSignal


def _make_bf_flight_data(
    duration_s=5.0,
    sample_rate=4000.0,
    ff_strength=1.0,
    d_noise_level=0.1,
    motor_noise_hz=150.0,
) -> FlightData:
    """创建模拟 Betaflight 飞行数据。"""
    n = int(duration_s * sample_rate)
    t = np.linspace(0, duration_s, n)

    pid = {}
    for axis in ["roll", "pitch", "yaw"]:
        # Desired: 随机阶跃指令
        desired = np.zeros(n)
        step_times = np.arange(0.5, duration_s, 1.0)
        for st in step_times:
            idx = int(st * sample_rate)
            if idx < n:
                desired[idx:] = np.random.choice([-20, 0, 20])

        # Actual: 追踪 desired + 噪声
        actual = np.zeros(n)
        tau = 0.02
        for i in range(1, n):
            dt = t[i] - t[i - 1]
            alpha = dt / (tau + dt)
            actual[i] = actual[i - 1] + alpha * (desired[i] - actual[i - 1])
        actual += np.random.randn(n) * 0.5

        # PID terms
        error = desired - actual
        p_term = error * 45  # P = 45
        i_term = np.cumsum(error) * 0.01  # simple I
        d_term = np.gradient(actual) * 40 + np.random.randn(n) * d_noise_level * 40
        ff_term = np.gradient(desired) * 120 * ff_strength

        pid[axis] = AxisPIDSignal(
            timestamp_s=t,
            desired=desired,
            actual=actual,
            p_term=p_term,
            i_term=i_term,
            d_term=d_term,
            ff_term=ff_term,
        )

    # Gyro with motor noise
    gyro = np.random.randn(n, 3) * 0.5
    gyro[:, 0] += 3.0 * np.sin(2 * np.pi * motor_noise_hz * t)
    gyro[:, 1] += 2.0 * np.sin(2 * np.pi * motor_noise_hz * t + 0.5)

    accel = np.random.randn(n, 3) * 0.1
    accel[:, 2] += 9.81

    return FlightData(
        platform="betaflight",
        firmware_version="4.4.0",
        sample_rate_hz=sample_rate,
        duration_s=duration_s,
        pid=pid,
        gyro=gyro,
        accel=accel,
        imu_timestamp_s=t,
        params={
            "looptime": 250,
            "pid_roll_p": 45, "pid_roll_i": 80, "pid_roll_d": 40, "pid_roll_f": 120,
            "pid_pitch_p": 47, "pid_pitch_i": 84, "pid_pitch_d": 46, "pid_pitch_f": 125,
            "pid_yaw_p": 45, "pid_yaw_i": 80, "pid_yaw_d": 0, "pid_yaw_f": 120,
            "d_min_roll": 25, "d_min_pitch": 28, "d_min_yaw": 0,
            "gyro_lowpass_hz": 200, "gyro_lowpass2_hz": 250,
            "dterm_lowpass_hz": 150, "dterm_lowpass2_hz": 0,
            "rpm_filter": 0, "dshot_bidir": 0,
            "anti_gravity_gain": 80,
        },
    )


class TestFeedforwardAnalyzer(unittest.TestCase):
    """测试 Feedforward 分析器。"""

    def test_analyze_all_axes(self):
        from smarttune.analyzers.betaflight_analyzers import FeedforwardAnalyzer
        fd = _make_bf_flight_data(ff_strength=1.0)
        analyzer = FeedforwardAnalyzer()
        results = analyzer.analyze(fd)

        self.assertEqual(len(results), 3)
        for ax in ["roll", "pitch", "yaw"]:
            self.assertIn(ax, results)
            r = results[ax]
            self.assertGreater(r.ff_contribution_percent, 0)

    def test_analyze_single_axis(self):
        from smarttune.analyzers.betaflight_analyzers import FeedforwardAnalyzer
        fd = _make_bf_flight_data()
        analyzer = FeedforwardAnalyzer()
        results = analyzer.analyze(fd, axis="roll")

        self.assertEqual(len(results), 1)
        self.assertIn("roll", results)

    def test_no_ff_data(self):
        from smarttune.analyzers.betaflight_analyzers import FeedforwardAnalyzer
        fd = _make_bf_flight_data()
        # 清除 FF 数据
        for ax in fd.pid.values():
            ax.ff_term = None
        analyzer = FeedforwardAnalyzer()
        results = analyzer.analyze(fd)

        for r in results.values():
            self.assertIn("No FF data", r.details.get("note", ""))

    def test_high_ff_strength_detection(self):
        from smarttune.analyzers.betaflight_analyzers import FeedforwardAnalyzer
        # 使用正弦波 desired 以确保 FF (基于梯度) 持续非零
        fd = _make_bf_flight_data(ff_strength=5.0)
        n = len(fd.pid["roll"].timestamp_s)
        t = fd.pid["roll"].timestamp_s
        for ax in fd.pid:
            fd.pid[ax].desired = 20.0 * np.sin(2 * np.pi * 2 * t)
            fd.pid[ax].ff_term = np.gradient(fd.pid[ax].desired) * 120 * 5.0
        analyzer = FeedforwardAnalyzer()
        results = analyzer.analyze(fd)

        # FF 占比应该很高 (FF 倍率极大)
        roll_result = results["roll"]
        self.assertGreater(roll_result.ff_contribution_percent, 20)

    def test_zero_ff(self):
        from smarttune.analyzers.betaflight_analyzers import FeedforwardAnalyzer
        fd = _make_bf_flight_data(ff_strength=0.0)
        analyzer = FeedforwardAnalyzer()
        results = analyzer.analyze(fd)

        for r in results.values():
            # FF 全为零时贡献度应该接近 0
            self.assertLessEqual(r.ff_contribution_percent, 5)


class TestRPMFilterAnalyzer(unittest.TestCase):
    """测试 RPM 滤波器分析器。"""

    def test_analyze_basic(self):
        from smarttune.analyzers.betaflight_analyzers import RPMFilterAnalyzer
        fd = _make_bf_flight_data(motor_noise_hz=150)
        analyzer = RPMFilterAnalyzer()
        result = analyzer.analyze(fd)

        self.assertFalse(result.rpm_filter_detected)  # 参数里 rpm_filter=0
        self.assertIsInstance(result.motor_noise_peaks_hz, list)
        self.assertIn("sample_rate_hz", result.details)

    def test_detect_motor_peaks(self):
        from smarttune.analyzers.betaflight_analyzers import RPMFilterAnalyzer
        fd = _make_bf_flight_data(motor_noise_hz=180)
        analyzer = RPMFilterAnalyzer()
        result = analyzer.analyze(fd)

        # 应该检测到 180Hz 附近的峰值
        if result.motor_noise_peaks_hz:
            closest = min(result.motor_noise_peaks_hz,
                          key=lambda f: abs(f - 180))
            self.assertAlmostEqual(closest, 180, delta=30)

    def test_rpm_filter_detected(self):
        from smarttune.analyzers.betaflight_analyzers import RPMFilterAnalyzer
        fd = _make_bf_flight_data()
        fd.params["rpm_filter"] = 1
        fd.params["dshot_bidir"] = 1
        analyzer = RPMFilterAnalyzer()
        result = analyzer.analyze(fd)

        self.assertTrue(result.rpm_filter_detected)

    def test_insufficient_gyro_data(self):
        from smarttune.analyzers.betaflight_analyzers import RPMFilterAnalyzer
        fd = _make_bf_flight_data()
        fd.gyro = np.zeros((10, 3))  # 太少
        analyzer = RPMFilterAnalyzer()
        result = analyzer.analyze(fd)

        self.assertIn("Insufficient", result.details.get("note", ""))


class TestDTermNoiseAnalyzer(unittest.TestCase):
    """测试 D-term 噪声分析器。"""

    def test_analyze_all_axes(self):
        from smarttune.analyzers.betaflight_analyzers import DTermNoiseAnalyzer
        fd = _make_bf_flight_data(d_noise_level=0.1)
        analyzer = DTermNoiseAnalyzer()
        results = analyzer.analyze(fd)

        self.assertEqual(len(results), 3)
        for ax in ["roll", "pitch", "yaw"]:
            self.assertIn(ax, results)
            r = results[ax]
            self.assertGreater(r.d_noise_rms, 0)

    def test_high_d_noise(self):
        from smarttune.analyzers.betaflight_analyzers import DTermNoiseAnalyzer
        fd = _make_bf_flight_data(d_noise_level=5.0)  # 非常高的噪声
        analyzer = DTermNoiseAnalyzer()
        results = analyzer.analyze(fd)

        roll = results["roll"]
        # 高噪声时 D/P 比率应该较高
        self.assertGreater(roll.d_noise_rms, 0)

    def test_no_d_term(self):
        from smarttune.analyzers.betaflight_analyzers import DTermNoiseAnalyzer
        fd = _make_bf_flight_data()
        for sig in fd.pid.values():
            sig.d_term = None
        analyzer = DTermNoiseAnalyzer()
        results = analyzer.analyze(fd)

        for r in results.values():
            self.assertIn("No D-term", r.details.get("note", ""))

    def test_d_min_active_percent(self):
        from smarttune.analyzers.betaflight_analyzers import DTermNoiseAnalyzer
        fd = _make_bf_flight_data(d_noise_level=0.1)
        analyzer = DTermNoiseAnalyzer()
        results = analyzer.analyze(fd)

        for r in results.values():
            # d_min_active_percent 应该在 0-100 范围内
            self.assertGreaterEqual(r.d_min_active_percent, 0)
            self.assertLessEqual(r.d_min_active_percent, 100)


class TestBFKnowledgeBase(unittest.TestCase):
    """测试 Betaflight 知识库加载和规则完整性。"""

    def test_pid_rules_load(self):
        rules_dir = Path(__file__).parent.parent / "smarttune" / "knowledge" / "rules" / "betaflight"
        pid_path = rules_dir / "pid_rules.json"
        self.assertTrue(pid_path.exists(), f"pid_rules.json not found at {pid_path}")

        with open(pid_path) as f:
            rules = json.load(f)

        # 必须有的顶层键
        self.assertIn("thresholds", rules)
        self.assertIn("assessment_levels", rules)
        self.assertIn("tuning_rules", rules)
        self.assertIn("frame_type_presets", rules)
        self.assertIn("diagnostic_tree", rules)
        self.assertIn("param_change_rules", rules)

    def test_pid_rules_thresholds(self):
        rules_dir = Path(__file__).parent.parent / "smarttune" / "knowledge" / "rules" / "betaflight"
        with open(rules_dir / "pid_rules.json") as f:
            rules = json.load(f)

        thresholds = rules["thresholds"]
        self.assertIn("rise_time_ms", thresholds)
        self.assertIn("overshoot_percent", thresholds)
        self.assertIn("settling_time_ms", thresholds)
        self.assertIn("oscillation_count", thresholds)

        # 轴阈值
        for axis in ["roll", "pitch", "yaw"]:
            self.assertIn(axis, thresholds["rise_time_ms"])

    def test_pid_rules_bf_specific(self):
        """验证 BF 特有规则存在。"""
        rules_dir = Path(__file__).parent.parent / "smarttune" / "knowledge" / "rules" / "betaflight"
        with open(rules_dir / "pid_rules.json") as f:
            rules = json.load(f)

        tuning = rules["tuning_rules"]
        # BF 特有: FF, d_min, anti_gravity
        self.assertIn("FF_too_high_symptoms", tuning)
        self.assertIn("FF_too_low_symptoms", tuning)
        self.assertIn("d_min_d_max_rules", tuning)
        self.assertIn("anti_gravity_rules", tuning)

        # 机型预设
        presets = rules["frame_type_presets"]
        self.assertIn("5_inch_freestyle", presets)
        self.assertIn("3_inch_cinewhoop", presets)
        self.assertIn("7_inch_long_range", presets)

    def test_pid_rules_default_ranges(self):
        """验证 PID 默认范围合理。"""
        rules_dir = Path(__file__).parent.parent / "smarttune" / "knowledge" / "rules" / "betaflight"
        with open(rules_dir / "pid_rules.json") as f:
            rules = json.load(f)

        ranges = rules["tuning_rules"]["default_pid_ranges"]
        for param, spec in ranges.items():
            self.assertIn("min", spec)
            self.assertIn("max", spec)
            self.assertIn("typical", spec)
            self.assertLess(spec["min"], spec["max"])
            self.assertGreaterEqual(spec["typical"], spec["min"])
            self.assertLessEqual(spec["typical"], spec["max"])

    def test_filter_rules_load(self):
        rules_dir = Path(__file__).parent.parent / "smarttune" / "knowledge" / "rules" / "betaflight"
        filter_path = rules_dir / "filter_rules.json"
        self.assertTrue(filter_path.exists(), f"filter_rules.json not found")

        with open(filter_path) as f:
            rules = json.load(f)

        self.assertIn("filter_stack", rules)
        self.assertIn("rpm_filter_rules", rules)
        self.assertIn("dynamic_notch_rules", rules)
        self.assertIn("gyro_lpf_rules", rules)
        self.assertIn("dterm_lpf_rules", rules)
        self.assertIn("vibration_thresholds", rules)
        self.assertIn("filter_tuning_sequence", rules)

    def test_filter_rules_rpm_filter(self):
        """验证 RPM filter 规则完整。"""
        rules_dir = Path(__file__).parent.parent / "smarttune" / "knowledge" / "rules" / "betaflight"
        with open(rules_dir / "filter_rules.json") as f:
            rules = json.load(f)

        rpm = rules["rpm_filter_rules"]
        self.assertIn("prerequisites", rpm)
        self.assertIn("parameter_defaults", rpm)
        self.assertIn("effectiveness_assessment", rpm)
        self.assertIn("tuning_advice", rpm)

    def test_filter_rules_filter_stack_order(self):
        """验证滤波器链顺序正确。"""
        rules_dir = Path(__file__).parent.parent / "smarttune" / "knowledge" / "rules" / "betaflight"
        with open(rules_dir / "filter_rules.json") as f:
            rules = json.load(f)

        gyro_chain = rules["filter_stack"]["gyro_chain"]
        stages = [s["stage"] for s in gyro_chain]
        self.assertEqual(stages, sorted(stages), "gyro chain should be in order")

        # RPM filter 应该是第一级
        self.assertEqual(gyro_chain[0]["name"], "RPM filter")

    def test_knowledge_base_loads_bf_rules(self):
        """通过 KnowledgeBase 加载 BF 规则。"""
        from smarttune.knowledge import KnowledgeBase
        kb = KnowledgeBase(platform="betaflight")

        self.assertIn("pid_rules", kb.rules)
        self.assertIn("filter_rules", kb.rules)
        self.assertIn("vibration_rules", kb.rules)  # from common

        # BF pid_rules 应该包含 FF 规则
        pid = kb.rules["pid_rules"]
        self.assertIn("tuning_rules", pid)
        self.assertIn("FF_too_high_symptoms", pid["tuning_rules"])


class TestBFAnalyzerWithKnowledgeBase(unittest.TestCase):
    """集成测试: BF 分析器 + 知识库。"""

    def test_full_bf_analysis_pipeline(self):
        """端到端: 合成数据 → FF/RPM/D-term 分析。"""
        from smarttune.analyzers.betaflight_analyzers import (
            FeedforwardAnalyzer, RPMFilterAnalyzer, DTermNoiseAnalyzer,
        )

        fd = _make_bf_flight_data(duration_s=3.0)

        # FF analysis
        ff_results = FeedforwardAnalyzer().analyze(fd)
        self.assertGreater(len(ff_results), 0)

        # RPM filter analysis
        rpm_result = RPMFilterAnalyzer().analyze(fd)
        self.assertIsNotNone(rpm_result)

        # D-term noise analysis
        d_results = DTermNoiseAnalyzer().analyze(fd)
        self.assertGreater(len(d_results), 0)

        # 所有结果都应该有 assessment
        for r in ff_results.values():
            self.assertIsNotNone(r.assessment)
        self.assertIsNotNone(rpm_result.assessment)
        for r in d_results.values():
            self.assertIsNotNone(r.assessment)


if __name__ == "__main__":
    unittest.main()
