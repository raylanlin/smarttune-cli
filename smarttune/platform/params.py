"""
smarttune/platform/params.py

Parameter table — loads from knowledge base JSON files.

Knowledge base JSON files live in smarttune/knowledge/params/<platform>.json.

Schema
------
``schema_version: 1`` (legacy, scraped by the pre-3.2 scraper)
    name/category/type/default/min/max/unit/description only. Known-bad:
    group prefixes stripped from names, descriptions offset by one, no enum
    members, fabricated ``default: 0.0``. See ``param_lint.py``.

``schema_version: 2`` (produced by ``tools/build_param_tables.py``)
    adds ``values`` (enum members), ``bitmask`` (bit meanings), real defaults,
    and full firmware parameter names. Unknown extra keys are tolerated so the
    scraper can add provenance fields without a code change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Any, Dict, List, Optional, Tuple

_KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge" / "params"

#: validate_detail() status values
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_OUT_OF_RANGE = "out_of_range"
STATUS_NOT_A_MEMBER = "not_a_member"
STATUS_NOT_AN_INTEGER = "not_an_integer"
STATUS_UNVERIFIABLE = "unverifiable"


@dataclass
class ParamDef:
    """Single parameter definition loaded from knowledge base."""

    name: str  # platform-specific parameter name, e.g. "ATC_RAT_RLL_P"
    category: str  # "pid" | "filter" | "mag" | "battery" | ...
    type: str  # "float" | "int" | "enum" | "bitmask"
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    unit: str = ""
    description: str = ""
    #: firmware parameter group, e.g. "ATC_" / "INS_HNTCH_" — schema_version >= 2
    group: str = ""
    #: upstream @DisplayName, e.g. "Roll axis rate controller P gain"
    display_name: str = ""
    #: enum members, {"4": "Analog Voltage and Current"} — schema_version >= 2
    values: Dict[str, str] = field(default_factory=dict)
    #: bitmask bit meanings, {"0": "Roll", "1": "Pitch"} — schema_version >= 2
    bitmask: Dict[str, str] = field(default_factory=dict)
    #: upstream @Increment (suggested step)
    increment: Optional[float] = None
    #: upstream @User — "Standard" | "Advanced"
    user: str = ""
    reboot_required: bool = False
    read_only: bool = False
    #: set when a discrete parameter's member list could not be captured from
    #: the firmware source (e.g. a Betaflight lookup table defined outside
    #: cli/settings.c) — validate() then reports 'unverifiable' instead of
    #: silently accepting any value
    unresolved_ref: str = ""

    @classmethod
    def from_dict(cls, item: Dict[str, Any]) -> "ParamDef":
        """Build from a JSON dict, ignoring unknown keys.

        (Was: ``ParamDef(**item)`` — any new key added by the scraper raised
        ``TypeError`` and broke the whole table.)
        """
        known = {f.name for f in dataclass_fields(cls)}
        kwargs = {k: v for k, v in item.items() if k in known}
        for key in ("values", "bitmask"):
            raw = kwargs.get(key)
            if isinstance(raw, dict):
                kwargs[key] = {str(k): str(v) for k, v in raw.items()}
            elif isinstance(raw, list):
                # tolerate [{"value": 4, "label": "..."}] shape
                kwargs[key] = {
                    str(e.get("value")): str(e.get("label", "")) for e in raw if isinstance(e, dict)
                }
            else:
                kwargs[key] = {}
        kwargs.setdefault("unit", "")
        kwargs.setdefault("description", "")
        return cls(**kwargs)

    # ── derived helpers ──────────────────────────────────────

    @property
    def is_discrete(self) -> bool:
        """True for enum/bitmask style parameters."""
        return self.type in ("enum", "bitmask") or bool(self.values) or bool(self.bitmask)

    def summary(self, max_len: int = 100) -> str:
        """First sentence of the description, for slim listings.

        Falls back to the upstream display name when the firmware ships no
        description (Betaflight).
        """
        text = (self.description or self.display_name or "").strip().replace("\n", " ")
        if not text:
            return ""
        head = text.split(". ")[0].rstrip(".")
        return head if len(head) <= max_len else head[: max_len - 1] + "…"

    def range_str(self) -> str:
        if self.min is not None and self.max is not None:
            return f"[{self.min}, {self.max}]"
        if self.min is not None:
            return f">= {self.min}"
        if self.max is not None:
            return f"<= {self.max}"
        return ""

    def options(self) -> Dict[str, str]:
        """Enum members or bitmask bits, whichever this parameter has."""
        return dict(self.values or self.bitmask)


# ---------------------------------------------------------------------------
# Dict shapes — defined ONCE here so CLI and MCP cannot drift apart
# ---------------------------------------------------------------------------


_NUMBERED_GROUP_RE = re.compile(r"^([A-Z][A-Z0-9]*?)([2-9]|1[0-9])(_.+)$")


def collapse_numbered(params: "List[ParamDef]") -> "List[Tuple[ParamDef, List[str]]]":
    """Collapse numbered-instance duplicates for display (search results).

    ArduPilot repeats whole groups per instance — BATT2_MONITOR ... BATT9_MONITOR
    are copies of BATT_MONITOR. A search for "monitor" returns ~40 such clones,
    drowning the handful of genuinely different parameters (and pushing them past
    the limit). This folds each numbered clone into its base parameter and
    returns (param, instances) pairs in the original ranking order, e.g.
    (BATT_MONITOR, ["BATT_", "BATT2_", ..., "BATT9_"]). A numbered parameter
    whose base is absent from the result set is kept as-is.

    Display-level only — validation and get_param always use exact names.
    """
    by_name = {p.name: p for p in params}
    order: List[str] = []
    instances: Dict[str, List[str]] = {}

    def base_of(name: str) -> Optional[str]:
        m = _NUMBERED_GROUP_RE.match(name)
        if not m:
            return None
        candidate = m.group(1) + m.group(3)
        return candidate if candidate in by_name else None

    for p in params:
        base = base_of(p.name)
        key = base if base else p.name
        if key not in instances:
            instances[key] = []
            order.append(key)
        prefix = key.split("_", 1)[0] + "_"
        if base:
            m = _NUMBERED_GROUP_RE.match(p.name)
            instances[key].append(m.group(1) + m.group(2) + "_")
        elif _NUMBERED_GROUP_RE.match(p.name) is None and key == p.name:
            # base itself present
            if prefix not in instances[key]:
                instances[key].insert(0, prefix)

    out: List[Tuple[ParamDef, List[str]]] = []
    for key in order:
        inst = instances[key]
        # only report instances when there actually was folding
        out.append((by_name[key], inst if len(inst) > 1 else []))
    return out


def to_slim_dict(p: ParamDef) -> Dict[str, Any]:
    """Compact row for listings and search hits (a few hundred bytes).

    Long descriptions and full enum tables are deliberately omitted — fetch
    them per-parameter with :func:`to_full_dict` (MCP: ``smarttune_get_param``).
    """
    d: Dict[str, Any] = {"name": p.name, "type": p.type}
    if p.group:
        d["group"] = p.group
    d["category"] = p.category
    rng = p.range_str()
    if rng:
        d["range"] = rng
    if p.unit:
        d["unit"] = p.unit
    if p.values:
        d["enum_count"] = len(p.values)
    if p.bitmask:
        d["bit_count"] = len(p.bitmask)
    summary = p.summary()
    if summary:
        d["summary"] = summary
    return d


def to_full_dict(p: ParamDef) -> Dict[str, Any]:
    """Complete definition — description, enum members, bitmask bits, metadata."""
    d: Dict[str, Any] = {
        "name": p.name,
        "group": p.group,
        "category": p.category,
        "display_name": p.display_name or p.name,
        "type": p.type,
        "default": p.default,
        "min": p.min,
        "max": p.max,
        "unit": p.unit,
        "description": p.description,
    }
    if p.increment is not None:
        d["increment"] = p.increment
    if p.user:
        d["user"] = p.user
    if p.reboot_required:
        d["reboot_required"] = True
    if p.read_only:
        d["read_only"] = True
    if p.values:
        d["values"] = dict(p.values)
    if p.bitmask:
        d["bitmask"] = dict(p.bitmask)
    return d


class ParamTable:
    """Read-only parameter table loaded from knowledge base JSON."""

    def __init__(
        self,
        platform_name: str,
        params: List[ParamDef],
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._platform = platform_name
        self._params = list(params)
        self._meta = dict(meta or {})
        self._by_name: Dict[str, ParamDef] = {}
        for p in self._params:
            self._by_name[p.name.upper()] = p

    # ── factory ──────────────────────────────────────────────

    @staticmethod
    def _normalize_fw(fw: str) -> str:
        return str(fw).strip().lower().replace(" ", "-").replace("_", "-")

    @classmethod
    def from_knowledge(cls, platform_name: str, fw_version: str = "") -> "ParamTable":
        """Load parameter table from knowledge base JSON.

        Default table:   smarttune/knowledge/params/<platform>.json
        Versioned table: smarttune/knowledge/params/<platform>.<fw_version>.json
        (v3.3) — e.g. ``from_knowledge("ardupilot", "copter-4.5")`` loads
        ``ardupilot.copter-4.5.json``. ``fw_version="default"``/"" loads the
        platform default.
        """
        platform_key = platform_name.lower()
        fw = cls._normalize_fw(fw_version) if fw_version else ""
        if fw in ("", "default"):
            path = _KNOWLEDGE_DIR / f"{platform_key}.json"
        else:
            path = _KNOWLEDGE_DIR / f"{platform_key}.{fw}.json"
        if not path.is_file():
            versions = cls.available_versions(platform_key)
            raise FileNotFoundError(
                f"Knowledge base not found for {platform_name}"
                + (f" @ {fw_version}" if fw_version else "")
                + f": {path}\n"
                f"Available platforms: {cls.available_platforms()}\n"
                f"Available versions for {platform_key}: {versions or ['(none)']}"
            )

        data = json.loads(path.read_text(encoding="utf-8"))
        params = [ParamDef.from_dict(item) for item in data.get("parameters", [])]
        meta = {k: v for k, v in data.items() if k != "parameters"}
        return cls(data.get("platform", platform_name), params, meta)

    @classmethod
    def available_platforms(cls) -> list[str]:
        """List platforms with a DEFAULT knowledge base JSON.

        Versioned tables (``<platform>.<fw>.json``) are not platforms — list
        them with :meth:`available_versions`.
        """
        return sorted(p.stem for p in _KNOWLEDGE_DIR.glob("*.json") if "." not in p.stem)

    @classmethod
    def available_versions(cls, platform_name: str) -> list[str]:
        """Firmware versions available for a platform, e.g. ["default", "copter-4.5"]."""
        platform_key = platform_name.lower()
        versions: list[str] = []
        if (_KNOWLEDGE_DIR / f"{platform_key}.json").is_file():
            versions.append("default")
        prefix = f"{platform_key}."
        for p in sorted(_KNOWLEDGE_DIR.glob(f"{platform_key}.*.json")):
            stem = p.name[: -len(".json")]
            if stem.startswith(prefix):
                versions.append(stem[len(prefix) :])
        return versions

    # ── properties ───────────────────────────────────────────

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def fw_version(self) -> str:
        """Firmware version tag of this table ("" for the platform default)."""
        return str(self._meta.get("fw_version", "") or "")

    @property
    def meta(self) -> Dict[str, Any]:
        """Table-level metadata (schema_version, source, firmware version, ...)."""
        return dict(self._meta)

    @property
    def schema_version(self) -> int:
        try:
            return int(self._meta.get("schema_version", 1))
        except (TypeError, ValueError):
            return 1

    # ── query ────────────────────────────────────────────────

    def query(self, name: str) -> Optional[ParamDef]:
        """Lookup by platform parameter name."""
        return self._by_name.get(name.upper())

    def search(self, keyword: str) -> List[ParamDef]:
        """Case-insensitive keyword search, ranked by match quality.

        Searches name, group, category, display name, description, and enum
        member labels (so "analog voltage" finds BATT_MONITOR). Results are
        ordered: exact name > name prefix > name substring > display name >
        description/enum text — so the parameter an agent meant is first.
        """
        kw = keyword.lower().strip()
        if not kw:
            return []
        scored: List[Tuple[int, str, ParamDef]] = []
        for p in self._params:
            name = p.name.lower()
            if name == kw:
                rank = 0
            elif name.startswith(kw):
                rank = 1
            elif kw in name:
                rank = 2
            elif kw in (p.display_name or "").lower():
                rank = 3
            elif kw in p.category.lower() or kw in p.group.lower():
                rank = 4
            elif kw in (p.description or "").lower():
                rank = 5
            elif any(kw in v.lower() for v in p.options().values()):
                rank = 6
            else:
                continue
            scored.append((rank, p.name, p))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [p for _, _, p in scored]

    def list_by_category(self, category: str) -> List[ParamDef]:
        """Return parameters matching the given category."""
        return [p for p in self._params if p.category == category]

    def list_by_group(self, group: str) -> List[ParamDef]:
        """Return parameters in a firmware parameter group.

        Accepts the group name with or without its trailing underscore
        (``ATC`` / ``ATC_`` both work), and falls back to a name-prefix match
        for legacy tables that carry no ``group`` field.
        """
        want = group.upper().rstrip("_")
        exact = [p for p in self._params if p.group.upper().rstrip("_") == want]
        if exact:
            return exact
        return [p for p in self._params if p.name.upper().startswith(want)]

    def groups(self) -> List[Dict[str, Any]]:
        """Group index: name, parameter count, category, sample members.

        Derived from the ``group`` field (schema_version >= 2); for legacy
        tables it degrades to a single synthetic group per category.
        """
        declared = self._meta.get("groups")
        counts: Dict[str, int] = {}
        cats: Dict[str, str] = {}
        samples: Dict[str, List[str]] = {}
        for p in self._params:
            key = p.group or f"({p.category})"
            counts[key] = counts.get(key, 0) + 1
            cats.setdefault(key, p.category)
            if len(samples.setdefault(key, [])) < 3:
                samples[key].append(p.name)

        described: Dict[str, str] = {}
        if isinstance(declared, list):
            for g in declared:
                if isinstance(g, dict) and g.get("name"):
                    described[str(g["name"])] = str(g.get("description", ""))

        out = []
        for key in sorted(counts):
            entry: Dict[str, Any] = {
                "group": key,
                "count": counts[key],
                "category": cats.get(key, ""),
                "sample": samples.get(key, []),
            }
            if described.get(key):
                entry["description"] = described[key]
            out.append(entry)
        return out

    def group_names(self) -> List[str]:
        return [g["group"] for g in self.groups()]

    def list_all(self) -> List[ParamDef]:
        return list(self._params)

    # ── validation ───────────────────────────────────────────

    def validate_detail(self, name: str, value: float) -> Dict[str, Any]:
        """Validate a proposed value, returning a structured verdict.

        Verdict keys: ``valid`` (bool), ``status`` (one of the STATUS_* constants),
        ``message``, plus ``options`` for enum/bitmask parameters.

        Fail-closed rule
        ----------------
        A discrete parameter (``type`` enum/bitmask) with **no** member metadata
        and **no** range is reported ``valid=False, status="unverifiable"`` —
        it is not silently accepted. (Was: ``return True, "value accepted"``,
        which let *any* number through for every enum-typed row — and the
        pre-3.2 scraper mistyped a large share of plain floats as ``enum``,
        so the mandatory safety gate was effectively open.)
        """
        pd = self.query(name)
        if pd is None:
            return {
                "valid": False,
                "status": STATUS_NOT_FOUND,
                "message": f"{name}: NOT FOUND in {self._platform} parameter table",
            }

        disp = pd.name

        # ── enum with known members: must be one of them ──
        if pd.values:
            key = _int_key(value)
            if key is None:
                return {
                    "valid": False,
                    "status": STATUS_NOT_AN_INTEGER,
                    "message": f"{disp}: {value} is not an integer enum value",
                    "options": dict(pd.values),
                }
            if key not in pd.values:
                return {
                    "valid": False,
                    "status": STATUS_NOT_A_MEMBER,
                    "message": (
                        f"{disp}: {key} is not a valid value "
                        f"(allowed: {', '.join(sorted(pd.values, key=_sort_key))})"
                    ),
                    "options": dict(pd.values),
                }
            return {
                "valid": True,
                "status": STATUS_OK,
                "message": f"{disp}: {key} = {pd.values[key]}",
                "options": dict(pd.values),
            }

        # ── bitmask with known bits: must be an int inside the bit span ──
        if pd.bitmask:
            key = _int_key(value)
            if key is None:
                return {
                    "valid": False,
                    "status": STATUS_NOT_AN_INTEGER,
                    "message": f"{disp}: {value} is not an integer bitmask",
                    "options": dict(pd.bitmask),
                }
            bits = [int(b) for b in pd.bitmask if str(b).isdigit()]
            allowed_mask = 0
            for b in bits:
                allowed_mask |= 1 << b
            ival = int(key)
            if ival < 0 or (ival & ~allowed_mask):
                return {
                    "valid": False,
                    "status": STATUS_OUT_OF_RANGE,
                    "message": (
                        f"{disp}: {ival} sets bits outside the defined mask "
                        f"(allowed bits: {sorted(bits)})"
                    ),
                    "options": dict(pd.bitmask),
                }
            set_bits = [pd.bitmask[str(b)] for b in sorted(bits) if ival & (1 << b)]
            return {
                "valid": True,
                "status": STATUS_OK,
                "message": f"{disp}: {ival} = {' | '.join(set_bits) or 'none'}",
                "options": dict(pd.bitmask),
            }

        # ── numeric range check ──
        if pd.min is not None and value < pd.min:
            return {
                "valid": False,
                "status": STATUS_OUT_OF_RANGE,
                "message": f"{disp}: {value:g} below min {pd.min:g}",
            }
        if pd.max is not None and value > pd.max:
            return {
                "valid": False,
                "status": STATUS_OUT_OF_RANGE,
                "message": f"{disp}: {value:g} exceeds max {pd.max:g}",
            }

        if pd.min is None and pd.max is None and pd.is_discrete:
            # discrete parameter with no member metadata → cannot verify
            return {
                "valid": False,
                "status": STATUS_UNVERIFIABLE,
                "message": (
                    f"{disp}: type={pd.type} but the knowledge base has no "
                    f"enum/bitmask members and no range — cannot verify {value:g}"
                ),
                "hint": (
                    pd.unresolved_ref
                    or "Regenerate the parameter table with tools/build_param_tables.py "
                    "(schema_version 2 carries @Values / @Bitmask metadata)"
                ),
            }

        rng = pd.range_str()
        return {
            "valid": True,
            "status": STATUS_OK,
            "message": f"{disp}: {value:g}" + (f" within {rng}" if rng else ""),
        }

    def validate(self, name: str, value: float) -> Tuple[bool, str]:
        """Backwards-compatible wrapper around :meth:`validate_detail`."""
        verdict = self.validate_detail(name, value)
        return bool(verdict["valid"]), str(verdict["message"])

    def categories(self) -> List[str]:
        return sorted({p.category for p in self._params})

    def __len__(self) -> int:
        return len(self._params)

    def __repr__(self) -> str:
        return f"<ParamTable platform={self._platform!r} params={len(self)}>"


def _int_key(value: float) -> Optional[str]:
    """Return the canonical string key for an integral value, else None."""
    try:
        fval = float(value)
    except (TypeError, ValueError):
        return None
    if fval != int(fval):
        return None
    return str(int(fval))


def _sort_key(k: str):
    return (0, int(k)) if str(k).lstrip("-").isdigit() else (1, str(k))
