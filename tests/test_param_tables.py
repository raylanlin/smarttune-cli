"""
tests/test_param_tables.py

参数表数据契约 + 校验逻辑测试。

每个用例都对应 v3.1 及更早版本里真实存在过的缺陷：
  · 名字被剥了组前缀（MONITOR / 0_BAUD）
  · 描述整列偏移一位（ARM_MAH 挂着 BATT_OPTIONS 的描述）
  · 描述里残留 @PREFIX@ 占位符
  · enum 没有成员表，validate() 对任何数值都返回 True（安全闸门失效）
  · default 全表清一色 0.0
"""

import json
import re

import pytest
from click.testing import CliRunner

from smarttune.cli import main
from smarttune.platform.param_lint import lint_table
from smarttune.platform.params import (
    STATUS_NOT_A_MEMBER,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_OUT_OF_RANGE,
    STATUS_UNVERIFIABLE,
    ParamDef,
    ParamTable,
    to_full_dict,
    to_slim_dict,
)

PLATFORMS = ["ardupilot", "betaflight", "px4"]


@pytest.fixture(scope="module")
def tables():
    return {p: ParamTable.from_knowledge(p) for p in PLATFORMS}


# ---------------------------------------------------------------------------
# 数据完整性
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", PLATFORMS)
def test_table_is_schema_v2_with_provenance(tables, platform):
    tbl = tables[platform]
    assert tbl.schema_version == 2
    src = tbl.meta.get("source") or {}
    assert src.get("upstream"), "table must record where the data came from"
    assert src.get("generated")
    assert len(tbl) > 0


@pytest.mark.parametrize("platform", PLATFORMS)
def test_lint_reports_no_errors(tables, platform):
    report = lint_table(tables[platform])
    assert report["ok"], report["findings"][:8]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_no_placeholder_leaks_in_descriptions(tables, platform):
    leaked = [p.name for p in tables[platform].list_all()
              if re.search(r"@[A-Z_]+@", p.description or "")]
    assert leaked == [], f"unexpanded upstream placeholders in {leaked[:5]}"


def test_ardupilot_names_are_full_firmware_names(tables):
    """The old scraper stripped group prefixes: MONITOR, 0_BAUD, 10_DIRECTION."""
    tbl = tables["ardupilot"]
    assert tbl.query("BATT_MONITOR") is not None
    assert tbl.query("MONITOR") is None
    assert tbl.query("SERIAL0_BAUD") is not None
    assert not [p.name for p in tbl.list_all() if p.name[:1].isdigit()]


def test_descriptions_are_not_offset_by_one(tables):
    """ARM_MAH used to carry BATT_OPTIONS's description."""
    tbl = tables["ardupilot"]
    monitor = tbl.query("BATT_MONITOR")
    assert "monitor" in monitor.description.lower()
    arm_mah = tbl.query("BATT_ARM_MAH")
    if arm_mah is not None:
        assert "capacity" in arm_mah.description.lower()
        assert "options" not in arm_mah.description.lower()


@pytest.mark.parametrize("platform", PLATFORMS)
def test_defaults_are_not_all_identical(tables, platform):
    """A single repeated default across the table means they were fabricated."""
    defaults = {repr(p.default) for p in tables[platform].list_all()}
    assert len(defaults) > 1 or defaults == {"None"}, (
        "every parameter shares one default value — data looks fabricated")


def test_px4_has_real_defaults(tables):
    px4 = tables["px4"]
    with_default = [p for p in px4.list_all() if p.default is not None]
    assert len(with_default) == len(px4)
    assert px4.query("MC_ROLLRATE_P").default == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# 枚举含义（AI 把参数表当知识库用的前提）
# ---------------------------------------------------------------------------

def test_enum_members_carry_meaning(tables):
    monitor = tables["ardupilot"].query("BATT_MONITOR")
    assert monitor.type == "enum"
    assert monitor.values["4"] == "Analog Voltage and Current"
    assert monitor.values["0"] == "Disabled"


def test_bitmask_members_present(tables):
    with_bits = [p for p in tables["ardupilot"].list_all() if p.bitmask]
    assert with_bits, "no bitmask metadata captured"
    assert all(k.isdigit() for p in with_bits for k in p.bitmask)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_every_enum_has_members_or_a_documented_gap(tables, platform):
    for p in tables[platform].list_all():
        if p.type in ("enum", "bitmask") and not p.values and not p.bitmask:
            assert p.unresolved_ref or p.min is not None or p.max is not None, (
                f"{p.name}: discrete parameter with no members, no range, no note")


# ---------------------------------------------------------------------------
# 校验：闸门必须真的关着
# ---------------------------------------------------------------------------

def test_enum_validate_checks_membership(tables):
    tbl = tables["ardupilot"]
    ok = tbl.validate_detail("BATT_MONITOR", 4)
    assert ok["valid"] and ok["status"] == STATUS_OK
    assert "Analog Voltage and Current" in ok["message"]

    bad = tbl.validate_detail("BATT_MONITOR", 99)
    assert not bad["valid"] and bad["status"] == STATUS_NOT_A_MEMBER
    assert "4" in bad["options"], "rejection must tell the agent what IS allowed"


def test_enum_validate_rejects_non_integer(tables):
    verdict = tables["ardupilot"].validate_detail("BATT_MONITOR", 4.5)
    assert not verdict["valid"]


def test_range_is_enforced(tables):
    tbl = tables["ardupilot"]
    assert tbl.validate_detail("ATC_RAT_RLL_P", 0.15)["valid"]
    high = tbl.validate_detail("ATC_RAT_RLL_P", 999)
    assert not high["valid"] and high["status"] == STATUS_OUT_OF_RANGE


