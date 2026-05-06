# SmartTune Migration Guide

## ap-tune → smarttune Module Migration Map

This document maps the module migration from `ap-tune` (ArduPilot-only) to `smarttune` (multi-platform),
to facilitate incremental migration of existing analysis logic.

### Module Mapping

| Old Module (ap_tune) | New Location (smarttune) | Status | Notes |
|---|---|---|---|
| `cli.py` | `smarttune/cli.py` | ✅ Rewritten | Added `--platform`, PID+FFT wired up |
| `errors.py` | `smarttune/errors.py` | ✅ Rewritten | Base class renamed to `SmartTuneError` |
| `log_parser.py` | `smarttune/platform/ardupilot/` | ✅ Migrated | Split into adapter's `parse()` method |
| `pid_reviewer.py` | `smarttune/analyzers/pid_reviewer.py` | ✅ Migrated | Accepts FlightData, outputs ParamRef |
| `fft_analyzer.py` | `smarttune/analyzers/fft_analyzer.py` | ✅ Migrated | Accepts FlightData, no LogParser dependency |
| `step_response_fft.py` | `smarttune/analyzers/step_response_fft.py` | ✅ As-is | Pure numerical computation, unmodified |
| `step_response_time_domain.py` | `smarttune/analyzers/step_response_td.py` | ✅ As-is | Pure numerical computation, unmodified |
| `magfit.py` | `smarttune/analyzers/magfit.py` | ✅ Migrated | Accepts FlightData, CLI pending wiring |
| `filter_transfer.py` | `smarttune/analyzers/filter_transfer.py` | ✅ As-is | Pure math, no LogParser dependency |
| `filter_visualization.py` | `smarttune/output/filter_visualization.py` | ✅ Import fixed | |
| `sysid_analyzer.py` | `smarttune/analyzers/sysid_analyzer.py` | ✅ Migrated | Accepts FlightData |
| `hardware_report.py` | `smarttune/analyzers/hardware_report.py` | ✅ Migrated | Accepts FlightData |
| `output.py` | `smarttune/output/formatter.py` | ✅ Rewritten | Parameter name translation via PlatformAdapter |
| `html_report.py` | `smarttune/output/html_report.py` | 🔲 Needs adaptation | Old code available as reference |
| `knowledge/__init__.py` | `smarttune/knowledge/loader.py` | ✅ Rewritten | Added platform dimension |
| `knowledge/rules/*.json` | `smarttune/knowledge/rules/ardupilot/` | ✅ Migrated | |

### Migrating Each Analyzer

Using `pid_reviewer.py` as an example:

1. **Change input signature**: `__init__(self, parser: LogParser, knowledge)` → `__init__(self, knowledge)`
2. **Change analyze signature**: `analyze(axis=None)` → `analyze(flight_data: FlightData, axis=None)`
3. **Replace data access**:
   - `parser.get_pid_data("roll")` → `flight_data.pid["roll"]`
   - `parser.get_parameters()` → `flight_data.params`
   - `parser.get_imu_data()` → `flight_data.gyro` / `flight_data.accel`
4. **Replace parameter names**: hardcoded `"ATC_RAT_RLL_P"` → `ParamRef("pid.roll.p")`
5. **Replace output**: `Recommendation(param="ATC_RAT_RLL_P", ...)` → `ParamRecommendation(param=ParamRef("pid.roll.p"), ...)`
6. **Test**: Verify output consistency against existing ArduPilot logs

### New Files (non-migration)

| File | Description |
|---|---|
| `smarttune/models/flight_data.py` | FlightData / AxisPIDSignal / ModeChange definitions |
| `smarttune/models/analysis_result.py` | ParamRef / all Result type definitions |
| `smarttune/platform/base.py` | PlatformAdapter abstract base class |
| `smarttune/platform/registry.py` | Platform registration + auto-detection |
| `smarttune/platform/betaflight/` | BF BBL adapter (Phase 2) |
| `smarttune/platform/px4/` | PX4 ULog adapter (Phase 3) |
| `smarttune/knowledge/rules/common/` | Cross-platform common rules |
| `smarttune/knowledge/rules/betaflight/` | BF-specific rules |

### Pro Knowledge Base Migration

`smarttune-knowledge-pro` requires:
1. Package rename from `ardupilot_knowledge_pro` → `smarttune_knowledge_pro`
2. `load()` signature change to `load(platform: str = None) -> Dict`
3. Return structure: `{"common": {...}, "ardupilot": {...}, "betaflight": {...}}`
4. Rule directories organized by platform subdirectory
