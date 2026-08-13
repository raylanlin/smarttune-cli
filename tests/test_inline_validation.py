"""
tests/test_inline_validation.py

v3.2.1 — 分析结果内联校验 + dict 形 FFT 推荐转换 的回归测试。

内联校验把「推荐前必须 validate」从纪律变成机制：services 层产出的每条
recommendation 都带 validated / validation_status，AI 不再需要逐条回访
validate_param。这里不依赖真实日志，直接对注入的 payload / 假表验证。
"""

from smarttune.models.analysis_result import FullAnalysisResult
from smarttune.platform.params import ParamDef, ParamTable
from smarttune.services.analysis import _attach_validation
from smarttune.services.serialize import serialize_fft_result


class _FakeAdapter:
    """Just enough adapter surface for _attach_validation / serialize."""

    def __init__(self, table=None, mapping=None):
        self._table = table
        self._mapping = mapping or {}

    def param_table(self):
        if self._table is None:
            raise FileNotFoundError("no table")
        return self._table

    def map_param_to_platform(self, generic_name):
        return self._mapping.get(generic_name, generic_name)


def _table():
    return ParamTable(
        "Test",
        [
            ParamDef(name="ATC_RAT_RLL_P", category="pid", type="float", min=0.0, max=0.35),
            ParamDef(
                name="BATT_MONITOR",
                category="battery",
                type="enum",
                values={"0": "Disabled", "4": "Analog Voltage and Current"},
            ),
        ],
    )


# ---------------------------------------------------------------------------
# _attach_validation
# ---------------------------------------------------------------------------


def test_recommendations_arrive_pre_validated():
    payload = {
        "modules": {
            "pid": {
                "axes": {
                    "roll": {
                        "recommendations": [
                            {"param": "ATC_RAT_RLL_P", "suggested": 0.15},
                            {"param": "ATC_RAT_RLL_P", "suggested": 999},
                            {"param": "NOPE_XYZ", "suggested": 1},
                        ]
                    }
                }
            },
            "fft": {"recommendations": [{"param": "BATT_MONITOR", "suggested": 4}]},
        }
    }
    _attach_validation(payload, _FakeAdapter(_table()))

    recs = payload["modules"]["pid"]["axes"]["roll"]["recommendations"]
    assert recs[0]["validated"] is True and recs[0]["validation_status"] == "ok"
    assert recs[1]["validated"] is False and recs[1]["validation_status"] == "out_of_range"
    assert "validation_message" in recs[1]
    assert recs[2]["validated"] is False and recs[2]["validation_status"] == "not_found"

    fft_rec = payload["modules"]["fft"]["recommendations"][0]
    assert fft_rec["validated"] is True


def test_non_numeric_suggestion_is_unverifiable():
    payload = {"recommendations": [{"param": "ATC_RAT_RLL_P", "suggested": None}]}
    _attach_validation(payload, _FakeAdapter(_table()))
    rec = payload["recommendations"][0]
    assert rec["validated"] is False and rec["validation_status"] == "unverifiable"


def test_missing_param_table_fails_open_as_annotation_only():
    """Absence of the table must never break analysis — payload untouched."""
    payload = {"recommendations": [{"param": "X", "suggested": 1}]}
    _attach_validation(payload, _FakeAdapter(table=None))
    assert "validated" not in payload["recommendations"][0]


# ---------------------------------------------------------------------------
# dict 形 FFT 推荐（v3.2.1 前被静默丢弃）
# ---------------------------------------------------------------------------


def test_serialize_fft_dict_recommendations_are_kept():
    result = {
        "vibration_level": "MARGINAL",
        "noise_floor": 1.0,
        "peaks": [],
        "recommendations": {
            "filter.notch1.freq": 93.7,
            "filter.notch1.enable": 1,
            "filter.notch1.mode": True,  # bool 被跳过
        },
    }
    out = serialize_fft_result(
        result, _FakeAdapter(mapping={"filter.notch1.freq": "INS_HNTCH_FREQ"})
    )
    recs = {r["generic_param"]: r for r in out["recommendations"]}
    assert recs["filter.notch1.freq"]["param"] == "INS_HNTCH_FREQ"
    assert recs["filter.notch1.freq"]["suggested"] == 93.7
    assert recs["filter.notch1.freq"]["action"] == "set"
    assert "filter.notch1.mode" not in recs  # bool skipped


def test_all_recommendations_includes_dict_fft():
    full = FullAnalysisResult(platform="ardupilot", log_file="x.bin")
    full.fft = {"recommendations": {"filter.notch1.freq": 93.7, "filter.gyro_lpf": 40}}
    recs = full.all_recommendations
    names = {r.param.generic_name for r in recs}
    assert names == {"filter.notch1.freq", "filter.gyro_lpf"}
    assert all(r.action == "set" for r in recs)
