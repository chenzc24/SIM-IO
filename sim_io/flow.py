"""
IO Ring Simulation Flow — Pipeline Module
Step 1: Export symbol from primary schematic (TSG)
Step 2: Redistribute symbol pins (extract → calculate layout → apply)
Step 3: Create _tb cellview
Step 4: Place DUT + extract pins + add labels + add sources
Step 5: Netlist export + Spectre run + verification (optional)
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# Resolve paths — this file lives in SIM-IO/sim_io/
_PKG_DIR = Path(__file__).resolve().parent
_SIM_IO = _PKG_DIR.parent
_BRIDGE_LITE = _SIM_IO.parent / "virtuoso-bridge-lite" / "src"
_T28_ROOT = _SIM_IO.parent / "io-ring-orchestrator-T28"
for p in (_BRIDGE_LITE, _T28_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from virtuoso_bridge import VirtuosoClient
from io_ring.bridge import load_skill_file, rb_exec
from sim_io.bridge.edit_patterns import (
    batch_ops,
    label_term,
    create_inst,
)
from sim_io.symbol.layout_engine import (
    LayoutConfig,
    LayoutEngine,
    Side,
    generate_apply_skill,
    parse_symbol_info,
)
from sim_io.pin_types import (
    PinInfo,
    PinClassification,
    ClassificationResult,
    PinType,
    PAD_RULES,
    SIDE_CONFIGS,
    classify_pin_heuristic,
    load_pin_classifications,
    write_pin_info_json,
    build_classification_map,
    get_rule_for_pin,
)

_SKILL_DIR = _SIM_IO / "skill_code"
_OUTPUT_ROOT = _SIM_IO / "output"

# Module-level LLM classification cache (loaded once per run)
_llm_classifications: dict[str, PinClassification] = {}


def _load_llm_classifications(run_dir: Path) -> dict[str, PinClassification]:
    """Load LLM pin classifications from run_dir if available.

    Search order:
      1. run_dir/pin_classifications.json (run-specific)
      2. _SIM_IO/pin_classifications.json (reusable global)

    Returns name→PinClassification map, or empty dict if not found.
    """
    global _llm_classifications
    for path in (run_dir / "pin_classifications.json", _SIM_IO / "pin_classifications.json"):
        if path.exists():
            result = load_pin_classifications(path)
            _llm_classifications = build_classification_map(result)
            print(f"[llm] Loaded {len(_llm_classifications)} LLM classifications from {path}")
            return _llm_classifications
    _llm_classifications = {}
    return _llm_classifications


def _classify_pin(pin: PinInfo) -> str:
    """Classify a pin — LLM if available, else heuristic fallback."""
    if pin.name in _llm_classifications:
        return _llm_classifications[pin.name].pin_type
    return classify_pin_heuristic(pin)


def create_run_dir() -> Path:
    """Create and return a timestamped output directory under SIM-IO/output/.

    Returns path like ``SIM-IO/output/20260430_153045/``.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _OUTPUT_ROOT / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def log_skill_code(run_dir: Path, skill_path: str | Path) -> None:
    """Copy a skill file into ``run_dir/skill_code/`` for logging."""
    src = Path(skill_path)
    if not src.is_file():
        return
    dest_dir = run_dir / "skill_code"
    dest_dir.mkdir(exist_ok=True)
    shutil.copy2(src, dest_dir / src.name)


# Spacing from DUT pin center to source/load center (schematic units)
_SRC_LOAD_OFFSET = 5.0
_LOAD_OFFSET = 8.0


@dataclass
class SimFlowResult:
    lib: str
    primary_cell: str
    tb_cell: str
    symbol_exported: bool
    redistributed: bool
    tb_created: bool
    dut_placed: bool
    pins: list[PinInfo]
    labels_added: list[str]
    sources_placed: list[str]
    sim_run_ok: Optional[bool] = None
    sim_verdict: Optional[str] = None
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


# ── Step 2: Redistribute Symbol Pins ────────────────────────────

