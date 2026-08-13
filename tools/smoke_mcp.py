#!/usr/bin/env python3
"""
tools/smoke_mcp.py

SmartTune MCP server stdio smoke test — 验收 v3.2 的返回体契约与 stdout 纯净性。

为什么需要它：stdio MCP 的 stdout 只允许 JSON-RPC 帧。任何一行杂音（第三方
解析器的 print、警告、进度条）都会让客户端解析失败并表现为「无响应」。本脚本
用真实 JSON-RPC 握手驱动服务器，因此**任何 stdout 污染都会直接让它失败**。

用法:
    pip install -e ".[all,mcp,dev]"
    python tools/smoke_mcp.py                        # 只跑不需要日志的工具
    python tools/smoke_mcp.py --log /path/flight.bin # 额外跑分析类工具

退出码: 0 全过 / 1 有失败（失败明细打印在 stderr）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
EXPECTED_TOOLS = {
    "smarttune_list_platforms",
    "smarttune_log_quality",
    "smarttune_analyze_log",
    "smarttune_analyze_pid",
    "smarttune_analyze_fft",
    "smarttune_analyze_magfit",
    "smarttune_analyze_sysid",
    "smarttune_analyze_filter",
    "smarttune_analyze_hardware",
    "smarttune_generate_plot",
    "smarttune_list_param_groups",
    "smarttune_list_params",
    "smarttune_get_param",
    "smarttune_search_params",
    "smarttune_validate_param",
    "smarttune_validate_params",
}

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((ok, name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), file=sys.stderr)
    return ok


class MCPClient:
    """Minimal stdio JSON-RPC client (no mcp package needed on the test side)."""

    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, **(env or {})},
        )
        self._id = 0

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _read(self, timeout: float = 60.0) -> dict[str, Any]:
        """Read one JSON-RPC frame. A non-JSON line means stdout got polluted."""
        assert self.proc.stdout
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout: " + self._drain_stderr())
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                raise RuntimeError("NON-JSON LINE ON STDOUT (this breaks stdio MCP): " + line[:400])
        raise TimeoutError("no response within timeout")

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        while True:
            frame = self._read()
            if frame.get("id") == self._id:
                return frame

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Return (parsed tool payload, raw text)."""
        frame = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in frame:
            raise RuntimeError(f"{name} transport error: {frame['error']}")
        content = frame.get("result", {}).get("content") or []
        text = content[0].get("text", "") if content else ""
        try:
            return json.loads(text), text
        except json.JSONDecodeError:
            return {}, text  # markdown responses are legitimately not JSON

    def _drain_stderr(self) -> str:
        assert self.proc.stderr
        try:
            return self.proc.stderr.read()[-2000:]
        except Exception:
            return ""

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def envelope_ok(payload: dict[str, Any]) -> bool:
    """v3.2 contract: every JSON tool response carries a boolean `ok`."""
    return isinstance(payload, dict) and isinstance(payload.get("ok"), bool)


