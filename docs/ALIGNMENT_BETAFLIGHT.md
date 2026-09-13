# Alignment with Betaflight's own tooling

Companion to `docs/ALIGNMENT_ARDUPILOT_WEBTOOLS.md`. Records what was compared
for the Betaflight path, line by line, what matched, what was wrong, and every
deliberate deviation.

Sources compared (read 2026-09-13):

| Reference | Repo / file | Covers |
|-----------|-------------|--------|
| Firmware (ground truth) | `raylanlin/betaflight@96d61291` — `src/main/blackbox/blackbox.c`, `src/main/cli/settings.c` | what the log actually contains |
| Blackbox log viewer | `betaflight/blackbox-log-viewer@a84755c5` — `src/flightlog.js`, `src/flightlog_parser.js`, `src/flightlog_fields_presenter.js` | decoding and unit conventions |
| PID-Analyzer | `Plasmatree/PID-Analyzer@d2ab676e` — `PID-Analyzer.py` | step-response estimation |
| PIDtoolbox | `bw1129/PIDtoolbox` — `PTstepcalc.m` | **repo delisted — cannot be verified** |

---

## 1. Gyro units — checked, and we were right

The suspicion going in was that we ignored the `gyro_scale` header. The viewer
does apply it: `flightlog.js:1468` computes
`gyroScale * 1e6 * (180/π) * value`, and `flightlog_parser.js:769-779`
normalises legacy scales (rad/µs) against modern ones (deg/s).

The firmware settles it: `blackbox.c:1513` writes

```c
BLACKBOX_PRINT_HEADER_LINE("gyro_scale","0x%x", castFloatBytesToInt(1.0f));
```

`gyro_scale = 1.0`, because `gyroADC` is logged from `gyro.gyroADCf[]`, which
is **already deg/s**. So raw `gyroADC` needs no scaling — SmartTune's reading
was correct, and the viewer's formula collapses to identity for modern logs.
The header value is now recorded in `extras["blackbox_info"]["gyro_scale_header"]`
so a legacy log (where it is *not* 1.0) is visible rather than silent.

> If a pre-Betaflight-3 / Cleanflight log ever needs supporting, that is where
> the legacy rad/µs branch would go.

## 2. `blackbox_high_resolution` — real bug, fixed in v3.5.0

`blackbox.c:2313`:

```c
blackboxHighResolutionScale = blackboxConfig()->high_resolution ? 10.0f : 1.0f;
```

and the affected fields (`blackbox.c:1262-1289`):

| Field | Line |
|-------|------|
| `gyroADC[i]` | 1262 |
| `gyroUnfilt[i]` | 1263 |
| `rcCommand[i]` | 1284 |
| `setpoint[i]` | 1289 |

PID term fields (`axisP/I/D/F`) are **not** scaled. The flag is written to the
header as `blackbox_high_resolution` (`blackbox.c:1780`) and the viewer divides
by it before display (`flightlog_fields_presenter.js:1633-1650`, 2428-2462).

SmartTune ignored the flag entirely. Consequences on a high-resolution log
(a BF 4.4+ option that tuners routinely enable):

* gyro, setpoint and rcCommand read **10× too large**;
* the outlier sanitiser (`_sanitize_signal`, bound to `rate_limits × 1.1`,
  ~2198) then treated every sample above ~220 deg/s real as corruption and
  **interpolated it away** — so the harder the flying, the more of the trace
  was silently replaced;
* vibration levels, step responses and every derived recommendation followed
  the corrupted signal.

Fixed: the header flag is parsed once, and `_col_f64()` divides the four
affected field families by it. Reported in
`extras["blackbox_info"]["high_resolution"]`.

## 3. Unfiltered gyro — exposed, not switched

`gyroUnfilt` (firmware `blackbox.c:1263`, from `gyro.gyroADC[]`) is the
pre-filter trace. Notch targeting properly belongs on pre-filter data — that is
what ArduPilot's FilterReview does, and what the viewer's spectrum view offers.

SmartTune's `FlightData.gyro` remains the **filtered** `gyroADC`: the FFT
analyser's vibration thresholds (GOOD / MARGINAL / POOR) were calibrated
against filtered data, and switching the source would move every assessment
without a verification path. The pre-filter trace is now available as
`extras["gyro_unfiltered"]`, with `blackbox_info.has_unfiltered_gyro`.

> Planned: a `--prefilter` mode for notch derivation that reads
> `extras["gyro_unfiltered"]` and states which trace it used.

## 4. Step response — realigned to PID-Analyzer in v3.5.0

