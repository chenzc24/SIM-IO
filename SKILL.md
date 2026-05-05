---
name: SIM-IO
description: >
  Build simulation testbenches for IO Ring / mixed-signal designs in Cadence Virtuoso.
  Automates the full flow: symbol export → pin redistribution → testbench creation →
  DUT placement → LLM-driven pin classification → source/load placement with label-based wiring.
  Use this skill whenever the user wants to create a simulation testbench, classify IO pins,
  place stimulus/load on a DUT, or run the sim_flow pipeline for any design.
  Also use when the user mentions "sim flow", "testbench", "TB setup", "pin classification",
  "source placement", or "stimulus generation" in the context of Virtuoso simulation.
---

# SIM-IO — Simulation Testbench Builder

Master orchestrator for building a complete simulation TB around a DUT cell in Cadence Virtuoso.

## Pipeline at a Glance

```
Source Schematic
       │
  [Phase A]  symbol export → pin redistribution → pin extraction
       │
  pin_info.json
       │
  [LLM stop] read pin_info.json + pin_classification.md
             → write pin_classifications.json
       │
  [Phase B]  create TB schematic → place DUT → wire labels
             → place sources/loads → Maestro setup
       │
  [Sim]      Spectre netlist → deck build → run → verify
```

Each phase is a single CLI call. The LLM classification step is a deliberate pause between them.

---

## Entry Points

| Situation | Start here |
|-----------|-----------|
| Fresh run: user provides `lib` + `cell` | Step 0 → Phase A |
| Phase A already done (`.latest_run` exists, no classification yet) | LLM Classification |
| `pin_classifications.json` already written | Phase B |
| TB exists, run simulation only | `maestro_runner.py --run-sim --run-dir <path>` |

---

## Step 0: Environment Setup

Run once per session before any other step.

```bash
# Auto-detect skill root
SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/SIM-IO"

# Resolve Python (project .venv preferred)
PROJECT_ROOT="$(cd "${SKILL_ROOT}" && while [ ! -d .venv ] && [ "$(pwd)" != "/" ]; do cd ..; done; pwd)"
if   [ -f "${PROJECT_ROOT}/.venv/Scripts/python.exe" ]; then export AMS_PYTHON="${PROJECT_ROOT}/.venv/Scripts/python.exe"
elif [ -f "${PROJECT_ROOT}/.venv/bin/python" ];         then export AMS_PYTHON="${PROJECT_ROOT}/.venv/bin/python"
elif command -v python3 &>/dev/null;                    then export AMS_PYTHON="python3"
else echo "ERROR: No Python found."; return 1; fi
echo "AMS_PYTHON=${AMS_PYTHON}"

# Load site configuration (.env in SIM-IO root)
[ -f "${SKILL_ROOT}/.env" ] && { set -a; . "${SKILL_ROOT}/.env"; set +a; }
```

**All subsequent steps use `$AMS_PYTHON`.**

Ask the user for `lib` and `cell` if not provided. Optional: `--vdd <volts>` (default 1.8).

---

## Phase A: Symbol Export + Pin Redistribution + Extraction

```bash
$AMS_PYTHON ${SKILL_ROOT}/scripts/symbol_export.py <lib> <cell> [--vdd <vdd_value>]
```

**What happens internally:**
1. **TSG export** — generates `{lib}/{cell}/symbol` from schematic via `schSchemToPinList` + `schPinListToSymbol`
2. **Redistribution** — extracts symbol geometry (`extract_symbol_info.il`), computes new layout (Python), applies it (`apply_layout.il`) — pins reorganized left/right
3. **Pin extraction** — reads terminal names, directions, positions from redistributed symbol

**Outputs:**
- `output/<timestamp>/pin_info.json` — input to LLM classification
- `output/<timestamp>/phase_a_result.json` — checkpoint for Phase B
- `.latest_run` — absolute path to the run directory

**Exit codes:**
- `0` → proceed to LLM Classification
- `1` → error printed to stderr (common causes: Virtuoso not connected, lib/cell not found, no schematic view)

---

## LLM Stop (Between Phases — YOU do this)

Phase A stops here. You must produce **two files** before Phase B can run:
`pin_classifications.json` and `sim_config.json`. Both go in the same run directory.

Find the run directory: read `SIM-IO/.latest_run` or use the path printed by Phase A.

---

### File 1 — Pin Classifications

1. Read `references/pin_classification.md` — classification rules, topology tables, domain definitions
2. Read `<run_dir>/pin_info.json` — pin names, directions, positions, side (left/right)
3. Classify every pin according to the rules
4. Write `<run_dir>/pin_classifications.json` following schema in `scripts/pin_classify_schema.json`

Key principles (full rules in `references/pin_classification.md`):
- `pin_type` from pin name prefix + direction
- `domain` (analog / digital / digital_hv) — sets the ground reference
- `stimulus` + `stimulus_params` for the outer (left/pad) side
- `inner_stimulus` + `inner_params` for the inner (right/CORE) side
- Non-round values only (e.g. `2.7m` not `3m`, `1.72` not `1.8`)

