# SmartTune v3.2 验收规范（Acceptance Test Spec）

给部署本更新包的本地 agent。**按顺序执行，每一节都有明确的通过判据。**
任何一项 `MUST` 失败 → 不推送、不发版，按 §12 的格式回报。

本次改动横跨数据、CLI、MCP、打包四层，其中两处涉及安全语义（参数校验闸门、
stdout 纯净性），因此验收重点不是「跑起来了」，而是**旧行为确实被修掉了**。

- 版本：`3.1.0` → `3.2.0`（`pyproject.toml` + `smarttune/__init__.py`）
- 生成环境未运行过 Python：`pytest` / `ruff` / `tools/build_param_tables.py`
  **从未在生成侧执行过**，§2–§4 是首次真实验证。

---

## 0. 变更清单（验收对象）

| # | 变更 | 风险 | 验收章节 |
|---|------|------|----------|
| 1 | 三平台参数表全量重建（schema_version 2，含参数组 + 枚举含义） | 数据错误会直接误导 AI | §4 |
| 2 | `ParamTable.validate()` 对 enum 不再无条件放行（fail-closed） | 安全闸门 | §4.3 |
| 3 | `stune params` 重写：组索引 / 组内查询 / 排序搜索 / `--lint` | CLI 兼容性 | §5 |
| 4 | 全部 10 个 CLI 命令支持 `-f/--format json`（v3.1 引入，本次一并验收） | 契约 | §6 |
| 5 | MCP：15 工具、返回体瘦身、统一错误形状、stdout 隔离 | 「无响应」故障 | §7 |
| 6 | `smarttune/platform/__init__.py` 改 PEP 562 懒加载 | 导入副作用 | §8 |
| 7 | `package-data` 补 `knowledge/params/*.json` + `py.typed` | pip 装完即坏 | §9 |
| 8 | 新增 `tools/build_param_tables.py`（抓取脚本入库） | 可重现性 | §10 |

---

## 1. 环境准备

```bash
git switch -c release/v3.2            # 在干净分支上验收
pip install -e ".[all,mcp,dev]"
python -c "import smarttune; print(smarttune.__version__)"     # 期望 3.2.0
```

**MUST**：版本输出为 `3.2.0`。

准备一份真实日志各一（用于 §6.4 / §7.4 / §11）：

```bash
export AP_LOG=/path/to/flight.bin     # ArduPilot DataFlash
export BF_LOG=/path/to/flight.bbl     # Betaflight Blackbox
export PX4_LOG=/path/to/flight.ulg    # PX4 ULog（可选）
```

---

## 2. 静态检查

```bash
python -m compileall -q smarttune tools
ruff check smarttune/ tools/
black --check smarttune/
```

**MUST**：`compileall` 无输出（零语法错误）。
**MUST**：`ruff` 零 error。
**SHOULD**：`black --check` 通过；若仅是格式差异，运行 `black smarttune/ tools/` 后**重跑 §3**再提交。

> 已知：`smarttune/cli.py` 与 `smarttune/mcp_server.py` 内有大量函数内 import（懒加载，
> 有意为之），ruff 若配置了 `PLC0415` 之类规则会报，属预期，可加 `# noqa` 或忽略该规则。

---

## 3. 单元测试

```bash
pytest -q
pytest -q tests/test_param_tables.py tests/test_cli_json.py   # 本次新增
```

**MUST**：全绿，**0 failed / 0 error / 0 skipped / 0 xfail**。
**MUST**：总数 ≥ 上一版的 167 + 新增两个文件的用例数（新增约 50）。
**MUST**：新增两个文件单独跑也全绿（用于定位）。

失败时优先看：`tests/test_param_tables.py::test_lint_reports_no_errors` 的 findings 输出，
它会直接指出哪一张表的哪条规则不过。

---

## 4. 参数表数据验收（本次核心）

### 4.1 表级体检

