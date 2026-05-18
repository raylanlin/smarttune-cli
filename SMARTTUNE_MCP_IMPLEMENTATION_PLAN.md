# SmartTune MCP Implementation Plan

Prepared for: Claude one-shot implementation
Date: 2026-05-18
Repository: `smarttune-cli`

## Goal

Add a safe Model Context Protocol server to SmartTune so OpenClaw customer-service agents can analyze user-uploaded flight logs without receiving `exec`, shell, or broad filesystem write permissions.

The result should let an OpenClaw agent call narrow, read-only SmartTune tools such as:

- list supported flight-controller platforms
- inspect flight-log quality
- run PID/FFT/MagFit analysis
- optionally compare two logs in a later iteration

The MCP server must not expose arbitrary command execution, arbitrary file writing, or parameter-writing capabilities.

## Current Repository State

SmartTune is a Python package with:

- CLI entry point: `stune = smarttune.cli:main`
- CLI implementation: `smarttune/cli.py`
- platform registry and adapters: `smarttune/platform/`
- analyzers: `smarttune/analyzers/`
- output rendering: `smarttune/output/`
- result dataclasses: `smarttune/models/analysis_result.py`
- unified input model: `smarttune/models/flight_data.py`
- tests: `tests/`

The CLI already does most of the work in library calls:

1. `resolve_adapter(platform_name, log_file)`
2. `adapter.parse(log_file)`
3. `KnowledgeBase(platform=adapter.name)`
4. call analyzers directly
5. render through `OutputFormatter`

This is good for MCP because the server can call the library layer directly instead of invoking `stune` through shell subprocesses.

## Important Existing Mismatch

`README.md` currently advertises commands like:

```bash
stune analyze -i flight.bin --format json
stune analyze -i flight.bin --format markdown -o report.md
```

But `smarttune/cli.py` currently defines:

```python
@click.option("--report", type=click.Choice(["md", "html"]))
```

There is no visible `--format json` path in the current CLI code. The MCP implementation should avoid depending on the CLI output path and should introduce a shared serializer/API that both MCP and future CLI JSON output can use.

## Recommended Architecture

Add three layers:

1. `smarttune/services/analysis.py`
   - Pure library functions that parse logs and return structured dataclasses/dicts.
   - No Rich console output.
   - No shell execution.
   - No arbitrary writes.

2. `smarttune/services/serialize.py`
   - Converts dataclasses, enums, NumPy arrays/scalars, and recommendation objects into JSON-safe dictionaries.
   - Provides compact output suitable for LLM agents.

3. `smarttune/mcp_server.py`
   - FastMCP stdio server.
   - Exposes read-only tools wrapping service functions.
   - Validates paths, size, extension, and output behavior.

This keeps CLI and MCP from drifting. CLI can later reuse `services.analysis` and `services.serialize`, but the initial patch does not have to fully refactor the CLI if that would expand the change too much.

## New Dependencies

Add MCP as an optional extra so normal CLI installs stay light:

```toml
[project.optional-dependencies]
mcp = ["mcp>=1.0.0", "pydantic>=2.0.0"]
```

Keep existing extras intact:

```toml
all = ["pymavlink>=2.4.0", "pyulog>=1.0.0"]
dev = [...]
```

Recommended install for OpenClaw:

```bash
pip install -e ".[all,mcp]"
```

Add optional script:

```toml
[project.scripts]
stune = "smarttune.cli:main"
smarttune-mcp = "smarttune.mcp_server:main"
```

If adding a second script is undesirable, `python -m smarttune.mcp_server` is enough, but the script makes OpenClaw config cleaner.

## MCP Tools

### 1. `smarttune_list_platforms`

Purpose:
Return supported platforms, extensions, and capabilities.

Inputs:
None.

Output:
JSON object:

```json
{
  "platforms": [
    {
      "name": "ardupilot",
      "display_name": "ArduPilot",
      "extensions": ".bin, .log",
      "capabilities": ["pid", "fft", "magfit", "hardware", "filter"]
    }
  ]
}
```

Implementation:
Use `smarttune.platform.registry.list_platforms()`.

Annotations:

```python
readOnlyHint=True
destructiveHint=False
idempotentHint=True
openWorldHint=False
```

### 2. `smarttune_log_quality`

Purpose:
Parse a log and return whether it is good enough for analysis.

