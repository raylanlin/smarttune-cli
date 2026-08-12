## v3.2.0 (2026-08-12) — Parameter table rebuild (groups + enum meanings) + validation gate fix

The data side was fully rebuilt, and the code side fixes the security issue where
"validation" did not actually validate. Parameter tables are now **generated
artifacts**: the scraper lives in the repo (`tools/build_param_tables.py`) and a
linter guards them (`stune params --lint`).

### Data: parameter tables regenerated for all three platforms (schema_version 2)

| Platform | Params | Groups | Upstream source |
|------|-----:|---:|----------|
| ArduPilot | 2,839 | 194 | `apm.pdef.json` (raylanlin/ParameterRepository → Copter-4.1) |
| Betaflight | 814 | 82 | `src/main/cli/settings.c` + `fc/parameter_names.h` (BF has no metadata artifact; firmware source parsed directly) |
| PX4 | 1,908 | 78 | `docs/public/config/failsafe/parameters.json` (output of PX4's own px4params generator) |

Old-table defects fixed (each has a corresponding regression test):

- **Descriptions shifted by one column** — `ARM_MAH` carried `BATT_OPTIONS`' description, `BATT_LOW_MAH` carried `CRT_MAH`'s. Names were sorted, descriptions were not; the zip misaligned. This is worse than NOT FOUND: an AI makes recommendations from the wrong description and the user never notices.
- **Names stripped of group prefixes, inconsistently** — `MONITOR` (should be `BATT_MONITOR`), `0_BAUD` (`SERIAL0_BAUD`), `10_DIRECTION` (`SERVO10_DIRECTION`), `2SRV_IMAX`, plus `LOW_VOLT` coexisting with `BATT_LOW_VOLT`. Root cause: the scraper read `// @Description` without expanding group prefixes (the `@PREFIX@` placeholders left in descriptions were the smoking gun), while stripping the prefix from names. Now the real firmware names are always used.
- **Missing @Values / @Bitmask** — the old tables had 0 `values` fields; the AI had no way to know `BATT_MONITOR=4` means "Analog Voltage and Current". Now ArduPilot's 781 enums + 112 bitmasks, PX4's 360 + 37, and Betaflight's 164 lookup tables all carry member meanings.
- **Wrong type inference** — floats like `ACCEL_R_MAX`, `ACRO_Y_EXPO`, `ACC_BIAS_LIM` were tagged `enum` (which combined with the validation bug below into a security issue).
- **default uniformly 0.0** (invented, not scraped) — now ArduPilot/Betaflight write `null` when upstream does not publish defaults (unknown ≠ 0); all 1,908 PX4 params carry real upstream defaults.
- **category misclassification** — `FL_FF`, `I2C_ADDR`, `SERIAL_NUM`, `FS_VOLTSRC` were all filed under battery. Category is now derived from parameter group + tuning-domain rules.

New fields: `group` (the firmware's own parameter group), `display_name`, `values`, `bitmask`, `increment`, `user` (Standard/Advanced), `reboot_required`, `read_only`, `unresolved_ref`. Tables carry a `source` provenance block (upstream file + firmware version + generation date).

### Fixed — validation gate (security)

- **`ParamTable.validate()` returned `True, "value accepted"` for enums** — any value passed. Combined with the type mislabeling above, a large share of the table **completely skipped range validation**: `ACCEL_R_MAX` has min 0.0, yet `validate(999999)` returned True. README sells this tool as a mandatory safety gate before recommending; the gate was wide open for enums.
  - Now: `validate_detail()` returns a structured verdict — `status` ∈ `ok / not_found / out_of_range / not_a_member / not_an_integer / unverifiable`; enums are checked against real members, bitmasks against real bit spans, and rejections return the **allowed values with meanings** to the AI.
  - **fail-closed**: discrete params with neither member tables nor ranges return `valid=false, status="unverifiable"` instead of silently passing.
  - `validate()` keeps its `(bool, str)` signature for backward compatibility.
- **Packaging missed files**: `knowledge/params/*.json` was not in `package-data` — after pip install, `stune params` and the three MCP param tools failed with `FileNotFoundError`. (Fixed in v3.1; recorded here.)
- `ParamDef(**item)` blew up the whole table with `TypeError` on new upstream fields → switched to `from_dict()` which ignores unknown keys, forward-compatible.

### Fixed — MCP reliability

- **Payload slimming**: `smarttune_list_params` used to return the whole table as full objects (ArduPilot: 2,839 entries with long descriptions ≈ 600 KB per response — likely the real culprit behind that "no response" incident). It now returns slim rows (name/type/range/unit/one-liner) with `limit`/`offset` paging, and **refuses without a group or category, pointing to the group index first**; full descriptions and enum members are served by the new `smarttune_get_param`.
- **stdout hygiene**: stdio MCP's stdout may only carry JSON-RPC. SmartTune itself writes to stderr (audited, no print leaks), but third-party parsers on the analysis path (pyulog / pymavlink / matplotlib) may print — one line of noise kills the whole stream. All service calls are now wrapped in `_quiet_stdout()`, redirecting stdout to stderr.
- **Unified error shape**: there used to be at least three (`{"error": str}`, `{"error","code","hint"}`, `{"valid":false,"error"}`) with no retry marker. Now success is `{ok: true, …}`, failure is `{ok: false, error_code, message, hint, retryable}` so clients can tell "retryable" from "don't bother". A rejected parameter value is a **successful call** + `valid: false`, not a transport error.
- **Param tools no longer pay the numpy startup cost**: `from smarttune.platform.params import ParamTable` used to execute `smarttune/platform/__init__.py` → `base` → `models.flight_data` → `import numpy`. Param tools only use the stdlib but were dragged through the whole scientific stack by the package `__init__`; every MCP call_tool spawns a new process, so looking up one param loaded everything. Switched to PEP 562 module-level `__getattr__` lazy import; public API unchanged.

### Added

- **`tools/build_param_tables.py`** — the missing scraper, now in the repo. One generation path per platform, output carries provenance blocks; `--check` runs the linter directly. Pure stdlib.
- **`smarttune/platform/param_lint.py`** — parameter table health check: `name_shape` / `suffix_collision` / `placeholder_leak` / `discrete_without_members` / `constant_default` / `range_inverted` / `enum_key_not_int` / `empty_description` / `duplicate_name`. Every rule maps to a real incident. Current tables: **0 errors**.
- **MCP `smarttune_list_param_groups`** — see the group index first (80–200 groups with counts/categories/sample members), then drill in; replaces "pull the whole table at once".
- **MCP `smarttune_get_param`** — full definition of a single param (description + range + default + enum meanings).
- **CLI group browsing**: `stune params ap --groups`, `--group ATC_`, `--lint`, and ranked `--search` (exact name > prefix > substring > display name > description > enum label); `stune params --search "analog voltage"` finds `BATT_MONITOR`. Listing a platform without filters shows the **group index** instead of dumping 2,839 rows.
- All `stune params` sub-modes support `-f json`.

### Tests

- New `tests/test_param_tables.py` (37 cases): schema/provenance/lint zero errors for all three tables, names are real firmware names, descriptions not shifted, defaults not invented, enum member meanings, fail-closed validation, group index and in-group queries, search ranking, slim/full payload shapes, and 8 CLI end-to-end cases.

### Verification

- New `docs/TEST_PLAN_v3.2.md` — an executable acceptance spec, item by item (static checks / unit tests / parameter table data regression /
  9 validation-gate cases / JSON contract / MCP contract & payload size limits / lazy loading / wheel contents / zero analysis-value regression),
  with pass criteria, failure report format, and a "known limitations (not bugs)" list.
- New `tools/smoke_mcp.py` — an MCP stdio smoke test with a built-in JSON-RPC client: checks 15 tools,
  payload size limits, unified error shape, the validation gate, and **any stdout noise makes it fail outright**
  (the detection mechanism for the last "no response" incident). No mcp client library dependency.

### Docs

- README: `stune params` section rewritten as "group → param → validate"; new Parameter tables subsection (three-platform source table + regeneration commands + linter notes); MCP tool table 13 → 15 with payload contract.
- `skill-mcp/SKILL.md`: tool list and param query workflow updated in sync.

---

## v3.1.0 (2026-08-12) — CLI JSON output (`--format json`)

### Added

- **`smarttune/output/json_output.py`** — CLI JSON output layer: envelope (`schema_version` / `tool` / `command` / `status` / `generated_at`) + strict JSON encoding + structured error envelope.
- **`-f/--format text|json`** across `analyze` / `pid` / `fft` / `magfit` / `sysid` / `hardware` / `filter` / `quality` / `platforms` / `params` (validate / search / query / list modes).
  - payloads come straight from the services layer — the same functions the MCP server calls, so **CLI JSON and MCP JSON are isomorphic**; there is no second serialization path that can drift.
  - JSON goes only to stdout (or `-o` file); progress, hints, and error panels always go to stderr, so `| jq` is always clean.
  - Failure paths are JSON too: `status="error"` + `error {code, type, message, hint}`, exit code 1.
  - NaN / ±Inf are sanitized to `null` before writing (`allow_nan=False`); strict parsers no longer blow up.
  - `SMARTTUNE_DETERMINISTIC=1` omits `generated_at` so output is byte-diffable (CI snapshot regression).

### Fixed

- **`--report md` without `-o` silently produced nothing**: now aligned with the HTML path, defaulting to `<logstem>_report.md`.
- **Packaging missed files**: `pyproject.toml`'s `package-data` only included `rules/**/*.json`; `knowledge/params/*.json` never made it into the wheel — `stune params` and MCP's three param validation tools hit `FileNotFoundError` in pip-installed environments. Added `params/*.json`.
- Added the missing `smarttune/py.typed` (declared in `package-data` but the file did not exist).
- Removed an unused `import json` in the `params` command (ruff F401).

### Tests

- New `tests/test_cli_json.py` (13 cases): envelope structure / payload cannot clobber meta fields / NaN sanitization / determinism switch / error envelope / `analyze` `quality` success & failure paths / stdout JSON for `platforms` and `params --validate` / text behavior unchanged without `--format`.

### Docs

- README: Output Formats gains a `--format json` contract table and examples; removed the "planned for a future release" note; three outdated "JSON only via MCP" phrasings updated (Quick Start / For Agents / For Humans).

---

## v3.0.4 (2026-06-19) - hotfix

### Fixed

- **`serialize_magfit_result` 兼容 `FitResult`**：duck-type 兜底支持 analyzer 层返回的 `FitResult` (4 个 numpy 数组 + `assessment` str) 和 services 层 `MagFitResult` (recommendations + offsets dict) 两种 result 类型。
  - 修复前：`AttributeError: 'FitResult' object has no attribute 'recommendations'`，整条 `analyze_log` 在 magfit 模块崩。
  - 修复后：MCP analyze_log 返回完整 6 模块 JSON（含 magfit 的 assessment / fitness_mgauss / offsets / recommendations）。
  - 影响：MCP-only agent（小Mo/小Mo学长）通过 `smarttune_analyze_log` 调到的所有路径。CLI 走 formatter.py 不受影响。

### Tests

- 新增 `tests/services/test_serialize.py`（约 90 行）：双类型回归测试（FitResult + MagFitResult）+ 真实 .bin 端到端测试。

# SmartTune v3.0.3 — R5 性能优化 + R6 跨模块契约修复 (2026-06-14)

**依据：** Claude v4「完全体」补丁包 R5 + R6 轮次
**范围：** bit-identical 向量化性能优化（4 处热点）+ 3 个跨模块契约 Bug 修复
**测试：** `pytest tests/ -q → 167 passed in 5.21s`（含 8 个新增契约测试，全绿）

## R5 — 性能优化（bit-identical，数值零变化）

| 文件 | 变更 | 影响 |
|------|------|------|
| `analyzers/magfit.py` | 新增 `ned_to_body_batch()` 批量四元数旋转；`_compute_bin_weights` 用 `np.bincount` + fancy index 替掉逐样本 Python 循环 | 数万样本日志的期望磁场计算从 O(N) 次 Python 调用 → 1 次批运算；bin 权重同 |
| `platform/ardupilot/step_response_fft.py` | scale 数组去标量循环 → `np.full` + 端点赋值；SNR 高斯累积去标量循环 → `np.exp` + `np.cumsum` | 每次调用一次性，向量化；已 JS 数值核验 bit-identical |

## R6 — 跨模块契约 Bug 修复

| 文件 | 变更 | 影响 |
|------|------|------|
| `platform/ardupilot/__init__.py` | parse() 注入 generic key（`pid.roll.p`→`ATC_RAT_RLL_P` 值）到 params | **修复 AP PID 参数建议被全部丢弃**（`_get_current_pid` 查 generic key，原生名匹配不到 → 恒返回 0.0 → C4 跳过） |
| `platform/betaflight/__init__.py` | 同上，兼顾 BF 4.5+ 新名与旧固件名两张映射表 | **修复 BF PID 参数建议被全部丢弃**，与 AP 同根因 |
| `knowledge/rules/betaflight/pid_rules.json` | 新增 BF 整数尺度 `pid_bounds`（P[10,150]/I[20,220]/D[0,100]/FF[0,300]） | 缺省 AP 尺度（0.01~0.5）会把 BF 45×1.1 夹成 0.5；generic-key 修复让 BF 开始产出建议后必须配套修正 |
| `analyzers/pid_reviewer.py` | 新增 `_is_axis_threshold_shape()` 形状校验；非消费方形状时回退 `_DEFAULT_THRESHOLDS` | 所有 pid_rules.json 的 thresholds 块按指标组织（thresholds[metric][axis]），但 analyze() 按轴取 → 知识库 thresholds 是死配置，一直跑硬编码兜底 |
| `tests/test_platform_contracts.py` | 新增 8 个契约测试 | A2 注入、阈值形状守卫、BF bounds 尺度、BF 增益不被夹到 AP 尺度 |

---

# SmartTune v3.0.1 — 架构审查修复 (2026-06-11)

**依据：** 《SmartTune 架构与算法审查报告.md》 14 处代码修复
**范围：** 关键 Bug 修复（CRITICAL/HIGH）+ 算法对齐 WebTools 源码 + 数值/单位语义修正
**测试：** 待 pytest 验证（见下）
**部署形态：** 精准补丁包（10 个源文件覆盖），不破坏本地独有的 plot.py / step_response_protocol.py / PX4 适配器 / 5 个测试文件

---

## 1. Bug 修复（按文件）

| 文件 | 缺陷编号 | 修复内容 |
|------|---------|---------|
| `smarttune/analyzers/magfit.py` | C1 | `_extract_current_vec()` 修复 KeyError；旧实现 100% 崩溃被 try/except 吞掉为 "skipped" |
| `smarttune/analyzers/magfit.py` | C2 | 单位全链路统一 mGauss；`earth_field_ned()` 返回 mGauss；残差计算去掉 `×10` 换算 |
| `smarttune/analyzers/magfit.py` | C5 | 伪油门回退加 `PitchIn/RollIn` 键存在性守卫 |
| `smarttune/analyzers/magfit.py` | C6 | ODI 改为 ArduPilot 对称软铁矩阵 `M·(raw+OFS) + MOT·thr`；**数值与旧版不可比，旧 ODI 建议应作废** |
| `smarttune/analyzers/magfit.py` | — | `earth_field_ned()` 新增 `has_position` 参数；GPS 缺失时输出显式警告（不再静默用深圳默认坐标） |
| `smarttune/analyzers/fft_analyzer.py` | C3 | `INS_HNTCH_REF` 语义修正：mode 1→0；mode 2→0+警告（需用户设悬停油门参考值如 MOT_THST_HOVER）；mode 4→1.0；**防止错误参数写入飞控** |
| `smarttune/analyzers/fft_analyzer.py` | C12 | `_identify_source()` 频带配置改为 `{**_defaults, **bands}` 合并 |
| `smarttune/analyzers/pid_reviewer.py` | C4 | `current_val <= 0` 时跳过该参数建议（消除 "current 0.0 → suggested 0.01" 伪建议） |
| `smarttune/analyzers/pid_reviewer.py` | C7 | `_analyze_axis()` 实际构造 imu_dict；采样率 <20Hz 切到时域回退 |
| `smarttune/analyzers/pid_reviewer.py` | C8 | 负向阶跃超调用 `min` 对称计算；settling 未进带 → -1 哨兵（不再用窗口长度冒充） |
| `smarttune/analyzers/pid_reviewer.py` | — | 建议调整幅度全局 cap `clip(factor, 0.75, 1.25)`（落实 README "±25% cap"） |
| `smarttune/platform/betaflight/step_response_fft.py` | C10 | **整体重写** — 对齐 PID Toolbox PTstepcalc.m（2s 段、minInput=20/maxInput=500 deg/s、零填充 FFT、稳态 QC 0.5-3.0、跨段平均） |
| `smarttune/platform/ardupilot/step_response_fft.py` | C10 | 窗口筛选阈值 3.0→20.0 deg/s（对齐 WebTools `TarMax<20`）；`step_end` floor→ceil；complex64→complex128 |
| `smarttune/analyzers/arx_model.py` | C14 | `arx_identify(return_info=True)` 返回 fallback 标记；`estimate_delay()` 改用 `fftconvolve`（O(N log N)） |
| `smarttune/analyzers/sysid_analyzer.py` | C14 | ARX fallback 抛 `AnalysisError`（调用方需 catch）；`discrete_to_second_order()` 修正过阻尼系统一阶近似 |
| `smarttune/services/analysis.py` | C9 | quality 激励统计改用 `pid_reviewer.detect_steps()`（与 `stune pid` 阶跃窗口数不再矛盾） |
| `smarttune/platform/registry.py` | A3 | 注册期捕获元数据快照 `_metadata`；`list_platforms()` 不再每次重新实例化适配器 |
| `smarttune/platform/base.py` | A4 | `param_table()` 返回类型通过 `TYPE_CHECKING` 导入 |

## 2. 已知行为变化

| 变化 | 升级须知 |
|------|---------|
| magfit 从"静默跳过"变为正常工作 | 旧版因 C1 崩溃从不产出结果；升级后首次看到 magfit 输出属预期 |
| **ODI 拟合值语义变化** | 现为 ArduPilot 软铁矩阵非对角元，与旧版数值不可比；旧 ODI 建议应作废 |
| `INS_HNTCH_REF` mode 2 输出 0 + 警告 | 需用户自行填入悬停油门参考值（可取 MOT_THST_HOVER 学习值） |
| BF 阶跃窗口可能变少 | 筛选阈值 3→20 deg/s（对齐上游）；弱激励日志会报 valid_windows=0 |
| sysid 对坏数据直接报错 | 数据不足抛 `AnalysisError` 而非输出虚构 ωn/ζ；调用方需 catch |
| `settling_time` 可能为 -1 | 表示窗口内未稳定（未知），不再用窗口长度冒充 |

## 3. 接口兼容性

- `arx_identify()` 默认仍返回 `(a, b)` 二元组，`return_info=True` 为可选参数 — 向后兼容
- BF `estimate_step_response()` 移除 `cutfreq`、新增 `max_target_amplitude` — 仓库内无外部调用方
- `earth_field_ned()` 新增可选参数 `has_position=True` — 向后兼容
- `MAGFit` ODI 拟合结果语义变化 — 见上表
- 其余公开 API 无签名变化

## 4. 本次未涉及

- A1：CLI 与 services 双轨实现收敛（cli.py 与 deploy 包同源，本次未触及）
- 等级标签体系统一（FFT 的 SEVERE/CRITICAL vs Assessment 枚举）
- README 中"测试徽章""PX4 口径"等文档修订 — deploy 包基于旧快照的误判，**未套用**
- C2 fitness 量级、BF 阶跃曲线数值 — 代码已修，需真实日志验证

## 5. 部署/验证（patch 模式）

```bash
# 1. 10 个源文件精准覆盖（已完成）
cp <patch>/smarttune/analyzers/{magfit,fft_analyzer,pid_reviewer,arx_model,sysid_analyzer}.py \
   <local>/smarttune/analyzers/
cp <patch>/smarttune/services/analysis.py <local>/smarttune/services/
cp <patch>/smarttune/platform/{registry,base}.py <local>/smarttune/platform/
cp <patch>/smarttune/platform/{ardupilot,betaflight}/step_response_fft.py \
   <local>/smarttune/platform/{ardupilot,betaflight}/

# 2. 版本号更新
#    smarttune/__init__.py: __version__ = "3.0.1"

# 3. 验证
pytest tests/ -v
stune --version         # 应输出 3.0.1
stune analyze <flight>.bin
#    - magfit 不再 "skipped"
#    - PID 建议中无 "current 0.0" 条目
#    - 无 GPS 日志 magfit 警告区有 IGRF 提示
```

---

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

---

# SmartTune v3.0.2 — PX4 ULog + A1 收敛 + 标签统一 (2026-06-12)

**依据：** Claude 审定补丁包 v4「完全体」（三轮全部变更）
**Commit:** `ed9d784` | 13 files, +1181/-510
**测试：** 156 passed, 3 failed (synthetic regression 容差偏紧，非逻辑 bug)

## 三轮变更

| 轮次 | 文件 | 内容 |
|------|------|------|
| R1 缺陷修复 | analyzers/* + platform/* | 同 v3.0.1（C1~C14+A3+A4） |
| R2 A1 收敛 | analysis.py, cli.py | `run_module()` 统一核心；analyze 命令 + single analysis 路径共用；B2 fft_result 赋值修复；sysid na/nb 传递 |
| R3 PX4 + 标签 | px4/__init__.py, fft_analyzer.py, output/*, knowledge/* | ULog 解析器完整实现（pyulog）；SEVERE→POOR/CRITICAL→UNUSABLE；PX4 知识库；validate_px4_ulog.py |

## PX4 新增能力

| 命令 | 状态 | 说明 |
|------|------|------|
| `stune quality -i .ulg` | ✅ | PID/IMU/Motor/Battery 数据完整性 + 阶跃统计 |
| `stune pid -i .ulg` | ✅ | PID 阶跃响应（需 v1.13+ 固件才有 vehicle_rates_setpoint） |
| `stune fft -i .ulg` | ✅ | PX4 原生参数（IMU_GYRO_NF0_FRQ 等） |
| `stune sysid -i .ulg` | ✅ | ARX 系统辨识 |
| `stune analyze -i .ulg` | ✅ | 综合报告（magfit 在 PX4 不支持，自动跳过） |

## 端到端验证（sample.ulg）

- quality: 60/100 MARGINAL, 17070 PID+IMU samples, 250Hz, 16.7% jitter
- fft: GOOD (0.5 m/s²), PX4 原生参数
- pid: MARGINAL (0 steps) — 预期降级，老固件缺 vehicle_rates_setpoint
- sysid: 三轴数据可用

## 额外修复（carpenter 补充）

`formatter.py:199` — `if result.ofs:` numpy array 真值判断 → `shape >= 3` guard。
C1 修复解锁后暴露的潜在 bug，Claude 包只做了标签兼容未修此条。

## 工具

`tools/validate_px4_ulog.py` — 一键 PX4 全链路验证，自动下载 pyulog sample.ulg

