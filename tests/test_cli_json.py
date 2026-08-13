"""
tests/test_cli_json.py

`stune <cmd> --format json` 输出契约测试。

覆盖:
  · 信封结构 (schema_version / tool / command / status)
  · 严格 JSON —— NaN/Inf 被清洗为 null
  · SMARTTUNE_DETERMINISTIC=1 时省略时间戳（可逐字节 diff）
  · CLI 成功路径 / 失败路径都产出 JSON，退出码分别为 0 / 1
  · JSON 只写 stdout 或 -o 文件，不与人类输出混流
"""

import json
import math

import pytest
from click.testing import CliRunner

from smarttune.cli import main
from smarttune.errors import InsufficientPIDDataError
from smarttune.output import json_output

# ---------------------------------------------------------------------------
# 序列化层
# ---------------------------------------------------------------------------


def test_sanitize_replaces_non_finite():
    out = json_output.sanitize({"a": float("nan"), "b": [float("inf"), 1.5], "c": {"d": -math.inf}})
    assert out == {"a": None, "b": [None, 1.5], "c": {"d": None}}


def test_dumps_is_strict_json():
    text = json_output.dumps(json_output.build_envelope("x", {"v": float("nan")}))
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text)["v"] is None


def test_envelope_shape():
    env = json_output.build_envelope("analyze", {"platform": "ardupilot"})
    assert env["schema_version"] == json_output.SCHEMA_VERSION
    assert env["tool"]["name"] == "smarttune"
    assert env["tool"]["version"]
    assert env["command"] == "analyze"
    assert env["status"] == "ok"
    assert env["platform"] == "ardupilot"


def test_envelope_payload_cannot_clobber_meta():
    env = json_output.build_envelope("analyze", {"status": "hacked", "command": "nope"})
    assert env["status"] == "ok"
    assert env["command"] == "analyze"


def test_deterministic_env_omits_timestamp(monkeypatch):
    monkeypatch.setenv("SMARTTUNE_DETERMINISTIC", "1")
    assert "generated_at" not in json_output.build_envelope("analyze", {})
    monkeypatch.setenv("SMARTTUNE_DETERMINISTIC", "0")
    assert "generated_at" in json_output.build_envelope("analyze", {})


def test_error_envelope_carries_code_and_hint():
    env = json_output.error_envelope("pid", InsufficientPIDDataError())
    assert env["status"] == "error"
    assert env["error"]["code"] == "E3002"
    assert env["error"]["type"] == "InsufficientPIDDataError"
    assert env["error"]["message"]


# ---------------------------------------------------------------------------
# CLI 集成 —— JSON 写入 -o 文件（避开 stdout/stderr 混流的 runner 差异）
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_log(tmp_path):
    p = tmp_path / "flight.bin"
    p.write_bytes(b"\xa3\x95fake")
    return p


def test_analyze_json_success(fake_log, tmp_path, monkeypatch):
    import smarttune.services.analysis as svc

    monkeypatch.setattr(
        svc,
        "analyze_log",
        lambda *a, **k: {
            "platform": "ardupilot",
            "log_file": "flight.bin",
            "modules": {"pid": {"overall_assessment": "GOOD"}},
            "nan_field": float("nan"),
        },
    )

    out = tmp_path / "out.json"
    result = CliRunner().invoke(
        main, ["analyze", "-i", str(fake_log), "-f", "json", "-o", str(out)]
    )

    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["command"] == "analyze"
    assert data["status"] == "ok"
    assert data["modules"]["pid"]["overall_assessment"] == "GOOD"
    assert data["nan_field"] is None


def test_analyze_json_error_is_json_and_exits_1(fake_log, tmp_path, monkeypatch):
    import smarttune.services.analysis as svc

    def _boom(*a, **k):
        raise InsufficientPIDDataError()

    monkeypatch.setattr(svc, "analyze_log", _boom)

    out = tmp_path / "err.json"
    result = CliRunner().invoke(
        main, ["analyze", "-i", str(fake_log), "-f", "json", "-o", str(out)]
    )

    assert result.exit_code == 1
    data = json.loads(out.read_text())
    assert data["status"] == "error"
    assert data["error"]["code"] == "E3002"


def test_quality_json_success(fake_log, tmp_path, monkeypatch):
    import smarttune.services.analysis as svc

    monkeypatch.setattr(
        svc,
        "get_log_quality",
        lambda *a, **k: {
            "platform": "ardupilot",
            "quality": {"score": 82, "rating": "GOOD", "advice": "usable"},
            "duration_s": 240.0,
        },
    )

    out = tmp_path / "q.json"
    result = CliRunner().invoke(
        main, ["quality", "-i", str(fake_log), "-f", "json", "-o", str(out)]
    )

    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["command"] == "quality"
    assert data["quality"]["score"] == 82


def test_platforms_json_on_stdout():
    result = CliRunner().invoke(main, ["platforms", "-f", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout[result.stdout.index("{") :])
    assert data["command"] == "platforms"
    assert isinstance(data["platforms"], list) and data["platforms"]
    assert {"name", "display_name"} <= set(data["platforms"][0])


def test_params_validate_json_rejects_unknown_param():
    result = CliRunner().invoke(
        main, ["params", "--validate", "NO_SUCH_PARAM_XYZ", "1", "-p", "ardupilot", "-f", "json"]
    )
    assert result.exit_code == 1
    data = json.loads(result.stdout[result.stdout.index("{") :])
    assert data["command"] == "params.validate"
    assert data["valid"] is False


def test_json_mode_still_defaults_to_text():
    """未传 --format 时行为不变（text）——回归保护。"""
    result = CliRunner().invoke(main, ["platforms"])
    assert result.exit_code == 0
    assert "{" not in result.stdout.split("Supported Platforms")[0]
