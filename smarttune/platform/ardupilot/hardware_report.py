"""
ArduPilot 硬件配置报告 — 平台分派入口。

直接复用 analyzers/hardware_report.py 中的实现
（该模块原本就是 AP 专用的），通过此入口暴露统一接口。

对外接口：
    generate_hardware_report(params, flight_data=None) -> Dict
    build_hardware_display_sections(report) -> List[str]
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 直接复用现有实现（AP 专用，不需要包装）
from smarttune.analyzers.hardware_report import (
    generate_hardware_report,
    get_filter_config,
    get_ins_config,
    get_compass_config,
    get_battery_report,
    check_log_integrity,
    get_board_name,
    decode_devid,
)

__all__ = [
    "generate_hardware_report",
    "build_hardware_display_sections",
]


def build_hardware_display_sections(report: Dict[str, Any]) -> List[str]:
    """
    将 generate_hardware_report() 的结果格式化为 CLI 显示行。

    Returns
    -------
    List[str]
        Rich markup 格式行，供 cli.py 直接 print。
    """
    lines: List[str] = []

    sys_info = report.get("sys_info", {})
    if sys_info:
        lines.append("[bold]System:[/bold]")
        lines.append(f"  Board: {sys_info.get('board_name', 'Unknown')}")
        lines.append(f"  Frame: class={sys_info.get('frame_class', '?')} type={sys_info.get('frame_type', '?')}")
        lines.append(f"  Loop rate: {sys_info.get('sched_loop_rate', '?')} Hz")
        lines.append(f"  EKF type: {sys_info.get('ahrs_ekf_type', '?')}")

    imu_cfgs = report.get("imu_configs", [])
    if imu_cfgs:
        lines.append("[bold]IMU:[/bold]")
        for cfg in imu_cfgs:
            gi = cfg.get("gyro_info", {})
            ai = cfg.get("accel_info", {})
            lines.append(
                f"  IMU{cfg['imu_index']}: Gyro={gi.get('name','?')} "
                f"({gi.get('bus_type','?')} bus{gi.get('bus','?')})  "
                f"Accel={ai.get('name','?')}"
            )

    fc = report.get("filter_config", {})
    if fc:
        lines.append("[bold]Filters:[/bold]")
        lines.append(f"  Gyro LPF: {fc.get('gyro_filter', 0):.0f} Hz")
        for nf in fc.get("notch_filters", []):
            if nf.get("enable"):
                lines.append(
                    f"  {nf['prefix']}: {nf['freq']:.0f} Hz  "
                    f"BW={nf['bw']:.0f}  ATT={nf['att']:.0f} dB  "
                    f"Mode={nf['mode']}"
                )

    issues = report.get("integrity_issues", [])
    if issues:
        lines.append("[bold yellow]Log integrity issues:[/bold yellow]")
        for iss in issues:
            lines.append(f"  ⚠ {iss}")

    return lines
