"""
tests/test_services_serialize.py

Tests for the smarttune.services.serialize module.
"""

import json

import numpy as np
import pytest

from smarttune.models.analysis_result import (
    Assessment,
    AxisPIDResult,
    Confidence,
    FFTAnalysisResult,
    MagFitResult,
    PIDAnalysisResult,
    ParamRecommendation,
    ParamRef,
    StepMetrics,
    VibrationPeak,
)
from smarttune.services.serialize import (
    _safe_float,
    _summarize_array,
    serialize_fft_result,
    serialize_magfit_result,
    serialize_param_recommendation,
    serialize_pid_result,
    to_jsonable,
)


class TestToJsonable:
    def test_none(self):
        assert to_jsonable(None) is None

    def test_primitives(self):
        assert to_jsonable("hello") == "hello"
        assert to_jsonable(42) == 42
        assert to_jsonable(3.14) == 3.14
        assert to_jsonable(True) is True

    def test_enum(self):
        assert to_jsonable(Assessment.GOOD) == "GOOD"
        assert to_jsonable(Confidence.HIGH) == "high"

    def test_numpy_scalar_int(self):
        assert to_jsonable(np.int64(42)) == 42
        assert isinstance(to_jsonable(np.int64(42)), int)

    def test_numpy_scalar_float(self):
        result = to_jsonable(np.float64(3.14))
        assert isinstance(result, float)

    def test_numpy_bool(self):
        assert to_jsonable(np.bool_(True)) is True

    def test_numpy_nan_becomes_none(self):
        assert _safe_float(float("nan")) is None

    def test_numpy_inf_becomes_none(self):
        assert _safe_float(float("inf")) is None

    def test_small_array(self):
        arr = np.array([1.0, 2.0, 3.0])
        result = to_jsonable(arr)
        assert "values" in result
        assert len(result["values"]) == 3

    def test_large_array_summarized(self):
        arr = np.random.randn(1000)
        result = to_jsonable(arr)
        assert "length" in result
        assert result["length"] == 1000
        assert "min" in result
        assert "max" in result
        assert "mean" in result
        assert "values" not in result

    def test_empty_array(self):
        arr = np.array([])
        result = to_jsonable(arr)
        assert result["values"] == []

    def test_dict_recursion(self):
        d = {"a": np.float64(1.0), "b": Assessment.GOOD}
        result = to_jsonable(d)
        assert result["a"] == 1.0
        assert result["b"] == "GOOD"

    def test_list_recursion(self):
        lst = [np.int64(1), "hello", Assessment.POOR]
        result = to_jsonable(lst)
        assert result == [1, "hello", "POOR"]

    def test_result_is_json_serializable(self):
        """Ensure to_jsonable output can actually be json.dumps'd."""
        data = {
            "arr": np.random.randn(100),
            "val": np.float64(1.23),
            "enum": Assessment.EXCELLENT,
            "nested": {"x": np.int32(5)},
        }
        result = to_jsonable(data)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)


class TestSerializeParamRecommendation:
    def test_basic(self):
        rec = ParamRecommendation(
            param=ParamRef("pid.roll.p", axis="roll"),
            current=0.12,
            suggested=0.14,
            reason="Slow rise time",
            confidence=Confidence.MEDIUM,
            action="increase",
        )
        result = serialize_param_recommendation(rec)
        assert result["generic_param"] == "pid.roll.p"
        assert result["current"] == 0.12
        assert result["suggested"] == 0.14
        assert result["action"] == "increase"
        assert result["confidence"] == "medium"

    def test_with_adapter(self):
        """With an adapter, platform param name should be included."""
        from smarttune.platform.registry import get_adapter
        adapter = get_adapter("ardupilot")
        rec = ParamRecommendation(
            param=ParamRef("pid.roll.p", axis="roll"),
            current=0.12,
            suggested=0.14,
            reason="test",
        )
        result = serialize_param_recommendation(rec, adapter)
        assert result["param"] == "ATC_RAT_RLL_P"
        assert result["generic_param"] == "pid.roll.p"


class TestSerializePIDResult:
    def test_basic(self):
        pid = PIDAnalysisResult(
            overall_assessment=Assessment.GOOD,
            axes={
                "roll": AxisPIDResult(
                    axis="roll",
                    metrics=StepMetrics(rise_time_ms=85.0, overshoot_percent=8.2),
                    assessment=Assessment.GOOD,
                    step_count=8,
                    recommendations=[
                        ParamRecommendation(
                            param=ParamRef("pid.roll.p"),
                            current=0.12,
                            suggested=0.14,
                            reason="Slow rise",
                        )
                    ],
                )
            },
        )
        result = serialize_pid_result(pid)
        assert result["overall_assessment"] == "GOOD"
        assert "roll" in result["axes"]
        assert result["axes"]["roll"]["step_count"] == 8
        assert len(result["axes"]["roll"]["recommendations"]) == 1

    def test_max_recommendations_cap(self):
        """Recommendations should be capped at max_recommendations."""
        recs = [
            ParamRecommendation(
                param=ParamRef(f"pid.roll.p{i}"),
                current=0.1,
                suggested=0.2,
                reason=f"reason {i}",
            )
            for i in range(10)
        ]
        pid = PIDAnalysisResult(
            axes={"roll": AxisPIDResult(axis="roll", metrics=StepMetrics(), recommendations=recs)}
        )
        result = serialize_pid_result(pid, max_recommendations=3)
        assert len(result["axes"]["roll"]["recommendations"]) == 3


class TestSerializeFFTResult:
    def test_basic(self):
        fft = FFTAnalysisResult(
            vibration_level=Assessment.GOOD,
            noise_floor=0.5,
            peaks=[VibrationPeak(frequency_hz=47.5, amplitude=2.1, source_guess="propeller")],
        )
        result = serialize_fft_result(fft)
        assert result["vibration_level"] == "GOOD"
        assert len(result["peaks"]) == 1
        assert result["peaks"][0]["frequency_hz"] == 47.5


class TestSerializeMagFitResult:
    def test_basic(self):
        mag = MagFitResult(
            assessment=Assessment.GOOD,
            fitness_mgauss=23.4,
            offsets={"x": 1.0, "y": -3.0, "z": 5.0},
        )
        result = serialize_magfit_result(mag)
        assert result["assessment"] == "GOOD"
        assert result["fitness_mgauss"] == 23.4
        assert result["offsets"]["x"] == 1.0
