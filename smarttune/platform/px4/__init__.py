"""
smarttune/platform/px4/__init__.py

PX4 ULog 日志适配器。

TODO: Phase 3 实现。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Set

from smarttune.platform.base import PlatformAdapter
from smarttune.platform.registry import register
from smarttune.models.flight_data import FlightData
from smarttune.errors import SmartTuneError

logger = logging.getLogger(__name__)

_PARAM_MAP_TO_PLATFORM = {
    "pid.roll.p":    "MC_ROLLRATE_P",
    "pid.roll.i":    "MC_ROLLRATE_I",
    "pid.roll.d":    "MC_ROLLRATE_D",
    "pid.roll.ff":   "MC_ROLLRATE_FF",
    "pid.pitch.p":   "MC_PITCHRATE_P",
    "pid.pitch.i":   "MC_PITCHRATE_I",
    "pid.pitch.d":   "MC_PITCHRATE_D",
    "pid.pitch.ff":  "MC_PITCHRATE_FF",
    "pid.yaw.p":     "MC_YAWRATE_P",
    "pid.yaw.i":     "MC_YAWRATE_I",
    "pid.yaw.d":     "MC_YAWRATE_D",
    "pid.yaw.ff":    "MC_YAWRATE_FF",
    "filter.gyro_lpf": "IMU_GYRO_CUTOFF",
}

_PARAM_MAP_TO_GENERIC = {v: k for k, v in _PARAM_MAP_TO_PLATFORM.items()}

# ULog magic bytes: "ULog" + version byte
_ULOG_MAGIC = b"ULog"


@register
class PX4Adapter(PlatformAdapter):
    """PX4 ULog 日志适配器。

    当前状态: 接口已就绪，ULog 解析器待 Phase 3 实现。
    将使用 pyulog 库进行解析。
    """

    @property
    def name(self) -> str:
        return "px4"

    @property
    def display_name(self) -> str:
        return "PX4"

    @property
    def supported_extensions(self) -> list[str]:
        return [".ulg", ".ulog"]

    @classmethod
    def detect(cls, path: Path) -> bool:
        if not path.is_file():
            return False
        suffix = path.suffix.lower()
        if suffix in (".ulg", ".ulog"):
            try:
                with open(path, "rb") as f:
                    header = f.read(8)
                return header[:4] == _ULOG_MAGIC or suffix in (".ulg", ".ulog")
            except (OSError, IOError):
                return False
        return False

    def parse(self, path: Path) -> FlightData:
        raise SmartTuneError(
            code="E9002",
            message="PX4 ULog parser not yet implemented",
            hint=(
                "PX4 support is planned for v2.x.\n"
                "Current version supports ArduPilot logs only."
            ),
        )

    def map_param_to_platform(self, generic_name: str) -> str:
        return _PARAM_MAP_TO_PLATFORM.get(generic_name, generic_name)

    def map_param_to_generic(self, platform_name: str) -> str:
        return _PARAM_MAP_TO_GENERIC.get(platform_name, platform_name)

    def capabilities(self) -> Set[str]:
        return {"pid", "fft", "filter", "magfit", "hardware", "quality"}

    def param_table(self):
        from smarttune.platform.params import ParamTable
        return ParamTable.from_knowledge("px4")