---

### File 2 — Simulation Config

1. Read `references/sim_config_rules.md` — IO Ring simulation rules
2. From `pin_classifications.json`, collect all `vpulse` stimulus params to compute `tstop`:
   - Gather every `per` value across all pin stimulus/inner_stimulus params
   - `tstop = 10 × max(per)`, clamped to `[100n, 10u]`
   - If no vpulse sources: use `500n`
3. List every placed device that is NOT `pin_type=ground` and NOT `pin_type=no_connect`:
   these are `SRC_<pin>`, `LOAD_<pin>`, `INNER_<pin>` instances
4. Write `<run_dir>/sim_config.json`

`sim_config.json` is consumed by Phase B in two places:
- **Maestro setup** (Step 4e) — configures analyses and outputs in Virtuoso Maestro
- **Spectre deck** (Step 5) — controls netlist analyses, save statements, and power expressions

Schema (see `scripts/sim_config_schema.json` for full spec).
The LLM specifies **measurement intent** via `pin_measurements` — the code translates
intent into correct Maestro OCEAN expressions automatically. Never write raw OCEAN
expressions in `outputs`; use `pin_measurements` instead.

```json
{
  "analyses": [
    {"name": "dc", "enabled": true},
    {"name": "tran", "enabled": true,
     "stop": "<tstop>", "maxstep": "<tstop/1000>", "errpreset": "moderate"}
  ],
  "model_includes": [],
  "save_default": "allpub",
  "pin_measurements": {
    "VDD": {"measures": ["voltage", "current", "power"], "spec": {"i_max": "0.1"}},
    "D0":  {"measures": ["voltage"], "spec": {"vmax_above": "0.9*VDD", "vmin_below": "0.1*VDD"}},
    "GND": {"measures": []}
  }
}
```

One `pin_measurements` entry per DUT pin. `model_includes` is always `[]` — Phase B injects PDK paths from `.env` automatically. `save_default` is always `"allpub"` — code auto-upgrades to `"all"` when current/power measurements are detected.

---

## Phase B: TB Build + Source/Load Placement + Maestro

```bash
$AMS_PYTHON ${SKILL_ROOT}/scripts/tb_builder.py [--run-dir <path>]
```

`--run-dir` is optional; defaults to path in `.latest_run`.

**What happens internally:**
1. **Create TB cellview** — creates `{lib}/{cell}_tb/schematic` (fresh, overwrites if exists)
2. **Place DUT** — instantiates `{lib}/{cell}/symbol` as `DUT` at (2.5, 0.0)
3. **Wire labels** — places net name labels on each DUT terminal (label-based wiring — no explicit wires)
4. **Sources + loads** — places stimulus/load devices based on your `pin_classifications.json`:
   - Outer (left): sources/loads for pad-side signals
   - Inner (right): complementary devices for CORE-side signals
   - PVSS devices (one per ground pin) + GND_REF bridge to `gnd!`
   - CDF parameters set via `set_inst_params.il`
5. **Maestro setup** — configures Maestro test for GUI simulation

**Outputs:**
- `{lib}/{cell}_tb/schematic` in Virtuoso
- `output/<timestamp>/result.json` — full run summary

**Exit codes:**
- `0` → TB complete, proceed to simulation or stop
- `1` → error printed to stderr

---

## Step 5: Run Simulation

```bash
$AMS_PYTHON ${SKILL_ROOT}/scripts/maestro_runner.py --run-sim [--intent "<description>"]
```

Or if the TB is already built, pass `--run-dir` to an existing run directory.

**Internal flow:**
1. `run_maestro_sim()` — opens background Maestro session, runs Spectre inside Maestro, polls until done
2. `read_results()` — reads scalar OCEAN outputs per point: `vmax_<pin>`, `vmin_<pin>`, `I_<pin>`, `P_<pin>`
3. `parse_maestro_measurements()` — maps outputs → `measurements.json` (Python-accessible per-pin dict)
4. `plot_maestro_waves()` — parses `maestro_waves/*.txt` → `plots/tran_maestro.svg`
5. `verify_results()` — compares measurements against golden specs → `verify.json`

**Outputs written to `output/<timestamp>/`:**

| File | Content |
|------|---------|
| `maestro_result.json` | Raw Maestro per-point output table |
| `measurements.json` | Per-pin `vmax`/`vmin`/`iavg`/`pavg` — Python-readable |
| `verify.json` | PASS/FAIL verdict per pin |
| `maestro_waves/*.txt` | Raw OCEAN two-column waveform text (time, voltage) |
| `plots/tran_maestro.svg` | SVG transient waveform visualization |

The Maestro cellview is always configured in Step 4e (even without `--run-sim`), so you can also open the test manually in the Virtuoso GUI and run it there.

---

## Troubleshooting