Inputs:

- `log_path: str`
- `platform: Literal["auto", "ardupilot", "betaflight", "px4"] = "auto"`

Output:
JSON object:

```json
{
  "platform": "ardupilot",
  "display_name": "ArduPilot",
  "log_file": "flight.bin",
  "file_size_mb": 12.4,
  "duration_s": 321.0,
  "sample_rate_hz": 400.0,
  "axes": ["pitch", "roll", "yaw"],
  "has_gyro": true,
  "has_accel": true,
  "has_mag": true,
  "has_motor": true,
  "has_battery": true,
  "validation_issues": [],
  "quality": {
    "score": 92,
    "rating": "EXCELLENT",
    "advice": "Proceed with analysis"
  }
}
```

Implementation:
Do not copy the Rich/report-building code from `quality()` blindly. Move the scoring logic into a service helper, then let both CLI and MCP use it if time permits. For a one-shot MCP implementation, it is acceptable to implement a small service-quality function first and leave CLI unchanged.

### 3. `smarttune_analyze_log`

Purpose:
Run comprehensive log analysis and return compact structured recommendations.

Inputs:

- `log_path: str`
- `platform: Literal["auto", "ardupilot", "betaflight", "px4"] = "auto"`
- `axis: Literal["all", "roll", "pitch", "yaw"] = "all"`
- `include_modules: list[Literal["pid", "fft", "magfit", "hardware", "filter"]] | None = None`
- `response_format: Literal["json", "markdown"] = "json"`
- `max_recommendations: int = 20`

Output for JSON:

```json
{
  "platform": "ardupilot",
  "display_name": "ArduPilot",
  "log_file": "flight.bin",
  "duration_s": 321.0,
  "modules": {
    "pid": {
      "overall_assessment": "GOOD",
      "axes": {
        "roll": {
          "assessment": "GOOD",
          "step_count": 8,
          "metrics": {
            "rise_time_ms": 85.0,
            "overshoot_percent": 8.2,
            "settling_time_ms": 210.0
          },
          "recommendations": [
            {
              "param": "ATC_RAT_RLL_P",
              "generic_param": "pid.roll.p",
              "current": 0.12,
              "suggested": 0.14,
              "change_percent": 16.7,
              "action": "increase",
              "confidence": "medium",
              "reason": "Slightly slow rise time"
            }
          ]
        }
      }
    },
    "fft": {
      "vibration_level": "GOOD",
      "peaks": [
        {
          "frequency_hz": 47.5,
          "amplitude": 2.1,
          "source_guess": "propeller"
        }
      ],
      "recommendations": []
    },
    "magfit": {
      "assessment": "GOOD",
      "fitness_mgauss": 23.4,
      "offsets": {"x": 1.0, "y": -3.0, "z": 5.0},
      "recommendations": []
    }
  },
  "module_failures": [],
  "safety": {
    "read_only": true,
    "path_validated": true,
    "parameter_write_performed": false
  }
}
```

Output for Markdown:
Return a concise report with:

- platform and log metadata
- quality summary
- PID diagnosis and parameter recommendations
- FFT peaks and filter recommendations
- MagFit result
- module failures
- note that no parameters were written

### 4. `smarttune_compare_logs` (optional, can defer)

Purpose:
Compare before/after logs for tuning progress.

Inputs:

- `before_log_path`
- `after_log_path`
- `platform = "auto"`
- `axis = "all"`

Output:
JSON/Markdown delta:

- PID metrics before/after
- vibration peaks before/after
- warnings if logs are not comparable

This should be deferred unless time remains. The first three tools are enough for OpenClaw customer support.

## Security Requirements

This is the most important part.

### No shell execution

Do not call:

- `subprocess`
- `os.system`
- shell commands
- `stune` CLI as a child process

Call Python library functions only.

### Path validation

Create `smarttune/security.py` or keep helper functions in `mcp_server.py`.

Allow roots should default to:

- current working directory
- `~/.openclaw/workspace/files/inbox`
- `~/.openclaw/workspace/files/output`
- `~/.openclaw/media`
- `/tmp`

Allow override via environment:

```bash
SMARTTUNE_MCP_ALLOWED_ROOTS="/path/a:/path/b"
SMARTTUNE_MCP_MAX_FILE_MB="300"
```