def redistribute_symbol(
    client: VirtuosoClient,
    lib: str,
    cell: str,
    run_dir: Path,
) -> bool:
    """Redistribute symbol pins on 2 sides (left=outer, right=CORE/duplicate).

    Sub-steps:
      2a. Regenerate symbol via TSG (fresh start)
      2b. Extract symbol info (rects, lines, labels, terminals)
      2c. Calculate new layout (body + pin positions) in pure Python
      2d. Apply layout via generated SKILL script

    Returns True if redistribution succeeded.
    """
    print(f"[step2] Redistributing symbol pins for {lib}/{cell}")

    # 2a: Fresh TSG — delete old symbol and regenerate
    client.execute_skill(f'ddDeleteCellView("{lib}" "{cell}" "symbol")')
    client.execute_skill('schSetEnv("ssgSortPins" "geometric")')
    r = client.execute_skill(
        f'let((pl) '
        f'pl = schSchemToPinList("{lib}" "{cell}" "schematic") '
        f'schPinListToSymbol("{lib}" "{cell}" "symbol" pl))'
    )
    if r.errors:
        print(f"[step2a] ERROR: TSG failed: {r.errors}")
        return False
    print(f"[step2a] Fresh TSG: OK")

    # 2b: Extract symbol info
    load_r = client.load_il(str(_SKILL_DIR / "extract_symbol_info.il"))
    if not load_r.ok:
        print(f"[step2b] ERROR loading extractor: {load_r.errors}")
        return False
    r = client.execute_skill(f'extractSymbolInfo("{lib}" "{cell}")', timeout=60)
    if r.errors:
        print(f"[step2b] ERROR extracting: {r.errors}")
        return False

    info = parse_symbol_info(r.output)
    print(f"[step2b] Extracted: {len(info.rects)} rects, {len(info.lines)} lines, "
          f"{len(info.labels)} labels, {len(info.terminals)} terminals")

    # Save raw extraction data
    (run_dir / "extract_raw.txt").write_text(r.output or "", encoding="utf-8")

    # 2c: Calculate layout
    engine = LayoutEngine(LayoutConfig())
    result = engine.redesign(info)
    body = result.body
    for side_name in ["left", "right"]:
        count = sum(1 for p in result.pins if p.side.value == side_name)
        print(f"[step2c] {side_name}: {count} pins")

    # Save layout result
    layout_data = {
        "lib": lib, "cell": cell,
        "body": {k: v for k, v in asdict(body).items()},
        "pins": [{k: (v.value if isinstance(v, Side) else v)
                  for k, v in asdict(p).items()} for p in result.pins],
    }
    (run_dir / "layout_result.json").write_text(
        json.dumps(layout_data, indent=2), encoding="utf-8"
    )

    # 2d: Apply layout
    skill_code = generate_apply_skill(lib, cell, result, engine.config)
    apply_il = run_dir / "apply_layout.il"
    apply_il.write_text(skill_code, encoding="utf-8")

    load_r = client.load_il(str(apply_il), timeout=120)
    if not load_r.ok:
        print(f"[step2d] ERROR: {load_r.errors}")
        return False
    print(f"[step2d] Layout applied: OK")

    # Verify
    sym = f'dbOpenCellViewByType("{lib}" "{cell}" "symbol" nil "r")'
    r = client.execute_skill(f'{sym}~>bBox')
    print(f"[step2] Verify bBox: {r.output}")
    r = client.execute_skill(f'length({sym}~>terminals)')
    print(f"[step2] Verify terminals: {r.output}")

    return True


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


# ── Step 3: Create TB Cellview ──────────────────────────────

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
        print(f"[step3] ERROR: Failed to create {lib}/{tb_cell}/schematic")
        return tb_cell

    # Save the empty cellview
    client.execute_skill(
        f'dbSave(dbOpenCellViewByType("{lib}" "{tb_cell}" "schematic" "schematic" "a"))'
    )
    print(f"[step3] Created: {lib}/{tb_cell}/schematic")
    return tb_cell


# ── Step 4a: Place DUT Instance ─────────────────────────────

