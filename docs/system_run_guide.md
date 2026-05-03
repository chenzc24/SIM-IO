# SIM-IO System Run Guide

> Updated: 2026-05-01

How the SIM-IO pipeline runs end-to-end: what each module does, how data flows between them, what runs locally vs remotely, and how to invoke the system.

---

## Architecture Overview

```
Windows Agent (this machine)              Linux Virtuoso Server (remote)
============================              ==============================

sim_flow.py  ──SKILL──>  Virtuoso IC
   │                         │
   ├─ Step 1: TSG            ├─ Symbol created
   ├─ Step 2: Redistribute   ├─ Pins repositioned
   ├─ Step 3: Create _tb     ├─ Empty schematic
   ├─ Step 4: Place DUT      ├─ Instance + labels + sources
   │                         │
sim_run.py  ──SKILL──>  si batch netlister
   │                         │
   ├─ Step 5a: si.env        ├─ netlist exported
   │                         │
sim_deck_template.py          │
   │                         │
   ├─ Step 5b: deck.scs      │  (local file, uploaded)
   │                         │
sim_run.py  ──SSH───>  Spectre simulator
   │                         │
   ├─ Step 5c: run spectre   ├─ PSF results
   │                         │
sim_run.py  ←─SCP────  PSF ASCII files
   │
sim_verify.py
   │
   └─ Step 5d: verify.json   (local report)
```

**Key principle**: Python orchestrates on Windows. SKILL executes on remote Virtuoso. Spectre runs on the remote Linux server. All coordination goes through `virtuoso-bridge-lite`.

---

## Prerequisites

1. **virtuoso-bridge running**: `virtuoso-bridge start` (establishes SSH tunnel + SKILL IPC)
2. **Virtuoso IC open** on the remote server with the target library loaded
3. **Python environment**: `.venv` with `virtuoso-bridge-lite` installed
4. **Environment variables** (in `.env`):

| Variable | Purpose | Required |
|----------|---------|----------|
| `VB_REMOTE_HOST` | SSH hostname of Virtuoso server | Yes |
| `VB_REMOTE_USER` | SSH username | Yes |
| `VB_CADENCE_CSHRC` | Path to Cadence cshrc on remote | For spectre |
| `VB_PDK_SPECTRE_INCLUDE` | PDK model file path on remote | For spectre |
| `SPECTRE_CMD` | Spectre binary name (default: `spectre`) | Optional |

---

## Pipeline Steps

### Step 1: Symbol Export (TSG)

**Module**: `sim_flow.py:export_symbol()`
**Remote**: SKILL in Virtuoso

Generates a symbol view from the schematic using Cadence's Text-to-Symbol Generator (TSG).

```
Input:  {lib}/{cell}/schematic  (must exist in Virtuoso)
Output: {lib}/{cell}/symbol     (new view created)
```

Two SKILL calls:
1. `schSchemToPinList()` — reads schematic, produces pin list
2. `schPinListToSymbol()` — generates symbol from pin list

If symbol already exists, skips (returns True). Sets `ssgSortPins = "geometric"` to preserve schematic spatial layout.

### Step 2: Symbol Pin Redistribution

**Module**: `sim_flow.py:redistribute_symbol()`
**Helper modules**: `symbol_layout_engine.py`, `skill_code/extract_symbol_info.il`
**Remote**: SKILL in Virtuoso (extract + apply phases)

Redistributes all symbol pins evenly on 4 sides (left, right, top, bottom). Three sub-steps:

```
2a. Fresh TSG          — delete old symbol, regenerate (clean slate)
2b. Extract symbol info — SKILL dumps all rects/lines/labels/terminals
2c. Calculate layout    — Pure Python LayoutEngine computes positions
2d. Apply layout        — Generated SKILL script moves all shapes
```

**Data flow**:
```
SKILL output (pipe-delimited text)
  → parse_symbol_info() → SymbolInfo dataclass
  → LayoutEngine.redesign() → LayoutResult (body + pin positions)
  → generate_apply_skill() → SKILL script string
  → client.load_il() → executed on remote
```

**Special cases**:
- `*_CORE` pins: moved to inner rect, not counted in outer distribution
- Multi-pin terminals (VSS has 2 pin figures): each gets independent positioning
- Duplicate labels: consumed-label tracking prevents overlap

### Step 3: Create TB Cellview

**Module**: `sim_flow.py:create_tb_cellview()`
**Remote**: SKILL in Virtuoso

Creates a new empty schematic named `{cell}_tb`.

