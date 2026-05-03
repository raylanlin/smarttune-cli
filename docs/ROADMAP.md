# SmartTune CLI — Development Roadmap

## 项目状态：v2.0.0 (多平台架构重构)

### 已完成 ✅

#### 架构层
- [x] `FlightData` / `AxisPIDSignal` / `ModeChange` 统一数据模型
- [x] `ParamRef` 平台无关参数引用 + `ParamRecommendation` 结果类型
- [x] `PlatformAdapter` 抽象基类 + 注册/自动检测机制 (`registry.py`)
- [x] ArduPilot 适配器 (完整 DataFlash .bin 解析 + 参数映射表)
- [x] Betaflight 适配器 (接口 + 参数映射表就绪，BBL parser 待实现)
- [x] PX4 适配器 (接口 + 参数映射表就绪，ULog parser 待实现)
- [x] 知识库分层加载器 (common → platform → user → Pro，6 层 deep_merge)

#### 分析引擎层 (全部平台无关化)
- [x] PID Reviewer — 阶跃检测 / 指标计算 / 诊断规则 / 建议生成
- [x] FFT Analyzer — 振动频谱分析 / 峰值检测 / 陷波建议
- [x] SysID Analyzer — ARX 系统辨识 / 自然频率 / 阻尼比
- [x] MagFit — 磁力计参数拟合 / 覆盖度检查
- [x] Hardware Report — 传感器配置 / 参数摘要
- [x] Filter Transfer — Bode 图 / 滤波器链传递函数 (纯数学，零依赖)
- [x] Step Response (FFT + Time Domain) — 纯数值计算模块

#### 输出层
- [x] `OutputFormatter` — terminal (Rich) + Markdown 输出
- [x] ParamRef → 平台参数名翻译 (通过 `adapter.map_param_to_platform()`)
- [x] `FullAnalysisResult` 综合结果容器 + 汇总建议
- [ ] HTML Report — 自包含 HTML 报告 (旧代码可参考，待适配新结果类型)

#### CLI
- [x] `stune analyze` — 综合分析 (PID + FFT + MagFit)
- [x] `stune pid` — PID 阶跃响应
- [x] `stune fft` — FFT 振动频谱
- [x] `stune magfit` — 磁力计校准
- [x] `stune sysid` — 系统辨识
- [x] `stune hardware` — 硬件配置报告
- [x] `stune platforms` — 列出支持的平台
- [x] `--platform auto|ardupilot|betaflight|px4` 全局参数
- [x] 自动日志格式检测 (magic bytes + 扩展名)

#### 测试
- [x] 40 个测试全部通过
- [x] 架构测试 (模型/注册/知识库/错误体系)
- [x] PID 分析器测试 (合成信号/空数据/知识库覆盖/ParamRef)
- [x] FFT 分析器测试 (基本分析/频率检测/数据不足/频谱数据)
- [x] 端到端集成测试 (合成数据全流水线/跨平台参数翻译)
- [x] 输出格式化器测试 (ArduPilot/Betaflight/PX4 参数翻译/Markdown)

---

### Phase 2: Betaflight 支持 (v2.0)

#### BBL 解析器实现
- [x] BBL 文件头解析 (H 行键值对)
- [x] 帧定义解析 (Field I name / Field P name / Field S name)
- [x] I-frame 解码 (关键帧，完整值)
- [x] P-frame 解码 (差值帧，signed/unsigned VB 变长编码 + 预测器)
- [x] S-frame 解码 (慢帧，GPS 等低频数据)
- [x] E-frame 解码 (事件帧，模式切换)
- [x] 字段映射到 FlightData:
  - `setpoint[0/1/2]` → `pid.{roll/pitch/yaw}.desired`
  - `gyroADC[0/1/2]` → `pid.{roll/pitch/yaw}.actual` + `gyro`
  - `axisP/I/D/F[0/1/2]` → `pid.{axis}.p_term/i_term/d_term/ff_term`
  - `motor[0-7]` → `motor_output` (归一化 0-1)
  - `accSmooth[0/1/2]` → `accel` (转 m/s²)
