"""
PX4 FFT 分析器扩展 — 占位实现（Phase 3）

PX4 使用 IMU_GYRO_CUTOFF，无等价 HarmonicNotchFilter。
Phase 3 实现。

对外接口（与其他平台一致）：
    format_notch_recommendation(generic_rec) -> Dict[str, Any]
    build_fft_recommendation_summary(generic_rec, params) -> str
"""

from __future__ import annotations

from typing import Any, Dict

_GENERIC_TO_PX4: Dict[str, str] = {
    "filter.gyro_lpf": "IMU_GYRO_CUTOFF",
}


def format_notch_recommendation(generic_rec: Dict[str, Any]) -> Dict[str, Any]:
    """将 generic 输出翻译为 PX4 参数名（仅 LPF，无陷波等价）。"""
    result: Dict[str, Any] = {}
    for k, v in generic_rec.items():
        px4_key = _GENERIC_TO_PX4.get(k)
        if px4_key:
            result[px4_key] = v
    return result


def build_fft_recommendation_summary(
    generic_rec: Dict[str, Any],
    current_params: Dict[str, float],
) -> str:
    """生成 PX4 格式建议摘要（param set 语法）。"""
    px4 = format_notch_recommendation(generic_rec)
    lines = []
    for key in ("IMU_GYRO_CUTOFF",):
        if key in px4:
            lines.append(f"  param set {key} {px4[key]}")
    if not lines:
        lines.append("  (PX4 notch filter not yet supported — Phase 3)")
    return "\n".join(lines)
