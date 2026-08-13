"""
Betaflight FFT 分析器扩展 — 陷波滤波器参数建议格式化。

BF 没有 INS_HNTCH 式的统一陷波器参数，
振动抑制依赖 RPM filter (rpm_filter_*) 和固定陷波 (gyro_notch*_hz)。
本模块将 generic FFT 建议翻译为 BF 参数名。

对外接口（与 ardupilot/fft_analyzer.py 一致）：
    format_notch_recommendation(generic_rec) -> Dict[str, Any]
    build_fft_recommendation_summary(generic_rec, params) -> str
"""

from __future__ import annotations

from typing import Any, Dict

# generic key → BF 参数名（尽量对应 Betaflight CLI set 命令）
_GENERIC_TO_BF: Dict[str, str] = {
    "filter.notch1.freq": "gyro_notch1_hz",
    "filter.notch1.bw": "gyro_notch1_cutoff",  # BF 用 cutoff 而非 BW
    "filter.notch1.enable": "gyro_notch1_enabled",
    "filter.gyro_lpf": "gyro_lowpass_hz",
    # BF 没有直接等价的 accel_lpf 参数
}


def format_notch_recommendation(generic_rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 FFTAnalyzer 的 generic 输出翻译为 Betaflight 参数名。

    注意：BF 的 gyro_notch cutoff 含义是截止频率（非带宽），
    转换：cutoff = freq - bw/2
    """
    result: Dict[str, Any] = {}
    freq = generic_rec.get("filter.notch1.freq", 0.0)
    bw = generic_rec.get("filter.notch1.bw", 0.0)

    for k, v in generic_rec.items():
        bf_key = _GENERIC_TO_BF.get(k)
        if bf_key is None:
            # 不在映射表里的 generic key 原样跳过（BF 无等价参数）
            continue
        # notch1.bw → cutoff 转换
        if k == "filter.notch1.bw" and freq > 0 and bw > 0:
            result[bf_key] = round(max(freq - bw / 2.0, 1.0), 1)
        else:
            result[bf_key] = v

    # BF: enabled 用整数 0/1
    if "gyro_notch1_enabled" in result:
        result["gyro_notch1_enabled"] = int(bool(result["gyro_notch1_enabled"]))

    return result


def build_fft_recommendation_summary(
    generic_rec: Dict[str, Any],
    current_params: Dict[str, float],
) -> str:
    """
    生成 Betaflight CLI set 格式的建议摘要。

    示例输出：
        set gyro_lowpass_hz = 80
        set gyro_notch1_hz = 95
        set gyro_notch1_cutoff = 71
    """
    bf = format_notch_recommendation(generic_rec)
    lines = []
    for key in ("gyro_lowpass_hz", "gyro_notch1_hz", "gyro_notch1_cutoff", "gyro_notch1_enabled"):
        if key in bf:
            lines.append(f"  set {key} = {bf[key]}")
    return "\n".join(lines) if lines else "  (no Betaflight-specific recommendations)"