def place_dut(lib: str, tb_cell: str, primary_cell: str) -> bool:
    """Place the primary cell's symbol as DUT instance in _tb schematic."""
    ops = [create_inst(lib, primary_cell, "symbol", "DUT", 2.5, 0.0, "R0")]
    batch_ops(lib, tb_cell, ops)
    print(f"[step4a] DUT placed: OK")
    return True


# ── Step 4b: Extract DUT Pin Info ───────────────────────────

def extract_dut_pins(client: VirtuosoClient, lib: str, primary_cell: str) -> list[PinInfo]:
    """Extract all pin info from the symbol view of the primary cell.

    Queries terminal names, directions, and positions from the symbol cellview.
    Determines pin side by distance to bBox edges (same logic as layout engine).
    CORE pins get their side flipped so TB wires point inward (toward DUT center).
    """
    sym_cv = f'dbOpenCellViewByType("{lib}" "{primary_cell}" "symbol" nil "r")'

    # Get terminal names
    r_names = client.execute_skill(f'{sym_cv}~>terminals~>name')
    names = re.findall(r'"([^"]+)"', r_names.output or "")
    if not names:
        print(f"[step4b] ERROR: No terminals found in {lib}/{primary_cell}/symbol")
        return []

    # Get terminal directions
    r_dirs = client.execute_skill(f'{sym_cv}~>terminals~>direction')
    directions = re.findall(r'"([^"]+)"', r_dirs.output or "")

    # Get symbol bBox edges for side classification
    r_bbox = client.execute_skill(f'{sym_cv}~>bBox')
    bbox_match = re.findall(r'[-\d.]+', r_bbox.output or "")
    if len(bbox_match) >= 4:
        body_L = float(bbox_match[0])
        body_B = float(bbox_match[1])
        body_R = float(bbox_match[2])
        body_T = float(bbox_match[3])
    else:
        body_L, body_B, body_R, body_T = -1.0, -1.0, 1.0, 1.0

    pins = []
    for i, name in enumerate(names):
        direction = directions[i] if i < len(directions) else "inputOutput"

        # Get first pin figure bBox (handles multi-pin terminals like VSS)
        r_pin = client.execute_skill(
            f'car(car(nth({i} {sym_cv}~>terminals)~>pins)~>figs)~>bBox'
        )
        pin_match = re.findall(r'[-\d.]+', r_pin.output or "")
        if len(pin_match) >= 4:
            px = (float(pin_match[0]) + float(pin_match[2])) / 2.0
            py = (float(pin_match[1]) + float(pin_match[3])) / 2.0
        else:
            px, py = 0.0, 0.0

        # Determine side by distance to bBox edges (matches layout engine logic)
        dists = {
            "left": abs(px - body_L),
            "right": abs(px - body_R),
            "top": abs(py - body_T),
            "bottom": abs(py - body_B),
        }
        side = min(dists, key=dists.__getitem__)

        pins.append(PinInfo(name=name, direction=direction, x=px, y=py, side=side))

    print(f"[step4b] Extracted {len(pins)} pins from {lib}/{primary_cell}/symbol")
    return pins


# ── Step 4c: Add Wire + Label (label-based wiring) ──────────


def add_wire_labels(
    lib: str,
    tb_cell: str,
    pins: list[PinInfo],
) -> list[str]:
    """Add labeled wire stubs on each DUT instance terminal.

    Uses label_term() from bridge-lite (auto-computes terminal center
    + stub direction from instance geometry) executed via batch_ops.

    Label-based wiring: same net name on DUT pin and source pin
    means Virtuoso auto-connects them.

    Ground pins are labeled with their local ground_net (e.g. gnd_DAT, dgnd)
    instead of gnd!, so they connect through PVSS devices.

    Returns list of net names that were labeled.
    """
    labeled_nets = []
    cfg = SIDE_CONFIGS
    ops = []

    for pin in pins:
        cls = _llm_classifications.get(pin.name)
        if cls and cls.pin_type == "ground":
            # Use local ground net from LLM classification
            net_name = cls.ground_net or "gnd!"
        elif cls is None and classify_pin_heuristic(pin) == "ground":
            # Fallback: no LLM classification, use gnd!
            net_name = "gnd!"
        else:
            net_name = pin.name
        labeled_nets.append(net_name)
        ops.append(label_term(
            "DUT", pin.name, net_name,
            rotation=cfg[pin.side]["label_rotation"],
            justification=cfg[pin.side]["label_align"],
            stub_direction=pin.side,
        ))

    batch_ops(lib, tb_cell, ops)
    print(f"[step4c] Added {len(labeled_nets)} labeled wire stubs on DUT pins: OK")
    return labeled_nets


