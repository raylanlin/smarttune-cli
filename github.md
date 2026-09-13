repo: raylanlin/smarttune-cli
branch: main

## Upstream data sources

These repositories are read (not vendored) to generate `smarttune/knowledge/params/*.json`
via `tools/build_param_tables.py`:

- raylanlin/ParameterRepository @main — `Copter-4.1/apm.pdef.json` (ArduPilot metadata)
- raylanlin/PX4-Autopilot @main `0bb36a30ed43` — `docs/public/config/failsafe/parameters.json`
- raylanlin/betaflight @master `96d612914267` — `src/main/cli/settings.c`, `src/main/fc/parameter_names.h`, `src/main/cli/settings.h` + bound headers

## Pending in this package (v3.3.0, not yet pushed)

- Firmware-version parameter tables: `ParamTable.from_knowledge(platform, fw_version)`, CLI `--fw-version`, MCP `fw_version` on all six parameter tools (unknown version → E4011), builder `--fw-tag`
- New table `ardupilot.copter-4.5.json` (4,121 params / 243 groups) from Copter-4.5/Parameters.md
- MCP validate_param: deprecated `status` alias removed (v3.2.1 promise)
- Docs synced: README (4.5 table row + fw_version contract), ROADMAP (v3.2.1/v3.3 marked done, Web UI remains the only open item), skill/SKILL.md + skill-mcp/SKILL.md (fw_version, verdict wording)
- Deferred by owner decision: Web UI; builder-native Parameters.md parser lands v3.3.1 (see docs/TEST_PLAN_v3.3.md)

## Pending in this package (v3.3.1, not yet pushed)

- Search folding: numbered-instance clones (BATT2_..BATT9_) collapse into their base with an `instances` list — shared `collapse_numbered()` in params.py, wired into CLI `--search` and MCP `smarttune_search_params`
- Truncation contract: `returned < count` always carries `truncated: true` + note; blocks report `raw_count` vs distinct `count`
- New tests/test_search_collapse.py (7 cases); TEST_PLAN_v3.3.md §E added
- Note for deployer: default tables ardupilot.json / px4.json are NOT in this package (size-capped on export) — keep the repo's existing copies; only betaflight.json + ardupilot.copter-4.5.json ship here unchanged

## Pending in this package (v3.4.0, not yet pushed)

- New `smarttune/platform/ardupilot/tlog_parser.py`: MAVLink telemetry (.tlog) parser handled inside the ArduPilot adapter (dispatch on extension — no new platform, all existing analyzer wiring untouched)
- PID reconstructed from ATTITUDE_TARGET vs ATTITUDE (P/I/D zero-filled, skipped with a stated reason when no target stream); gyro/accel/mag from RAW_IMU/SCALED_IMU*/HIGHRES_IMU with ATTITUDE fallback
- Honesty layer: `extras["telemetry_notes"]` + `extras["log_source"]` surfaced through quality (scored lower, advice points at the .bin), analyze (terminal block), and JSON/MCP payloads
- PX4-sourced .tlog refused with a pointer to the onboard .ulg; `.tlog` added to the MCP path validator
- New tests/test_tlog_parser.py (21 cases), docs/TEST_PLAN_v3.4.md; README + both SKILL.md files updated
- Aligned with ArduPilot's reference tooling (UAVLogViewer + WebTools StreamStats/PIDReview), full table in docs/ALIGNMENT_ARDUPILOT_WEBTOOLS.md:
  - step response output signal was IMU.Gyr, reference uses PIDx.Act → default changed (ONLY intentional numeric change; affects .bin too), off-by-one in the SNR cutoff bin fixed, cutoff clamped at low sample rates
  - tlog: GCS heartbeats no longer mistaken for the vehicle, mode maps keyed by MAV_TYPE (Copter/Plane/Rover/Sub/Tracker + base_mode fallback), vehicle clock (time_boot_ms) as the timeline, per-srcSystem isolation + sequence-number drop counting, param-id sanitising, AHRS2 fallback, STATUSTEXT severity
- TEST_PLAN §E: everything except PID step response must stay byte-identical vs v3.3.1; §E0 covers the intentional step-response change (cross-check against the PIDReview web tool)
- Note for deployer: default tables ardupilot.json / px4.json are NOT in this package (export size cap) — keep the repo's existing copies

