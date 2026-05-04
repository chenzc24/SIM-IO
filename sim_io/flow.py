"""
sim_io.flow — Building blocks for the SIM-IO pipeline.

Step functions and dataclasses used by scripts/phase_a.py and scripts/phase_b.py.
Do not call run_phase_a / run_phase_b from here — those live in the scripts.
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

SKILL_DIR = _SIM_IO / "skill_code"
_OUTPUT_ROOT = _SIM_IO / "output"


def load_llm_classifications(run_dir: Path, *, cell: str = "") -> dict[str, PinClassification]:
    """Load LLM pin classifications from run_dir/pin_classifications.json.

    Returns a name→PinClassification map.  Returns empty dict (with a warning)
    if the file does not exist — callers fall back to heuristic classification.

    Validates the ``cell`` field in the JSON to catch stale files.
    """
    run_path = run_dir / "pin_classifications.json"

    if run_path.exists():
        result = load_pin_classifications(run_path)
        if cell and result.cell and result.cell != cell:
            print(f"[llm] WARNING: {run_path} has cell={result.cell!r}, "
                  f"expected {cell!r} — skipping stale file")
        else:
            classifications = build_classification_map(result)
            print(f"[llm] Loaded {len(classifications)} classifications from {run_path}")
            return classifications

    print(f"[llm] No pin_classifications.json in {run_dir} — using heuristic fallback. "
          f"Write classifications to {run_path} for LLM-driven placement.")
    return {}


def classify_pin(pin: PinInfo, classifications: dict[str, PinClassification]) -> str:
    """Return pin type from LLM classifications, or heuristic fallback."""
    if pin.name in classifications:
        return classifications[pin.name].pin_type
    return classify_pin_heuristic(pin)


def create_run_dir() -> Path:
    """Create and return a timestamped output directory under SIM-IO/output/.

    Returns path like ``SIM-IO/output/20260430_153045/``.

    Also writes ``.latest_run`` in the SIM-IO root so the LLM skill
    can discover the current run directory without guessing.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _OUTPUT_ROOT / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    # Write .latest_run so the LLM skill can find the run directory
    latest_marker = _SIM_IO / ".latest_run"
    latest_marker.write_text(str(run_dir), encoding="utf-8")

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


