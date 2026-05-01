---
name: sim-tb-builder
description: >
  Build simulation testbenches for IO Ring / mixed-signal designs in Cadence Virtuoso.
  Automates the full flow: symbol export → pin redistribution → testbench creation →
  DUT placement → LLM-driven pin classification → source/load placement with label-based wiring.
  Use this skill whenever the user wants to create a simulation testbench, classify IO pins,
  place stimulus/load on a DUT, or run the sim_flow pipeline for any design.
  Also use when the user mentions "sim flow", "testbench", "TB setup", "pin classification",
  "source placement", or "stimulus generation" in the context of Virtuoso simulation.
---

# SIM_IO — Simulation Testbench Builder

## Overview

This skill builds a complete simulation testbench (TB) around a DUT in Cadence Virtuoso.
The pipeline runs through these stages:

1. **Symbol Export** — TSG pipeline generates a symbol from the schematic
2. **Pin Redistribution** — symbol pins are redistributed evenly on 4 sides
3. **TB Cellview Creation** — a `{cell}_tb` schematic is created
4. **DUT Placement + Wiring** — DUT instance placed, pins labeled, sources/loads added
5. **ADE Assembler** — deferred (requires ADE permission)

## Key Concept: LLM-Driven Pin Classification

Pin type classification (power, ground, digital_input, analog_output, clock, etc.)
is **not hardcoded**. Instead:

1. The Python pipeline (`src/sim_flow.py`) extracts pin info (name, direction, position, side)
   from the Virtuoso symbol and writes it to `pin_info.json`
2. **You (the LLM)** read `pin_info.json` + the classification rules in
   `references/pin_classification.md`, then classify each pin
3. You write the classification result to `pin_classifications.json`
4. The Python pipeline reads your classifications and places the correct stimulus/load

This makes the system adaptable — you can add new pin types, handle naming conventions
from different foundries, and apply domain knowledge that hardcoded regex cannot capture.

## When to Classify Pins

When the sim_flow pipeline reaches Step 4b, it will output `pin_info.json` to the
run directory. At that point:

1. Read `references/pin_classification.md` for the classification rules
2. Read the `pin_info.json` file from the current run directory
3. Classify every pin according to the rules
4. Write the result following the schema in `scripts/pin_classify_schema.json`
5. Save as `pin_classifications.json` in the same run directory
6. The pipeline will pick it up and proceed with source/load placement

## Output Schema

See `scripts/pin_classify_schema.json` for the exact JSON structure.
Each pin entry must include:

```json
{
  "name": "DIN0",
  "pin_type": "digital_input",
  "confidence": 0.95,
  "reason": "direction=input, no power/ground keyword, digital naming convention"
}
```

Valid `pin_type` values are defined in `src/pin_types.py:PinType`:
- `power`, `ground`
- `digital_input`, `digital_output`, `digital_bidirectional`
- `analog_input`, `analog_output`, `analog_bidirectional`
- `clock`, `reset`, `reference`, `no_connect`

## Stimulus/Load Rules

The mapping from pin type to actual Virtuoso instances is in `src/pin_types.py:PAD_RULES`.
When the LLM classifies a pin, the Python code looks up the corresponding rule:

| pin_type            | Stimulus  | Load  |
|---------------------|-----------|-------|
| power               | vdc (VDD) | —     |
| ground              | vdc (0V)  | —     |
| digital_input       | vpulse    | —     |
| digital_output      | —         | cap   |
| digital_bidirectional | vpulse  | cap   |
| clock               | vpulse    | —     |
| reset               | vpulse    | —     |
| analog_input        | vdc       | —     |
| analog_output       | —         | cap   |

## Label-Based Wiring

No explicit wires are drawn. Instead, the same net name label is placed on both
the DUT terminal and the source/load terminal. Virtuoso auto-connects terminals
that share a net label.

- DUT pin `DIN0` gets label `DIN0`
- Source `SRC_DIN0` terminal `PLUS` gets label `DIN0`  →  auto-connected
- Source `SRC_DIN0` terminal `MINUS` gets label `gnd!`  →  tied to global ground

Ground-type pins are wired directly to `gnd!` and get no source/load instance.

## Fallback Behavior

If no LLM classification file exists, the pipeline falls back to
`src/pin_types.py:classify_pin_heuristic()` — the original name-matching heuristic.
This ensures the pipeline always works, even without LLM involvement.

## File Guide

| Path | Purpose |
|------|---------|
| `src/sim_flow.py` | Main pipeline (Steps 1–4) |
| `src/pin_types.py` | Pin data structures, PAD_RULES, heuristic fallback, LLM loader |
| `src/symbol_layout_engine.py` | Layout calculation engine for pin redistribution |
| `src/symbol_redistribute.py` | Standalone redistribution runner |
| `src/bridge/` | Bridge call patterns for Virtuoso operations |
| `skill_code/` | SKILL (.il) files executed on the Virtuoso side |
| `references/pin_classification.md` | Classification rules (LLM reads this) |
| `scripts/pin_classify_schema.json` | JSON schema for classification output |
| `docs/` | Design docs and progress notes |