```
Input:  {lib}/{cell}              (primary cell)
Output: {lib}/{cell}_tb/schematic  (empty, mode="w")
```

Uses `dbOpenCellViewByType(mode="w")` — destroys existing content if the _tb cell already exists.

### Step 4: DUT Placement + Wiring + Stimulus

**Module**: `sim_flow.py` (Step 4a-4d)
**Helper modules**: `pin_types.py`, `skill_code/set_inst_params.il`
**Remote**: SKILL + SchematicEditor in Virtuoso

Four sub-steps that build the complete testbench:

#### 4a: Place DUT Instance

```
Input:  {lib}/{cell}_tb/schematic + {lib}/{cell}/symbol
Output: DUT instance at (2.5, 0.0) with R0 orientation
```

Uses `SchematicEditor` context + `schematic_create_inst_by_master_name()`. Auto `schCheck` + `dbSave` on exit.

#### 4b: Extract Pin Info

```
Input:  {lib}/{cell}/symbol
Output: list[PinInfo] — name, direction, x, y, side for each terminal
```

Queries symbol terminal names, directions, and positions via SKILL. Determines side (left/right/top/bottom) by minimum distance to bBox edge.

Also writes `pin_info.json` to the run directory for LLM classification (see LLM Mode below).

#### 4c: Add Wire Labels (label-based wiring)

```
Input:  DUT instance + pins list
Output: Net labels on every DUT terminal
```

For each pin:
- Ground pins → label `gnd!` (global ground)
- All others → label = pin name (e.g., `DIN0`)

These labels are the **wiring mechanism**: when the source/load instance later gets the same net label on its terminal, Virtuoso auto-connects them.

#### 4d: Place Sources & Loads

```
Input:  pins list + PAD_RULES (from pin_types.py)
Output: analogLib instances placed + CDF params set
```

Two-phase:

**Phase 1 — SchematicEditor**: Place all instances + labels
- For each pin, classify via `classify_pin_heuristic()` → pad_type
- Look up `PAD_RULES[pad_type]` → source/load config
- Place instances at offset positions outward from pin
- Label primary terminal with pin name, reference terminal with `gnd!`

**Phase 2 — setInstParams SKILL**: Set CDF parameters
- Load `set_inst_params.il` (headless-safe, no GUI dependency)
- For each instance with params, call `setInstParams()` with resolved values
- `VDD` placeholder replaced with actual `vdd_value` (e.g., 1.8)

**Placement geometry**:
- Sources: 5.0um outward from DUT pin
- Loads: 8.0um outward (when both source and load exist)
- Left/right instances rotated R90 to avoid vertical overlap

**Wiring example** (digital_input pin `DIN0`):
```
DUT.DIN0        ──label "DIN0"──→  auto-connect
SRC_DIN0.PLUS   ──label "DIN0"──→  (same net)
SRC_DIN0.MINUS  ──label "gnd!"──→  global ground
```

### Step 5: Netlist Export + Spectre + Verification

**Module**: `sim_run.py` (Steps 5a-5d), `sim_deck_template.py`, `sim_verify.py`
**Remote**: SKILL (si netlist) + SSH (spectre execution)

Only runs when `run_sim=True`. ADE-free path using `si` batch netlister + command-line Spectre.

#### 5a: Export Netlist (si batch)

```
Input:  {lib}/{cell}_tb/schematic
Output: netlist.scs (circuit-only Spectre netlist)
```

**Prerequisites** (discovered automatically from Virtuoso session):
1. License env vars (`LM_LICENSE_FILE`, `CDS_LIC_FILE`) — si doesn't inherit Virtuoso's license
2. Local cds.lib with SOFTINCLUDE of IC618 defaults — ensures `analogLib`/`basic` resolve
3. `schCheck + dbSave` on the _tb schematic — si refuses modified cellviews (OSSHNL-109)
4. Complete `si.env` — `simInitEnvWithArgs()` produces incomplete output; must write manually

Steps:
1. `schCheck + dbSave` on the _tb schematic via SKILL
2. `_discover_cadence_env()` — query Virtuoso for license vars and cds.lib path
3. Generate local cds.lib on remote: `SOFTINCLUDE <IC618>/share/cdssetup/cds.lib` + `INCLUDE <user>/cds.lib`
4. Write complete `si.env` (simLibName, simCellName, simSimulator, simViewList, simStopList, simNetlistHier)
5. Run `si -batch -cdslib <local_cdslib> -command nl` via SSH with license env vars set
6. Download netlist to local `run_dir/netlist.scs`

