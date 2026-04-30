"""
IO Ring Simulation Flow — Pipeline Module
Step 1: Export symbol from primary schematic (TSG)
Step 2: Create _tb cellview
Step 3: Place DUT + extract pins + add source/load labels
Step 4: ADE assembler (blocked — needs SKILL code from user)
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# Resolve virtuoso-bridge-lite
_BRIDGE_LITE = Path(__file__).resolve().parent.parent / "virtuoso-bridge-lite" / "src"
_T28_ROOT = Path(__file__).resolve().parent.parent / "io-ring-orchestrator-T28"
for p in (_BRIDGE_LITE, _T28_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_create_inst_by_master_name as inst,
    schematic_label_instance_term as label_inst_term,
)
from io_ring.bridge import load_skill_file

_SKILL_DIR = Path(__file__).resolve().parent / "skill_code"
_OUTPUT_ROOT = Path(__file__).resolve().parent / "output"


def create_run_dir() -> Path:
    """Create and return a timestamped output directory under SIM_IO/output/.

    Returns path like ``SIM_IO/output/20260430_153045/``.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _OUTPUT_ROOT / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


# ── Pin Classification & Stimulus/Load Rules ─────────────────

def classify_pin(pin: PinInfo) -> str:
    """Classify a pin by name heuristic + direction → pad_type.

    Returns one of: power, ground, digital_input, digital_output,
    digital_bidirectional.
    """
    name_upper = pin.name.upper()
    if "VDD" in name_upper or "VCC" in name_upper or "DVDD" in name_upper or "AVDD" in name_upper:
        return "power"
    if "VSS" in name_upper or "GND" in name_upper or "DVSS" in name_upper or "AVSS" in name_upper:
        return "ground"
    if pin.direction == "input":
        return "digital_input"
    if pin.direction == "output":
        return "digital_output"
    return "digital_bidirectional"


