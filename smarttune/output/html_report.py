"""
html_report.py — 生成带嵌入图表的 HTML 分析报告

支持：
- 所有分析结果（PID / FFT / 磁力计）嵌入单一 HTML 文件
- 图表以 base64 data URL 内嵌，无需外部文件依赖
- 响应式布局，可直接用浏览器打开分享
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _b64_img(fig) -> str:
    """将 matplotlib figure 转为 base64 PNG data URL。"""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    data = base64.b64encode(buf.read()).decode("ascii")
    return f"data:image/png;base64,{data}"


def _assessment_color(level: str) -> str:
    """评估等级 → CSS 颜色。"""
    return {
        "EXCELLENT": "#22c55e",
        "GOOD":      "#86efac",
        "ACCEPTABLE":"#fbbf24",
        "MARGINAL":  "#f97316",
        "POOR":      "#ef4444",
        "BAD":       "#ef4444",
        "NO_DATA":   "#94a3b8",
    }.get(level.upper() if level else "", "#94a3b8")


def _vib_color(level: str) -> str:
    return {
        "EXCELLENT": "#22c55e",
        "GOOD":      "#86efac",
        "MARGINAL":  "#fbbf24",
        "SEVERE":    "#f97316",
        "CRITICAL":  "#ef4444",
    }.get(level.upper() if level else "", "#94a3b8")


# ---------------------------------------------------------------------------
# HTML 生成主函数
# ---------------------------------------------------------------------------

def generate_html_report(
    pid_results=None,
    fft_results=None,
    magfit_results=None,
    filter_results=None,
    sysid_results=None,
    hardware_report=None,
    log_path: str = "",
    pid_plot_fig=None,
    fft_plot_fig=None,
) -> str:
    """
    生成完整的 HTML 分析报告字符串。

    Parameters
    ----------
    pid_results : PIDAnalysisResult | Dict | None
        PID 分析结果（支持新 dataclass 和旧 dict 格式）。
    fft_results : FFTANalysisResult | Dict | None
        FFT 振动分析结果。
    magfit_results : MagFitResult | Dict | None
        磁力计分析结果。
    filter_results : FilterAnalysisResult | None
        滤波器传递函数分析结果。
    sysid_results : List[SysIDResult] | None
        系统辨识结果列表（每轴一条）。
    hardware_report : HardwareReport | None
        硬件配置报告。
    log_path : str
        原始日志文件路径（用于显示）。
    pid_plot_fig, fft_plot_fig :
        matplotlib Figure 对象，若提供则内嵌为图片。

    Returns
    -------
    str
        完整 HTML 字符串。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_name = Path(log_path).name if log_path else "—"

    # ── 图表（base64）────────────────────────────────────────────────────
    pid_img_tag = ""
    fft_img_tag = ""

    if pid_plot_fig is not None:
        try:
            pid_img_tag = f'<img src="{_b64_img(pid_plot_fig)}" class="chart-img" alt="PID Step Response">'
        except Exception:
            pass

    if fft_plot_fig is not None:
        try:
            fft_img_tag = f'<img src="{_b64_img(fft_plot_fig)}" class="chart-img" alt="FFT Spectrum">'
        except Exception:
            pass

    # ── PID 部分 HTML ─────────────────────────────────────────────────────
    pid_html = _build_pid_html(pid_results)

    # ── FFT 部分 HTML ─────────────────────────────────────────────────────
    fft_html = _build_fft_html(fft_results)

    # ── 磁力计部分 HTML ───────────────────────────────────────────────────
    magfit_html = _build_magfit_html(magfit_results)

    # ── 滤波器部分 HTML ───────────────────────────────────────────────────
    filter_html = _build_filter_html(filter_results)

    # ── 系统辨识部分 HTML ─────────────────────────────────────────────────
    sysid_html = _build_sysid_html(sysid_results)

    # ── 硬件报告部分 HTML ─────────────────────────────────────────────────
    hardware_html = _build_hardware_html(hardware_report)

    # ── 组装 ──────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ArduPilot 调参分析报告 — {log_name}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #0f172a; color: #e2e8f0; line-height: 1.6; }}
    .container {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px; }}
    header {{ background: linear-gradient(135deg, #1e3a5f 0%, #0f2942 100%);
              border-radius: 12px; padding: 28px 32px; margin-bottom: 28px;
              border: 1px solid #1e40af; }}
    header h1 {{ font-size: 1.7rem; color: #93c5fd; font-weight: 700; }}
    header .meta {{ color: #64748b; font-size: 0.85rem; margin-top: 6px; }}
    .section {{ background: #1e293b; border-radius: 10px; padding: 24px;
               margin-bottom: 20px; border: 1px solid #334155; }}
    .section-title {{ font-size: 1.1rem; font-weight: 600; color: #93c5fd;
                      border-bottom: 1px solid #334155; padding-bottom: 10px;
                      margin-bottom: 16px; }}
    .axis-block {{ background: #0f172a; border-radius: 8px; padding: 16px;
                   margin-bottom: 12px; border: 1px solid #1e293b; }}
    .axis-title {{ font-size: 1rem; font-weight: 600; color: #e2e8f0;
                   margin-bottom: 10px; }}
    .badge {{ display: inline-block; padding: 2px 10px; border-radius: 9999px;
              font-size: 0.8rem; font-weight: 600; color: #0f172a; }}
    .metrics {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 10px 0; }}
    .metric {{ background: #1e293b; border-radius: 6px; padding: 8px 12px;
               min-width: 120px; }}
    .metric-label {{ font-size: 0.72rem; color: #64748b; text-transform: uppercase;
                     letter-spacing: 0.05em; }}
    .metric-value {{ font-size: 1.05rem; font-weight: 600; color: #e2e8f0; }}
    .rec-list {{ list-style: none; margin-top: 10px; }}
    .rec-item {{ background: #0f172a; border-left: 3px solid #3b82f6;
                 padding: 10px 14px; margin-bottom: 8px; border-radius: 0 6px 6px 0; }}
    .rec-param {{ font-family: monospace; font-size: 0.9rem; color: #60a5fa; }}
    .rec-arrow {{ color: #94a3b8; }}
    .rec-reason {{ font-size: 0.82rem; color: #94a3b8; margin-top: 3px; }}
    .confidence-high {{ color: #22c55e; }} .confidence-medium {{ color: #fbbf24; }}
    .confidence-low {{ color: #ef4444; }}
    .peak-table {{ width: 100%; border-collapse: collapse; font-size: 0.87rem; }}
    .peak-table th {{ background: #0f172a; padding: 7px 12px; text-align: left;
                      color: #64748b; font-weight: 500; }}
    .peak-table td {{ padding: 7px 12px; border-top: 1px solid #1e293b; }}
    .param-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    .param-table td {{ padding: 6px 12px; border-top: 1px solid #1e293b; }}
    .param-table .name {{ font-family: monospace; color: #93c5fd; }}
    .param-table .val {{ color: #e2e8f0; text-align: right; }}
    .chart-img {{ width: 100%; border-radius: 8px; margin-top: 14px;
                  border: 1px solid #334155; }}
    .safety-block {{ background: #1c1917; border: 1px solid #78350f;
                     border-radius: 8px; padding: 14px 18px; margin-top: 16px; }}
    .safety-block .s-title {{ color: #fbbf24; font-weight: 600; margin-bottom: 8px; }}
    .safety-block ul {{ padding-left: 18px; color: #fbbf24; font-size: 0.88rem; }}
    footer {{ text-align: center; color: #334155; font-size: 0.78rem; margin-top: 32px; }}
  </style>
</head>
<body>
<div class="container">
  <header>
    <h1>🛸 ArduPilot 调参分析报告</h1>
    <div class="meta">日志文件: {log_name} &nbsp;|&nbsp; 生成时间: {now} &nbsp;|&nbsp; 工具: SmartTune CLI</div>
  </header>

  {pid_html}
  {fft_html}
  {magfit_html}
  {filter_html}
  {sysid_html}
  {hardware_html}

  {"" if not pid_img_tag else f'<div class="section"><div class="section-title">📈 PID 阶跃响应图表</div>{pid_img_tag}</div>'}
  {"" if not fft_img_tag else f'<div class="section"><div class="section-title">📊 FFT 频谱图表</div>{fft_img_tag}</div>'}

  <footer>Generated by SmartTune CLI · Multi-Platform Flight Log Analyzer</footer>
</div>
</body>
</html>"""


def _build_pid_html(results) -> str:
    if not results:
        return '<div class="section"><div class="section-title">⚙️ PID Tuning Recommendations</div><p style="color:#64748b">No data</p></div>'

    # ── Adapt both PIDAnalysisResult (dataclass) and old dict format ──
    axes_data: dict = {}

    # Check if it's the new PIDAnalysisResult dataclass
    if hasattr(results, 'axes') and hasattr(results, 'overall_assessment'):
        # New dataclass format: PIDAnalysisResult
        for axis_name, ax_result in results.axes.items():
            metrics = ax_result.metrics.to_dict() if hasattr(ax_result.metrics, 'to_dict') else {}
            recs = []
            for r in ax_result.recommendations:
                recs.append({
                    "param": r.param.generic_name if hasattr(r.param, 'generic_name') else str(r.param),
                    "current": r.current,
                    "recommended": r.suggested,
                    "reason": r.reason,
                    "confidence": r.confidence.value if hasattr(r.confidence, 'value') else str(r.confidence),
                })
            axes_data[axis_name] = {
                "assessment": ax_result.assessment.value if hasattr(ax_result.assessment, 'value') else str(ax_result.assessment),
                "metrics": metrics,
                "recommendations": recs,
                "step_count": ax_result.step_count,
            }
    elif isinstance(results, dict):
        # Old dict format
        if "axis" in results:
            axes_data[results["axis"]] = results
        else:
            for ax in ["roll", "pitch", "yaw"]:
                if ax in results:
                    axes_data[ax] = results[ax]

    blocks = []
    for ax, data in axes_data.items():
        assessment = data.get("assessment", "UNKNOWN")
        color = _assessment_color(assessment)
        metrics = data.get("metrics", {})
        recs = data.get("recommendations", [])

        # metrics display
        m_parts = []
        for key, label, fmt in [
            ("rise_time_ms", "Rise Time", "{:.0f} ms"),
            ("overshoot_percent", "Overshoot", "{:.1f} %"),
            ("settling_time_ms", "Settling", "{:.0f} ms"),
            ("oscillation_count", "Oscillations", "{:.0f}"),
        ]:
            val = metrics.get(key, -1)
            if val >= 0:
                m_parts.append(f'<div class="metric"><div class="metric-label">{label}</div><div class="metric-value">{fmt.format(val)}</div></div>')

        metrics_html = f'<div class="metrics">{"".join(m_parts)}</div>'

        # recommendations
        rec_items = []
        for rec in recs:
            param = rec.get("param", "?")
            cur = rec.get("current", 0)
            recom = rec.get("recommended", rec.get("suggested", cur))
            reason = rec.get("reason", "")
            conf = rec.get("confidence", "medium")
            direction = "↑" if recom > cur else "↓"
            if cur > 0:
                pct = abs(recom - cur) / cur * 100
                delta = f"{direction}{pct:.1f}%"
            else:
                delta = direction
            conf_color = _assessment_color("EXCELLENT" if conf == "high" else ("MARGINAL" if conf == "medium" else "POOR"))
            rec_items.append(
                f'<li class="rec-item"><span class="rec-param">{param}</span>'
                f'<span class="rec-arrow"> {cur:.4g} → {recom:.4g} </span>'
                f'<span class="badge" style="background:{conf_color}">{delta}</span>'
                f'<div class="rec-reason">{reason} · Confidence: <span class="confidence-{conf}">{conf}</span></div></li>'
            )
        recs_html = (
            f'<ul class="rec-list">{"".join(rec_items)}</ul>' if rec_items
            else '<p style="color:#22c55e;font-size:0.87rem;margin-top:8px">✓ Parameters are within target — no adjustments needed</p>'
        )

        step_count = data.get("step_count", 0)
        blocks.append(f"""
  <div class="axis-block">
    <div class="axis-title">
      {ax.capitalize()} Axis
      <span class="badge" style="background:{color};margin-left:8px">{assessment}</span>
      <span style="color:#64748b;font-size:0.8rem;margin-left:10px">· {step_count} valid step windows</span>
    </div>
    {metrics_html}
    {recs_html}
  </div>""")

    return f'<div class="section"><div class="section-title">⚙️ PID Tuning Recommendations</div>{"".join(blocks)}</div>'


def _build_fft_html(results: Optional[Dict[str, Any]]) -> str:
    if not results:
        return '<div class="section"><div class="section-title">📊 FFT 振动分析</div><p style="color:#64748b">暂无数据</p></div>'

    vib_level = results.get("vibration_level", "?")
    vib_val = results.get("vibration_value_mss", 0)
    color = _vib_color(vib_level)

    peaks = results.get("peak_frequencies", [])
    peak_rows = ""
    for p in peaks:
        src = p.get("source", "unknown")
        is_h = "✓" if p.get("is_harmonic") else "—"
        mag = p.get("magnitude_db", p.get("amplitude_dbfs", p.get("magnitude", p.get("amplitude", 0))))
        freq = p.get("freq", p.get("frequency_hz", 0))
        peak_rows += f"<tr><td>{freq:.1f} Hz</td><td>{mag:.1f} dBFS</td><td>{src}</td><td>{is_h}</td></tr>"

    peaks_html = ""
    if peaks:
        peaks_html = f"""
    <div style="margin-top:14px">
      <div style="color:#64748b;font-size:0.82rem;margin-bottom:6px">频率峰值</div>
      <table class="peak-table">
        <thead><tr><th>频率</th><th>幅值</th><th>来源</th><th>谐波</th></tr></thead>
        <tbody>{peak_rows}</tbody>
      </table>
    </div>"""

    recs = results.get("recommendations", {})
    rec_params = ""
    for k in ["INS_HNTCH_ENABLE","INS_HNTCH_MODE","INS_HNTCH_FREQ","INS_HNTCH_BW",
              "INS_HNTCH_ATT","INS_HNTCH_HMC","INS_GYRO_FILTER"]:
        if k in recs:
            rec_params += f'<tr><td class="name">{k}</td><td class="val">{recs[k]}</td></tr>'

    notch_html = ""
    if rec_params:
        notch_html = f"""
    <div style="margin-top:14px">
      <div style="color:#64748b;font-size:0.82rem;margin-bottom:6px">陷波滤波器建议参数</div>
      <table class="param-table"><tbody>{rec_params}</tbody></table>
    </div>"""

    warnings = results.get("warnings", [])
    warn_html = ""
    if warnings:
        items = "".join(f"<li>{w}</li>" for w in warnings)
        warn_html = f'<div class="safety-block"><div class="s-title">⚠ 警告</div><ul>{items}</ul></div>'

    return f"""<div class="section">
  <div class="section-title">📊 FFT 振动分析</div>
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <div>
      <span style="color:#94a3b8;font-size:0.85rem">振动等级</span><br>
      <span class="badge" style="background:{color};font-size:1rem;padding:4px 16px">{vib_level}</span>
    </div>
    <div>
      <span style="color:#94a3b8;font-size:0.85rem">振动值</span><br>
      <span style="font-size:1.2rem;font-weight:600">{vib_val:.2f} m/s²</span>
    </div>
  </div>
  {peaks_html}
  {notch_html}
  {warn_html}
</div>"""


def _build_magfit_html(results) -> str:
    if not results:
        return '<div class="section"><div class="section-title">🧭 Magnetometer Calibration</div><p style="color:#64748b">No data</p></div>'

    fitness = getattr(results, "fitness_mgauss", None)
    if fitness is None and hasattr(results, "fitness_mGauss"):
        fitness = results.fitness_mGauss
    if fitness is None and hasattr(results, "get"):
        fitness = results.get("fitness_mgauss", results.get("fitness_mGauss"))
    assessment = getattr(results, "assessment", None) or (results.get("assessment") if hasattr(results, "get") else None) or "?"
    if hasattr(assessment, 'value'):
        assessment = assessment.value
    color = _assessment_color(assessment)

    fitness_html = ""
    if fitness is not None:
        fitness_html = f"""
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:12px">
    <div>
      <span style="color:#94a3b8;font-size:0.85rem">Assessment</span><br>
      <span class="badge" style="background:{color};font-size:1rem;padding:4px 16px">{assessment}</span>
    </div>
    <div>
      <span style="color:#94a3b8;font-size:0.85rem">Fitness</span><br>
      <span style="font-size:1.2rem;font-weight:600">{fitness:.2f} mGauss</span>
    </div>
  </div>"""

    # offset params — support multiple formats
    ofs_rows = ""
    if hasattr(results, "ofs") and results.ofs:
        for label, val in zip(["OFS_X", "OFS_Y", "OFS_Z"], results.ofs):
            ofs_rows += f'<tr><td class="name">COMPASS_{label}</td><td class="val">{val:.2f}</td></tr>'
    elif hasattr(results, "offsets") and results.offsets:
        for axis, val in results.offsets.items():
            ofs_rows += f'<tr><td class="name">COMPASS_OFS_{axis.upper()}</td><td class="val">{val:.2f}</td></tr>'
    elif hasattr(results, "get"):
        params = results.get("parameters", results.get("offsets", {}))
        if isinstance(params, dict):
            for k, v in params.items():
                ofs_rows += f'<tr><td class="name">{k}</td><td class="val">{v:.2f}</td></tr>'

    params_html = ""
    if ofs_rows:
        params_html = f'<table class="param-table"><tbody>{ofs_rows}</tbody></table>'

    return f'<div class="section"><div class="section-title">🧭 Magnetometer Calibration</div>{fitness_html}{params_html}</div>'


def _build_filter_html(results) -> str:
    """滤波器传递函数分析 section。"""
    if not results:
        return ""

    # support both dataclass and dict
    cutoff = getattr(results, "cutoff_3db_hz", None)
    if cutoff is None and hasattr(results, "get"):
        cutoff = results.get("cutoff_3db_hz")
    config = (getattr(results, "config_summary", "") or
              (results.get("config_summary", "") if hasattr(results, "get") else ""))
    recs = getattr(results, "recommendations", [])
    if not recs and hasattr(results, "get"):
        recs = results.get("recommendations", [])

    cutoff_html = ""
    if cutoff is not None:
        cutoff_html = f"""
    <div>
      <span style="color:#94a3b8;font-size:0.85rem">-3dB Cutoff</span><br>
      <span style="font-size:1.2rem;font-weight:600">{cutoff:.1f} Hz</span>
    </div>"""

    config_html = ""
    if config:
        config_html = f"""
    <div>
      <span style="color:#94a3b8;font-size:0.85rem">Filter Chain</span><br>
      <span style="color:#93c5fd;font-family:monospace;font-size:0.85rem">{config}</span>
    </div>"""

    # filter recommendations
    rec_html = ""
    if recs:
        items = ""
        for r in recs:
            param = r.param.generic_name if hasattr(r, "param") and hasattr(r.param, "generic_name") else (r.get("param", "?") if hasattr(r, "get") else str(r))
            cur = r.current if hasattr(r, "current") else r.get("current", 0)
            sug = r.suggested if hasattr(r, "suggested") else r.get("suggested", r.get("recommended", cur))
            reason = r.reason if hasattr(r, "reason") else r.get("reason", "")
            conf = (r.confidence.value if hasattr(r.confidence, "value")
                    else (r.get("confidence", "medium") if hasattr(r, "get") else "medium"))
            direction = "↑" if sug > cur else "↓"
            if cur > 0:
                delta = f"{direction}{abs(sug - cur) / cur * 100:.1f}%"
            else:
                delta = direction
            conf_color = _assessment_color("EXCELLENT" if conf == "high" else ("MARGINAL" if conf == "medium" else "POOR"))
            items += (
                f'<li class="rec-item"><span class="rec-param">{param}</span>'
                f'<span class="rec-arrow"> {cur:.4g} → {sug:.4g} </span>'
                f'<span class="badge" style="background:{conf_color}">{delta}</span>'
                f'<div class="rec-reason">{reason} · Confidence: <span class="confidence-{conf}">{conf}</span></div></li>'
            )
        rec_html = f'<ul class="rec-list">{items}</ul>'

    return f"""<div class="section">
  <div class="section-title">🔧 滤波器传递函数分析</div>
  <div style="display:flex;align-items:center;gap:24px;flex-wrap:wrap">
    {cutoff_html}
    {config_html}
  </div>
  {rec_html}
</div>"""


def _build_sysid_html(results) -> str:
    """系统辨识（ARX 模型）section。"""
    if not results:
        return ""

    # results may be a single SysIDResult or a list
    items = results if isinstance(results, list) else [results]
    if not items:
        return ""

    rows = ""
    for r in items:
        axis = getattr(r, "axis", "unknown")
        if hasattr(r, "get") and not hasattr(r, "axis"):
            axis = r.get("axis", "?")
        nf = getattr(r, "natural_freq_hz", 0) or (r.get("natural_freq_hz", 0) if hasattr(r, "get") else 0)
        dr = getattr(r, "damping_ratio", 0) or (r.get("damping_ratio", 0) if hasattr(r, "get") else 0)
        bw = getattr(r, "bandwidth_hz", 0) or (r.get("bandwidth_hz", 0) if hasattr(r, "get") else 0)
        order = getattr(r, "model_order", "") or (r.get("model_order", "") if hasattr(r, "get") else "")
        fit = getattr(r, "fit_quality", 0) or (r.get("fit_quality", 0) if hasattr(r, "get") else 0)
        fit_color = _assessment_color("EXCELLENT" if fit > 0.9 else ("GOOD" if fit > 0.7 else ("MARGINAL" if fit > 0.5 else "POOR")))
        damping_assess = ("Under-damped" if dr < 0.7 else ("Good" if dr < 1.0 else "Over-damped"))
        if dr == 0:
            damping_assess = "—"
        rows += f"""<tr>
    <td style="font-weight:600;color:#e2e8f0">{axis.capitalize()}</td>
    <td>{nf:.1f} Hz</td>
    <td>{dr:.3f} <span style="color:#64748b;font-size:0.8rem">{damping_assess}</span></td>
    <td>{bw:.1f} Hz</td>
    <td style="font-family:monospace;color:#93c5fd;font-size:0.82rem">{order}</td>
    <td><span class="badge" style="background:{fit_color}">{fit:.3f}</span></td>
  </tr>"""

    return f"""<div class="section">
  <div class="section-title">🧪 系统辨识（ARX 模型）</div>
  <p style="color:#64748b;font-size:0.82rem;margin-bottom:12px">
    System identification via ARX model fitting. Natural frequency and damping ratio
    are used to validate whether current PID gains match the airframe dynamics.
  </p>
  <table class="peak-table">
    <thead><tr>
      <th>Axis</th><th>Natural Freq</th><th>Damping</th><th>Bandwidth</th><th>Model</th><th>Fit (R²)</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


def _build_hardware_html(results) -> str:
    """硬件配置报告 section。"""
    if not results:
        return ""

    # support both dataclass and dict
    firmware = getattr(results, "firmware_version", "") or (results.get("firmware_version", "") if hasattr(results, "get") else "")
    board = getattr(results, "board_name", "") or (results.get("board_name", "") if hasattr(results, "get") else "")

    imus = getattr(results, "imu_configs", None)
    if imus is None and hasattr(results, "get"):
        imus = results.get("imu_configs", [])
    imus = imus or []

    compass = getattr(results, "compass_configs", None)
    if compass is None and hasattr(results, "get"):
        compass = results.get("compass_configs", [])
    compass = compass or []

    pid_params = getattr(results, "pid_params", None)
    if pid_params is None and hasattr(results, "get"):
        pid_params = results.get("pid_params", {})
    pid_params = pid_params or {}

    issues = getattr(results, "integrity_issues", None)
    if issues is None and hasattr(results, "get"):
        issues = results.get("integrity_issues", [])
    issues = issues or []

    # Firmware + Board header
    header_parts = []
    if firmware:
        header_parts.append(f'<span style="color:#93c5fd;font-family:monospace">{firmware}</span>')
    if board:
        header_parts.append(f'<span style="color:#64748b">on</span> <span style="color:#e2e8f0">{board}</span>')

    # IMU table
    imu_rows = ""
    if imus:
        for imu in imus:
            name = imu.get("name", "?") if isinstance(imu, dict) else getattr(imu, "name", "?")
            rate = imu.get("sample_rate_hz", "?") if isinstance(imu, dict) else getattr(imu, "sample_rate_hz", "?")
            health = imu.get("health", "?") if isinstance(imu, dict) else getattr(imu, "health", "?")
            health_color = "#22c55e" if str(health).upper() in ("OK", "GOOD", "HEALTHY") else "#fbbf24"
            imu_rows += f"""<tr>
    <td style="font-family:monospace;color:#93c5fd">{name}</td>
    <td>{rate} Hz</td>
    <td><span style="color:{health_color}">{health}</span></td>
  </tr>"""

    imu_section = ""
    if imu_rows:
        imu_section = f"""
  <div style="margin-top:10px">
    <div style="color:#64748b;font-size:0.82rem;margin-bottom:6px">IMU / Gyro Configuration</div>
    <table class="peak-table">
      <thead><tr><th>Sensor</th><th>Sample Rate</th><th>Health</th></tr></thead>
      <tbody>{imu_rows}</tbody>
    </table>
  </div>"""

    # Compass table
    comp_rows = ""
    if compass:
        for c in compass:
            name = c.get("name", "?") if isinstance(c, dict) else getattr(c, "name", "?")
            ext = "✓" if (c.get("external", False) if isinstance(c, dict) else getattr(c, "external", False)) else "—"
            comp_rows += f'<tr><td style="font-family:monospace;color:#93c5fd">{name}</td><td>{ext}</td></tr>'

    comp_section = ""
    if comp_rows:
        comp_section = f"""
  <div style="margin-top:10px">
    <div style="color:#64748b;font-size:0.82rem;margin-bottom:6px">Compass / Magnetometer</div>
    <table class="peak-table">
      <thead><tr><th>Sensor</th><th>External</th></tr></thead>
      <tbody>{comp_rows}</tbody>
    </table>
  </div>"""

    # PID params summary
    pid_rows = ""
    if pid_params:
        for axis in ["roll", "pitch", "yaw"]:
            if axis in pid_params:
                p = pid_params[axis].get("P", 0) if isinstance(pid_params[axis], dict) else getattr(pid_params[axis], "P", 0)
                i = pid_params[axis].get("I", 0) if isinstance(pid_params[axis], dict) else getattr(pid_params[axis], "I", 0)
                d = pid_params[axis].get("D", 0) if isinstance(pid_params[axis], dict) else getattr(pid_params[axis], "D", 0)
                pid_rows += f'<tr><td style="font-weight:600;color:#e2e8f0">{axis.capitalize()}</td><td>{p:.4f}</td><td>{i:.4f}</td><td>{d:.4f}</td></tr>'
        if pid_rows:
            pid_rows = f"""
  <div style="margin-top:10px">
    <div style="color:#64748b;font-size:0.82rem;margin-bottom:6px">PID Gains</div>
    <table class="peak-table">
      <thead><tr><th>Axis</th><th>P</th><th>I</th><th>D</th></tr></thead>
      <tbody>{pid_rows}</tbody>
    </table>
  </div>"""

    # Integrity issues
    issue_section = ""
    if issues:
        items = "".join(f"<li>{w}</li>" for w in issues)
        issue_section = f'<div class="safety-block"><div class="s-title">⚠ 完整性警告</div><ul>{items}</ul></div>'

    header_str = " · ".join(header_parts) if header_parts else '<span style="color:#64748b">No hardware info</span>'

    return f"""<div class="section">
  <div class="section-title">💻 硬件配置</div>
  <div style="margin-bottom:10px">
    {header_str}
  </div>
  {imu_section}
  {comp_section}
  {pid_rows}
  {issue_section}
</div>"""


def save_html_report(
    html: str,
    output_path: str,
) -> None:
    """将 HTML 字符串写入文件。"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
