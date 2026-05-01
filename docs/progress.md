# IO Ring Simulation Framework — Progress & Reference

> Updated: 2026-05-01

---

## Overall Architecture

```
Step 1: Symbol Generation (TSG + Redistribute)
Step 2: Testbench Build (place DUT + sources/loads)
Step 3: Simulation Run (ADE / netlist export)
Step 4: Result Verification (measure + compare)
```

Current status: **Steps 1-2 complete**, Step 3-4 planned (see `docs/sim_run_plan.md`).

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
- `ssgSortPins = "geometric"` preserves schematic spatial layout
- CDF auto-propagates from schematic to symbol — no `cdfCopyCDF` needed
- Verified on IO_RING_12x12 (66 terminals, 68 pin figures)

### 1.2 Symbol Redistribute — DONE (rearchitected 2026-04-30, updated 2026-05-01)

3-step clean architecture replaces old fragile flow:

| Step | File | Purpose |
|------|------|---------|
| Extract | `skill_code/extract_symbol_info.il` | Reusable SKILL: dump all rects/lines/labels/terminals in one call |
| Calculate | `symbol_layout_engine.py` | Pure Python: classify pins, compute body dims, calculate positions |
| Apply | `symbol_layout_engine.py::generate_apply_skill()` | Generate one SKILL script that applies all changes |

**Orchestrator**: `symbol_redistribute.py` — 5 network round-trips (down from 3+N+ceil(N/22)).

#### 1.2.1 Multi-pin extraction (2026-05-01)

Problem: VSS and VSSIB each have **2 physical pin figures** (2 pin objects per terminal) but old extraction only dumped `car(term~>pins)` — losing the second pin entirely.

Fix: extraction now iterates ALL pins per terminal with pin sub-index:
```skill
TERM|terminal_index|pin_index|name|direction|cx|cy
```
- Terminals with 1 pin: `TERM|0|0|VREFDES|inputOutput|0|-1.5`
- Terminals with 2 pins (VSS): `TERM|53|0|VSS|...` and `TERM|53|1|VSS|...`
- IO_RING_12x12: 66 terminals → **68 pin figures** extracted

Apply SKILL uses `nth(pin_idx, term~>pins)` to access each figure independently.

#### 1.2.2 CORE signal handling (2026-04-30)

`*_CORE` signals (e.g., `D0_CORE`, `SYNC_CORE`) are moved to the **inner rect** at their base signal's Y/X position. CORE pins:
- Do NOT participate in outer distribution (body sized by non-CORE count only)
- Pin figure moved to inner rect edge at base signal's position
- Wire and label deleted

Non-CORE signals without a `_CORE` counterpart keep normal outer pin+wire+label only.

#### 1.2.3 Duplicate label handling (2026-05-01)

VSS and VSSIB have 2 labels each (one per physical pin). Labels are assigned to pin figures using **consumed-label tracking**:
1. For each pin figure, find the closest label by original position
2. Once matched, mark that label as consumed — no other pin can claim it
3. SKILL uses position-based matching (`setof` with `abs(xy - orig) < 0.01`) for precise label targeting

#### 1.2.4 Outer rect layer (2026-04-30)

Both outer and inner body rects use `device/drawing` layer (was `instance/drawing` for outer). Old `instance/drawing` rects are also deleted during cleanup for backward compat.

#### 1.2.5 Wire geometry

Wires extend OUTWARD from body — left pins go left, right pins go right, top up, bottom down. Pin at far end of wire, label just inside outer rect (`label_inset` from edge).

#### Tunable parameters (`LayoutConfig`)

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `pin_pitch` | 0.5 | Pin-to-pin spacing |
| `body_margin` | 1.25 | Inset from outer to inner rect |
| `wire_length` | 0.375 | Wire stub length outward from outer edge |
| `end_margin` | 2.0 | Reserved space at both ends of each side |
| `label_inset` | 0.125 | Label offset inside outer rect |
| `center_x/y` | (2.5, -0.5) | Symbol center |

#### SKILL extraction output format (pipe-delimited)

