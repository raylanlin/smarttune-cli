# SmartTune v3.5.0 验收规范（增量）

基线：v3.4.0 已发版（commit 7c814dc）。回归照跑 v3.2.1 §A（版本号判 **3.5.0**）。
本次只动 **Betaflight** 路径 —— ArduPilot / PX4 / tlog 必须逐字节不变（§D 硬判据）。

准备：一份真实 `.bbl`，**最好两份**：一份 `blackbox_high_resolution = OFF`，
一份 `ON`（BF 配置器 → Blackbox → High resolution）。后者是本次核心。

```bash
export BF_LOG=/path/to/normal.bbl
export BF_HR_LOG=/path/to/high_resolution.bbl
export AP_LOG=/path/to/flight.bin
```

## A. 基线

```bash
pip install -e ".[all,mcp,dev]"
python -c "import smarttune; print(smarttune.__version__)"    # MUST 3.5.0
python -m compileall -q smarttune tools
ruff check --select E9,F63,F7,F82 smarttune/ tests/            # MUST 0
black --check smarttune/ tools/ tests/                         # SHOULD 过（不过则格式化后重测）
pytest -q                                                      # MUST 全绿（288 + 14 新增）
pytest -q tests/test_bf_alignment.py                           # MUST 14 passed
```

## B. high_resolution descaling（本次核心）

```bash
stune quality -i "$BF_HR_LOG" -f json | jq '.log_source, .sample_rate_hz'
stune analyze -i "$BF_HR_LOG" -f json | jq '{hr: .modules.hardware, rate: .sample_rate_hz}'
python - <<'PY'
import os, json
from smarttune.services.analysis import load_flight_data
from pathlib import Path
adapter, fd = load_flight_data(Path(os.environ["BF_HR_LOG"]), "betaflight")
info = fd.extras["blackbox_info"]
print(json.dumps(info, indent=1))
import numpy as np
print("gyro peak deg/s:", float(np.max(np.abs(fd.gyro))))
print("setpoint peak deg/s:", float(np.max(np.abs(fd.pid["roll"].desired))))
PY
```

**MUST**：`blackbox_info.high_resolution == true`、`high_resolution_scale == 10.0`。
**MUST**：gyro 峰值落在物理合理区间（特技机通常 300–1800 deg/s）。
**MUST NOT**：峰值贴着 ~2198 不动（那是旧版被 sanitizer 削平的特征）。

**关键交叉验证**（只有这一步能最终确认）：把同一份 `$BF_HR_LOG` 传进
<https://betaflight.com/blackbox>，看 roll gyro 的峰值读数。

| | 期望 |
|---|---|
| viewer 显示峰值 | 与上面 python 打印的 gyro 峰值**同数量级且接近**（±5%） |
| 旧版（v3.4.0）对比 | 旧版峰值应约为 viewer 的 10 倍、或被削平在 2198 附近 |

普通日志回归：

```bash
stune analyze -i "$BF_LOG" --report md -o /tmp/bf_new.md
# 与 v3.4.0 的同一份报告对比：high_resolution=OFF 时
# MUST 除阶跃响应段外逐字节一致（descaling 对 hr=0 是恒等变换）
```

## C. 阶跃响应（⚠ 有意的数值变化）

```bash
stune pid -i "$BF_LOG" -f json -a roll | jq '.axes.roll | {
  fft_step_info: (.fft_step.info // "n/a"), assessment }'
```

**MUST**：`info.method == "pid_analyzer_wiener"`、`info.output_signal == "gyro_adc"`、
`info.input_window_deg_s == [20, 500]`。
**MUST**：`info.steady_state` 在 0.5–3.0 之间且 `steady_state_ok == true`
（否则说明这份日志激励不足或反卷积没收敛 —— 换一份有明显 stick input 的日志）。

**交叉验证**：用 Plasmatree PID-Analyzer 跑同一份 `.bbl`
（`python PID-Analyzer.py -l log.bbl`），比 low-input（≤500 deg/s）那条曲线：

| | 期望 |
|---|---|
| 上升时间 | 同量级（±20%） |
| 曲线形状 | 同形（超调方向/有无振铃一致） |
| 稳态 | 都收敛到 ~1.0 |

若我们的曲线明显更"干净"或稳态偏离 1.0 很多 → 停止发版，回报差异。

## D. 其他平台零回归（硬判据）

```bash
stune analyze -i "$AP_LOG" --report md -o /tmp/ap_new.md
diff /tmp/ap_v340.md /tmp/ap_new.md       # MUST 逐字节一致（仅版本号）
stune quality -i "$TLOG" -f json | jq '.telemetry_notes | length'   # MUST 与 v3.4.0 相同
pytest -q tests/test_tlog_parser.py tests/test_param_tables.py      # MUST 全绿
```

ArduPilot 的阶跃响应本次**不应再变**（v3.4.0 已经改过 output_signal）——
`.bin` 报告 MUST 与 v3.4.0 逐字节一致。共享内核只新增了一个**默认关闭**的
`max_target_amplitude` 参数，测试 `test_shared_kernel_max_gate_is_opt_in` 守着这一点。

## E. MCP / 打包

```bash
python tools/smoke_mcp.py --log "$AP_LOG"     # MUST 全过
python -m build && python - <<'PY'
import glob, zipfile
whl = sorted(glob.glob("dist/smarttune-3.5.0-*.whl"))[-1]
names = zipfile.ZipFile(whl).namelist()
need = ["smarttune/platform/betaflight/step_response_fft.py",
        "smarttune/platform/ardupilot/tlog_parser.py",
        "smarttune/py.typed"]
print("MISSING:", [n for n in need if n not in names] or "none")
PY
```

## 已知限制（不要按 bug 报）

1. **PIDtoolbox 无法逐行核对** —— 仓库已下架（`bw1129/PIDtoolbox`）。本次以
   Plasmatree/PID-Analyzer 为准，它是该算法族的公开源头，也是 WebTools 的来源。
2. **窗长取 2 的幂** —— 共享内核沿用 WebTools 的 fft.js 约束，PID-Analyzer 用
   精确 1 秒样本数，频率分辨率略有差异（见 ALIGNMENT_BETAFLIGHT.md §4）。
3. **正则化形状不同** —— PID-Analyzer 硬掩码 vs 共享内核高斯 CDF 平滑掩码，
   两者都是 25 Hz 处的 SNR 正则化。
4. **只产出 low-input 一条曲线** —— PID-Analyzer 画 ≤500 / >500 两条。
5. **`FlightData.gyro` 仍是滤波后陀螺** —— 未滤波陀螺已放进
   `extras["gyro_unfiltered"]`，但振动判级阈值是按滤波数据标定的，
   切换数据源需要单独一版验证（计划中的 `--prefilter` 陷波模式）。
6. **Cleanflight / BF 2.x 旧日志** —— 那些固件的 `gyro_scale` 不是 1.0，
   本版未实现 legacy rad/µs 分支（现代 BF 不受影响）。
