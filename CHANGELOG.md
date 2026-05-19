# SmartTune MCP 能力对齐 — 变更文档

**日期：** 2026-05-19
**范围：** MCP Server / Services 层 / 图表生成
**测试：** 141 passed, 0 failed

---

## 1. 变更总览

本次升级解决了 MCP 接口远落后于 CLI 的问题。CLI 有 8 条命令（`platforms`, `quality`, `analyze`, `pid`, `fft`, `magfit`, `sysid`, `filter`, `hardware`），而旧版 MCP 只暴露了 3 个 tool 且实现简陋。

| 维度 | 升级前 | 升级后 |
|------|--------|--------|
| MCP Tools | 3 | **10** |
| analysis.py 公开函数 | 3 (`load_flight_data`, `get_log_quality`, `analyze_log`) | **9** (+`analyze_pid/fft/magfit/sysid/filter/hardware`) |
| serialize.py 序列化器 | 5 | **8** (+`sysid/filter/extra`) |
| 图表生成 | 无 | **新模块 `plot.py`** — 3 种图表 base64 PNG |
| 代码行数 | 936 行（3 文件） | **2231 行**（4 文件） |

---

## 2. 文件清单

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `smarttune/mcp_server.py` | 422 → 724 | **重写** | 3 tool → 10 tool |
| `smarttune/services/analysis.py` | 240 → 746 | **重写** | 3 函数 → 9 函数 |
| `smarttune/services/serialize.py` | 274 → 337 | **增补** | +3 序列化器 |
| `smarttune/services/plot.py` | 0 → 424 | **新增** | base64 图表生成 |

---

## 3. MCP Tool 对照表

### 3.1 已有 Tool（保留或增强）

| # | MCP Tool | CLI 命令 | 变更说明 |
|---|----------|----------|----------|
| 1 | `smarttune_list_platforms` | `stune platforms` | 无改动 |
| 2 | `smarttune_log_quality` | `stune quality` | **重写**：补齐 4 维评分 |
| 3 | `smarttune_analyze_log` | `stune analyze` | **扩充**：新增 sysid/filter/extra 模块 |

`smarttune_log_quality` 旧版仅调用 `fd.validate()`，现在与 CLI 完全一致：

- **数据完整性** — PID/gyro/mag/motor/battery 各通道是否存在
- **飞行时长** — 是否满足最低分析要求
- **摇杆激励** — 每轴检测 step response 窗口数
- **采样率一致性** — 计算 jitter 和 drop rate

`smarttune_analyze_log` 新增模块：

- `sysid` — ARX 系统辨识（通过 `include_modules` 选择）
- `filter` — 滤波器传递函数
- `extra` — 平台专属分析器（Betaflight FF/RPM/DTerm）
- Markdown 渲染器同步补齐这三个 section

### 3.2 新增 Tool

| # | MCP Tool | CLI 命令 | 参数 |
|---|----------|----------|------|
| 4 | `smarttune_analyze_pid` | `stune pid` | `log_path`, `platform`, `axis`, `max_recommendations` |
| 5 | `smarttune_analyze_fft` | `stune fft` | `log_path`, `platform`, `max_recommendations` |
| 6 | `smarttune_analyze_magfit` | `stune magfit` | `log_path`, `platform`, `max_recommendations` |
| 7 | `smarttune_analyze_sysid` | `stune sysid` | `log_path`, `platform`, `axis`, `na`, `nb` |
| 8 | `smarttune_analyze_filter` | `stune filter` | `log_path`, `platform`, `gyro_filter_hz`, `notch_freq_hz`, `auto_derive` |
| 9 | `smarttune_analyze_hardware` | `stune hardware` | `log_path`, `platform` |
| 10 | `smarttune_generate_plot` | `stune pid/fft/filter --visual` | `log_path`, `plot_type`, `platform`, `axis`, `theme` |

### 3.3 `smarttune_generate_plot` 详细说明

