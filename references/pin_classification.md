# Pin Classification Rules

This document defines the rules for classifying IO pins by type and determining
their complete testbench topology (outer + inner side devices).
The LLM reads these rules alongside `pin_info.json` to produce `pin_classifications.json`.

---

## Available Pin Types

| Type | Description |
|------|-------------|
| `power` | Supply voltage pins (core or IO) |
| `ground` | Ground / substrate pins |
| `digital_input` | Digital signal input |
| `digital_output` | Digital signal output |
| `digital_bidirectional` | Digital tri-state or bidirectional |
| `analog_input` | Analog signal input (voltage) |
| `analog_output` | Analog signal output |
| `analog_bidirectional` | Analog bidirectional |
| `clock` | Clock signal |
| `reset` | Reset signal (active high or low) |
| `reference` | Voltage reference pin |
| `bias_current` | Bias current input (uses idc, NOT vdc) |
| `no_connect` | Unconnected / reserved pin |

---

## Domains

Every pin belongs to a **domain** that determines its ground reference and device style:

| Domain | Description | Ground device | Ground net convention (internal) | Source MINUS label |
|--------|-------------|---------------|----------------------------------|-------------------|
| `analog` | Analog/mixed-signal pins | PVSS (vdc ~0V) per pin | `gnd_{BLOCK}` | Primary pin name (e.g. `GND_DAT`) |
| `digital` | Digital IO and core pins | GIOL / PVSS1DGZ | `dgnd` | `GIOL` |
| `digital_hv` | High-voltage digital supply (PVDD2POC, PVSS2DGZ) | PVSS2DGZ | `dgnd_hv` | `PVSS2DGZ` |

The `domain` field is required in every pin classification entry.
The "Ground net convention" column is the `ground_net` field value — used
internally for grouping, never displayed as a schematic label.

---

## Naming Convention System

Pin names in IO ring designs follow a **naming convention** that encodes both
function and the correct stimulus type.  The convention below is derived from
real lab supply configurations and applies across designs — different users may
use slightly different suffixes, but the prefix pattern is stable.

### How to read the convention

Each prefix group implies:
1. **pin_type** — which determines the stimulus/load cell class (vdc, idc, vpulse, cap, ...)
2. **Stimulus value** — the actual voltage or current to apply
3. **Domain** — analog, digital, or digital_hv
4. **Dual-side topology** — what goes on the OUTER (left) vs INNER (right/CORE) side

### Core vs IO voltage domains

Most IO ring designs have **two independent IO voltage domains**:

| Domain | Typical pin | Core voltage | IO voltage |
|--------|-------------|-------------|------------|
| Low (L) | `VIOL` | 0.9V | 0.9V |
| High (H) | `VIOH` | 0.9V | 1.8V |

**CRITICAL**: Do NOT assume all power pins use the same VDD.
- Core supplies (`VDD*`) are typically **0.9V**
- IO supplies (`VIO*`) may be **0.9V (low)** or **1.8V (high)** depending on the domain suffix
- Always check the domain suffix (`L`/`H`) before assigning a voltage value

---

## Dual-Side Topology — Core Concept

IO ring testbenches have devices on **both sides** of the symbol:

```
        OUTER (left)              DUT (symbol)          INNER (right)
        ============              ============          ============

         ┌──outer──┐              ┌───────┐            ┌──inner──┐
   VDD──┤ vdc     ├──gnd    ←→   │  pin  │    ←→  ...──┤ idc     ├──gnd
         └─────────┘     label    └───────┘   label     └─────────┘
    (PLUS=pin name   (MINUS=                       (MINUS=primary
     matches DUT)    ground pin                     ground pin name,
                     name or                        NOT --GND)
                     --GND)
```

- **OUTER** = symbol left side = IO pad side = the actual pin
- **INNER** = symbol right side = CORE side = `_CORE` suffix pins or duplicate pins

**The outer and inner devices are complementary** — the type of device on one side
determines what goes on the other side.

---

## Value Selection Rule

When the rules below specify "a few mA" or "hundreds of mV" or "around VDD",
**pick a reasonable but non-round value** within the typical range.
Do NOT use exact integers like 3mA, 300mV, 1.8V.

