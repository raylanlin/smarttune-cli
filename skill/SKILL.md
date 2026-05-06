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

| Scenario | Action |
|----------|--------|
| Full parameter list | `stune <command> --help` |
| Parameter meaning reference | Knowledge base built into CLI (`smarttune/knowledge/`) |

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

## Relationship to Legacy ap-tune

- `ap-tune` (ArduPilot-only) → **Deprecated**, code merged into SmartTune v2.0
- `stune` (SmartTune v2.0+) → **Current sole CLI**, unified multi-platform interface
- Never use `ap-tune`. Always use `stune`.
