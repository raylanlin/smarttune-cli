"""
PX4 硬件配置报告 — 占位实现（Phase 3）

对外接口：
    generate_hardware_report(params, flight_data=None) -> Dict
    build_hardware_display_sections(report) -> List[str]
"""

from __future__ import annotations

from typing import Any, Dict, List


def generate_hardware_report(
    params: Dict[str, float],
    flight_data: Any = None,
) -> Dict[str, Any]:
    """PX4 硬件报告 — ULog 解析待 Phase 3 实现。"""
    firmware_version = getattr(flight_data, "firmware_version", "") if flight_data else ""
    return {
        "firmware_version": firmware_version,
        "filter_config": {
            "IMU_GYRO_CUTOFF":  params.get("IMU_GYRO_CUTOFF",  0.0),
            "IMU_DGYRO_CUTOFF": params.get("IMU_DGYRO_CUTOFF", 0.0),
        },
        "pid_params": {
            axis: {
                "P":  params.get(f"MC_{axis.upper()}RATE_P",  0.0),
                "I":  params.get(f"MC_{axis.upper()}RATE_I",  0.0),
                "D":  params.get(f"MC_{axis.upper()}RATE_D",  0.0),
                "FF": params.get(f"MC_{axis.upper()}RATE_FF", 0.0),
            }
            for axis in ["roll", "pitch", "yaw"]
        },
        "battery_reports": [],
        "integrity_issues": [],
        "total_params": len(params),
        "_note": "PX4 ULog parse not yet implemented (Phase 3)",
    }


def build_hardware_display_sections(report: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    if report.get("firmware_version"):
        lines.append(f"  Firmware: {report['firmware_version']}")
    fc = report.get("filter_config", {})
    if fc.get("IMU_GYRO_CUTOFF", 0) > 0:
        lines.append(f"  IMU_GYRO_CUTOFF = {fc['IMU_GYRO_CUTOFF']:.0f} Hz")
    lines.append("  [yellow](PX4 full hardware report pending Phase 3)[/yellow]")
    return lines