Good examples: `idc=2.7m`, `vdc=0.34`, `v2=1.72`
Bad examples:  `idc=3m`,   `vdc=0.3`,  `v2=1.8`

The reason: simulation with exact round values can mask real-circuit behavior.
Slight variation exercises the design more realistically.

---

## Complete Topology Tables

### Analog domain — Voltage-type pins (`power`, `reference`, `analog_input`)

Outer gets a **voltage source** (vdc or vpulse). Inner gets a **compliance current source** (idc).

| pin_type | OUTER device | OUTER params (example) | INNER device | INNER params (range) | INNER on |
|----------|-------------|----------------------|-------------|---------------------|----------|
| `power` | vdc | `vdc=0.9` (per domain) | idc | `idc=1~5mA` pick non-round, e.g. `2.7m` | CORE/duplicate |
| `reference` | vdc | `vdc=<specific V>` | idc | `idc=1~5mA` pick non-round, e.g. `3.4m` | CORE/duplicate |
| `analog_input` | vdc | `vdc=<bias V>` | idc | `idc=1~5mA` pick non-round, e.g. `1.8m` | CORE/duplicate |

Inner wiring: idc PLUS = CORE pin net, idc MINUS = block local ground.

### Analog domain — Current-type pins (`bias_current`)

Outer gets a **current source** (idc). Inner gets a **compliance voltage source** (vdc).

| pin_type | OUTER device | OUTER params (example) | INNER device | INNER params (range) | INNER on |
|----------|-------------|----------------------|-------------|---------------------|----------|
| `bias_current` | idc | `idc=-10u` (per design) | vdc | `vdc=200~500mV` pick non-round, e.g. `0.37` | CORE/duplicate |

Inner wiring: vdc PLUS = CORE pin net, vdc MINUS = block local ground.

### Analog domain — Ground pins

| pin_type | OUTER device | INNER device |
|----------|-------------|-------------|
| `ground` | PVSS (vdc vdc=~0V) | — (ground pin has no CORE counterpart) |

PVSS is NOT `gnd!`. It is a `vdc` instance at ~0V. PLUS = pin name (same as DUT ground pin label), MINUS = `--GND` (global ground reference).

### Digital domain — Output IO

Outer = **load** (cap). Inner (CORE) = **stimulus** (vpulse to digital ground).

| pin_type | OUTER device | OUTER params | INNER device | INNER params | INNER on |
|----------|-------------|-------------|-------------|-------------|----------|
| `digital_output` | cap | `c=10p` | vpulse | `v1=0, v2=~VDD (non-round, e.g. 1.72), per=~7n, tr=0.1n, tf=0.1n, pw=~3.5n` | CORE |

Inner wiring: vpulse PLUS = CORE pin net, vpulse MINUS = digital ground (primary pin name, e.g. GIOL).

### Digital domain — Input IO

Outer = **stimulus** (vpulse to ground). Inner (CORE) = **load** (cap to digital ground).

| pin_type | OUTER device | OUTER params | INNER device | INNER params | INNER on |
|----------|-------------|-------------|-------------|-------------|----------|
| `digital_input` | vpulse | `v1=0, v2=~VDD (non-round, e.g. 0.87), per=~7n, tr=0.1n, tf=0.1n, pw=~3.5n` | cap | `c=10p` | CORE |

Inner wiring: cap PLUS = CORE pin net, cap MINUS = digital ground (primary pin name, e.g. GIOL).

### Digital domain — Bidirectional IO

Both sides get both stimulus and load. Outer = vpulse + cap. Inner (CORE) = vpulse + cap.
The outer vpulse drives the pad side; the inner vpulse drives the core side.

| pin_type | OUTER device | INNER device | INNER on |
|----------|-------------|-------------|----------|
| `digital_bidirectional` | vpulse + cap | vpulse + cap | CORE |

Outer vpulse MINUS = ground (primary pin name), cap MINUS = ground (primary pin name).
Inner vpulse MINUS = digital ground (primary pin name, e.g. GIOL), cap MINUS = digital ground (primary pin name, e.g. GIOL).

### Digital domain — Clock and Reset