Validation steps:

1. Expand `~`.
2. Resolve symlinks: `Path(path).expanduser().resolve(strict=True)`.
3. Check it is a file.
4. Check suffix in `{".bin", ".log", ".bbl", ".bfl", ".ulg"}`.
5. Check resolved path is under an allowed root using `Path.is_relative_to` or safe fallback for Python 3.9.
6. Check size <= configured limit.
7. Never return full absolute paths in user-facing output unless needed. Prefer basename plus safe metadata.

Reject examples:

- `../../.ssh/id_rsa`
- `/etc/passwd`
- non-log extensions
- paths outside allowed roots
- directories
- symlink escaping allowed root

### Output safety

MCP should return data directly. It should not create files by default.

If future image/HTML output is needed:

- only write under a fixed output directory
- generate server-side filenames
- never accept arbitrary output path from the model

### Timeout and resource control

MCP calls can parse large logs. Add at least one guard:

- file size limit first
- optional `SMARTTUNE_MCP_TIMEOUT_S`
- avoid expensive visual plot generation in MCP v1

Python cannot hard-timeout CPU work cleanly inside the same process without signal/process isolation. For v1, size limit is acceptable. Add TODO for process isolation if customer logs become huge.

### Tool annotations

All MCP tools must mark:

```python
readOnlyHint=True
destructiveHint=False
idempotentHint=True
openWorldHint=False
```

## Implementation Steps

### Step 1: Add serializers

Create `smarttune/services/serialize.py`.

Implement:

- `to_jsonable(value)`
- dataclass handling via `dataclasses.asdict` or explicit conversion
- Enum `.value`
- NumPy scalar `.item()`
- NumPy array truncation/summarization; do not return raw long arrays
- `serialize_param_recommendation(rec, adapter=None)`
- `serialize_pid_result(result, adapter)`
- `serialize_fft_result(result, adapter)`
- `serialize_magfit_result(result, adapter)`

Important:
Do not dump `raw_data`, `step_responses`, or full FFT arrays into MCP by default. Those can be huge.

### Step 2: Add analysis service

Create `smarttune/services/analysis.py`.

Suggested public functions:

```python
def load_flight_data(log_path: Path, platform: str = "auto") -> tuple[PlatformAdapter, FlightData]:
    ...

def get_log_quality(log_path: Path, platform: str = "auto") -> dict:
    ...

def analyze_log(
    log_path: Path,
    platform: str = "auto",
    axis: str = "all",
    include_modules: list[str] | None = None,
    max_recommendations: int = 20,
) -> dict:
    ...
```

Use the same analyzer calls currently in `smarttune/cli.py`:

- PID: `PIDReviewer(knowledge=kb.get("pid_rules", {})).analyze(...)`
- FFT: `FFTAnalyzer(knowledge=kb.get("filter_rules", {})).analyze(...)`
- MagFit: `MAGFit(knowledge=kb.get("magfit_rules", {})).analyze(...)`
- Hardware: platform hardware report module if requested
- Filter: only if there is already clean service logic; otherwise defer

Module behavior:

- If one module fails, record it in `module_failures`.
- If every requested module fails, return an actionable error or raise `SmartTuneError`.
- Do not exit the process.
- Do not print Rich output.

### Step 3: Add MCP server

Create `smarttune/mcp_server.py`.

Skeleton:

```python
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

mcp = FastMCP("smarttune_mcp")

class AnalyzeLogInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    log_path: str = Field(..., description="Path to a flight log file...")
    platform: str = Field(default="auto", description="auto, ardupilot, betaflight, px4")
    axis: str = Field(default="all", description="all, roll, pitch, yaw")
    include_modules: list[str] | None = Field(default=None)
    response_format: str = Field(default="json")
    max_recommendations: int = Field(default=20, ge=1, le=100)
```

Use Pydantic validators for enum-like fields, or use `Literal` if compatible.

Tool functions should return strings:

- JSON: `json.dumps(result, ensure_ascii=False, indent=2)`
- Markdown: render a compact Markdown report from the result dict

End with:

```python
def main() -> None:
    mcp.run()

if __name__ == "__main__":
    main()
```

### Step 4: Wire pyproject

Update:

- optional dependency `mcp`
- script `smarttune-mcp`

### Step 5: Tests

Add tests:

