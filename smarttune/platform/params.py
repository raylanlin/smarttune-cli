"""
smarttune/platform/params.py

Parameter table — loads from knowledge base JSON files.

Knowledge base JSON files live in smarttune/knowledge/params/<platform>.json.
Each file contains a platform name, version info, and a list of parameter
definitions scraped from the official firmware source.
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


_KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge" / "params"


@dataclass
class ParamDef:
    """Single parameter definition loaded from knowledge base."""
    name: str               # platform-specific parameter name, e.g. "ATC_RAT_RLL_P"
    category: str           # "pid" | "filter" | "rate" | "mag" | "misc" | ...
    type: str               # "float" | "int" | "enum"
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None
    unit: str = ""
    description: str = ""


class ParamTable:
    """Read-only parameter table loaded from knowledge base JSON."""

    def __init__(self, platform_name: str, params: List[ParamDef]) -> None:
        self._platform = platform_name
        self._params = list(params)
        self._by_name: dict[str, ParamDef] = {}
        for p in self._params:
            self._by_name[p.name.upper()] = p

    # ── factory ──────────────────────────────────────────────

    @classmethod
    def from_knowledge(cls, platform_name: str) -> "ParamTable":
        """Load parameter table from knowledge base JSON.

        Looks for smarttune/knowledge/params/<platform_name>.json.
        """
        path = _KNOWLEDGE_DIR / f"{platform_name.lower()}.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"Knowledge base not found for {platform_name}: {path}\n"
                f"Available: {[p.stem for p in _KNOWLEDGE_DIR.glob('*.json')]}"
            )

        data = json.loads(path.read_text(encoding="utf-8"))
        params = [ParamDef(**item) for item in data.get("parameters", [])]
        return cls(data.get("platform", platform_name), params)

    @classmethod
    def available_platforms(cls) -> List[str]:
        """List platforms with knowledge base JSON files."""
        return sorted(p.stem for p in _KNOWLEDGE_DIR.glob("*.json"))

    # ── properties ───────────────────────────────────────────

    @property
    def platform(self) -> str:
        return self._platform

    # ── query ────────────────────────────────────────────────

    def query(self, name: str) -> Optional[ParamDef]:
        """Lookup by platform parameter name."""
        return self._by_name.get(name.upper())

    def search(self, keyword: str) -> List[ParamDef]:
        """Case-insensitive search across name, category, and description."""
        kw = keyword.lower()
        return [
            p for p in self._params
            if kw in p.name.lower()
            or kw in p.category.lower()
            or kw in p.description.lower()
        ]

    def list_by_category(self, category: str) -> List[ParamDef]:
        """Return parameters matching the given category."""
        return [p for p in self._params if p.category == category]

    def list_all(self) -> List[ParamDef]:
        return list(self._params)

    def validate(self, name: str, value: float) -> Tuple[bool, str]:
        """Check if param exists and value is within [min, max].

        Returns (is_valid, human_message).
        """
        pd = self.query(name.upper())
        if pd is None:
            return False, f"{name}: NOT FOUND in {self._platform} parameter table"

        display_name = pd.name
        if pd.type not in ("float", "int"):
            return True, f"{display_name}: type={pd.type}, value accepted"

        if pd.min is not None and value < pd.min:
            return False, f"{display_name}: {value:.3f} below min {pd.min:.3f}"
        if pd.max is not None and value > pd.max:
            return False, f"{display_name}: {value:.3f} exceeds max {pd.max:.3f}"
        return True, f"{display_name}: {value:.3f} within [{pd.min:.3f}, {pd.max:.3f}]"

    def categories(self) -> List[str]:
        return sorted({p.category for p in self._params})

    def __len__(self) -> int:
        return len(self._params)

    def __repr__(self) -> str:
        return f"<ParamTable platform={self._platform!r} params={len(self)}>"
