# SmartTune v3.2.1 验收规范（增量）

在 **v3.2.0 已验收通过**的基础上验证本次增量。基线回归照跑 §A；新功能验收 §B–§F。
任何 MUST 失败 → 不推送，按 v3.2 规范 §12 的格式回报。

版本：`3.2.0` → `3.2.1`。本次**不应改变任何分析数值**（§F 复用 v3.2 的 §11 判据）。

---

## A. 基线回归

```bash
pip install -e ".[all,mcp,dev]"
python -c "import smarttune; print(smarttune.__version__)"    # MUST 3.2.1
python -m compileall -q smarttune tools
ruff check --select E9,F63,F7,F82 smarttune/ tests/            # MUST 0
black --check smarttune/ tools/ tests/                         # SHOULD 过；不过则 black 后重测
pytest -q                                                      # MUST 全绿（v3.2 的 232 + 新增 ~11）
stune params --lint                                            # MUST exit 0
```

新增测试文件单独跑：

```bash
pytest -q tests/test_inline_validation.py tests/test_param_tables.py
```

---

## B. 内联校验（本次核心）

分析结果里的每条 recommendation 必须自带校验结论：

```bash
stune analyze -i "$AP_LOG" -f json | jq '[.. | objects | select(has("suggested"))] |
  {total: length, annotated: [.[] | select(has("validated"))] | length}'
```

**MUST**：`total == annotated`（每条推荐都有 `validated` 字段）。
**MUST**：任一条目形如 `{"validated": true, "validation_status": "ok"}` 或
`{"validated": false, "validation_status": "...", "validation_message": "..."}`。

```bash
stune pid -i "$AP_LOG" -f json | jq '.axes.roll.recommendations[0] | {validated, validation_status}'
stune fft -i "$AP_LOG" -f json | jq '.recommendations[] | {param, suggested, validated}'
```

**MUST**：单模块命令（pid/fft/magfit）的推荐同样带注解。
**MUST**：`stune fft` 的推荐列表**非空**（v3.2.0 及之前 dict 形 FFT 推荐被静默丢弃 —— 若这份日志确实无振动建议则换一份能产生 notch 建议的日志验证）。

Markdown 报告同样要有 FFT 推荐：

```bash
stune analyze -i "$AP_LOG" --report md -o /tmp/r.md && grep -c "filter\.\|INS_HNTCH\|notch" /tmp/r.md
```

**SHOULD**：命中 > 0（取决于该日志是否触发滤波建议）。

---

## C. 批量校验

```bash
echo '[{"param":"BATT_MONITOR","value":4},
       {"param":"ATC_RAT_RLL_P","value":999},
       {"param":"NO_SUCH_PARAM_XYZ","value":1},
       {"param":"BATT_MONITOR","value":"abc"}]' > /tmp/recs.json

stune params --validate-batch /tmp/recs.json -p ap -f json ; echo "exit=$?"
```

**MUST**：exit=1；`command == "params.validate_batch"`；`count==4, valid_count==1, all_valid==false`；
`results[].verdict == ["ok","out_of_range","not_found","invalid_input"]`。

```bash
echo '[{"param":"ATC_RAT_RLL_P","value":0.15}]' | stune params --validate-batch - -p ap ; echo "exit=$?"
# MUST exit=0（stdin 路径 + 全部合法）
stune params --validate-batch /tmp/recs.json ; echo "exit=$?"
# MUST exit=1 且提示需要 --platform
```

---

## D. analyze 新选项

```bash
stune analyze -i "$AP_LOG" --modules pid,fft -f json | jq '.modules | keys'
# MUST 只含 pid/fft（及 module_failures 允许存在），无 magfit/hardware/sysid/filter
stune analyze -i "$AP_LOG" --modules pid --max-recommendations 2 -f json \
  | jq '[.modules.pid.axes[].recommendations | length] | add'
# MUST <= 2
stune analyze -i "$AP_LOG" --modules nope ; echo "exit=$?"
# MUST exit=1 且列出合法模块名
stune analyze -i "$AP_LOG" --modules pid,fft        # 文本模式也只跑这两个模块
```

---

## E. 契约收口 + MCP

```bash
stune params --validate BATT_MONITOR 99 -p ap -f json | jq '{status, valid, verdict}'
```

**MUST**：`status == "ok"`（信封只有 ok/error）、`valid == false`、`verdict == "not_a_member"`、退出码 1。

```bash
python tools/smoke_mcp.py --log "$AP_LOG"
```

**MUST**：exit 0。本版脚本新增断言：16 个工具、`smarttune_validate_params` 批量往返、
`validate_param` 返回 `verdict` 字段、`analyze_log` 的推荐全部预校验。

**MUST**（3.9 环境或未装 mcp 的 venv 里）：

```bash
python -m smarttune.mcp_server ; echo "exit=$?"
# MUST exit=1 且 stderr 是可读的安装指引（不是 ImportError traceback）
python -c "import smarttune.mcp_server; print('importable')"
# MUST 可导入（测试收集依赖这一点）
```

**MUST**：MCP `validate_param` 响应同时含 `verdict` 与 `status`（同值；status 已标记 v3.3 移除）。

---

## F. 数值零回归

与 v3.2.0 对比同一日志的 `--report md` 输出：**除了新增的 FFT 推荐行**（B 节的修复本就该新增内容），
PID/FFT/MagFit 的指标与推荐数值必须逐字一致。若指标数值有变 → 停止发版。

---

## G. 发版

全过后：commit（英文 message）→ push → tag `v3.2.1` → release（取 CHANGELOG v3.2.1 段）→
服务器同步 + `smoke_mcp` 部署验证。

已知非问题：MCP `status` 字段与 `verdict` 并存属预期（一个版本的弃用期）；
`--visual` 在 json 模式仍被忽略。