**Important**: `si` must run via SSH shell command, NOT via SKILL `system()` (would deadlock CIW).

**Full working command** (tested on IO_RING_12x12_tb):
```bash
export PATH=/home/cadence/ic618/IC618Hotfix4/tools/bin:.../dfII/bin:$PATH
export LD_LIBRARY_PATH=.../tools/lib/64bit:$LD_LIBRARY_PATH
export LM_LICENSE_FILE=1717@lic_server:5280@thu-han
export CDS_LIC_FILE=5280@thu-han
/home/cadence/ic618/IC618Hotfix4/tools/dfII/bin/si \
    -batch -cdslib /tmp/sim_io_si_run/cds.lib -command nl
```

See `docs/netlist_export_findings.md` for detailed debug notes.

#### 5b: Build Sim Deck

**Module**: `sim_deck_template.py:build_sim_deck()`

```
Input:  netlist.scs + SimConfig
Output: deck.scs (complete, ready-to-run Spectre deck)
```

The `si` netlist contains only circuit definitions. `build_sim_deck()` appends:

```spectre
// === si-generated circuit netlist (DO NOT EDIT above this line) ===
include "/path/to/PDK/models.scs" section=TT

simulatorOptions options reltol=1e-4 vabstol=1e-6 iabstol=1e-12 temp=27 tnom=27 gmin=1e-12

tran tran stop=10u errpreset=moderate

saveOptions options save=allpub
```

`SimConfig` parameters control analysis type, stop time, temperature, model path, etc.

#### 5c: Run Spectre

**Module**: `sim_run.py:run_spectre()`
**Wrapper**: `SpectreSimulator.from_env()` from virtuoso-bridge-lite

```
Input:  deck.scs (local file)
Output: SimulationResult with .data dict of signal arrays
```

`SpectreSimulator` handles:
- Upload deck to remote via SSH
- Source Cadence cshrc for spectre binary
- Run `spectre -64 deck.scs +escchars -format psfascii`
- Download PSF ASCII results to local
- Parse PSF into `result.data` dict

#### 5d: Parse Results + Verify

**Module**: `sim_run.py:parse_results()`, `sim_verify.py`

```
Input:  SimulationResult.data + list[PinInfo]
Output: measurements.json + verify.json
```

**Parsing**: Maps PSF signal names back to pin names:
- Try exact patterns: `DUT.D0`, `D0`, `/DUT/D0`
- Fuzzy fallback: any key ending with `.D0` or `/D0`
- Extracts: vmax, vmin, vavg, vpp, slew_rate (rising edge 10%-90%)

**Verification**: Compares per-pin measurements against golden values:
- `digital_input/output`: vmax >= 0.9*VDD, vmin <= 0.1*VDD
- `power`: vmax within 1% of VDD
- Tolerance: voltage ±50mV or 3%, delay ±1ns or 10%
- Output: `verify.json` with overall verdict (PASS/FAIL/INCOMPLETE)

---

## Pin Classification: Heuristic vs LLM

### Heuristic Mode (default, always works)

`pin_types.py:classify_pin_heuristic()` — name-matching rules:

| Name pattern | Direction | Result |
|-------------|-----------|--------|
| VDD/VCC/DVDD/AVDD | any | `power` |
| VSS/GND/DVSS/AVSS | any | `ground` |
| (no power/gnd match) | input | `digital_input` |
| (no power/gnd match) | output | `digital_output` |
| (no power/gnd match) | inputOutput | `digital_bidirectional` |

### LLM Mode (when `pin_classifications.json` exists)

1. Pipeline writes `pin_info.json` at Step 4b
2. LLM reads `pin_info.json` + `references/pin_classification.md`
3. LLM writes `pin_classifications.json` following `scripts/pin_classify_schema.json`
4. `pin_types.py:load_pin_classifications()` loads the JSON
5. Pipeline uses LLM classifications instead of heuristic

LLM mode can identify types the heuristic cannot: `clock`, `reset`, `analog_input`, `reference`, `no_connect`.

---

## File Guide

### Python Modules (src/)

