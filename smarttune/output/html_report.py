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
    pid_results: Optional[Dict[str, Any]],
    fft_results: Optional[Dict[str, Any]],
    magfit_results: Optional[Dict[str, Any]],
    log_path: str = "",
    pid_plot_fig=None,
    fft_plot_fig=None,
) -> str:
    """
    生成完整的 HTML 分析报告字符串。

    Parameters
    ----------
    pid_results, fft_results, magfit_results : Optional[Dict]
        各模块分析结果。
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

  {"" if not pid_img_tag else f'<div class="section"><div class="section-title">📈 PID 阶跃响应图表</div>{pid_img_tag}</div>'}
  {"" if not fft_img_tag else f'<div class="section"><div class="section-title">📊 FFT 频谱图表</div>{fft_img_tag}</div>'}

  <footer>Generated by SmartTune CLI · Multi-Platform Flight Log Analyzer</footer>
</div>
</body>
</html>"""


def _build_pid_html(results: Optional[Dict[str, Any]]) -> str:
    if not results:
        return '<div class="section"><div class="section-title">⚙️ PID 调参建议</div><p style="color:#64748b">暂无数据</p></div>'

    axes_data: Dict[str, Dict] = {}
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
            ("rise_time_ms", "上升时间", "{:.0f} ms"),
            ("overshoot_percent", "超调", "{:.1f} %"),
            ("settling_time_ms", "稳定时间", "{:.0f} ms"),
            ("oscillation_count", "振荡次数", "{:.0f} 次"),
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
            recom = rec.get("recommended", cur)
            reason = rec.get("reason", "")
            conf = rec.get("confidence", "medium")
            direction = "↑" if recom > cur else "↓"
            if cur > 0:
                pct = abs(recom - cur) / cur * 100
                delta = f"{direction}{pct:.1f}%"
            else:
                delta = direction
            rec_items.append(
                f'<li class="rec-item"><span class="rec-param">{param}</span><span class="rec-arrow"> {cur:.4g} → {recom:.4g} </span><span class="badge" style="background:{_assessment_color(conf.upper() if conf == "high" else "MARGINAL" if conf == "medium" else "POOR")}">{delta}</span><div class="rec-reason">{reason} · 置信度: <span class="confidence-{conf}">{conf}</span></div></li>'
            )
        recs_html = (
            f'<ul class="rec-list">{"".join(rec_items)}</ul>' if rec_items
            else '<p style="color:#22c55e;font-size:0.87rem;margin-top:8px">✓ 当前参数已达标，无需调整</p>'
        )

        step_count = data.get("step_count", 0)
        blocks.append(f"""
  <div class="axis-block">
    <div class="axis-title">
      {ax.capitalize()} 轴
      <span class="badge" style="background:{color};margin-left:8px">{assessment}</span>
      <span style="color:#64748b;font-size:0.8rem;margin-left:10px">· {step_count} 个有效阶跃窗口</span>
    </div>
    {metrics_html}
    {recs_html}
  </div>""")

    return f'<div class="section"><div class="section-title">⚙️ PID 调参建议</div>{"".join(blocks)}</div>'


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
        mag = p.get("magnitude_db", p.get("magnitude", 0))
        peak_rows += f"<tr><td>{p.get('freq', 0):.1f} Hz</td><td>{mag:.1f} dBFS</td><td>{src}</td><td>{is_h}</td></tr>"

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
        return '<div class="section"><div class="section-title">🧭 磁力计校准</div><p style="color:#64748b">暂无数据</p></div>'

    fitness = getattr(results, "fitness_mgauss", None)
    if fitness is None and hasattr(results, "get"):
        fitness = results.get("fitness_mgauss")
    assessment = getattr(results, "assessment", None) or (results.get("assessment") if hasattr(results, "get") else None) or "?"
    color = _assessment_color(assessment)

    fitness_html = ""
    if fitness is not None:
        fitness_html = f"""
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:12px">
    <div>
      <span style="color:#94a3b8;font-size:0.85rem">评估</span><br>
      <span class="badge" style="background:{color};font-size:1rem;padding:4px 16px">{assessment}</span>
    </div>
    <div>
      <span style="color:#94a3b8;font-size:0.85rem">Fitness</span><br>
      <span style="font-size:1.2rem;font-weight:600">{fitness:.2f} mGauss</span>
    </div>
  </div>"""

    # offset params
    ofs_rows = ""
    if hasattr(results, "ofs"):
        for label, val in zip(["OFS_X","OFS_Y","OFS_Z"], results.ofs):
            ofs_rows += f'<tr><td class="name">COMPASS_{label}</td><td class="val">{val:.2f}</td></tr>'
    elif hasattr(results, "get"):
        params = results.get("parameters", {})
        for k, v in params.items():
            ofs_rows += f'<tr><td class="name">{k}</td><td class="val">{v:.2f}</td></tr>'

    params_html = ""
    if ofs_rows:
        params_html = f'<table class="param-table"><tbody>{ofs_rows}</tbody></table>'

    return f'<div class="section"><div class="section-title">🧭 磁力计校准</div>{fitness_html}{params_html}</div>'


def save_html_report(
    html: str,
    output_path: str,
) -> None:
    """将 HTML 字符串写入文件。"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
