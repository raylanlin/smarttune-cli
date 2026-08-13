"""
smarttune/platform/step_response_protocol.py

Protocol 定义 — 约束各平台子模块必须导出的公共接口。

涵盖：
  - step_response_fft  (EstimateStepResponseFn / ComputeStepResponseForAxisFn)
  - filter_transfer    (DeriveFiltersFromParamsFn / ComputeFilterResponseFn)
  - fft_analyzer       (FormatNotchRecommendationFn)
  - hardware_report    (GenerateHardwareReportFn)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

import numpy as np


class EstimateStepResponseFn(Protocol):
    """estimate_step_response 函数签名约束。"""

    def __call__(
        self,
        target: np.ndarray,
        actual: np.ndarray,
        sample_rate: float,
        window_size: Optional[int] = None,
        step_duration_s: float = 0.5,
        min_target_amplitude: float = 20.0,
        cutfreq: float = 25.0,
    ) -> Dict[str, Any]:
        """
        估计阶跃响应。

        Returns
        -------
        Dict with required keys:
            time: np.ndarray           — 时间轴 (秒)
            step_response: np.ndarray  — 阶跃响应曲线
            valid_windows: int         — 有效窗口数
            total_windows: int         — 总窗口数
            method: str                — 算法标识 ("webtools_fft" | "wiener_fft" | ...)
        Optional keys:
            error: str                 — 错误描述（无有效窗口时）
            skipped_quality: int       — 被质量过滤跳过的窗口数
            window_size: int
            sample_rate: float
        """
        ...


class ComputeStepResponseForAxisFn(Protocol):
    """compute_step_response_for_axis 函数签名约束。"""

    def __call__(
        self,
        pid_data: Dict[str, np.ndarray],
        axis: str = "roll",
        imu_data: Optional[Dict[str, np.ndarray]] = None,
    ) -> Dict[str, Any]:
        """
        为指定轴计算阶跃响应。

        Parameters
        ----------
        pid_data : Dict[str, np.ndarray]
            必须包含 "Desired", "Actual", "time"。
            可选: "P", "I", "D", "FF"。
        axis : str
            "roll" | "pitch" | "yaw"
        imu_data : Dict, optional
            IMU 陀螺仪数据 (高采样率替代)。

        Returns
        -------
        Dict with required keys:
            axis: str
            time_s: list[float]
            step_response: list[float]
            info: Dict[str, Any]
        """
        ...


# ---------------------------------------------------------------------------
# filter_transfer Protocol
# ---------------------------------------------------------------------------


class DeriveFiltersFromParamsFn(Protocol):
    """derive_filters_from_params 函数签名约束。"""

    def __call__(self, params: dict) -> dict: ...


class ComputeFilterResponseFn(Protocol):
    """compute_filter_response 函数签名约束。"""

    def __call__(
        self,
        freqs: np.ndarray,
        sample_rate: float,
        gyro_filter_hz: float = 0.0,
        notch_params: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> tuple: ...  # (np.ndarray, np.ndarray)


class BuildFilterDisplayLinesFn(Protocol):
    """build_filter_display_lines 函数签名约束。"""

    def __call__(self, params: dict) -> list: ...  # List[str]


# ---------------------------------------------------------------------------
# fft_analyzer Protocol
# ---------------------------------------------------------------------------


class FormatNotchRecommendationFn(Protocol):
    """format_notch_recommendation 函数签名约束。"""

    def __call__(self, generic_rec: dict) -> dict: ...


# ---------------------------------------------------------------------------
# hardware_report Protocol
# ---------------------------------------------------------------------------


class GenerateHardwareReportFn(Protocol):
    """generate_hardware_report 函数签名约束。"""

    def __call__(self, params: dict, flight_data: Optional[object] = None) -> dict: ...
