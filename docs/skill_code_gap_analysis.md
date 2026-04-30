# SKILL Code Gap Analysis — IO Ring Simulation Framework

> Generated: 2026-04-30
> Updated: 2026-04-30 — Project restructured; S2/S3 resolved; verified edit ops; symbol code obtained externally
> Context: Simulation framework needs SKILL code to go from "existing schematic → symbol → testbench → simulation → verification"
> Key insight: Virtuoso's built-in symbol generation (from schematic) auto-handles CDF. The real need is editing existing schematics to add simulation sources.
> Experiment cell: `LLM_Layout_Design_Lab/skill_lab` (schematic + symbol views created)

---

## CRITICAL BUG: Cellview Overwrite (mode="w")

All existing code opens cellviews in **write mode ("w")**, which **creates a new blank cellview and destroys existing content**. This makes incremental editing (adding simulation sources, testbench wiring) impossible.

### Affected locations

| File | Line | Code | Effect |
|------|------|------|--------|
| `io_ring/bridge/client.py` | 236 | `mode: str = "w"` | Default overwrites cellview |
| `skill_code/create_io_ring_lib_full.il` | 13 | `dbOpenCellViewByType(... "w")` | Creates fresh, destroys existing |
| `skill_code/create_schematic_cv.il` | 6 | `dbOpenCellViewByType(... "w")` | Same |
| `skill_code/helper_based_device_T28.il` | 3 | `dbOpenCellViewByType(... "w")` | Same |
| `scripts/run_il_with_screenshot.py` | 89 | `mode="w"` | Same |

### Virtuoso `dbOpenCellViewByType` modes

| Mode | Meaning | Use Case |
|------|---------|----------|
| **"r"** | Read-only | View without modifying |
| **"a"** | Append/edit | Open existing for editing, **preserves content**; creates new if not exists |
| **"w"** | Write (overwrite) | Create new cellview, **destroys existing content** |

### Fix required

- Initial io-ring generation (schematic + layout from scratch) → `"w"` is correct
- Symbol generation from existing schematic → must use `"a"`
- Testbench editing (add sources, wires, nets to existing schematic) → must use `"a"`
- Opening an existing schematic for viewing/modification → must use `"a"`

**Action**: `client.py` default should change to `"a"`, and callers that need fresh creation should explicitly pass `"w"`.

---

## Existing SKILL Code (Already Available)

### `skill_code/` directory
| File | Capability |
|------|-----------|
| `create_io_ring_lib_full.il` | Create library + schematic cellview |
| `create_schematic_cv.il` | Open existing schematic (hardcoded to IO_RING_LIB) |
| `get_cellview_info.il` | Get current cellview info |
| `screenshot.il` | Take window screenshot |
| `helper_based_device_T28.il` | Extract device template info for T28 |

### Python bridge already wraps
`dbCreateInst`, `dbCreateParamInstByMasterName`, `dbOpenCellView`, `dbOpenCellViewByType`, `dbSave`, `schCreateWire`, `schCreateWireLabel`, `schCreatePin`, `schCheck`, `geOpen`, `geGetWindowCellView`, `dbCreatePath`, `dbCreateLabel`, `dbCreateVia`, `techGetTechFile`, `hiRedraw`, `hiZoomAbsoluteScale`, `ddGetObjReadPath`

### `symbo_skills_log.txt` partial capture
`schHiViewToView()`, `hiiSetCurrentForm('schViewToViewForm)`, `hiFormDone(schViewToViewForm)`, delete cellview

---

## What's NOT Needed (Already Covered by Existing io-ring Skills)

- Schematic building — already done by existing T28 io-ring skills
- Opening schematics — already have `create_schematic_cv.il`
- Creating library/cellview — already have `create_io_ring_lib_full.il`
- Placing device instances in schematic — already handled by `generator.py`
- Layout operations — already handled by `layout/skill_generator.py`

---

## Verified Edit Operations (Experiment on skill_lab)

以下操作已在 `LLM_Layout_Design_Lab/skill_lab` 上通过 bridge 实际验证通过。
详细 API 参考: `Sim_IO/skill_code/schematic_edit_reference.il`, `Sim_IO/bridge/edit_patterns.py`