```bash
stune params                       # 三张表概览
stune params --lint                # 退出码 0
python tools/build_param_tables.py --check
```

**MUST**：`--lint` 退出码 0，即三张表 **0 errors**。
**MUST**：概览数字符合下表（±0，除非重新生成过）：

| 平台 | key | 参数数 | 组数 | schema |
|------|-----|-------:|-----:|:------:|
| ArduPilot | `ardupilot` | 2839 | 194 | v2 |
| Betaflight | `betaflight` | 814 | 82 | v2 |
| PX4 | `px4` | 1908 | 78 | v2 |

**INFO**（warnings 允许存在，仅供参考）：ArduPilot ~13、Betaflight ~20、PX4 0。
warning 的合法来源：`suffix_collision`（`ANGLE_MAX` / `PSC_ANGLE_MAX` 是两个真实参数）、
`constant_default`（上游不发布默认值）、`empty_description`（Betaflight 固件无描述）、
`enum_key_not_int`（上游 Values 里的非整数键）、`discrete_without_members`（`unresolved_ref` 已标注）。

### 4.2 旧缺陷回归（逐条确认已修）

```bash
# ① 名字必须是飞控里的真名，不是剥了前缀的碎片
stune params BATT_MONITOR                     # MUST 找到
stune params MONITOR                          # MUST 退出码 1（不存在）
stune params SERIAL0_BAUD                     # MUST 找到

# ② 描述不再整列偏移一位
stune params BATT_ARM_MAH -f json | jq -r '.matches[0].description' | head -c 120
#   MUST 讲的是 "capacity ... required to arm"，MUST NOT 出现 "options"
stune params BATT_MONITOR -f json | jq -r '.matches[0].description'
#   MUST 讲 monitoring voltage/current

# ③ 描述里不得残留上游占位符
grep -c '@PREFIX@' smarttune/knowledge/params/*.json      # MUST 全为 0

# ④ 枚举必须带含义
stune params BATT_MONITOR -f json | jq '.matches[0].values["4"]'
#   MUST == "Analog Voltage and Current"

# ⑤ 默认值不再是编的 0.0
jq '[.parameters[].default] | unique | length' smarttune/knowledge/params/px4.json
#   MUST > 1（PX4 是上游真默认值）
jq '[.parameters[].default] | unique' smarttune/knowledge/params/ardupilot.json
#   MUST == [null]（上游不发布默认值 → null 表示 unknown，不是 0）

# ⑥ 溯源块存在
jq '.source' smarttune/knowledge/params/betaflight.json
#   MUST 含 upstream / path / generated / generator
```

### 4.3 校验闸门（安全项，**逐条 MUST**）

v3.1 的 `validate()` 对任何非 float/int 类型直接 `return True`，叠加 type 误标，
使相当大比例参数完全跳过校验。以下用例必须全部符合预期：

```bash
stune params --validate BATT_MONITOR 4  -p ap ; echo "exit=$?"   # exit=0  ✓ 合法枚举成员
stune params --validate BATT_MONITOR 99 -p ap ; echo "exit=$?"   # exit=1  ✗ not_a_member，并列出允许值
stune params --validate BATT_MONITOR 4.5 -p ap ; echo "exit=$?"  # exit=1  ✗ not_an_integer
stune params --validate ATC_RAT_RLL_P 0.15 -p ap ; echo "exit=$?" # exit=0
stune params --validate ATC_RAT_RLL_P 999  -p ap ; echo "exit=$?" # exit=1  ✗ out_of_range
stune params --validate NO_SUCH_PARAM_XYZ 1 -p ap ; echo "exit=$?" # exit=1 ✗ not_found
stune params --validate p_roll 45  -p bf ; echo "exit=$?"         # exit=0
stune params --validate p_roll 999 -p bf ; echo "exit=$?"         # exit=1
stune params --validate MC_ROLLRATE_P 0.15 -p px4 ; echo "exit=$?" # exit=0
```

