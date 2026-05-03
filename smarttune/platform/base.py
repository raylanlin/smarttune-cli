"""
smarttune/platform/base.py

PlatformAdapter 抽象基类 — 每个飞控平台实现一个。

职责：
1. 日志文件检测与解析 → FlightData
2. 平台参数名 ↔ 通用参数名互转
3. 声明平台支持的分析能力
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Set

from smarttune.models.flight_data import FlightData


class PlatformAdapter(ABC):
    """飞控平台适配器抽象基类。

    子类必须实现所有 @abstractmethod 方法。
    新平台接入只需：
    1. 在 smarttune/platform/{name}/ 下创建模块
    2. 实现 PlatformAdapter 子类
    3. 在 registry.py 中注册
    """

    # ── 平台标识 ────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """平台标识符: "ardupilot" | "betaflight" | "px4" """

    @property
    @abstractmethod
    def display_name(self) -> str:
        """用户可见名: "ArduPilot" | "Betaflight" | "PX4" """

    @property
    @abstractmethod
    def supported_extensions(self) -> list[str]:
        """支持的日志文件扩展名列表: [".bin", ".log"] 等"""

    # ── 日志检测与解析 ──────────────────────────────────────

    @classmethod
    @abstractmethod
    def detect(cls, path: Path) -> bool:
        """检测指定文件是否属于本平台的日志格式。

        通过文件扩展名 + magic bytes / 文件头特征判断。
        此方法应该快速返回（只读文件头部），不做完整解析。

        Parameters
        ----------
        path : Path
            日志文件路径

        Returns
        -------
        bool
            True 表示该文件匹配本平台格式
        """

    @abstractmethod
    def parse(self, path: Path) -> FlightData:
        """解析日志文件，输出统一 FlightData 结构。

        Parameters
        ----------
        path : Path
            日志文件路径

        Returns
        -------
        FlightData
            填充了平台数据的统一飞行数据结构

        Raises
        ------
        SmartTuneError
            文件不存在、格式错误、数据不足等
        """

    # ── 参数名映射 ──────────────────────────────────────────

    @abstractmethod
    def map_param_to_platform(self, generic_name: str) -> str:
        """通用参数名 → 平台参数名。

        Examples
        --------
        ArduPilot:  "pid.roll.p"  → "ATC_RAT_RLL_P"
        Betaflight: "pid.roll.p"  → "pid_roll_p"
        PX4:        "pid.roll.p"  → "MC_ROLLRATE_P"
        """

    @abstractmethod
    def map_param_to_generic(self, platform_name: str) -> str:
        """平台参数名 → 通用参数名。

        ArduPilot:  "ATC_RAT_RLL_P"  → "pid.roll.p"
        Betaflight: "pid_roll_p"     → "pid.roll.p"
        PX4:        "MC_ROLLRATE_P"  → "pid.roll.p"
        """

    # ── 能力声明 ────────────────────────────────────────────

    @abstractmethod
    def capabilities(self) -> Set[str]:
        """声明该平台支持的分析能力集合。

        标准能力标识:
            "pid"       - PID 阶跃响应分析
            "fft"       - FFT 频谱分析
            "filter"    - 滤波器传递函数分析
            "sysid"     - ARX 系统辨识
            "magfit"    - 磁力计校准分析
            "hardware"  - 硬件配置报告
            "quality"   - 日志质量评分

        Returns
        -------
        Set[str]
            支持的能力标识集合

        Examples
        --------
        ArduPilot:  {"pid", "fft", "filter", "sysid", "magfit", "hardware", "quality"}
        Betaflight: {"pid", "fft", "filter", "hardware", "quality"}
        PX4:        {"pid", "fft", "filter", "magfit", "hardware", "quality"}
        """

    # ── 平台特有分析器注册（可选）────────────────────────────

    def extra_analyzers(self) -> list:
        """返回平台特有的分析器实例列表。

        默认返回空列表。平台可以注册自己的分析器，
        如 Betaflight 的 FeedforwardAnalyzer。

        Returns
        -------
        list
            额外分析器实例列表，每个须实现 analyze(FlightData) 方法
        """
        return []
