# SmartTune v3.4.0 验收规范（增量）

基线：v3.3.1 已验收发版。回归照跑 v3.2.1 §A（版本号判 **3.4.0**）。
本次新增 `.tlog`（MAVLink 遥测日志）支持，**分析算法零改动** —— §E 是数值零回归的硬判据。

准备：一份真实 `.tlog`（Mission Planner 的 `logs/*.tlog`，或飞控连过 MAVProxy 的录制）

```bash
export TLOG=/path/to/"2026-09-13 10-00-00.tlog"
export AP_LOG=/path/to/flight.bin        # 用于 §E 对比
```

## A. 基线

```bash
pip install -e ".[all,mcp,dev]"
python -c "import smarttune; print(smarttune.__version__)"   # MUST 3.4.0
python -m compileall -q smarttune tools
ruff check --select E9,F63,F7,F82 smarttune/ tests/           # MUST 0
black --check smarttune/ tools/ tests/                        # SHOULD 过
pytest -q                                                     # MUST 全绿（254 + 21 新增）
pytest -q tests/test_tlog_parser.py                           # MUST 21 passed
stune params --lint                                           # MUST exit 0
```

## B. 识别与平台声明

```bash
stune platforms -f json | jq '.platforms[] | select(.name=="ardupilot").extensions'
# MUST 含 .tlog
stune analyze -i "$TLOG" -f json | jq -r '.platform'          # MUST "ardupilot"（自动识别）
cp "$AP_LOG" /tmp/fake.tlog && stune quality -i /tmp/fake.tlog ; echo exit=$?
# MUST exit=1：.bin 内容伪装成 .tlog 必须被结构嗅探拒绝
```

## C. 解析与诚实标注（核心）

```bash
stune quality -i "$TLOG"
# MUST 出现 "Telemetry Log (.tlog) Limits" 区块
# MUST advice 指向 onboard DataFlash .bin
stune quality -i "$TLOG" -f json | jq '{kind:.log_source.kind, gyro:.log_source.gyro_source,
  notes:(.telemetry_notes|length), score:.quality.score, advice:.quality.advice}'
# MUST kind=="mavlink_telemetry"；notes>0；score <= 85
stune analyze -i "$TLOG" -f json | jq '.telemetry_notes'      # MUST 非空
stune analyze -i "$TLOG" | tail -40                            # MUST 打印遥测限制块
```

逐项人工确认（对照这份 tlog 的实际内容）：

| 情况 | 期望 |
|------|------|
| 有 `ATTITUDE_TARGET` | `.axes` 三轴齐全；每轴 `p_term/d_term` 全 0；notes 说明 P/I/D 不存在 |
| 无 `ATTITUDE_TARGET` | 无 PID 模块；notes 明确说"没有 desired rate，去看 .bin" |
| 无 `RAW_IMU` | `log_source.gyro_source == "ATTITUDE"`；notes 说明是 EKF 滤波后数据 |
| 遥测流 < 100 Hz | notes 含 FFT 上限与 40–120 Hz 共振带说明 |
| GCS 未下载参数 | notes 含 "No PARAM_VALUE"；filter 模块无当前值可读 |
| 有掉链 > 3s | `log_source.link_gaps_s` 非空 + notes 报告最长间隔 |

```bash
# 单位抽查：ATTITUDE rollspeed(rad/s) → deg/s，RAW_IMU xgyro(mrad/s) → deg/s
stune pid -i "$TLOG" -f json -a roll | jq '{n:.axes.roll.sample_count,
  assess:.axes.roll.assessment}'
# MUST 角速率量级为 deg/s（几十～几百），不是 rad/s（个位数）
```

## D. PX4 tlog 拒绝 + MCP

```bash
# 若有 PX4 载机录制的 tlog：
stune quality -i px4_vehicle.tlog ; echo exit=$?
# MUST exit=1，错误提示指向 .ulg

python tools/smoke_mcp.py --log "$AP_LOG"                     # MUST 全过（回归）
```

MCP 手动补验：
- `smarttune_log_quality(log_path="<tlog>")` MUST `ok:true` 且含 `telemetry_notes`/`log_source`
- `smarttune_analyze_log(log_path="<tlog>")` MUST 带 `telemetry_notes`
- `smarttune_list_platforms` 的 ardupilot 项 `mcp_accepted_extensions` MUST 含 `.tlog`
- 路径校验 MUST 接受 `.tlog`（不再报 Disallowed file extension）

## E0. 阶跃响应对齐（⚠ 本次唯一的数值变化）

```bash
stune pid -i "$AP_LOG" -f json -a roll | jq '.axes.roll.fft_step.info // .axes.roll'
```