| Problem | Solution |
|---------|---------|
| Phase A exit 1 "not found in Virtuoso" | Check `lib`/`cell` spelling; verify Virtuoso is connected and cds.lib is loaded |
| Phase A exit 1 "no schematic view" | Cell exists but has no schematic — open schematic in Virtuoso first |
| Virtuoso not responding | Check `SIM_VB_LOCAL_PORT` in `.env`; verify `virtuoso-bridge start` is running |
| Symbol redistribution wrong layout | Inspect `output/<ts>/extract_raw.txt` and `layout_result.json`; check `LayoutConfig` in `sim_io/symbol/layout_engine.py` |
| Phase B: "pin_classifications.json not found" | WARNING only — runs with heuristic fallback. Write the file for accurate placement |
| Phase B: wrong device placed | Re-check `pin_classifications.json`; verify `pin_type`, `domain`, `stimulus`, `inner_stimulus` fields |
| Spectre: license error | Set `SIM_LM_LICENSE_FILE` and `SIM_CDS_LIC_FILE` in `.env` |
| Spectre: no convergence | Check stimulus values — ensure `vdc`/`vpulse` params are within PDK operating range |
| Maestro eval error | Known issue — Maestro dialog may require manual confirmation; see memory `feedback_sim_io_pipeline.md` |
| `si` netlist export hangs | Confirmation dialog opened in Virtuoso GUI — dismiss it manually or set `si_batch=yes` in site config |

---

## File Guide

| Path | Purpose |
|------|---------|
| `scripts/symbol_export.py` | CLI: Symbol export + pin extraction entry point |
| `scripts/tb_builder.py` | CLI: TB build entry point |
| `scripts/maestro_runner.py` | CLI: Maestro setup + simulation entry point |
| `sim_io/flow.py` | Core pipeline — `run_symbol_export()`, `run_tb_builder()`, `run_maestro_runner()`, `run_sim_flow()` |
| `sim_io/pin_types.py` | `PinInfo`, `PinClassification`, heuristic fallback, JSON loader |
| `sim_io/symbol/layout_engine.py` | Pure-Python layout calculator for pin redistribution |
| `sim_io/bridge/edit_patterns.py` | Virtuoso schematic editing API (`batch_ops`, `label_term`, `create_inst`) |
| `sim_io/sim/viz.py` | `TranData`, `plot_tran()` — SVG waveform generator (reused by Maestro route) |
| `sim_io/maestro/setup.py` | Maestro testbench setup generator |
| `sim_io/maestro/results.py` | `parse_maestro_measurements()` — Maestro outputs → `measurements.json` |
| `sim_io/maestro/waves.py` | `plot_maestro_waves()` — `maestro_waves/*.txt` → SVG |
| `skill_code/extract_symbol_info.il` | SKILL: extract symbol geometry (called in Phase A Step 2) |
| `skill_code/set_inst_params.il` | SKILL: set CDF parameters on instances (called in Phase B Step 4) |
| `references/pin_classification.md` | **Classification rules + dual-side topology tables — read before classifying** |
| `scripts/pin_classify_schema.json` | JSON schema for `pin_classifications.json` |
| `scripts/sim_config_schema.json` | JSON schema for simulation deck configuration |
| `.env` | Site-specific paths: cds.lib, IC_ROOT, MMSIM_ROOT, license, PDK model paths |
| `.latest_run` | Absolute path to current run directory (written by Phase A) |

---

## Run Directory Structure

```
output/<YYYYMMDD_HHMMSS>/
├── pin_info.json              Phase A output → LLM input
├── pin_classifications.json   LLM output → Phase B (source/load placement)
├── sim_config.json            LLM output → Phase B (Maestro setup + Spectre deck)
├── phase_a_result.json        Symbol export checkpoint (loaded by tb_builder.py / maestro_runner.py)
├── result.json                Phase B final summary
├── extract_raw.txt            Raw output from extract_symbol_info.il
├── layout_result.json         Computed pin layout (body + pin positions)
├── apply_layout.il            Generated SKILL for redistribution
├── verify.json                Simulation verdict + measurements
├── deck.raw                   PSF data from Spectre
├── skill_code/                Logged copies of all .il files used
└── plots/                     SVG waveforms (DC/AC/TRAN)
```

---

## Checklist

- [ ] Step 0: `AMS_PYTHON` resolved; `.env` loaded
- [ ] Phase A exit 0: `pin_info.json` written, `.latest_run` updated
- [ ] LLM: read `references/pin_classification.md` before classifying
- [ ] LLM: every pin classified — including `_CORE` suffix pins
- [ ] LLM: `pin_classifications.json` written to `<run_dir>/`, not to SIM-IO root
- [ ] LLM: read `references/sim_config_rules.md` before writing sim config
- [ ] LLM: `tstop` computed from max vpulse `per` × 10
- [ ] LLM: `sim_config.json` written to `<run_dir>/` with `pin_measurements` for every DUT pin
- [ ] Phase B exit 0: `{cell}_tb/schematic` created in Virtuoso
- [ ] Phase B: `result.json` written to run directory
- [ ] Sim (if requested): `measurements.json`, `verify.json`, and `plots/tran_maestro.svg` present in run directory