def run(log_path: Path | None, command: list[str]) -> int:
    print(f"MCP smoke test — command: {' '.join(command)}", file=sys.stderr)
    env = {}
    if log_path:
        env["SMARTTUNE_MCP_ALLOWED_ROOTS"] = str(log_path.resolve().parent)

    client = MCPClient(command, env)
    try:
        # ── handshake ──
        init = client.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "smarttune-smoke", "version": "1.0"},
            },
        )
        check("initialize handshake", "result" in init, str(init.get("error", ""))[:200])
        client.notify("notifications/initialized")

        # ── tool inventory ──
        listed = client.request("tools/list")
        tools = {t["name"] for t in listed.get("result", {}).get("tools", [])}
        missing = EXPECTED_TOOLS - tools
        check(
            f"tools/list exposes {len(EXPECTED_TOOLS)} tools",
            not missing,
            f"missing: {sorted(missing)}" if missing else f"got {len(tools)}",
        )

        annotations = {
            t["name"]: (t.get("annotations") or {})
            for t in listed.get("result", {}).get("tools", [])
        }
        not_readonly = [n for n, a in annotations.items() if a.get("readOnlyHint") is not True]
        check(
            "every tool is annotated readOnlyHint=True",
            not not_readonly,
            f"not read-only: {not_readonly}" if not_readonly else "",
        )

        # ── platforms ──
        payload, _ = client.call_tool("smarttune_list_platforms", {})
        check(
            "list_platforms envelope has ok=true",
            envelope_ok(payload) and payload.get("ok") is True,
        )
        check(
            "list_platforms returns 3 platforms",
            len(payload.get("platforms", [])) >= 3,
            f"got {len(payload.get('platforms', []))}",
        )

        # ── parameter groups ──
        payload, _ = client.call_tool("smarttune_list_param_groups", {"platform": "ardupilot"})
        groups = payload.get("groups", [])
        check(
            "list_param_groups(ardupilot) returns the group index",
            payload.get("ok") is True and len(groups) > 100,
            f"{len(groups)} groups",
        )
        check(
            "group index records upstream provenance",
            bool((payload.get("source") or {}).get("upstream")),
        )

        # ── refuse to dump a whole table ──
        payload, raw = client.call_tool("smarttune_list_params", {"platform": "ardupilot"})
        check(
            "list_params without a filter is refused (payload-size guard)",
            payload.get("ok") is False and payload.get("error_code") == "E4000",
            f"ok={payload.get('ok')} code={payload.get('error_code')}",
        )

        # ── slim group listing ──
        payload, raw = client.call_tool(
            "smarttune_list_params", {"platform": "ardupilot", "group": "ATC_", "limit": 50}
        )
        params = payload.get("params", [])
        check(
            "list_params(group=ATC_) returns compact rows",
            payload.get("ok") is True and params and "description" not in params[0],
            f"{len(params)} rows, {len(raw)} bytes",
        )
        check("compact group listing stays under 32 KB", len(raw) < 32_000, f"{len(raw)} bytes")

        # ── one parameter, full detail + enum meanings ──
        payload, _ = client.call_tool("smarttune_get_param", {"param_name": "BATT_MONITOR"})
        match = (payload.get("matches") or [{}])[0]
        check(
            "get_param(BATT_MONITOR) carries description + enum members",
            payload.get("ok") is True
            and bool(match.get("description"))
            and match.get("values", {}).get("4") == "Analog Voltage and Current",
            f"values[4]={match.get('values', {}).get('4')!r}",
        )

        # ── ranked search reaches enum labels ──
        payload, _ = client.call_tool(
            "smarttune_search_params", {"keyword": "analog voltage", "platform": "ardupilot"}
        )
        hits = [
            p["name"]
            for p in (payload.get("platforms", {}).get("ArduPilot", {}).get("params") or [])
        ]
        check("search_params matches enum labels", "BATT_MONITOR" in hits, f"top hits: {hits[:5]}")

        # ── validation: the safety gate ──
        payload, _ = client.call_tool(
            "smarttune_validate_param",
            {"param_name": "BATT_MONITOR", "param_value": 4, "platform": "ardupilot"},
        )
        check(
            "validate_param accepts a legal enum member",
            payload.get("ok") is True
            and payload.get("valid") is True
            and payload.get("verdict") == "ok",
        )

        payload, _ = client.call_tool(
            "smarttune_validate_param",
            {"param_name": "BATT_MONITOR", "param_value": 99, "platform": "ardupilot"},
        )
        check(
            "validate_param REJECTS an undefined enum value (v3.1 accepted it)",
            payload.get("valid") is False and payload.get("verdict") == "not_a_member",
            f"verdict={payload.get('verdict')}",
        )
        check("rejection tells the agent what is allowed", bool(payload.get("options")))

        payload, _ = client.call_tool(
            "smarttune_validate_param",
            {"param_name": "ATC_RAT_RLL_P", "param_value": 999, "platform": "ardupilot"},
        )
        check(
            "validate_param enforces numeric range",
            payload.get("valid") is False and payload.get("verdict") == "out_of_range",
            f"verdict={payload.get('verdict')}",
        )

        payload, _ = client.call_tool(
            "smarttune_validate_param",
            {"param_name": "NO_SUCH_PARAM_XYZ", "param_value": 1, "platform": "ardupilot"},
        )
        check(
            "validate_param rejects unknown names",
            payload.get("valid") is False and payload.get("verdict") == "not_found",
        )

        # ── batch validation (v3.2.1) ──
        payload, _ = client.call_tool(
            "smarttune_validate_params",
            {
                "recommendations": [
                    {"param": "BATT_MONITOR", "value": 4},
                    {"param": "ATC_RAT_RLL_P", "value": 999},
                    {"param": "NO_SUCH_PARAM_XYZ", "value": 1},
                ],
                "platform": "ardupilot",
            },
        )
        verdicts = [r.get("verdict") for r in payload.get("results", [])]
        check(
            "validate_params batches a whole recommendation set",
            payload.get("ok") is True
            and payload.get("all_valid") is False
            and payload.get("valid_count") == 1
            and verdicts == ["ok", "out_of_range", "not_found"],
            f"verdicts={verdicts}",
        )

        # ── unified error shape ──
        payload, _ = client.call_tool("smarttune_get_param", {"param_name": "NOPE_XYZ"})
        keys = {"ok", "error_code", "message", "hint", "retryable"}
        check(
            "failures use the unified error shape",
            payload.get("ok") is False and keys <= set(payload),
            f"missing: {sorted(keys - set(payload))}",
        )
        check("deterministic failures are marked non-retryable", payload.get("retryable") is False)

        payload, _ = client.call_tool("smarttune_log_quality", {"log_path": "/etc/passwd"})
        check(
            "path validation rejects a file outside the allowed roots",
            payload.get("ok") is False,
            f"code={payload.get('error_code')}",
        )

        # ── log-dependent tools ──
        if log_path:
            payload, raw = client.call_tool("smarttune_log_quality", {"log_path": str(log_path)})
            check(
                "log_quality on a real log",
                payload.get("ok") is True,
                payload.get("message", "")[:160],
            )

            payload, raw = client.call_tool(
                "smarttune_analyze_log", {"log_path": str(log_path), "max_recommendations": 10}
            )
            check(
                "analyze_log on a real log",
                payload.get("ok") is True,
                payload.get("message", "")[:160],
            )
            if payload.get("ok"):
                check(
                    "analyze_log returns modules",
                    bool(payload.get("modules")),
                    f"{list(payload.get('modules', {}))}",
                )
                check(
                    "analyze_log payload stays under 256 KB",
                    len(raw) < 262_144,
                    f"{len(raw)} bytes",
                )

                # v3.2.1: every recommendation ships pre-validated
                found_recs = []

                def _collect(node):
                    if isinstance(node, dict):
                        recs = node.get("recommendations")
                        if isinstance(recs, list):
                            found_recs.extend(r for r in recs if isinstance(r, dict))
                        for v in node.values():
                            _collect(v)
                    elif isinstance(node, list):
                        for v in node:
                            _collect(v)

                _collect(payload.get("modules", {}))
                if found_recs:
                    unannotated = [r.get("param") for r in found_recs if "validated" not in r]
                    check(
                        "analyze_log recommendations arrive pre-validated",
                        not unannotated,
                        f"{len(found_recs)} recs, unannotated: {unannotated[:5]}",
                    )
                else:
                    print("  [SKIP] no recommendations in this log's analysis", file=sys.stderr)

            _, md = client.call_tool(
                "smarttune_analyze_log", {"log_path": str(log_path), "response_format": "markdown"}
            )
            check(
                "markdown report still renders",
                md.lstrip().startswith("#"),
                md[:80].replace("\n", " "),
            )
        else:
            print("  [SKIP] log-dependent tools (pass --log to include them)", file=sys.stderr)

    except Exception as exc:
        check("smoke run completed without transport errors", False, f"{type(exc).__name__}: {exc}")
    finally:
        stderr_tail = client._drain_stderr()
        client.close()

    failures = [name for ok, name, _ in _results if not ok]
    print("", file=sys.stderr)
    if failures:
        print(f"FAILED {len(failures)}/{len(_results)}:", file=sys.stderr)
        for name in failures:
            print(f"  - {name}", file=sys.stderr)
        if stderr_tail.strip():
            print("\nserver stderr tail:\n" + stderr_tail[-1200:], file=sys.stderr)
        return 1
    print(f"OK — {len(_results)}/{len(_results)} checks passed", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--log", type=Path, default=None, help="Flight log to exercise the analysis tools with"
    )
    parser.add_argument(
        "--command", default=None, help="Server command (default: python -m smarttune.mcp_server)"
    )
    args = parser.parse_args(argv)

    command = (
        args.command.split() if args.command else [sys.executable, "-m", "smarttune.mcp_server"]
    )
    if args.log and not args.log.exists():
        parser.error(f"log not found: {args.log}")
    return run(args.log, command)


if __name__ == "__main__":
    raise SystemExit(main())
