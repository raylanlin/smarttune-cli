# Changelog

All notable changes to SmartTune CLI will be documented in this file.

## [2.3.1] — 2026-05-19

### Added
* Added a dedicated `skill-mcp/` package for read-only MCP-connected agents that must not use shell, `exec`, or the `stune` CLI.
* Documented MCP-only agent workflow, supported log extensions, tool selection, and safety boundaries for customer-support deployments.

---

## [2.3.0] — 2026-05-19

### Added
- **Read-only MCP server** — `smarttune-mcp` exposes SmartTune analysis to OpenClaw and other MCP clients without shell access.
- `smarttune_list_platforms` MCP tool — reports CLI-supported extensions and MCP-accepted extensions separately.
- `smarttune_log_quality` MCP tool — returns structured log quality, data availability, and validation metadata.
- `smarttune_analyze_log` MCP tool — returns compact JSON or Markdown analysis for PID, FFT, MagFit, and hardware modules.
- Pure service layer under `smarttune/services/` for analysis and JSON-safe serialization.
- MCP security tests covering extension allowlists, path traversal, symlink escape, and file size limits.

### Security
- MCP path validation resolves symlinks with `resolve(strict=True)`, enforces allowed roots, and rejects arbitrary file extensions.
- MCP tools do not expose subprocess, shell execution, arbitrary output paths, parameter writes, or filesystem mutation.
- Betaflight `.txt` logs remain CLI-supported but are intentionally excluded from the MCP allowlist.

### Fixed
- Fixed `smarttune/analyzers/magfit.py` indentation so MagFit imports cleanly.
- Hardware analysis service now uses the existing `generate_hardware_report()` function path.
- Runtime `SMARTTUNE_MCP_MAX_FILE_MB` changes are respected during path validation.

---

## [2.0.0] — 2026-05-03

### Added
- **Multi-platform architecture** — Unified `FlightData` intermediate representation
- **Platform Adapter Layer** — `PlatformAdapter` ABC + auto-detection + registry
- **Betaflight support** — Pure Python BBL parser (950 lines), FF/RPM/D-term noise analyzers
- **ParamRef system** — Generic parameter names → platform-native mapping
- **6-layer Knowledge Base** — common → platform → user common → user platform → Pro common → Pro platform
- **Error code system** — E10xx (file) / E20xx (parse) / E30xx (data) / E40xx (input) / E50xx (analysis)
- `stune platforms` command — list available adapters
- `stune filter --auto` — auto-derive filters from params
- Betaflight-specific analyzers: `FeedforwardAnalyzer`, `RPMFilterAnalyzer`, `DTermNoiseAnalyzer`

### Changed
- **CLI renamed** — `ap-tune` → `stune`
- **Package renamed** — `ap_tune` → `smarttune`
- **Architecture restructured** — flat module layout → layered (platform/analyzers/knowledge/output/models)
- Knowledge loader now accepts `platform` parameter for platform-specific rules
- Pro knowledge base renamed and restructured: `ardupilot-knowledge-pro` → `smarttune-knowledge-pro` v0.2.0

### Removed
- `--vision` option (MiniMax CLI integration not included in open-source version)
- `output/` directory with cached analysis images from old repo

---

## [1.0.0] — 2026-04-29

### Added
- Full ArduPilot WebTools algorithm alignment (PIDReview, FilterReview, HardwareReport, MAGFit)
- WMM 2020 geomagnetic model (19×37 grid + bilinear interpolation)
- SysID module (ARX system identification)
- Filter analysis (Bode plot, multi-notch, harmonic notch)
- Hardware report (IMU, compass, barometer, GPS, battery)
- Layered knowledge base (builtin → user)
- 309 tests, 0 xfail

### Changed
- Open-source restructuring: knowledge in `rules/` subdirectory, user loading from `~/.ap-tune/knowledge/`

---

## [0.1.0-beta] — 2026-04-07

### Added
- Initial release: PID reviewer, FFT analyzer, MagFit, log parser
- Built-in knowledge base (pid_rules, filter_rules, magfit_rules)
- 7 CLI commands: analyze, pid, fft, filter, sysid, hardware, magfit
- 159 tests