1. `tests/test_mcp_security.py`
   - allowed extension accepted
   - disallowed extension rejected
   - path outside allowed roots rejected
   - symlink escape rejected if easy to implement
   - oversized file rejected

2. `tests/test_services_serialize.py`
   - dataclass to JSON-safe conversion
   - NumPy scalar conversion
   - NumPy arrays are summarized/truncated
   - recommendations include generic and platform-specific parameter names

3. `tests/test_services_analysis.py`
   - use existing synthetic fixtures/helpers if available
   - ensure `analyze_log` returns top-level keys and does not raise on missing optional modules

4. Optional smoke test for MCP import:
   - `python -m py_compile smarttune/mcp_server.py`
   - import `smarttune.mcp_server`

Do not require real `.bin`/`.bbl` logs for unit tests unless fixtures already exist.

### Step 6: Documentation

Update `README.md`:

- Add MCP section under "For Agents".
- Explain read-only safety boundary.
- Add OpenClaw config example:

```json
{
  "mcp": {
    "servers": {
      "SmartTune": {
        "command": "smarttune-mcp",
        "args": [],
        "env": {
          "SMARTTUNE_MCP_ALLOWED_ROOTS": "/home/raylan/.openclaw/workspace/files/inbox:/home/raylan/.openclaw/workspace/files/output:/tmp",
          "SMARTTUNE_MCP_MAX_FILE_MB": "300"
        }
      }
    }
  }
}
```

If `smarttune-mcp` script is not added, use:

```json
{
  "command": "python",
  "args": ["-m", "smarttune.mcp_server"]
}
```

Also fix the `--format json` mismatch:

Option A:
- Implement CLI `--format json|markdown|html` now.

Option B:
- Adjust README to current `--report md|html` and document MCP as the JSON interface for now.

Recommended:
Implement a small JSON output path in CLI using the new serializer if time permits. If this expands too much, leave a clear TODO and at least stop advertising a nonexistent flag.

## Acceptance Criteria

The implementation is complete when:

1. `pip install -e ".[all,mcp,dev]"` succeeds.
2. `smarttune-mcp` starts as a stdio MCP server.
3. The MCP server exposes:
   - `smarttune_list_platforms`
   - `smarttune_log_quality`
   - `smarttune_analyze_log`
4. No MCP tool can execute arbitrary shell commands.
5. No MCP tool accepts arbitrary output paths.
6. Path validation blocks files outside allowed roots.
7. Log analysis returns structured JSON without Rich formatting or ANSI sequences.
8. Module failures are returned as structured data instead of crashing the server.
9. Existing CLI tests still pass.
10. New MCP/security/serializer tests pass.

## Suggested Verification Commands

```bash
cd smarttune-cli
python -m py_compile smarttune/mcp_server.py
python -m py_compile smarttune/services/analysis.py smarttune/services/serialize.py
pytest -q
pip install -e ".[all,mcp,dev]"
smarttune-mcp --help || true
```

If `smarttune-mcp --help` is not meaningful because stdio servers wait for MCP input, just verify import/compile and use MCP Inspector if available:

```bash
npx @modelcontextprotocol/inspector smarttune-mcp
```

## Non-Goals for This Patch

Do not implement:

- MAVLink parameter writes
- automatic tuning writes
- firmware flashing
- arbitrary report file export from MCP
- cloud upload
- network calls
- background job queue
- user authentication

Those can come later, after the read-only tool surface is proven safe.

## Files Most Likely to Change

Expected new files:

- `smarttune/services/__init__.py`
- `smarttune/services/analysis.py`
- `smarttune/services/serialize.py`
- `smarttune/mcp_server.py`
- `tests/test_mcp_security.py`
- `tests/test_services_serialize.py`
- `tests/test_services_analysis.py`

Expected modified files:

- `pyproject.toml`
- `README.md`
- optionally `smarttune/cli.py` if adding shared JSON output

## Implementation Notes

Keep the first version conservative. A useful, boring, read-only MCP server is the right target. The customer-service agents need reliable diagnosis, not broad control.

The most important design choice is not the MCP framework; it is capability narrowing:

- user uploads log
- agent calls SmartTune MCP
- SmartTune parses and analyzes
- agent explains result
- no shell
- no writes
- no parameter mutation

That is the safety boundary this patch must preserve.