| File | Purpose | Key Functions |
|------|---------|---------------|
| `sim_flow.py` | Main pipeline (Steps 1-4 + optional Step 5) | `run_sim_flow()`, `export_symbol()`, `redistribute_symbol()`, `create_tb_cellview()`, `place_dut()`, `extract_dut_pins()`, `add_wire_labels()`, `place_sources_and_loads()` |
| `sim_run.py` | Simulation run (Steps 5a-5d) | `run_sim_run()`, `export_netlist()`, `build_deck()`, `run_spectre()`, `parse_results()` |
| `sim_deck_template.py` | SimConfig + deck builder | `SimConfig`, `build_sim_deck()`, `build_sim_deck_from_file()` |
| `sim_verify.py` | Golden mapping + verification | `verify_results()`, `golden_for_pin_type()`, `Tolerance` |
| `pin_types.py` | Pin data structures + classification | `PinType`, `PAD_RULES`, `SIDE_CONFIGS`, `classify_pin_heuristic()`, `load_pin_classifications()`, `write_pin_info_json()` |
| `symbol_layout_engine.py` | Layout calculation + SKILL generation | `LayoutEngine`, `LayoutConfig`, `generate_apply_skill()`, `parse_symbol_info()` |
| `symbol_redistribute.py` | Standalone redistribution runner | Entry point for running Step 2 independently |

### SKILL Scripts (skill_code/)

| File | Purpose | Called by |
|------|---------|-----------|
| `extract_symbol_info.il` | Dump all symbol shapes (pipe-delimited) | Step 2b |
| `set_inst_params.il` | Set CDF params on named instance (headless-safe) | Step 4d Phase 2 |
| `symbol_move_pin.il` | Move single pin rect+wire+label | Legacy, not used by main pipeline |
| `schematic_edit_procs.il` | Schematic edit utilities | Internal |

### Reference Docs

| File | Purpose |
|------|---------|
| `references/pin_classification.md` | Rules LLM uses to classify pins |
| `scripts/pin_classify_schema.json` | JSON schema for LLM classification output |

---

## Output Directory Structure

Every run creates a timestamped directory under `SIM-IO/output/`:

```
SIM-IO/output/20260501_153045/
├── result.json              # SimFlowResult — overall status
├── pin_info.json            # Pin extraction data (LLM input)
├── extract_raw.txt          # Raw SKILL extraction output
├── layout_result.json       # Layout engine calculation results
├── apply_layout.il          # Generated SKILL script for Step 2d
├── skill_code/              # Snapshot of SKILL scripts used in this run
│   ├── extract_symbol_info.il
│   └── set_inst_params.il
│
│   # Step 5 outputs (only when run_sim=True):
├── netlist.scs              # si-exported circuit netlist
├── deck.scs                 # Complete Spectre deck
├── spectre_result.json      # Spectre run metadata
├── measurements.json        # Per-pin extracted measurements
├── sim_run_result.json      # SimRunResult
├── verify.json              # Verification report (PASS/FAIL)
└── {deck}.raw/              # PSF ASCII result files
```

---

## How to Run

### Option 1: TB Build Only (Steps 1-4)

```bash
cd SIM-IO/src
python sim_flow.py LLM_Layout_Design_Lab IO_RING_12x12
```

Or from Python:

```python
from sim_flow import run_sim_flow

result = run_sim_flow("LLM_Layout_Design_Lab", "IO_RING_12x12")
print(result.tb_cell)          # "IO_RING_12x12_tb"
print(len(result.pins))        # number of pins
print(result.sources_placed)   # list of source/load instance names
```

### Option 2: TB Build + Simulation (Steps 1-5)

```python
from sim_flow import run_sim_flow

result = run_sim_flow(
    "LLM_Layout_Design_Lab", "IO_RING_12x12",
    run_sim=True,
    model_include="/home/process/tsmc28n/.../toplevel.scs",
    vdd_value=1.8,
)
print(result.sim_run_ok)       # True/False
print(result.sim_verdict)      # "PASS" / "FAIL" / "INCOMPLETE"
```

### Option 3: Standalone Simulation (Step 5 only)

If the _tb schematic already exists in Virtuoso:

```python
from sim_run import run_sim_run
from sim_deck_template import SimConfig
from pin_types import PinInfo

cfg = SimConfig(
    model_include="/path/to/PDK/models.scs",
    analysis="tran",
    stop="10u",
    errpreset="moderate",
)
sim_result = run_sim_run(
    "LLM_Layout_Design_Lab", "IO_RING_12x12_tb",
    pins=[...],  # PinInfo list
    run_dir=Path("output/20260501_153045"),
    config=cfg,
)
```

### Option 4: Standalone Verification

```bash
cd SIM-IO/src
python sim_verify.py ../../output/20260501_153045/measurements.json 1.8
```

### Option 5: LLM Pin Classification

When the pipeline pauses at Step 4b (after writing `pin_info.json`):

