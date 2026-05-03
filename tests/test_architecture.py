"""
tests/test_architecture.py

验证多平台架构的核心骨架：
- 模型可实例化
- 平台注册和检测机制正常
- 知识库加载逻辑正常
"""

import numpy as np
import pytest
from pathlib import Path


class TestFlightData:
    def test_create_minimal(self):
        from smarttune.models.flight_data import FlightData
        fd = FlightData(platform="ardupilot")
        assert fd.platform == "ardupilot"
        assert fd.axes == []
        assert not fd.has_mag

    def test_create_with_pid(self):
        from smarttune.models.flight_data import FlightData, AxisPIDSignal
        sig = AxisPIDSignal(
            timestamp_s=np.linspace(0, 10, 1000),
            desired=np.random.randn(1000),
            actual=np.random.randn(1000),
        )
        fd = FlightData(platform="betaflight", pid={"roll": sig, "pitch": sig})
        assert fd.axes == ["pitch", "roll"]
        assert sig.sample_count == 1000
        assert abs(sig.duration_s - 10.0) < 0.1

    def test_validate(self):
        from smarttune.models.flight_data import FlightData
        fd = FlightData(platform="ardupilot")
        issues = fd.validate()
        assert len(issues) > 0
        assert any("PID" in i for i in issues)


class TestParamRef:
    def test_auto_category(self):
        from smarttune.models.analysis_result import ParamRef
        ref = ParamRef("pid.roll.p", axis="roll")
        assert ref.category == "pid"

    def test_explicit_category(self):
        from smarttune.models.analysis_result import ParamRef
        ref = ParamRef("custom.thing", category="custom")
        assert ref.category == "custom"


class TestPlatformRegistry:
    def test_list_platforms(self):
        from smarttune.platform.registry import list_platforms
        platforms = list_platforms()
        names = [p["name"] for p in platforms]
        assert "ardupilot" in names
        assert "betaflight" in names
        assert "px4" in names

    def test_get_adapter(self):
        from smarttune.platform.registry import get_adapter
        adapter = get_adapter("ardupilot")
        assert adapter.name == "ardupilot"
        assert ".bin" in adapter.supported_extensions

    def test_get_unknown_raises(self):
        from smarttune.platform.registry import get_adapter
        from smarttune.errors import UnsupportedPlatformError
        with pytest.raises(UnsupportedPlatformError):
            get_adapter("unknown_fc")

    def test_capabilities(self):
        from smarttune.platform.registry import get_adapter
        ap = get_adapter("ardupilot")
        assert "pid" in ap.capabilities()
        assert "magfit" in ap.capabilities()

        bf = get_adapter("betaflight")
        assert "pid" in bf.capabilities()
        assert "magfit" not in bf.capabilities()

    def test_param_mapping_roundtrip(self):
        from smarttune.platform.registry import get_adapter
        ap = get_adapter("ardupilot")
        platform_name = ap.map_param_to_platform("pid.roll.p")
        assert platform_name == "ATC_RAT_RLL_P"
        generic = ap.map_param_to_generic("ATC_RAT_RLL_P")
        assert generic == "pid.roll.p"

        bf = get_adapter("betaflight")
        assert bf.map_param_to_platform("pid.roll.p") == "pid_roll_p"


class TestKnowledgeBase:
    def test_load_ardupilot(self):
        from smarttune.knowledge import KnowledgeBase
        kb = KnowledgeBase(platform="ardupilot")
        assert kb.source_info["builtin_platform"] is True
        assert "pid_rules" in kb.rules

    def test_load_common(self):
        from smarttune.knowledge import KnowledgeBase
        kb = KnowledgeBase(platform="ardupilot")
        assert "vibration_rules" in kb.rules

    def test_load_betaflight(self):
        from smarttune.knowledge import KnowledgeBase
        kb = KnowledgeBase(platform="betaflight")
        assert "pid_rules" in kb.rules


class TestErrors:
    def test_hierarchy(self):
        from smarttune.errors import SmartTuneError, LogFileNotFoundError, UnsupportedPlatformError
        assert issubclass(LogFileNotFoundError, SmartTuneError)
        assert issubclass(UnsupportedPlatformError, SmartTuneError)

    def test_render(self):
        from smarttune.errors import SmartTuneError
        exc = SmartTuneError(message="test error", hint="try again")
        panel = exc.rich_render()
        assert panel is not None
