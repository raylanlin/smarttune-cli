<p align="center">
  <img src="https://raw.githubusercontent.com/raylanlin/smarttune-cli/main/assets/banner-hero.png" alt="SmartTune" width="100%" />
</p>

<p align="center">
  <strong>Multi-platform flight log analysis & tuning advisor CLI</strong><br>
  One command. Zero cloud dependency. Actionable tuning recommendations.
</p>

<p align="center">
  <a href="https://pypi.org/project/smarttune"><img src="https://img.shields.io/pypi/v/smarttune.svg" alt="PyPI version" /></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT" /></a>
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+" /></a>
  <a href="https://github.com/raylanlin/smarttune-cli/actions"><img src="https://img.shields.io/badge/tests-96%20passed-brightgreen.svg" alt="Tests" /></a>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#quick-start">Quick Start</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#supported-platforms">Platforms</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#knowledge-base">Knowledge Base</a> ·
  <a href="#roadmap">Roadmap</a>
</p>

---

## Features

| Category | Capability | Description |
|----------|-----------|-------------|
| 🎯 **PID Analysis** | Step response + tuning recommendations | Detects overshoot, rise time, settling time, oscillations. Recommends P/I/D/FF adjustments. |
| 📊 **FFT Spectrum** | Frequency-domain vibration analysis | Gyro spectrum with peak detection, notch filter suggestions, vibration level grading. |
| 🧲 **MagFit** | Magnetometer calibration | Field strength fitting, offset estimation, coverage and interference assessment. |
| 🔧 **SysID** | ARX system identification | Natural frequency and damping ratio estimation from flight data. |
| 🖥️ **Hardware Report** | Sensor and parameter summary | IMU, compass, barometer, GPS, battery — full board health inspection. |
| 📈 **Filter Analysis** | Bode plot + transfer functions | Multi-notch, harmonic notch, and LPF chain visualization. |
| 🌐 **Multi-Platform** | ArduPilot · Betaflight · PX4 | Auto-detect log format. Unified `FlightData` IR powers all analyzers. |
| 🧠 **Layered KB** | 6-layer rule merge | common → platform → user → Pro. Overridable at any level. |
| 🎨 **Rich Output** | Terminal + Markdown + HTML | Pretty tables, progress bars, color-coded diagnostics. |
| 📦 **Zero Cloud** | Offline-first | Pure Python. No API keys. No network required. |

---

## Install

```bash
# Core (auto-detection, no platform parsing deps)
pip install smarttune

# With ArduPilot support
pip install smarttune[ardupilot]

# With Betaflight support (pure Python BBL parser, no extra deps needed)
pip install smarttune

# With all platforms
pip install smarttune[all]

# From source (development)
git clone https://github.com/raylanlin/smarttune-cli.git
cd smarttune-cli
pip install -e ".[dev,all]"
```

> Requires Python 3.9+

> Betaflight BBL parser is pure Python — no external dependencies needed.

---

## Quick Start

```bash
# Full analysis (auto-detect platform)
stune analyze -i flight.bin

# With charts
stune analyze -i flight.bbl --visual

# Single-module deep dive
stune pid -i flight.bin -a roll --visual
stune fft -i flight.bin --visual
stune magfit -i flight.bin
stune sysid -i flight.bin -a pitch
stune hardware -i flight.bin

# Output as Markdown
stune analyze -i flight.bin --format markdown -o report.md

# List supported platforms
stune platforms
```

---

## Commands

### `stune analyze`

Full-spectrum analysis: PID + FFT + MagFit (all in one pass).

```bash
stune analyze -i flight.bin                     # Auto-detect
stune analyze -i flight.bbl --platform betaflight
stune analyze -i flight.bin --visual            # With charts
stune analyze -i flight.bin -o report.md --format markdown
```

### `stune pid`

PID step-response analysis with tuning recommendations.

```bash
stune pid -i flight.bin                         # All axes
stune pid -i flight.bin -a roll                 # Single axis
stune pid -i flight.bbl --visual                # Betaflight
```

### `stune fft`

Frequency-domain vibration analysis with notch filter suggestions.

```bash
stune fft -i flight.bin                         # Full spectrum
stune fft -i flight.bin --visual                # With spectrogram
```

### `stune magfit`

Magnetometer calibration — computes offset and coverage quality.

```bash
stune magfit -i flight.bin                      # ArduPilot only
```

### `stune sysid`

ARX system identification — estimates natural frequency and damping.

```bash
stune sysid -i flight.bin -a roll               # Single axis
stune sysid -i flight.bin -a pitch --na 4 --nb 3
```