| pin_type | OUTER device | OUTER params | INNER device | INNER params | INNER on |
|----------|-------------|-------------|-------------|-------------|----------|
| `clock` | vpulse | `v1=0, v2=~VDD (non-round), per=actual, tr=0.1n, tf=0.1n` | cap | `c=10p` | CORE |
| `reset` | vpulse | `v1=0, v2=~VDD (non-round), per=~1u, pw=~500n` | cap | `c=10p` | CORE |

Clock outer: MINUS = ground (primary pin name). Clock inner: cap MINUS = digital ground (primary pin name, e.g. GIOL).

### Digital domain — Supply and Ground pins

| Pin pattern | pin_type | domain | OUTER device | INNER device | INNER on |
|-------------|----------|--------|-------------|-------------|----------|
| Normal digital VDD (`VDDCLK`, `VDD_CKB`, etc.) | `power` | `digital` | vdc (actual V) | idc (~few mA, non-round) | duplicate |
| Normal digital ground (`VSSCLK`, `GND_CKB`, etc.) | `ground` | `digital` | PVSS (~0V) | — | — |
| High-voltage VDD (`PVDD2POC`) | `power` | `digital_hv` | vdc (actual V) | **noConn** (basic lib) | duplicate |
| High-voltage ground (`PVSS2DGZ`) | `ground` | `digital_hv` | PVSS (~0V) | **noConn** (basic lib) | duplicate |

**noConn**: `analogLib/noConn` symbol. Placed on the duplicate (right) side to indicate
the pin is intentionally left unconnected on the inner side. This prevents LVS warnings.

### Digital domain — GIOL (Digital ground device)

The digital ground device is conventionally named **GIOL** or **PVSS1DGZ**.
It is a PVSS (`vdc vdc=0`) whose PLUS is labeled with the pin name (e.g. `GIOL`),
and MINUS = `--GND` (global ground reference, NOT `gnd!`).
All digital inner-side device reference terminals connect to the primary digital
ground pin name (e.g. `GIOL`), NOT `--GND` or `gnd!`.

---

## Prefix -> pin_type Mapping (with domain)

### Power supply pins (`VDD*`, `VIO*`)

| Prefix | pin_type | domain | OUTER Stimulus | Typical value | Notes |
|--------|----------|--------|---------------|---------------|-------|
| `VDD*` | `power` | `analog` | vdc | 0.9V | Core supply |
| `VIOL` | `power` | `analog` | vdc | 0.9V | Low-voltage IO domain supply |
| `VIOH` | `power` | `analog` | vdc | 1.8V | High-voltage IO domain supply |
| `VDDCLK`, `VDD_CKB`, `VDDDAT` | `power` | `digital` | vdc | 0.9V | Digital block supply |
| `PVDD2POC` | `power` | `digital_hv` | vdc | per design | High-voltage digital supply |

### Ground pins (`VSS*`, `GND*`)

| Prefix | pin_type | domain | Stimulus | Notes |
|--------|----------|--------|----------|-------|
| `VSS*` (analog) | `ground` | `analog` | PVSS | Block-local ground |
| `GND*` (analog) | `ground` | `analog` | PVSS | Block-local ground |
| `VSS*`/`GND*` (digital) | `ground` | `digital` | PVSS → pin name (e.g. GIOL) | Digital ground domain |
| `PVSS2DGZ` | `ground` | `digital_hv` | PVSS → pin name | High-voltage digital ground |

### Bias current pins (`IB*`)

| Prefix | pin_type | domain | OUTER Stimulus | Typical value | Notes |
|--------|----------|--------|---------------|---------------|-------|
| `IB*` | `bias_current` | `analog` | **idc** | -10uA to -15uA | Current INTO the DUT |
| `IBUF*` | `bias_current` | `analog` | **idc** | +10uA to +120uA | Buffer bias |

**IMPORTANT**: `IB*` pins use `analogLib/idc`, NOT `analogLib/vdc`.
Negative current = flowing into the DUT pin.

### Reference voltage pins (`VREF*`)

| Prefix | pin_type | domain | OUTER Stimulus | Typical value | Notes |
|--------|----------|--------|---------------|---------------|-------|
| `VREFH` | `reference` | `analog` | vdc | 1.8V | High reference |
| `VREFM` | `reference` | `analog` | vdc | 1.4V | Mid reference |
| `VREFN` | `reference` | `analog` | vdc | 0.0V | Low reference |
| `VREFDES*` | `reference` | `analog` | vdc | 0.9V | Design reference |

