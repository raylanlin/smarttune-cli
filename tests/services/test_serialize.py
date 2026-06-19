"""回归测试: serialize_magfit_result 兼容 FitResult + MagFitResult 两种 result 类型.

背景 (2026-06-19 hotfix): analyzer.MAGFit.analyze() 返回 FitResult (4 个 numpy 数组),
services 层期望 MagFitResult (recommendations + offsets dict). 类型不匹配导致
AttributeError, 整条 analyze_log 在 magfit 模块崩.
"""
import pytest
import numpy as np

from smarttune.services.serialize import serialize_magfit_result
from smarttune.analyzers.magfit import FitResult
from smarttune.models.analysis_result import (
    MagFitResult,
    Assessment,
    Confidence,
    ParamRecommendation,
    ParamRef,
)


def _make_fit_result() -> FitResult:
    """构造一个典型的 FitResult (analyzer 返回)."""
    return FitResult(
        ofs=np.array([1247.4, 2000.0, -971.1]),
        dia=np.array([1.0, 1.0, 1.0]),
        odi=np.array([0.0, 0.0, 0.0]),
        mot=np.array([0.0, 0.0, 0.0]),
        scale=1.0,
        fitness_mgauss=277.3,
        assessment="POOR",
        warnings=[],
        coverage={},
    )


def _make_magfit_result() -> MagFitResult:
    """构造一个典型的 MagFitResult (services 层)."""
    return MagFitResult(
        fitness_mgauss=148.4,
        assessment=Assessment.POOR,
        offsets={"x": 100.0, "y": 50.0, "z": -75.0},
        recommendations=[],
    )


class TestSerializeMagfitResultDuckType:
    """serialize_magfit_result 必须兼容 analyzer 的 FitResult."""

    def test_fit_result_returns_dict(self):
        result = serialize_magfit_result(_make_fit_result())
        assert isinstance(result, dict)
        assert {"assessment", "fitness_mgauss", "offsets", "recommendations"} <= set(result.keys())

    def test_fit_result_assessment_str_passthrough(self):
        result = serialize_magfit_result(_make_fit_result())
        assert result["assessment"] == "POOR"

    def test_fit_result_offsets_from_ofs_ndarray(self):
        result = serialize_magfit_result(_make_fit_result())
        assert result["offsets"] == {"x": 1247.4, "y": 2000.0, "z": -971.1}

    def test_fit_result_recommendations_empty(self):
        result = serialize_magfit_result(_make_fit_result())
        assert result["recommendations"] == []

    def test_fit_result_fitness_passthrough(self):
        result = serialize_magfit_result(_make_fit_result())
        assert result["fitness_mgauss"] == 277.3


class TestSerializeMagfitResultMagFitResult:
    """serialize_magfit_result 必须仍兼容 services 层的 MagFitResult (向后兼容)."""

    def test_magfit_result_returns_dict(self):
        result = serialize_magfit_result(_make_magfit_result())
        assert isinstance(result, dict)
        assert {"assessment", "fitness_mgauss", "offsets", "recommendations"} <= set(result.keys())

    def test_magfit_result_assessment_enum_value(self):
        result = serialize_magfit_result(_make_magfit_result())
        assert result["assessment"] == Assessment.POOR.value

    def test_magfit_result_offsets_dict(self):
        result = serialize_magfit_result(_make_magfit_result())
        assert result["offsets"] == {"x": 100.0, "y": 50.0, "z": -75.0}

    def test_magfit_result_recommendations_preserved(self):
        recs_in = [
            ParamRecommendation(
                param=ParamRef(generic_name="mag.ofs.x", axis="x", category="mag"),
                current=100.0,
                suggested=85.0,
                reason="test",
                confidence=Confidence.HIGH,
                action="decrease",
            ),
        ]
        mfr = MagFitResult(
            fitness_mgauss=148.4,
            assessment=Assessment.POOR,
            offsets={"x": 100.0, "y": 50.0, "z": -75.0},
            recommendations=recs_in,
        )
        result = serialize_magfit_result(mfr)
        assert len(result["recommendations"]) == 1
        assert result["recommendations"][0]["generic_param"] == "mag.ofs.x"
        # change_percent is a property: (85-100)/100*100 = -15.0
        assert result["recommendations"][0]["change_percent"] == pytest.approx(-15.0)


class TestSerializeMagfitResultRegression:
    """端到端: 真实 .bin 跑 analyze_log, 6 个模块全在, magfit 不再崩."""

    @pytest.fixture
    def real_bin_path(self, request):
        from pathlib import Path
        import sys
        candidate = Path.home() / "ardupilot/ArduCopter/logs/00000288.BIN"
        if not candidate.exists():
            pytest.skip(f"无真实 .bin 跳过: {candidate}")

        # analyze_log() 会通过 importlib.import_module() 加载
        # smarttune.platform.{platform}.hardware_report, 该模块在加载时
        # `from smarttune.analyzers.hardware_report import generate_hardware_report`
        # 缓存了当时引用的函数对象. 一旦缓存, 后续测试即使 mock 了
        # analyzers.hardware_report.generate_hardware_report 也无法影响
        # platform 子模块里的引用 (importlib 返回缓存的 module, 不再执行
        # 模块级代码). 为避免污染后续 test_services_analysis 的 mock,
        # 跑完后清理 sys.modules 中的 platform 子模块.
        yield str(candidate)

        to_remove = [
            name for name in sys.modules
            if name.startswith("smarttune.platform.")
        ]
        for name in to_remove:
            del sys.modules[name]

    def test_analyze_log_with_real_bin(self, real_bin_path):
        from smarttune.services.analysis import analyze_log
        result = analyze_log(real_bin_path, platform="ardupilot")
        assert "modules" in result
        assert "magfit" in result["modules"]
        mf = result["modules"]["magfit"]
        assert "assessment" in mf
        assert "fitness_mgauss" in mf
        assert "offsets" in mf
        assert "recommendations" in mf