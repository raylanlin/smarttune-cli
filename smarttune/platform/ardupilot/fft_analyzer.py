"""
ArduPilot FFT 分析器扩展 — 陷波滤波器参数建议格式化。

fft_analyzer.FFTAnalyzer.recommend_notch_filter() 返回 generic key
（filter.notch1.enable / filter.gyro_lpf 等）。
本模块负责将 generic 结果翻译为 ArduPilot 原生参数名，
并补充 AP 专用字段（INS_HNTCH_HMC / INS_GYRO_FILTER）。

对外接口：
    format_notch_recommendation(generic_rec) -> Dict[str, Any]
        将 generic key dict 转为 AP INS_HNTCH_* 参数名 dict

    build_fft_recommendation_summary(generic_rec, params) -> str
        生成可在 CLI 打印的单行摘要
"""

from __future__ import annotations

from typing import Any, Dict

# generic key → AP 参数名
_GENERIC_TO_AP: Dict[str, str] = {
    "filter.notch1.enable": "INS_HNTCH_ENABLE",
    "filter.notch1.mode": "INS_HNTCH_MODE",
    "filter.notch1.freq": "INS_HNTCH_FREQ",
    "filter.notch1.bw": "INS_HNTCH_BW",
    "filter.notch1.att": "INS_HNTCH_ATT",
    "filter.notch1.ref": "INS_HNTCH_REF",
    "filter.notch1.hmc": "INS_HNTCH_HMNCS",
    "filter.gyro_lpf": "INS_GYRO_FILTER",
    "filter.accel_lpf": "INS_ACCEL_FILTER",
}


def format_notch_recommendation(generic_rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 FFTAnalyzer.recommend_notch_filter() 的 generic key 输出
    翻译为 ArduPilot INS_HNTCH_* 原生参数名。

    Parameters
    ----------
    generic_rec : Dict
        recommend_notch_filter() 返回的 dict，key 为 generic name。

    Returns
    -------
    Dict[str, Any]
        同样内容，key 替换为 AP 参数名；未知 key 原样保留。
    """
    result: Dict[str, Any] = {}
    for k, v in generic_rec.items():
        ap_key = _GENERIC_TO_AP.get(k, k)
        result[ap_key] = v
    return result


def build_fft_recommendation_summary(
    generic_rec: Dict[str, Any],
    current_params: Dict[str, float],
) -> str:
    """
    生成 ArduPilot 格式的 CLI 单行建议摘要。

    示例输出：
        INS_HNTCH_ENABLE=1 FREQ=95.0 BW=47.5 ATT=40 MODE=2 HMNCS=1
    """
    ap = format_notch_recommendation(generic_rec)
    parts = []
    for key in (
        "INS_HNTCH_ENABLE",
        "INS_HNTCH_MODE",
        "INS_HNTCH_FREQ",
        "INS_HNTCH_BW",
        "INS_HNTCH_ATT",
        "INS_HNTCH_REF",
        "INS_HNTCH_HMNCS",
        "INS_GYRO_FILTER",
    ):
        if key in ap:
            val = ap[key]
            short = key.replace("INS_HNTCH_", "").replace("INS_", "")
            parts.append(f"{short}={val}")
    return "  " + "  ".join(parts)