# ── Step 4d: Place Sources & Loads ───────────────────────────

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
    """Replace VDD placeholder with the actual voltage value.

    Handles:
      "VDD" → str(vdd_value)
      "VDD/2" → computed division
    Other values are returned as-is.
    """
    if "VDD" not in value:
        return value
    result = value.replace("VDD", str(vdd_value))
    # Evaluate simple division like "0.9/2"
    if "/" in result:
        parts = result.split("/")
        if len(parts) == 2:
            try:
                val = float(parts[0]) / float(parts[1])
                return f"{val:g}"
            except (ValueError, ZeroDivisionError):
                pass
    return result


def _find_core_pin_name(pin_name: str, right_pins: dict[str, PinInfo]) -> str:
    """Find the corresponding right-side (CORE) pin name for a left-side pin.

    Search order:
      1. {pin_name}_CORE in right_pins
      2. Same name in right_pins (duplicate pin)
      3. Default to {pin_name}_CORE
    """
    core_name = f"{pin_name}_CORE"
    if core_name in right_pins:
        return core_name
    if pin_name in right_pins:
        return pin_name
    return core_name


def _set_cdf_params(
    lib: str,
    tb_cell: str,
    inst_name: str,
    params: dict,
    vdd_value: float,
    *,
    resolve_vdd: bool = False,
) -> None:
    """Set CDF parameters on an instance via setInstParams SKILL function."""
    if not params:
        return
    pairs = []
    for key, val in params.items():
        pairs.append(f'"{key}"')
        resolved = _resolve_param_value(val, vdd_value) if resolve_vdd else str(val)
        pairs.append(f'"{resolved}"')
    skill = (
        f'setInstParams("{lib}" "{tb_cell}" "{inst_name}" '
        f"list({' '.join(pairs)}))"
    )
    r = rb_exec(skill, timeout=30)
    if "error" in r.lower():
        print(f"[step4d] WARNING: CDF params failed for {inst_name}: {r}")


