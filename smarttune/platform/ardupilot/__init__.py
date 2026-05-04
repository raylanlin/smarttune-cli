"""
smarttune/platform/ardupilot/__init__.py

ArduPilot DataFlash 日志适配器。

parse() 完整移植自旧版 LogParser._dispatch()，保留全部消息类型处理：
PIDR/PIDP/PIDY、ATC_RAT_*、RATE (legacy)、IMU、ATT、GYRO、
COMPASS、MAG、GPS、AHR2、POS、BAT、VER、MSG、PARM、ORGN、ATUN、ATDE

PID 格式优先级（与旧版一致）:
  PIDR/PIDP/PIDY (modern)  >  ATC_RAT_RLL/PIT/YAW (modern)  >  RATE (legacy fallback)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from smarttune.platform.base import PlatformAdapter
from smarttune.platform.registry import register
from smarttune.models.flight_data import AxisPIDSignal, FlightData, ModeChange
from smarttune.errors import (
    LogFileNotFoundError,
    LogFileCorruptError,
    LogFormatError,
)

logger = logging.getLogger(__name__)

_PARAM_MAP_TO_PLATFORM: Dict[str, str] = {
    "pid.roll.p":       "ATC_RAT_RLL_P",
    "pid.roll.i":       "ATC_RAT_RLL_I",
    "pid.roll.d":       "ATC_RAT_RLL_D",
    "pid.roll.ff":      "ATC_RAT_RLL_FF",
    "pid.roll.filt":    "ATC_RAT_RLL_FLTD",
    "pid.roll.filt_t":  "ATC_RAT_RLL_FLTT",
    "pid.pitch.p":      "ATC_RAT_PIT_P",
    "pid.pitch.i":      "ATC_RAT_PIT_I",
    "pid.pitch.d":      "ATC_RAT_PIT_D",
    "pid.pitch.ff":     "ATC_RAT_PIT_FF",
    "pid.pitch.filt":   "ATC_RAT_PIT_FLTD",
    "pid.pitch.filt_t": "ATC_RAT_PIT_FLTT",
    "pid.yaw.p":        "ATC_RAT_YAW_P",
    "pid.yaw.i":        "ATC_RAT_YAW_I",
    "pid.yaw.d":        "ATC_RAT_YAW_D",
    "pid.yaw.ff":       "ATC_RAT_YAW_FF",
    "pid.yaw.filt":     "ATC_RAT_YAW_FLTD",
    "pid.yaw.filt_t":   "ATC_RAT_YAW_FLTT",
    "filter.gyro_lpf":      "INS_GYRO_FILTER",
    "filter.accel_lpf":     "INS_ACCEL_FILTER",
    "filter.notch1.enable": "INS_HNTCH_ENABLE",
    "filter.notch1.freq":   "INS_HNTCH_FREQ",
    "filter.notch1.bw":     "INS_HNTCH_BW",
    "filter.notch1.att":    "INS_HNTCH_ATT",
    "filter.notch1.mode":   "INS_HNTCH_MODE",
    "filter.notch2.enable": "INS_HNTC2_ENABLE",
    "filter.notch2.freq":   "INS_HNTC2_FREQ",
    "filter.notch2.bw":     "INS_HNTC2_BW",
    "filter.notch2.att":    "INS_HNTC2_ATT",
    "filter.notch2.mode":   "INS_HNTC2_MODE",
    "mag.ofs.x": "COMPASS_OFS_X",
    "mag.ofs.y": "COMPASS_OFS_Y",
    "mag.ofs.z": "COMPASS_OFS_Z",
}

_PARAM_MAP_TO_GENERIC: Dict[str, str] = {v: k for k, v in _PARAM_MAP_TO_PLATFORM.items()}

_MODE_MAP: Dict[str, str] = {
    "STABILIZE": "stabilize", "ALT_HOLD": "althold", "LOITER": "loiter",
    "AUTO": "auto", "GUIDED": "guided", "LAND": "land", "RTL": "rtl",
    "ACRO": "acro", "POSHOLD": "poshold", "AUTOTUNE": "autotune",
}

_AP_MAGIC = b"\xa3\x95"


def _rate_msg_dict(msg: Any, ts: float) -> Dict[str, Any]:
    d = {
        "time":  ts,
        "Des":   getattr(msg, "Des", 0.0),
        "Act":   getattr(msg, "Act", 0.0),
        "Err":   getattr(msg, "Err", 0.0),
        "P":     getattr(msg, "P",   0.0),
        "I":     getattr(msg, "I",   0.0),
        "D":     getattr(msg, "D",   0.0),
        "Limit": int(getattr(msg, "Limit", 0)),
    }
    ff = getattr(msg, "FF", None)
    if ff is not None:
        d["FF"] = ff
    return d


def _to_axis_pid(store: List[Dict], t0: float) -> Optional[AxisPIDSignal]:
    if not store:
        return None
    ts = np.array([m["time"] for m in store], dtype=np.float64) - t0
    desired = np.array([m["Des"]  for m in store], dtype=np.float64)
    actual  = np.array([m["Act"]  for m in store], dtype=np.float64)
    p_term  = np.array([m["P"]    for m in store], dtype=np.float64)
    i_term  = np.array([m["I"]    for m in store], dtype=np.float64)
    d_term  = np.array([m["D"]    for m in store], dtype=np.float64)
    ff_term = np.array([m.get("FF", 0.0) for m in store], dtype=np.float64)

    # ── Unit normalisation ────────────────────────────────────────────
    # FlightData contract: desired/actual must be deg/s.
    #
    # ArduPilot PIDR/PIDP/PIDY Tar/Act are in **rad/s** in firmware ≥ 4.0
    # (the internal rate controller operates in rad/s).
    # Legacy RATE messages (RDes/R) are in deg/s.
    # ATC_RAT_RLL/PIT/YAW Des/Act can be either, depending on firmware.
    #
    # Heuristic: if max |desired| < 6.5 rad/s (≈370 deg/s) AND the data
    # has enough dynamic range, it is very likely in rad/s because typical
    # aggressive stick inputs reach ~20 deg/s (0.35 rad/s) and extreme
    # manoeuvres might reach ~300 deg/s (5.2 rad/s).  A cutoff at 6.5
    # avoids false positives: 6.5 deg/s is already a very sluggish input
    # that would rarely be seen in stabilised flight.
    #
    # More precise test: if max(|desired|) < 6.5 AND we can verify the
    # range is plausible for rad/s (i.e., ≤ ~6 rad/s), convert.
    # Conversely, genuine deg/s data typically has max > 10.
    max_des = float(np.max(np.abs(desired))) if desired.size > 0 else 0.0
    max_act = float(np.max(np.abs(actual)))  if actual.size > 0 else 0.0
    max_signal = max(max_des, max_act)

    if 0 < max_signal < 6.5:
        # Almost certainly rad/s → convert to deg/s
        _RAD2DEG = 180.0 / np.pi  # ≈ 57.2958
        desired *= _RAD2DEG
        actual  *= _RAD2DEG
        # P/I/D/FF terms are internal controller outputs, they are NOT
        # angular rates and must NOT be converted.  They are dimensionless
        # controller gains × error and are consumed as-is by downstream.
        logger.debug(
            "PID data auto-converted rad/s → deg/s "
            "(max_signal=%.3f rad/s → %.1f deg/s)",
            max_signal, max_signal * _RAD2DEG,
        )

    return AxisPIDSignal(
        timestamp_s=ts,
        desired=desired,
        actual=actual,
        p_term=p_term,
        i_term=i_term,
        d_term=d_term,
        ff_term=ff_term,
    )


def _gps_position(gps, ahr2, gps_origin) -> Tuple[float, float, float]:
    if gps:
        for m in reversed(gps):
            if m["Status"] >= 3:
                lat, lng = m["Lat"], m["Lng"]
                if abs(lat) > 1000: lat *= 1e-7
                if abs(lng) > 1000: lng *= 1e-7
                return lat, lng, m["Alt"]
    if gps_origin:
        return gps_origin["Lat"], gps_origin["Lng"], gps_origin["Alt"]
    if ahr2:
        m = ahr2[-1]
        lat, lng = m["Lat"], m["Lng"]
        if abs(lat) > 1000: lat *= 1e-7
        if abs(lng) > 1000: lng *= 1e-7
        if lat != 0 or lng != 0:
            return lat, lng, m["Alt"]
    return 0.0, 0.0, 0.0


@register
class ArduPilotAdapter(PlatformAdapter):

    @property
    def name(self) -> str:
        return "ardupilot"

    @property
    def display_name(self) -> str:
        return "ArduPilot"

    @property
    def supported_extensions(self) -> list[str]:
        return [".bin", ".log"]

    @classmethod
    def detect(cls, path: Path) -> bool:
        if not path.is_file():
            return False
        suffix = path.suffix.lower()
        if suffix == ".bin":
            try:
                with open(path, "rb") as f:
                    return f.read(2) == _AP_MAGIC
            except OSError:
                return False
        if suffix == ".log":
            try:
                with open(path, "r", errors="ignore") as f:
                    head = f.read(1024)
                return any(tok in head for tok in ("FMT", "IMU", "PARM"))
            except OSError:
                return False
        return False

    def parse(self, path: Path) -> FlightData:  # noqa: C901
        from pymavlink import mavutil
        from pymavlink.DFReader import DFReader_binary

        if not path.is_file():
            raise LogFileNotFoundError(message=f"Log file not found: {path}")

        try:
            with open(path, "rb") as f:
                head = f.read(4)
            if head[:3] == b"\xa3\x95\x80":
                mlog = DFReader_binary(str(path))
            else:
                mlog = mavutil.mavlink_connection(str(path), robust_parsing=True, notimestamps=True)
        except Exception as exc:
            raise LogFileCorruptError(message=f"Cannot open log: {exc}") from exc

        # --- message buffers ---
        imu: List[Dict] = []
        att: List[Dict] = []
        gyro: List[Dict] = []
        rate_rll: List[Dict] = []; rate_pit: List[Dict] = []; rate_yaw: List[Dict] = []
        rate_rll_legacy: List[Dict] = []; rate_pit_legacy: List[Dict] = []; rate_yaw_legacy: List[Dict] = []
        rate_modern_seen = False; rate_legacy_seen = False
        compass: List[Dict] = []
        gps: List[Dict] = []; ahr2: List[Dict] = []; pos: List[Dict] = []
        bat: List[Dict] = []; atun: List[Dict] = []; atde: List[Dict] = []
        msg_log: List[Dict] = []
        params: Dict[str, float] = {}
        ver: Dict[str, Any] = {}
        mode_changes: List[Dict] = []
        gps_origin: Optional[Dict] = None
        t_min = t_max = 0.0; t_start_set = False; msg_count = 0

        try:
            while True:
                msg = mlog.recv_match()
                if msg is None:
                    break
                msg_count += 1
                mtype = msg.get_type()
                ts = getattr(msg, "_timestamp", None)
                if ts is None:
                    continue
                if not t_start_set:
                    t_min = ts; t_start_set = True
                t_max = ts

                if mtype == "IMU":
                    imu.append({"imu_id": getattr(msg,"I",0), "time": ts,
                        "GyrX": getattr(msg,"GyrX",0.0), "GyrY": getattr(msg,"GyrY",0.0), "GyrZ": getattr(msg,"GyrZ",0.0),
                        "AccX": getattr(msg,"AccX",0.0), "AccY": getattr(msg,"AccY",0.0), "AccZ": getattr(msg,"AccZ",0.0)})

                elif mtype == "ATT":
                    att.append({"time": ts,
                        "Roll":    getattr(msg,"Roll",   0.0), "Pitch":   getattr(msg,"Pitch",  0.0), "Yaw":     getattr(msg,"Yaw",    0.0),
                        "RollIn":  getattr(msg,"DesRoll",  getattr(msg,"RollIn",  0.0)),
                        "PitchIn": getattr(msg,"DesPitch", getattr(msg,"PitchIn", 0.0)),
                        "YawIn":   getattr(msg,"DesYaw",   getattr(msg,"YawIn",   0.0))})

                elif mtype == "GYRO":
                    gyro.append({"time": ts,
                        "GyrX": getattr(msg,"GyrX",0.0), "GyrY": getattr(msg,"GyrY",0.0), "GyrZ": getattr(msg,"GyrZ",0.0)})

                elif mtype == "ATC_RAT_RLL":
                    rate_modern_seen = True; rate_rll.append(_rate_msg_dict(msg, ts))
                elif mtype == "ATC_RAT_PIT":
                    rate_modern_seen = True; rate_pit.append(_rate_msg_dict(msg, ts))
                elif mtype == "ATC_RAT_YAW":
                    rate_modern_seen = True; rate_yaw.append(_rate_msg_dict(msg, ts))

                elif mtype == "PIDR":
                    rate_modern_seen = True
                    rate_rll.append({"time":ts, "Des":getattr(msg,"Tar",getattr(msg,"Des",0.0)), "Act":getattr(msg,"Act",0.0),
                        "Err":getattr(msg,"Err",0.0), "P":getattr(msg,"P",0.0), "I":getattr(msg,"I",0.0),
                        "D":getattr(msg,"D",0.0), "FF":getattr(msg,"FF",0.0), "Limit":getattr(msg,"Flags",0)})
                elif mtype == "PIDP":
                    rate_modern_seen = True
                    rate_pit.append({"time":ts, "Des":getattr(msg,"Tar",getattr(msg,"Des",0.0)), "Act":getattr(msg,"Act",0.0),
                        "Err":getattr(msg,"Err",0.0), "P":getattr(msg,"P",0.0), "I":getattr(msg,"I",0.0),
                        "D":getattr(msg,"D",0.0), "FF":getattr(msg,"FF",0.0), "Limit":getattr(msg,"Flags",0)})
                elif mtype == "PIDY":
                    rate_modern_seen = True
                    rate_yaw.append({"time":ts, "Des":getattr(msg,"Tar",getattr(msg,"Des",0.0)), "Act":getattr(msg,"Act",0.0),
                        "Err":getattr(msg,"Err",0.0), "P":getattr(msg,"P",0.0), "I":getattr(msg,"I",0.0),
                        "D":getattr(msg,"D",0.0), "FF":getattr(msg,"FF",0.0), "Limit":getattr(msg,"Flags",0)})

                elif mtype == "RATE":
                    rate_legacy_seen = True
                    rdes=getattr(msg,"RDes",0.0); r=getattr(msg,"R",0.0)
                    pdes=getattr(msg,"PDes",0.0); p=getattr(msg,"P",0.0)
                    ydes=getattr(msg,"YDes",0.0); y=getattr(msg,"Y",0.0)
                    rate_rll_legacy.append({"time":ts,"Des":rdes,"Act":r,"Err":rdes-r,"P":0.0,"I":0.0,"D":0.0,"Limit":0})
                    rate_pit_legacy.append({"time":ts,"Des":pdes,"Act":p,"Err":pdes-p,"P":0.0,"I":0.0,"D":0.0,"Limit":0})
                    rate_yaw_legacy.append({"time":ts,"Des":ydes,"Act":y,"Err":ydes-y,"P":0.0,"I":0.0,"D":0.0,"Limit":0})

                elif mtype == "COMPASS":
                    compass.append({"compass_id":getattr(msg,"I",0),"time":ts,
                        "MagX":getattr(msg,"MagX",0.0),"MagY":getattr(msg,"MagY",0.0),"MagZ":getattr(msg,"MagZ",0.0),
                        "OfsX":getattr(msg,"OfsX",0.0),"OfsY":getattr(msg,"OfsY",0.0),"OfsZ":getattr(msg,"OfsZ",0.0),
                        "MOfsX":getattr(msg,"MOfsX",0.0),"MOfsY":getattr(msg,"MOfsY",0.0),"MOfsZ":getattr(msg,"MOfsZ",0.0)})

                elif mtype == "MAG":
                    compass.append({"compass_id":getattr(msg,"I",0),"time":ts,
                        "MagX":getattr(msg,"MagX",0.0),"MagY":getattr(msg,"MagY",0.0),"MagZ":getattr(msg,"MagZ",0.0),
                        "OfsX":getattr(msg,"OfsX",0.0),"OfsY":getattr(msg,"OfsY",0.0),"OfsZ":getattr(msg,"OfsZ",0.0),
                        "MOfsX":getattr(msg,"MOX",0.0),"MOfsY":getattr(msg,"MOY",0.0),"MOfsZ":getattr(msg,"MOZ",0.0)})

                elif mtype == "GPS":
                    gps.append({"time":ts, "Status":getattr(msg,"Status",0),
                        "Lat":getattr(msg,"Lat",0.0),"Lng":getattr(msg,"Lng",0.0),"Alt":getattr(msg,"Alt",0.0),
                        "Spd":getattr(msg,"Spd",0.0),"NSats":getattr(msg,"NSats",0),"HDop":getattr(msg,"HDop",0.0)})

                elif mtype == "AHR2":
                    ahr2.append({"time":ts,
                        "Roll":getattr(msg,"Roll",0.0),"Pitch":getattr(msg,"Pitch",0.0),"Yaw":getattr(msg,"Yaw",0.0),
                        "Q1":getattr(msg,"Q1",1.0),"Q2":getattr(msg,"Q2",0.0),"Q3":getattr(msg,"Q3",0.0),"Q4":getattr(msg,"Q4",0.0),
                        "Lat":getattr(msg,"Lat",0.0),"Lng":getattr(msg,"Lng",0.0),"Alt":getattr(msg,"Alt",0.0)})

                elif mtype == "POS":
                    pos.append({"time":ts,
                        "Lat":getattr(msg,"Lat",0.0),"Lng":getattr(msg,"Lng",0.0),"Alt":getattr(msg,"Alt",0.0),
                        "RelHomeAlt":getattr(msg,"RelHomeAlt",0.0),"RelOriginAlt":getattr(msg,"RelOriginAlt",0.0)})

                elif mtype == "BAT":
                    bat.append({"time":ts, "I":getattr(msg,"I",0),
                        "Volt":getattr(msg,"Volt",0.0),"Curr":getattr(msg,"Curr",0.0),
                        "CurrTot":getattr(msg,"CurrTot",0.0),"EnrgTot":getattr(msg,"EnrgTot",0.0),
                        "Temp":getattr(msg,"Temp",0.0),"Res":getattr(msg,"Res",0.0)})

                elif mtype == "PARM":
                    raw_name = getattr(msg, "Name", b"")
                    name = raw_name.decode("utf-8", errors="replace").strip("\x00") if isinstance(raw_name, bytes) else str(raw_name).strip("\x00")
                    try:
                        params[name] = float(getattr(msg, "Value", 0.0))
                    except (TypeError, ValueError):
                        pass

                elif mtype == "MODE":
                    raw = str(getattr(msg, "Mode", getattr(msg, "ModeNum", "")))
                    mode_changes.append({"time": ts, "raw_mode": raw})

                elif mtype == "VER":
                    ver = {"time":ts, "FWVer":getattr(msg,"FWVer",""),
                           "APJ":getattr(msg,"APJ",0), "GH":getattr(msg,"GH",""), "FV":getattr(msg,"FV",0)}

                elif mtype == "MSG":
                    raw_text = getattr(msg, "Message", b"")
                    if isinstance(raw_text, bytes):
                        raw_text = raw_text.decode("utf-8", errors="replace")
                    msg_log.append({"time": ts, "text": str(raw_text)})

                elif mtype == "ORGN":
                    lat = getattr(msg,"Lat",0.0); lng = getattr(msg,"Lng",0.0); alt = getattr(msg,"Alt",0.0)
                    if lat != 0 or lng != 0:
                        gps_origin = {
                            "Lat": lat*1e-7 if abs(lat)>1000 else lat,
                            "Lng": lng*1e-7 if abs(lng)>1000 else lng,
                            "Alt": alt}

                elif mtype == "ATUN":
                    atun.append({"time":ts,"axis":getattr(msg,"Axis",-1),
                        "P":getattr(msg,"P",0.0),"I":getattr(msg,"I",0.0),"D":getattr(msg,"D",0.0)})

                elif mtype == "ATDE":
                    atde.append({"time":ts,"axis":getattr(msg,"Axis",-1),"step":getattr(msg,"Step",-1),
                        "wave":getattr(msg,"Wave",-1),"rate":getattr(msg,"Rate",0.0),
                        "P":getattr(msg,"P",0.0),"I":getattr(msg,"I",0.0),"D":getattr(msg,"D",0.0)})

        except (LogFileCorruptError, LogFormatError):
            raise
        except Exception as exc:
            raise LogFileCorruptError(
                message=f"Parsing error after {msg_count} messages: {exc}",
                hint="Log may have been interrupted mid-write.",
            ) from exc

        if rate_legacy_seen and not rate_modern_seen:
            logger.warning(
                "Log only has legacy RATE messages (no PIDR/ATC_RAT_*). "
                "P/I/D fields will be 0. Use firmware >= 4.0 for full analysis."
            )

        # --- PID buffer selection: modern > legacy ---
        rll_buf = rate_rll if rate_rll else rate_rll_legacy
        pit_buf = rate_pit if rate_pit else rate_pit_legacy
        yaw_buf = rate_yaw if rate_yaw else rate_yaw_legacy

        t0 = t_min
        fd = FlightData(platform="ardupilot", log_file=str(path), params=params,
                        firmware_version=str(ver.get("FWVer", "")))

        # PID signals
        for axis, buf in [("roll", rll_buf), ("pitch", pit_buf), ("yaw", yaw_buf)]:
            sig = _to_axis_pid(buf, t0)
            if sig is not None and sig.sample_count >= 10:
                fd.pid[axis] = sig

        # IMU (id=0 first)
        imu0 = [m for m in imu if m["imu_id"] == 0] or imu
        if len(imu0) >= 10:
            ts_arr = np.array([m["time"] for m in imu0]) - t0
            fd.imu_timestamp_s = ts_arr
            fd.gyro  = np.column_stack([[m["GyrX"] for m in imu0],[m["GyrY"] for m in imu0],[m["GyrZ"] for m in imu0]])
            fd.accel = np.column_stack([[m["AccX"] for m in imu0],[m["AccY"] for m in imu0],[m["AccZ"] for m in imu0]])

        # Magnetometer
        mag0 = [m for m in compass if m["compass_id"] == 0]
        if len(mag0) >= 10:
            ts_arr = np.array([m["time"] for m in mag0]) - t0
            fd.mag_timestamp_s = ts_arr
            fd.mag = np.column_stack([[m["MagX"] for m in mag0],[m["MagY"] for m in mag0],[m["MagZ"] for m in mag0]])

        # Battery
        bat0 = [m for m in bat if m.get("I", 0) == 0] or bat
        if len(bat0) >= 2:
            ts_arr = np.array([m["time"] for m in bat0]) - t0
            fd.battery_timestamp_s = ts_arr
            fd.battery_voltage = np.array([m["Volt"] for m in bat0])
            fd.battery_current  = np.array([m["Curr"] for m in bat0])

        # Mode changes
        for mc in mode_changes:
            raw = mc["raw_mode"]
            fd.mode_changes.append(ModeChange(
                timestamp_s=mc["time"] - t0,
                mode_name=_MODE_MAP.get(raw.upper(), raw.lower()),
                raw_mode=raw,
            ))

        # Extras for downstream analyzers
        if att:
            ts_arr = np.array([m["time"] for m in att]) - t0
            fd.extras["attitude"] = {
                "time": ts_arr,
                "Roll":    np.array([m["Roll"]    for m in att]),
                "Pitch":   np.array([m["Pitch"]   for m in att]),
                "Yaw":     np.array([m["Yaw"]     for m in att]),
                "RollIn":  np.array([m["RollIn"]  for m in att]),
                "PitchIn": np.array([m["PitchIn"] for m in att]),
                "YawIn":   np.array([m["YawIn"]   for m in att]),
            }
        if ahr2:
            ts_arr = np.array([m["time"] for m in ahr2]) - t0
            fd.extras["ahr2_data"] = {
                "time":  ts_arr,
                "Q1":    np.array([m["Q1"]    for m in ahr2]),
                "Q2":    np.array([m["Q2"]    for m in ahr2]),
                "Q3":    np.array([m["Q3"]    for m in ahr2]),
                "Q4":    np.array([m["Q4"]    for m in ahr2]),
                "Roll":  np.array([m["Roll"]  for m in ahr2]),
                "Pitch": np.array([m["Pitch"] for m in ahr2]),
                "Yaw":   np.array([m["Yaw"]   for m in ahr2]),
                "Lat":   np.array([m["Lat"]   for m in ahr2]),
                "Lng":   np.array([m["Lng"]   for m in ahr2]),
                "Alt":   np.array([m["Alt"]   for m in ahr2]),
            }

        lat, lon, alt = _gps_position(gps, ahr2, gps_origin)
        fd.extras["gps_position"]  = {"lat": lat, "lon": lon, "alt": alt}
        fd.extras["autotune"]      = {"ATUN": atun, "ATDE": atde}
        fd.extras["msg_log"]       = msg_log
        fd.extras["version_info"]  = ver
        fd.extras["compass_raw"]   = compass   # OfsX/OfsY/OfsZ for MagFit

        # Timing
        fd.duration_s = t_max - t_min
        if fd.imu_timestamp_s is not None and len(fd.imu_timestamp_s) > 1:
            dt = float(np.median(np.diff(fd.imu_timestamp_s)))
            fd.sample_rate_hz = 1.0 / dt if dt > 0 else 0.0
        elif fd.pid:
            sig = next(iter(fd.pid.values()))
            if sig.sample_count > 1:
                dt = float(np.median(np.diff(sig.timestamp_s)))
                fd.sample_rate_hz = 1.0 / dt if dt > 0 else 0.0

        return fd

    def map_param_to_platform(self, generic_name: str) -> str:
        return _PARAM_MAP_TO_PLATFORM.get(generic_name, generic_name)

    def map_param_to_generic(self, platform_name: str) -> str:
        return _PARAM_MAP_TO_GENERIC.get(platform_name, platform_name)

    def capabilities(self) -> Set[str]:
        return {"pid", "fft", "filter", "sysid", "magfit", "hardware", "quality"}
