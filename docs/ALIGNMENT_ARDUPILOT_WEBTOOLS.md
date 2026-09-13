# Alignment with ArduPilot's own tooling

SmartTune's ArduPilot analysis is deliberately kept in step with the reference
implementations ArduPilot ships. This document records **what was compared,
line by line, what matched, and every place SmartTune deliberately differs.**
It is written so a reviewer can re-run the comparison without re-deriving it.

Sources compared (read at the commits below, 2026-09-13):

| Reference | Repo / file | Covers |
|-----------|-------------|--------|
| UAVLogViewer | `ArduPilot/UAVLogViewer@01f9d510` — `src/tools/parsers/mavlinkParser.js`, `src/tools/parsers/modeMaps.js`, `src/tools/mavlinkDataExtractor.js` | `.tlog` message extraction, mode maps, param handling |
| WebTools · StreamStats | `raylanlin/WebTools@cb46984f` — `StreamStats/StreamStats.js` | `.tlog` framing, per-system accounting, link-loss measurement |
| WebTools · PIDReview | `raylanlin/WebTools@cb46984f` — `PIDReview/PIDReview.js`, `PIDReview/Readme.md` | step-response estimation (Wiener / transfer function) |

---

## 1. Step response (`smarttune/platform/ardupilot/step_response_fft.py`)

PIDReview's Readme describes the method as a "Wiener-filter / transfer-function
approach between the target and actual signals", individual windows in grey and
the mean as the coloured line. SmartTune reproduces `redraw_step`
(PIDReview.js:1031-1160). Item-by-item:

| Step | PIDReview.js | SmartTune | Status |
|------|--------------|-----------|--------|
| Hanning window | `hanning(window_size)` :1064 | `_hanning()` (same `2π/(n-1)` form) | ✅ |
| Window spacing | `Math.round(window_size / 16)` :1068 | `int(np.round(window_size / 16))` | ✅ |
| FFT scaling | DC/Nyquist ×1/N, rest ×2/N (`run_fft`) | `scale` array, same | ✅ |
| Double-sided spectrum | `to_double_sided` | `_to_double_sided` (complex128) | ✅ |
| Step length | `min(ceil(0.5 / sample_time), window_size)` :1072 | same (`ceil`, not `floor`) | ✅ |
| SNR cutoff | `cutfreq = 25` :1082 | 25 Hz, clamped at low rates — see below | ⚠️ deliberate |
| LPF bin index | `bins.findIndex(x => x >= cutfreq)` :1083 | `searchsorted(..., side="left")` | ✅ **fixed v3.4.0** |
| Double-side correction | `len_lpf += len_lpf - 2` :1084 | same | ✅ |
| Gaussian radius / sigma | `ceil(len_lpf*0.5)`, `len_lpf/6` :1085-1086 | same | ✅ |
| Cumulative Gaussian + normalise | loops :1091-1098 | `cumsum` / `csum[-1]` | ✅ |
| Mirror | `[...sn, ...sn.slice(1, real_len-1).reverse()]` :1100 | same | ✅ |
| Scale to SNR | `×-1`, `+1+1e-9`, `×10`, inverse :1103-1106 | same | ✅ |
| Window amplitude gate | `if (FFT_res.TarMax[k] < 20.0) continue` :1136 | `tar_max < 20.0` on the **windowed** signal | ✅ |
| Wiener regularisation | `Pxx[0] = array_add(Pxx[0], sn)` :1157 (real part only) | `Pxx_real += sn` | ✅ |
| Impulse → step | IFFT then cumulative sum | `np.fft.ifft(H).real`, `np.cumsum` | ✅ |
| Window averaging | arithmetic mean | `np.mean(axis=0)` | ✅ |

### Fixed in v3.4.0 — off-by-one in the LPF bin index

`searchsorted(..., side="right")` returns the first index **after** an exact
match, `findIndex(x => x >= cutfreq)` returns the match itself. When an FFT bin
landed exactly on 25 Hz (e.g. 400 Hz sampling with a 512-point window →
0.78125 Hz bins, no exact hit; but 800 Hz/512 → 1.5625 Hz bins do hit 25.0)
SmartTune used one extra bin of regularisation. Now `side="left"`.

### Fixed in v3.4.0 — output signal was the IMU gyro, not `Act`

This was the substantive divergence. PIDReview builds its test sets from
`log_msg.Act` / `log_msg[axis_prefix]` (PIDReview.js:1600 for `RATE`, :1608 for
`PIDx`, with a per-message `unitScale`) — the rate the controller measured. It
never substitutes `IMU.Gyr`.

SmartTune's `compute_step_response_for_axis()` preferred the IMU gyro whenever
it was available (and the docstring wrongly claimed this matched WebTools). That
path also interpolates the target onto the gyro time base and re-samples both
onto a uniform grid, so it added interpolation dynamics to the very transfer
function being measured, on top of using an unfiltered signal.

`prefer_imu=False` is now the default; the gyro path stays available as an
explicit comparison tool. The result dict reports which was used:
`info.output_signal ∈ {"pid_act", "imu_gyro"}`.

**This changes ArduPilot step-response numbers** (rise time / overshoot /
settling) for logs that have IMU data — in the direction of the reference
implementation. It is the one intentional numeric change in v3.4.0.

