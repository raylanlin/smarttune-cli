#!/usr/bin/env python3
"""
tools/build_param_tables.py

Generate SmartTune's firmware parameter tables from upstream metadata.

This is the scraper that used to live outside the repository — and whose absence
let a broken table ship: prefix-stripped names (``MONITOR`` instead of
``BATT_MONITOR``), descriptions shifted one row out of alignment, fabricated
``default: 0.0`` everywhere, and no ``@Values`` metadata at all. Everything it
writes is traceable to a named upstream file recorded in the output's ``source``
block, and ``stune params --lint`` re-checks the result.

Sources
-------
ArduPilot   ``apm.pdef.json`` from ArduPilot's generated parameter metadata
            (e.g. https://github.com/raylanlin/ParameterRepository →
            ``Copter-4.1/apm.pdef.json``). Top-level keys are parameter groups
            (``ATC_``, ``INS_HNTCH_``, plus one vehicle group); each parameter
            carries Description / DisplayName / Range / Values / Bitmask /
            Units / Increment / User / RebootRequired / ReadOnly.

PX4         ``parameters.json`` produced by PX4's own ``px4params`` generator
            (in PX4-Autopilot: ``docs/public/config/failsafe/parameters.json``).
            Carries real defaults, ranges, units, values and bitmasks.

Betaflight  parsed from firmware source, because Betaflight publishes no
            parameter metadata artifact:
              src/main/cli/settings.c        valueTable + lookup tables
              src/main/fc/parameter_names.h  PARAM_NAME_* → name strings
              src/main/cli/settings.h        lookupTableIndex_e enum
            plus headers that define numeric bounds (pid.h, rx.h, osd.h, ...).
            Bounds behind unresolvable macros are written as ``null`` rather
            than guessed; enum member lists defined outside settings.c are
            recorded in ``unresolved_ref`` so validate() reports them as
            unverifiable instead of accepting anything.

Usage
-----
    python tools/build_param_tables.py ardupilot  ../ParameterRepository/Copter-4.1/apm.pdef.json
    python tools/build_param_tables.py px4  ../PX4-Autopilot/.../failsafe/parameters.json
    python tools/build_param_tables.py betaflight ../betaflight
    python tools/build_param_tables.py --check      # lint the current tables

Add ``--out PATH`` to write elsewhere, ``--stdout`` to print instead of writing.
Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = REPO_ROOT / "smarttune" / "knowledge" / "params"
SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _today() -> str:
    return _dt.date.today().isoformat()


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return int(f) if f.is_integer() and abs(f) < 2**53 else f


def _slug(text: str) -> str:
    return re.sub(r"_+$|^_+", "", re.sub(r"[^a-z0-9]+", "_", str(text).lower()))


def _first_rule(name: str, rules: list[tuple[str, str]]) -> str | None:
    for pattern, category in rules:
        if re.search(pattern, name):
            return category
    return None


def _envelope(
    platform: str,
    source: dict[str, Any],
    notes: list[str],
    params: list[dict[str, Any]],
    groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    params = sorted(params, key=lambda p: p["name"])
    if groups is None:
        counts: dict[str, int] = {}
        for p in params:
            counts[p.get("group", "")] = counts.get(p.get("group", ""), 0) + 1
        groups = [{"name": g, "count": n} for g, n in sorted(counts.items())]
    return {
        "platform": platform,
        "schema_version": SCHEMA_VERSION,
        "source": {**source, "generated": _today(), "generator": "tools/build_param_tables.py"},
        "notes": notes,
        "group_count": len(groups),
        "parameter_count": len(params),
        "categories": sorted({p["category"] for p in params}),
        "groups": groups,
        "parameters": params,
    }


# ---------------------------------------------------------------------------
# ArduPilot
# ---------------------------------------------------------------------------

_AP_RULES: list[tuple[str, str]] = [
    (r"^ATC_RAT_(RLL|PIT|YAW)_", "pid"),
    (r"^ATC_ANG_(RLL|PIT|YAW)_P", "pid"),
    (r"^ATC_(ACCEL|RATE)_", "rate"),
    (r"^ATC_", "attitude"),
    (r"^PSC", "position"),
    (r"^INS_(HNTCH|HNTC2|NOTCH|GYRO_FILTER|ACCEL_FILTER|LOG_BAT|FAST_SAMPLE|GYRO_RATE)", "filter"),
    (r"^INS_", "imu"),
    (r"^COMPASS_", "mag"),
    (r"^BATT\d*_", "battery"),
    (r"^MOT_", "motor"),
    (r"^(SERVO|RC\d|RC_|RCMAP_|FLTMODE|SIMPLE|SUPER_SIMPLE)", "rc"),
    (r"^GPS", "gps"),
    (r"^LOG_", "logging"),
    (r"^EK[23]_", "ekf"),
    (r"^(FS_|ARMING_|FENCE_|AVOID_|AFS_|BRD_SAFETY)", "safety"),
    (r"^(SERIAL|CAN_|SCR_|BRD_|NET_|MAV|SR\d)", "system"),
    (r"^(H_|AROT_)", "heli"),
    (r"^OSD", "osd"),
    (r"^AUTOTUNE_", "autotune"),
    (r"^BARO", "baro"),
    (r"^(WPNAV_|LOIT_|RTL_|LAND_|CIRCLE_|AUTO_)", "navigation"),
]


def _ap_category(name: str, group: str) -> str:
    hit = _first_rule(name, _AP_RULES)
    if hit:
        return hit
    g = group.rstrip("_").lower()
    vehicle_groups = ("copter", "plane", "rover", "sub", "tracker", "blimp")
    return "vehicle" if (not g or g in vehicle_groups) else g


def build_ardupilot(pdef_path: Path) -> dict[str, Any]:
    raw = json.loads(pdef_path.read_text(encoding="utf-8"))
    params: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    seen: dict[str, str] = {}

    for group in sorted(k for k in raw if k != "json"):
        bag = raw[group]
        if not isinstance(bag, dict):
            continue
        kept = 0
        for name in sorted(bag):
            meta = bag[name]
            if not isinstance(meta, dict) or name in seen:
                continue
            seen[name] = group

            values = meta.get("Values") if isinstance(meta.get("Values"), dict) else None
            bitmask = meta.get("Bitmask") if isinstance(meta.get("Bitmask"), dict) else None
            rng = meta.get("Range") if isinstance(meta.get("Range"), dict) else None

            entry: dict[str, Any] = {
                "name": name,
                "group": group,
                "category": _ap_category(name, group),
                "display_name": meta.get("DisplayName") or name,
                "type": "enum" if values else ("bitmask" if bitmask else "float"),
                # ArduPilot metadata publishes no defaults — null means unknown
                "default": None,
                "min": _num(rng.get("low")) if rng else None,
                "max": _num(rng.get("high")) if rng else None,
                "increment": _num(meta.get("Increment")),
                "unit": meta.get("Units") or "",
                "user": meta.get("User") or "",
                "reboot_required": str(meta.get("RebootRequired", "")).lower() == "true",
                "read_only": str(meta.get("ReadOnly", "")).lower() == "true",
                "description": (meta.get("Description") or meta.get("DisplayName") or "").strip(),
            }
            if values:
                entry["values"] = {str(k).strip(): str(v).strip() for k, v in values.items()}
            if bitmask:
                entry["bitmask"] = {str(k).strip(): str(v).strip() for k, v in bitmask.items()}
            params.append(entry)
            kept += 1
        if kept:
            groups.append({"name": group, "count": kept})

    vehicle = next(
        (
            k
            for k in raw
            if k in ("Copter", "Plane", "Rover", "Sub", "Tracker", "Blimp", "AP_Periph")
        ),
        "",
    )
    return _envelope(
        "ArduPilot",
        {
            "upstream": "ArduPilot generated parameter metadata (apm.pdef.json)",
            "path": str(pdef_path),
            "vehicle": vehicle,
            "firmware": pdef_path.parent.name,
        },
        [
            "Names are full firmware parameter names — never prefix-stripped "
            "(BATT_MONITOR, not MONITOR).",
            "default is null: ArduPilot parameter metadata does not publish defaults "
            "(they are vehicle/board specific). null means unknown, not zero.",
            "type: 'enum' and 'bitmask' come from upstream @Values / @Bitmask and are "
            "authoritative. 'float' means numeric whose exact storage type is not stated.",
            "description is the upstream @Description for this exact parameter — "
            "no offset, no @PREFIX@ placeholders.",
        ],
        params,
        groups,
    )


# ---------------------------------------------------------------------------
# PX4
# ---------------------------------------------------------------------------

_PX4_RULES: list[tuple[str, str]] = [
    (r"^MC_(ROLL|PITCH|YAW)RATE_", "pid"),
    (r"^MC_", "attitude"),
    (r"^IMU_(GYRO|DGYRO|ACCEL)_(CUTOFF|NF|DNF)", "filter"),
    (r"^IMU_", "imu"),
    (r"^EKF2_", "ekf"),
    (r"^BAT\d*_", "battery"),
    (r"^(CAL_MAG|SENS_MAG|MAG_)", "mag"),
    (r"^MPC_", "position"),
    (r"^FW_", "fixedwing"),
    (r"^(COM_|FD_|GF_|NAV_FORCE)", "safety"),
    (r"^(SDLOG_|LOG_)", "logging"),
    (r"^(GPS_|SENS_GPS)", "gps"),
    (r"^(PWM_|MOT_|CA_)", "motor"),
    (r"^RC(\d+|_)", "rc"),
]


def build_px4(json_path: Path) -> dict[str, Any]:
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    params: list[dict[str, Any]] = []

    for meta in raw.get("parameters", []):
        name = meta.get("name")
        if not name:
            continue
        group = meta.get("group") or ""
        values = meta.get("values") if isinstance(meta.get("values"), list) else None
        bitmask = meta.get("bitmask") if isinstance(meta.get("bitmask"), list) else None

        entry: dict[str, Any] = {
            "name": name,
            "group": group,
            "category": _first_rule(name, _PX4_RULES) or _slug(group) or "misc",
            "display_name": (meta.get("shortDesc") or name).strip(),
            "type": (
                "enum"
                if values
                else ("bitmask" if bitmask else ("float" if meta.get("type") == "Float" else "int"))
            ),
            "default": meta.get("default"),
            "min": _num(meta.get("min")),
            "max": _num(meta.get("max")),
            "increment": _num(meta.get("increment")),
            "unit": meta.get("units") or "",
            # PX4's own "category" is an audience level, not a topic
            "user": meta.get("category") or "",
            "reboot_required": bool(meta.get("rebootRequired")),
            "read_only": False,
            "description": str(meta.get("longDesc") or meta.get("shortDesc") or "").strip(),
        }
        if values:
            entry["values"] = {
                str(v.get("value")): str(v.get("description", "")).strip() for v in values
            }
        if bitmask:
            entry["bitmask"] = {
                str(b.get("index")): str(b.get("description", "")).strip() for b in bitmask
            }
        params.append(entry)

    return _envelope(
        "PX4",
        {
            "upstream": "PX4 generated parameter metadata (px4params jsonout.py)",
            "path": str(json_path),
            "metadata_version": raw.get("version"),
        },
        [
            "Defaults, ranges, units and increments are upstream values from PX4 "
            "firmware metadata.",
            "user carries PX4's own parameter audience (Standard / Developer / System).",
            "type: 'enum'/'bitmask' derived from upstream values/bitmask lists; otherwise "
            "the firmware storage type (float/int).",
        ],
        params,
    )


# ---------------------------------------------------------------------------
# Betaflight
# ---------------------------------------------------------------------------

_BF_RULES: list[tuple[str, str]] = [
    (r"^(p|i|d|f)_(roll|pitch|yaw)$", "pid"),
    (r"^d_(max|min)_", "pid"),
    (
        r"^(anti_gravity|iterm_|feedforward_|tpa_|abs_control|d_max|thrust_linear|pidsum|"
        r"pid_at_min_throttle|simplified_(pid|d))",
        "pid",
    ),
    (
        r"^(gyro_lpf|gyro_notch|gyro_hardware_lpf|dterm_lpf|dterm_notch|dyn_notch|rpm_filter|"
        r"acc_lpf|yaw_lowpass|simplified_(gyro|dterm)_filter)",
        "filter",
    ),
    (r"^(roll|pitch|yaw)_(rc_rate|srate|expo)$", "rate"),
    (r"^(rates_type|thr_mid|thr_expo|throttle_limit)", "rate"),
    (r"^(vbat_|ibata|current_meter|battery_|voltage_meter|amperage_meter|bat_)", "battery"),
    (r"^(mag_|align_mag|compass)", "mag"),
    (
        r"^(acc_|align_acc|gyro_[1-8]_|gyro_offset|gyro_enable|gyro_calib|gyro_to_use|"
        r"gyro_overflow|gyro_hardware)",
        "imu",
    ),
    (r"^(motor_|mixer_|dshot_|min_throttle|max_throttle|idle_)", "motor"),
    (
        r"^(rc_|rx_|serialrx|rssi|min_check|max_check|deadband|yaw_deadband|srxl2|spektrum|"
        r"sbus|crsf|fport|msp_override|switch_arming|throttle_correction)",
        "rc",
    ),
    (r"^(osd|displayport|max7456|cms)", "osd"),
    (r"^(blackbox|debug)", "logging"),
    (r"^(gps|imu_|nav_|pos_hold|alt_hold)", "navigation"),
    (r"^(failsafe|arming|small_angle|runaway|crash_recovery|gyro_cal_on_first_arm)", "safety"),
    (
        r"^(vtx|led|beeper|buzzer|telemetry|frsky|smartport|ibus|jetiexbus|mavlink|hott|ltm)",
        "peripheral",
    ),
]

_BF_PG_CATEGORY = {
    "PG_PID_PROFILE": "pid",
    "PG_GYRO_CONFIG": "imu",
    "PG_ACCELEROMETER_CONFIG": "imu",
    "PG_COMPASS_CONFIG": "mag",
    "PG_OSD_CONFIG": "osd",
    "PG_BLACKBOX_CONFIG": "logging",
    "PG_MOTOR_CONFIG": "motor",
    "PG_RX_CONFIG": "rc",
    "PG_VOLTAGE_SENSOR_ADC_CONFIG": "battery",
}

#: lookup arrays whose C identifier does not follow TABLE_FOO_BAR → lookupTableFooBar
_BF_TABLE_ALIASES = {
    "TABLE_GYRO_LPF_TYPE": "lookupTableLowpassType",
    "TABLE_DTERM_LPF_TYPE": "lookupTableDtermLowpassType",
    "TABLE_MOTOR_PWM_PROTOCOL": "lookupTablePwmProtocol",
    "TABLE_GPS_RESCUE_SANITY_CHECK": "lookupTableRescueSanityType",
    "TABLE_GPS_RESCUE_ALT_MODE": "lookupTableRescueAltitudeMode",
    "TABLE_RX_FRSKY_SPI_A1_SOURCE": "lookupTableFrskySpiA1Source",
    "TABLE_POSITION_ALT_SOURCE": "lookupTablePositionAltitudeSource",
    "TABLE_LED_PROFILE": "lookupTableLEDProfile",
    "TABLE_POSHOLD_SOURCE": "lookupTablePosHoldSource",
    "TABLE_CMS_BACKGROUND": "lookupTableCMSMenuBackgroundType",
    "TABLE_SERIAL_RX": "lookupTableSerialRX",
    "TABLE_RATES_TYPE": "lookupTableRatesType",
    "TABLE_LEDSTRIP_COLOR": "lookupTableLedstripColors",
}

#: headers scanned for numeric bound macros used inside valueTable
_BF_BOUND_HEADERS = [
    "src/main/flight/pid.h",
    "src/main/rx/rx.h",
    "src/main/osd/osd.h",
    "src/main/fc/controlrate_profile.h",
    "src/main/sensors/battery.h",
    "src/main/sensors/gyro.h",
    "src/main/config/simplified_tuning.h",
    "src/main/drivers/bus_i2c.h",
    "src/main/common/filter.h",
]

_C_LIMITS = {
    "UINT8_MAX": 255,
    "INT8_MIN": -128,
    "INT8_MAX": 127,
    "UINT16_MAX": 65535,
    "INT16_MIN": -32768,
    "INT16_MAX": 32767,
    "UINT32_MAX": 4294967295,
    "XYZ_AXIS_COUNT": 3,
}


def _bf_camel(table_id: str) -> str:
    parts = table_id.replace("TABLE_", "", 1).lower().split("_")
    return "lookupTable" + "".join(p.capitalize() for p in parts)


def build_betaflight(bf_root: Path, curated: dict[str, str] | None = None) -> dict[str, Any]:
    settings_c = (bf_root / "src/main/cli/settings.c").read_text(encoding="utf-8", errors="replace")
    settings_h = (bf_root / "src/main/cli/settings.h").read_text(encoding="utf-8", errors="replace")
    names_h = (bf_root / "src/main/fc/parameter_names.h").read_text(
        encoding="utf-8", errors="replace"
    )

    header_text = ""
    for rel in _BF_BOUND_HEADERS:
        path = bf_root / rel
        if path.is_file():
            header_text += "\n" + path.read_text(encoding="utf-8", errors="replace")

    # numeric #defines (two passes: literals, then one level of indirection)
    defines: dict[str, int] = dict(_C_LIMITS)
    literal_re = re.compile(
        r"^\s*#define\s+([A-Z][A-Z0-9_]*)\s+\(?\s*(-?\d+)\s*\)?\s*(?://.*)?$", re.M
    )
    alias_re = re.compile(
        r"^\s*#define\s+([A-Z][A-Z0-9_]*)\s+\(?\s*([A-Z][A-Z0-9_]*)\s*\)?\s*$", re.M
    )
    for text in (header_text, settings_c, settings_h):
        for m in literal_re.finditer(text):
            defines.setdefault(m.group(1), int(m.group(2)))
    for text in (header_text, settings_c, settings_h):
        for m in alias_re.finditer(text):
            if m.group(1) not in defines and m.group(2) in defines:
                defines[m.group(1)] = defines[m.group(2)]

    def resolve(token: Any) -> int | None:
        if token is None:
            return None
        t = str(token).strip().strip("()").strip()
        if re.fullmatch(r"-?\d+", t):
            return int(t)
        if re.fullmatch(r"0[xX][0-9a-fA-F]+", t):
            return int(t, 16)
        return defines.get(t)

    # PARAM_NAME_* → literal
    param_names = dict(re.findall(r'#define\s+(PARAM_NAME_\w+)\s+"([^"]+)"', names_h))

    # lookup arrays
    tables: dict[str, list[str]] = {}
    for m in re.finditer(
        r"(?:static\s+)?const\s+char\s*\*\s*const\s+(\w+)\s*\[\s*\]\s*=\s*\{(.*?)\}\s*;",
        settings_c,
        re.S,
    ):
        tables[m.group(1)] = re.findall(r'"([^"]*)"', m.group(2))

    enum_block = re.search(r"typedef enum \{(.*?)\} lookupTableIndex_e;", settings_h, re.S)
    table_map: dict[str, str] = {}
    for m in re.finditer(r"^\s*(TABLE_\w+)", enum_block.group(1) if enum_block else "", re.M):
        table_id = m.group(1)
        candidate = _BF_TABLE_ALIASES.get(table_id, _bf_camel(table_id))
        if candidate in tables:
            table_map[table_id] = candidate
            continue
        loose = next(
            (k for k in tables if k.lower().rstrip("s") == candidate.lower().rstrip("s")), None
        )
        if loose:
            table_map[table_id] = loose

    value_table = re.search(r"const clivalue_t valueTable\[\]\s*=\s*\{(.*?)\n\};", settings_c, re.S)
    if not value_table:
        raise SystemExit("could not locate valueTable[] in settings.c")

    entry_re = re.compile(r'^\s*\{\s*(PARAM_NAME_\w+|"[^"]*")\s*,\s*([A-Z0-9_ |]+?)\s*,')
    params: list[dict[str, Any]] = []
    unresolved_bounds = 0

    for line in value_table.group(1).splitlines():
        m = entry_re.match(line)
        if not m:
            continue
        token = m.group(1)
        name = token[1:-1] if token.startswith('"') else param_names.get(token)
        if not name:
            continue
        flags = m.group(2)
        pg_match = re.search(r"\bPG_[A-Z0-9_]+", line)
        pg = pg_match.group(0) if pg_match else ""
        var_match = re.search(r"VAR_(U?INT\d+)", flags)

        minimum = maximum = None
        values = bitmask = None
        unresolved_ref = ""

        mm = re.search(r"\.config\.minmaxUnsigned\s*=\s*\{([^}]*)\}", line) or re.search(
            r"\.config\.minmax\s*=\s*\{([^}]*)\}", line
        )
        if mm:
            body = mm.group(1)
            split = body.rfind(",")
            minimum, maximum = resolve(body[:split]), resolve(body[split + 1 :])
            if minimum is None or maximum is None:
                unresolved_bounds += 1

        u32 = re.search(r"\.config\.(?:u32Max|d32Max)\s*=\s*([^,]+),", line)
        if u32:
            minimum, maximum = 0, resolve(u32.group(1))
            if maximum is None:
                unresolved_bounds += 1

        lookup = re.search(r"\.config\.lookup\s*=\s*\{\s*(TABLE_\w+)\s*\}", line)
        if lookup:
            array = tables.get(table_map.get(lookup.group(1), ""))
            if array:
                values = {str(i): label for i, label in enumerate(array)}
            else:
                unresolved_ref = (
                    f"{lookup.group(1)} (lookup table defined outside " f"cli/settings.c)"
                )

        bitpos = re.search(r"\.config\.bitpos\s*=\s*([^,]+),", line)
        if bitpos:
            bit = resolve(bitpos.group(1))
            if bit is not None:
                bitmask = {str(bit): "set"}
            minimum, maximum = 0, 1

        array_len = re.search(r"\.config\.array\.length\s*=\s*([^,]+),", line)

        if values or lookup:
            ptype = "enum"
        elif array_len:
            ptype = "array"
        elif bitpos:
            ptype = "bitmask"
        elif var_match:
            ptype = "int"
        else:
            ptype = "int"

        entry: dict[str, Any] = {
            "name": name,
            "group": pg[3:] if pg.startswith("PG_") else pg,
            "category": (
                _first_rule(name, _BF_RULES)
                or _BF_PG_CATEGORY.get(pg)
                or _slug(pg.replace("PG_", "")).replace("_config", "")
                or "misc"
            ),
            "display_name": name.replace("_", " "),
            "type": ptype,
            # settings.c has no defaults — they live in each PG's pgResetTemplate
            "default": None,
            "min": minimum,
            "max": maximum,
            "increment": None,
            "unit": (
                "Hz"
                if re.search(r"_hz$|_hz_|_freq", name)
                else ("ms" if re.search(r"_ms$|_us$", name) else "")
            ),
            "user": "",
            "reboot_required": False,
            "read_only": False,
            "description": (curated or {}).get(name, ""),
        }
        if values:
            entry["values"] = values
        if bitmask:
            entry["bitmask"] = bitmask
        if array_len:
            entry["array_length"] = resolve(array_len.group(1))
        if unresolved_ref:
            entry["unresolved_ref"] = unresolved_ref
        params.append(entry)

    print(
        f"  betaflight: {len(params)} params, {unresolved_bounds} bounds left null "
        f"(macro not resolvable from headers)",
        file=sys.stderr,
    )

    return _envelope(
        "Betaflight",
        {
            "upstream": "Betaflight firmware CLI settings table",
            "path": "src/main/cli/settings.c (+ fc/parameter_names.h, cli/settings.h)",
            "checkout": str(bf_root),
        },
        [
            "Names, types, ranges, groups (PG_*) and enum members are parsed from firmware "
            "source — Betaflight has no separate parameter metadata artifact.",
            "Entries are the union of all build options: a parameter guarded by #ifdef may be "
            "absent from a specific firmware build.",
            "default is null: settings.c carries no defaults (they live in each PG's "
            "pgResetTemplate).",
            "description: Betaflight firmware ships no parameter descriptions; text here is "
            "SmartTune's curated set where available, empty otherwise.",
            "unresolved_ref marks enums whose member list is defined outside cli/settings.c — "
            "validate() reports them as unverifiable rather than accepting any value.",
        ],
        params,
    )


def _existing_descriptions(platform: str) -> dict[str, str]:
    """Carry curated descriptions over from the table already in the repo."""
    path = KNOWLEDGE_DIR / f"{platform}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        p["name"]: p["description"]
        for p in data.get("parameters", [])
        if p.get("name") and p.get("description")
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _check() -> int:
    from smarttune.platform.param_lint import lint_table
    from smarttune.platform.params import ParamTable

    failed = False
    for platform in ParamTable.available_platforms():
        report = lint_table(ParamTable.from_knowledge(platform))
        status = "OK " if report["ok"] else "FAIL"
        print(
            f"{status} {report['platform']:<12} schema v{report['schema_version']} "
            f"{report['parameter_count']:>5} params  "
            f"{report['error_count']} errors  {report['warning_count']} warnings"
        )
        for check, count in sorted(report["by_check"].items(), key=lambda kv: -kv[1]):
            print(f"       {check:<28} {count}")
        failed = failed or not report["ok"]
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("platform", nargs="?", choices=["ardupilot", "px4", "betaflight"])
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        help="apm.pdef.json / parameters.json / betaflight checkout root",
    )
    parser.add_argument("--out", type=Path, default=None, help="output path")
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    parser.add_argument("--check", action="store_true", help="lint the tables already in the repo")
    args = parser.parse_args(argv)

    if args.check:
        return _check()
    if not args.platform or not args.source:
        parser.error("platform and source are required (or pass --check)")
    if not args.source.exists():
        parser.error(f"source not found: {args.source}")

    if args.platform == "ardupilot":
        table = build_ardupilot(args.source)
    elif args.platform == "px4":
        table = build_px4(args.source)
    else:
        table = build_betaflight(args.source, _existing_descriptions("betaflight"))

    text = json.dumps(table, indent=2, ensure_ascii=False) + "\n"
    if args.stdout:
        sys.stdout.write(text)
        return 0

    out = args.out or (KNOWLEDGE_DIR / f"{args.platform}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(
        f"wrote {out} — {table['parameter_count']} parameters, " f"{table['group_count']} groups",
        file=sys.stderr,
    )
    print("now run: python tools/build_param_tables.py --check", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
