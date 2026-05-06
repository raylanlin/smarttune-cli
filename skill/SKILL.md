---
name: smarttune
description: ArduPilot 飞行日志离线分析 + 调参建议。当用户发送 .bin 日志文件需要分析 PID、滤波器、磁力计校准时触发。
---

# SmartTune CLI (stune)

Multi-platform flight log analysis & tuning advisor.
Supports **ArduPilot** (.bin/.log), **Betaflight** (.bbl/.bfl), and **PX4** (.ulg).

平台自动检测，无需手动指定。

## 安装

```bash
cd ~/cli-tools/smarttune-cli
pip install -e .
```

## 高频命令

```bash
# 综合分析（推荐）— PID + FFT + Filter + Mag 全部
stune analyze -i log.bin

# 日志质量评分 — 数据完整度 / 激励充分性 / 采样率
stune quality -i log.bin

# 单项分析
stune pid -i log.bin -a roll
stune fft -i log.bin
stune filter -i log.bin --gyro-filter 40 --visual
stune sysid -i log.bin -a roll
stune hardware -i log.bin
stune magfit -i log.bin

# 查看支持的平台
stune platforms
```

## 使用原则

1. **先看帮助**：不确定参数时，用 `stune <command> --help`
2. **默认只输出终端**：不加 `-o` 参数时，结果只在终端显示
3. **可选图表**：加 `--visual` 生成 matplotlib 图表
4. **分析后清理**：日志分析完成后删除原始 `.bin/.log/.bbl/.ulg` 文件，不留中间产物

⚠️ 分析完成 = 输出结果即可，无需文件存留

## 工作流程

### 入门流程

```bash
# 1. 硬件配置检查
stune hardware -i flight.bin

# 2. 日志质量评分
stune quality -i flight.bin

# 3. 综合分析
stune analyze -i flight.bin --visual

# 4. 针对性调参
stune pid -i flight.bin -a roll
stune fft -i flight.bin --visual
```

### 高级流程

```bash
# 系统辨识（ARX 模型）
stune sysid -i flight.bin -a roll --na 3 --nb 2

# 滤波器传递函数分析
stune filter -i flight.bin --gyro-filter 40 --visual

# 滤波前后对比
stune fft -i flight.bin --visual
```

### 多平台

```bash
# 默认自动检测平台
stune analyze -i flight.bbl

# 也可手动指定
stune analyze -i flight.bin --platform ardupilot
```

## 命令详解

### stune analyze

综合分析日志，输出 PID + FFT + Filter + Mag 全套调参建议。

```bash
stune analyze -i flight.bin                          # 基础分析
stune analyze -i flight.bin --visual                 # 生成图表
stune analyze -i flight.bin -a roll                  # 只分析 Roll 轴
stune analyze -i flight.bin -o report.md --report md # Markdown 报告
stune analyze -i flight.bin --theme dark --visual    # 暗色主题图表
```

### stune quality

日志质量评分 — 检查数据完整度、激励充分性（PID 阶跃窗口数）、采样率一致性。

```bash
stune quality -i flight.bin
stune quality -i flight.bin -o quality.txt
```

### stune pid

PID 阶跃响应分析 — 上升时间、超调、稳定时间、振荡次数。

```bash
stune pid -i flight.bin                              # 全轴分析
stune pid -i flight.bin -a roll                      # 单轴分析
stune pid -i flight.bin -a roll --visual             # 阶跃响应图
stune pid -i flight.bin --visual --theme dark        # 暗色主题
```

### stune fft

FFT 振动频谱分析 — 识别主振动频率，建议陷波滤波器参数。

```bash
stune fft -i flight.bin
stune fft -i flight.bin --visual                     # 频谱图
stune fft -i flight.bin --visual --theme dark
```

### stune filter

滤波器传递函数分析 (Bode Plot) — 两种模式：

- **Auto 模式 (默认)**：从日志参数自动推导滤波器配置
  - ArduPilot: 读取 `INS_HNTCH_*` 参数
  - Betaflight: 读取 `gyro_lowpass_hz` / notch 参数
- **Manual 模式**: 指定 `--gyro-filter` / `--notch-freq`

```bash
stune filter -i flight.bin                           # auto-derive
stune filter -i flight.bin --no-auto --gyro-filter 20 --visual
stune filter -i flight.bin --notch-freq 80 --visual
```

### stune sysid

ARX 系统辨识 — 从日志估计传递函数（自然频率、阻尼比、时间常数）。

```bash
stune sysid -i flight.bin                            # 分析所有轴
stune sysid -i flight.bin -a roll                    # 只分析 Roll
stune sysid -i flight.bin -a roll --na 3 --nb 2     # 自定义 ARX 阶数
```

### stune hardware

生成硬件配置报告 — IMU、磁力计、滤波器、PID 参数一览。

```bash
stune hardware -i flight.bin
stune hardware -i flight.bin --platform ardupilot    # 强制指定平台
```

### stune magfit

磁力计校准分析 — Fitness 评估、硬铁/软铁干扰诊断、飞行覆盖范围检查。

```bash
stune magfit -i flight.bin
```

## 平台支持矩阵

| 能力 | ArduPilot | Betaflight | PX4 |
|------|-----------|------------|-----|
| `analyze` | ✅ | ✅ | 🔲 |
| `quality` | ✅ | ✅ | 🔲 |
| `pid` | ✅ | ✅ | 🔲 |
| `fft` | ✅ | ✅ | 🔲 |
| `filter` | ✅ | ✅ | 🔲 |
| `sysid` | ✅ | ✅ | 🔲 |
| `hardware` | ✅ | ✅ | 🔲 |
| `magfit` | ✅ | — | 🔲 |
| 日志格式 | .bin / .log | .bbl / .bfl | .ulg |

## 需要更多信息时

| 场景 | 做法 |
|------|------|
| 完整参数列表 | `stune <command> --help` |
| 参数含义说明 | 知识库封装在 CLI 内部 (`smarttune/knowledge/`) |

## 与 WebTools 对齐

| WebTools 工具 | stune 命令 | 状态 |
|--------------|-----------|------|
| PIDReview | `pid` + `analyze` | ✅ |
| FilterReview | `filter` + `fft` | ✅ |
| HardwareReport | `hardware` | ✅ |
| MAGFit | `magfit` | ✅ |
| SysID | `sysid` | ✅ |
| — | `quality` | ✅ 新增 |

## 能力状态

| 能力 | 命令 | 状态 |
|------|------|------|
| 日志解析 | 多平台 auto-detect | ✅ |
| PID 分析 | `stune pid` | ✅ |
| FFT 分析 | `stune fft` | ✅ |
| 磁力计校准 | `stune magfit` | ✅ |
| 综合分析 | `stune analyze` | ✅ |
| 系统辨识 | `stune sysid` | ✅ |
| 滤波器分析 | `stune filter` | ✅ |
| 硬件报告 | `stune hardware` | ✅ |
| 日志质量评分 | `stune quality` | ✅ |
| 多平台支持 | AP + BF + PX4 | ✅ |

## 与旧版 ap-tune 的关系

- `ap-tune` (ArduPilot 单平台) → **已废弃**，代码合并进 SmartTune v2.0
- `stune` (SmartTune v2.0+) → **当前唯一 CLI**，多平台统一接口
- 任何时候不要使用 `ap-tune`，只用 `stune`