1. Read `references/pin_classification.md`
2. Read `output/<timestamp>/pin_info.json`
3. Classify each pin
4. Write `pin_classifications.json` to the same run directory
5. Pipeline picks it up automatically in Step 4d

---

## Stimulus/Load Rules (PAD_RULES)

| pad_type | Source | Source CDF Params | Load | Load CDF Params |
|----------|--------|-------------------|------|-----------------|
| power | vdc | vdc=VDD | — | — |
| ground | — | — | — | — |
| digital_input | vpulse | v1=0, v2=VDD, per=100n, tr=1n, tf=1n, pw=50n | — | — |
| digital_output | — | — | cap | c=10p |
| digital_bidirectional | vpulse | v1=0, v2=VDD, per=100n, tr=1n, tf=1n, pw=50n | cap | c=10p |
| clock | vpulse | v1=0, v2=VDD, per=100n, tr=0.1n, tf=0.1n, pw=50n | — | — |
| reset | vpulse | v1=0, v2=VDD, per=1u, tr=1n, tf=1n, pw=500n | — | — |
| analog_input | vdc | vdc=VDD/2 | — | — |
| analog_output | — | — | cap | c=1p |
| analog_bidirectional | vdc | vdc=VDD/2 | cap | c=1p |
| reference | — | — | — | — |
| no_connect | — | — | — | — |

`VDD` is replaced at runtime with the `vdd_value` parameter (default 1.8V). `VDD/2` remains as a string expression for Spectre to evaluate.

---

## Network Round-Trip Summary

| Step | Method | Calls | Data Direction |
|------|--------|-------|----------------|
| 1. TSG | SKILL | 2-3 | Agent → Virtuoso |
| 2a. Fresh TSG | SKILL | 2 | Agent → Virtuoso |
| 2b. Extract | SKILL | 2 (load + exec) | Agent → Virtuoso → Agent |
| 2c. Calculate | Local | 0 | Pure Python |
| 2d. Apply | SKILL | 1 (load_il) | Agent → Virtuoso |
| 3. Create _tb | SKILL | 2 | Agent → Virtuoso |
| 4a. Place DUT | SchematicEditor | 1 batch | Agent → Virtuoso |
| 4b. Extract pins | SKILL | 3-4 | Agent → Virtuoso → Agent |
| 4c. Labels | SchematicEditor | 1 batch | Agent → Virtuoso |
| 4d. Place sources | SchematicEditor + SKILL | 1 batch + N | Agent → Virtuoso |
| 5a. si netlist | SKILL + Shell + SCP | 3 | Agent ↔ Remote |
| 5b. Build deck | Local | 0 | Pure Python |
| 5c. Spectre | SSH | 1 (SpectreSimulator) | Agent ↔ Remote |
| 5d. Parse + Verify | Local | 0 | Pure Python |

Total: ~15-20 network round-trips for TB build, ~4 additional for simulation.

---

## Error Recovery

| Failure | Symptom | Recovery |
|---------|---------|----------|
| Symbol exists | Step 1 skips | Delete symbol manually: `ddDeleteCellView(lib cell "symbol")` |
| TSG fails | Step 1/2a returns False | Check schematic has pins, library is writable |
| si fails | Step 5a returns None | Verify `cds.lib` path, check Virtuoso license |
| Spectre fails | Step 5c returns error | Check `spectre_result.json`, model include path |
| PSF signal not found | Pin marked "error" in measurements | Check PSF naming — inspect `result.data.keys()` |
| CDF params fail | WARNING in Step 4d | Verify instance exists, param name is correct |

---

## Key Design Decisions

1. **Label-based wiring** — no coordinate-based wire routing. Same net label on DUT terminal and source terminal → Virtuoso auto-connects. Simpler, more robust.

2. **SchematicEditor context** — `client.schematic.edit(lib, cell, mode="a")` batches all operations. Auto `schCheck` + `dbSave` on exit. Reduces round-trips.

3. **setInstParams.il** — headless-safe CDF setter. Opens schematic directly via `dbOpenCellViewByType()` instead of depending on `geGetEditCellView()` (GUI-only).

4. **si batch netlister** — ADE-free netlist export. No Maestro session required. Produces circuit-only netlist; simulation commands appended by Python.

5. **SpectreSimulator** — existing class from virtuoso-bridge-lite. Handles SSH upload, Cadence env sourcing, spectre execution, PSF download, and parsing.

6. **Pin classification dual mode** — heuristic always works; LLM mode enables advanced types (clock, reset, analog) when the classification JSON is provided.