def place_sources_and_loads(
    lib: str,
    tb_cell: str,
    pins: list[PinInfo],
    *,
    dut_xy: tuple[float, float] = (2.5, 0.0),
    vdd_value: float = 1.8,
    client: Optional[VirtuosoClient] = None,
) -> list[str]:
    """Place sources, loads, PVSS devices, and inner devices based on pin classification.

    Dual-side topology:
      Phase 0: Collect ground domains → determine PVSS instances
      Phase 1: Place PVSS ground devices (one per unique ground_net)
      Phase 2: Place outer devices (left side) using AI classification
      Phase 3: Place inner devices (right side) for CORE/duplicate pins
      Phase 4: Set CDF parameters for all instances

    Falls back to PAD_RULES when no LLM classification is available.

    Returns list of instance names placed.
    """
    load_skill_file(str(_SKILL_DIR / "set_inst_params.il"))
    placed: list[str] = []
    ops: list[str] = []

    # Build lookups
    right_pins = {p.name: p for p in pins if p.side == "right"}
    left_pins = [p for p in pins if p.side == "left"]
    has_llm = bool(_llm_classifications)

    # ── Phase 0: Collect ground domains ─────────────────
    ground_nets: dict[str, str] = {}  # ground_net → PVSS instance name

    if has_llm:
        for pin in pins:
            cls = _llm_classifications.get(pin.name)
            if cls and cls.ground_net and cls.ground_net != "gnd!":
                gnet = cls.ground_net
                if gnet not in ground_nets:
                    if gnet == "dgnd":
                        ground_nets[gnet] = "GIOL"
                    elif gnet == "dgnd_hv":
                        ground_nets[gnet] = "PVSS2DGZ"
                    else:
                        block = gnet.replace("gnd_", "").replace("gnd", "")
                        ground_nets[gnet] = f"PVSS_{block}" if block else "PVSS"

    # ── Phase 1: Place PVSS ground devices ──────────────
    if ground_nets:
        # PVSS: analogLib/vdc, vdc=0, PLUS=local_ground, MINUS=gnd!
        pvss_base_y = dut_xy[1] + min((p.y for p in pins), default=-5.0) - 4.0
        pvss_x_start = dut_xy[0] - 4.0
        pvss_spacing = 2.5

        for i, (gnet, inst_name) in enumerate(sorted(ground_nets.items())):
            px = pvss_x_start + i * pvss_spacing
            py = pvss_base_y
            ops.append(create_inst("analogLib", "vdc", "symbol", inst_name, px, py, "R0"))
            placed.append(inst_name)
            ops.append(label_term(inst_name, "PLUS", gnet))
            ops.append(label_term(inst_name, "MINUS", "gnd!"))
    else:
        # Fallback: global gnd symbol (no LLM classifications)
        min_pin_y = min((p.y for p in pins), default=-5.0)
        gnd_x = dut_xy[0] - _LOAD_OFFSET - 2.0
        gnd_y = dut_xy[1] + min_pin_y - 3.0
        ops.append(create_inst("analogLib", "gnd", "symbol", "GND0", gnd_x, gnd_y, "R0"))
        placed.append("GND0")

    # ── Phase 2: Place outer devices ────────────────────
    # Only left-side (outer) pins get outer devices on the left
    for pin in left_pins:
        cls = _llm_classifications.get(pin.name)
        px, py = _pin_position_in_tb(pin, dut_xy)

        if cls:
            # Use LLM classification
            gnet = cls.ground_net or "gnd!"

            # Ground and no_connect pins only get labels (from add_wire_labels)
            if cls.pin_type in ("ground", "no_connect"):
                continue

            # Outer stimulus
            if cls.stimulus:
                inst_name = f"SRC_{pin.name}"
                sx, sy = _source_load_position(px, py, "left")
                inst_rot = "R90"
                ops.append(create_inst("analogLib", cls.stimulus, "symbol",
                                       inst_name, sx, sy, inst_rot))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", pin.name))
                ops.append(label_term(inst_name, "MINUS", gnet))

            # Outer load
            if cls.load:
                inst_name = f"LOAD_{pin.name}"
                sx, sy = _source_load_position(px, py, "left", offset=_LOAD_OFFSET)
                inst_rot = "R90"
                ops.append(create_inst("analogLib", cls.load, "symbol",
                                       inst_name, sx, sy, inst_rot))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", pin.name))
                ops.append(label_term(inst_name, "MINUS", gnet))

        else:
            # Fallback: PAD_RULES
            pad_type = classify_pin_heuristic(pin)
            if pad_type == "ground":
                continue
            rule = PAD_RULES.get(pad_type)
            if rule is None:
                continue

            has_both = "source" in rule and "load" in rule

            for role in ("source", "load"):
                cfg = rule.get(role)
                if cfg is None:
                    continue
                inst_name = f"{'SRC' if role == 'source' else 'LOAD'}_{pin.name}"
                if has_both and role == "load":
                    sx, sy = _source_load_position(px, py, "left", offset=_LOAD_OFFSET)
                else:
                    sx, sy = _source_load_position(px, py, "left")
                inst_rot = "R90"
                ops.append(create_inst(cfg["lib"], cfg["cell"], "symbol",
                                       inst_name, sx, sy, inst_rot))
                placed.append(inst_name)
                ops.append(label_term(inst_name, cfg["term"], pin.name))
                ops.append(label_term(inst_name, cfg["ref_term"], "gnd!"))

    # ── Phase 3: Place inner devices ────────────────────
    # Left-side pins with inner_stimulus get inner devices on the right side,
    # connected to the corresponding CORE/duplicate pin
    for pin in left_pins:
        cls = _llm_classifications.get(pin.name)
        if not cls or not cls.inner_stimulus:
            continue

        # Find the corresponding right-side (CORE) pin
        core_pin_name = _find_core_pin_name(pin.name, right_pins)
        core_pin = right_pins.get(core_pin_name)

        if core_pin:
            cpx, cpy = _pin_position_in_tb(core_pin, dut_xy)
        else:
            # Fallback: estimate position on the right side
            cpx = dut_xy[0] + abs(pin.x) + 1.5
            cpy = dut_xy[1] + pin.y

        gnet = cls.ground_net or "dgnd"

        # Place inner stimulus
        inst_name = f"INNER_{pin.name}"
        sx, sy = _source_load_position(cpx, cpy, "right")
        inst_rot = "R90"

        if cls.inner_stimulus == "noConn":
            # noConn: placed on inner side to prevent LVS warnings
            ops.append(create_inst("analogLib", "noConn", "symbol",
                                   inst_name, sx, sy, inst_rot))
            placed.append(inst_name)
            ops.append(label_term(inst_name, "PLUS", core_pin_name))
        else:
            # Standard device: vdc, idc, vpulse, cap
            ops.append(create_inst("analogLib", cls.inner_stimulus, "symbol",
                                   inst_name, sx, sy, inst_rot))
            placed.append(inst_name)
            ops.append(label_term(inst_name, "PLUS", core_pin_name))
            ops.append(label_term(inst_name, "MINUS", gnet))

        # Place inner load (for bidirectional pins)
        if cls.inner_load:
            load_inst_name = f"INNER_LOAD_{pin.name}"
            lx, ly = _source_load_position(cpx, cpy, "right", offset=_LOAD_OFFSET)
            ops.append(create_inst("analogLib", cls.inner_load, "symbol",
                                   load_inst_name, lx, ly, inst_rot))
            placed.append(load_inst_name)
            ops.append(label_term(load_inst_name, "PLUS", core_pin_name))
            ops.append(label_term(load_inst_name, "MINUS", gnet))

    batch_ops(lib, tb_cell, ops, timeout=120)

    # ── Phase 4: Set CDF parameters ────────────────────
    for pin in left_pins:
        cls = _llm_classifications.get(pin.name)

        if cls:
            if cls.pin_type in ("ground", "no_connect"):
                continue

            # Outer stimulus params
            if cls.stimulus and cls.stimulus_params:
                _set_cdf_params(lib, tb_cell, f"SRC_{pin.name}",
                                cls.stimulus_params, vdd_value)

            # Outer load params
            if cls.load and cls.load_params:
                _set_cdf_params(lib, tb_cell, f"LOAD_{pin.name}",
                                cls.load_params, vdd_value)

            # Inner stimulus params (skip noConn — no CDF params)
            if cls.inner_stimulus and cls.inner_params and cls.inner_stimulus != "noConn":
                _set_cdf_params(lib, tb_cell, f"INNER_{pin.name}",
                                cls.inner_params, vdd_value)

            # Inner load params
            if cls.inner_load and cls.inner_load_params:
                _set_cdf_params(lib, tb_cell, f"INNER_LOAD_{pin.name}",
                                cls.inner_load_params, vdd_value)

        else:
            # Fallback: PAD_RULES
            pad_type = classify_pin_heuristic(pin)
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
                if params:
                    _set_cdf_params(lib, tb_cell, inst_name, params,
                                    vdd_value, resolve_vdd=True)

    # Set PVSS params (vdc=0 for all ground devices)
    for gnet, inst_name in ground_nets.items():
        _set_cdf_params(lib, tb_cell, inst_name, {"vdc": "0"}, vdd_value)

    n_pvss = len(ground_nets) or 1  # 1 for the fallback GND0
    n_outer = sum(1 for p in placed if p.startswith(("SRC_", "LOAD_")))
    n_inner = sum(1 for p in placed if p.startswith("INNER_"))
    print(f"[step4d] Placed {n_pvss} PVSS + {n_outer} outer + {n_inner} inner instances: OK")
    return placed