@dataclass
class PhaseAResult:
    """Result of Phase A: symbol export + redistribution + pin extraction.

    Phase A ends after writing ``pin_info.json`` so the LLM can classify pins.
    Pass this to ``run_phase_b()`` after writing ``pin_classifications.json``
    to the run directory.

    ``tb_cell`` is always ``f"{primary_cell}_tb"`` — TB creation happens in
    Phase B, not Phase A.
    """
    lib: str
    primary_cell: str
    pins: list[PinInfo]
    run_dir: Path
    vdd_value: float
    symbol_exported: bool
    redistributed: bool

    @property
    def tb_cell(self) -> str:
        return f"{self.primary_cell}_tb"

    def save(self, run_dir: Path) -> None:
        """Serialize to run_dir/phase_a_result.json for cross-process use."""
        data = asdict(self)
        data["run_dir"] = str(run_dir)
        (run_dir / "phase_a_result.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> PhaseAResult:
        """Load from a phase_a_result.json file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data.pop("tb_cell", None)  # compat: tb_cell is now a property
        data["run_dir"] = Path(data["run_dir"])
        data["pins"] = [PinInfo(**p) for p in data["pins"]]
        return cls(**data)


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
    load_r = client.load_il(str(SKILL_DIR / "extract_symbol_info.il"))
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
    load_skill_file(str(SKILL_DIR / "symbol_move_pin.il"))
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

        # After redistribution, all pins are on left/right only.
        # CORE pins always go right; others by x-position relative to body center.
        if name.endswith("_CORE"):
            side = "right"
        else:
            body_cx = (body_L + body_R) / 2.0
            side = "left" if px < body_cx else "right"

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

    All DUT pin labels use the original pin name (e.g., GIOL, GND_DAT).
    The ground_net field is internal — it tells the program which PVSS
    to connect source MINUS terminals to, but is never used as a label.

    Returns list of net names that were labeled.
    """
    labeled_nets = []
    cfg = SIDE_CONFIGS
    ops = []

    for pin in pins:
        net_name = pin.name
        labeled_nets.append(net_name)
        ops.append(label_term(
            "DUT", pin.name, net_name,
            rotation=cfg[pin.side]["label_rotation"],
            justification=cfg[pin.side]["label_align"],
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
    """Return (x, y) for a source/load placed offset um outward from a pin.

    Only supports left/right sides (all pins are on left/right after redistribution).
    """
    if side == "left":
        return (px - offset, py)
    if side == "right":
        return (px + offset, py)
    print(f"[WARN] _source_load_position: unexpected side={side!r}, treating as left")
    return (px - offset, py)


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
    classifications: dict[str, PinClassification] | None = None,
    dut_xy: tuple[float, float] = (2.5, 0.0),
    vdd_value: float = 1.8,
    client: Optional[VirtuosoClient] = None,
) -> list[str]:
    """Place sources, loads, PVSS devices, and inner devices based on pin classification.

    Dual-side topology:
      Phase 0: Collect ground pins → one PVSS per ground pin
      Phase 1: Place GND_REF (--GND→gnd! bridge) + PVSS devices
      Phase 2: Place outer devices (left side) using AI classification
      Phase 3: Place inner devices (right side) for CORE/duplicate pins
      Phase 4: Set CDF parameters for all instances

    Label convention:
      - DUT pin labels always use the original pin name (GIOL, GND_DAT, …)
      - PVSS PLUS uses the ground pin name (matches DUT pin for connectivity)
      - PVSS MINUS and fallback ground use "--GND" (not "gnd!")
      - Source/load MINUS uses the primary ground pin name for the domain

    Falls back to PAD_RULES when no LLM classification is available.

    Returns list of instance names placed.
    """
    load_skill_file(str(SKILL_DIR / "set_inst_params.il"))
    placed: list[str] = []
    ops: list[str] = []
    classifications = classifications or {}

    # Build lookups
    right_pins = {p.name: p for p in pins if p.side == "right"}
    left_pins = [p for p in pins if p.side == "left"]
    has_llm = bool(classifications)

    # ── Phase 0: Collect ground pins and build mappings ──
    # One PVSS per ground pin — each ground pin gets its own vdc=0 source.
    ground_pin_pvss: list[str] = []       # ordered list of ground pin names
    ground_net_primary: dict[str, str] = {}  # ground_net → primary pin name (for source MINUS)

    if has_llm:
        for pin in pins:
            cls = classifications.get(pin.name)
            if cls and cls.pin_type == "ground" and cls.ground_net:
                if pin.name not in ground_pin_pvss:
                    ground_pin_pvss.append(pin.name)
                gnet = cls.ground_net
                if gnet not in ground_net_primary:
                    ground_net_primary[gnet] = pin.name

    def _resolve_gnet_label(cls_or_none: PinClassification | None) -> str:
        """Resolve the ground net label for source/load MINUS terminal.

        Uses the primary ground pin name for the domain (e.g., "GIOL" for dgnd,
        "GND_DAT" for gnd_DAT). Falls back to "--GND" for global ground.
        """
        if cls_or_none and cls_or_none.ground_net:
            return ground_net_primary.get(cls_or_none.ground_net, "--GND")
        return "--GND"

    # ── Phase 1: Place GND_REF + PVSS devices ──────────
    min_pin_y = min((p.y for p in pins), default=-5.0)
    pvss_base_y = dut_xy[1] + min_pin_y - 4.0
    pvss_x_start = dut_xy[0] - 4.0
    pvss_spacing = 2.5

    # GND_REF: bridges "--GND" local net to "gnd!" global ground
    gnd_ref_x = pvss_x_start - pvss_spacing
    gnd_ref_y = pvss_base_y
    ops.append(create_inst("analogLib", "vdc", "symbol", "GND_REF", gnd_ref_x, gnd_ref_y, "R0"))
    placed.append("GND_REF")
    ops.append(label_term("GND_REF", "PLUS", "--GND"))
    ops.append(label_term("GND_REF", "MINUS", "gnd!"))

    # One PVSS per ground pin
    for i, pin_name in enumerate(sorted(ground_pin_pvss)):
        px = pvss_x_start + i * pvss_spacing
        py = pvss_base_y
        ops.append(create_inst("analogLib", "vdc", "symbol", pin_name, px, py, "R0"))
        placed.append(pin_name)
        ops.append(label_term(pin_name, "PLUS", pin_name))
        ops.append(label_term(pin_name, "MINUS", "--GND"))

    # ── Phase 2: Place outer devices ────────────────────
    # Only left-side (outer) pins get outer devices on the left
    for pin in left_pins:
        cls = classifications.get(pin.name)
        px, py = _pin_position_in_tb(pin, dut_xy)

        if cls:
            # Ground and no_connect pins only get labels (from add_wire_labels)
            if cls.pin_type in ("ground", "no_connect"):
                continue

            gnet_label = _resolve_gnet_label(cls)

            # Outer stimulus
            if cls.stimulus:
                inst_name = f"SRC_{pin.name}"
                sx, sy = _source_load_position(px, py, "left")
                inst_rot = "R90"
                ops.append(create_inst("analogLib", cls.stimulus, "symbol",
                                       inst_name, sx, sy, inst_rot))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", pin.name))
                ops.append(label_term(inst_name, "MINUS", gnet_label))

            # Outer load
            if cls.load:
                inst_name = f"LOAD_{pin.name}"
                sx, sy = _source_load_position(px, py, "left", offset=_LOAD_OFFSET)
                inst_rot = "R90"
                ops.append(create_inst("analogLib", cls.load, "symbol",
                                       inst_name, sx, sy, inst_rot))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", pin.name))
                ops.append(label_term(inst_name, "MINUS", gnet_label))

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
                ops.append(label_term(inst_name, cfg["ref_term"], "--GND"))

    # ── Phase 3: Place inner devices ────────────────────
    # Left-side pins with inner_stimulus get inner devices on the right side,
    # connected to the corresponding CORE/duplicate pin
    for pin in left_pins:
        cls = classifications.get(pin.name)
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

        gnet_label = _resolve_gnet_label(cls)

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
            ops.append(label_term(inst_name, "MINUS", gnet_label))

        # Place inner load (for bidirectional pins)
        if cls.inner_load:
            load_inst_name = f"INNER_LOAD_{pin.name}"
            lx, ly = _source_load_position(cpx, cpy, "right", offset=_LOAD_OFFSET)
            ops.append(create_inst("analogLib", cls.inner_load, "symbol",
                                   load_inst_name, lx, ly, inst_rot))
            placed.append(load_inst_name)
            ops.append(label_term(load_inst_name, "PLUS", core_pin_name))
            ops.append(label_term(load_inst_name, "MINUS", gnet_label))

    batch_ops(lib, tb_cell, ops, timeout=120)

    # ── Phase 4: Set CDF parameters ────────────────────
    for pin in left_pins:
        cls = classifications.get(pin.name)

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

    # Set GND_REF + PVSS params (vdc=0 for all ground devices)
    _set_cdf_params(lib, tb_cell, "GND_REF", {"vdc": "0"}, vdd_value)
    for pin_name in ground_pin_pvss:
        _set_cdf_params(lib, tb_cell, pin_name, {"vdc": "0"}, vdd_value)

    n_pvss = len(ground_pin_pvss) + 1  # +1 for GND_REF
    n_outer = sum(1 for p in placed if p.startswith(("SRC_", "LOAD_")))
    n_inner = sum(1 for p in placed if p.startswith("INNER_"))
    print(f"[step4d] Placed {n_pvss} PVSS + {n_outer} outer + {n_inner} inner instances: OK")
    return placed