**IMPORTANT**: Reference pins are NOT "no stimulus". They require specific DC voltage.

### Common-mode pins (`VCM*`, `VINCM*`)

| Prefix | pin_type | domain | OUTER Stimulus | Typical value |
|--------|----------|--------|---------------|---------------|
| `VCM*` | `reference` | `analog` | vdc | 0.45V (VIOL/2) |
| `VINCM*` | `reference` | `analog` | vdc | 0.45V |

### Differential analog input pins (`VIN*`)

| Prefix | pin_type | domain | OUTER Stimulus | Typical value |
|--------|----------|--------|---------------|---------------|
| `VINP` | `analog_input` | `analog` | vdc | 0.45V (biased at VCM) |
| `VINN` | `analog_input` | `analog` | vdc | 0.45V |

### Clock pins (`*CLK*`, `*CK*`, `SCK*`)

| Prefix | pin_type | domain | OUTER Stimulus | Typical value |
|--------|----------|--------|---------------|---------------|
| `*CLK*` | `clock` | `digital` | vpulse | 0→~VDD |

### Reset pins (`RST*`)

| Prefix | pin_type | domain | OUTER Stimulus | Typical value |
|--------|----------|--------|---------------|---------------|
| `RST*` | `reset` | `digital` | vpulse | 0→~VDD |

### Digital data pins (`D*`, `SDI`, `SDO`, `SLP`, `SYNC`, `GIO*`)

| Prefix | pin_type | domain | OUTER Stimulus | OUTER Load | Notes |
|--------|----------|--------|---------------|-----------|-------|
| `D[0-9]*` | `digital_bidirectional` | `digital` | vpulse | cap 10pF | Data bus |
| `SDI` | `digital_input` | `digital` | vpulse | — | SPI data in |
| `SDO` | `digital_output` | `digital` | — | cap 10pF | SPI data out |
| `SLP` | `digital_input` | `digital` | vpulse | — | Sleep control |
| `SYNC` | `digital_bidirectional` | `digital` | vpulse | cap 10pF | Synchronization |
| `GIO*` | `digital_bidirectional` | `digital` | vpulse | cap 10pF | GPIO pad |

### Core-side duplicates (`*_CORE`)

`*_CORE` pins share the same `pin_type` and `domain` as their base pin.
Their **inner device** is determined by the dual-side topology tables above.
The pipeline handles their placement on the right side of the symbol.

---

## Classification Priority

Classify in this order — first match wins:

1. **Power** — `VDD*`, `VIO*`, `PVDD*` prefixes
2. **Ground** — `VSS*`, `GND*`, `PVSS*` prefixes
3. **Bias current** — `IB*`, `IBUF*` prefixes (BEFORE analog — NOT analog_input!)
4. **Reference** — `VREF*`, `VCM*`, `VINCM*` prefixes
5. **Clock** — `*CLK*`, `*CK*`, `SCK*` prefixes
6. **Reset** — `RST*` prefix
7. **Analog input** — `VINP`, `VINN` prefixes
8. **Digital by direction** — `SDI`→input, `SDO`→output, `D*`/`GIO*`/`SYNC`→bidirectional
9. **Direction-based fallback** — input→digital_input, output→digital_output, inputOutput→digital_bidirectional

---

## Testbench Topology Rules

### Rule 1: PVSS ground device — every ground pin gets a PVSS, not `gnd!`

Place a `vdc` instance (cell = `vdc`, param `vdc=0`) for each ground pin.
The PVSS PLUS terminal is labeled with the **original pin name** (e.g. `GND_DAT`);
MINUS connects to `--GND` (global ground reference via GND_REF).

Each ground pin gets its own PVSS instance named after the pin.
The `ground_net` field in classification is internal — used for grouping pins
that belong to the same ground domain, but **never used as a schematic label**.

```
GND_DAT:   analogLib/vdc  params={vdc=0}
  PLUS  → net "GND_DAT"   (same as DUT pin label — label-based wiring)
  MINUS → net "--GND"     (global ground reference)
```

