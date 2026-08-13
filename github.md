repo: raylanlin/smarttune-cli
branch: main

## Upstream data sources

These repositories are read (not vendored) to generate `smarttune/knowledge/params/*.json`
via `tools/build_param_tables.py`:

- raylanlin/ParameterRepository @main — `Copter-4.1/apm.pdef.json` (ArduPilot metadata)
- raylanlin/PX4-Autopilot @main `0bb36a30ed43` — `docs/public/config/failsafe/parameters.json`
- raylanlin/betaflight @master `96d612914267` — `src/main/cli/settings.c`, `src/main/fc/parameter_names.h`, `src/main/cli/settings.h` + bound headers

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