**MUST**：`info.output_signal == "pid_act"`（对齐 WebTools PIDReview，不再默认用 IMU 陀螺）。

**MUST**：`.bin` 的 PID 阶跃响应指标（rise time / overshoot / settling）与 v3.3.1 **会变**，
方向是靠近参考实现。人工确认方式：把同一份 `.bin` 传进 ArduPilot PIDReview
（https://firmware.ardupilot.org/Tools/WebTools/PIDReview/），选同一轴、同一时间窗，
对比阶跃曲线形状与上升时间量级。**MUST 同量级、同形状**；若我们的曲线明显更"干净"
或更晚起跳，说明数据源仍不对。

```bash
# 对比手段：显式开陀螺路径，确认两者都能跑且有差异
python - <<'PY'
from smarttune.platform.ardupilot.step_response_fft import compute_step_response_for_axis
# 需要真实 pid_dict/imu_dict，见 analyzers/pid_reviewer.py 的构造方式
PY
```

其余模块（FFT / MagFit / filter / hardware / quality）**MUST 与 v3.3.1 逐字节一致** —— 见 §E。

## E. 数值零回归（除阶跃响应外的硬判据）

```bash
stune analyze -i "$AP_LOG" --report md -o /tmp/new34.md
# 与 v3.3.1 的同一日志报告对比
diff /tmp/old331.md /tmp/new34.md
```

**MUST 逐字节一致，除了 PID 阶跃响应段**（见 §E0：输出信号由 IMU 陀螺改为 PID.Act，
这是有意的对齐改动）。FFT 峰值、MagFit 偏移、滤波器建议、硬件报告、质量评分
**MUST 不变**。除阶跃响应外有任何数值变化 → 停止发版。

## F. 打包

```bash
python -m build && python - <<'PY'
import glob, zipfile
whl = sorted(glob.glob("dist/smarttune-3.4.0-*.whl"))[-1]
names = zipfile.ZipFile(whl).namelist()
need = ["smarttune/platform/ardupilot/tlog_parser.py",
        "smarttune/knowledge/params/ardupilot.json",
        "smarttune/knowledge/params/ardupilot.copter-4.5.json",
        "smarttune/py.typed"]
print("MISSING:", [n for n in need if n not in names] or "none")
PY
```

## G. 与 ArduPilot 参考实现的对齐

```bash
python -c "from smarttune.platform.ardupilot import step_response_fft as s; print(s.__doc__)"
```

**MUST**：docstring 里的 PIDReview.js 行号核对表与
`docs/ALIGNMENT_ARDUPILOT_WEBTOOLS.md` 一致。

tlog 侧对齐项（`pytest -q tests/test_tlog_parser.py` 已覆盖，真机 tlog 上人工抽查）：

```bash
stune quality -i "$TLOG" -f json | jq '.log_source | {clock, clock_offset_s, vehicle_sysid,
  gcs_heartbeats_ignored, frames_received, frames_dropped, drop_percent, systems}'
```

| 检查 | 期望 |
|------|------|
| `clock` | `vehicle_boot`（真机 tlog 必有 `time_boot_ms`）；`clock_offset_s` 约等于 PC 与飞控上电时间差 |
| `gcs_heartbeats_ignored` | > 0（Mission Planner 一定在发自己的心跳）—— 若为 0，说明过滤没生效或录制方式特殊 |
| `vehicle_sysid` | 通常 1；`systems` 里应能看到 GCS 的 255 且 `is_vehicle: false` |
| `drop_percent` | 与 Mission Planner 显示的丢包率同量级；可用 ArduPilot StreamStats 网页工具加载同一份 tlog 交叉验证 |
| 模式名 | 与 UAVLogViewer 加载同一份 tlog 显示的模式序列**逐项一致**（含 `ALT_HOLD` 这种带下划线的写法） |

## 已知限制（不要按 bug 报）

1. **MagFit 对 `.tlog` 基本不可用** —— 遥测没有 per-compass 偏移量（`extras["compass_raw"]` 为空），
   只有 `RAW_IMU` 的磁场三轴。有磁场数据时 magfit 可能跑出结果，但可信度低于 `.bin`。
2. **SysID 对 `.tlog` 不可用**（数据率不够），能力声明未改 —— 数据不足会以标准 "insufficient data" 收场。
3. **P/I/D 全 0** 是事实而非 bug：遥测流里没有控制器分项。
4. `SERVO_OUTPUT_RAW` 按 1000–2000 µs 归一化；非标准输出范围的机型电机占比会偏。
5. tlog 时间戳来自地面站 PC 时钟，不是飞控时钟 —— 绝对时间可能与 `.bin` 有偏移（时长计算不受影响）。