```bash
# status 字段必须精确
stune params --validate BATT_MONITOR 99 -p ap -f json | jq '{valid,status,options:(.options|length)}'
# MUST: {"valid": false, "status": "not_a_member", "options": <>0>}
```

**MUST**：拒绝时 `options` 非空（拒绝必须告诉 AI 什么才是允许的）。
**MUST**：不存在「任何数值都通过」的参数 —— 抽查 5 个 `type=enum` 的参数各喂一个荒谬值：

```bash
for p in $(jq -r '[.parameters[] | select(.type=="enum") | .name] | .[0:5] | .[]' \
          smarttune/knowledge/params/ardupilot.json); do
  stune params --validate "$p" 987654 -p ap >/dev/null 2>&1
  echo "$p exit=$?"      # MUST 全部 exit=1
done
```

### 4.4 组分类 / 组内查询 / 搜索（本次新需求）

```bash
stune params ap --groups                       # MUST 194 组，带计数/分类/样例
stune params ap --group ATC_                   # MUST 含 ATC_RAT_RLL_P
stune params ap --group atc                    # MUST 与上一条同结果（大小写/下划线容错）
stune params bf --group PID_PROFILE             # MUST 含 p_roll / d_max_roll
stune params px4 --group "Multicopter Rate Control"   # MUST 含 MC_ROLLRATE_P
stune params ap -c pid                          # MUST 只有 pid 类
stune params --search notch                     # MUST 跨平台命中
stune params --search "analog voltage"          # MUST 命中 BATT_MONITOR（搜到枚举标签）
stune params --search BATT_MONITOR -f json | jq -r '.matches[0].name'   # MUST == BATT_MONITOR（精确名排第一）
```

**MUST**：`stune params ap`（不带筛选）显示**组索引**，不是倒出 2839 行。

---

## 5. CLI 兼容性回归

**MUST**：以下命令的既有行为不变（只验证不报错、输出形态未退化）：

```bash
stune --version                    # 3.2.0
stune --help                       # 命令清单含 params
stune params --help
stune platforms
stune analyze --help ; stune pid --help ; stune fft --help
stune filter --help ; stune quality --help ; stune sysid --help ; stune hardware --help
```

**MUST**：`stune params <PARAM>` / `--search` / `--validate` / `--category` 四种旧用法仍可用
（本次只新增 `--groups` / `--group` / `--lint` / `--limit` / `-f`）。

---

## 6. JSON 契约验收（`-f json`）

### 6.1 stdout 纯净

```bash
stune platforms -f json | jq . > /dev/null && echo "stdout clean"
stune params ap --groups -f json 2>/dev/null | jq -e '.command=="params.groups"'
```

**MUST**：`jq` 解析成功 —— 即 stdout 只有 JSON，人类可读输出全在 stderr。
**MUST**：`2>/dev/null` 后仍是完整 JSON。

### 6.2 信封字段

```bash
stune platforms -f json | jq 'keys'
```

**MUST**：含 `schema_version` / `tool` / `command` / `status`（`generated_at` 可被
`SMARTTUNE_DETERMINISTIC=1` 抹掉）。

```bash
SMARTTUNE_DETERMINISTIC=1 stune platforms -f json > /tmp/a.json
SMARTTUNE_DETERMINISTIC=1 stune platforms -f json > /tmp/b.json
diff /tmp/a.json /tmp/b.json && echo "byte-identical"
```

**MUST**：两次输出逐字节相同。

### 6.3 失败路径也是 JSON

```bash
stune params NOPE_NOT_A_PARAM -f json | jq '{status, code:.error.code}'; echo "exit=${PIPESTATUS[0]}"
```

**MUST**：`status == "error"`、`error.code` 存在、退出码 1。

### 6.4 真机日志（需要 §1 的日志）