# stimulus/load rules — keyed by pad_type.
# Each entry: source and/or load with analogLib cell + default CDF params.
# The special value "VDD" in params is replaced at runtime by the vdd_value arg.
PAD_RULES = {
    "digital_input": {
        "source": {
            "lib": "analogLib", "cell": "vpulse",
            "term": "PLUS", "ref_term": "MINUS",
            "params": {"v1": "0", "v2": "VDD", "period": "100n",
                       "rise": "1n", "fall": "1n", "width": "50n"},
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
            "params": {"v1": "0", "v2": "VDD", "period": "100n",
                       "rise": "1n", "fall": "1n", "width": "50n"},
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
}

# Spacing from DUT pin center to source/load center (schematic units)
_SRC_LOAD_OFFSET = 2.5


# ── Data types ──────────────────────────────────────────────

@dataclass
class PinInfo:
    name: str
    direction: str      # "input" / "output" / "inputOutput"
    x: float
    y: float
    side: str           # "left" / "right" / "top" / "bottom"


@dataclass
class SimFlowResult:
    lib: str
    primary_cell: str
    tb_cell: str
    symbol_exported: bool
    tb_created: bool
    dut_placed: bool
    pins: list[PinInfo]
    labels_added: list[str]
    sources_placed: list[str]
    run_dir: Optional[str] = None

    def save(self, run_dir: Path) -> None:
        """Serialize result to run_dir/result.json."""
        data = asdict(self)
        data["run_dir"] = str(run_dir)
        (run_dir / "result.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


# ── Step 1: Export Symbol ───────────────────────────────────

def export_symbol(client: VirtuosoClient, lib: str, cell: str) -> bool:
    """Generate symbol view from schematic via TSG pipeline.

    Returns True if symbol was created (or already existed).
    """
    # Check if symbol already exists
    r = client.execute_skill(f'ddGetObj("{lib}" "{cell}")~>views~>name')
    views = re.findall(r'"([^"]+)"', r.output or "")
    if "symbol" in views:
        print(f"[step1] Symbol already exists: {lib}/{cell}/symbol")
        return True

    # Set geometric pin sorting (preserves schematic spatial layout)
    client.execute_skill('schSetEnv("ssgSortPins" "geometric")')

    # TSG two-call pipeline
    r = client.execute_skill(
        f'let((pl) '
        f'pl = schSchemToPinList("{lib}" "{cell}" "schematic") '
        f'schPinListToSymbol("{lib}" "{cell}" "symbol" pl))'
    )
    if r.errors:
        print(f"[step1] ERROR: TSG failed: {r.errors}")
        return False

    # Verify
    r = client.execute_skill(f'ddGetObj("{lib}" "{cell}")~>views~>name')
    views = re.findall(r'"([^"]+)"', r.output or "")
    ok = "symbol" in views
    print(f"[step1] Symbol export: {'OK' if ok else 'FAILED'} — views: {views}")
    return ok


# ── Symbol Editing Ops ────────────────────────────────────────


def symbol_move_pin(
    client: VirtuosoClient,
    lib: str,
    cell: str,
    pin_name: str,
    x: float,
    y: float,
    side: str = "left",
) -> bool:
    """Move one symbol pin (rect + wire + label) to a new (x, y) position.

    Must call after TSG has generated the symbol view.  The wire stub
    is re-oriented to extend outward from the body based on ``side``
    (one of "left"/"right"/"top"/"bottom").
    """
    load_skill_file(str(_SKILL_DIR / "symbol_move_pin.il"))
    r = client.execute_skill(
        f'symbolMovePin("{lib}" "{cell}" "{pin_name}" {x:g} {y:g} "{side}")'
    )
    if r.errors:
        print(f"[symbol_move_pin] ERROR: {r.errors}")
        return False
    ok = "MOVE-PIN" in (r.output or "")
    if ok:
        print(f"[symbol_move_pin] {pin_name} -> ({x:.3f}, {y:.3f})")
    else:
        print(f"[symbol_move_pin] FAILED for {pin_name}")
    return ok


# ── Step 2: Create TB Cellview ──────────────────────────────

def create_tb_cellview(client: VirtuosoClient, lib: str, primary_cell: str) -> str:
    """Create a new schematic cellview named {primary_cell}_tb.

    Returns the tb cell name.
    """
    tb_cell = f"{primary_cell}_tb"

    # Create fresh (mode "w") — overwrites if exists
    r = client.execute_skill(
        f'dbOpenCellViewByType("{lib}" "{tb_cell}" "schematic" "schematic" "w")'
    )
    if not r.output or r.output.strip().lower() == "nil":
        print(f"[step2] ERROR: Failed to create {lib}/{tb_cell}/schematic")
        return tb_cell

    # Save the empty cellview
    client.execute_skill(
        f'dbSave(dbOpenCellViewByType("{lib}" "{tb_cell}" "schematic" "schematic" "a"))'
    )
    print(f"[step2] Created: {lib}/{tb_cell}/schematic")
    return tb_cell


# ── Step 3a: Place DUT Instance ─────────────────────────────

def place_dut(client: VirtuosoClient, lib: str, tb_cell: str, primary_cell: str) -> bool:
    """Place the primary cell's symbol as DUT instance in _tb schematic."""
    with client.schematic.edit(lib, tb_cell, mode="a") as sch:
        cmd = inst(lib, primary_cell, "symbol", "DUT", 2.5, 0.0, "R0")
        sch.add(cmd)
    print(f"[step3a] DUT placed: OK")
    return True


# ── Step 3b: Extract DUT Pin Info ───────────────────────────

def extract_dut_pins(client: VirtuosoClient, lib: str, primary_cell: str) -> list[PinInfo]:
    """Extract all pin info from the symbol view of the primary cell.

    Queries terminal names, directions, and positions from the symbol cellview.
    Determines pin side (left/right/top/bottom) from position relative to symbol center.
    """
    sym_cv = f'dbOpenCellViewByType("{lib}" "{primary_cell}" "symbol" nil "r")'

    # Get terminal names
    r_names = client.execute_skill(f'{sym_cv}~>terminals~>name')
    names = re.findall(r'"([^"]+)"', r_names.output or "")
    if not names:
        print(f"[step3b] ERROR: No terminals found in {lib}/{primary_cell}/symbol")
        return []

    # Get terminal directions
    r_dirs = client.execute_skill(f'{sym_cv}~>terminals~>direction')
    directions = re.findall(r'"([^"]+)"', r_dirs.output or "")

    # Get symbol bBox to determine center
    r_bbox = client.execute_skill(f'{sym_cv}~>bBox')
    # Parse ((x1 y1) (x2 y2))
    bbox_match = re.findall(r'[-\d.]+', r_bbox.output or "")
    if len(bbox_match) >= 4:
        cx = (float(bbox_match[0]) + float(bbox_match[2])) / 2.0
        cy = (float(bbox_match[1]) + float(bbox_match[3])) / 2.0
    else:
        cx, cy = 0.0, 0.0

    # Get pin figure positions from the symbol
    # TSG symbols: each terminal has a pin with a fig (rect on pin/drawing layer)
    # We need to get the center of each pin's fig
    # Extract pin bBoxes per terminal
    pins = []
    for i, name in enumerate(names):
        direction = directions[i] if i < len(directions) else "inputOutput"

        # Get this terminal's pin figure bBox
        # Use nth to access terminal by index
        r_pin = client.execute_skill(
            f'nth({i} {sym_cv}~>terminals)~>pins~>figs~>bBox'
        )
        pin_match = re.findall(r'[-\d.]+', r_pin.output or "")
        if len(pin_match) >= 4:
            px = (float(pin_match[0]) + float(pin_match[2])) / 2.0
            py = (float(pin_match[1]) + float(pin_match[3])) / 2.0
        else:
            px, py = 0.0, 0.0

        # Determine side
        if px < cx - 0.1:
            side = "left"
        elif px > cx + 0.1:
            side = "right"
        elif py > cy + 0.1:
            side = "top"
        else:
            side = "bottom"

        pins.append(PinInfo(name=name, direction=direction, x=px, y=py, side=side))

    print(f"[step3b] Extracted {len(pins)} pins from {lib}/{primary_cell}/symbol")
    return pins


# ── Step 3c: Add Wire + Label (label-based wiring) ──────────

# Side config: wire extend direction + label offset + alignment
# Same pattern as io-ring-orchestrator-T28/io_ring/schematic/generator.py
_SIDE_CONFIGS = {
    "right": {
        "extend_x": 0.750, "extend_y": 0.0,
        "label_offset_x": 0.25, "label_offset_y": 0.0,
        "label_align": "lowerLeft", "label_rotation": "R0",
    },
    "left": {
        "extend_x": -0.750, "extend_y": 0.0,
        "label_offset_x": -0.25, "label_offset_y": 0.0,
        "label_align": "lowerRight", "label_rotation": "R0",
    },
    "top": {
        "extend_x": 0.0, "extend_y": 0.750,
        "label_offset_x": 0.0, "label_offset_y": 0.25,
        "label_align": "lowerLeft", "label_rotation": "R90",
    },
    "bottom": {
        "extend_x": 0.0, "extend_y": -0.750,
        "label_offset_x": 0.0, "label_offset_y": -0.25,
        "label_align": "lowerRight", "label_rotation": "R90",
    },
}


def add_wire_labels(
    client: VirtuosoClient,
    lib: str,
    tb_cell: str,
    pins: list[PinInfo],
    dut_xy: tuple[float, float] = (2.5, 0.0),
) -> list[str]:
    """Add labeled wire stubs on each DUT instance terminal.

    Uses SchematicEditor context + schematic_label_instance_term from
    virtuoso-bridge-lite which:
    1. Finds the instance terminal center (with coordinate transform)
    2. Creates a short wire stub from the terminal outward
    3. Places a net label on the stub

    Label-based wiring: same net name on DUT pin and source pin
    means Virtuoso auto-connects them.

    Returns list of net names that were labeled.
    """
    labeled_nets = []
    cfg = _SIDE_CONFIGS

    with client.schematic.edit(lib, tb_cell, mode="a") as sch:
        for pin in pins:
            net_name = pin.name
            labeled_nets.append(net_name)
            cmd = label_inst_term(
                "DUT", pin.name, net_name,
                cv_expr="cv",
                rotation=cfg[pin.side]["label_rotation"],
                justification=cfg[pin.side]["label_align"],
            )
            sch.add(cmd)
        # Context exit auto-runs schCheck + dbSave

    print(f"[step3c] Added {len(labeled_nets)} labeled wire stubs on DUT pins: OK")
    return labeled_nets


# ── Step 3d: Place Sources & Loads ───────────────────────────

def _pin_position_in_tb(
    pin: PinInfo, dut_xy: tuple[float, float]
) -> tuple[float, float]:
    """Convert symbol-coordinate pin position to tb-schematic coordinate."""
    return (dut_xy[0] + pin.x, dut_xy[1] + pin.y)


def _source_load_position(
    px: float, py: float, side: str, offset: float = _SRC_LOAD_OFFSET,
) -> tuple[float, float]:
    """Return (x, y) for a source/load placed offset um outward from a pin."""
    if side == "left":
        return (px - offset, py)
    if side == "right":
        return (px + offset, py)
    if side == "top":
        return (px, py + offset)
    return (px, py - offset)  # bottom


def _resolve_param_value(value: str, vdd_value: float) -> str:
    """Replace placeholder VDD with the actual voltage value."""
    if value == "VDD":
        return str(vdd_value)
    return value


def place_sources_and_loads(
    client: VirtuosoClient,
    lib: str,
    tb_cell: str,
    pins: list[PinInfo],
    *,
    dut_xy: tuple[float, float] = (2.5, 0.0),
    vdd_value: float = 1.8,
    ref_net: str = "gnd!",
) -> list[str]:
    """Place analogLib sources/loads for each DUT pin based on pad type rules.

    Uses label-based wiring: source/load terminals get the same net label
    as the corresponding DUT pin, so Virtuoso auto-connects them.

    Reference terminals (MINUS of sources, MINUS of loads) are tied to
    ``ref_net`` (default ``gnd!`` — the global Virtuoso ground net).

    Ground-type pins (VSS/GND) are skipped — they are already labeled
    by ``add_wire_labels()`` and implicitly serve as the ground reference.

    Two-phase:
      1. SchematicEditor: place all instances + wire labels → save
      2. setInstParams SKILL: configure CDF params on each instance → save

    Returns list of instance names placed.
    """
    load_skill_file(str(_SKILL_DIR / "set_inst_params.il"))
    placed: list[str] = []

    # ── Phase 1: place instances + labels ─────────────────
    with client.schematic.edit(lib, tb_cell, mode="a") as sch:
        # Global gnd symbol for visual ground reference
        sch.add(inst("analogLib", "gnd", "symbol", "GND0", -1.5, -3.0, "R0"))
        placed.append("GND0")

        for pin in pins:
            pad_type = classify_pin(pin)
            # ground pins need no source — they ARE the reference
            if pad_type == "ground":
                continue
            rule = PAD_RULES.get(pad_type)
            if rule is None:
                continue
            px, py = _pin_position_in_tb(pin, dut_xy)

            for role in ("source", "load"):
                cfg = rule.get(role)
                if cfg is None:
                    continue

                inst_name = f"{'SRC' if role == 'source' else 'LOAD'}_{pin.name}"
                sx, sy = _source_load_position(px, py, pin.side)
                sch.add(inst(cfg["lib"], cfg["cell"], "symbol",
                             inst_name, sx, sy, "R0"))
                placed.append(inst_name)

                # Label primary terminal → net = pin.name (connects to DUT)
                sch.add(label_inst_term(
                    inst_name, cfg["term"], pin.name,
                    justification="centerCenter", rotation="R0",
                ))

                # Label reference terminal → ref_net (gnd!)
                sch.add(label_inst_term(
                    inst_name, cfg["ref_term"], ref_net,
                    justification="centerCenter", rotation="R0",
                ))
        # schCheck + dbSave on exit

    # ── Phase 2: set CDF params ───────────────────────────
    for pin in pins:
        pad_type = classify_pin(pin)
        if pad_type == "ground":
            continue
        rule = PAD_RULES.get(pad_type)
        if rule is None:
            continue
        for role in ("source", "load"):
            cfg = rule.get(role)
            if cfg is None:
                continue
            inst_name = f"{'SRC' if role == 'source' else 'LOAD'}_{pin.name}"
            params = cfg.get("params", {})
            if not params:
                continue
            # Build SKILL paramPairs list, resolving VDD placeholder
            pairs = []
            for key, val in params.items():
                pairs.append(f'"{key}"')
                pairs.append(f'"{_resolve_param_value(val, vdd_value)}"')
            skill = (
                f'setInstParams("{lib}" "{tb_cell}" "{inst_name}" '
                f"list({' '.join(pairs)}))"
            )
            r = client.execute_skill(skill, timeout=30)
            if r.errors:
                print(f"[step3d] WARNING: CDF params failed for {inst_name}: {r.errors}")

    n_src = len([p for p in placed if p != "GND0"])
    print(f"[step3d] Placed 1 global ref + {n_src} source/load instances: OK")
    return placed


# ── Main Pipeline ────────────────────────────────────────────

def run_sim_flow(
    lib: str,
    primary_cell: str,
    *,
    vdd_value: float = 1.8,
    client: Optional[VirtuosoClient] = None,
) -> SimFlowResult:
    """Run the full simulation build-up flow (Steps 1-3).

    Step 4 (ADE assembler) is deferred — blocked by ADE permission.
    All outputs are saved under ``SIM_IO/output/<timestamp>/``.
    """
    if client is None:
        client = VirtuosoClient.from_env()

    run_dir = create_run_dir()

    print(f"\n{'='*60}")
    print(f" Sim Flow: {lib}/{primary_cell}  (VDD={vdd_value}V)")
    print(f" Output:   {run_dir}")
    print(f"{'='*60}\n")

    # Step 1: Export symbol
    symbol_ok = export_symbol(client, lib, primary_cell)

    # Step 2: Create _tb cellview
    tb_cell = create_tb_cellview(client, lib, primary_cell)

    # # Step 3a: Place DUT instance
    # dut_ok = place_dut(client, lib, tb_cell, primary_cell)

    # # Step 3b: Extract pin info
    # pins = extract_dut_pins(client, lib, primary_cell)

    # # Step 3c: Add wire labels on DUT pins
    # labels = add_wire_labels(client, lib, tb_cell, pins) if pins else []

    # # Step 3d: Place sources & loads based on pin classification
    # sources = place_sources_and_loads(
    #     client, lib, tb_cell, pins, vdd_value=vdd_value,
    # ) if pins else []

    result = SimFlowResult(
        lib=lib,
        primary_cell=primary_cell,
        tb_cell=tb_cell,
        symbol_exported=symbol_ok,
        tb_created=True,
        dut_placed=False,
        pins=[],
        labels_added=[],
        sources_placed=[],
    )

    # Save result to timestamped output dir
    result.save(run_dir)

    # Summary
    print(f"\n{'='*60}")
    print(f" Result Summary")
    print(f"{'='*60}")
    print(f"  Output dir:        {run_dir}")
    print(f"  Symbol exported:   {result.symbol_exported}")
    print(f"  TB cellview:       {lib}/{result.tb_cell}/schematic")
    print(f"  DUT placed:        {result.dut_placed}")
    print(f"  Pins extracted:    {len(result.pins)}")
    print(f"  DUT labels added:  {len(result.labels_added)}")
    print(f"  Sources placed:    {len(result.sources_placed)}")
    print(f"  Left pins:         {sum(1 for p in result.pins if p.side == 'left')}")
    print(f"  Right pins:        {sum(1 for p in result.pins if p.side == 'right')}")
    print(f"  Top pins:          {sum(1 for p in result.pins if p.side == 'top')}")
    print(f"  Bottom pins:       {sum(1 for p in result.pins if p.side == 'bottom')}")
    # Pin type breakdown
    types = {}
    for p in result.pins:
        t = classify_pin(p)
        types[t] = types.get(t, 0) + 1
    print(f"  Pin types:         {dict(sorted(types.items()))}")
    # # Step 4: ADE assembler + simulation (deferred — ADE permission required)
    print(f"\n  Next: ADE assembler + simulation (deferred — ADE permission required)")
    print(f"{'='*60}\n")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: python sim_flow.py <lib> <primary_cell> [vdd_value]")
        print(f"Example: python sim_flow.py LLM_Layout_Design_Lab IO_RING_12x12")
        print(f"Example: python sim_flow.py LLM_Layout_Design_Lab IO_RING_12x12 3.3")
        sys.exit(1)

    vdd = float(sys.argv[3]) if len(sys.argv) > 3 else 1.8
    run_sim_flow(sys.argv[1], sys.argv[2], vdd_value=vdd)
