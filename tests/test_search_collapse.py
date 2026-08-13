"""
tests/test_search_collapse.py

v3.3.1 — 搜索结果折叠编号组 + 截断信号的回归测试。

背景：ArduPilot 按实例复制整组参数（BATT2_..BATT9_ 是 BATT_ 的克隆）。
搜 "monitor" 返回 ~46 条，其中 ~40 条是同名克隆，把真正不同的参数挤出
limit 截断线之外；且旧返回体 returned<count 时没有任何截断信号，LLM 会
把看到的部分当成全集下结论。
"""

import json

from click.testing import CliRunner

from smarttune.cli import main
from smarttune.platform.params import ParamDef, ParamTable, collapse_numbered


def _mk(name):
    return ParamDef(
        name=name,
        category="battery",
        type="enum",
        values={"0": "Disabled", "4": "Analog Voltage and Current"},
    )


def test_collapse_folds_numbered_clones_into_base():
    params = [_mk("BATT_MONITOR")] + [_mk(f"BATT{i}_MONITOR") for i in range(2, 10)]
    folded = collapse_numbered(params)
    assert len(folded) == 1
    base, instances = folded[0]
    assert base.name == "BATT_MONITOR"
    assert instances[0] == "BATT_" and "BATT9_" in instances
    assert len(instances) == 9


def test_collapse_keeps_distinct_params_and_order():
    params = [
        _mk("BATT_MONITOR"),
        _mk("BATT2_MONITOR"),
        _mk("CAN_D1_UC_ESC_BM"),
        _mk("EK3_MAG_CAL"),
    ]
    folded = collapse_numbered(params)
    names = [p.name for p, _ in folded]
    assert names == ["BATT_MONITOR", "CAN_D1_UC_ESC_BM", "EK3_MAG_CAL"]


def test_collapse_clone_before_base_still_folds():
    params = [_mk("BATT2_MONITOR"), _mk("BATT_MONITOR")]
    folded = collapse_numbered(params)
    assert len(folded) == 1
    assert folded[0][0].name == "BATT_MONITOR"
    assert set(folded[0][1]) == {"BATT_", "BATT2_"}


def test_collapse_numbered_without_base_is_kept():
    params = [_mk("GPS2_RATE")]  # base GPS_RATE absent from the result set
    folded = collapse_numbered(params)
    assert folded == [(params[0], [])]


def test_real_table_monitor_search_is_dominated_no_more():
    tbl = ParamTable.from_knowledge("ardupilot")
    hits = tbl.search("monitor")
    folded = collapse_numbered(hits)
    assert len(folded) < len(hits), "numbered clones should fold"
    base = next((inst for p, inst in folded if p.name == "BATT_MONITOR"), None)
    assert base and len(base) >= 8, "BATT_MONITOR should fold its 8+ instances"


def _json_out(result):
    return json.loads(result.stdout[result.stdout.index("{") :])


def test_cli_search_json_has_instances_and_no_silent_truncation():
    res = CliRunner().invoke(main, ["params", "--search", "monitor", "-p", "ap", "-f", "json"])
    assert res.exit_code == 0, res.output
    data = _json_out(res)
    block = data["platforms"]["ArduPilot"]
    assert block["raw_count"] > block["count"], "clones folded"
    monitor = next(m for m in block["matches"] if m["name"] == "BATT_MONITOR")
    assert len(monitor["instances"]) >= 8
    # contract: if returned < count the block MUST say truncated
    if block["returned"] < block["count"]:
        assert block["truncated"] is True and "note" in block


def test_cli_search_truncation_is_flagged():
    res = CliRunner().invoke(
        main, ["params", "--search", "gps", "-p", "ap", "--limit", "3", "-f", "json"]
    )
    assert res.exit_code == 0, res.output
    block = _json_out(res)["platforms"]["ArduPilot"]
    assert block["returned"] == 3
    assert block["truncated"] is True
    assert "more distinct hits" in block["note"]
