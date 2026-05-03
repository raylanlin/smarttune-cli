# SmartTune Migration Guide

## ap-tune → smarttune 模块迁移映射

本文档记录从 `ap-tune` (ArduPilot-only) 到 `smarttune` (multi-platform) 的模块映射关系，
便于逐步迁移现有分析逻辑。

### 模块映射

| 旧模块 (ap_tune) | 新位置 (smarttune) | 状态 | 说明 |
|---|---|---|---|
| `cli.py` | `smarttune/cli.py` | ✅ 已重写 | 新增 `--platform`，PID+FFT 已接通 |
| `errors.py` | `smarttune/errors.py` | ✅ 已重写 | 基类改名 `SmartTuneError` |
| `log_parser.py` | `smarttune/platform/ardupilot/` | ✅ 已迁入 | 拆为 adapter 的 `parse()` 方法 |
| `pid_reviewer.py` | `smarttune/analyzers/pid_reviewer.py` | ✅ 已迁移 | 接收 FlightData，输出 ParamRef |
| `fft_analyzer.py` | `smarttune/analyzers/fft_analyzer.py` | ✅ 已迁移 | 接收 FlightData，不依赖 LogParser |
| `step_response_fft.py` | `smarttune/analyzers/step_response_fft.py` | ✅ 原样迁移 | 纯数值计算，无改动 |
| `step_response_time_domain.py` | `smarttune/analyzers/step_response_td.py` | ✅ 原样迁移 | 纯数值计算，无改动 |
| `magfit.py` | `smarttune/analyzers/magfit.py` | ✅ 已迁移 | 接收 FlightData，CLI 待接通 |
| `filter_transfer.py` | `smarttune/analyzers/filter_transfer.py` | ✅ 原样迁移 | 纯数学，无 LogParser 依赖 |
| `filter_visualization.py` | `smarttune/output/filter_visualization.py` | ✅ import 已修正 | |
| `sysid_analyzer.py` | `smarttune/analyzers/sysid_analyzer.py` | ✅ 已迁移 | 接收 FlightData |
| `hardware_report.py` | `smarttune/analyzers/hardware_report.py` | ✅ 已迁移 | 接收 FlightData |
| `output.py` | `smarttune/output/formatter.py` | ✅ 已重写 | 通过 PlatformAdapter 翻译参数名 |
| `html_report.py` | `smarttune/output/html_report.py` | 🔲 待适配 | 可用旧代码参考 |
| `knowledge/__init__.py` | `smarttune/knowledge/loader.py` | ✅ 已重写 | 新增平台维度 |
| `knowledge/rules/*.json` | `smarttune/knowledge/rules/ardupilot/` | ✅ 已迁移 | |

### 迁移每个分析器的步骤

以 `pid_reviewer.py` 为例：

1. **改输入签名**: `__init__(self, parser: LogParser, knowledge)` → `__init__(self, knowledge)`
2. **改 analyze 签名**: `analyze(axis=None)` → `analyze(flight_data: FlightData, axis=None)`
3. **替换数据访问**:
   - `parser.get_pid_data("roll")` → `flight_data.pid["roll"]`
   - `parser.get_parameters()` → `flight_data.params`
   - `parser.get_imu_data()` → `flight_data.gyro` / `flight_data.accel`
4. **替换参数名**: 硬编码的 `"ATC_RAT_RLL_P"` → `ParamRef("pid.roll.p")`
5. **替换输出**: `Recommendation(param="ATC_RAT_RLL_P", ...)` → `ParamRecommendation(param=ParamRef("pid.roll.p"), ...)`
6. **测试**: 用现有 ArduPilot 日志验证输出结果一致

### 新增文件（非迁移）

| 文件 | 说明 |
|---|---|
| `smarttune/models/flight_data.py` | FlightData / AxisPIDSignal / ModeChange 定义 |
| `smarttune/models/analysis_result.py` | ParamRef / 所有 Result 类型定义 |
| `smarttune/platform/base.py` | PlatformAdapter 抽象基类 |
| `smarttune/platform/registry.py` | 平台注册 + 自动检测 |
| `smarttune/platform/betaflight/` | BF BBL 适配器（Phase 2） |
| `smarttune/platform/px4/` | PX4 ULog 适配器（Phase 3） |
| `smarttune/knowledge/rules/common/` | 跨平台通用规则 |
| `smarttune/knowledge/rules/betaflight/` | BF 特有规则 |

### Pro 知识库迁移

`smarttune-knowledge-pro` 需要：
1. 包名从 `ardupilot_knowledge_pro` → `smarttune_knowledge_pro`
2. `load()` 函数签名改为 `load(platform: str = None) -> Dict`
3. 返回结构: `{"common": {...}, "ardupilot": {...}, "betaflight": {...}}`
4. 规则目录按平台分子目录