```
RECT|layer|purpose|L|B|R|T
LINE|layer|purpose|x1|y1|x2|y2
LABEL|layer|purpose|text|x|y
TERM|term_index|pin_index|name|direction|cx|cy
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

## Step 3: Simulation Run — IMPLEMENTED

ADE is blocked. Alternative path: **export netlist via `si` batch → run spectre from Python**.

### Pipeline

```
3a. export_netlist()   — simInitEnvWithArgs + si -batch -command nl (no ADE)     ✅ sim_run.py
3b. build_sim_deck()   — append model include + analysis + options to netlist     ✅ sim_deck_template.py
3c. run_spectre()      — SpectreSimulator.from_env().run_simulation()             ✅ sim_run.py
3d. parse_results()    — extract measurements from PSF data                        ✅ sim_run.py
```

### Implementation

| File | Purpose | Status |
|------|---------|--------|
| `src/sim_deck_template.py` | SimConfig dataclass + `build_sim_deck()` | ✅ Done |
| `src/sim_run.py` | `export_netlist()` + `run_spectre()` + `parse_results()` + orchestrator | ✅ Done |
| `src/sim_verify.py` | Golden mapping + Tolerance + verification report | ✅ Done |
| `src/sim_flow.py` | Integrated `run_sim=True` option in `run_sim_flow()` | ✅ Done |

### Usage

```python
# TB build only (Steps 1-4)
result = run_sim_flow("LLM_Layout_Design_Lab", "IO_RING_12x12")

# TB build + simulation (Steps 1-5)
result = run_sim_flow(
    "LLM_Layout_Design_Lab", "IO_RING_12x12",
    run_sim=True,
    model_include="/path/to/PDK/toplevel.scs",
)

