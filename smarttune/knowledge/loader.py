"""
smarttune/knowledge/loader.py

分层知识库加载器 — 按平台 + 优先级合并规则。

加载顺序（后者 deep_merge 覆盖前者）:
  1. common/              — 跨平台物理规则
  2. {platform}/          — 平台内置规则
  3. ~/.smarttune/knowledge/common/       — 用户通用自定义
  4. ~/.smarttune/knowledge/{platform}/   — 用户平台自定义
  5. smarttune-knowledge-pro common/      — Pro 通用增强
  6. smarttune-knowledge-pro {platform}/  — Pro 平台增强
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_BUILTIN_RULES_DIR = Path(__file__).parent / "rules"
_USER_RULES_DIR = Path.home() / ".smarttune" / "knowledge"


def _deep_merge(base: Dict, override: Dict) -> Dict:
    result = copy.deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def _load_json_dir(directory: Path) -> Dict[str, Any]:
    if not directory.is_dir():
        return {}
    result: Dict[str, Any] = {}
    for json_file in sorted(directory.glob("*.json")):
        try:
            with open(json_file, encoding="utf-8") as fh:
                result[json_file.stem] = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping invalid JSON %s: %s", json_file, exc)
    return result


class KnowledgeBase:
    def __init__(self, platform: str = "ardupilot"):
        self.platform = platform
        self._rules: Dict[str, Any] = {}
        self._source_info: Dict[str, bool] = {
            "builtin_common": False, "builtin_platform": False,
            "user_common": False, "user_platform": False,
            "pro_common": False, "pro_platform": False,
        }
        self._load()

    def _load(self) -> None:
        rules: Dict[str, Any] = {}
        for label, path in [
            ("builtin_common",   _BUILTIN_RULES_DIR / "common"),
            ("builtin_platform", _BUILTIN_RULES_DIR / self.platform),
            ("user_common",      _USER_RULES_DIR / "common"),
            ("user_platform",    _USER_RULES_DIR / self.platform),
        ]:
            loaded = _load_json_dir(path)
            if loaded:
                rules = _deep_merge(rules, loaded)
                self._source_info[label] = True

        try:
            from smarttune_knowledge_pro import load as pro_load
            pro_rules = pro_load(platform=self.platform)
            if pro_rules:
                for label, key in [("pro_common", "common"), ("pro_platform", self.platform)]:
                    sub = pro_rules.get(key, {})
                    if sub:
                        rules = _deep_merge(rules, sub)
                        self._source_info[label] = True
        except ImportError:
            pass

        self._rules = rules

    @property
    def rules(self) -> Dict[str, Any]:
        return self._rules

    @property
    def source_info(self) -> Dict[str, bool]:
        return self._source_info

    def get(self, key: str, default: Any = None) -> Any:
        return self._rules.get(key, default)
