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

1. Read `references/pin_classification.md` for the classification rules and topology tables
2. Read the `pin_info.json` file from the current run directory
3. Classify every pin according to the rules — include BOTH outer and inner side devices
4. Write the result following the schema in `scripts/pin_classify_schema.json`
5. Save as `pin_classifications.json` in the same run directory
6. The pipeline will pick it up and proceed with source/load placement

## Dual-Side Topology

The testbench has devices on **both sides** of the DUT symbol. You MUST specify
what goes on each side for every pin.

**OUTER (left)** = IO pad side = the pin itself.
**INNER (right)** = CORE side = `_CORE` suffix pins or duplicate pins.

The outer and inner devices are **complementary**:

| Outer device | Inner device | Domain |
|-------------|-------------|--------|
| vdc (voltage source) | idc (compliance current, ~few mA) | analog |
| idc (current source) | vdc (compliance voltage, ~200-500mV) | analog |
| cap (digital output load) | vpulse (digital output stimulus) | digital |
| vpulse (digital input stimulus) | cap (digital input load) | digital |
| — (ground = PVSS) | — | ground |
| — (noConn) | digital_hv supply inner | digital_hv |

**Value selection**: Pick reasonable but NON-ROUND values within typical ranges.
Good: `2.7m`, `0.37`, `1.72`, `0.87`. Bad: `3m`, `0.3`, `1.8`, `0.9`.

Full topology tables are in `references/pin_classification.md`.

## Domains

Every pin belongs to a domain that determines its ground reference:

| Domain | Ground net | Ground device | Used by |
|--------|-----------|---------------|---------|
| `analog` | `gnd_{BLOCK}` | PVSS per block | VDD*, VREF*, VCM*, IB*, VIN* |
| `digital` | `dgnd` | GIOL / PVSS1DGZ | D*, SDI, SDO, SYNC, GIO*, clock, reset |
| `digital_hv` | `dgnd_hv` | PVSS2DGZ | PVDD2POC, PVSS2DGZ |

## Output Schema

See `scripts/pin_classify_schema.json` for the exact JSON structure.
Each pin entry must include:

```json
{
  "name": "D0",
  "pin_type": "digital_output",
  "domain": "digital",
  "confidence": 0.90,
  "reason": "D prefix = data, output direction, digital domain",
  "stimulus": null,
  "stimulus_params": null,
  "load": "cap",
  "load_params": {"c": "10p"},
  "inner_stimulus": "vpulse",
  "inner_params": {"v1": "0", "v2": "1.72", "per": "7n", "tr": "0.1n", "tf": "0.1n", "pw": "3.5n"},
  "ground_net": "dgnd"
}
```

Required fields: `name`, `pin_type`, `domain`, `confidence`, `reason`
Dual-side fields: `stimulus`/`stimulus_params` (outer), `inner_stimulus`/`inner_params` (inner)

Valid `pin_type`: `power`, `ground`, `digital_input`, `digital_output`, `digital_bidirectional`,
`analog_input`, `analog_output`, `analog_bidirectional`, `clock`, `reset`, `reference`,
`bias_current`, `no_connect`

Valid `domain`: `analog`, `digital`, `digital_hv`

## Naming Convention System

Pin names encode their function via **prefix patterns**. The full convention is
documented in `references/pin_classification.md`, which you MUST read before
classifying pins. Key points:

- **`VDD*`** → `power` / `analog` (typically 0.9V core), **`VIOL`** → 0.9V, **`VIOH`** → 1.8V
- **`IB*`/`IBUF*`** → `bias_current` / `analog` (idc, NOT vdc — these are current sources)
- **`VREF*`/`VCM*`** → `reference` / `analog` (need specific DC voltage, NOT "no stimulus")
- **`VINP`/`VINN`** → `analog_input` / `analog` (biased at VCM, typically 0.45V)
- **`D*`/`SDI`/`SDO`/`SYNC`/`GIO*`** → digital types / `digital` domain
- **`PVDD2POC`** → `power` / `digital_hv`, inner = noConn
- **`PVSS2DGZ`** → `ground` / `digital_hv`, inner = noConn
- **`*_CORE`** → same type as the base pin, but gets inner-side devices

Different users may use slightly different suffixes, but the prefix pattern is stable.

## Testbench Topology: PVSS and Ground Sharing

Ground pins are NOT wired to the global `gnd!` net. Instead, each functional block
gets a **PVSS device** — a `vdc` at ~0V that provides a named local ground.

Full topology rules are in `references/pin_classification.md` (Testbench Topology Rules).
Key principles:

1. **PVSS ≠ gnd!**: PVSS is a `vdc(vdc=0)` instance. PLUS = local ground net,
   MINUS = `gnd!`. This allows ground current measurement and domain isolation.
2. **Block-based sharing**: Pins with the same block suffix (e.g., `GND_DAT` + `GND_DAT_CORE`
   + `VDD_DAT` = `DAT` block) share one PVSS (`PVSS_DAT`) and one local ground net (`gnd_DAT`).
3. **Digital ground is separate**: Digital-domain pins use `dgnd` (provided by GIOL/PVSS1DGZ),
   not analog block grounds.
4. **Source reference terminals**: The MINUS terminal of every source/load in a block
   connects to the block's local ground net, NOT `gnd!`.
5. **Inner devices also use local ground**: Inner-side (CORE) device reference terminals
   connect to the same ground net as the outer-side devices for that block.

## Label-Based Wiring

No explicit wires are drawn. Instead, the same net name label is placed on both
the DUT terminal and the source/load terminal. Virtuoso auto-connects terminals
that share a net label.

- DUT pin `DIN0` gets label `DIN0`
- Source `SRC_DIN0` terminal `PLUS` gets label `DIN0`  →  auto-connected
- Source `SRC_DIN0` terminal `MINUS` gets label for the block's local ground net

Ground-type pins are labeled with their block's local ground net (e.g., `gnd_DAT`),
NOT `gnd!`. The PVSS device bridges the local ground to `gnd!`.

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
| `src/sim_viz.py` | PSF parser + SVG plot generator (DC sweep, AC Bode, TRAN waveform) |
| `src/symbol_redistribute.py` | Standalone redistribution runner |
| `src/bridge/` | Bridge call patterns for Virtuoso operations |
| `skill_code/` | SKILL (.il) files executed on the Virtuoso side |
| `references/pin_classification.md` | Classification rules + dual-side topology tables (LLM reads this) |
| `scripts/pin_classify_schema.json` | JSON schema for classification output |
| `docs/` | Design docs and progress notes |