Before v3.5.0 the BF step response followed `PTstepcalc.m`'s *published
description* (the repo is delisted, so nothing could be verified): 2-second
segments, **no window function**, constant-λ regularisation, segment/4 spacing.

Line-by-line against PID-Analyzer — the open, verifiable ancestor of this
algorithm family (and of the WebTools implementation we already match for
ArduPilot):

| Item | PID-Analyzer.py | SmartTune before | SmartTune now |
|------|-----------------|------------------|---------------|
| Frame length | `framelen = 1.0` s :32 | 2.0 s | 1 s (shared kernel) ✅ |
| Response length | `resplen = 0.5` :33 | 0.5 s ✅ | 0.5 s ✅ |
| Cutoff | `cutfreq = 25.` :34 | n/a (constant λ) | 25 Hz ✅ |
| Sub-windowing | `superpos = 16`, `shift = flen/superpos` :36,201 | segment/4 | N/16 ✅ |
| Window | `np.hanning(self.flen)` :64 | **none** ❌ | Hanning ✅ |
| Deconvolution | Wiener :212-225 | Wiener, constant λ | Wiener, Gaussian-CDF SNR ✅ |
| Step | `cumsum`, `[:rlen]` :232 | same ✅ | same ✅ |
| Input split | `low_high_mask(max_in, 500)` :66,96-100 | reject outside [20, 500] | keep ≤ 500 (= low-input response) ✅ |
| Averaging | mean over windows | mean ✅ | mean ✅ |

The missing window function was the substantive defect: an unwindowed
2-second segment leaks spectrally into the deconvolution, biasing the very
transfer function being estimated.

Implementation: BF now delegates to the shared
`platform/ardupilot/step_response_fft.estimate_step_response` (the
WebTools-verified core) and supplies only BF's input gating. One verified
kernel, two platform-specific gates.

**This moves Betaflight step-response numbers.** The direction is toward the
reference implementation; `info.method` is now `pid_analyzer_wiener`.

### Deliberate deviations

* **Window length is rounded to a power of two** (inherited from WebTools,
  whose `fft.js` requires it); PID-Analyzer uses the exact 1-second sample
  count. Frequency resolution differs slightly.
* **Regularisation shape**: PID-Analyzer uses a hard mask at the cutoff
  (`to_mask(np.clip(np.abs(freq), cutfreq-1e-9, cutfreq))`, :219); the shared
  kernel uses WebTools' Gaussian-CDF smoothed mask. Same 25 Hz intent, softer
  transition.
* **One curve, not two**: PID-Analyzer plots low-input and high-input
  responses separately. SmartTune reports the low-input one (what tuning
  decisions are read from) and says so via `info.input_window_deg_s`.
* **Steady-state sanity check** (`0.5 < mean(step[0.2..0.5 s]) < 3.0`) is
  SmartTune's own: with SP and GY in the same units the response should settle
  near 1.0, and a wild value means the deconvolution did not converge. Reported
  as `info.steady_state` / `info.steady_state_ok` rather than hidden.
* **Per-window data-quality pre-checks** (NaN, |gyro| > 1500 deg/s, static
  segments, implied overshoot > 300%) — counted in `info.skipped_quality`.
* `compute_step_response_for_axis` no longer resamples through the IMU path by
  default: for Betaflight `Actual` **is** `gyroADC`, so the old default
  interpolated one signal onto its own timebase for nothing.

## 5. Checked and unchanged

| Item | Reference | Status |
|------|-----------|--------|
| `acc_1G` scaling for accelerometer | viewer divides by `acc_1G` | already matched |
| Motor normalisation via `minthrottle`/`maxthrottle` | viewer uses the same header pair | already matched |
| Flight-mode events from `EVENT_FLIGHT_MODE` flags | viewer decodes the same event | already matched |
| Setpoint as the PID target | PID-Analyzer uses `input` = setpoint | already matched |
| P-interval-derived sample rate | viewer derives from `looptime` + `P interval` | already matched |

---

## Re-running the comparison

```bash
pytest -q tests/test_bf_alignment.py
python -c "from smarttune.platform.betaflight import step_response_fft as s; print(s.__doc__)"
```

Cross-check a real `.bbl` against the references:

1. Load it in <https://betaflight.com/blackbox> (blackbox log viewer) and
   confirm gyro traces agree in magnitude with `stune fft -i log.bbl -f json`
   — decisive on a **high-resolution** log.
2. Run PID-Analyzer on the same file and compare the low-input step response
   shape and rise time against `stune pid -i log.bbl -f json`.