# ── Main Pipeline ────────────────────────────────────────────

def run_sim_flow(
    lib: str,
    primary_cell: str,
    *,
    vdd_value: float = 1.8,
    run_sim: bool = False,
    sim_mode: str = "spectre",
    spectre_mode: str = "spectre",
    client: Optional[VirtuosoClient] = None,
    user_intent: str = "",
) -> SimFlowResult:
    """Run the full simulation build-up flow (Steps 1-4) and optionally simulate.

    When ``run_sim=True``, also executes simulation:
      - ``sim_mode="spectre"``: si netlist → deck build → CLI Spectre → verify
      - ``sim_mode="maestro"``: Maestro test setup → background simulation → read results

    Simulation config is resolved in this priority order:
      1. run_dir/sim_config.json (LLM-generated)
      2. run_dir/active.state (Maestro export)
      3. SiteConfig defaults with pin-driven heuristics

    user_intent is a free-text description of the desired simulation
    (e.g. "DC sweep VDD from 0 to 3, then AC analysis for gain").
    It is written to sim_config_input.json for the LLM to read.

    All outputs are saved under ``SIM-IO/output/<timestamp>/``.
    """
    if client is None:
        client = VirtuosoClient.from_env()

    run_dir = create_run_dir()

    # Log current skill code snapshot (files may change between runs)
    for il_file in sorted(_SKILL_DIR.glob("*.il")):
        log_skill_code(run_dir, il_file)

    print(f"\n{'='*60}")
    print(f" Sim Flow: {lib}/{primary_cell}  (VDD={vdd_value}V)")
    print(f" Output:   {run_dir}")
    print(f"{'='*60}\n")

    # Step 1: Export symbol (TSG)
    symbol_ok = export_symbol(client, lib, primary_cell)

    # Step 2: Redistribute symbol pins (extract → calculate → apply)
    redistributed = redistribute_symbol(client, lib, primary_cell, run_dir)

    # Step 3: Create _tb cellview
    tb_cell = create_tb_cellview(client, lib, primary_cell)

    # Step 4a: Place DUT instance
    dut_ok = place_dut(lib, tb_cell, primary_cell)

    # Step 4b: Extract pin info (from redistributed symbol)
    pins = extract_dut_pins(client, lib, primary_cell)

    # Write pin_info.json for LLM classification
    write_pin_info_json(pins, lib, primary_cell, vdd_value, run_dir / "pin_info.json")

    # Load LLM classifications if available (pin_classifications.json)
    _load_llm_classifications(run_dir)

    # Step 4c: Add wire labels on DUT pins
    labels = add_wire_labels(lib, tb_cell, pins) if pins else []

    # Step 4d: Place sources & loads based on pin classification
    sources = place_sources_and_loads(
        lib, tb_cell, pins, vdd_value=vdd_value, client=client,
    ) if pins else []

    # Step 5: Run simulation (optional)
    sim_run_ok = None
    sim_verdict = None
    if run_sim and pins:
        if sim_mode == "maestro":
            # ── Maestro path ──
            from sim_io.maestro import build_maestro_setup, run_maestro_sim
            from sim_io.site_config import SiteConfig
            from sim_io.sim.config import resolve_sim_config

            site = SiteConfig.from_env()
            deck_config = resolve_sim_config(
                run_dir=run_dir, lib=lib, cell=tb_cell,
                vdd_value=vdd_value, user_intent=user_intent,
            )

            # Append IO pad model include if available in site config.
            # TSMC28 IO pad cells (tphn28hpcpgv18) need their subcircuit
            # definitions included separately from the core model file.
            if site.pdk_io_spectre_include:
                from sim_io.sim.config import ModelInclude
                deck_config.model_includes.append(
                    ModelInclude(path=site.pdk_io_spectre_include, section="")
                )
                print(f"[maestro-flow] Added IO model include: "
                      f"{site.pdk_io_spectre_include}")

            # Step 5a: Build Maestro test setup from SimDeckConfig
            build_maestro_setup(
                client, lib, tb_cell, deck_config,
                pins=pins,
                auto_close=True,
            )

            # Step 5b: Run simulation in background mode
            # Signal paths are top-level nets (label-based wiring),
            # NOT /DUT/X (which is the Spectre instance-terminal path)
            wave_signals = [f"/{p.name}" for p in pins
                           if _classify_pin(p) not in ("ground", "no_connect")]
            mae_result = run_maestro_sim(
                client, lib, tb_cell,
                test_name=f"{tb_cell}_test",
                timeout=600,
                export_waves=True,
                wave_signals=wave_signals,
                run_dir=run_dir,
            )
            sim_run_ok = mae_result.sim_ok
            if mae_result.overall_spec:
                sim_verdict = mae_result.overall_spec

        else:
            # ── Standalone Spectre path (original) ──
            from sim_io.sim.run import run_sim_run
            from sim_io.sim.verify import verify_results
            from sim_io.site_config import SiteConfig
            from sim_io.sim.config import SimDeckConfig

            site = SiteConfig.from_env()
            sim_result = run_sim_run(
                lib, tb_cell, pins, run_dir,
                site=site,
                client=client,
                spectre_mode=spectre_mode,
                user_intent=user_intent,
                vdd_value=vdd_value,
            )
            sim_run_ok = sim_result.spectre_ok

            if sim_result.measurements:
                report = verify_results(sim_result.measurements, vdd=vdd_value, cell=tb_cell)
                sim_verdict = report.verdict
                report.save(run_dir / "verify.json")

    result = SimFlowResult(
        lib=lib,
        primary_cell=primary_cell,
        tb_cell=tb_cell,
        symbol_exported=symbol_ok,
        redistributed=redistributed,
        tb_created=True,
        dut_placed=dut_ok,
        pins=pins,
        labels_added=labels,
        sources_placed=sources,
        sim_run_ok=sim_run_ok,
        sim_verdict=sim_verdict,
    )

    # Save result to timestamped output dir
    result.save(run_dir)

    # Summary
    print(f"\n{'='*60}")
    print(f" Result Summary")
    print(f"{'='*60}")
    print(f"  Output dir:        {run_dir}")
    print(f"  Symbol exported:   {result.symbol_exported}")
    print(f"  Redistributed:     {result.redistributed}")
    print(f"  TB cellview:       {lib}/{result.tb_cell}/schematic")
    print(f"  DUT placed:        {result.dut_placed}")
    print(f"  Pins extracted:    {len(result.pins)}")
    print(f"  DUT labels added:  {len(result.labels_added)}")
    print(f"  Sources placed:    {len(result.sources_placed)}")
    print(f"  Left pins:         {sum(1 for p in result.pins if p.side == 'left')}")
    print(f"  Right pins:        {sum(1 for p in result.pins if p.side == 'right')}")
    # Pin type breakdown
    types = {}
    for p in result.pins:
        t = _classify_pin(p)
        types[t] = types.get(t, 0) + 1
    print(f"  Pin types:         {dict(sorted(types.items()))}")
    if sim_run_ok is not None:
        print(f"  Sim run:           {'OK' if sim_run_ok else 'FAILED'} ({sim_mode})")
    if sim_verdict is not None:
        print(f"  Verify verdict:    {sim_verdict}")
    print(f"{'='*60}\n")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: python sim_flow.py <lib> <primary_cell> [vdd_value] [sim_mode]")
        print(f"  sim_mode: spectre (default) | maestro")
        print(f"Example: python sim_flow.py LLM_Layout_Design_Lab IO_RING_12x12")
        print(f"Example: python sim_flow.py LLM_Layout_Design_Lab IO_RING_12x12 3.3 maestro")
        sys.exit(1)

    vdd = float(sys.argv[3]) if len(sys.argv) > 3 else 1.8
    mode = sys.argv[4] if len(sys.argv) > 4 else "spectre"
    run_sim = mode in ("maestro", "spectre") and len(sys.argv) > 3 or False
    run_sim_flow(sys.argv[1], sys.argv[2], vdd_value=vdd,
                 sim_mode=mode, run_sim=run_sim)
