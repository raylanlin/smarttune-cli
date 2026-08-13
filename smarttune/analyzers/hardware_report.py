"""
硬件配置报告模块 — 对齐 WebTools HardwareReport.js

增强功能（相比旧版 152 行）：
- 固件版本信息（VER 消息）
- 板类型解析（DEV ID 解码）
- 电池报告（BAT 消息统计）
- 传感器 ID 解码
- 日志完整性检查（MSG 消息扫描）
- 陷波滤波器完整配置（含 HNTC2）
- 调度器 / 循环率
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

# ---------------------------------------------------------------------------
# 传感器 DEV ID 解码（简化版，参考 WebTools DecodeDevID.js）
# ---------------------------------------------------------------------------

_IMU_TYPES = {
    1: "LSM9DS0",
    2: "LSM9DS1",
    3: "MPU6000",
    4: "ICM20689",
    5: "BMI055",
    6: "BMI088",
    7: "ICM20649",
    8: "ICM20948",
    9: "ICM20602",
    10: "ICM42688",
    11: "ICM42605",
    12: "ICM40609",
    13: "BMI270",
    14: "ICM45686",
    15: "LSM6DSL",
    16: "LSM6DSOQ1",
    21: "MPU9250",
    24: "ICM20789",
    25: "ICM20689",
}

_COMPASS_TYPES = {
    1: "HMC5843",
    2: "LSM303D",
    3: "AK8963",
    4: "BMM150",
    5: "LSM9DS1",
    6: "LIS3MDL",
    7: "AK09916",
    8: "IST8310",
    9: "ICM20948",
    10: "MMC3416",
    11: "QMC5883L",
    12: "MAG3110",
    13: "IST8308",
    14: "RM3100",
    15: "QMC5883",
    16: "AK09918",
    17: "MMC5603",
    18: "MMC5983",
}

_BUS_TYPES = {0: "UNKNOWN", 1: "I2C", 2: "SPI", 3: "UAVCAN", 4: "SITL", 5: "MSP", 6: "SERIAL"}

# ---------------------------------------------------------------------------
# 板类型映射（从 ArduPilot board_types.txt 提取常见板）
# ---------------------------------------------------------------------------

_BOARD_TYPES = {
    5: "PX4 FMU V1",
    9: "PX4 FMU V2/V3/CubeBlack",
    11: "PX4 FMU V4",
    13: "PX4 FMU V4 Pro",
    20: "Uvify Core / F4BY",
    28: "FMUK66 V3",
    29: "AV V1",
    32: "SmartAP Pro",
    33: "AUAV X2V1",
    42: "OmnibusF4SD",
    50: "PX4 FMU V5 / Pixhawk4",
    51: "PX4 FMU V5X",
    52: "PX4 FMU V6",
    53: "PX4 FMU V6X",
    55: "SmartAP AirLink",
    57: "ARK FMU V6X",
    64: "TAP V1",
    65: "AeroFC V1",
    78: "Holybro Pix32 V5",
    88: "MindPX V2",
    120: "CubeYellow",
    121: "OmnibusF7V2",
    122: "KakuteF4",
    123: "KakuteF7",
    124: "Revolution",
    125: "MatekF405",
    127: "MatekF405-Wing",
    130: "SparkyV2",
    131: "OmnibusF4Pro",
    134: "SpeedyBeeF4",
    139: "Durandal",
    140: "CubeOrange",
    143: "MatekF765-Wing",
    146: "H757I-Eval",
    1009: "CUAV Nora",
    1010: "CUAV X7 Pro",
    1013: "MatekH743",
    1036: "QioTek ZealotH743",
    1048: "KakuteH7",
    1058: "KakuteH7Mini",
    1063: "CubeOrange+",
    1082: "SpeedyBeeF4V3",
    1105: "KakuteH7-Wing",
    1106: "SpeedyBeeF405WING",
    1117: "BlitzF7",
    1136: "T-MotorH7",
}


def get_board_name(board_id: int) -> str:
    """根据 APJ_BOARD_ID 返回板名称。"""
    return _BOARD_TYPES.get(board_id, f"Unknown Board ({board_id})")


def decode_devid(devid: int, is_compass: bool = False) -> Dict[str, Any]:
    """解码 ArduPilot 设备 ID 为可读信息。"""
    if devid == 0:
        return {"name": "None", "bus_type": "N/A", "bus": 0, "address": 0, "devtype": 0}

    bus_type = (devid >> 0) & 0x07
    bus = (devid >> 3) & 0x1F
    address = (devid >> 8) & 0xFF
    devtype = (devid >> 16) & 0xFF

    type_map = _COMPASS_TYPES if is_compass else _IMU_TYPES
    name = type_map.get(devtype, f"Unknown({devtype})")
    bus_name = _BUS_TYPES.get(bus_type, f"Unknown({bus_type})")

    return {
        "name": name,
        "bus_type": bus_name,
        "bus": bus,
        "address": address,
        "devtype": devtype,
        "raw_id": devid,
    }


# ---------------------------------------------------------------------------
# IMU 配置
# ---------------------------------------------------------------------------


def get_ins_config(params: Dict[str, float], imu_index: int = 0) -> Dict[str, Any]:
    """获取指定 IMU 的配置信息。"""
    if imu_index == 0:
        gyr_prefix, acc_prefix = "INS_GYR", "INS_ACC"
        gyr_id_key, acc_id_key = "INS_GYR_ID", "INS_ACC_ID"
    elif imu_index == 1:
        gyr_prefix, acc_prefix = "INS_GYR2", "INS_ACC2"
        gyr_id_key, acc_id_key = "INS_GYR2_ID", "INS_ACC2_ID"
    elif imu_index == 2:
        gyr_prefix, acc_prefix = "INS_GYR3", "INS_ACC3"
        gyr_id_key, acc_id_key = "INS_GYR3_ID", "INS_ACC3_ID"
    else:
        pfx = f"INS{imu_index + 1}"
        gyr_prefix, acc_prefix = f"{pfx}_GYR", f"{pfx}_ACC"
        gyr_id_key, acc_id_key = f"{pfx}_GYR_ID", f"{pfx}_ACC_ID"

    gyro_offset = [params.get(f"{gyr_prefix}OFFS_{a}", 0.0) for a in "XYZ"]
    accel_offset = [params.get(f"{acc_prefix}OFFS_{a}", 0.0) for a in "XYZ"]
    accel_scale = [params.get(f"{acc_prefix}SCAL_{a}", 1.0) for a in "XYZ"]

    gyro_id_val = int(params.get(gyr_id_key, 0))
    accel_id_val = int(params.get(acc_id_key, 0))

    gyro_info = decode_devid(gyro_id_val, is_compass=False)
    accel_info = decode_devid(accel_id_val, is_compass=False)

    return {
        "imu_index": imu_index,
        "gyro_id": gyro_id_val,
        "accel_id": accel_id_val,
        "gyro_info": gyro_info,
        "accel_info": accel_info,
        "gyro_offset": gyro_offset,
        "accel_offset": accel_offset,
        "accel_scale": accel_scale,
        "gyro_calibrated": any(abs(v) > 0.001 for v in gyro_offset),
        "accel_calibrated": any(abs(v) > 0.1 for v in accel_offset),
        "scale_calibrated": any(abs(v - 1.0) > 0.01 for v in accel_scale),
    }


# ---------------------------------------------------------------------------
# 磁力计配置
# ---------------------------------------------------------------------------


def get_compass_config(params: Dict[str, float], compass_index: int = 0) -> Dict[str, Any]:
    """获取磁力计配置。"""
    idx = compass_index + 1
    if idx == 1:
        pfx = "COMPASS_"
    else:
        pfx = f"COMPASS{idx}_" if idx <= 3 else f"COMPASS_{idx}_"

    dev_id = int(params.get(f"COMPASS_DEV_ID{'' if idx==1 else idx}", 0))
    info = decode_devid(dev_id, is_compass=True)

    ofs = [params.get(f"COMPASS_OFS{'_' if idx==1 else str(idx)+'_'}{a}", 0.0) for a in "XYZ"]
    dia = [params.get(f"COMPASS_DIA{'_' if idx==1 else str(idx)+'_'}{a}", 1.0) for a in "XYZ"]
    mot = [params.get(f"COMPASS_MOT{'_' if idx==1 else str(idx)+'_'}{a}", 0.0) for a in "XYZ"]

    return {
        "index": compass_index,
        "dev_id": dev_id,
        "info": info,
        "offsets": ofs,
        "diagonals": dia,
        "motor_comp": mot,
        "use": int(params.get(f"COMPASS_USE{''+'' if idx==1 else str(idx)}", 1)),
        "external": int(params.get(f"COMPASS_EXTERN{''+'' if idx==1 else str(idx)}", 0)),
    }


# ---------------------------------------------------------------------------
# 滤波器配置（含 HNTCH / HNTC2）
# ---------------------------------------------------------------------------


def get_filter_config(params: Dict[str, float]) -> Dict[str, Any]:
    """获取完整滤波器配置。"""
    gyro_filter = params.get("INS_GYRO_FILTER", 0.0)
    accel_filter = params.get("INS_ACCEL_FILTER", 0.0)

    notch_filters = []
    # 旧版兼容字段
    notch_enable = False
    notch_freq = 0.0
    notch_bw = 0.0
    notch_att = 0.0

    for i, prefix in enumerate(["INS_HNTCH_", "INS_HNTC2_"]):
        enable = params.get(f"{prefix}ENABLE", 0.0) > 0
        nf = {
            "prefix": prefix.rstrip("_"),
            "enable": enable,
            "freq": params.get(f"{prefix}FREQ", 0.0),
            "bw": params.get(f"{prefix}BW", 0.0),
            "att": params.get(f"{prefix}ATT", 0.0),
            "mode": int(params.get(f"{prefix}MODE", 0)),
            "harmonics": int(params.get(f"{prefix}HMNCS", 0)),
            "options": int(params.get(f"{prefix}OPTS", 0)),
            "ref": params.get(f"{prefix}REF", 0.0),
            "min_ratio": params.get(f"{prefix}FM_RAT", 1.0),
        }
        notch_filters.append(nf)
        # 第一个启用的 notch 填充旧版兼容字段
        if enable and not notch_enable:
            notch_enable = True
            notch_freq = nf["freq"]
            notch_bw = nf["bw"]
            notch_att = nf["att"]

    return {
        "gyro_filter": gyro_filter,
        "accel_filter": accel_filter,
        "notch_filters": notch_filters,
        # 旧版兼容字段
        "notch_enable": notch_enable,
        "notch_freq": notch_freq,
        "notch_bw": notch_bw,
        "notch_att": notch_att,
    }


# ---------------------------------------------------------------------------
# 电池报告
# ---------------------------------------------------------------------------


def get_battery_report(parser_or_flight_data: Any) -> List[Dict[str, Any]]:
    """从 LogParser 或 FlightData 生成电池统计报告。"""
    reports = []

    # Support old LogParser interface
    if hasattr(parser_or_flight_data, "get_battery_data"):
        parser = parser_or_flight_data
        for bat_id in range(2):
            data = parser.get_battery_data(bat_id)
            if data["Volt"].size == 0:
                continue
            report = {
                "id": bat_id,
                "voltage_min": float(np.min(data["Volt"])) if data["Volt"].size else 0,
                "voltage_max": float(np.max(data["Volt"])) if data["Volt"].size else 0,
                "voltage_mean": float(np.mean(data["Volt"])) if data["Volt"].size else 0,
                "current_max": float(np.max(data["Curr"])) if data["Curr"].size else 0,
                "current_mean": float(np.mean(data["Curr"])) if data["Curr"].size else 0,
                "samples": int(data["Volt"].size),
            }
            if data["CurrTot"].size > 0:
                report["consumed_mah"] = float(data["CurrTot"][-1])
            reports.append(report)
        return reports

    # Support new FlightData interface
    fd = parser_or_flight_data
    if (
        hasattr(fd, "battery_voltage")
        and fd.battery_voltage is not None
        and len(fd.battery_voltage) > 0
    ):
        v = fd.battery_voltage
        report = {
            "id": 0,
            "voltage_min": float(np.min(v)),
            "voltage_max": float(np.max(v)),
            "voltage_mean": float(np.mean(v)),
            "current_max": 0.0,
            "current_mean": 0.0,
            "consumed_mah": 0,
        }
        if (
            hasattr(fd, "battery_current")
            and fd.battery_current is not None
            and len(fd.battery_current) > 0
        ):
            c = fd.battery_current
            report["current_max"] = float(np.max(c))
            report["current_mean"] = float(np.mean(c))
        reports.append(report)

    return reports


# ---------------------------------------------------------------------------
# 日志完整性检查
# ---------------------------------------------------------------------------


def check_log_integrity(parser_or_flight_data: Any) -> List[str]:
    """扫描日志数据完整性。支持 LogParser 和 FlightData。"""
    issues = []

    # Support old LogParser interface
    if hasattr(parser_or_flight_data, "get_time_range"):
        parser = parser_or_flight_data
        t_min, t_max = parser.get_time_range()
        duration = t_max - t_min
        if duration < 5.0:
            issues.append(f"Log duration too short: {duration:.1f}s (recommend > 30s)")

        att = parser.get_attitude_data()
        if att["time"].size < 100:
            issues.append(f"Attitude data sparse: {att['time'].size} samples (recommend > 500)")

        imu = parser.get_imu_data(0)
        if imu["time"].size < 100:
            issues.append(f"IMU data sparse: {imu['time'].size} samples")

        if hasattr(parser, "get_messages"):
            for m in parser.get_messages():
                txt = m.get("text", "").lower()
                if any(kw in txt for kw in ["error", "fail", "crash", "watchdog", "reset"]):
                    issues.append(f"MSG alert: {m['text'][:80]}")
        return issues

    # Support new FlightData interface
    fd = parser_or_flight_data
    if hasattr(fd, "duration_s"):
        if fd.duration_s < 5.0:
            issues.append(f"Log duration too short: {fd.duration_s:.1f}s (recommend > 30s)")

    if hasattr(fd, "validate"):
        issues.extend(fd.validate())

    return issues


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def generate_hardware_report(
    params: Dict[str, float],
    flight_data: Any = None,
) -> Dict[str, Any]:
    """
    生成完整硬件配置报告。

    Parameters
    ----------
    params : Dict[str, float]
        参数字典。
    flight_data : FlightData, optional
        统一飞行数据（提供电池/版本等附加信息）。

    Returns
    -------
    Dict[str, Any]
    """
    # IMU 配置
    imu_configs = []
    for i in range(5):
        config = get_ins_config(params, i)
        if config["gyro_id"] > 0 or config["accel_id"] > 0:
            imu_configs.append(config)

    # 磁力计配置
    compass_configs = []
    for i in range(3):
        cc = get_compass_config(params, i)
        if cc["dev_id"] > 0:
            compass_configs.append(cc)

    # 滤波器配置
    filter_config = get_filter_config(params)

    # PID 参数
    pid_params = {}
    for axis in ["RLL", "PIT", "YAW"]:
        prefix = f"ATC_RAT_{axis}_"
        pid_params[axis.lower()] = {
            "P": params.get(f"{prefix}P", 0.0),
            "I": params.get(f"{prefix}I", 0.0),
            "D": params.get(f"{prefix}D", 0.0),
            "FF": params.get(f"{prefix}FF", 0.0),
            "FLTT": params.get(f"{prefix}FLTT", 0.0),
            "FLTD": params.get(f"{prefix}FLTD", 0.0),
            "FLTE": params.get(f"{prefix}FLTE", 0.0),
            "SMAX": params.get(f"{prefix}SMAX", 0.0),
        }

    # 系统信息
    board_id = int(params.get("BRD_TYPE", params.get("BOARD_TYPE", 0)))
    sys_info = {
        "sysid": int(params.get("SYSID_THISMAV", 0)),
        "format_version": int(params.get("FORMAT_VERSION", 0)),
        "frame_class": int(params.get("FRAME_CLASS", 0)),
        "frame_type": int(params.get("FRAME_TYPE", 0)),
        "sched_loop_rate": int(params.get("SCHED_LOOP_RATE", 400)),
        "ahrs_ekf_type": int(params.get("AHRS_EKF_TYPE", 3)),
        "board_id": board_id,
        "board_name": get_board_name(board_id) if board_id > 0 else "Unknown",
    }

    # 固件版本
    version_info = {}
    if flight_data:
        version_info = {"firmware": getattr(flight_data, "firmware_version", "")}

    # 电池报告
    battery_reports = []
    if flight_data and getattr(flight_data, "has_battery", False):
        battery_reports = _battery_report_from_flight_data(flight_data)

    # 日志完整性
    integrity_issues = []
    if flight_data:
        issues = flight_data.validate() if hasattr(flight_data, "validate") else []
        integrity_issues = issues

    return {
        "imu_configs": imu_configs,
        "compass_configs": compass_configs,
        "filter_config": filter_config,
        "pid_params": pid_params,
        "sys_info": sys_info,
        "version_info": version_info,
        "battery_reports": battery_reports,
        "integrity_issues": integrity_issues,
        "total_params": len(params),
    }


def _battery_report_from_flight_data(flight_data) -> List[Dict[str, Any]]:
    """Extract battery stats from FlightData."""
    reports = []
    if flight_data.battery_voltage is not None and len(flight_data.battery_voltage) > 0:
        v = flight_data.battery_voltage
        report = {
            "id": 0,
            "voltage_min": float(np.min(v)),
            "voltage_max": float(np.max(v)),
            "voltage_mean": float(np.mean(v)),
            "current_max": 0.0,
            "current_mean": 0.0,
            "consumed_mah": 0,
        }
        if flight_data.battery_current is not None and len(flight_data.battery_current) > 0:
            c = flight_data.battery_current
            report["current_max"] = float(np.max(c))
            report["current_mean"] = float(np.mean(c))
        reports.append(report)
    return reports
