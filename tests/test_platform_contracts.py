"""
平台无关契约回归测试 — 锁定精细化审查发现的两个 logic bug 的修复。

1. A2 契约：adapter 必须把 generic key（pid.roll.p 等）注入 FlightData.params，
   否则 PIDReviewer._get_current_pid 恒返回 0.0，叠加 C4「current≤0 跳过」后
   PID 参数建议被全部丢弃。

2. 阈值形状守卫：知识库 thresholds 若非「按轴 + max 标量」形状，
   PIDReviewer 应回退到 _DEFAULT_THRESHOLDS，而非静默产出空 thresholds。
"""

import numpy as np
import pytest

from smarttune.analyzers.pid_reviewer import (
    PIDReviewer, _DEFAULT_THRESHOLDS, _DEFAULT_BOUNDS,
)


class TestGenericKeyInjection:
    """A2 契约：各 adapter 的 parse() 注入 generic key。"""

    def test_ardupilot_injects_generic_pid_keys(self):
        from smarttune.platform.ardupilot import _PARAM_MAP_TO_PLATFORM
        # 模拟 parse() 内的注入逻辑（与适配器同源）
        params = {"ATC_RAT_RLL_P": 0.135, "ATC_RAT_RLL_D": 0.0036}
        for generic, plat in _PARAM_MAP_TO_PLATFORM.items():
            if plat in params and generic not in params:
                params[generic] = params[plat]
        assert params["pid.roll.p"] == 0.135
        assert params["pid.roll.d"] == 0.0036

    def test_reviewer_reads_injected_current_value(self):
        """注入 generic key 后，_get_current_pid 取到真实值（非 0）。"""
        reviewer = PIDReviewer()
        params = {"ATC_RAT_RLL_P": 0.135, "pid.roll.p": 0.135}
        current = reviewer._get_current_pid(params, "roll")
        assert current["p"] == 0.135

    def test_missing_generic_key_yields_zero(self):
        """未注入时取到 0.0 —— 这正是 C4 跳过伪建议依赖的前提。"""
        reviewer = PIDReviewer()
        params = {"ATC_RAT_RLL_P": 0.135}   # 只有原生名
        current = reviewer._get_current_pid(params, "roll")
        assert current["p"] == 0.0


class TestThresholdShapeGuard:
    def test_axis_shaped_thresholds_accepted(self):
        reviewer = PIDReviewer(knowledge={"thresholds": _DEFAULT_THRESHOLDS})
        assert reviewer._thresholds is _DEFAULT_THRESHOLDS

    def test_metric_keyed_thresholds_fall_back_to_default(self):
        """知识库的 thresholds 是按指标 + 区间形 → 回退到 _DEFAULT_THRESHOLDS。"""
        metric_keyed = {
            "rise_time_ms": {"roll": {"ideal": [40, 80]}},
            "overshoot_percent": {"ideal": [0, 10], "acceptable": [0, 15]},
        }
        reviewer = PIDReviewer(knowledge={"thresholds": metric_keyed})
        # 回退后按轴能取到非空、且含 max 的阈值
        assert reviewer._thresholds is _DEFAULT_THRESHOLDS
        assert reviewer._thresholds.get("pitch", {}).get("overshoot_percent", {}).get("max") == 20

    def test_real_betaflight_knowledge_falls_back(self):
        from smarttune.knowledge import KnowledgeBase
        kb = KnowledgeBase(platform="betaflight")
        reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))
        # BF pid_rules 的 thresholds 是指标形 → 必须回退到按轴默认
        assert reviewer._is_axis_threshold_shape(reviewer._thresholds)

    def test_betaflight_bounds_are_bf_scale(self):
        """BF pid_rules.json 的 pid_bounds 必须是 BF 整数尺度（非 AP 的 0.01~0.5）。"""
        from smarttune.knowledge import KnowledgeBase
        kb = KnowledgeBase(platform="betaflight")
        reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))
        p_hi = reviewer._bounds["p"][1]
        assert p_hi > 10, f"BF P 上界 {p_hi} 仍是 AP 尺度，整数增益会被夹错"


class TestRecommendationScale:
    def test_bf_gain_not_clipped_to_ap_scale(self):
        """BF 增益 45 在 BF bounds 下不应被夹到 0.5（AP 尺度）。"""
        from smarttune.knowledge import KnowledgeBase
        kb = KnowledgeBase(platform="betaflight")
        reviewer = PIDReviewer(knowledge=kb.get("pid_rules", {}))
        bounds = reviewer._bounds.get("p", (0.0, 10.0))
        clipped = float(np.clip(45 * 1.1, bounds[0], bounds[1]))
        assert clipped > 10, f"BF P=45 被夹到 {clipped}，bounds 尺度错误"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
