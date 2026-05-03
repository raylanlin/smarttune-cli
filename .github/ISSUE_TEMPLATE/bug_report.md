---
name: Bug report
about: Report incorrect analysis results or crashes
title: "[Bug] "
labels: bug
assignees: ""
---

## Environment

- **CLI version:** `stune --version` output
- **Python version:** `python3 --version`
- **OS:** Ubuntu / macOS / WSL2 / other
- **Flight controller:** Pixhawk / Cube / Matek / other
- **Firmware:** ArduPilot / Betaflight / PX4 — version

## Describe the bug

Clear description of what's wrong — wrong PID suggestion, FFT shows nothing, crash, etc.

## To Reproduce

```bash
stune analyze -i /path/to/log.bin
```

Paste full command and output above. If it crashed, include the traceback.

## Expected vs actual

| Metric | Expected | Actual |
|--------|----------|--------|
| PID Roll P | ~0.12 | 0.35 |
| Vibration level | GOOD | SEVERE |
| ... | ... | ... |

## Log file (optional)

If the bug is analysis-related, attach a `.BIN` or `.log` file that triggers it. Small logs only (under 5MB) — otherwise share via cloud link.

## Screenshots

If `--visual` output looks wrong, attach the generated PNG.
