# Simulation Configuration Rules

This document defines the rules for generating a Spectre simulation deck configuration.
The LLM reads this along with `sim_config_input.json` and produces `sim_config.json`.

## Spectre Deck Structure

A complete Spectre deck has these sections in order:

```
simulator lang=spectre
global 0
parameters VDD=1.8
include "model_file.scs" section=section_name
simulatorOptions options reltol=1e-4 ...
info what=models where=rawfile
dc dc param=VDD start=0 stop=3 lin=100
save /VOUT /VIP /VIN
saveOptions options save=allpub
```

## Design Variables

Syntax: `parameters NAME=VALUE NAME2=VALUE2`

- `VDD` is the most common design variable, used by voltage sources like `vsource dc=VDD`
- The value should match the `vdd_value` from the input
- Other variables may appear in instance parameters (check the netlist)

## Model Includes

TSMC28 uses a single model file with multiple sections. Always include at minimum:

| Section | Purpose | Required? |
|---------|---------|-----------|
| `pre_simu` | Pre-simulation setup, model definitions | **Always** |
| `noise_worst` | Noise models | For noise/ac analysis |
| `ttmacro_mos_moscap` | TT corner MOS + MOSCAP models | **Always** |
| `tt_res_bip_dio_disres` | TT corner resistor/bipolar/diode | If circuit has R/B/D |
| `tt_mom` | MOM capacitor models | If circuit has MOM caps |
| `tt_ind_jvar` | Inductor/junction varactor models | If circuit has inductors |
| `tt_r_metal` | Metal resistor models | For parasitic-aware sim |

The model file path is provided in `pdk_info.model_file` of the input.

## Analysis Types

### DC Analysis
```
dc dc param=VDD start=0 stop=3 lin=100
```
- Used for: DC operating point, DC sweep of supply voltage
- `param`: the design variable or parameter to sweep
- `start/stop`: sweep range
- `lin`: number of linear steps
- When to use: amplifiers (DC gain), digital cells (transfer curve)

### Transient Analysis
```
tran tran stop=1u errpreset=moderate
```
- `stop`: simulation end time
- `errpreset`: accuracy level — `liberal` (fast), `moderate` (balanced), `conservative` (accurate)
- When to use: digital switching, IO ring transient, settling time

### AC Analysis
```
ac ac start=1 stop=1000M dec=1000
```
- `start/stop`: frequency range (Hz)
- `dec`: points per decade
- Requires an AC stimulus: a voltage source with `mag=1` or `acm=1`
- When to use: amplifier gain/phase, bandwidth, stability

### Noise Analysis
```
noise (VOUT 0) vsrc=V3 start=1 stop=1000M dec=100
```
- Requires output node and input source specification
- When to use: low-noise amplifier, input-referred noise

## Choosing Analyses Based on Pin Types and Intent

| Circuit Type | Primary Analysis | Secondary Analysis |
|---|---|---|
| Amplifier (analog_input → analog_output) | DC sweep + AC | Tran (settling) |
| IO Ring (digital_bidirectional) | Tran | DC operating point |
| Digital block | Tran | DC (transfer curve) |
| ADC/DAC | Tran + FFT | DC |
| PLL | Tran (lock time) | AC (loop gain) |
| Voltage reference | DC sweep | Temperature sweep |

## Save Signals

- For specific signals: `save VOUT VIP VIN` (no slash prefix in save statements)
- In sim_config.json, use slash prefix for signal paths (e.g. `"/VOUT"`) — the deck builder strips it
- For all public signals: `saveOptions options save=allpub`
- For everything including internal nodes: `saveOptions options save=all`
- Use specific saves when you know which signals matter for output expressions
- Use `allpub` as a safe default when you're not sure

## Output Expressions

These define named measurements computed from simulation results:

```
save Av_big = VOUT / (VIP - VIN)
```

- `eval_type=point`: scalar value (for DC analysis)
- `eval_type=wave`: waveform (for TRAN/AC analysis)
- Use top-level net names without slash prefix in expressions: `VOUT`, not `/VOUT`

## Info Statements

Standard set (always include these):
```
modelParameter info what=models where=rawfile
element info what=inst where=rawfile
outputParameter info what=output where=rawfile
designParamVals info what=parameters where=rawfile
primitives info what=primitives where=rawfile
subckts info what=subckts where=rawfile
```

## Simulator Options

Default values (override if user specifies):
```
reltol=1e-4
vabstol=1e-6
iabstol=1e-12
gmin=1e-12
temp=27.0
tnom=27.0
pivrel=1e-3
```

- For transient with digital signals: use `errpreset=moderate`
- For precision analog (ADC, PLL): use `errpreset=conservative`
- For quick DC sweep: `reltol=1e-3` is acceptable

## Safety Rules

1. **Always** include `global 0` — Spectre needs a ground reference
2. **Always** include `pre_simu` and `ttmacro_mos_moscap` sections for TSMC28
3. **Always** declare design variables that the netlist references (e.g. `VDD`)
4. DC analysis should run **before** AC or TRAN (sets operating point)
5. If a voltage source has `mag=1` or `acm=1`, AC analysis is expected
6. If a voltage source is `vpulse`, TRAN analysis is expected
7. The `user_intent` field takes priority over heuristic pin-type rules

## Common Configurations

### 5T Amplifier (DC gain + AC frequency response)
```json
{
  "design_vars": [{"name": "VDD", "expression": "1.8"}],
  "analyses": [
    {"name": "dc", "enabled": true, "sweep": {"param": "VDD", "start": "0", "stop": "3", "lin": "100"}},
    {"name": "ac", "enabled": true, "sweep": {"param": "freq", "start": "1", "stop": "1000M", "dec": "1000"}}
  ],
  "save_signals": [{"signal": "/VIP"}, {"signal": "/VIN"}, {"signal": "/VOUT"}],
  "outputs": [{"name": "Av_big", "expression": "VOUT / (VIP - VIN)", "eval_type": "point"}]
}
```

### IO Ring (Transient switching test)
```json
{
  "design_vars": [{"name": "VDD", "expression": "1.8"}],
  "analyses": [
    {"name": "tran", "enabled": true, "stop": "100n", "errpreset": "moderate"}
  ],
  "save_signals": [],
  "save_default": "allpub"
}
```