```bash
stune analyze -i "$AP_LOG" -f json | jq '.modules | keys'
stune analyze -i "$AP_LOG" -f json -o /tmp/ap.json && jq -e '.status=="ok"' /tmp/ap.json
stune quality -i "$AP_LOG" -f json | jq '.quality.score'
stune pid -i "$AP_LOG" -f json -a roll | jq '.axes.roll.assessment'
stune fft -i "$AP_LOG" -f json | jq '.vibration_level'
stune filter -i "$AP_LOG" -f json | jq '.config_summary'
stune hardware -i "$AP_LOG" -f json | jq '.firmware_version'
stune analyze -i "$BF_LOG" -f json | jq '.platform'
```

**MUST**：均退出码 0 且 JSON 可解析。
**MUST**：JSON 里没有 `NaN` / `Infinity` 字面量：

```bash
stune analyze -i "$AP_LOG" -f json | grep -E '\b(NaN|Infinity)\b' && echo "FAIL" || echo "strict JSON OK"
```

### 6.5 `--report md` 无 `-o` 的旧 bug

```bash
cd /tmp && rm -f *_report.md
stune analyze -i "$AP_LOG" --report md
ls -la *_report.md          # MUST 生成 <logstem>_report.md（v3.1 前静默什么都不产出）
```

---

## 7. MCP 验收

### 7.1 冒烟脚本（一条命令覆盖契约与 stdout 纯净）

```bash
python tools/smoke_mcp.py                      # 不需要日志的部分
python tools/smoke_mcp.py --log "$AP_LOG"      # 含分析类工具
```

**MUST**：退出码 0，输出 `OK — N/N checks passed`。
脚本本身就是 JSON-RPC 客户端，**任何一行 stdout 杂音都会让它直接失败**（这正是
上次「无响应」故障的检测手段）。

### 7.2 工具清单

**MUST**：`tools/list` 暴露 15 个工具，且全部 `readOnlyHint=True`（脚本已断言）：
`list_platforms` `log_quality` `analyze_log` `analyze_pid` `analyze_fft` `analyze_magfit`
`analyze_sysid` `analyze_filter` `analyze_hardware` `generate_plot`
**`list_param_groups`**（新） `list_params` **`get_param`**（新） `search_params` `validate_param`

### 7.3 返回体大小（上次疑似「无响应」的真凶）

**MUST**：`list_params(platform="ardupilot")` **不带** `group`/`category` 时返回
`ok:false, error_code:"E4000"`，并提示先看组索引 —— 不再一次性吐 ~600KB。
**MUST**：`list_params(group="ATC_")` 响应 < 32 KB，且行内**不含** `description`。
**MUST**：`analyze_log` 响应 < 256 KB。

### 7.4 统一错误形状

**MUST**：任一失败响应含全部五个键：`ok` `error_code` `message` `hint` `retryable`。
**MUST**：确定性失败（找不到参数、日志损坏、数据不足）`retryable == false`；
仅内部异常（`E9999`）为 `true`。
**MUST**：参数值被拒是**成功调用** —— `ok:true` + `valid:false` + `status`，
客户端不应把它当传输错误重试。

### 7.5 接入真实客户端（人工一次）

用 README 里的配置接入 OpenClaw / Claude Desktop，跑一轮：
`list_param_groups → list_params(group=…) → get_param → validate_param → analyze_log`。

**MUST**：无「无响应」/超时；工具描述在客户端里可读且指向正确的工作流。

---

## 8. 懒加载（导入副作用）

```bash
python - <<'PY'
import subprocess, sys, time, json
code = ("import sys, time;"
        "t=time.perf_counter();"
        "from smarttune.platform.params import ParamTable;"
        "ParamTable.from_knowledge('ardupilot');"
        "print(json.dumps({'ms': round((time.perf_counter()-t)*1000,1),"
        "'numpy': 'numpy' in sys.modules}))")
print(subprocess.run([sys.executable, "-c", "import json;" + code],
                     capture_output=True, text=True).stdout)
PY
```