- [x] 多段日志处理 (一个 .bbl 可能包含多次飞行)
- [x] 飞行模式标志位解码 (ARM/ANGLE/HORIZON/ACRO/FAILSAFE)
- [x] 34 个 BBL 解析器单元测试 (编码往返/头解析/帧解码/适配器集成)

#### Betaflight 知识库
- [x] PID 规则适配 (d_min/d_max、feedforward 独立项、anti_gravity、机型预设)
- [x] 滤波器规则 (RPM filter、dynamic notch、gyro_lpf1/2、dterm_lpf、滤波器链)
- [x] 典型机型阈值 (5" freestyle / 3" cinewhoop / 7" long range / toothpick)

#### Betaflight 特有分析器
- [x] Feedforward 分析器 (FF 贡献度/过冲检测/追踪误差)
- [x] RPM Filter 效果评估 (电机峰值检测/噪声衰减量化)
- [x] D-term 噪声分析 (D/P 比率/d_min 激活占比/高频能量占比)
- [x] 22 个 BF 分析器 + 知识库测试

#### 验证
- [ ] 使用真实 BF 日志验证解析正确性 (对照 Blackbox Explorer)
- [ ] PID 分析结果与 Blackbox Explorer 的 Step Response 对比
- [ ] FFT 分析结果与 Betaflight 内置频谱对比

---

### Phase 2.x: PX4 支持

- [ ] pyulog 集成 (`pip install pyulog`)
- [ ] ULog → FlightData 映射:
  - `vehicle_angular_velocity` → gyro
  - `rate_ctrl_status` → PID signals
  - `vehicle_magnetometer` → mag
- [ ] PX4 参数映射完善 (MC_ROLLRATE_* 等)
- [ ] PX4 知识库规则

---

### Phase 3: 高级功能 (v3.0)

- [ ] 跨平台对比分析 (同一机体不同固件的调参对比)
- [ ] Skill / Agent 编排层适配新架构
- [ ] SKILL-PRO 视觉审阅流程适配
- [ ] Web UI (可选，基于现有 HTML 报告扩展)
- [ ] Plugin 系统 (第三方平台适配器注册)

---

### 技术债务

- [ ] HTML Report 适配新结果类型 (`smarttune/output/html_report.py`)
- [ ] filter_transfer.py 中 `derive_filters_from_params()` 的 ArduPilot 参数名硬编码需泛化
- [ ] 旧 `_legacy_formatter.py` 中的可视化 (matplotlib plot) 代码迁入新输出层
- [x] arx_model.py / wmm.py docstring 中的残留 ap_tune 引用清理（2026-05-03 完成）
- [x] html_report.py 工具名 ap-tune → SmartTune CLI（2026-05-03 完成）
- [ ] CI/CD 设置 (GitHub Actions: lint + test + build)
- [ ] PyPI 发布配置

---

### 设计决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 项目名 | SmartTune CLI (`stune`) | 平台中性，不绑定任何飞控品牌 |
| BBL 解析 | 纯 Python 自研 | 零外部依赖，与"离线零云"理念一致 |
| ULog 解析 | pyulog 库 | PX4 官方维护，成熟度足够 |
| 抽象层 | FlightData 最大公约数 + extras 扩展槽 | 避免过度抽象导致信息丢失 |
| 知识库 | 按平台分目录，6 层 deep_merge | 灵活覆盖，Pro 层无侵入 |
| 参数引用 | ParamRef generic → adapter 翻译 | 分析引擎完全平台无关 |
| 安全原则 | 保守调参，单次 ≤20%，只建议不写入 | 飞行安全第一 |
| 许可证 | MIT (CLI) + 私有 (Pro) | 开源核心 + 闭源增值 |