### Rule 2: Ground domain grouping and source MINUS connections

Pins in the same functional block share the same **ground domain**, identified
by the `ground_net` field (e.g., `gnd_DAT`, `dgnd`). This field is used
internally by the program to determine which primary ground pin name to use
for source/load MINUS terminal labels.

Extract the **block identifier** from pin names by stripping the type prefix
(`VDD`/`GND`/`VSS`/`IB`/`VCM`/`VREF`/`VIN`) and keeping the suffix.

| Pin names | Block ID | ground_net (internal) | Source MINUS label |
|-----------|----------|-----------------------|-------------------|
| `GND_DAT`, `GND_DAT_CORE`, `VDD_DAT`, `VDD_DAT_CORE` | `DAT` | `gnd_DAT` | `GND_DAT` |
| `GND_CKB`, `GND_CKB_CORE`, `VDD_CKB`, `VDD_CKB_CORE` | `CKB` | `gnd_CKB` | `GND_CKB` |
| `VSSCLK`, `VDDCLK`, `DCLK` | `CLK` | `gnd_CLK` | `VSSCLK` |
| `VSSSAR`, `VDDSAR` | `SAR` | `gnd_SAR` | `VSSSAR` |
| `VSSIB`, `VSSIB_CORE`, `VDDIB`, `VDDIB_CORE`, `IBUF_IBIAS` | `IB` | `gnd_IB` | `VSSIB` |
| `VSS` | (top-level) | `gnd_VSS` | `VSS` |
| `GIOL`, `VSSCLK`, `GND_CKB` (digital) | — | `dgnd` | `GIOL` |
| `PVSS2DGZ` (HV digital) | — | `dgnd_hv` | `PVSS2DGZ` |

All non-ground pins in the same block connect their source/load reference
terminals (MINUS of vdc/idc/vpulse) to the block's **primary ground pin name**, NOT to `--GND`.

Example: in the `DAT` block:
- `GND_DAT` PVSS: PLUS = `GND_DAT`, MINUS = `--GND`
- `VDD_DAT` source: PLUS = `VDD_DAT`, MINUS = `GND_DAT` (NOT `--GND`)
- `GND_DAT` DUT pin label: `GND_DAT` (original pin name, NOT `gnd_DAT`)

### Rule 3: Digital ground is separate

All digital-domain inner-side device reference terminals connect to the
**primary digital ground pin name** (e.g., `GIOL`), provided by the GIOL/PVSS1DGZ
PVSS instance. This is a separate PVSS from analog block grounds.

```
GIOL:  analogLib/vdc  params={vdc=0}
  PLUS  → net "GIOL"      (digital ground, matches DUT pin label)
  MINUS → net "--GND"     (global reference)
```

### Rule 4: Global ground reference — GND_REF

A `GND_REF` instance bridges the `--GND` local net to the `gnd!` global ground.
All PVSS MINUS terminals connect to `--GND`, which goes through GND_REF to `gnd!`.

```
GND_REF:  analogLib/vdc  params={vdc=0}
  PLUS  → net "--GND"     (local ground bus)
  MINUS → net "gnd!"      (Cadence global ground, node 0)
```

### Rule 5: Cross-block pins without an explicit ground

Some blocks may have VDD pins but no explicit GND pin (e.g., only `VDD3` without
`GND3`). In this case:
- If the block suffix matches an existing PVSS, use that local ground
- If no matching PVSS exists, create a new `PVSS_{block}` and use it

### Rule 6: Top-level ground pins

Pins with no block suffix (e.g., plain `VSS`) get their own `PVSS` instance
named after the pin (e.g., `VSS`) with PLUS = `VSS`, MINUS = `--GND`.

---

## Classification Output — Full Example

**Note on `ground_net`**: This field is for internal program use only — it
groups pins that belong to the same ground domain. It is **never used as a
schematic label**. All DUT pin labels, PVSS PLUS labels, and source MINUS
labels use the **original pin name** (e.g., `GIOL`, `GND_DAT`), not the
ground_net value (e.g., `dgnd`, `gnd_DAT`).

### Analog block pin

