# Pin Classification Rules

This document defines the rules for classifying IO pins by type.
The LLM reads these rules alongside `pin_info.json` to produce `pin_classifications.json`.

> **Status**: This is a scaffold. The user will fill in detailed rules for their
> specific technology / design convention. The sections below show the expected
> structure and provide starter examples.

---

## Available Pin Types

| Type | Description |
|------|-------------|
| `power` | Supply voltage pins (core or IO) |
| `ground` | Ground / substrate pins |
| `digital_input` | Digital signal input |
| `digital_output` | Digital signal output |
| `digital_bidirectional` | Digital tri-state or bidirectional |
| `analog_input` | Analog signal input |
| `analog_output` | Analog signal output |
| `analog_bidirectional` | Analog bidirectional |
| `clock` | Clock signal |
| `reset` | Reset signal (active high or low) |
| `reference` | Voltage/current reference |
| `no_connect` | Unconnected / reserved pin |

---

## Classification Priority

Classify in this order — first match wins:

1. **Power / Ground** — supply nets (see naming patterns below)
2. **Clock** — clock signals
3. **Reset** — reset signals
4. **Analog vs Digital** — distinguish by naming or direction
5. **Input / Output / Bidirectional** — use terminal direction

---

## Rule 1: Power Pins

<!-- USER: Fill in your technology-specific power pin naming conventions -->

**Keywords**: `VDD`, `VCC`, `DVDD`, `AVDD`, `VDDIO`, `VDDCORE`, ...

**Examples**:
| Pin name | Direction | → Type |
|----------|-----------|--------|
| VDD | inputOutput | power |
| DVDD | inputOutput | power |
| AVDD | inputOutput | power |
| VDDIO | inputOutput | power |

---

## Rule 2: Ground Pins

<!-- USER: Fill in your technology-specific ground pin naming conventions -->

**Keywords**: `VSS`, `GND`, `DVSS`, `AVSS`, `VSSIO`, `VSSCORE`, ...

**Examples**:
| Pin name | Direction | → Type |
|----------|-----------|--------|
| VSS | inputOutput | ground |
| DVSS | inputOutput | ground |
| GND | inputOutput | ground |

---

## Rule 3: Clock Pins

<!-- USER: Fill in clock pin patterns -->

**Keywords**: `CLK`, `CK`, `CLOCK`, ...

**Examples**:
| Pin name | Direction | → Type |
|----------|-----------|--------|
| CLK | input | clock |
| SYS_CLK | input | clock |
| REF_CLK | input | clock |

---

## Rule 4: Reset Pins

<!-- USER: Fill in reset pin patterns -->

**Keywords**: `RST`, `RESET`, `RST_N`, ...

**Examples**:
| Pin name | Direction | → Type |
|----------|-----------|--------|
| RST | input | reset |
| RST_N | input | reset |
| RESET | input | reset |

---

## Rule 5: Analog vs Digital

<!-- USER: Define how to distinguish analog from digital pins in your design -->

**Analog keywords**: `A_`, `ANA`, `VIN`, `VOUT`, `VBG`, `VREF`, ...
**Digital keywords**: `D_`, `DIN`, `DOUT`, `DATA`, `EN`, `CS`, ...

**When no keyword matches**: fall back to terminal direction alone.

---

## Rule 6: Direction-Based Fallback

If no naming pattern matches, use the terminal direction:
- `input` → `digital_input`
- `output` → `digital_output`
- `inputOutput` → `digital_bidirectional`

---

## Confidence Levels

When classifying, assign a confidence score:

| Score | Meaning |
|-------|---------|
| 0.9–1.0 | Exact keyword match, unambiguous |
| 0.7–0.9 | Pattern match with high confidence |
| 0.5–0.7 | Direction-based fallback, no keyword clue |
| < 0.5 | Wild guess — flag for user review |

---

## Custom Overrides

If the user provides additional context (e.g., "pin X is actually a clock"),
override the rule-based classification and note it in the `reason` field.
