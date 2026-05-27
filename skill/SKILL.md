---
name: smarttune
description: Multi-platform flight log offline analysis and tuning advice. Triggered when user sends .bin/.bbl/.ulg log files for PID/FFT/filter/magnetometer analysis. Supports ArduPilot, Betaflight, and PX4.
---

# SmartTune CLI (stune)

Multi-platform flight log analysis & tuning advisor.
Supports **ArduPilot** (.bin/.log), **Betaflight** (.bbl/.bfl), and **PX4** (.ulg).

Platform is auto-detected from the log file format.

## Installation

```bash
cd ~/cli-tools/smarttune-cli
pip install -e .
```

## Quick Reference

```bash
# Comprehensive analysis (recommended) — PID + FFT + Filter + Mag
stune analyze -i log.bin

# Log quality scoring — data completeness / excitation / sample rate
stune quality -i log.bin

# Individual analyses
stune pid -i log.bin -a roll
stune fft -i log.bin
stune filter -i log.bin --gyro-filter 40 --visual
stune sysid -i log.bin -a roll
stune hardware -i log.bin
stune magfit -i log.bin

# List supported platforms
stune platforms
```

## Usage Rules

1. **Check help first**: `stune <command> --help` when unsure about flags
2. **Terminal output by default**: no `-o` flag → stdout only
3. **Optional charts**: add `--visual` for matplotlib plots
4. **Cleanup after analysis**: delete raw `.bin/.log/.bbl/.ulg` files after processing
5. **⚠️ MANDATORY: Validate parameters before recommending**. After analysis generates tuning suggestions, you MUST verify each parameter exists in the target firmware by running:
   ```bash
   stune params --validate <PARAM_NAME> <VALUE> -p <platform>
   ```
   If validation fails (exit code 1), do NOT recommend that parameter — it may not exist in the firmware, or the value is out of valid range. Find an alternative or tell the user the parameter is not available.

   This is critical because:
   - Betaflight 4.5+ renamed many parameters (e.g., `d_min_roll` → `d_max_roll`, `gyro_lowpass_hz` → `gyro_lpf1_static_hz`)
   - Parameter names differ between firmware versions
   - Some parameters have strict value ranges that must be respected

   **Never** blindly output parameter recommendations without validation.

⚠️ Analysis done = output results. No residual files needed.

## Workflows

### Getting Started

```bash
# 1. Hardware config check
stune hardware -i flight.bin

# 2. Log quality scoring
stune quality -i flight.bin

# 3. Comprehensive analysis
stune analyze -i flight.bin --visual

# 4. Targeted tuning
stune pid -i flight.bin -a roll
stune fft -i flight.bin --visual
```

### Advanced

```bash
# System identification (ARX model)
stune sysid -i flight.bin -a roll --na 3 --nb 2

# Filter transfer function analysis
stune filter -i flight.bin --gyro-filter 40 --visual

# Before/after FFT comparison
stune fft -i flight.bin --visual
```

### Multi-platform

```bash
# Auto-detect platform (default)
stune analyze -i flight.bbl

# Manual override
stune analyze -i flight.bin --platform ardupilot
```

### Parameter Validation Workflow (MANDATORY)

After analysis produces recommendations, always validate:

```bash
# 1. Run analysis
stune pid -i flight.bin -a roll
# → Recommends: ATC_RAT_RLL_P: 0.12 → 0.15

# 2. Validate the parameter exists and value is in range
stune params --validate ATC_RAT_RLL_P 0.15 -p ardupilot
# → ✓ ATC_RAT_RLL_P: 0.150 within [0.010, 1.000]
# OK, proceed to recommend

# 3. If validation fails
stune params --validate XYZZY_PARAM 0.5 -p ardupilot
# → ✗ XYZZY_PARAM: NOT FOUND in ArduPilot parameter table
# DO NOT recommend — parameter doesn't exist in this firmware

# 4. Also search to find correct param name if unsure
stune params --search "feedforward" --platform betaflight
# → Shows actual BF 4.5+ parameter names (f_roll, etc.)
```

**Rule: One `stune params --validate` call per recommended parameter before presenting results.**

## Command Reference

### stune analyze

Comprehensive log analysis — PID + FFT + Filter + Mag tuning recommendations.

```bash
stune analyze -i flight.bin                          # Basic analysis
stune analyze -i flight.bin --visual                 # Generate plots
stune analyze -i flight.bin -a roll                  # Roll axis only
stune analyze -i flight.bin -o report.md --report md # Markdown report
stune analyze -i flight.bin --theme dark --visual    # Dark theme plots
```

### stune quality

Log quality scoring — checks data completeness, excitation adequacy (number of PID step windows), sample rate consistency.

```bash
stune quality -i flight.bin
stune quality -i flight.bin -o quality.txt
```

### stune pid

PID step response analysis — rise time, overshoot, settling time, oscillation count.