## Pending in this package (v3.5.0, not yet pushed)

- Betaflight aligned against the firmware + blackbox-log-viewer + PID-Analyzer; full table in docs/ALIGNMENT_BETAFLIGHT.md
  - FIXED (data corruption): `blackbox_high_resolution` (BF 4.4+, x10 on gyroADC/gyroUnfilt/rcCommand/setpoint) was ignored — signals read 10x high and the outlier sanitiser then interpolated away everything above ~220 deg/s real
  - CHANGED (numbers move): BF step response had NO window function; PID-Analyzer uses np.hanning(flen). BF now delegates to the same verified kernel as ArduPilot with BF's [20, 500] deg/s gate (= PID-Analyzer low-input response)
  - VERIFIED CORRECT: gyro needs no `gyro_scale` — firmware writes 1.0f because gyroADC is already deg/s
  - ADDED: extras["gyro_unfiltered"] (pre-filter trace for notch targeting), extras["blackbox_info"]
- New tests/test_bf_alignment.py (14 cases), docs/TEST_PLAN_v3.5.md
- TEST_PLAN §D: ArduPilot / PX4 / tlog must stay byte-identical vs v3.4.0 (shared-kernel change is opt-in only)
- Note for deployer: default param tables ardupilot.json / px4.json are NOT in this package (export size cap) — keep the repo's existing copies

## Last sync

date: 2026-08-12T16:05:00Z
tree: 029577673a08

### Updated in this project

- v3.2: regenerated all three parameter tables from upstream metadata — full firmware names, parameter groups, @Values/@Bitmask meanings, real PX4 defaults; added `tools/build_param_tables.py` (the previously missing scraper) and `smarttune/platform/param_lint.py`
- v3.2: `validate()` no longer accepts any value for enum-typed parameters — real member/bitmask checks, fail-closed `unverifiable` status
- v3.2: MCP payload slimming (`list_param_groups` / `get_param` added, 15 tools), unified `{ok, error_code, retryable}` shape, stdout isolation, lazy numpy import for parameter tools
- v3.2.1 (this package, pending push): inline recommendation validation in services layer; batch validate (CLI --validate-batch + MCP smarttune_validate_params, 16 tools); analyze --modules/--max-recommendations; envelope status ok/error only with domain verdict field; dict-form FFT recommendations now reach JSON+Markdown reports; friendly smarttune-mcp error on Python 3.9; docs/TEST_PLAN_v3.2.1.md
- v3.2: added `docs/TEST_PLAN_v3.2.md` (executable acceptance spec) and `tools/smoke_mcp.py` (MCP stdio contract smoke test)
- v3.1: added `smarttune/output/json_output.py` and `-f/--format json` across all 10 CLI commands, sourced from the services layer (same payloads the MCP server returns)

## Screen map

| Area | Repo files |
|------|-----------|
| CLI commands, `--format`, `params` browse/search/validate/lint | `smarttune/cli.py` |
| JSON output layer | `smarttune/output/json_output.py` |
| Parameter tables (generated data) | `smarttune/knowledge/params/{ardupilot,betaflight,px4}.json` |
| Parameter table loader + validation | `smarttune/platform/params.py` |
| Parameter data linter | `smarttune/platform/param_lint.py` |
| Table generator (scraper) | `tools/build_param_tables.py` |
| MCP server (15 tools, unified envelopes) | `smarttune/mcp_server.py` |
| Lazy platform package import | `smarttune/platform/__init__.py` |
| Shared payloads (CLI + MCP) | `smarttune/services/analysis.py`, `smarttune/services/serialize.py` |
| Tests | `tests/test_cli_json.py`, `tests/test_param_tables.py` |
| Release verification | `docs/TEST_PLAN_v3.2.md`, `tools/smoke_mcp.py` |
| Docs | `README.md`, `CHANGELOG.md`, `docs/ROADMAP.md`, `skill/SKILL.md`, `skill-mcp/SKILL.md` |

## Notes

- Not run here (no Python runtime in this environment): `pytest -q`, `ruff check smarttune/`,
  and `python tools/build_param_tables.py --check`. The generated tables were produced by an
  equivalent transform and verified against the linter's rules (0 errors on all three tables);
  re-run the builder locally to confirm byte-for-byte reproducibility.