### Deliberate deviation — SNR cutoff clamp at low sample rates

With `cutfreq = 25` Hz fixed, `findIndex` returns `-1` when 25 Hz is beyond the
spectrum, making `len_lpf` negative; the Gaussian loops then never run, `sn`
stays all-ones, and `H ≈ 0` — a flat zero step response. WebTools only ever
loads `.bin` logs (400 Hz+), so it cannot reach that branch. A `.tlog` at
1-50 Hz reaches it every time, so SmartTune clamps the cutoff to
`0.8 × Nyquist` and reports `info.cutfreq_clamped` + a note.

### Deliberate deviation — per-window data-quality pre-checks

SmartTune additionally drops windows with NaN/Inf, `|actual| > 1500 deg/s`,
`actual ≫ 4 × target`, a near-static segment (`std < 5 deg/s`), or an implied
overshoot above 300%. WebTools has no such step (a human is looking at the grey
per-window traces). Counted in `info.skipped_quality`.

### Known difference, not changed — unit normalisation

WebTools applies a fixed `unitScale` per message type. SmartTune's DataFlash
adapter infers rad/s vs deg/s from signal magnitude (`max |rate| < 6.5` ⇒
rad/s). The heuristic is wrong for an extremely gentle flight whose true peak
rate never exceeds 6.5 deg/s, which would be scaled up by 57.3×. Changing it
would move `.bin` numbers for every existing log, so it is documented here and
left for a release that can carry a data migration note.

---

## 2. Telemetry logs (`smarttune/platform/ardupilot/tlog_parser.py`)

### Matched to UAVLogViewer

| Behaviour | Reference | SmartTune |
|-----------|-----------|-----------|
| Vehicle vs GCS heartbeats | `validGCSs` allow-list; `getModeString` returns `''` for `MAV_TYPE_GCS` | GCS/unknown types skipped for autopilot id, frame type, modes and armed state; counted in `log_source.gcs_heartbeats_ignored` |
| Mode maps by vehicle type | `getModeMap` → ACM / APM / Rover / Tracker / Sub | same dispatch, tables copied verbatim from `modeMaps.js` (+ Copter 27 `AUTO_RTL`) |
| `base_mode` fallback | bits 4/8/16 → Auto/Guided/Stabilize | same |
| Armed state | `base_mode & 0b10000000` | `extras["armed_events"]` |
| Timeline | plots against `time_boot_ms` | vehicle clock preferred; wall-clock-only messages shifted by the median offset |
| GPS scaling | `lat * 1e-7`, `alt / 1000` | same (plus a defensive "already scaled?" guard) |
| `GLOBAL_POSITION_INT` | `lat/1e7`, `relative_alt/1000` | same, used only when `GPS_RAW_INT` is absent |
| AHRS2 as position source | offered as a trajectory source | position fallback (mirrors `AHR2` in our DataFlash path) |
| Param id sanitising | `replace(/[^a-z0-9A-Z_]/ig, '')` | identical regex |
| Param value semantics | last value wins per name | dict assignment, last wins |
| `STATUSTEXT` | keeps `severity` | `msg_log[].severity` |

### Matched to StreamStats

| Behaviour | Reference | SmartTune |
|-----------|-----------|-----------|
| tlog framing | 8-byte big-endian µs stamp + `0xFE`/`0xFD` | `looks_like_tlog()` sniffs exactly this (plus a plausible-epoch range) |
| Per-system separation | keyed by `srcSystem` / `srcComponent` | locks onto the first vehicle heartbeat's system id; other systems counted in `frames_from_other_systems`, all systems listed in `log_source.systems` |
| Link loss | `dropped += seq - next_seq`, 8-bit wrap | same, reported as `frames_dropped` / `drop_percent` and a note above 5% |

### Deliberate differences

* **Units.** UAVLogViewer keeps radians (its 3D view consumes them);
  `FlightData` declares degrees, so attitude and rates are converted.
* **Time origin.** StreamStats measures from the first wrapper timestamp and
  aborts on non-monotonic time; SmartTune uses the vehicle clock and tolerates
  reordering (a tlog interleaves systems, so wall-clock order is not global).
* **`time_usec` guard.** Accepted as uptime only below ~30 days; `GPS_RAW_INT`
  and `HIGHRES_IMU` may carry a UNIX epoch, which would otherwise appear as a
  55-year flight.
* **Honesty layer.** No reference tool states analysis limits, because a human
  is reading the plots. SmartTune is consumed by agents, so every structural
  gap (no `ATTITUDE_TARGET`, sub-50 Hz rates, FFT Nyquist ceiling, missing
  accel stream, no parameter download, dropped frames, foreign systems) lands
  in `extras["telemetry_notes"]` and is scored in `stune quality`.

---

## Re-running the comparison

```bash
# step response
python - <<'PY'
from smarttune.platform.ardupilot import step_response_fft as s
print(s.__doc__)          # carries the PIDReview.js line-number table
PY

pytest -q tests/test_tlog_parser.py     # pins the tlog alignment behaviours
```

When a reference tool changes, update the commit hashes in the table above,
re-read the three files, and amend this document in the same commit as any code
change it causes.
