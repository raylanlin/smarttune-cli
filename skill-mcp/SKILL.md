---
name: smarttune-mcp
description: Read-only SmartTune flight log analysis through MCP tools. Use for agents that do not have exec/shell/write permission, especially customer-support agents handling .bin/.log/.bbl/.bfl/.ulg flight logs through OpenClaw MCP.
---

# SmartTune MCP

Use this skill when SmartTune is connected through MCP and the agent does not have shell, exec, or write permission.

This is the MCP-only variant of the SmartTune skill. If the agent has normal CLI access, use the `smarttune` skill instead.

## Hard Rules

* Do not call `exec`, shell commands, Python subprocesses, or the `stune` CLI.
* Do not ask for write, edit, or process execution permissions to analyze a log.
* Do not create report files or choose arbitrary output paths.
* Do not modify logs, parameters, firmware, local files, or aircraft configuration.
* Use only the SmartTune MCP tools listed below.
* If these tools are unavailable, say SmartTune MCP is not connected and ask the operator to enable the MCP server.

## Available MCP Tools

* `smarttune_list_platforms` — list supported platforms, capabilities, CLI extensions, and MCP-accepted extensions.
* `smarttune_log_quality` — inspect whether the log has enough useful data before deeper analysis.
* `smarttune_analyze_log` — run read-only PID, FFT, magnetometer, and hardware analysis and return JSON or Markdown.

## Supported Logs

* ArduPilot: `.bin`, `.log`
* Betaflight: `.bbl`, `.bfl`
* PX4: `.ulg`

Betaflight `.txt` blackbox logs may be supported by the CLI, but they are intentionally not accepted by the MCP server.

## Workflow

1. If the platform or extension is unclear, call `smarttune_list_platforms` first.
2. Call `smarttune_log_quality` before making tuning claims.
3. If the log quality is usable, call `smarttune_analyze_log`.
4. Use `include_modules` only when the user asks for a narrow topic:
   * PID tuning: `pid`
   * vibration or notch filters: `fft`
   * compass problems: `magfit`
   * hardware and parameter overview: `hardware`
5. Prefer `response_format="markdown"` for direct user replies and `response_format="json"` when another agent will consume the result.

## Response Style

* Start with whether the log is usable.
* Explain the main fault signals in plain language.
* Give conservative tuning advice and separate confidence from assumptions.
* Recommend one change at a time and ask for a new log after changes.
* Do not claim an aircraft is safe to fly. Say what the log does and does not support.
* Do not paste raw JSON unless the user asks for machine-readable output.

## Error Handling

* If the path is rejected: ask the user/operator to upload the original log through the supported channel.
* If the extension is unsupported: list the accepted MCP extensions.
* If the log is too large: ask the operator to raise the MCP server file-size limit or provide a smaller log segment.
* If analysis returns partial failures: report the modules that succeeded, then explain which data was missing for the failed modules.
