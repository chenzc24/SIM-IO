# IO Ring Simulation Framework — Progress & Reference

> Updated: 2026-04-30

---

## Overall Architecture

```
Step 1: Symbol Generation (TSG + Redistribute)
Step 2: Testbench Build (place DUT + sources/loads)
Step 3: Simulation Run (ADE / netlist export)
Step 4: Result Verification (measure + compare)
```

Current status: **Steps 1-2 complete**, Step 3+4 deferred (no ADE permission).

---

## Step 1: Symbol Generation — DONE

### 1.1 TSG Pipeline

Two-call headless SKILL pipeline — no GUI dependency:

```python
client.execute_skill('schSetEnv("ssgSortPins" "geometric")')
client.execute_skill(
    f'let((pl) '
    f'pl = schSchemToPinList("{lib}" "{cell}" "schematic") '
    f'schPinListToSymbol("{lib}" "{cell}" "symbol" pl))'
)
```

Key points:
- `ssgSortPins = "geometric"` preserves schematic spatial layout (IN left, OUT right, VSS bottom)
- CDF auto-propagates from schematic to symbol — no `cdfCopyCDF` needed
- Verified on IO_RING_12x12 (66 terminals)

### 1.2 Symbol Redistribute — DONE (rearchitected 2026-04-30)

3-step clean architecture replaces old fragile flow:

| Step | File | Purpose |
|------|------|---------|
| Extract | `skill_code/extract_symbol_info.il` | Reusable SKILL: dump all rects/lines/labels/terminals in one call |
| Calculate | `symbol_layout_engine.py` | Pure Python: classify pins, compute body dims, calculate positions |
| Apply | `symbol_layout_engine.py::generate_apply_skill()` | Generate one SKILL script that applies all changes |

**Orchestrator**: `symbol_redistribute.py` — 5 network round-trips (down from 3+N+ceil(N/22)).

**Wire direction fix**: wires extend OUTWARD from body — left pins go left, right pins go right, top up, bottom down. Pin movement uses structural `terminal->pins->figs` path (100% reliable, replaces fragile proximity search).

**Tunable parameters** (`LayoutConfig`):
- `pin_margin = 0.075` — gap from outer body edge to pin center
- `body_margin = 0.125` — inset from outer to inner body rect
- `pin_pitch = 0.125` — preferred pin-to-pin spacing
- `wire_length = 0.375` — wire stub length outward from pin
- `center_x/y = (2.5, -0.5)` — symbol center

**SKILL extraction output format** (pipe-delimited):
```
RECT|layer|purpose|L|B|R|T
LINE|layer|purpose|x1|y1|x2|y2
LABEL|layer|purpose|text|x|y
TERM|name|direction|pinCx|pinCy
```

---

## Step 2: Testbench Build — DONE

Implemented in `sim_flow.py`. Full pipeline:

```
Step 2a: Create _tb cellview (dbOpenCellViewByType mode="w")
Step 2b: Place DUT symbol instance (dbCreateInst)
Step 2c: Extract DUT pin info (terminal names, directions, positions)
Step 2d: Add wire labels on DUT pins (label-based wiring)
Step 2e: Place sources/loads by pin type (analogLib instances)
```

### Pin Classification Rules

| Name heuristic | Direction | pad_type | Source | Load |
|---------------|-----------|----------|--------|------|
| VDD/VCC/DVDD/AVDD | any | power | VDC (vdc=VDD) | — |
| VSS/GND/DVSS/AVSS | any | ground | — | — (reference) |
| — | input | digital_input | VPULSE (v1=0, v2=VDD) | — |
| — | output | digital_output | — | CAP (c=10p) |
| — | inputOutput | digital_bidirectional | VPULSE | CAP |

### Wiring Pattern

Label-based wiring: same net name on DUT pin and source/load terminal → Virtuoso auto-connects. Reference terminals tied to `gnd!` (global ground).

### Source/Load Placement

- Sources/loads placed 2.5um outward from DUT pin position
- Side config: wire extend direction + label offset + alignment per side
- CDF params set via `skill_code/set_inst_params.il`

