---
name: smarttune-mcp
description: Read-only SmartTune flight log analysis through MCP tools. 10 tools covering CLI parity (platforms, quality, analyze, pid, fft, magfit, sysid, filter, hardware) plus chart generation. Use for agents that do not have exec/shell/write permission, especially customer-support agents handling .bin/.log/.bbl/.bfl/.ulg flight logs through OpenClaw.
---

# SmartTune MCP

Use this skill when SmartTune is connected through MCP and the agent does not have shell, exec, or write permission.

This is the MCP-only variant of the SmartTune skill. If the agent has normal CLI access, use the `smarttune` skill instead.

## Hard Rules

* Do not call `exec`, shell commands, Python subprocesses, or the `stune` CLI.
* Do not ask for write, edit, or process execution permissions to analyze a log.
* Do not create report files or choose arbitrary output paths.
* Do not modify logs, parameters, firmware, local files, or aircraft configuration.
* Use only the SmartTune MCP tools listed below.
* If these tools are unavailable, say SmartTune MCP is not connected and ask the operator to enable the MCP server.
* **MANDATORY: Validate every parameter recommendation.** Before suggesting any parameter change, call `smarttune_validate_param` with the exact parameter name, proposed value, and platform. Never recommend a parameter that fails validation — search for alternatives with `smarttune_search_params`. A `status` of `unverifiable` is **not** approval.
* **Every tool returns one shape:** `{ok: true, ...}` on success, `{ok: false, error_code, message, hint, retryable}` on failure. A rejected parameter value is a successful call with `valid: false`.
* **Never list a whole parameter table.** Browse with `smarttune_list_param_groups` → `smarttune_list_params(group=...)` → `smarttune_get_param(name)` for full detail. ArduPilot alone is ~2,800 parameters.

## Available MCP Tools (15 total)

### Core Tools

| # | Tool | CLI Equivalent | Description |
|---|------|----------------|-------------|
| 1 | `smarttune_list_platforms` | `stune platforms` | List supported platforms, extensions, and capabilities. |
| 2 | `smarttune_log_quality` | `stune quality` | 4-dimension quality assessment (data completeness, duration, excitation, sample rate consistency). Returns score 0–100, rating (EXCELLENT/GOOD/MARGINAL/POOR), and detailed breakdown. |
| 3 | `smarttune_analyze_log` | `stune analyze` | Comprehensive analysis: PID + FFT + magfit + hardware + filter + sysid. Use `include_modules` to select. Returns JSON or Markdown. |

### Parameter Validation Tools (USE BEFORE RECOMMENDING)

| # | Tool | CLI Equivalent | Description |
|---|------|----------------|-------------|
| 4 | `smarttune_validate_param` | `stune params --validate` | ⚠️ **MANDATORY before recommending.** Checks the name exists, and that the value is a defined enum member / legal bit combination / inside [min, max]. Returns `valid` plus `status`: `ok` / `not_found` / `out_of_range` / `not_a_member` / `not_an_integer` / `unverifiable`, with the allowed values when it rejects. |
| 5 | `smarttune_list_param_groups` | `stune params <platform> --groups` | **Start here.** The platform's firmware parameter groups (ArduPilot 194, Betaflight 82, PX4 78) with counts, categories and sample members. |
| 6 | `smarttune_list_params` | `stune params <platform> --group X` | Parameters in one group or category, as compact rows (name/type/range/unit/one-line summary). Paged via `limit`/`offset`. Refuses to dump a whole table. |
| 7 | `smarttune_get_param` | `stune params <NAME>` | Full definition of one parameter: upstream description, range, default, increment, and **what each enum value means** (BATT_MONITOR 4 = "Analog Voltage and Current"). Call this before explaining a parameter. |
| 8 | `smarttune_search_params` | `stune params --search` | Ranked keyword search across names, groups, display names, descriptions and enum labels. Exact/prefix name matches rank first. |

### Individual Analysis Tools

| # | Tool | CLI Equivalent | Description | Key Parameters |
|---|------|----------------|-------------|----------------|
| 7 | `smarttune_analyze_pid` | `stune pid` | PID step response analysis per axis. Detects step windows, computes rise time, overshoot, settling time. | `axis` (all/roll/pitch/yaw), `max_recommendations` |
| 8 | `smarttune_analyze_fft` | `stune fft` | FFT vibration spectrum analysis. Identifies vibration peaks, noise floor, source guess (motor/prop/frame), and notch filter recommendations. | `max_recommendations` |
| 9 | `smarttune_analyze_magfit` | `stune magfit` | Magnetometer calibration analysis. Returns fitness (mGauss), offsets, assessment. | `max_recommendations` |
| 10 | `smarttune_analyze_sysid` | `stune sysid` | ARX system identification. Estimates natural frequency, damping ratio, suggests PID bandwidth/gains. | `axis`, `na` (1-10, default 3), `nb` (1-10, default 2) |
| 11 | `smarttune_analyze_filter` | `stune filter` | Filter transfer function analysis. Returns Bode plot data (key freq response points, -3dB cutoff, config summary). Supports auto-derive from log params or manual override. | `gyro_filter_hz`, `notch_freq_hz`, `auto_derive` |
| 12 | `smarttune_analyze_hardware` | `stune hardware` | Hardware configuration report. IMU setup, filter config, PID params, battery report, firmware/board info. | — |

### Chart Generation Tool

| # | Tool | CLI Equivalent | Description |
|---|------|----------------|-------------|
| 13 | `smarttune_generate_plot` | `stune pid/fft/filter --visual` | Generate analysis chart as **base64 PNG image**. Returns `image_base64` (data:image/png;base64,...) and `file_path` (saved to `/tmp/smarttune-plots/`). |

