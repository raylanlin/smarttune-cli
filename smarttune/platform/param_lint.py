"""
smarttune/platform/param_lint.py

参数表数据体检 —— 供 ``stune params --lint`` 与 CI 使用。

这些检查是针对 v3.1 及更早 ``ardupilot.json`` 里真实存在过的缺陷写的，
每一条都对应一个曾经把错信息喂给 AI 的 bug：

============================  =============================================
check                         它抓的是什么
============================  =============================================
``name_shape``                名字被剥了组前缀后剩下碎片（``0_BAUD`` /
                              ``10_DIRECTION`` —— 实际是 ``SERIAL0_BAUD`` /
                              ``SERVO10_DIRECTION``）
``suffix_collision``          同一参数可能以完整名和碎片名同时存在
                              （``BATT_LOW_VOLT`` 与 ``LOW_VOLT``）——
                              warn，因为也存在合法的同后缀参数对
``placeholder_leak``          描述里留着上游模板占位符 ``@PREFIX@`` ——
                              抓取时没展开组前缀的铁证
``discrete_without_members``  ``type`` 是 enum/bitmask 却没有成员表也没有区间
                              → 校验闸门对它常开（最高危）
``constant_default``          全表 default 取值只有一种（例如清一色 0.0）
                              → 默认值是编的，不是抓的
``empty_description``         没有描述，AI 只能瞎猜语义
``duplicate_name``            同名参数重复出现
``range_inverted``            min > max
``enum_key_not_int``          枚举键不是整数
============================  =============================================

纯标准库、零依赖，导入不碰 numpy。
"""

from __future__ import annotations

import re
from typing import Any

from smarttune.platform.params import ParamDef, ParamTable

#: Betaflight parameter names are lowercase and some legitimately start with a
#: digit (3d_deadband_high), so the shape rule only rejects genuinely malformed
#: identifiers; prefix-stripping is caught by suffix_collision + placeholder_leak.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"@[A-Z_]+@")

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"


def _finding(check: str, severity: str, param: str | None, detail: str) -> dict[str, Any]:
    return {"check": check, "severity": severity, "param": param, "detail": detail}


def lint_params(params: list[ParamDef]) -> list[dict[str, Any]]:
    """Run every check over a list of parameter definitions."""
    findings: list[dict[str, Any]] = []
    names = [p.name for p in params]
    name_set = set(names)
    missing_descriptions: list[str] = []

    # duplicate names
    seen = set()
    for n in names:
        if n in seen:
            findings.append(_finding("duplicate_name", SEVERITY_ERROR, n,
                                     "appears more than once in the table"))
        seen.add(n)

    # constant default across the whole table → fabricated
    defaults = {repr(p.default) for p in params}
    if len(params) > 20 and len(defaults) == 1:
        only = next(iter(defaults))
        sev = SEVERITY_WARN if only in ("None", "null") else SEVERITY_ERROR
        findings.append(_finding(
            "constant_default", sev, None,
            f"every parameter has default={only} across {len(params)} rows — "
            f"defaults look fabricated rather than scraped"
            if sev == SEVERITY_ERROR else
            f"no defaults in this table (default={only}); upstream metadata may not publish them",
        ))

    for p in params:
        if not _NAME_RE.match(p.name):
            findings.append(_finding(
                "name_shape", SEVERITY_ERROR, p.name,
                "not a valid firmware parameter name — looks like a stripped "
                "group prefix left a fragment",
            ))
        elif p.name[0].isdigit() and p.name.isupper():
            # ALLCAPS starting with a digit is the ArduPilot fragment signature
            # (0_BAUD ← SERIAL0_BAUD); lowercase 3d_* names are real Betaflight
            findings.append(_finding(
                "digit_leading_name", SEVERITY_WARN, p.name,
                "starts with a digit — check it is not a prefix-stripped fragment",
            ))

        # a name that is a strict suffix of another name, e.g. MONITOR vs BATT_MONITOR.
        # Warn, not error: ANGLE_MAX / PSC_ANGLE_MAX and SIMPLE / SUPER_SIMPLE are
        # both real ArduPilot parameters, so this needs a human look.
        for other in name_set:
            if other != p.name and other.endswith("_" + p.name):
                findings.append(_finding(
                    "suffix_collision", SEVERITY_WARN, p.name,
                    f"also present as {other} — check neither is a "
                    f"prefix-stripped duplicate of the other",
                ))
                break

        if _PLACEHOLDER_RE.search(p.description or ""):
            findings.append(_finding(
                "placeholder_leak", SEVERITY_ERROR, p.name,
                "description contains an unexpanded upstream placeholder "
                f"({_PLACEHOLDER_RE.search(p.description).group(0)})",
            ))

        if not (p.description or "").strip():
            missing_descriptions.append(p.name)

        if p.type in ("enum", "bitmask") and not p.values and not p.bitmask \
                and p.min is None and p.max is None:
            # a documented capture gap (unresolved_ref) is a known limitation,
            # not silent corruption — warn instead of error
            findings.append(_finding(
                "discrete_without_members",
                SEVERITY_WARN if p.unresolved_ref else SEVERITY_ERROR,
                p.name,
                (f"type={p.type}, members not captured: {p.unresolved_ref}"
                 if p.unresolved_ref else
                 f"type={p.type} but no members and no range — validate() cannot "
                 f"verify any value for it"),
            ))

        if p.min is not None and p.max is not None and p.min > p.max:
            findings.append(_finding("range_inverted", SEVERITY_ERROR, p.name,
                                     f"min {p.min} > max {p.max}"))

        for key in list(p.values) + list(p.bitmask):
            if not str(key).lstrip("-").isdigit():
                findings.append(_finding("enum_key_not_int", SEVERITY_WARN, p.name,
                                         f"non-integer member key {key!r}"))
                break

    # descriptions: one table-level finding when the upstream simply has none
    # (Betaflight), per-parameter findings when it is a handful of gaps
    if missing_descriptions:
        if params and len(missing_descriptions) > len(params) // 2:
            findings.append(_finding(
                "empty_description", SEVERITY_WARN, None,
                f"{len(missing_descriptions)} of {len(params)} parameters have no "
                f"description (upstream firmware may not publish any)",
            ))
        else:
            for name in missing_descriptions:
                findings.append(_finding("empty_description", SEVERITY_WARN, name,
                                         "no description — agents cannot reason about it"))

    return findings


def lint_table(table: ParamTable) -> dict[str, Any]:
    """Lint a whole table, returning a report dict."""
    findings = lint_params(table.list_all())
    errors = [f for f in findings if f["severity"] == SEVERITY_ERROR]
    warns = [f for f in findings if f["severity"] == SEVERITY_WARN]

    by_check: dict[str, int] = {}
    for f in findings:
        by_check[f["check"]] = by_check.get(f["check"], 0) + 1

    return {
        "platform": table.platform,
        "schema_version": table.schema_version,
        "parameter_count": len(table),
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warns),
        "by_check": by_check,
        "findings": findings,
    }
