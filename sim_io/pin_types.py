"""
Pin Classification Module for SIM-IO Simulation Flow.

Extracted from sim_flow.py to support LLM-driven pin type classification.
The classification rules live in the skill reference docs — this module
provides the data structures, fallback heuristic, and the loader that
reads LLM-generated classification JSON.

Two modes of operation:
  1. LLM mode:  load_pin_classifications() reads a JSON file produced by
                the LLM skill → returns PinClassification for each pin.
  2. Fallback:  classify_pin_heuristic() uses the original name-matching
                heuristic when no LLM output is available.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Optional


# ── Pin Type Enum ─────────────────────────────────────────────

class PinType(str, Enum):
    """Pad/signal type that determines stimulus/load behavior."""
    POWER = "power"
    GROUND = "ground"
    DIGITAL_INPUT = "digital_input"
    DIGITAL_OUTPUT = "digital_output"
    DIGITAL_BIDIRECTIONAL = "digital_bidirectional"
    # Extended types — LLM can assign these when rules are populated
    ANALOG_INPUT = "analog_input"
    ANALOG_OUTPUT = "analog_output"
    ANALOG_BIDIRECTIONAL = "analog_bidirectional"
    REFERENCE = "reference"
    CLOCK = "clock"
    RESET = "reset"
    BIAS_CURRENT = "bias_current"
    NO_CONNECT = "no_connect"


# ── Data Structures ──────────────────────────────────────────

@dataclass
class PinInfo:
    name: str
    direction: str      # "input" / "output" / "inputOutput"
    x: float
    y: float
    side: str           # "left" / "right" / "top" / "bottom"


@dataclass
class PinClassification:
    """Single pin classification result from LLM or heuristic."""
    name: str
    pin_type: str           # PinType value
    confidence: float       # 0.0–1.0, LLM self-assessed
    reason: str             # Why this classification was chosen
    domain: str = ""        # "analog" | "digital" | "digital_hv"
    stimulus: Optional[str] = None   # Outer stimulus cell: vdc, vpulse, idc
    stimulus_params: Optional[dict] = None  # Outer stimulus params
    load: Optional[str] = None       # Outer load cell: cap, res
    load_params: Optional[dict] = None       # Outer load params
    inner_stimulus: Optional[str] = None    # Inner (CORE) device: vdc, idc, vpulse, cap, noConn
    inner_params: Optional[dict] = None     # Inner device params
    inner_load: Optional[str] = None        # Inner load cell (for bidirectional): cap, res
    inner_load_params: Optional[dict] = None  # Inner load params
    ground_net: Optional[str] = None        # Local ground net (e.g. gnd_DAT, dgnd)


@dataclass
class ClassificationResult:
    """Full classification output for all pins in a design."""
    lib: str
    cell: str
    vdd_value: float
    pins: list[PinClassification]
    vio_low: float = 0.9
    vio_high: float = 1.8
    llm_model: str = ""
    timestamp: str = ""


# ── Stimulus/Load Rules ──────────────────────────────────────

# Default pad rules keyed by PinType value.
# "VDD" in params is replaced at runtime by the vdd_value arg.
PAD_RULES: dict[str, dict] = {
    "digital_input": {
        "source": {
            "lib": "analogLib", "cell": "vpulse",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"v1": "0", "v2": "VDD", "per": "100n",
                       "tr": "1n", "tf": "1n", "pw": "50n"},
        },
    },
    "digital_output": {
        "load": {
            "lib": "analogLib", "cell": "cap",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"c": "10p"},
        },
    },
    "digital_bidirectional": {
        "source": {
            "lib": "analogLib", "cell": "vpulse",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"v1": "0", "v2": "VDD", "per": "100n",
                       "tr": "1n", "tf": "1n", "pw": "50n"},
        },
        "load": {
            "lib": "analogLib", "cell": "cap",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"c": "10p"},
        },
    },
    "power": {
        "source": {
            "lib": "analogLib", "cell": "vdc",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"vdc": "VDD"},
        },
    },
    "ground": {
        "source": {
            "lib": "analogLib", "cell": "vdc",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"vdc": "0"},
        },
    },
    # Extended types — stimulus rules to be filled by user
    "clock": {
        "source": {
            "lib": "analogLib", "cell": "vpulse",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"v1": "0", "v2": "VDD", "per": "100n",
                       "tr": "0.1n", "tf": "0.1n", "pw": "50n"},
        },
    },
    "reset": {
        "source": {
            "lib": "analogLib", "cell": "vpulse",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"v1": "0", "v2": "VDD", "per": "1u",
                       "tr": "1n", "tf": "1n", "pw": "500n"},
        },
    },
    "bias_current": {
        "source": {
            "lib": "analogLib", "cell": "idc",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"idc": "-10u"},
        },
    },
    "analog_input": {
        "source": {
            "lib": "analogLib", "cell": "vdc",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"vdc": "VDD/2"},
        },
    },
    "analog_output": {
        "load": {
            "lib": "analogLib", "cell": "cap",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"c": "1p"},
        },
    },
    "analog_bidirectional": {
        "source": {
            "lib": "analogLib", "cell": "vdc",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"vdc": "VDD/2"},
        },
        "load": {
            "lib": "analogLib", "cell": "cap",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"c": "1p"},
        },
    },
    "reference": {},      # No stimulus/load for reference pins
    "no_connect": {},     # No stimulus/load for NC pins
}


# ── Side Configs for Label Placement ─────────────────────────

SIDE_CONFIGS = {
    "right": {
        "extend_x": 0.750, "extend_y": 0.0,
        "label_offset_x": 0.25, "label_offset_y": 0.0,
        "label_align": "lowerCenter", "label_rotation": "R0",
    },
    "left": {
        "extend_x": -0.750, "extend_y": 0.0,
        "label_offset_x": -0.25, "label_offset_y": 0.0,
        "label_align": "lowerCenter", "label_rotation": "R0",
    },
    "top": {
        "extend_x": 0.0, "extend_y": 0.750,
        "label_offset_x": 0.0, "label_offset_y": 0.25,
        "label_align": "lowerCenter", "label_rotation": "R0",
    },
    "bottom": {
        "extend_x": 0.0, "extend_y": -0.750,
        "label_offset_x": 0.0, "label_offset_y": -0.25,
        "label_align": "lowerCenter", "label_rotation": "R0",
    },
}


# ── Fallback Heuristic ──────────────────────────────────────

def classify_pin_heuristic(pin: PinInfo) -> str:
    """Original name-matching heuristic — used when no LLM output exists.

    Returns one of: power, ground, digital_input, digital_output,
    digital_bidirectional.
    """
    name_upper = pin.name.upper()
    if any(kw in name_upper for kw in ("VDD", "VCC", "DVDD", "AVDD")):
        return "power"
    if any(kw in name_upper for kw in ("VSS", "GND", "DVSS", "AVSS")):
        return "ground"
    if name_upper.startswith("IB") or name_upper.startswith("IBUF"):
        return "bias_current"
    if pin.direction == "input":
        return "digital_input"
    if pin.direction == "output":
        return "digital_output"
    return "digital_bidirectional"


# ── LLM Classification Loader ───────────────────────────────

def load_pin_classifications(path: str | Path) -> ClassificationResult:
    """Load LLM-generated pin classification JSON.

    Expected schema (see skill scripts/pin_classify_schema.json):
    {
      "lib": "...",
      "cell": "...",
      "vdd_value": 1.8,
      "llm_model": "...",
      "timestamp": "...",
      "pins": [
        {"name": "DIN0", "pin_type": "digital_input", "confidence": 0.95,
         "reason": "...", "stimulus": null, "load": null},
        ...
      ]
    }
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    pins = [
        PinClassification(
            name=p["name"],
            pin_type=p["pin_type"],
            confidence=p.get("confidence", 0.0),
            reason=p.get("reason", ""),
            domain=p.get("domain", ""),
            stimulus=p.get("stimulus"),
            stimulus_params=p.get("stimulus_params"),
            load=p.get("load"),
            load_params=p.get("load_params"),
            inner_stimulus=p.get("inner_stimulus"),
            inner_params=p.get("inner_params"),
            inner_load=p.get("inner_load"),
            inner_load_params=p.get("inner_load_params"),
            ground_net=p.get("ground_net"),
        )
        for p in data.get("pins", [])
    ]
    return ClassificationResult(
        lib=data.get("lib", ""),
        cell=data.get("cell", ""),
        vdd_value=data.get("vdd_value", 1.8),
        pins=pins,
        vio_low=data.get("vio_low", 0.9),
        vio_high=data.get("vio_high", 1.8),
        llm_model=data.get("llm_model", ""),
        timestamp=data.get("timestamp", ""),
    )


def write_pin_info_json(
    pins: list[PinInfo],
    lib: str,
    cell: str,
    vdd_value: float,
    path: str | Path,
) -> None:
    """Write pin info to JSON for the LLM skill to read and classify.

    This is the INPUT that gets handed to the LLM.
    """
    data = {
        "lib": lib,
        "cell": cell,
        "vdd_value": vdd_value,
        "pins": [
            {
                "name": p.name,
                "direction": p.direction,
                "x": p.x,
                "y": p.y,
                "side": p.side,
            }
            for p in pins
        ],
        # Context hints the LLM can use
        "available_pin_types": [t.value for t in PinType],
    }
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def get_rule_for_pin(
    classification: PinClassification,
) -> dict:
    """Look up PAD_RULES for a classified pin.

    If the LLM specified stimulus/load overrides, those take precedence.
    """
    rule = dict(PAD_RULES.get(classification.pin_type, {}))
    return rule


def build_classification_map(
    result: ClassificationResult,
) -> dict[str, PinClassification]:
    """Build name → PinClassification lookup from LLM result."""
    return {pc.name: pc for pc in result.pins}
