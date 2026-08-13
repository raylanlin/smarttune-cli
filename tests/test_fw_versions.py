"""
tests/test_fw_versions.py

v3.3 — 多固件版本参数表解析 + MCP status 别名移除的回归测试。
"""

import pytest
from click.testing import CliRunner

from smarttune.cli import main
from smarttune.platform.params import ParamTable


def test_available_platforms_excludes_versioned_tables():
    platforms = ParamTable.available_platforms()
    assert set(platforms) == {"ardupilot", "betaflight", "px4"}


def test_available_versions_lists_default_and_copter45():
    versions = ParamTable.available_versions("ardupilot")
    assert versions[0] == "default"
    assert "copter-4.5" in versions
    assert ParamTable.available_versions("betaflight") == ["default"]


def test_versioned_table_loads_and_differs_from_default():
    default = ParamTable.from_knowledge("ardupilot")
    v45 = ParamTable.from_knowledge("ardupilot", "copter-4.5")
    assert v45.fw_version == "copter-4.5"
    assert v45.schema_version == 2
    assert len(v45) > len(default), "Copter-4.5 carries more parameters than 4.1"
    assert v45.query("BATT_MONITOR") is not None
    assert v45.query("ATC_RAT_RLL_P").max == pytest.approx(0.5)


def test_fw_normalization_and_default_alias():
    a = ParamTable.from_knowledge("ardupilot", "Copter_4.5")
    assert a.fw_version == "copter-4.5"
    b = ParamTable.from_knowledge("ardupilot", "default")
    assert b.fw_version == ""


def test_unknown_fw_version_lists_available():
    with pytest.raises(FileNotFoundError) as exc:
        ParamTable.from_knowledge("ardupilot", "copter-9.9")
    assert "copter-4.5" in str(exc.value)


def _json_out(result):
    import json

    return json.loads(result.stdout[result.stdout.index("{") :])


def test_cli_fw_version_flag():
    res = CliRunner().invoke(
        main, ["params", "ap", "--fw-version", "copter-4.5", "--groups", "-f", "json"]
    )
    assert res.exit_code == 0, res.output
    data = _json_out(res)
    assert data["parameter_count"] > 4000


def test_cli_fw_version_validate():
    res = CliRunner().invoke(
        main,
        [
            "params",
            "--validate",
            "ATC_RAT_RLL_P",
            "0.45",
            "-p",
            "ap",
            "--fw-version",
            "copter-4.5",
            "-f",
            "json",
        ],
    )
    # 0.45 is legal in 4.5 (max 0.5) but out of range in the 4.1 default (max 0.35)
    assert res.exit_code == 0, res.output
    old = CliRunner().invoke(
        main, ["params", "--validate", "ATC_RAT_RLL_P", "0.45", "-p", "ap", "-f", "json"]
    )
    assert old.exit_code == 1
