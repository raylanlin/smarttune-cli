# Claude Task: Implement SmartTune MCP Server

You are working in the `smarttune-cli` repository.

Please implement the plan in `SMARTTUNE_MCP_IMPLEMENTATION_PLAN.md`.

Primary objective:
Add a safe read-only MCP server so OpenClaw agents can analyze flight logs without `exec` or filesystem write privileges.

Hard requirements:

- Do not expose shell execution.
- Do not call `stune` through subprocess.
- Do not accept arbitrary output paths.
- Validate log paths, extensions, allowed roots, and file size.
- Return structured JSON/Markdown suitable for LLM agents.
- Keep existing CLI behavior working.
- Add focused tests for security validation, serialization, and service analysis.

Minimum deliverable:

- `smarttune/mcp_server.py`
- service/serializer helpers
- `pyproject.toml` MCP optional dependency and entry point
- README MCP usage docs
- tests passing

Recommended verification:

```bash
pip install -e ".[all,mcp,dev]"
python -m py_compile smarttune/mcp_server.py
pytest -q
```

If the full CLI JSON flag cleanup is too large, keep it as a documented follow-up. Do not let that block the read-only MCP server.
