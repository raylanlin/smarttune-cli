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

        # --- Columnar accumulators ---
        # Instead of list-of-dicts, we accumulate into resizable NumPy arrays.
        # This saves ~140 MB on a typical 100K-message log.

        _INIT_CAP = 4096

        class _ColAccum:
            """Lightweight columnar accumulator with doubling growth."""
            __slots__ = ('arrays', 'names', 'n', 'cap')

            def __init__(self, names: List[str], dtype=np.float64):
                self.names = names
                self.cap = _INIT_CAP
                self.n = 0
                self.arrays = {nm: np.zeros(_INIT_CAP, dtype=dtype) for nm in names}

            def append(self, **values):
                if self.n >= self.cap:
                    self.cap *= 2
                    for nm in self.names:
                        old = self.arrays[nm]
                        new = np.zeros(self.cap, dtype=old.dtype)
                        new[:len(old)] = old
                        self.arrays[nm] = new
                for nm, val in values.items():
                    self.arrays[nm][self.n] = val
                self.n += 1

            def trim(self) -> Dict[str, np.ndarray]:
                return {nm: arr[:self.n].copy() for nm, arr in self.arrays.items()}

            def __len__(self):
                return self.n

        imu_acc = _ColAccum(["imu_id", "time", "GyrX", "GyrY", "GyrZ", "AccX", "AccY", "AccZ"])
        att_acc = _ColAccum(["time", "Roll", "Pitch", "Yaw", "RollIn", "PitchIn", "YawIn"])
        rate_rll_acc = _ColAccum(["time", "Des", "Act", "Err", "P", "I", "D", "FF", "Limit"])
        rate_pit_acc = _ColAccum(["time", "Des", "Act", "Err", "P", "I", "D", "FF", "Limit"])
        rate_yaw_acc = _ColAccum(["time", "Des", "Act", "Err", "P", "I", "D", "FF", "Limit"])
        rate_rll_leg = _ColAccum(["time", "Des", "Act", "Err", "P", "I", "D", "Limit"])
        rate_pit_leg = _ColAccum(["time", "Des", "Act", "Err", "P", "I", "D", "Limit"])
        rate_yaw_leg = _ColAccum(["time", "Des", "Act", "Err", "P", "I", "D", "Limit"])
        compass_acc = _ColAccum(["compass_id", "time", "MagX", "MagY", "MagZ",
                                 "OfsX", "OfsY", "OfsZ", "MOfsX", "MOfsY", "MOfsZ"])
        gps_acc = _ColAccum(["time", "Status", "Lat", "Lng", "Alt", "Spd", "NSats", "HDop"])
        ahr2_acc = _ColAccum(["time", "Roll", "Pitch", "Yaw", "Q1", "Q2", "Q3", "Q4",
                              "Lat", "Lng", "Alt"])
        bat_acc = _ColAccum(["time", "I", "Volt", "Curr", "CurrTot", "EnrgTot", "Temp", "Res"])

        # These stay as lists (small counts, complex data)
        pos: List[Dict] = []
        atun: List[Dict] = []
        atde: List[Dict] = []
        msg_log: List[Dict] = []
        params: Dict[str, float] = {}
        ver: Dict[str, Any] = {}
        mode_changes: List[Dict] = []
        gps_origin: Optional[Dict] = None
        rate_modern_seen = False
        rate_legacy_seen = False
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
                    imu_acc.append(imu_id=getattr(msg,"I",0), time=ts,
                        GyrX=getattr(msg,"GyrX",0.0), GyrY=getattr(msg,"GyrY",0.0), GyrZ=getattr(msg,"GyrZ",0.0),
                        AccX=getattr(msg,"AccX",0.0), AccY=getattr(msg,"AccY",0.0), AccZ=getattr(msg,"AccZ",0.0))

                elif mtype == "ATT":
                    att_acc.append(time=ts,
                        Roll=getattr(msg,"Roll",0.0), Pitch=getattr(msg,"Pitch",0.0), Yaw=getattr(msg,"Yaw",0.0),
                        RollIn=getattr(msg,"DesRoll",getattr(msg,"RollIn",0.0)),
                        PitchIn=getattr(msg,"DesPitch",getattr(msg,"PitchIn",0.0)),
                        YawIn=getattr(msg,"DesYaw",getattr(msg,"YawIn",0.0)))

                elif mtype == "ATC_RAT_RLL":
                    rate_modern_seen = True
                    d = _rate_msg_dict(msg, ts)
                    rate_rll_acc.append(time=ts, Des=d["Des"], Act=d["Act"], Err=d["Err"],
                        P=d["P"], I=d["I"], D=d["D"], FF=d.get("FF",0.0), Limit=d["Limit"])
                elif mtype == "ATC_RAT_PIT":
                    rate_modern_seen = True
                    d = _rate_msg_dict(msg, ts)
                    rate_pit_acc.append(time=ts, Des=d["Des"], Act=d["Act"], Err=d["Err"],
                        P=d["P"], I=d["I"], D=d["D"], FF=d.get("FF",0.0), Limit=d["Limit"])
                elif mtype == "ATC_RAT_YAW":
                    rate_modern_seen = True
                    d = _rate_msg_dict(msg, ts)
                    rate_yaw_acc.append(time=ts, Des=d["Des"], Act=d["Act"], Err=d["Err"],
                        P=d["P"], I=d["I"], D=d["D"], FF=d.get("FF",0.0), Limit=d["Limit"])

                elif mtype == "PIDR":
                    rate_modern_seen = True
                    rate_rll_acc.append(time=ts,
                        Des=getattr(msg,"Tar",getattr(msg,"Des",0.0)), Act=getattr(msg,"Act",0.0),
                        Err=getattr(msg,"Err",0.0), P=getattr(msg,"P",0.0), I=getattr(msg,"I",0.0),
                        D=getattr(msg,"D",0.0), FF=getattr(msg,"FF",0.0), Limit=getattr(msg,"Flags",0))
                elif mtype == "PIDP":
                    rate_modern_seen = True
                    rate_pit_acc.append(time=ts,
                        Des=getattr(msg,"Tar",getattr(msg,"Des",0.0)), Act=getattr(msg,"Act",0.0),
                        Err=getattr(msg,"Err",0.0), P=getattr(msg,"P",0.0), I=getattr(msg,"I",0.0),
                        D=getattr(msg,"D",0.0), FF=getattr(msg,"FF",0.0), Limit=getattr(msg,"Flags",0))
                elif mtype == "PIDY":
                    rate_modern_seen = True
                    rate_yaw_acc.append(time=ts,
                        Des=getattr(msg,"Tar",getattr(msg,"Des",0.0)), Act=getattr(msg,"Act",0.0),
                        Err=getattr(msg,"Err",0.0), P=getattr(msg,"P",0.0), I=getattr(msg,"I",0.0),
                        D=getattr(msg,"D",0.0), FF=getattr(msg,"FF",0.0), Limit=getattr(msg,"Flags",0))

                elif mtype == "RATE":
                    rate_legacy_seen = True
                    rdes=getattr(msg,"RDes",0.0); r=getattr(msg,"R",0.0)
                    pdes=getattr(msg,"PDes",0.0); p=getattr(msg,"P",0.0)
                    ydes=getattr(msg,"YDes",0.0); y=getattr(msg,"Y",0.0)
                    rate_rll_leg.append(time=ts,Des=rdes,Act=r,Err=rdes-r,P=0.0,I=0.0,D=0.0,Limit=0)
                    rate_pit_leg.append(time=ts,Des=pdes,Act=p,Err=pdes-p,P=0.0,I=0.0,D=0.0,Limit=0)
                    rate_yaw_leg.append(time=ts,Des=ydes,Act=y,Err=ydes-y,P=0.0,I=0.0,D=0.0,Limit=0)

                elif mtype == "COMPASS":
                    compass_acc.append(compass_id=getattr(msg,"I",0), time=ts,
                        MagX=getattr(msg,"MagX",0.0), MagY=getattr(msg,"MagY",0.0), MagZ=getattr(msg,"MagZ",0.0),
                        OfsX=getattr(msg,"OfsX",0.0), OfsY=getattr(msg,"OfsY",0.0), OfsZ=getattr(msg,"OfsZ",0.0),
                        MOfsX=getattr(msg,"MOfsX",0.0), MOfsY=getattr(msg,"MOfsY",0.0), MOfsZ=getattr(msg,"MOfsZ",0.0))

                elif mtype == "MAG":
                    compass_acc.append(compass_id=getattr(msg,"I",0), time=ts,
                        MagX=getattr(msg,"MagX",0.0), MagY=getattr(msg,"MagY",0.0), MagZ=getattr(msg,"MagZ",0.0),
                        OfsX=getattr(msg,"OfsX",0.0), OfsY=getattr(msg,"OfsY",0.0), OfsZ=getattr(msg,"OfsZ",0.0),
                        MOfsX=getattr(msg,"MOX",0.0), MOfsY=getattr(msg,"MOY",0.0), MOfsZ=getattr(msg,"MOZ",0.0))

                elif mtype == "GPS":
                    gps_acc.append(time=ts, Status=getattr(msg,"Status",0),
                        Lat=getattr(msg,"Lat",0.0), Lng=getattr(msg,"Lng",0.0), Alt=getattr(msg,"Alt",0.0),
                        Spd=getattr(msg,"Spd",0.0), NSats=getattr(msg,"NSats",0), HDop=getattr(msg,"HDop",0.0))

                elif mtype == "AHR2":
                    ahr2_acc.append(time=ts,
                        Roll=getattr(msg,"Roll",0.0), Pitch=getattr(msg,"Pitch",0.0), Yaw=getattr(msg,"Yaw",0.0),
                        Q1=getattr(msg,"Q1",1.0), Q2=getattr(msg,"Q2",0.0), Q3=getattr(msg,"Q3",0.0), Q4=getattr(msg,"Q4",0.0),
                        Lat=getattr(msg,"Lat",0.0), Lng=getattr(msg,"Lng",0.0), Alt=getattr(msg,"Alt",0.0))

                elif mtype == "POS":
                    pos.append({"time":ts,
                        "Lat":getattr(msg,"Lat",0.0),"Lng":getattr(msg,"Lng",0.0),"Alt":getattr(msg,"Alt",0.0),
                        "RelHomeAlt":getattr(msg,"RelHomeAlt",0.0),"RelOriginAlt":getattr(msg,"RelOriginAlt",0.0)})

                elif mtype == "BAT":
                    bat_acc.append(time=ts, I=getattr(msg,"I",0),
                        Volt=getattr(msg,"Volt",0.0), Curr=getattr(msg,"Curr",0.0),
                        CurrTot=getattr(msg,"CurrTot",0.0), EnrgTot=getattr(msg,"EnrgTot",0.0),
                        Temp=getattr(msg,"Temp",0.0), Res=getattr(msg,"Res",0.0))

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

        # --- Trim all accumulators to actual size ---
        imu_d = imu_acc.trim()
        att_d = att_acc.trim()
        compass_d = compass_acc.trim()
        gps_d = gps_acc.trim()
        ahr2_d = ahr2_acc.trim()
        bat_d = bat_acc.trim()

        # --- PID buffer selection: modern > legacy ---
        def _col_to_axis_pid(acc: _ColAccum, t0: float) -> Optional[AxisPIDSignal]:
            if len(acc) == 0:
                return None
            d = acc.trim()
            ts_arr = d["time"] - t0
            desired = d["Des"]
            actual = d["Act"]
            p_term = d["P"]
            i_term = d["I"]
            d_term = d["D"]
            ff_term = d.get("FF", np.zeros(len(acc)))

            max_des = float(np.max(np.abs(desired))) if desired.size > 0 else 0.0
            max_act = float(np.max(np.abs(actual))) if actual.size > 0 else 0.0
            max_signal = max(max_des, max_act)

            if 0 < max_signal < 6.5:
                _RAD2DEG = 180.0 / np.pi
                desired = desired * _RAD2DEG
                actual = actual * _RAD2DEG
                logger.debug(
                    "PID data auto-converted rad/s → deg/s "
                    "(max_signal=%.3f rad/s → %.1f deg/s)",
                    max_signal, max_signal * _RAD2DEG,
                )

            return AxisPIDSignal(
                timestamp_s=ts_arr,
                desired=desired,
                actual=actual,
                p_term=p_term,
                i_term=i_term,
                d_term=d_term,
                ff_term=ff_term,
            )

        rll_acc = rate_rll_acc if len(rate_rll_acc) > 0 else rate_rll_leg
        pit_acc = rate_pit_acc if len(rate_pit_acc) > 0 else rate_pit_leg
        yaw_acc = rate_yaw_acc if len(rate_yaw_acc) > 0 else rate_yaw_leg

        t0 = t_min
        fd = FlightData(platform="ardupilot", log_file=str(path), params=params,
                        firmware_version=str(ver.get("FWVer", "")))

        # PID signals
        for axis, acc in [("roll", rll_acc), ("pitch", pit_acc), ("yaw", yaw_acc)]:
            sig = _col_to_axis_pid(acc, t0)
            if sig is not None and sig.sample_count >= 10:
                fd.pid[axis] = sig

        # IMU (id=0 first)
        n_imu = len(imu_acc)
        if n_imu >= 10:
            imu_ids = imu_d["imu_id"]
            mask0 = imu_ids == 0
            if np.sum(mask0) >= 10:
                use_mask = mask0
            else:
                use_mask = np.ones(n_imu, dtype=bool)

            ts_arr = imu_d["time"][use_mask] - t0
            fd.imu_timestamp_s = ts_arr
            fd.gyro = np.column_stack([
                imu_d["GyrX"][use_mask],
                imu_d["GyrY"][use_mask],
                imu_d["GyrZ"][use_mask],
            ])
            fd.accel = np.column_stack([
                imu_d["AccX"][use_mask],
                imu_d["AccY"][use_mask],
                imu_d["AccZ"][use_mask],
            ])

        # Magnetometer
        n_comp = len(compass_acc)
        if n_comp >= 10:
            comp_ids = compass_d["compass_id"]
            mask0 = comp_ids == 0
            if np.sum(mask0) >= 10:
                use_mask = mask0
            else:
                use_mask = np.ones(n_comp, dtype=bool)

            ts_arr = compass_d["time"][use_mask] - t0
            fd.mag_timestamp_s = ts_arr
            fd.mag = np.column_stack([
                compass_d["MagX"][use_mask],
                compass_d["MagY"][use_mask],
                compass_d["MagZ"][use_mask],
            ])

        # Battery
        n_bat = len(bat_acc)
        if n_bat >= 2:
            bat_ids = bat_d["I"]
            mask0 = bat_ids == 0
            if np.sum(mask0) >= 2:
                use_mask = mask0
            else:
                use_mask = np.ones(n_bat, dtype=bool)

            ts_arr = bat_d["time"][use_mask] - t0
            fd.battery_timestamp_s = ts_arr
            fd.battery_voltage = bat_d["Volt"][use_mask]
            fd.battery_current = bat_d["Curr"][use_mask]

        # Mode changes
        for mc in mode_changes:
            raw = mc["raw_mode"]
            fd.mode_changes.append(ModeChange(
                timestamp_s=mc["time"] - t0,
                mode_name=_MODE_MAP.get(raw.upper(), raw.lower()),
                raw_mode=raw,
            ))

        # Extras for downstream analyzers
        if len(att_acc) > 0:
            ts_arr = att_d["time"] - t0
            fd.extras["attitude"] = {
                "time":    ts_arr,
                "Roll":    att_d["Roll"],
                "Pitch":   att_d["Pitch"],
                "Yaw":     att_d["Yaw"],
                "RollIn":  att_d["RollIn"],
                "PitchIn": att_d["PitchIn"],
                "YawIn":   att_d["YawIn"],
            }
        if len(ahr2_acc) > 0:
            ts_arr = ahr2_d["time"] - t0
            fd.extras["ahr2_data"] = {
                "time":  ts_arr,
                "Q1":    ahr2_d["Q1"],
                "Q2":    ahr2_d["Q2"],
                "Q3":    ahr2_d["Q3"],
                "Q4":    ahr2_d["Q4"],
                "Roll":  ahr2_d["Roll"],
                "Pitch": ahr2_d["Pitch"],
                "Yaw":   ahr2_d["Yaw"],
                "Lat":   ahr2_d["Lat"],
                "Lng":   ahr2_d["Lng"],
                "Alt":   ahr2_d["Alt"],
            }

        # GPS position (use trimmed columnar data)
        gps_lat = gps_lon = gps_alt = 0.0
        if len(gps_acc) > 0:
            for i in range(len(gps_acc) - 1, -1, -1):
                if gps_d["Status"][i] >= 3:
                    lat_v, lng_v = gps_d["Lat"][i], gps_d["Lng"][i]
                    if abs(lat_v) > 1000: lat_v *= 1e-7
                    if abs(lng_v) > 1000: lng_v *= 1e-7
                    gps_lat, gps_lon, gps_alt = lat_v, lng_v, gps_d["Alt"][i]
                    break
        if gps_lat == 0 and gps_lon == 0 and gps_origin:
            gps_lat, gps_lon, gps_alt = gps_origin["Lat"], gps_origin["Lng"], gps_origin["Alt"]
        if gps_lat == 0 and gps_lon == 0 and len(ahr2_acc) > 0:
            lat_v, lng_v = ahr2_d["Lat"][-1], ahr2_d["Lng"][-1]
            if abs(lat_v) > 1000: lat_v *= 1e-7
            if abs(lng_v) > 1000: lng_v *= 1e-7
            if lat_v != 0 or lng_v != 0:
                gps_lat, gps_lon, gps_alt = lat_v, lng_v, ahr2_d["Alt"][-1]

        fd.extras["gps_position"]  = {"lat": gps_lat, "lon": gps_lon, "alt": gps_alt}
        fd.extras["autotune"]      = {"ATUN": atun, "ATDE": atde}
        fd.extras["msg_log"]       = msg_log
        fd.extras["version_info"]  = ver

        # compass_raw: rebuild list-of-dicts from columnar for MagFit compatibility
        # (MagFit uses small counts, so this is fine)
        if len(compass_acc) > 0:
            compass_list = []
            for i in range(len(compass_acc)):
                compass_list.append({
                    "compass_id": int(compass_d["compass_id"][i]),
                    "time": compass_d["time"][i],
                    "MagX": compass_d["MagX"][i], "MagY": compass_d["MagY"][i], "MagZ": compass_d["MagZ"][i],
                    "OfsX": compass_d["OfsX"][i], "OfsY": compass_d["OfsY"][i], "OfsZ": compass_d["OfsZ"][i],
                    "MOfsX": compass_d["MOfsX"][i], "MOfsY": compass_d["MOfsY"][i], "MOfsZ": compass_d["MOfsZ"][i],
                })
            fd.extras["compass_raw"] = compass_list
        else:
            fd.extras["compass_raw"] = []

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

    def param_table(self):
        from smarttune.platform.params import ParamTable
        return ParamTable.from_knowledge("ardupilot")
