# SmartTune CLI — Development Roadmap

## Project Status: v2.0.0 (Multi-platform Architecture Refactor)

### Completed ✅

#### Architecture
- [x] `FlightData` / `AxisPIDSignal` / `ModeChange` unified data model
- [x] `ParamRef` platform-agnostic parameter reference + `ParamRecommendation` result type
- [x] `PlatformAdapter` abstract base class + registry/auto-detection (`registry.py`)
- [x] ArduPilot adapter (full DataFlash .bin parser + parameter mapping)
- [x] Betaflight adapter (interface + parameter map ready, BBL parser implemented)
- [x] PX4 adapter (interface + parameter map ready, ULog parser pending)
- [x] Knowledge base layered loader (common → platform → user → Pro, 6-level deep_merge)

#### Analysis Engine (all platform-agnostic)
- [x] PID Reviewer — step detection / metric computation / diagnostics / recommendations
- [x] FFT Analyzer — vibration spectrum analysis / peak detection / notch suggestions
- [x] SysID Analyzer — ARX system identification / natural frequency / damping ratio
- [x] MagFit — magnetometer parameter fitting / coverage checking
- [x] Hardware Report — sensor configuration / parameter summary
- [x] Filter Transfer — Bode plot / filter chain transfer function (pure math, zero deps)
- [x] Step Response (FFT + Time Domain) — pure numerical computation module

#### Output Layer
- [x] `OutputFormatter` — terminal (Rich) + Markdown output
- [x] ParamRef → platform parameter name translation (via `adapter.map_param_to_platform()`)
- [x] `FullAnalysisResult` — combined result container + summary
- [x] HTML Report — self-contained HTML report with PID/FFT/MagFit/Filter/SysID/Hardware sections (v2.4.2)

#### CLI
- [x] `stune analyze` — comprehensive analysis (PID + FFT + MagFit)
- [x] `stune pid` — PID step response
- [x] `stune fft` — FFT vibration spectrum
- [x] `stune magfit` — magnetometer calibration
- [x] `stune sysid` — system identification
- [x] `stune hardware` — hardware configuration report
- [x] `stune platforms` — list supported platforms
- [x] `--platform auto|ardupilot|betaflight|px4` global flag
- [x] Auto log format detection (magic bytes + extension)

#### Tests
- [x] 40 tests all passing
- [x] Architecture tests (models/registry/knowledge/error system)
- [x] PID analyzer tests (synthetic signals/empty data/knowledge coverage/ParamRef)
- [x] FFT analyzer tests (basic analysis/frequency detection/insufficient data/spectrum data)
- [x] End-to-end integration tests (synthetic data full pipeline/cross-platform param translation)
- [x] Output formatter tests (ArduPilot/Betaflight/PX4 param translation/Markdown)

---

### Phase 2: Betaflight Support (v2.0)

#### BBL Parser Implementation
- [x] BBL header parsing (H line key-value pairs)
- [x] Frame definition parsing (Field I name / Field P name / Field S name)
- [x] I-frame decoding (keyframes, full values)
- [x] P-frame decoding (delta frames, signed/unsigned VB variable-length encoding + predictor)
- [x] S-frame decoding (slow frames, GPS etc. low-rate data)
- [x] E-frame decoding (event frames, mode switches)
- [x] Field mapping to FlightData:
  - `setpoint[0/1/2]` → `pid.{roll/pitch/yaw}.desired`
  - `gyroADC[0/1/2]` → `pid.{roll/pitch/yaw}.actual` + `gyro`
  - `axisP/I/D/F[0/1/2]` → `pid.{axis}.p_term/i_term/d_term/ff_term`
  - `motor[0-7]` → `motor_output` (normalized 0-1)
  - `accSmooth[0/1/2]` → `accel` (converted to m/s²)
- [x] Multi-segment log handling (single .bbl may contain multiple flights)
- [x] Flight mode flag decoding (ARM/ANGLE/HORIZON/ACRO/FAILSAFE)
- [x] 34 BBL parser unit tests (roundtrip encoding/header parsing/frame decoding/adapter integration)