```json
{
  "name": "VDD_DAT",
  "pin_type": "power",
  "domain": "analog",
  "confidence": 0.98,
  "reason": "VDD prefix, DAT block, analog domain, 0.9V core supply",
  "stimulus": "vdc",
  "stimulus_params": {"vdc": "0.9"},
  "load": null,
  "load_params": null,
  "inner_stimulus": "idc",
  "inner_params": {"idc": "2.7m"},
  "ground_net": "gnd_DAT"
}
```

### Analog bias current pin

```json
{
  "name": "IB3",
  "pin_type": "bias_current",
  "domain": "analog",
  "confidence": 0.95,
  "reason": "IB prefix = bias current, idc not vdc, block=3",
  "stimulus": "idc",
  "stimulus_params": {"idc": "-10u"},
  "load": null,
  "load_params": null,
  "inner_stimulus": "vdc",
  "inner_params": {"vdc": "0.37"},
  "ground_net": "gnd_3"
}
```

### Digital output pin (outer=cap, inner=vpulse)

```json
{
  "name": "D0",
  "pin_type": "digital_output",
  "domain": "digital",
  "confidence": 0.90,
  "reason": "D prefix = data, direction=output, digital domain",
  "stimulus": null,
  "stimulus_params": null,
  "load": "cap",
  "load_params": {"c": "10p"},
  "inner_stimulus": "vpulse",
  "inner_params": {"v1": "0", "v2": "1.72", "per": "7n", "tr": "0.1n", "tf": "0.1n", "pw": "3.5n"},
  "ground_net": "dgnd"
}
```

### Digital input pin (outer=vpulse, inner=cap)

```json
{
  "name": "SDI",
  "pin_type": "digital_input",
  "domain": "digital",
  "confidence": 0.95,
  "reason": "SDI = serial data in, digital input",
  "stimulus": "vpulse",
  "stimulus_params": {"v1": "0", "v2": "0.87", "per": "7n", "tr": "0.1n", "tf": "0.1n", "pw": "3.5n"},
  "load": null,
  "load_params": null,
  "inner_stimulus": "cap",
  "inner_params": {"c": "10p"},
  "ground_net": "dgnd"
}
```

### Digital high-voltage supply pin

```json
{
  "name": "PVDD2POC",
  "pin_type": "power",
  "domain": "digital_hv",
  "confidence": 0.98,
  "reason": "PVDD prefix, high-voltage digital supply, inner=noConn",
  "stimulus": "vdc",
  "stimulus_params": {"vdc": "3.3"},
  "load": null,
  "load_params": null,
  "inner_stimulus": "noConn",
  "inner_params": null,
  "ground_net": "dgnd_hv"
}
```

### Ground pin

```json
{
  "name": "GND_DAT",
  "pin_type": "ground",
  "domain": "analog",
  "confidence": 0.98,
  "reason": "GND prefix, DAT block ground, PVSS labeled GND_DAT",
  "stimulus": null,
  "stimulus_params": null,
  "load": null,
  "load_params": null,
  "inner_stimulus": null,
  "inner_params": null,
  "ground_net": "gnd_DAT"
}
```

### Reference pin (voltage source + inner compliance)

```json
{
  "name": "VREFH",
  "pin_type": "reference",
  "domain": "analog",
  "confidence": 0.95,
  "reason": "VREFH = high reference voltage 1.8V, inner=idc compliance",
  "stimulus": "vdc",
  "stimulus_params": {"vdc": "1.8"},
  "load": null,
  "load_params": null,
  "inner_stimulus": "idc",
  "inner_params": {"idc": "3.4m"},
  "ground_net": "gnd"
}
```

---

## Confidence Levels

When classifying, assign a confidence score:

| Score | Meaning |
|-------|---------|
| 0.9–1.0 | Exact prefix match, unambiguous type, domain, and topology |
| 0.7–0.9 | Prefix match with some ambiguity (e.g., `SYNC` could be digital or analog) |
| 0.5–0.7 | Direction-based fallback, no prefix clue |
| < 0.5 | Wild guess — flag for user review |

---

## Custom Overrides

If the user provides additional context (e.g., "pin X is actually a clock",
"VDD is 3.3V for this design"), override the rule-based classification and
note it in the `reason` field.
