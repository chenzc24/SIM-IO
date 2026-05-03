# IO Ring Simulation Framework — Progress & Reference

> Updated: 2026-05-03

---

## Overall Architecture

```
Step 1: Symbol Generation (TSG + Redistribute)
Step 2: Testbench Build (place DUT + sources/loads)
Step 3: Netlist Export (si batch)
Step 4: Simulation Config (LLM / Maestro active.state / legacy)
Step 5: Spectre Run (SpectreSimulator)
Step 6: Result Verification (measure + compare)
```

Current status: **Steps 1-6 implemented**. End-to-end verified on `5T_AMP_dc` (5T amplifier DC+AC analysis).

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

## Step 3: Netlist Export — DONE

Export via `si` batch netlister (no ADE needed).

### Pipeline

```
3a. schCheck + dbSave on _tb schematic
3b. Generate si.env from template (templates/si_spectre.env)
3c. Run si -batch -command nl with user's cds.lib
3d. Download netlist to local run_dir
```

### si.env Template

`templates/si_spectre.env` uses `@PLACEHOLDER@` substitution (same pattern as T28's `si_T28.env`):

```ini
simLibName = "@LIBRARY@"
simCellName = "@TOP_CELL@"
simSimulator = "spectre"
simViewList = '("spectre" "cmos_sch" "schematic" "veriloga")
simStopList = '("spectre")
simNetlistHier = t
simNotIncremental = t
simReNetlistAll = nil
simRunDir = "@SI_RUN_DIR@"
```

Replaces the old hardcoded si.env string and the incomplete `simInitEnvWithArgs()` output.

### Site Configuration

Site-specific paths are loaded from `SIM-IO/.env` via `SiteConfig` (not hardcoded):

| Env Var | Purpose | Example |
|---------|---------|---------|
| `SIM_CDS_LIB` | cds.lib path on remote server | `/home/chenzc_intern25/TSMC28/llm_IO/cds.lib` |
| `SIM_IC_ROOT` | Cadence IC installation root | `/home/cadence/ic618/IC618Hotfix4` |
| `SIM_LM_LICENSE_FILE` | License (optional, auto-discovered from Virtuoso) | `1717@lic_server:5280@thu-han` |
| `SIM_CDS_LIC_FILE` | License (optional, auto-discovered from Virtuoso) | `5280@thu-han` |
| `SIM_PDK_SPECTRE_INCLUDE` | PDK model file on remote (optional) | path to `toplevel.scs` |

License resolution priority: `SIM_*` env var > SKILL `getShellEnvVar()` discovery from Virtuoso.

### Netlist Export — Debugged (2026-05-01, refactored 2026-05-03)

Original 4 issues and their fixes:

| Issue | Original Fix | Current Status |
|-------|-------------|----------------|
| License env vars | SKILL discovery | SiteConfig with SKILL fallback |
| analogLib/basic | Wrapper cds.lib with SOFTINCLUDE | User provides complete cds.lib via `SIM_CDS_LIB` |
| schCheck + dbSave | SKILL call before si | Same — still required |
| Incomplete si.env | Hardcoded Python string | `templates/si_spectre.env` with @PLACEHOLDER@ |

See `docs/netlist_export_findings.md` for full debug log.

---

## Step 4: Simulation Config — DONE

Configuration system with 3-tier priority resolution:

```
Priority 1: LLM-generated sim_config.json (in run_dir)
Priority 2: Maestro active.state XML (parsed into SimDeckConfig)
Priority 3: Legacy SimConfig (backward compatible)
Priority 4: Site defaults (from sim_config.py)
```

### SimDeckConfig Data Model (`sim_config.py`)

Replaces the old flat `SimConfig` with a structured config supporting:

| Field | Type | Purpose |
|-------|------|---------|
| `model_includes` | `list[ModelInclude]` | Multiple include files with sections |
| `analyses` | `list[AnalysisSpec]` | Multiple analyses (dc, tran, ac) with sweeps |
| `design_vars` | `list[DesignVar]` | Design parameters (e.g. `VDD=1.8`) |
| `save_signals` | `list[SaveSignal]` | Specific signals to save |
| `outputs` | `list[OutputExpression]` | Output expressions (e.g. gain = VOUT/(VIP-VIN)) |
| `info_statements` | `list[InfoStatement]` | Info what/where for rawfile output |
| `sim_options` | `SimOptions` | reltol, abstol, temp, etc. |

### LLM Integration (`sim_config.py`)

- `summarize_netlist()` — extracts circuit structure (subckts, instances, pins) from si netlist
- `write_sim_config_input()` — writes `sim_config_input.json` for LLM to read (includes netlist summary, pin classifications, PDK model info, user intent)
- `load_sim_config()` — loads LLM-generated `sim_config.json` into `SimDeckConfig`

### Maestro active.state Parser (`sim_config.py`)

- `parse_active_state()` — parses Maestro's `active.state` XML into `SimDeckConfig`
- Extracts: model includes, design variables, analyses with sweeps, simulator options, save signals, output expressions
- Verified on `5T_AMP_dc` Maestro session

### PDK Constants (TSMC28, hardcoded in `sim_config.py`)

```python
TSMC28_MODEL_FILE = "/home/process/tsmc28n/PDK_mmWave/.../crn28ull_1d8_elk_v1d8_2p2_shrink0d9_embedded_usage.scs"
TSMC28_SECTIONS = ["pre_simu", "noise_worst", "ttmacro_mos_moscap", "tt_res_bip_dio_disres", "tt_mom", "tt_ind_jvar", "tt_r_metal"]
SPECTRE_BIN = "/home/cadence/spectre/SPECTRE211/tools/bin/spectre"
```

### Deck Builder (`sim_deck_template.py`)

Upgraded to support `SimDeckConfig`:

```
1. si netlist (circuit only)
2. global 0
3. parameters VDD=1.8
4. include "path" section=X (multiple)
5. simulatorOptions options ...
6. info what=X where=Y (multiple)
7. Analysis blocks (dc, tran, ac, with sweep params)
8. save signals (specific)
9. saveOptions fallback
```

Legacy `SimConfig` still accepted via `sim_config_from_legacy()` conversion.

---

## Step 5: Spectre Run — DONE

### Pipeline

```
5a. build_deck()      — assemble complete spectre deck from netlist + SimDeckConfig
5b. run_spectre()     — SpectreSimulator.from_env().run_simulation()
5c. parse_results()   — extract measurements from PSF data
```

### SpectreSimulator (from virtuoso-bridge-lite)

`SpectreSimulator.from_env()` handles:
- Upload deck + include files to remote server via SSH
- Source Cadence cshrc for spectre binary
- Run `spectre -64 netlist.scs +escchars -format psfascii`
- Download PSF result directory
- Parse PSF ASCII into `result.data` dict

Requires bridge `.env` vars: `VB_REMOTE_HOST`, `VB_REMOTE_USER`, `VB_LOCAL_PORT`, `VB_REMOTE_PORT`, and optionally `VB_CADENCE_CSHRC`.

### Verified Run: 5T_AMP_dc

Successfully ran DC sweep + AC analysis on a 5T amplifier:

```
output/5T_AMP_dc/
├── netlist          # si-exported circuit netlist
├── active.state     # Maestro session (parsed for config)
├── maestro.sdb      # Maestro session database
├── deck.scs         # Complete spectre deck (107 lines)
└── spectre_results/ # PSF output data
```

Deck includes TSMC28 model includes (7 sections), `parameters VDD=1.8`, DC sweep of VDD 0→3, AC analysis 1→1G Hz, and specific signal saves.

---

## Step 6: Verification — DONE

Integrated into `sim_verify.py`. Generates per-pin PASS/FAIL report with:
- Golden mapping per pad type (voltage thresholds based on VDD)
- Tolerance rules (absolute + relative)
- Overall verdict: PASS / FAIL / INCOMPLETE

### Golden mapping (from sim_verify.py)

| pad_type | vmax_min | vmin_max |
|----------|----------|----------|
| digital_input | 0.9 * VDD | 0.1 * VDD |
| digital_output | 0.9 * VDD | 0.1 * VDD |
| clock | 0.9 * VDD | 0.1 * VDD |
| power | 0.99 * VDD | 1.01 * VDD |

Tolerance: voltage ±50mV or 3%, delay ±1ns or 10%, current 20%.

Report saved as `verify.json` in the run directory.

---

## Changelog

### 2026-05-03: Site config + si.env template + SimDeckConfig refactor

**New files**:
- `src/site_config.py`: `SiteConfig` dataclass — loads `SIM_*` vars from `SIM-IO/.env` (cds_lib, ic_root, PDK model path, license vars). Falls back to SKILL discovery for license vars.
- `src/sim_config.py`: `SimDeckConfig` data model with full simulation config (model includes, analyses with sweeps, design vars, save signals, output expressions, info statements). Includes `parse_active_state()` for Maestro XML, `summarize_netlist()` for LLM input, `resolve_sim_config()` for 3-tier priority resolution.
- `templates/si_spectre.env`: si.env template with `@PLACEHOLDER@` substitution (replaces hardcoded Python string)
- `SIM-IO/.env`: Site configuration file (follows T28 .env pattern)

**Modified**:
- `src/sim_run.py`: Refactored `export_netlist()` to use `SiteConfig` (user provides cds.lib, IC root) and `si_spectre.env` template. Removed hardcoded `_IC_ROOT`/`_SI_BIN` constants, `_LOCAL_CDSLIB_TEMPLATE` wrapper, and inline si.env string. Added step 3a.5: write `sim_config_input.json` for LLM and resolve `SimDeckConfig`. Uses `SPECTRE_BIN` from `sim_config.py`.
- `src/sim_deck_template.py`: Upgraded to support `SimDeckConfig` (multiple model includes, analyses with sweeps, design vars, info statements, save signals). Legacy `SimConfig` preserved for backward compatibility via `sim_config_from_legacy()`.
- `src/sim_flow.py`: Removed `model_include` and `cds_lib` params from `run_sim_flow()` (now in SiteConfig). Added `user_intent` param for LLM simulation config. Passes `SimDeckConfig` through to `run_sim_run()`.

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
│   ├── sim_flow.py                  # Testbench build flow (Steps 1-2) + optional sim run
│   ├── sim_run.py                   # Netlist export → config resolution → spectre → parse
│   ├── sim_config.py                # SimDeckConfig data model + LLM I/O + Maestro parser
│   ├── sim_deck_template.py         # Deck builder (supports SimConfig + SimDeckConfig)
│   ├── sim_verify.py                # Golden mapping + tolerance verification
│   ├── site_config.py               # SiteConfig — loads SIM_* vars from .env
│   ├── pin_types.py                 # Pin classification, PAD_RULES, LLM loader
│   ├── symbol_layout_engine.py      # Pure Python: data structs + layout calc + SKILL gen
│   ├── symbol_redistribute.py       # Orchestrator: TSG → extract → calculate → apply
│   └── bridge/edit_patterns.py      # Bridge call patterns
├── templates/
│   └── si_spectre.env               # si.env template with @PLACEHOLDER@ substitution
├── docs/
│   ├── progress.md                  # This file
│   ├── system_run_guide.md          # Full system run guide (all 5 steps)
│   ├── netlist_export_findings.md   # Netlist export debug findings (4 issues + fixes)
│   └── sim_run_plan.md              # Step 3-4 plan: netlist → spectre → verify
├── skill_code/
│   ├── extract_symbol_info.il       # Reusable SKILL extractor (all pin figures, pipe-delimited)
│   ├── symbol_move_pin.il           # Single-pin move (used by sim_flow.py)
│   ├── set_inst_params.il           # Generic CDF param setter
│   └── schematic_edit_procs.il      # Schematic edit utilities
├── .env                             # Site config (SIM_CDS_LIB, SIM_IC_ROOT, etc.)
├── pin_classifications.json         # LLM pin classification results (reusable)
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
3. ~~Step 3: Netlist export~~ → DONE (sim_run.py + si_spectre.env template + SiteConfig)
4. ~~Step 4: Simulation config~~ → DONE (sim_config.py: SimDeckConfig + LLM + Maestro parser)
5. ~~Step 5: Spectre run~~ → DONE (SpectreSimulator from bridge)
6. ~~Step 6: Verification~~ → DONE (sim_verify.py)
7. **IO Ring end-to-end test** — run full pipeline on IO_RING_12x12 with spectre (IO pad cell models needed)
8. **PDK model path** — locate TSMC28 IO pad cell spectre models on remote (analog vs digital pad models)
9. **PSF signal naming** — validate fuzzy mapping on real spectre output from IO ring
10. **Iterate layout rules** — tune `LayoutConfig` params for different designs
