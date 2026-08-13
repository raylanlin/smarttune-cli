"""
smarttune/errors.py

SmartTune 统一异常体系。

错误码约定:
  E10xx  文件/日志相关
  E20xx  解析相关
  E30xx  数据不足相关
  E40xx  参数/输入相关
  E50xx  分析模块相关
  E90xx  平台/功能未实现
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class SmartTuneError(Exception):
    """所有 SmartTune 自定义异常的基类。"""

    code = "E0000"
    message = "Unknown error"
    hint = ""

    def __init__(
        self,
        message: str | None = None,
        hint: str | None = None,
        code: str | None = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.hint = hint or self.__class__.hint
        self.code = code or self.__class__.code
        super().__init__(self.message)

    def rich_render(self) -> Panel:
        lines = [Text(self.message, style="bold red")]
        if self.hint:
            lines.append(Text(f"\n💡 Hint: {self.hint}", style="dim"))
        lines.append(Text(f"\n[Error] {self.code}", style="dim"))
        return Panel(
            "\n".join(str(l) for l in lines),
            title="[red]Error[/red]",
            border_style="red",
            expand=False,
        )

    def print(self) -> None:
        Console(stderr=True).print(self.rich_render())


# ---------------------------------------------------------------------------
# E10xx - 文件/日志相关
# ---------------------------------------------------------------------------


class LogFileError(SmartTuneError):
    code = "E1000"
    message = "Log file operation failed"


class LogFileNotFoundError(LogFileError):
    code = "E1001"
    message = "Log file not found"
    hint = "Check the file path and ensure the file exists."


class LogFileCorruptError(LogFileError):
    code = "E1002"
    message = "Log file corrupt or incompatible"
    hint = "Ensure the file is a valid flight log and was not truncated during writing."


# ---------------------------------------------------------------------------
# E20xx - 解析相关
# ---------------------------------------------------------------------------


class ParseError(SmartTuneError):
    code = "E2000"
    message = "Log parse failed"


class LogFormatError(ParseError):
    code = "E2001"
    message = "Unrecognized log format"
    hint = "SmartTune supports ArduPilot (.bin), Betaflight (.bbl), and PX4 (.ulg) logs."


class LogVersionError(ParseError):
    code = "E2002"
    message = "Log firmware version may be incompatible"


class ParseIncompleteError(ParseError):
    code = "E2003"
    message = "Log parse incomplete — recording may have been interrupted"


# ---------------------------------------------------------------------------
# E30xx - 数据不足
# ---------------------------------------------------------------------------


class InsufficientDataError(SmartTuneError):
    code = "E3000"
    message = "Insufficient data for analysis"


class InsufficientIMUDataError(InsufficientDataError):
    code = "E3001"
    message = "Insufficient IMU data in log"


class InsufficientPIDDataError(InsufficientDataError):
    code = "E3002"
    message = "Insufficient PID data in log"


class InsufficientCompassDataError(InsufficientDataError):
    code = "E3004"
    message = "Insufficient compass data in log"


class InsufficientAttitudeDataError(InsufficientDataError):
    code = "E3005"
    message = "Insufficient attitude data in log"


# ---------------------------------------------------------------------------
# E40xx - 参数/输入
# ---------------------------------------------------------------------------


class InvalidParameterError(SmartTuneError):
    code = "E4000"
    message = "Invalid parameter"


class InvalidAxisError(InvalidParameterError):
    code = "E4001"
    message = "Invalid axis — expected roll, pitch, or yaw"


class UnsupportedPlatformError(InvalidParameterError):
    code = "E4010"
    message = "Unsupported platform"


# ---------------------------------------------------------------------------
# E50xx - 分析模块
# ---------------------------------------------------------------------------


class AnalysisError(SmartTuneError):
    code = "E5000"
    message = "Analysis failed"


class FFTAnalysisError(AnalysisError):
    code = "E5001"
    message = "FFT analysis failed"


class PIDAnalysisError(AnalysisError):
    code = "E5002"
    message = "PID analysis failed"


class MAGFitError(AnalysisError):
    code = "E5003"
    message = "Magnetometer calibration analysis failed"


class CapabilityNotSupportedError(AnalysisError):
    code = "E5010"
    message = "This analysis is not supported on the current platform"
