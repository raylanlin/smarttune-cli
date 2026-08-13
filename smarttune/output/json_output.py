"""
smarttune/output/json_output.py

CLI JSON 输出层 —— 支撑 ``stune <cmd> --format json``。

设计约束（与 README "For Agents" 的承诺对齐）:

1. **stdout 只有 JSON**。所有人类可读输出（进度、错误面板、提示）走 stderr，
   因此 ``stune analyze -i log.bin -f json | jq .`` 永远拿到干净的 JSON。
2. **单一序列化路径**。payload 直接来自 services 层 —— 与 MCP server 调用同一批
   函数（``analyze_log`` / ``analyze_pid`` / ...），所以 CLI JSON 与 MCP JSON 天然
   同构，不存在会漂移的第二套序列化逻辑。
3. **严格 JSON**。NaN / Infinity 在写出前替换为 ``null``（``allow_nan=False``），
   避免下游 ``json.loads`` 在严格解析器上炸掉。
4. **失败也是 JSON**。异常输出 ``status="error"`` + 结构化 error 对象，退出码 1；
   agent 不需要区分"成功解析 stdout"和"去 stderr 抓文字"两条路径。
5. **可确定性**。设置 ``SMARTTUNE_DETERMINISTIC=1`` 时省略 ``generated_at``，
   同一日志的输出可逐字节 diff（CI 回归 / 快照测试用）。
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smarttune import __version__

#: JSON 契约版本。破坏性字段变更时递增 major。
SCHEMA_VERSION = "1.0"

_TRUTHY = {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# 清洗 / 编码
# ---------------------------------------------------------------------------


def sanitize(value: Any) -> Any:
    """递归把非有限浮点（NaN/±Inf）替换为 ``None``。

    services 层的 ``_safe_float`` 只在 NumPy 可用时兜底，纯 Python float
    仍可能带 NaN（例如未采集到的 step 指标）。这里做最后一道保险，
    保证 ``allow_nan=False`` 不会抛 ``ValueError``。
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return value


def dumps(payload: Any) -> str:
    """编码为严格 JSON 文本（尾随换行，便于逐行管道消费）。"""
    return (
        json.dumps(
            sanitize(payload),
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            default=str,
            sort_keys=False,
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# 信封
# ---------------------------------------------------------------------------


def _deterministic() -> bool:
    return os.environ.get("SMARTTUNE_DETERMINISTIC", "").lower() in _TRUTHY


def build_envelope(
    command: str,
    payload: dict[str, Any] | None = None,
    *,
    status: str = "ok",
) -> dict[str, Any]:
    """包装 payload：元信息在前，业务字段在后（保持 key 顺序稳定）。"""
    env: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "smarttune", "version": __version__},
        "command": command,
        "status": status,
    }
    if not _deterministic():
        env["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if payload:
        for key, value in payload.items():
            if key not in env:
                env[key] = value
    return env


def error_envelope(command: str, exc: BaseException) -> dict[str, Any]:
    """把异常渲染成结构化 error 信封。"""
    code = getattr(exc, "code", None) or "E0000"
    message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    hint = getattr(exc, "hint", "") or ""
    return build_envelope(
        command,
        {
            "error": {
                "code": code,
                "type": exc.__class__.__name__,
                "message": message,
                "hint": hint,
            }
        },
        status="error",
    )


# ---------------------------------------------------------------------------
# 写出
# ---------------------------------------------------------------------------


def emit(envelope: dict[str, Any], output_file: Path | None = None) -> int:
    """写出信封。返回建议的进程退出码（ok=0 / error=1）。"""
    text = dumps(envelope)
    if output_file is not None:
        path = Path(output_file)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        # 确认信息走 stderr —— stdout 保持"要么 JSON，要么空"
        print(f"JSON saved: {path}", file=sys.stderr)
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 0 if envelope.get("status") == "ok" else 1


def emit_result(
    command: str,
    payload: dict[str, Any],
    output_file: Path | None = None,
) -> int:
    """成功路径：``emit(build_envelope(...))``。

    v3.2.1 契约收口：信封 ``status`` 只有 ``ok`` / ``error`` 两个值。
    领域裁决（如参数校验的 ``not_a_member``）放在 payload 的 ``verdict``
    字段里 —— 被拒是一次成功的调用，不是传输错误。
    """
    return emit(build_envelope(command, payload), output_file)


def fail(
    command: str,
    exc: BaseException,
    output_file: Path | None = None,
) -> int:
    """失败路径：写出 error 信封，返回退出码 1。"""
    return emit(error_envelope(command, exc), output_file)