CLI 的 `--visual` 将 matplotlib 图保存为本地 PNG 文件。MCP 不能写文件给用户，因此改为返回 **base64 Data URL**，agent 可直接用 `<img src="...">` 渲染，或转存为 PNG。

**参数：**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `log_path` | str | 必填 | 日志文件路径 |
| `plot_type` | str | `"pid"` | `"pid"` / `"fft"` / `"filter"` |
| `platform` | str | `"auto"` | 平台覆写 |
| `axis` | str | `"all"` | 仅 PID 有效：`"roll"` / `"pitch"` / `"yaw"` / `"all"` |
| `theme` | str | `"light"` | `"light"` / `"dark"` |

**返回示例：**

```json
{
 "image_base64": "data:image/png;base64,iVBORw0KGgo...",
 "plot_type": "pid",
 "axes_plotted": ["roll", "pitch", "yaw"],
 "platform": "ArduPilot",
 "log_file": "flight.bin",
 "format": "png"
}
```

**三种图表：**

| plot_type | 图表内容 | 对应 CLI |
|-----------|----------|----------|
| `pid` | 阶跃响应曲线（每轴子图），标注 rise time / overshoot / settling time，10%/90% 参考线 | `stune pid --visual` |
| `fft` | FFT 频谱图，连续频谱曲线或峰值柱状图，标注峰值频率/噪声底 | `stune fft --visual` |
| `filter` | Bode 图（幅度+相位双面板），-3dB 参考线，±45° 相位区 | `stune filter --visual` |

---

## 4. Services 层变更

### 4.1 `analysis.py` — 新增 6 个公开函数

| 函数 | 对应 CLI | 核心逻辑 |
|------|----------|----------|
| `analyze_pid()` | `stune pid` | PIDReviewer → serialize_pid_result |
| `analyze_fft()` | `stune fft` | FFTAnalyzer → serialize_fft_result |
| `analyze_magfit()` | `stune magfit` | MAGFit → serialize_magfit_result |
| `analyze_sysid()` | `stune sysid` | SysIDAnalyzer → serialize_sysid_results |
| `analyze_filter()` | `stune filter` | 平台专属 filter_transfer 模块，auto/manual 双模式 |
| `analyze_hardware()` | `stune hardware` | 平台专属 hardware_report 模块 |

### 4.2 关键 Bug 修复

**Hardware import 路径错误：**

```python
# 旧版（错误）
from smarttune.analyzers.hardware_report import generate_hardware_report

# 新版（与 CLI 一致）
mod = importlib.import_module(f"smarttune.platform.{adapter.name}.hardware_report")
mod.generate_hardware_report(fd.params, flight_data=fd)
```

旧版使用通用路径，而实际上 ArduPilot / Betaflight / PX4 各有独立的 hardware_report 实现。

### 4.3 `serialize.py` — 新增 3 个序列化器

| 函数 | 输入 | 处理 |
|------|------|------|
| `serialize_sysid_results()` | `Dict[str, SysIDResult]` | 调用 `SysIDResult.to_dict()`，封装为 axes 结构 |
| `serialize_filter_result()` | Bode 原始数据 | 压缩为关键频率点格式（不含原始数组） |
| `serialize_extra_analyzers_results()` | 平台 extra_analyzers 输出 | 通用 dict 处理 |

### 4.4 `plot.py` — 新模块

纯函数式设计，不依赖任何 MCP / CLI 代码：

| 函数 | 功能 |
|------|------|
| `generate_pid_plot(pid_result, theme)` | 接受 PIDAnalysisResult 或序列化 dict → base64 PNG |
| `generate_fft_plot(fft_result, spectrum_data, theme)` | 接受 FFT 结果 + 可选完整频谱 → base64 PNG |
| `generate_filter_bode_plot(filter_result, theme)` | 接受含 `bode_data` 的 filter 结果 → base64 PNG |
| `generate_plot(log_path, plot_type, ...)` | 高层调度：解析 → 分析 → 绘图，一步到位 |