### `stune hardware`

Full hardware configuration report: sensors, firmware version, battery, params.

```bash
stune hardware -i flight.bin
stune hardware -i flight.bbl
```

### `stune platforms`

List all detected and available platform adapters.

```bash
stune platforms
```

### `stune filter`

Filter chain analysis with Bode plots.

```bash
stune filter -i flight.bin --gyro-filter 40 --visual
stune filter -i flight.bin --auto               # Auto-derive from params
```

---

## Supported Platforms

| Platform | Log Format | Parser | Status |
|----------|-----------|--------|--------|
| **ArduPilot** | `.bin` / `.log` (DataFlash) | pymavlink | ✅ Full support |
| **Betaflight** | `.bbl` / `.bfl` (Blackbox) | Pure Python `bbl_parser` | ✅ Full support |
| **PX4** | `.ulg` (ULog) | pyulog | 🔲 Coming in v2.x |

### Auto-Detection

`stune` detects your log format automatically via file headers:

| Header | Platform |
|--------|----------|
| `0xA3 0x95` | ArduPilot DataFlash |
| `H Product:Blackbox` | Betaflight Blackbox |
| `.ulg` + ULog magic | PX4 ULog |

---

## Architecture

```
┌─────────────────────────────────────────────┐
│  CLI Layer                                   │
│  stune analyze / pid / fft / magfit / ...   │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  Platform Adapter Layer                      │
│  ArduPilot · Betaflight · PX4               │
│  .bin/.log    .bbl/.bfl    .ulg             │
└──────────────────┬──────────────────────────┘
                   │ FlightData (unified IR)
┌──────────────────▼──────────────────────────┐
│  Analysis Engine Layer (platform-agnostic)   │
│  PID · FFT · SysID · MagFit · Filter · HW  │
│  Betaflight: FF · RPM Filter · D-term Noise │
└──────────────────┬──────────────────────────┘
                   │ AnalysisResult + ParamRef
┌──────────────────▼──────────────────────────┐
│  Knowledge Base Layer                        │
│  common → platform → user → Pro (6-layer)   │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  Output Layer                                │
│  Terminal (Rich) / Markdown / HTML           │
│  ParamRef → Platform-native parameter names │
└─────────────────────────────────────────────┘
```

---

## Knowledge Base

SmartTune uses a 6-layer deep-merge rule engine. Each layer overrides the previous:

| # | Layer | Location | Editable |
|---|-------|----------|----------|
| 1 | Common physics rules | `smarttune/knowledge/rules/common/` | ❌ Built-in |
| 2 | Platform built-in rules | `smarttune/knowledge/rules/{platform}/` | ❌ Built-in |
| 3 | User common custom | `~/.smarttune/knowledge/common/` | ✅ |
| 4 | User platform custom | `~/.smarttune/knowledge/{platform}/` | ✅ |
| 5 | Pro common enhancement | `smarttune-knowledge-pro` (optional) | 🔒 |
| 6 | Pro platform enhancement | `smarttune-knowledge-pro` (optional) | 🔒 |

Rules are standard JSON files — easy to inspect, modify, or extend.

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev,all]"

# Run tests
pytest tests/ -v                          # All 96 tests
pytest tests/test_bbl_parser.py -v        # BBL parser only
pytest tests/test_betaflight_analyzers.py -v  # BF analyzers

# Lint
ruff check smarttune/
black --check smarttune/
```

### Extending with a New Platform

1. **Create adapter** — Subclass `PlatformAdapter`, implement `parse()`, `detect()`, `map_param_to_platform()`
2. **Add knowledge rules** — Drop JSON files in `smarttune/knowledge/rules/{platform}/`
3. **Register** — Use `@register` decorator. `stune platforms` will pick it up automatically.

---

## Roadmap

| Phase | Content | Status |
|-------|---------|--------|
| v1.x | ArduPilot full support | ✅ |
| v2.0 Phase 1 | Multi-platform architecture + unified data model | ✅ |
| v2.0 Phase 2 | Betaflight BBL parser + analytics + KB | ✅ |
| v2.x | PX4 ULog adapter | 🔲 |
| v3.0 | Cross-platform comparison / Plugin system / Web UI | 🔲 |

---

## Author

**Raylan LIN** — [@raylanlin](https://github.com/raylanlin)

SmartTune is built and maintained by [Raylan LIN](https://github.com/raylanlin).

---

## License

MIT — see [LICENSE](LICENSE) for details.

`smarttune-knowledge-pro` is a separate, closed-source enhancement package with a proprietary license.