**MUST**：`numpy == false` —— 参数查询链路不再拉起 numpy。
**SHOULD**：`ms` 显著低于 v3.1（参考量级：几十 ms 而非几百 ms）。

```bash
python -c "from smarttune.platform import resolve_adapter; print(resolve_adapter)"
python -c "import smarttune.platform as p; print(sorted(dir(p)))"
```

**MUST**：公开 API 仍可用（PEP 562 `__getattr__` 生效），`dir()` 列出 6 个符号。

---

## 9. 打包验收（v3.1 修的「装完即坏」）

```bash
pip install build && python -m build
python - <<'PY'
import glob, zipfile
whl = sorted(glob.glob("dist/smarttune-3.2.0-*.whl"))[-1]
names = zipfile.ZipFile(whl).namelist()
need = ["smarttune/knowledge/params/ardupilot.json",
        "smarttune/knowledge/params/betaflight.json",
        "smarttune/knowledge/params/px4.json",
        "smarttune/py.typed"]
missing = [n for n in need if n not in names]
print("wheel:", whl)
print("rules json:", sum(1 for n in names if "/knowledge/rules/" in n))
print("MISSING:", missing or "none")
PY
```

**MUST**：`MISSING: none`，且 `rules json > 0`。

```bash
# 干净环境冒烟：装 wheel 而非源码树
python -m venv /tmp/v32 && /tmp/v32/bin/pip install "dist/smarttune-3.2.0-py3-none-any.whl[all,mcp]"
/tmp/v32/bin/stune params --lint            # MUST 退出码 0
/tmp/v32/bin/stune params BATT_MONITOR      # MUST 找到（证明 params/*.json 进了包）
```

---

## 10. 生成器可重现性

```bash
# 各自 clone 到本地后：
python tools/build_param_tables.py ardupilot  ../ParameterRepository/Copter-4.1/apm.pdef.json --out /tmp/ap.json
python tools/build_param_tables.py px4        ../PX4-Autopilot/docs/public/config/failsafe/parameters.json --out /tmp/px4.json
python tools/build_param_tables.py betaflight ../betaflight --out /tmp/bf.json

python - <<'PY'
import json
for name, gen, cur in [("ardupilot","/tmp/ap.json","smarttune/knowledge/params/ardupilot.json"),
                       ("px4","/tmp/px4.json","smarttune/knowledge/params/px4.json"),
                       ("betaflight","/tmp/bf.json","smarttune/knowledge/params/betaflight.json")]:
    a, b = json.load(open(gen)), json.load(open(cur))
    ka = {p["name"] for p in a["parameters"]}; kb = {p["name"] for p in b["parameters"]}
    diff = [p for p in a["parameters"] if p != next((q for q in b["parameters"] if q["name"]==p["name"]), None)]
    print(f"{name}: gen={len(ka)} repo={len(kb)} only_gen={len(ka-kb)} only_repo={len(kb-ka)} field_diffs={len(diff)}")
PY
```

**MUST**：`only_gen` / `only_repo` 为 0（同一上游快照下参数集合一致）。
**SHOULD**：`field_diffs` 为 0；若非 0，逐条核对差异字段并以**生成器输出为准**覆盖仓库表，
然后回到 §4 重跑。`generated` 日期字段差异属正常，可忽略。

> 上游若已更新（例如 Betaflight master 前进），参数集合本就会变 —— 这种情况
> 记录新 commit sha 并接受新表，但仍必须满足 §4.1 的 lint 0 errors。

---

## 11. 分析结果不回归（重要）

本次**不应**改变任何分析数值。用同一份日志对比 v3.1 与 v3.2：

```bash
git stash list   # 确保工作区干净
pip install "git+https://github.com/raylanlin/smarttune-cli.git@v3.1.0" --target /tmp/old31 2>/dev/null || true
# 或：git worktree add /tmp/w31 v3.1.0 && (cd /tmp/w31 && pip install -e .)

stune analyze -i "$AP_LOG" --report md -o /tmp/new.md
# 用 v3.1 生成 /tmp/old.md，然后：
diff /tmp/old.md /tmp/new.md
```

