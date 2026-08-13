"""
Betaflight 硬件配置报告 — 从 BBL header 参数生成。

BF 日志的硬件信息存在于 BBL header properties（由 bbl_parser 解析）：
  - Firmware revision / type
  - craft_name / board_info
  - looptime / pid_process_denom
  - PID gains (pid_roll_p / pid_roll_i / pid_roll_d / pid_roll_f ...)
  - 滤波器参数

对外接口：
    generate_hardware_report(params, flight_data=None) -> Dict
    build_hardware_display_sections(report) -> List[str]
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np


def generate_hardware_report(
    params: Dict[str, float],
    flight_data: Any = None,
) -> Dict[str, Any]:
    """
    生成 Betaflight 硬件配置报告。

    Parameters
    ----------
    params : Dict[str, float]
        从 BBL header 解析的参数（由 FlightData.params 提供）。
    flight_data : FlightData, optional
        完整飞行数据（提供 extras 中的 firmware_type / craft_name 等）。

    Returns
    -------
    Dict[str, Any]
    """
    # ── 固件信息 ───────────────────────────────────────────────
    firmware_version = ""
    craft_name = ""
    board_info = ""
    firmware_type = ""
    data_version = 0
    frame_count = 0

    if flight_data is not None:
        firmware_version = getattr(flight_data, "firmware_version", "") or ""
        board_info = getattr(flight_data, "board_name", "") or ""
        extras = getattr(flight_data, "extras", {}) or {}
        craft_name = extras.get("craft_name", "")
        firmware_type = extras.get("firmware_type", "")
        data_version = extras.get("data_version", 0)
        frame_count = extras.get("frame_count", 0)

    # ── 循环率 ────────────────────────────────────────────────
    looptime = params.get("looptime", 0.0)
    if looptime and looptime > 100:
        loop_hz = round(1_000_000.0 / looptime)
    elif looptime and looptime > 0:
        loop_hz = round(looptime)
    else:
        loop_hz = 0

    pid_denom = int(params.get("pid_process_denom", params.get("P interval", 1)))
    pid_hz = round(loop_hz / pid_denom) if pid_denom > 0 and loop_hz > 0 else 0

    # ── PID 参数 ──────────────────────────────────────────────
    pid_params: Dict[str, Dict] = {}
    for axis in ["roll", "pitch", "yaw"]:
        pid_params[axis] = {
            "P": params.get(f"pid_{axis}_p", 0.0),
            "I": params.get(f"pid_{axis}_i", 0.0),
            "D": params.get(f"pid_{axis}_d", 0.0),
            "FF": params.get(f"pid_{axis}_f", params.get(f"pid_{axis}_FF", 0.0)),
        }

    # ── 滤波器参数 ────────────────────────────────────────────
    filter_config = {
        "gyro_lowpass_hz": params.get("gyro_lowpass_hz", 0.0),
        "gyro_lowpass2_hz": params.get("gyro_lowpass2_hz", 0.0),
        "dterm_lowpass_hz": params.get("dterm_lowpass_hz", 0.0),
        "dterm_lowpass2_hz": params.get("dterm_lowpass2_hz", 0.0),
        "gyro_notch1_hz": params.get("gyro_notch1_hz", 0.0),
        "gyro_notch1_cutoff": params.get("gyro_notch1_cutoff", 0.0),
        "gyro_notch2_hz": params.get("gyro_notch2_hz", 0.0),
        "gyro_notch2_cutoff": params.get("gyro_notch2_cutoff", 0.0),
        "rpm_filter_min_hz": params.get("rpm_filter_min_hz", 0.0),
    }

    # ── 电池 ──────────────────────────────────────────────────
    battery_reports: List[Dict] = []
    if flight_data is not None:
        bv = getattr(flight_data, "battery_voltage", None)
        if bv is not None and len(bv) > 0:
            bc = getattr(flight_data, "battery_current", None)
            rep: Dict[str, Any] = {
                "id": 0,
                "voltage_min": float(np.min(bv)),
                "voltage_max": float(np.max(bv)),
                "voltage_mean": float(np.mean(bv)),
                "current_max": float(np.max(bc)) if bc is not None and len(bc) > 0 else 0.0,
                "current_mean": float(np.mean(bc)) if bc is not None and len(bc) > 0 else 0.0,
            }
            battery_reports.append(rep)

    # ── 日志完整性 ────────────────────────────────────────────
    integrity_issues: List[str] = []
    if flight_data is not None and hasattr(flight_data, "validate"):
        integrity_issues = flight_data.validate()

    return {
        "firmware_version": firmware_version,
        "firmware_type": firmware_type,
        "craft_name": craft_name,
        "board_info": board_info,
        "data_version": data_version,
        "frame_count": frame_count,
        "loop_rate_hz": loop_hz,
        "pid_rate_hz": pid_hz,
        "pid_params": pid_params,
        "filter_config": filter_config,
        "battery_reports": battery_reports,
        "integrity_issues": integrity_issues,
        "total_params": len(params),
    }


def build_hardware_display_sections(report: Dict[str, Any]) -> List[str]:
    """
    将 generate_hardware_report() 结果格式化为 CLI 显示行。

    Returns
    -------
    List[str]
        Rich markup 格式行。
    """
    lines: List[str] = []

    lines.append("[bold]Firmware:[/bold]")
    lines.append(f"  Version: {report.get('firmware_version', '?')}")
    lines.append(f"  Type: {report.get('firmware_type', '?')}")
    if report.get("craft_name"):
        lines.append(f"  Craft: {report['craft_name']}")
    if report.get("board_info"):
        lines.append(f"  Board: {report['board_info']}")

    lines.append("[bold]Loop rates:[/bold]")
    lines.append(f"  Gyro loop: {report.get('loop_rate_hz', '?')} Hz")
    lines.append(f"  PID loop:  {report.get('pid_rate_hz', '?')} Hz")

    pid_params = report.get("pid_params", {})
    if pid_params:
        lines.append("[bold]PID gains:[/bold]")
        for axis in ["roll", "pitch", "yaw"]:
            g = pid_params.get(axis, {})
            lines.append(
                f"  {axis.capitalize()}: P={g.get('P', 0):.0f}  "
                f"I={g.get('I', 0):.0f}  D={g.get('D', 0):.0f}  "
                f"FF={g.get('FF', 0):.0f}"
            )

    fc = report.get("filter_config", {})
    if fc:
        lines.append("[bold]Filters:[/bold]")
        for key, label in [
            ("gyro_lowpass_hz", "Gyro LPF1"),
            ("gyro_lowpass2_hz", "Gyro LPF2"),
            ("dterm_lowpass_hz", "D-term LPF"),
        ]:
            val = fc.get(key, 0.0)
            if val > 0:
                lines.append(f"  {label}: {val:.0f} Hz")
        for idx in [1, 2]:
            hz = fc.get(f"gyro_notch{idx}_hz", 0.0)
            cut = fc.get(f"gyro_notch{idx}_cutoff", 0.0)
            if hz > 0:
                lines.append(f"  Notch{idx}: {hz:.0f} Hz, cutoff={cut:.0f} Hz")
        rpm = fc.get("rpm_filter_min_hz", 0.0)
        if rpm > 0:
            lines.append(f"  RPM filter: min={rpm:.0f} Hz")

    issues = report.get("integrity_issues", [])
    if issues:
        lines.append("[bold yellow]Log integrity issues:[/bold yellow]")
        for iss in issues:
            lines.append(f"  ⚠ {iss}")

    return lines
