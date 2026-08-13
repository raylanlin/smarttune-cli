# SmartTune v3.3.0 验收规范（增量）

基线：v3.2.1 已验收。回归照跑 v3.2.1 §A（版本号改判 3.3.0）。

## A. 多版本参数表

```bash
stune params                                # MUST：ardupilot 行 Versions 含 default, copter-4.5
stune params ap --fw-version copter-4.5 --groups -f json | jq '.parameter_count'
# MUST 4121
stune params ap --fw-version copter-4.5 --group ATC_ | head    # MUST 正常列出
stune params --validate ATC_RAT_RLL_P 0.45 -p ap --fw-version copter-4.5 ; echo exit=$?
# MUST exit=0（4.5 的 max 是 0.5）
stune params --validate ATC_RAT_RLL_P 0.45 -p ap ; echo exit=$?
# MUST exit=1（默认 4.1 表 max 0.35）—— 这一对是多版本语义的核心证据
stune params ap --fw-version copter-9.9 --groups ; echo exit=$?
# MUST exit=1 且报错里列出可用版本
stune params --lint                          # MUST exit 0（含新表，0 errors）
pytest -q tests/test_fw_versions.py          # MUST 7 passed
pytest -q                                    # MUST 全绿
```

## B. MCP

```bash
python tools/smoke_mcp.py --log "$AP_LOG"    # MUST 全过
```

手动/脚本补验：
- `smarttune_validate_param` 响应 **MUST 不含** `status` 键（v3.3 移除），`verdict` 仍在
- `smarttune_list_params(platform="ardupilot", fw_version="copter-4.5", group="ATC_")` MUST 正常返回
- `fw_version="copter-9.9"` MUST 返回 `ok:false, error_code:"E4011"` 且 message 列出可用版本
- `smarttune_get_param("BATT_MONITOR", platform="all", fw_version="copter-4.5")` MUST 只命中 ardupilot（其他平台无此版本被跳过）

## C. 打包

wheel 里 MUST 含 `ardupilot.copter-4.5.json`（package-data 的 `params/*.json` 通配已覆盖，验证即可）。

## D. 数值零回归

分析链路本次零改动 —— `stune analyze` 输出与 v3.2.1 逐字节一致（MUST）。

## E. v3.3.1 增量 — 搜索折叠与截断信号

```bash
pytest -q tests/test_search_collapse.py        # MUST 7 passed
stune params --search monitor -p ap -f json | jq '.platforms.ArduPilot | {count, raw_count, returned, truncated}'
# MUST raw_count > count（克隆已折叠）；若 returned<count 则 MUST truncated:true
stune params --search monitor -p ap -f json | jq '.platforms.ArduPilot.matches[] | select(.name=="BATT_MONITOR").instances'
# MUST 长度 >= 8（BATT_ + BATT2_..BATT9_）
stune params --search gps -p ap --limit 3 -f json | jq '.platforms.ArduPilot | {returned, truncated, note}'
# MUST returned=3, truncated=true, note 含 "more distinct hits"
stune params --search monitor -p ap                 # 终端表格 MUST 显示折叠说明与截断警告
```

MCP 侧：`smarttune_search_params(keyword="monitor", platform="ardupilot")` 的 ArduPilot block
MUST 含 `raw_count`、折叠后 `params[].instances`，截断时 `truncated+note`。

## 已知限制

1. 生成器暂不含 Parameters.md 解析器 —— copter-4.5 表由等价变换生成（§10 类比对暂不可重放），
   v3.3.1 补 `build_ardupilot_md()` 后按 --fw-tag 重放。
2. Web UI 顺延 v3.3.x。
3. copter-4.5 表 default 全为 null（上游 md 不含默认值，与 4.1 表一致的诚实空缺）。