def test_unknown_param_is_rejected(tables):
    verdict = tables["ardupilot"].validate_detail("NO_SUCH_PARAM_XYZ", 1)
    assert not verdict["valid"] and verdict["status"] == STATUS_NOT_FOUND


def test_betaflight_range_is_enforced(tables):
    tbl = tables["betaflight"]
    assert tbl.validate_detail("p_roll", 45)["valid"]
    assert not tbl.validate_detail("p_roll", 999)["valid"]
    assert tbl.query("gyro_lpf1_static_hz").max == 1000


def test_discrete_without_members_fails_closed():
    """The old validate() returned True for ANY value on a non-numeric type."""
    tbl = ParamTable("Test", [ParamDef(name="MYSTERY_ENUM", category="misc", type="enum")])
    verdict = tbl.validate_detail("MYSTERY_ENUM", 12345)
    assert verdict["valid"] is False
    assert verdict["status"] == STATUS_UNVERIFIABLE
    assert verdict["hint"]


def test_legacy_validate_wrapper_still_returns_tuple(tables):
    ok, msg = tables["ardupilot"].validate("ATC_RAT_RLL_P", 0.15)
    assert ok is True and isinstance(msg, str)


def test_loader_tolerates_unknown_keys():
    pd = ParamDef.from_dict({"name": "X", "category": "misc", "type": "float",
                             "future_field": 1, "values": [{"value": 2, "label": "Two"}]})
    assert pd.name == "X" and pd.values == {"2": "Two"}


# ---------------------------------------------------------------------------
# 组 / 搜索 / 返回体形状
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", PLATFORMS)
def test_groups_are_indexed(tables, platform):
    groups = tables[platform].groups()
    assert len(groups) > 10
    assert all(g["count"] > 0 and g["sample"] for g in groups)
    assert sum(g["count"] for g in groups) == len(tables[platform])


def test_group_lookup_accepts_both_spellings(tables):
    tbl = tables["ardupilot"]
    assert tbl.list_by_group("ATC_") == tbl.list_by_group("atc")
    assert any(p.name == "ATC_RAT_RLL_P" for p in tbl.list_by_group("ATC_"))
    assert any(p.name == "p_roll" for p in tables["betaflight"].list_by_group("PID_PROFILE"))


def test_search_ranks_exact_name_first(tables):
    hits = tables["ardupilot"].search("BATT_MONITOR")
    assert hits[0].name == "BATT_MONITOR"


def test_search_matches_enum_labels(tables):
    names = [p.name for p in tables["ardupilot"].search("analog voltage and current")]
    assert "BATT_MONITOR" in names


def test_search_matches_description_text(tables):
    assert tables["px4"].search("notch"), "keyword search should reach descriptions"


def test_slim_dict_is_small_and_full_dict_is_complete(tables):
    pd = tables["ardupilot"].query("BATT_MONITOR")
    slim, full = to_slim_dict(pd), to_full_dict(pd)
    assert "description" not in slim and "values" not in slim
    assert slim["enum_count"] == len(pd.values)
    assert len(json.dumps(slim)) < 400
    assert full["description"] and full["values"]["4"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _json_out(result):
    return json.loads(result.stdout[result.stdout.index("{"):])


def test_cli_groups_json():
    res = CliRunner().invoke(main, ["params", "ap", "--groups", "-f", "json"])
    assert res.exit_code == 0, res.output
    data = _json_out(res)
    assert data["command"] == "params.groups"
    assert data["group_count"] > 100


def test_cli_group_drilldown_json():
    res = CliRunner().invoke(main, ["params", "ap", "--group", "ATC_", "-f", "json"])
    assert res.exit_code == 0, res.output
    data = _json_out(res)
    assert data["group"] == "ATC_"
    assert any(p["name"] == "ATC_RAT_RLL_P" for p in data["params"])


def test_cli_get_param_json_has_enum_values():
    res = CliRunner().invoke(main, ["params", "BATT_MONITOR", "-f", "json"])
    assert res.exit_code == 0, res.output
    data = _json_out(res)
    assert data["matches"][0]["values"]["4"] == "Analog Voltage and Current"


def test_cli_validate_enum_member_json():
    ok = CliRunner().invoke(main, ["params", "--validate", "BATT_MONITOR", "4",
                                   "-p", "ap", "-f", "json"])
    assert ok.exit_code == 0
    assert _json_out(ok)["valid"] is True

    bad = CliRunner().invoke(main, ["params", "--validate", "BATT_MONITOR", "99",
                                    "-p", "ap", "-f", "json"])
    assert bad.exit_code == 1
    body = _json_out(bad)
    assert body["status"] == STATUS_NOT_A_MEMBER and body["options"]


def test_cli_search_json_is_ranked():
    res = CliRunner().invoke(main, ["params", "--search", "notch", "-p", "ap", "-f", "json"])
    assert res.exit_code == 0, res.output
    data = _json_out(res)
    assert data["count"] > 0 and data["matches"][0]["name"]


def test_cli_lint_passes_for_every_table():
    res = CliRunner().invoke(main, ["params", "--lint", "-f", "json"])
    assert res.exit_code == 0, res.output
    assert _json_out(res)["ok"] is True


def test_cli_unknown_query_exits_1():
    res = CliRunner().invoke(main, ["params", "NOPE_NOT_A_PARAM", "-f", "json"])
    assert res.exit_code == 1
    assert _json_out(res)["status"] == "error"
