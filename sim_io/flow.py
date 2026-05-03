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
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_create_inst_by_master_name as inst,
    schematic_label_instance_term as label_inst_term,
)
from io_ring.bridge import load_skill_file
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
    """Redistribute symbol pins evenly on 4 sides.

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
    for side_name in ["left", "right", "top", "bottom"]:
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

def place_dut(client: VirtuosoClient, lib: str, tb_cell: str, primary_cell: str) -> bool:
    """Place the primary cell's symbol as DUT instance in _tb schematic."""
    with client.schematic.edit(lib, tb_cell, mode="a") as sch:
        cmd = inst(lib, primary_cell, "symbol", "DUT", 2.5, 0.0, "R0")
        sch.add(cmd)
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
    cfg = SIDE_CONFIGS

    with client.schematic.edit(lib, tb_cell, mode="a") as sch:
        for pin in pins:
            # Ground pins connect directly to the global ground net
            pad_type = _classify_pin(pin)
            net_name = "gnd!" if pad_type == "ground" else pin.name
            labeled_nets.append(net_name)
            cmd = label_inst_term(
                "DUT", pin.name, net_name,
                cv_expr="cv",
                rotation=cfg[pin.side]["label_rotation"],
                justification=cfg[pin.side]["label_align"],
            )
            sch.add(cmd)
        # Context exit auto-runs schCheck + dbSave

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
            pad_type = _classify_pin(pin)
            # ground pins are already labeled gnd! by add_wire_labels
            if pad_type == "ground":
                continue
            rule = PAD_RULES.get(pad_type)
            if rule is None:
                continue
            px, py = _pin_position_in_tb(pin, dut_xy)

            has_both = "source" in rule and "load" in rule

            for role in ("source", "load"):
                cfg = rule.get(role)
                if cfg is None:
                    continue

                inst_name = f"{'SRC' if role == 'source' else 'LOAD'}_{pin.name}"
                # Place load further out to avoid overlap with source
                if has_both and role == "load":
                    sx, sy = _source_load_position(px, py, pin.side, offset=_LOAD_OFFSET)
                else:
                    sx, sy = _source_load_position(px, py, pin.side)
                # Rotate left/right instances horizontal to avoid vertical overlap
                inst_rot = "R90" if pin.side in ("left", "right") else "R0"
                sch.add(inst(cfg["lib"], cfg["cell"], "symbol",
                             inst_name, sx, sy, inst_rot))
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
        pad_type = _classify_pin(pin)
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
                print(f"[step4d] WARNING: CDF params failed for {inst_name}: {r.errors}")

    n_src = len([p for p in placed if p != "GND0"])
    print(f"[step4d] Placed 1 global ref + {n_src} source/load instances: OK")
    return placed


# ── Main Pipeline ────────────────────────────────────────────

def run_sim_flow(
    lib: str,
    primary_cell: str,
    *,
    vdd_value: float = 1.8,
    run_sim: bool = False,
    spectre_mode: str = "spectre",
    client: Optional[VirtuosoClient] = None,
    user_intent: str = "",
) -> SimFlowResult:
    """Run the full simulation build-up flow (Steps 1-4) and optionally run spectre.

    When ``run_sim=True``, also executes Steps 5a-5d:
      netlist export → deck build → spectre run → result verification.

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
    dut_ok = place_dut(client, lib, tb_cell, primary_cell)

    # Step 4b: Extract pin info (from redistributed symbol)
    pins = extract_dut_pins(client, lib, primary_cell)

    # Write pin_info.json for LLM classification
    write_pin_info_json(pins, lib, primary_cell, vdd_value, run_dir / "pin_info.json")

    # Load LLM classifications if available (pin_classifications.json)
    _load_llm_classifications(run_dir)

    # Step 4c: Add wire labels on DUT pins
    labels = add_wire_labels(client, lib, tb_cell, pins) if pins else []

    # Step 4d: Place sources & loads based on pin classification
    sources = place_sources_and_loads(
        client, lib, tb_cell, pins, vdd_value=vdd_value,
    ) if pins else []

    # Step 5: Run simulation (optional)
    sim_run_ok = None
    sim_verdict = None
    if run_sim and pins:
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
    print(f"  Top pins:          {sum(1 for p in result.pins if p.side == 'top')}")
    print(f"  Bottom pins:       {sum(1 for p in result.pins if p.side == 'bottom')}")
    # Pin type breakdown
    types = {}
    for p in result.pins:
        t = _classify_pin(p)
        types[t] = types.get(t, 0) + 1
    print(f"  Pin types:         {dict(sorted(types.items()))}")
    if sim_run_ok is not None:
        print(f"  Spectre run:       {'OK' if sim_run_ok else 'FAILED'}")
    if sim_verdict is not None:
        print(f"  Verify verdict:    {sim_verdict}")
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