| 操作 | SKILL API | 验证状态 | 关键发现 |
|------|-----------|---------|----------|
| 打开 cellview (edit) | `dbOpenCellViewByType(... "a")` | ✅ | mode="a" 保留内容，mode="w" 清空 |
| 放置 instance | `dbCreateInst(cv master name xy orient)` | ✅ | master 获取不要加 viewType: `dbOpenCellViewByType("lib" "cell" "symbol")` |
| 设置 CDF 参数 | `cdfGetInstCDF(inst)~>parameters` | ✅ | 找到 param 后设 `param~>value = "1.8"` |
| 创建 net | `dbCreateNet(cv "name")` | ✅ | |
| 创建 wire | `schCreateWire(cv "route" "full" points 0 0 0 nil nil)` | ✅ | 9 个参数，points 格式 `list(list(x1 y1) list(x2 y2))` |
| 创建 wire label | `schCreateWireLabel(cv nil xy text align "0" "stick" 0.0625 nil)` | ✅ | |
| 创建 pin | `schCreatePin(cv nil name dir nil xy orient)` | ✅ | |
| 保存 | `dbSave(cv)` | ✅ | |
| **Symbol 生成 (TSG)** | `schSchemToPinList` + `schPinListToSymbol` | ✅ | 见下方详细说明 |

### Symbol 生成：TSG (Text-to-Symbol Generator) API

> 来源: `virtuoso-bridge-lite/examples/01_virtuoso/symbol/`，已复制到 `Sim_IO/skill_code/`

**核心两步调用（headless-safe，无需 GUI 交互）：**

```python
# 设置 pin 排序方式（geometric = 按 schematic 位置排列）
client.execute_skill('schSetEnv("ssgSortPins" "geometric")')

# TSG 管线
client.execute_skill(
    'let((pl) '
    f'pl = schSchemToPinList("{lib}" "{cell}" "schematic") '
    f'schPinListToSymbol("{lib}" "{cell}" "symbol" pl))'
)
```

**关键要点：**
1. 使用低层 `schSchemToPinList` + `schPinListToSymbol` 而非 `schHiViewToView()`（后者依赖 GUI form，headless 下会超时）
2. `ssgSortPins` 设为 `"geometric"` 保持 schematic 中的空间布局（IN 左, OUT 右, VSS 底），默认 `"alphanumeric"` 按字母排序
3. CDF 自动从 schematic 传播到 symbol，无需 `cdfCopyCDF`
4. 参考: `01_rc_create_with_symbol.py`（3-pin RC 滤波器）, `02_bus10_create_with_symbol.py`（20-pin bus）

### 高层 API：`virtuoso_bridge.virtuoso.schematic.ops`

virtuoso-bridge-lite 已封装了高层操作，比我们手写的 rb_exec 更健壮：

| 函数 | 用途 | 对比手写 |
|------|------|---------|
| `schematic_create_inst_by_master_name(lib, cell, view, name, x, y, orient)` | 放置实例 | 自动处理 master 打开和 let 绑定 |
| `schematic_create_pin_at_instance_term(inst, term, pin_name, direction)` | 在实例端子上放 pin | 自动计算端子中心坐标 |
| `schematic_create_wire_between_instance_terms(from_inst, from_term, to_inst, to_term)` | 连线 | 自动计算两端坐标 |
| `schematic_create_wire(points)` | 按坐标连线 | 与手写 `schCreateWire` 相同 |
| `schematic_label_instance_term(inst, term, label)` | 标注端子 | 自动定位 |

**SchematicEditor 上下文管理器：**
```python
with client.schematic.edit(lib, cell) as sch:
    sch.add(inst("analogLib", "res", "symbol", "R0", 0.5, 0.0, "R90"))
    sch.add(wire("R0", "MINUS", "C0", "PLUS"))
    sch.add(pin_at("R0", "PLUS", "IN", direction="input"))
    # 退出时自动 schCheck + dbSave
```

### Bridge 调用关键约束

1. **rb_exec 包装为 let 块** — 只能返回单个表达式，多语句需用 .il 文件
2. **analogLib symbol 不加 viewType** — `dbOpenCellViewByType("analogLib" "vdc" "symbol")` 而非 `("analogLib" "vdc" "symbol" "symbol" "r")`
3. **Symbol bBox 格式** — `list(list(x1 y1) list(x2 y2))` 不是 `list(x1 y1 x2 y2)`
4. **Symbol 图层** — body: `instance/drawing`, pin: `pin/drawing`, label: `device/drawing`

---

## Missing SKILL Code — Must Gather from CIW

### Priority 1: Symbol Generation (Framework Step 1)

