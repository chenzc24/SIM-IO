# Simulation Build-Up Flow — Gap Analysis

> Date: 2026-04-30
> Based on user-defined 4-step flow

---

## Flow Definition

```
Step 1: Export symbol from primary schematic (TSG)
Step 2: Create _tb cellview (new schematic)
Step 3: In _tb, add DUT symbol + sources/loads via label-based wiring
Step 4: ADE assembler → simulation → results
```

---

## Step-by-Step Gap Analysis

### Step 1: Export Symbol ✅ DONE

| 操作 | 能力 | 状态 |
|------|------|------|
| TSG: `schSchemToPinList` + `schPinListToSymbol` | 已验证 (IO_RING_12x12, 66 terminals) | ✅ |
| Geometric pin sorting | `schSetEnv("ssgSortPins" "geometric")` | ✅ |
| Symbol visual optimization (manual edit) | 需要手动操作或 CIW 抓取 | 📋 TODO (low priority) |

**What we have**: `Sim_IO/skill_code/01_rc_create_with_symbol.py`, `02_bus10_create_with_symbol.py`

---

### Step 2: Create _tb Cellview ✅ CAN DO NOW

| 操作 | 能力 | 状态 |
|------|------|------|
| Create new schematic cellview | `dbOpenCellViewByType(lib, cell+"_tb", "schematic", "schematic", "w")` | ✅ 已验证 |
| Naming convention: `{primary}_tb` | 纯字符串拼接 | ✅ |

**No gap.** This is a straightforward `dbOpenCellViewByType` with mode="w" (fresh create).

---

### Step 3: Build Testbench — PARTIALLY READY

#### 3a: Place DUT symbol as instance ✅

```python
# Place the primary cell's symbol into _tb schematic
dbCreateInst(tb_cv, dbOpenCellViewByType(lib, cell, "symbol"), "DUT", list(0, 0), "R0")
```

Already verified with analogLib. Same API for any symbol.

#### 3b: Extract pin/label info from DUT instance ⚠️ PARTIAL GAP

**What we need:**
- Get all pins from the symbol instance (name, direction, position)
- Get all labels from the symbol instance (text, position)

**What we have:**
- ✅ Terminal names: `inst~>instTerms~>term~>name`
- ✅ Terminal direction: `inst~>instTerms~>term~>direction`
- ❌ Pin position in symbol (transformed to instance coordinates)
- ❌ Label text and position from the symbol

**What's needed:**

```python
# For each pin of the DUT instance, get:
#   - pin name (terminal name)
#   - pin direction (input/output/inputOutput)
#   - pin position (transformed to schematic coordinates)
#   - pin side (left/right/top/bottom — inferred from position relative to DUT center)
```

The `schematic_create_pin_at_instance_term` from virtuoso-bridge-lite already does
coordinate transformation internally via `_schematic_term_center_expr`. We need to
expose this logic to *read* pin positions, not just create pins at them.

**Approach**: Write a SKILL procedure that extracts all pin info from a symbol instance
and returns it as a structured list.

#### 3c: Add sources/loads based on rules ⚠️ NEEDS RULES FROM USER

**What we have:**
- ✅ Place VDC/VPULSE/VSIN/CAP/RES instances (verified)
- ✅ Set CDF parameters on instances (verified)
- ✅ Create wire labels for net naming (verified)
- ✅ `schematic_create_inst_by_master_name` (high-level API)

**What we DON'T have:**
- ❌ **The rules** — which pin type gets which source/load (user will provide)
- ❌ Position calculation — where to place sources relative to DUT pins
- ❌ Label naming convention for wiring

**Label-based wiring approach** (user's preference):
Instead of drawing wires, place `schCreateWireLabel` on the DUT pin and on the
source pin with the SAME net name. Virtuoso auto-connects same-named labels.

```
DUT pin "D0" (input) ← label "NET_D0" → VPULSE "VP0" PLUS ← label "NET_D0"
```

This is much simpler for AI than coordinate-based wire routing.

#### 3d: Wire labels for power/ground ⚠️ NEEDS CONVENTION

- VDD pins → label "VDD" (with `vdd` source elsewhere)
- VSS pins → label "VSS" (with `gnd` symbol)
- Need convention for global vs local supply nets

---

### Step 4: ADE Assembler → Simulation ❌ BLOCKED — NEEDS SKILL CODE FROM USER

| 操作 | 能力 | 状态 |
|------|------|------|
| ADE-L / OCEAN session setup | ❌ Need SKILL code | 🚫 BLOCKED |
| Configure spectre analysis (tran/dc) | ❌ Need SKILL code | 🚫 BLOCKED |
| Add output signals | ❌ Need SKILL code | 🚫 BLOCKED |
| Run simulation | ❌ Need SKILL code | 🚫 BLOCKED |
| Read results / export CSV | ❌ Need SKILL code | 🚫 BLOCKED |

**Entirely blocked** until user provides ADE/Simulation SKILL code and rules.

---

## Summary: Ready vs Blocked

| Step | Status | Blocker |
|------|--------|---------|
| 1. Export symbol | ✅ Done | — |
| 2. Create _tb cellview | ✅ Can do now | — |
| 3a. Place DUT instance | ✅ Can do now | — |
| 3b. Extract pin/label info from DUT | ⚠️ Partial | Need to write pin extraction procedure |
| 3c. Add sources/loads by rules | ⚠️ Partial | ❌ Need **rules** from user |
| 3d. Label-based wiring | ✅ Can do now | Need label naming convention |
| 4. ADE assembler + simulation | ❌ Blocked | ❌ Need **SKILL code** from user |

---

## TODO List (Ordered by Dependency)

1. **Write pin extraction procedure** — extract all pin names/directions/positions from a symbol instance
2. **Get TB building rules from user** — which pin type → which source/load + parameters
3. **Get ADE/Simulation SKILL code from user** — unblocks Step 4
4. **Integrate into end-to-end pipeline** — after rules and sim code are available
5. **Symbol visual optimization** — low priority, manual for now
