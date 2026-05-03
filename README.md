# SmartTune CLI

**Multi-platform flight log analysis & tuning advisor.**

One command. Zero cloud dependency. Actionable tuning recommendations.

## Supported Platforms

| Platform | Log Format | Status |
|---|---|---|
| **ArduPilot** | `.bin` / `.log` (DataFlash) | ✅ Full support |
| **Betaflight** | `.bbl` / `.bfl` (Blackbox) | 🚧 Planned v2.0 |
| **PX4** | `.ulg` (ULog) | 🚧 Planned v2.x |

## Install

```bash
# Core (platform auto-detection, no parsing deps)
pip install smarttune

# With ArduPilot support
pip install smarttune[ardupilot]

# With all platforms
pip install smarttune[all]
```

## Quick Start

```bash
# Auto-detect platform and run full analysis
stune analyze -i flight.bin

# Explicit platform
stune analyze -i flight.bin --platform ardupilot

# Individual analysis
stune pid -i flight.bin --visual
stune fft -i flight.bin --visual
stune magfit -i flight.bin

# List supported platforms
stune platforms
```

## Architecture

```
Log File
  │
  ▼
┌──────────────────────────────┐
│  Platform Adapter            │  Auto-detect + parse + param mapping
│  (ArduPilot / BF / PX4)     │
└──────────┬───────────────────┘
           │  FlightData (unified)
           ▼
┌──────────────────────────────┐
│  Analysis Engines            │  Platform-agnostic
│  PID · FFT · Filter · MagFit│
└──────────┬───────────────────┘
           │  AnalysisResult
           ▼
┌──────────────────────────────┐
│  Knowledge Base              │  common → platform → user → Pro
│  (layered rule merge)        │
└──────────┬───────────────────┘
           │
           ▼
┌──────────────────────────────┐
│  Output                      │  Terminal / Markdown / HTML
│  (ParamMapper → native names)│
└──────────────────────────────┘
```

## Knowledge Base Layers

Rules are loaded and deep-merged in this order:

1. `common/` — Cross-platform physics rules (vibration thresholds, etc.)
2. `{platform}/` — Platform-specific built-in rules
3. `~/.smarttune/knowledge/{platform}/` — User customization
4. `smarttune-knowledge-pro` — Pro enhancement (optional, closed-source)

## Roadmap

- **v1.x** — ArduPilot full support (current)
- **v2.0** — Multi-platform architecture + Betaflight BBL adapter
- **v2.x** — PX4 ULog adapter
- **v3.0** — Cross-platform comparison analysis

## License

MIT