> **Key insight**: Virtuoso's built-in "Create → Cellview → From Symbol" auto-handles CDF propagation.
> Symbol generation SKILL code 已从其他渠道获取，此优先级已解决。

| # | Operation | SKILL APIs to capture | CIW Steps | Status |
|---|-----------|----------------------|-----------|--------|
| S1 | Open existing schematic (parameterized, mode="a") | `geOpen(lib cell view)` or `dbOpenCellViewByType(... "a")` | No CIW capture needed — just parameterize existing `create_schematic_cv.il` and change mode to "a" | TODO (parameterize + fix mode) |
| S2 | Generate symbol from schematic | — | 已从其他渠道获取 SKILL code | ✅ RESOLVED |
| S3 | ~~CDF copy~~ | Not needed — Virtuoso auto-propagates CDF | N/A | ✅ RESOLVED (auto-handled) |
| S4 | Validate CDF on symbol (optional check) | `cdfGetCellCDF`, compare with schematic CDF | Verification only | TODO (low priority) |
| S5 | Delete a cellview | `ddDeleteCellView` | Partially captured in log — wrap into reusable `.il` | TODO (wrap existing log) |

### Priority 2: Testbench Building — Edit Existing Schematic (Framework Step 2)

> **Key insight**: Testbench is built by EDITING an existing schematic (adding sources, nets, wires).
> Must use mode="a" to preserve existing content. The cellview overwrite bug (above) must be fixed first.

| # | Operation | SKILL APIs to capture | CIW Steps | Status |
|---|-----------|----------------------|-----------|--------|
| T1 | Open existing schematic for editing (mode="a") | `dbOpenCellViewByType(lib cell "schematic" "schematic" "a")` | No CIW capture needed — fix mode in existing code | TODO (fix mode) |
| T2 | Place analogLib instances (VDC, VPULSE, CAP, RES, VPWL, VSIN) | `dbCreateInst` with analogLib cells + CDF parameter setting | Place each stimulus type in schematic → set properties (vdc=1.8, period=100n, c=10p, etc.) → capture both placement and parameter-setting calls | TODO (CIW capture) |
| T3 | Create nets and wires in testbench | `dbCreateNet`, `schCreateWire`, connection logic | Connect DUT symbol pins to stimulus pins → capture | TODO (CIW capture) |
| T4 | Add global nets (VDD, VSS) | `dbCreateNet` with `isGlobal=t` | Wire power/ground → capture | TODO (CIW capture) |

### Priority 3: Simulation Setup & Run (Framework Steps 2-3)

| # | Operation | SKILL APIs to capture | CIW Steps | Status |
|---|-----------|----------------------|-----------|--------|
| R1 | ADE-L session setup + configure analysis | `sevOpenSession`, `sevCreateAnalysis`, `sevAddOutput`, `sevSetSimulator` | Set up ADE-L manually (simulator=spectre, add tran/dc analysis, add outputs) with CIW log on | TODO (CIW capture) |
| R2 | Run simulation | `sevRun` | Run from ADE-L → capture | TODO (CIW capture) |
| R3 | OCEAN alternative (may be cleaner than ADE-L) | `openResults`, `getData`, `ocnPrint`, `run()` | Record an OCEAN session | TODO (CIW capture) |

### Priority 4: Results Collection (Framework Steps 3-4)

| # | Operation | SKILL APIs to capture | CIW Steps | Status |
|---|-----------|----------------------|-----------|--------|
| D1 | Read simulation results | `openResults`, `getData`, `getResults` | OCEAN approach | TODO (CIW capture) |
| D2 | Export waveform data to CSV | `ocnPrint(?output "file.csv" ...)` | OCEAN CSV export | TODO (CIW capture) |
| D3 | Run measurements (delay, slew, etc.) | `ocnEvalY`, spectre measurement expressions | OCEAN measurement | TODO (CIW capture) |

---

## Minimum CIW Captures to Unblock Full Pipeline

S2 (Symbol) 已解决，剩余 2 个关键抓取：

1. ~~**S2** — Symbol from schematic~~ → ✅ RESOLVED
2. **T2** — analogLib instance placement + parameter setting → unlocks Step 2
3. **R1** or **R3** — Simulation setup & run (ADE-L or OCEAN) → unlocks Steps 3-4

---

## CIW Capture Procedure

```
hiSetCIWLogLevel(3)
```

Perform the operation manually. The CIW log will show the underlying SKILL calls. Save each captured sequence as a `.il` file and pass to the AI for parameterization.
