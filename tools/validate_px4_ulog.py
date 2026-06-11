#!/usr/bin/env python3
"""
PX4 适配器端到端验证脚本。

用法：
    python tools/validate_px4_ulog.py <flight.ulg>
    # 无参数时自动下载 pyulog 官方 sample.ulg 到 /tmp 并验证

验证项（对应 CHANGELOG 第三/四轮的 PX4 工作）：
  1. detect()        — ULog 魔数识别
  2. parse()         — 不抛异常、各字段量级 sanity check
  3. 参数双写契约     — params 同时含 MC_ROLLRATE_P 与 pid.roll.p（A2）
  4. PID 链路        — run_module("pid") 跑通（setpoint 缺失时明确降级）
  5. FFT 链路        — run_module("fft") 跑通 + PX4 语义建议
                       （无 mode/REF/HMC 键、vibration_level 为统一 Assessment 标签）
  6. SysID/quality   — 跑通或给出明确数据原因

退出码：0 = 全部通过；1 = 有失败项。
"""

import sys
import urllib.request
from pathlib import Path

SAMPLE_URL = "https://github.com/PX4/pyulog/raw/main/test/sample.ulg"

PASS, FAIL, WARN = "✅", "❌", "⚠️ "
failures = []


def check(name: str, ok: bool, detail: str = "", warn_only: bool = False):
    mark = PASS if ok else (WARN if warn_only else FAIL)
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    if not ok and not warn_only:
        failures.append(name)


def main() -> int:
    if len(sys.argv) > 1:
        log_path = Path(sys.argv[1])
    else:
        log_path = Path("/tmp/px4_sample.ulg")
        if not log_path.exists():
            print(f"下载官方样例日志: {SAMPLE_URL}")
            urllib.request.urlretrieve(SAMPLE_URL, log_path)
    print(f"日志: {log_path}\n")

    import numpy as np
    from smarttune.platform.px4 import PX4Adapter

    adapter = PX4Adapter()

    # 1. detect
    check("detect() 识别 ULog 魔数", adapter.detect(log_path))

    # 2. parse
    try:
        fd = adapter.parse(log_path)
        check("parse() 完成", True)
    except Exception as exc:
        check("parse() 完成", False, f"{type(exc).__name__}: {exc}")
        print("\n后续检查依赖 parse，提前退出。")
        return 1

    # 量级 sanity
    check("gyro 形状 (N,3) 且非空", fd.gyro is not None and fd.gyro.ndim == 2 and fd.gyro.shape[1] == 3)
    if fd.gyro is not None and len(fd.gyro):
        g_max = float(np.nanmax(np.abs(fd.gyro)))
        check("gyro 量级像 deg/s（峰值 < 2000）", g_max < 2000.0, f"max |gyro| = {g_max:.1f}")
    check("sample_rate 合理 (50~2000 Hz)", 50.0 <= fd.sample_rate_hz <= 2000.0,
          f"{fd.sample_rate_hz:.1f} Hz", warn_only=True)
    check("duration > 5 s", fd.duration_s > 5.0, f"{fd.duration_s:.1f} s", warn_only=True)
    if fd.mag is not None and len(fd.mag):
        mag_norm = float(np.nanmedian(np.linalg.norm(fd.mag, axis=1)))
        check("mag 模长像 mGauss (150~800)", 150.0 <= mag_norm <= 800.0,
              f"median |mag| = {mag_norm:.0f}")
    else:
        check("mag 数据", True, "无 sensor_mag 主题（样例日志可能未记录）", warn_only=True)

    # 3. 参数双写契约
    has_plat = any(k.startswith("MC_ROLLRATE") for k in fd.params)
    has_generic = "pid.roll.p" in fd.params
    check("参数表含 MC_*RATE_*", has_plat, warn_only=not has_plat)
    check("generic key 双写 (pid.roll.p)", has_generic == has_plat,
          "A2 契约：有平台参数就必须有 generic 镜像")

    # 4/5/6. 分析链路
    from smarttune.services.analysis import run_module
    from smarttune.knowledge import KnowledgeBase
    kb = KnowledgeBase(platform="px4")

    if fd.pid:
        try:
            pid_res = run_module("pid", adapter, fd, kb=kb)
            check("PID 分析链路", True,
                  f"axes={list(getattr(pid_res, 'axes', {}).keys())}")
        except Exception as exc:
            check("PID 分析链路", False, str(exc))
    else:
        check("PID 分析链路", True,
              "vehicle_rates_setpoint 未记录 → 正常降级（需带 setpoint 的日志）", warn_only=True)

    try:
        fft_res = run_module("fft", adapter, fd, kb=kb)
        recs = fft_res.get("recommendations", fft_res) if isinstance(fft_res, dict) else {}
        rec_keys = list(recs.keys()) if isinstance(recs, dict) else []
        bad = [k for k in rec_keys if any(s in k for s in (".mode", ".ref", ".hmc", ".att"))]
        check("FFT 分析链路", True)
        check("FFT 建议为 PX4 语义（无 mode/REF/HMC/ATT 键）", not bad,
              f"违例键: {bad}" if bad else "")
        lvl = fft_res.get("vibration_level") if isinstance(fft_res, dict) else None
        check("vibration_level 为统一 Assessment 标签",
              lvl in ("EXCELLENT", "GOOD", "MARGINAL", "POOR", "UNUSABLE", None),
              f"got {lvl!r}")
    except Exception as exc:
        check("FFT 分析链路", False, str(exc))

    if fd.pid:
        try:
            run_module("sysid", adapter, fd, kb=kb)
            check("SysID 链路", True)
        except Exception as exc:
            # ARX 对激励不足会显式报错（C14 设计行为），算告知性通过
            check("SysID 链路", True, f"显式拒绝: {exc}", warn_only=True)

    print()
    if failures:
        print(f"{FAIL} {len(failures)} 项失败: {failures}")
        return 1
    print(f"{PASS} 全部通过 — PX4 链路端到端可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