#### Betaflight Knowledge Base
- [x] PID rule adaptation (d_min/d_max, feedforward as independent term, anti_gravity, model presets)
- [x] Filter rules (RPM filter, dynamic notch, gyro_lpf1/2, dterm_lpf, filter chain)
- [x] Typical model thresholds (5" freestyle / 3" cinewhoop / 7" long range / toothpick)

#### Betaflight-Specific Analyzers
- [x] Feedforward analyzer (FF contribution/overshoot detection/tracking error)
- [x] RPM Filter efficiency assessment (motor peak detection/noise attenuation quantification)
- [x] D-term noise analysis (D/P ratio/d_min activation ratio/high-frequency energy ratio)
- [x] 22 BF analyzer + knowledge base tests

#### Validation
- [ ] Verify parsing accuracy against real BF logs (cross-reference with Blackbox Explorer)
- [ ] Compare PID analysis with Blackbox Explorer's Step Response
- [ ] Compare FFT analysis with Betaflight's built-in spectrum

---

### Phase 2.x: PX4 Support

- [ ] pyulog integration (`pip install pyulog`)
- [ ] ULog → FlightData mapping:
  - `vehicle_angular_velocity` → gyro
  - `rate_ctrl_status` → PID signals
  - `vehicle_magnetometer` → mag
- [ ] PX4 parameter mapping completion (MC_ROLLRATE_* etc.)
- [ ] PX4 knowledge base rules

---

### Phase 3: Advanced Features (v3.0)

- [ ] Cross-platform comparison (same airframe, different firmware tuning comparison)
- [ ] Skill / Agent orchestration layer adaptation for new architecture
- [ ] SKILL-PRO visual review workflow adaptation
- [ ] Web UI (optional, extend from existing HTML reports)
- [ ] Plugin system (third-party platform adapter registration)

---

### Technical Debt

- [x] HTML Report adaptation for new result types — added Filter, SysID, Hardware sections (v2.4.2)
- [x] `derive_filters_from_params()` — moved AP-specific logic to platform/ardupilot/, core layer now platform-agnostic (v2.4.2)
- [x] Old visualization (matplotlib plot) code in `_legacy_formatter.py` — file already removed, ROADMAP entry stale (v2.4.2)
- [x] Clean up residual `ap_tune` references in arx_model.py / wmm.py docstrings (2026-05-03)
- [x] Rename `ap-tune` → `SmartTune CLI` in html_report.py (2026-05-03)
- [x] CLI `--format json` — parity with MCP schema, shared services-layer payload (v3.1)
- [x] Parameter tables regenerated from upstream metadata — full firmware names, parameter
      groups, @Values/@Bitmask meanings, real PX4 defaults; scraper now in-repo
      (`tools/build_param_tables.py`) with a data linter (`stune params --lint`) (v3.2)
- [x] `validate()` no longer accepts any value for enum-typed parameters — real member/bitmask
      checking, fail-closed on unverifiable rows (v3.2)
- [x] MCP payload slimming + unified `{ok, error_code, retryable}` shape + stdout isolation +
      lazy numpy import for parameter tools (v3.2)
- [x] Inline recommendation validation (every recommendation ships `validated`/`validation_status`), batch validate (CLI `--validate-batch` + MCP `smarttune_validate_params`), `analyze --modules`/`--max-recommendations`, envelope `verdict` contract (v3.2.1)
- [x] Firmware-version parameter tables — `<platform>.<fw>.json`, CLI `--fw-version`, MCP `fw_version` on all six parameter tools, builder `--fw-tag`; new ArduPilot Copter-4.5 table (4,121 params) (v3.3)
- [x] `knowledge/params/*.json` missing from wheel `package-data` (v3.1)
- [ ] CI/CD setup (GitHub Actions: lint + test + build)
- [ ] PyPI publishing config

---

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Project name | SmartTune CLI (`stune`) | Platform-neutral, not tied to any FC brand |
| BBL parsing | Pure Python in-house | Zero external deps, aligns with "offline zero-cloud" philosophy |
| ULog parsing | pyulog library | PX4-official, mature enough |
| Abstraction layer | FlightData common denominator + extras slots | Avoid information loss through over-abstraction |
| Knowledge base | Per-platform directories, 6-level deep_merge | Flexible coverage, Pro layer is non-intrusive |
| Parameter reference | ParamRef generic → adapter translation | Analysis engine completely platform-agnostic |
| Safety principle | Conservative tuning, single-step ≤20%, read-only suggestions | Flight safety first |
| License | MIT (CLI) + Proprietary (Pro) | Open-source core + closed-source value-add |