`analyze_filter()` 新增内部参数 `_include_bode_data=True`（不暴露给 MCP），启用时在返回值中附带完整频率/幅度/相位数组供绘图使用。

---

## 5. MCP 架构改进

### 5.1 `_call_service()` 统一封装

所有 tool（除 list_platforms 和 analyze_log）均通过 `_call_service(func, log_path, **kwargs)` 调用：

1. 路径验证 → PathValidationError → JSON error
2. 调用业务函数
3. SmartTuneError → 结构化 JSON error（含 code + hint）
4. 未知异常 → logger.exception + JSON error

消除了旧版各 tool 各自处理异常的重复代码。

### 5.2 Tool Annotations

所有 tool 共享 `_READ_ONLY_ANNOTATIONS`：

```python
{
 "readOnlyHint": True,
 "destructiveHint": False,
 "idempotentHint": True,
 "openWorldHint": False,
}
```

### 5.3 输入校验

所有 tool 在调用 service 前校验：

- `axis` ∈ {`all`, `roll`, `pitch`, `yaw`}
- `response_format` ∈ {`json`, `markdown`}
- `include_modules` ⊆ {`pid`, `fft`, `magfit`, `hardware`, `filter`, `sysid`}
- `plot_type` ∈ {`pid`, `fft`, `filter`}
- `na`, `nb` clamp 到 [1, 10]
- `max_recommendations` clamp 到 [1, 100]

---

## 6. 平台能力矩阵

各 MCP tool 自动检查平台 `capabilities()` 后再执行：

| 能力 | ArduPilot | Betaflight | PX4 |
|------|-----------|------------|-----|
| pid | ✅ | ✅ | ✅ |
| fft | ✅ | ✅ | ✅ |
| magfit | ✅ | ❌ | ❌ |
| sysid | ✅ | ❌ | ❌ |
| filter | ✅ | ✅ | ✅ |
| hardware | ✅ | ✅ | ✅ |
| quality | ✅ | ✅ | ✅ |

不支持的 tool 返回结构化 error（含 code 和 hint），不会 crash。

---

## 7. Agent 推荐工作流

```
1. smarttune_list_platforms
 → 确认日志格式是否支持

2. smarttune_log_quality(log_path)
 → 评估数据质量（0-100 分），决定是否继续

3. smarttune_analyze_log(log_path, response_format="markdown")
 → 综合分析，给用户完整报告

4. smarttune_generate_plot(log_path, plot_type="pid")
 → 阶跃响应图表，辅助解释 PID 调参建议

5. 按需单独调用：
 smarttune_analyze_sysid(log_path, na=3, nb=2)
 smarttune_analyze_filter(log_path)
```

---

## 8. 2GB 服务器注意事项

- `generate_plot` 每次调用会加载 matplotlib + 解析日志 + 绘图，峰值内存约 200-400MB
- 建议不要在一次对话中连续调用多种 plot_type（pid + fft + filter），可以按需分步
- matplotlib 使用 `Agg` backend，不需要 X11 / display server
- 如果 matplotlib 未安装，`generate_plot` 返回 ImportError 而非 crash

---

## 9. 未涉及项

以下问题在本次升级范围外，记录备查：

| 项目 | 说明 |
|------|------|
| Serialize 层脆弱性 | dict vs dataclass 格式不统一，serialize 函数需要同时处理两种格式 |
| Betaflight .bbl 数据缺失 | motor/battery/PID 参数在 .bbl 格式中经常缺失，quality 评分会偏低 |
| MCP vs Plugin 架构 | OpenClaw 实际使用 native plugin 而非 MCP 协议，两套接口需长期维护 |
| pyproject.toml | 未更新 MCP tool 文档 |
| README.md | 未更新 tool 列表（旧版仅列 3 个） |