**Plot types:**

| plot_type | Chart Content | Description |
|-----------|--------------|-------------|
| `pid` | PID step response curve | Per-axis subplot with rise time / overshoot / settling time annotations, 10%/90% reference lines, step count. |
| `fft` | FFT vibration spectrum | Continuous spectrum curve or peak bar chart, annotated with frequency/source/noise floor. |
| `filter` | Filter Bode plot | Magnitude + phase dual panel, -3dB reference line, ±45° phase zone. |

**Parameters:**
- `plot_type`: `"pid"` (default) / `"fft"` / `"filter"`
- `axis`: `"all"` (default) / `"roll"` / `"pitch"` / `"yaw"` (PID only)
- `theme`: `"light"` (default) / `"dark"`
- `platform`: `"auto"` (default) / `"ardupilot"` / `"betaflight"` / `"px4"`

**Sending plots to users:**
The tool returns both `image_base64` (data URL for inline display) and `file_path` (local PNG path). To send to a Feishu group chat, use the file path with your message tool's media parameter.

## Supported Logs

* ArduPilot: `.bin`, `.log`
* Betaflight: `.bbl`, `.bfl`
* PX4: `.ulg`

## Workflow

### Quick Analysis
1. Call `smarttune_list_platforms` if unsure about platform support.
2. Call `smarttune_log_quality(log_path)` — always check quality first.
3. If score ≥ 55, call `smarttune_analyze_log(log_path, response_format="markdown")` for a full report.
4. If quality is MARGINAL/POOR, explain what data is missing and how to improve (e.g., enable motor logging, fly more aggressive stick inputs).

### Parameter Validation (MANDATORY)
**Before presenting any parameter change recommendation:**

1. For each recommended parameter, call `smarttune_validate_param`:
   ```
   smarttune_validate_param(param_name="ATC_RAT_RLL_P", param_value=0.15, platform="ardupilot")
   ```
   - If `valid: true` → proceed to recommend
   - If `status: "not_a_member"` → the response's `options` lists every legal value and its
     meaning; pick from those or drop the recommendation
   - If `status: "unverifiable"` → the table cannot confirm this value. Say so; do not present
     it as validated
   - If `valid: false` with "NOT FOUND" → the parameter doesn't exist in this firmware. Search for alternatives:
     ```
     smarttune_search_params(keyword="roll rate", platform="ardupilot")
     ```
   - If `valid: false` with "below min" or "exceeds max" → adjust the value to within range

2. If unsure about a parameter name, use `smarttune_search_params` to find the correct name.

3. If you need to see all available parameters, use `smarttune_list_params`.

**This step prevents recommending parameters that don't exist in the target firmware** — a critical issue because:
- Betaflight 4.5+ renamed many parameters (e.g., `d_min_roll` → `d_max_roll`, `gyro_lowpass_hz` → `gyro_lpf1_static_hz`)
- Parameter names differ between firmware versions
- Some parameters have strict value ranges

### Detailed Single-Topic Analysis
5. For a narrow topic, call the specific tool:
   * PID tuning: `smarttune_analyze_pid(log_path, axis="all")`
   * Vibration: `smarttune_analyze_fft(log_path)`
   * Compass: `smarttune_analyze_magfit(log_path)`
   * System ID: `smarttune_analyze_sysid(log_path, na=3, nb=2)`
   * Filter: `smarttune_analyze_filter(log_path)`
   * Hardware: `smarttune_analyze_hardware(log_path)`

### Charts
6. To generate a chart: `smarttune_generate_plot(log_path, plot_type="pid", theme="light")`
   * The result includes `image_base64` (data URL) and `file_path` (local PNG).
   * Send the image to users via your channel's media/file send capability.

### Response Format
* Use `response_format="markdown"` for direct user replies.
* Use `response_format="json"` when another agent will consume the result.

## Response Style

* Start with whether the log is usable (quality score + rating).
* Explain the main fault signals in plain language.
* Give conservative tuning advice and separate confidence from assumptions.
* Recommend one change at a time and ask for a new log after changes.
* **Parameter names**: Analysis tools output generic or platform-specific parameter names. These may differ between firmware versions (e.g., Betaflight 4.5+ renamed `d_min_roll` to `d_max_roll`). Using the CLI command `stune params --validate <name> <value> -p <platform>` is the authoritative way to verify a parameter exists before applying changes.
* Do not claim an aircraft is safe to fly. Say what the log does and does not support.
* Do not paste raw JSON unless the user asks for machine-readable output.

## Error Handling

* If the path is rejected: ask the user/operator to upload the original log through the supported channel.
* If the extension is unsupported: list the accepted MCP extensions.
* If the log is too large: ask the operator to raise the MCP server file-size limit or provide a smaller log segment.
* If analysis returns partial failures: report the modules that succeeded, then explain which data was missing for the failed modules.
* If a platform doesn't support a tool (e.g., magfit on Betaflight): the tool returns a structured error with code and hint.

## Platform Capability Matrix

| Tool | ArduPilot | Betaflight | PX4 |
|------|-----------|------------|-----|
| quality | ✅ | ✅ | ✅ |
| analyze_log (all modules) | ✅ | partial | partial |
| analyze_pid | ✅ | ✅ | ✅ |
| analyze_fft | ✅ | ✅ | ✅ |
| analyze_magfit | ✅ | ❌ | ❌ |
| analyze_sysid | ✅ | ❌ | ❌ |
| analyze_filter | ✅ | ✅ | ✅ |
| analyze_hardware | ✅ | ✅ | ✅ |
| generate_plot | ✅ | ✅ | ✅ |

Betaflight does not support magfit or sysid (no magnetometer data in blackbox logs, no ARX model).