```bash
stune pid -i flight.bin                              # All axes
stune pid -i flight.bin -a roll                      # Single axis
stune pid -i flight.bin -a roll --visual             # Step response plot
stune pid -i flight.bin --visual --theme dark        # Dark theme
```

### stune fft

FFT vibration spectrum analysis — identify dominant vibration frequencies, suggest notch filter parameters.

```bash
stune fft -i flight.bin
stune fft -i flight.bin --visual                     # Spectrum plot
stune fft -i flight.bin --visual --theme dark
```

### stune filter

Filter transfer function analysis (Bode Plot) — two modes:

- **Auto mode (default)**: derive filter config from log parameters
  - ArduPilot: reads `INS_HNTCH_*` params
  - Betaflight: reads `gyro_lowpass_hz` / notch params
- **Manual mode**: specify `--gyro-filter` / `--notch-freq` directly

```bash
stune filter -i flight.bin                           # auto-derive
stune filter -i flight.bin --no-auto --gyro-filter 20 --visual
stune filter -i flight.bin --notch-freq 80 --visual
```

### stune sysid

ARX system identification — estimate transfer function from log data (natural frequency, damping ratio, time constant).

```bash
stune sysid -i flight.bin                            # All axes
stune sysid -i flight.bin -a roll                    # Single axis
stune sysid -i flight.bin -a roll --na 3 --nb 2     # Custom ARX order
```

### stune hardware

Hardware configuration report — IMU, compass, filter, PID parameters at a glance.

```bash
stune hardware -i flight.bin
stune hardware -i flight.bin --platform ardupilot    # Force platform
```

### stune magfit

Magnetometer calibration analysis — Fitness assessment, hard/soft iron interference diagnosis, flight coverage check.

```bash
stune magfit -i flight.bin
```

### stune params

Query and validate firmware parameter tables. Data sourced from official firmware repositories.

```bash
# List all parameters for a platform
stune params ap                           # ArduPilot (2,574 params)
stune params bf                           # Betaflight (45 params)
stune params px4                          # PX4 (20 params)

# Show detail for a specific parameter
stune params ATC_RAT_RLL_P                # auto-detects platform
stune params p_roll                       # Betaflight param

# Search across all platforms
stune params --search notch
stune params --search roll --platform ardupilot

# Filter by category
stune params --category pid --platform betaflight

# Validate a parameter recommendation (CRITICAL for agents)
stune params --validate ATC_RAT_RLL_P 0.15 -p ardupilot   # exit 0 if valid
stune params --validate XYZZY_PARAM 0.0 -p ardupilot       # exit 1 if not found
stune params --validate p_roll 500 -p betaflight           # exit 1 if out of range
```

**Parameter tables are loaded from knowledge base JSON files** (`smarttune/knowledge/params/`). Update the JSON to refresh parameters without code changes. Parameters scraped from official firmware source code — ArduPilot from `@Param` annotations, Betaflight from `settings.c`, PX4 from YAML definitions.

## Platform Support Matrix

| Feature | ArduPilot | Betaflight | PX4 |
|---------|-----------|------------|-----|
| `analyze` | ✅ | ✅ | 🔲 |
| `quality` | ✅ | ✅ | 🔲 |
| `pid` | ✅ | ✅ | 🔲 |
| `fft` | ✅ | ✅ | 🔲 |
| `filter` | ✅ | ✅ | 🔲 |
| `sysid` | ✅ | ✅ | 🔲 |
| `hardware` | ✅ | ✅ | 🔲 |
| `magfit` | ✅ | — | 🔲 |
| Log format | .bin / .log | .bbl / .bfl | .ulg |

## Further Help

**Rule: Always check `--help` before using a command you haven't run recently.** Every subcommand has a detailed help block with examples and option descriptions.

| Scenario | Action |
|----------|--------|
| Full parameter list | `stune <command> --help` |
| Usage examples | Built into each subcommand's help text |
| Parameter meaning reference | Knowledge base built into CLI (`smarttune/knowledge/`) | |

## Comparison with WebTools

| WebTools Tool | stune command | Status |
|--------------|---------------|--------|
| PIDReview | `pid` + `analyze` | ✅ |
| FilterReview | `filter` + `fft` | ✅ |
| HardwareReport | `hardware` | ✅ |
| MAGFit | `magfit` | ✅ |
| SysID | `sysid` | ✅ |
| — | `quality` | ✅ New |

## Capability Status

| Capability | Command | Status |
|------------|---------|--------|
| Log parsing | Multi-platform auto-detect | ✅ |
| PID analysis | `stune pid` | ✅ |
| FFT analysis | `stune fft` | ✅ |
| Magnetometer calibration | `stune magfit` | ✅ |
| Comprehensive analysis | `stune analyze` | ✅ |
| System identification | `stune sysid` | ✅ |
| Filter analysis | `stune filter` | ✅ |
| Hardware report | `stune hardware` | ✅ |
| Log quality scoring | `stune quality` | ✅ |
| Multi-platform support | AP + BF + PX4 | ✅ |


