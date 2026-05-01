"""
Simulation Deck Template — SimConfig + deck builder for Spectre.

Takes an si-exported netlist (circuit only) and appends:
  - Model include
  - Simulator options
  - Analysis (tran/dc)
  - Save signals

Produces a complete, ready-to-run Spectre deck.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SimConfig:
    """Configurable parameters for the simulation deck."""

    model_include: str = ""           # PDK model file path on remote server
    model_section: str = "TT"         # Process corner section
    analysis: str = "tran"            # "tran" | "dc"
    stop: str = "10u"                 # tran stop time
    errpreset: str = "moderate"       # "liberal" | "moderate" | "conservative"
    temperature: float = 27.0         # Temperature in Celsius
    save_signals: str = "allpub"      # "allpub" | "all"
    reltol: float = 1e-4
    vabstol: float = 1e-6
    iabstol: float = 1e-12
    gmin: float = 1e-12
    extra_analyses: list[str] = field(default_factory=list)  # Additional analysis lines
    extra_options: list[str] = field(default_factory=list)   # Additional option lines


# ── Deck Builder ────────────────────────────────────────────────

_SEPARATOR = "// === si-generated circuit netlist (DO NOT EDIT above this line) ==="


def build_sim_deck(
    netlist_text: str,
    config: SimConfig,
) -> str:
    """Append simulation commands to an si-exported netlist.

    The si netlist contains only the circuit (subckts, instances, global).
    We append model include, simulator options, analysis, and save commands
    to make it a complete Spectre deck.

    Returns the full deck as a string.
    """
    lines: list[str] = []

    # Original netlist
    lines.append(netlist_text.rstrip())
    lines.append("")
    lines.append(_SEPARATOR)
    lines.append("")

    # Model include
    if config.model_include:
        section = f" section={config.model_section}" if config.model_section else ""
        lines.append(f'include "{config.model_include}"{section}')
        lines.append("")

    # Simulator options
    lines.append(
        f"simulatorOptions options reltol={config.reltol} "
        f"vabstol={config.vabstol} iabstol={config.iabstol} "
        f"temp={config.temperature} tnom={config.temperature} "
        f"gmin={config.gmin}"
    )
    for opt in config.extra_options:
        lines.append(opt)
    lines.append("")

    # Analysis
    if config.analysis == "tran":
        lines.append(f"tran tran stop={config.stop} errpreset={config.errpreset}")
    elif config.analysis == "dc":
        lines.append("dc dc")
    else:
        lines.append(f"{config.analysis} {config.analysis}")
    for extra in config.extra_analyses:
        lines.append(extra)
    lines.append("")

    # Save signals
    lines.append(f"saveOptions options save={config.save_signals}")

    return "\n".join(lines) + "\n"


def build_sim_deck_from_file(
    netlist_path: str | Path,
    config: SimConfig,
) -> str:
    """Read an si netlist file and build a complete Spectre deck."""
    netlist_text = Path(netlist_path).read_text(encoding="utf-8")
    return build_sim_deck(netlist_text, config)
