# IO Ring Simulation Configuration Rules

Rules for generating a Spectre simulation deck for IO Ring testbenches.
The LLM reads this file plus `pin_info.json` / `pin_classifications.json` and produces `sim_config.json`.

---

## Core Rules (Non-Negotiable)

1. **No design variables.** Never emit `design_vars` or `parameters`. All voltage/current values come
   directly from `pin_classifications.json` stimulus params — they are already fixed numbers.

2. **No AC analysis.** IO Ring cells do not need frequency response. Only DC and transient.

3. **Always run both DC and transient.** DC first (sets operating point), transient second.
   This applies to both analog and digital IO cells — no exceptions.

4. **Power is always calculated from transient.** Use `integ(pwr(...), t_start, tstop) / tstop`
   to get average power per device. Never estimate from DC.

---

## Analysis Order and Settings

### 1. DC Operating Point

```spectre
dcOp dc
```

- **No sweep parameter.** Just an operating point — all sources are at their DC values.
- Runs first to establish initial conditions for transient.
- Applied to both analog and digital cells.

### 2. Transient

```spectre
tran tran stop=<tstop> errpreset=moderate
```

- `stop`: should be long enough to see at least 10 full cycles of the slowest `vpulse` stimulus.
  Compute from stimulus params: `tstop = 10 × max(per)` across all `vpulse` sources.
  Minimum: `100n`. Maximum: `10u`. Round to a clean value (e.g. `500n`, `1u`).
- `errpreset=moderate` for digital-dominant cells; `errpreset=conservative` for analog-dominant.
  A cell is "analog-dominant" if more than half its non-ground pins are `analog_*`, `reference`, or `bias_current` type.
- `maxstep`: set to `tstop / 1000` (Spectre default is often too coarse for IO switching).

---

## Model Includes

**Do not specify model includes in `sim_config.json`.** Leave `model_includes: []`.

Phase B injects the full include list automatically from `.env` after loading your config.
The generated deck will contain these lines (one `include` per section):

```spectre
include "/home/process/tsmc28n/PDK_mmWave/iPDK_CRN28HPC+ULL_v1.8_2p2a_20190531/tsmcN28/../models/spectre/crn28ull_1d8_elk_v1d8_2p2_shrink0d9_embedded_usage.scs" section=pre_simu
include "..." section=ttmacro_mos_moscap
include "..." section=tt_res_bip_dio_disres
include "..." section=tt_mom
include "..." section=tt_ind_jvar
include "..." section=tt_r_metal
include "..." section=noise_worst
simulator lang=spice
.include "/home/process/tsmc28n/IO/tphn28hpcpgv18_170a/0971001_20180621/tphn28hpcpgv18_110a_spi/TSMCHOME/digital/Back_End/spice/tphn28hpcpgv18_110a/tphn28hpcpgv18.spi"
simulator lang=spectre
```

All 7 core sections plus the IO pad SPICE model are required for IO Ring. Omitting the IO model
causes undefined subcircuit errors for pad cells.

---

## Save Signals

```spectre
saveOptions options save=allpub
```

Always use `allpub` for IO Ring. This saves all top-level node voltages and branch currents,
which are needed for power calculation. Do not use a selective save list — you cannot know in
advance which internal nodes the power expressions will reference.

---

## Power Calculation (Required Output Expressions)

Power must be computed from transient using the `pwr()` function and `integ()`.

### Per-Device Average Power

For every stimulus/load instance placed in the TB (every `SRC_*`, `LOAD_*`, `INNER_*`, `PVSS` device):

```spectre
<device_name>_pwr = integ(pwr(<device_name>), 0, <tstop>) / <tstop>
```

- `pwr(instance_name)` returns the instantaneous power drawn by that instance (watts).
- `integ(..., 0, tstop) / tstop` gives average power over the simulation window.
- Name the output `<instance_name>_pwr` (e.g. `SRC_VDD_pwr`, `INNER_D0_pwr`).

### DUT Supply Current (optional, for cross-check)

```spectre
idd = integ(-i(SRC_VDD:PLUS), 0, <tstop>) / <tstop>
```

Where `SRC_VDD` is the stimulus instance for the VDD pin. The minus sign accounts for
Spectre's current direction convention (positive current flows into PLUS terminal).

---

## sim_config.json Schema (Produced by LLM)

```json
{
  "analyses": [
    {
      "name": "dcOp",
      "type": "dc",
      "enabled": true
    },
    {
      "name": "tran",
      "type": "tran",
      "enabled": true,
      "stop": "<computed tstop, e.g. 500n>",
      "maxstep": "<tstop/1000, e.g. 500p>",
      "errpreset": "moderate"
    }
  ],
  "model_includes": [],
  "save_default": "allpub",
  "outputs": [
    {
      "name": "SRC_VDD_pwr",
      "expression": "integ(pwr(SRC_VDD), 0, <tstop>) / <tstop>",
      "eval_type": "wave",
      "from_analysis": "tran"
    }
  ]
}
```

Rules for field values:
- `model_includes`: leave empty `[]` — the deck builder injects paths from `.env` automatically.
- `eval_type`: always `"wave"` for power expressions (they are time-domain waveforms from tran).
- `from_analysis`: always `"tran"` for power expressions; `"dcOp"` for any DC operating-point readout.
- `outputs`: list every `SRC_*`, `LOAD_*`, `INNER_*` device individually. Skip `GND_REF` and PVSS devices — they are reference sources, not power consumers.

---

## Determining `tstop`

1. Collect all `per` values from `vpulse` stimulus params across all pins.
2. `tstop = 10 × max(per)`. If no vpulse sources exist (pure analog cell), use `500n`.
3. Clamp to `[100n, 10u]`.
4. Round to a readable value: prefer multiples of 100n or 500n.

Example: if `per` values are `7n`, `10n`, `14n` → `tstop = 10 × 14n = 140n` → round to `200n`.

---

## Simulator Options (Standard)

Always emit these in the deck header:

```spectre
simulatorOptions options reltol=1e-4 vabstol=1e-6 iabstol=1e-12 gmin=1e-12 temp=27.0 tnom=27.0
```

Do not change these unless the user explicitly asks. Do not add `pivrel` — it conflicts with some
IO pad model formulations.

---

## What NOT to Include

| Item | Reason |
|------|--------|
| `design_vars` / `parameters` block | No design variables in IO Ring TB |
| `ac` analysis | Not applicable to IO Ring |
| DC sweep (`param=VDD start=0 stop=3`) | No sweep — fixed operating point only |
| `save VIN VOUT ...` specific signals | Use `allpub` instead |
| Signal-level output expressions (e.g. gain, bandwidth) | Power only — no signal-processing metrics |
| `info what=models ...` blocks | Optional, omit unless user asks for model audit |