# Standalone sim run (after TB already exists)
from sim_run import run_sim_run
from sim_deck_template import SimConfig
cfg = SimConfig(model_include="/path/to/PDK/toplevel.scs")
sim_result = run_sim_run("LLM_Layout_Design_Lab", "IO_RING_12x12_tb", pins, run_dir, config=cfg)
```

### Golden mapping (from sim_verify.py)

| pad_type | vmax_min | vmin_max |
|----------|----------|----------|
| digital_input | 0.9 * VDD | 0.1 * VDD |
| digital_output | 0.9 * VDD | 0.1 * VDD |
| clock | 0.9 * VDD | 0.1 * VDD |
| power | 0.99 * VDD | 1.01 * VDD |

Tolerance: voltage ±50mV or 3%, delay ±1ns or 10%, current 20%.

### Open questions

1. PDK model path on remote — `VB_PDK_SPECTRE_INCLUDE` in `.env` (configurable via SimConfig)
2. `cds.lib` path for si — configurable via `cds_lib` param
3. IO pad cell spectre models — behavioral? SPICE-only?
4. PSF signal naming → pin name mapping — fuzzy matching implemented in `parse_results()`

---

## Step 4: Verification — IMPLEMENTED

Integrated into `sim_verify.py`. Generates per-pin PASS/FAIL report with:
- Golden mapping per pad type (voltage thresholds based on VDD)
- Tolerance rules (absolute + relative)
- Overall verdict: PASS / FAIL / INCOMPLETE

Report saved as `verify.json` in the run directory.

Automatically triggered when `run_sim=True` in `run_sim_flow()`, or standalone:
```python
from sim_verify import verify_results
report = verify_results(measurements, vdd=1.8, cell="IO_RING_12x12_tb")
```

---

## Changelog

### 2026-05-01: Step 3-4 implementation (sim run + verify)

**New files**:
- `src/sim_deck_template.py`: SimConfig dataclass + `build_sim_deck()` — appends model include, options, analysis, save commands to si netlist
- `src/sim_run.py`: Full sim pipeline — `export_netlist()` (si batch), `build_deck()`, `run_spectre()` (SpectreSimulator wrapper), `parse_results()` (measurement extraction with PSF→pin mapping), `run_sim_run()` orchestrator
- `src/sim_verify.py`: Golden mapping per pad type, tolerance rules (voltage ±50mV/3%, delay ±1ns/10%, current 20%), per-pin PASS/FAIL verification report

**Modified**:
- `src/sim_flow.py`: Added `run_sim=True` option to `run_sim_flow()` — triggers netlist export → spectre → verify after TB build. Added `sim_run_ok`/`sim_verdict` to `SimFlowResult`. Added `write_pin_info_json()` output for LLM classification.

### 2026-05-01: Multi-pin extraction + duplicate label fix

**Problem**: VSS and VSSIB each have 2 physical pin figures (2 pin objects per terminal). Old extraction only dumped `car(term~>pins)`, losing the second pin. After apply, both labels overlapped at the same position instead of staying separate.

**Changes**:
- `extract_symbol_info.il`: iterate ALL pins per terminal, output `TERM|term_idx|pin_idx|name|dir|cx|cy` (7 fields, was 6)
- `TermData`: added `pin_index` field
- `PinLayout`: added `pin_index` field
- `_calc_pin_layouts()`: added consumed-label tracking (`claim_label()`) — each label matched to at most one pin figure, preventing overlap
- `generate_apply_skill()`: uses `nth(pin_idx, term~>pins)` instead of `car(term~>pins)` for per-figure access
- Label matching reverted to position-based `car(setof(...position...))` (precise per-label targeting) instead of `foreach(setof(...name...))` (moves all same-name labels to same spot)
- `parse_symbol_info()`: parses 7-field TERM records

**Verified**: IO_RING_12x12 — 68 pin figures (66 + VSS extra + VSSIB extra), VSS/VSSIB each have 2 independent pins and 2 independent labels at separate positions.

### 2026-04-30: Symbol redistribute rearchitecture

**Problem**: Old flow had ~3+N+ceil(N/22) SKILL round-trips, fragile proximity-based shape search, and interleaved logic split between Python and SKILL.

**Changes**:
- New 3-step architecture: extract once → calculate in Python → apply in one shot
- `extract_symbol_info.il`: reusable pipe-delimited extractor (replaces `extract_symbol_pins.il`)
- `symbol_layout_engine.py`: pure Python layout engine with `LayoutEngine` class, `generate_apply_skill()` for SKILL generation
- `symbol_redistribute.py`: 5-round-trip orchestrator
- CORE signal handling: `*_CORE` pins moved to inner rect, wire+label deleted
- Outer rect changed from `instance/drawing` to `device/drawing`
- Terminal access via `nth(index, cv~>terminals)` (replaces name-based `setof`)
- Archived old skill scripts to `archive/`

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
| Outer body | `device/drawing` | rect | `bBox` |
| Inner body | `device/drawing` | rect | `bBox` |
| Pin figure | `pin/drawing` | rect | `bBox` |
| Wire stub | `device/drawing` | line | `points` |
| Pin label | `pin/label` | label | `xy` |

### SKILL Pitfalls

| Pitfall | Details |
|---------|---------|
| `t` is protected | SKILL boolean `true` — never use as variable name (use `trm` instead) |
| `return(nil)` in `let` | Must use explicit `return()` — bare `nil` doesn't exit |
| String output wrapping | Bridge returns SKILL strings wrapped in `"quotes"` with `\n` escapes — strip quotes, unescape |
| `load()` parse errors | Large inline SKILL fails via `execute_skill()` — use `load_il()` pattern instead |
| Duplicate terminals | `setof` by name grabs first match only — use `nth(index, cv~>terminals)` for reliable access |

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
SIM-IO/
├── src/
│   ├── sim_flow.py                  # Testbench build flow (Steps 1-4) + optional sim run
│   ├── sim_run.py                   # Netlist export → spectre → parse (Step 3a-d)
│   ├── sim_deck_template.py         # SimConfig + deck builder
│   ├── sim_verify.py                # Golden mapping + tolerance verification (Step 4)
│   ├── pin_types.py                 # Pin classification, PAD_RULES, LLM loader
│   ├── symbol_layout_engine.py      # Pure Python: data structs + layout calc + SKILL gen
│   ├── symbol_redistribute.py       # Orchestrator: TSG → extract → calculate → apply
│   └── bridge/edit_patterns.py      # Bridge call patterns
├── docs/
│   ├── progress.md                  # This file
│   └── sim_run_plan.md              # Step 3-4 plan: netlist → spectre → verify
├── skill_code/
│   ├── extract_symbol_info.il       # Reusable SKILL extractor (all pin figures, pipe-delimited)
│   ├── symbol_move_pin.il           # Single-pin move (used by sim_flow.py)
│   ├── set_inst_params.il           # Generic CDF param setter
│   └── schematic_edit_procs.il      # Schematic edit utilities
└── archive/
    ├── extract_symbol_pins.il       # Old extractor (replaced by extract_symbol_info.il)
    ├── symbol_rebuild_body.il       # Old body rebuild (replaced by generated SKILL)
    ├── build_symbol.il/v2/v3        # Early symbol creation experiments
    └── test_*.il                    # Test scripts
```

---

## TODO

1. ~~Symbol generation~~ → DONE (TSG + redistribute)
2. ~~Testbench build~~ → DONE (sim_flow.py)
3. ~~Step 3a: `export_netlist()`~~ → DONE (sim_run.py)
4. ~~Step 3b: `build_sim_deck()`~~ → DONE (sim_deck_template.py)
5. ~~Step 3c: `run_spectre()`~~ → DONE (sim_run.py)
6. ~~Step 3d: `parse_results()`~~ → DONE (sim_run.py)
7. ~~Step 4: `sim_verify.py`~~ → DONE
8. **End-to-end test** — run full pipeline on a known cell with spectre
9. **Iterate layout rules** — tune `LayoutConfig` params for different designs
10. **PDK model path** — resolve `VB_PDK_SPECTRE_INCLUDE` on remote
11. **PSF signal naming** — validate fuzzy mapping on real spectre output