**MUST**：PID/FFT/MagFit 的指标与推荐值**完全一致**（仅允许时间戳、版本号、
参数名展示差异）。若数值有变 → 说明有意外副作用，**停止发版**。

---

## 12. 通过判据与回报格式

**可发版**（全部满足）：

- §2 compileall + ruff 无 error
- §3 `pytest -q` 全绿、无 skip
- §4.1 三张表 lint 0 errors、数量与表格一致
- §4.2 六条旧缺陷回归全部符合预期
- §4.3 校验闸门 9 条退出码 + status 全对
- §4.4 组/搜索 9 条全对
- §6 JSON 契约（纯净 / 信封 / 确定性 / 失败也是 JSON / 无 NaN）
- §7.1 `smoke_mcp.py` 退出码 0，§7.3 三条大小上限满足
- §8 `numpy == false`
- §9 wheel 含 `params/*.json`，干净环境 `--lint` 通过
- §11 分析数值零变化

失败回报请给出：

```
FAIL <章节号> <命令原文>
期望: <本文档写的期望>
实际: <完整输出，含退出码>
环境: python <版本> / OS / pip list | grep -E "numpy|scipy|click|rich|mcp|pymavlink|pyulog"
```

---

## 13. 已知限制（**不是** bug，不要按缺陷回报）

1. **ArduPilot / Betaflight `default` 为 `null`** —— 上游元数据不发布默认值
   （AP 的默认值随载具/板子变化；BF 的默认值在各 PG 的 `pgResetTemplate` 里）。
   `null` 表示 unknown，比编一个 `0.0` 诚实。PX4 1908 个全部有真默认值。
2. **Betaflight 769 个参数没有描述** —— 固件源码里就没有描述文本。已保留
   SmartTune 自己整理的 45 条；其余为空，并在表级 notes 里写明。
3. **Betaflight 13 个枚举没有成员表** —— 这些 lookup 数组定义在 `cli/settings.c`
   之外（如 `TABLE_DEBUG` → `debugModeNames`）。已在 `unresolved_ref` 字段如实标注，
   `validate()` 对它们返回 `status="unverifiable"`（fail-closed，不放行）。
4. **Betaflight ~160 个数值上界为 `null`** —— 上界是无法从已扫描头文件解析的宏
   （多为 OSD 位置类 `OSD_POSCFG_MAX`）。宁可留 `null` 也不猜。
5. **ArduPilot 参数表基于 `Copter-4.1`** —— ParameterRepository 中 Copter 系列最新的
   `apm.pdef.json` 就是 4.1（4.2+ 只有 XML / Markdown）。要升级到 4.2 需先用
   ParameterRepository 的 `scripts/json_from_xml.py` 生成 pdef.json，再跑 §10。
6. **`suffix_collision` 警告不为零** —— `ANGLE_MAX` / `PSC_ANGLE_MAX`、
   `SIMPLE` / `SUPER_SIMPLE` 是两组真实存在的不同参数，规则按 warn 处理，需人工判断。
7. **`stune analyze -f json` 忽略 `--visual`** —— json 模式不产图，会在 stderr 提示。
8. **MCP 保留 legacy `error` 键** —— 与新的 `message` 同值，给旧 prompt 一个版本的
   过渡期，计划 v3.3 移除。

---

## 14. 发版清单（验收全过之后）

```bash
git add -A && git commit -m "v3.2.0: rebuild parameter tables from upstream metadata; fix enum validation gate; slim MCP payloads"
git tag -a v3.2.0 -m "SmartTune v3.2.0"
git push origin release/v3.2 --tags        # 或按你的分支策略合并到 main 后打 tag
```

发版说明直接取 `CHANGELOG.md` 的 v3.2.0 段落。
若同时发 PyPI：先 §9 的 `python -m build`，再 `twine upload dist/*`。