---

## Step 3: Simulation Run — DEFERRED

Blocked by ADE permission. Alternative path: export netlist from _tb schematic → run spectre externally.

Required SKILL captures (CIW):
- ADE-L session setup: `sevOpenSession`, `sevCreateAnalysis`
- OCEAN alternative: `openResults`, `getData`, `ocnPrint`, `run()`

---

## Step 4: Verification — DEFERRED

Depends on Step 3. Design exists in framework doc: golden mapping, tolerance rules, failure signatures.

---

## Schematic Edit API Reference

### Cellview Modes

| Mode | Meaning | Use Case |
|------|---------|----------|
| `"r"` | Read-only | View without modifying |
| `"a"` | Append/edit | Open existing for editing, **preserves content** |
| `"w"` | Write (overwrite) | Create new cellview, **destroys existing** |

### Key SKILL APIs (all verified)

| Operation | SKILL | Notes |
|-----------|-------|-------|
| Open cellview (edit) | `dbOpenCellViewByType(lib cell view viewType "a")` | mode="a" preserves content |
| Place instance | `dbCreateInst(cv master name xy orient)` | master: `dbOpenCellViewByType("lib" "cell" "symbol")` — no 4th param! |
| Set CDF param | `car(setof(p cdfGetInstCDF(inst)~>parameters p~>name=="k"))~>value = "v"` | |
| Create net | `dbCreateNet(cv "name")` | |
| Create wire | `schCreateWire(cv "route" "full" points 0 0 0 nil nil)` | 9 params, points = `list(list(x1 y1) list(x2 y2))` |
| Create wire label | `schCreateWireLabel(cv nil xy text align "0" "stick" 0.0625 nil)` | |
| Create pin | `schCreatePin(cv nil name dir nil xy orient)` | |
| Save | `dbSave(cv)` | |

### Symbol Shape Layers

| Element | Layer | Type | Movable property |
|---------|-------|------|------------------|
| Outer body | `instance/drawing` | rect | `bBox` |
| Inner body | `device/drawing` | rect | `bBox` |
| Pin figure | `pin/drawing` | rect | `bBox` |
| Wire stub | `device/drawing` | line | `points` |
| Pin label | `pin/drawing` | label | `xy` |

### Virtuoso-bridge-lite High-Level API

```python
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_create_inst_by_master_name as inst,
    schematic_label_instance_term as label_inst_term,
)

with client.schematic.edit(lib, cell) as sch:
    sch.add(inst("analogLib", "vdc", "symbol", "V0", 0.0, 0.0, "R0"))
    sch.add(label_inst_term("V0", "PLUS", "VDD"))
    # auto schCheck + dbSave on exit
```

---

## File Map

```
SIM_IO/
├── symbol_redistribute.py          # Orchestrator: TSG → extract → calculate → apply
├── symbol_layout_engine.py         # Pure Python: data structs + layout calc + SKILL gen
├── sim_flow.py                     # Testbench build flow (Steps 1-3e)
├── docs/
│   └── progress.md                 # This file
├── skill_code/
│   ├── extract_symbol_info.il      # Reusable SKILL extractor (pipe-delimited output)
│   ├── symbol_move_pin.il          # Single-pin move (used by sim_flow.py)
│   ├── set_inst_params.il          # Generic CDF param setter
│   └── schematic_edit_procs.il     # Schematic edit utilities
└── archive/
    ├── extract_symbol_pins.il      # Old extractor (replaced by extract_symbol_info.il)
    ├── symbol_rebuild_body.il      # Old body rebuild (replaced by generated SKILL)
    ├── build_symbol.il/v2/v3       # Early symbol creation experiments
    └── test_*.il                   # Test scripts
```

---

## TODO

1. ~~Symbol generation~~ → DONE (TSG + redistribute)
2. ~~Testbench build~~ → DONE (sim_flow.py)
3. **Get ADE permission** — unblocks Step 3
4. **Alternative: netlist export** — export netlist from _tb, run spectre externally
5. **Iterate layout rules** — tune `LayoutConfig` params for different designs
