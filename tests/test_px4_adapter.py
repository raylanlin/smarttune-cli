"""
PX4 适配器单元测试 — 不依赖真实 .ulg 日志的部分。

parse() 的端到端验证需要真实 ULog 文件（见部署指南），
此处覆盖：detect 魔数、参数映射、能力声明、generic key 注入逻辑。
"""

from pathlib import Path

import numpy as np
import pytest

from smarttune.platform.px4 import (
    PX4Adapter,
    _PARAM_MAP_TO_PLATFORM,
    _ULOG_MAGIC,
)


@pytest.fixture
def adapter():
    return PX4Adapter()


class TestDetect:
    def test_magic_match(self, tmp_path, adapter):
        p = tmp_path / "flight.ulg"
        p.write_bytes(_ULOG_MAGIC + b"\x00" * 64)
        assert adapter.detect(p) is True

    def test_wrong_magic_rejected(self, tmp_path, adapter):
        """扩展名对但魔数错 → 拒绝（旧实现仅凭扩展名就放行）。"""
        p = tmp_path / "fake.ulg"
        p.write_bytes(b"NOTULOG" + b"\x00" * 64)
        assert adapter.detect(p) is False

    def test_wrong_extension_rejected(self, tmp_path, adapter):
        p = tmp_path / "flight.bin"
        p.write_bytes(_ULOG_MAGIC + b"\x00" * 64)
        assert adapter.detect(p) is False


class TestParamMapping:
    def test_roundtrip(self, adapter):
        for generic, plat in _PARAM_MAP_TO_PLATFORM.items():
            assert adapter.map_param_to_platform(generic) == plat
            assert adapter.map_param_to_generic(plat) == generic

    def test_unknown_passthrough(self, adapter):
        assert adapter.map_param_to_platform("unknown.key") == "unknown.key"


class TestCapabilities:
    def test_honest_capability_set(self, adapter):
        """能力集合只声明已实现的模块（filter/magfit/hardware 无 PX4 特化模块）。"""
        caps = adapter.capabilities()
        assert "pid" in caps and "fft" in caps and "sysid" in caps
        assert "filter" not in caps
        assert "magfit" not in caps
        assert "hardware" not in caps


class TestStepResponseDispatch:
    def test_px4_dispatch_module_importable(self):
        """pid_reviewer 的动态分派目标必须存在且导出标准接口。"""
        import importlib

        mod = importlib.import_module("smarttune.platform.px4.step_response_fft")
        assert hasattr(mod, "estimate_step_response")
        assert hasattr(mod, "compute_step_response_for_axis")


class TestParseWithSyntheticUlog:
    def test_parse_requires_pyulog(self, tmp_path, adapter, monkeypatch):
        """pyulog 缺失时给出明确安装提示而非裸 ImportError。"""
        import builtins
        import smarttune.platform.px4 as px4mod
        from smarttune.errors import SmartTuneError

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pyulog":
                raise ImportError("No module named 'pyulog'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        p = tmp_path / "flight.ulg"
        p.write_bytes(_ULOG_MAGIC + b"\x00" * 64)
        with pytest.raises(SmartTuneError) as ei:
            adapter.parse(p)
        assert "pyulog" in str(ei.value.hint or ei.value.message)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
