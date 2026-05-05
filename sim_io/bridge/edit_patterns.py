"""
Virtuoso schematic editing via rb_exec.

Two APIs:

  1. Batch API (recommended):
     batch_ops(lib, cell, ops) — opens CV once, runs all ops, schCheck + dbSave.
     Use label_term() / create_inst() / create_pin() to generate ops.
     Each op references 'cv' from the outer let — all ops share one cellview.

  2. Direct API (legacy, one rb_exec per call):
     place_instance(), create_wire(), create_wire_label(), etc.
     Each call independently opens/saves the cellview.

Notes:
  - rb_exec() is a thin wrapper around execute_skill(), no let-wrapping.
  - dbOpenCellViewByType on analogLib symbols: omit viewType param.
  - mode: "a"=append, "w"=overwrite, "r"=read-only.
"""

from pathlib import Path
import sys

# sim_io/bridge/ → bridge-Agent/io-ring-orchestrator-T28/
_T28_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "io-ring-orchestrator-T28"
if str(_T28_ROOT) not in sys.path:
    sys.path.insert(0, str(_T28_ROOT))

# sim_io/bridge/ → bridge-Agent/virtuoso-bridge-lite/src/
_BRIDGE_LITE = Path(__file__).resolve().parent.parent.parent.parent / "virtuoso-bridge-lite" / "src"
if str(_BRIDGE_LITE) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_LITE))

from io_ring.bridge import rb_exec, load_skill_file, open_cell_view_by_type, ge_open_window, save_current_cellview, ui_redraw
from io_ring.bridge.client import _get_client

from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_label_instance_term as label_term,
    schematic_create_inst_by_master_name as create_inst,
    schematic_create_pin as create_pin_skill,
    schematic_create_wire as create_wire_skill,
    schematic_create_wire_between_instance_terms as wire_between_terms,
    _schematic_bind_instance_and_term_expr as _bind_inst_term,
)
from virtuoso_bridge.virtuoso.ops import escape_skill_string as _esc


# ── Directed label: force stub direction ──────────────────────

_STUB_DIR_OFFSETS = {
    "left":  (-1.0,  0.0),
    "right": ( 1.0,  0.0),
    "up":    ( 0.0,  1.0),
    "down":  ( 0.0, -1.0),
}


def label_term_directed(
    instance_name: str,
    term_name: str,
    net_name: str,
    *,
    stub_direction: str,
    extension_length: float = 0.75,
    justification: str = "centerCenter",
    rotation: str = "R0",
    style: str = "stick",
    height: float = 0.0625,
    cv_expr: str = "cv",
) -> str:
    """Place a labeled wire stub with a forced direction.

    Identical to ``label_term`` but bypasses the geometric stub-direction
    heuristic.  After symbol pin redistribution all pins are on left/right,
    yet the geometric heuristic routes wires up/down for pins far from the
    DUT center.  This function forces the stub to extend in the known
    ``stub_direction`` ("left"/"right"/"up"/"down").

    Inherits the instance/terminal lookup from bridge-lite's
    ``_schematic_bind_instance_and_term_expr`` and reuses its coordinate
    transform logic — only the stub-end calculation is replaced.
    """
    if stub_direction not in _STUB_DIR_OFFSETS:
        raise ValueError(
            f"stub_direction must be one of {sorted(_STUB_DIR_OFFSETS)}, "
            f"got {stub_direction!r}"
        )
    dx, dy = _STUB_DIR_OFFSETS[stub_direction]
    ext_x = dx * extension_length
    ext_y = dy * extension_length

    return (
        "let((rbInst rbTerm rbPin rbFig rbLocalBBox rbLocalCtr rbCtr "
        "rbStubEnd rbMid) "
        f"{_bind_inst_term(instance_name, term_name, cv_expr=cv_expr)}"
        "rbLocalBBox = when(rbFig rbFig~>bBox) "
        "rbLocalCtr = when(rbLocalBBox "
        "list((xCoord(car(rbLocalBBox)) + xCoord(cadr(rbLocalBBox))) / 2.0 "
        "(yCoord(car(rbLocalBBox)) + yCoord(cadr(rbLocalBBox))) / 2.0)) "
        "rbCtr = when(rbLocalCtr dbTransformPoint(rbLocalCtr rbInst~>transform)) "
        f"rbStubEnd = when(rbCtr list(xCoord(rbCtr) + {ext_x:g} yCoord(rbCtr) + {ext_y:g})) "
        "rbMid = when(rbCtr && rbStubEnd "
        "list((xCoord(rbCtr) + xCoord(rbStubEnd)) / 2.0 "
        "(yCoord(rbCtr) + yCoord(rbStubEnd)) / 2.0)) "
        "when(rbCtr && rbStubEnd schCreateWire(cv \"route\" \"full\" list(rbCtr rbStubEnd) 0 0 0 nil nil)) "
        "when(rbMid "
        f'schCreateWireLabel({cv_expr} nil rbMid "{_esc(net_name)}" '
        f'"{_esc(justification)}" '
        f'"{_esc(rotation)}" '
        f'"{_esc(style)}" {height:g} nil)))'
    )


# ═══════════════════════════════════════════════════════════════
# Batch API (recommended)
# ═══════════════════════════════════════════════════════════════

def batch_ops(
    lib: str,
    cell: str,
    ops: list[str],
    *,
    view: str = "schematic",
    view_type: str = "schematic",
    mode: str = "a",
    timeout: int = 60,
) -> str:
    """Execute a batch of SKILL schematic operations in one rb_exec call.

    Opens CV once -> runs all ops -> schCheck -> dbSave.

    ops: list of SKILL expressions referencing ``cv`` as the cellview.
         Use label_term(), create_inst(), create_pin_skill() etc. to
         generate ops (they accept cv_expr="cv" by default).
    """
    cv_open = (
        f'dbOpenCellViewByType("{_esc(lib)}" "{_esc(cell)}" '
        f'"{view}" "{view_type}" "{mode}")'
    )
    body = " ".join(ops)
    skill = (
        f'let((cv) cv = {cv_open} '
        f'{body} '
        f'schCheck(cv) dbSave(cv) "BATCH-OK")'
    )
    return rb_exec(skill, timeout=timeout).strip()


def label_instance_term(
    lib: str,
    cell: str,
    instance_name: str,
    term_name: str,
    net_name: str,
    *,
    extension_length: float = 0.25,
    justification: str = "centerCenter",
    rotation: str = "R0",
) -> str:
    """Place a labeled wire stub on one instance terminal (single rb_exec).

    Auto-computes terminal center + stub direction from instance geometry.
    Same as bridge-lite's schematic_label_instance_term but executed via rb_exec
    with its own open/save lifecycle.
    """
    return batch_ops(lib, cell, [
        label_term(
            instance_name, term_name, net_name,
            extension_length=extension_length,
            justification=justification,
            rotation=rotation,
        ),
    ])


def label_instance_terms(
    lib: str,
    cell: str,
    labels: list[dict],
) -> str:
    """Batch: label multiple instance terminals in one rb_exec call.

    labels: list of dicts with keys:
        "instance", "term", "net"  (required)
        "extension_length", "justification", "rotation"  (optional)
    Opens CV once -> labels all terminals -> schCheck -> dbSave once.
    """
    ops = []
    for lbl in labels:
        ops.append(label_term(
            lbl["instance"], lbl["term"], lbl["net"],
            extension_length=lbl.get("extension_length", 0.25),
            justification=lbl.get("justification", "centerCenter"),
            rotation=lbl.get("rotation", "R0"),
        ))
    return batch_ops(lib, cell, ops)


def place_and_label(
    lib: str,
    cell: str,
    instances: list[dict],
    labels: list[dict],
) -> str:
    """Place instances + label terminals in one rb_exec call.

    instances: list of dicts with keys:
        "lib", "cell", "view"(default "symbol"), "name", "x", "y",
        "orient"(default "R0")
    labels:    same format as label_instance_terms().
    """
    ops = []
    for i in instances:
        ops.append(create_inst(
            i["lib"], i["cell"], i.get("view", "symbol"),
            i["name"], i["x"], i["y"], i.get("orient", "R0"),
        ))
    for lbl in labels:
        ops.append(label_term(
            lbl["instance"], lbl["term"], lbl["net"],
            extension_length=lbl.get("extension_length", 0.25),
            justification=lbl.get("justification", "centerCenter"),
            rotation=lbl.get("rotation", "R0"),
        ))
    return batch_ops(lib, cell, ops, timeout=120)


# ═══════════════════════════════════════════════════════════════
# Direct API (legacy, one rb_exec per call)
# ═══════════════════════════════════════════════════════════════

def get_cv_info(lib: str, cell: str, view: str = "schematic") -> dict:
    """Get cellview basic info."""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "{view}" "schematic" "a")'
    return {
        "lib": rb_exec(f'{cv}~>libName', timeout=15).strip().strip('"'),
        "cell": rb_exec(f'{cv}~>cellName', timeout=15).strip().strip('"'),
        "instances": rb_exec(f'length({cv}~>instances)', timeout=15).strip(),
        "nets": rb_exec(f'{cv}~>nets~>name', timeout=15).strip(),
    }


def place_instance(lib: str, cell: str, master_lib: str, master_cell: str,
                   inst_name: str, x: float, y: float, orient: str = "R0") -> str:
    """Place an instance (no viewType)."""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    master = f'dbOpenCellViewByType("{master_lib}" "{master_cell}" "symbol")'
    result = rb_exec(f'dbCreateInst({cv} {master} "{inst_name}" list({x} {y}) "{orient}")~>name', timeout=30)
    return result.strip().strip('"')


def set_cdf_param(lib: str, cell: str, inst_name: str,
                  param_name: str, param_value: str) -> str:
    """Set instance CDF parameter."""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    inst = f'car(setof(x {cv}~>instances x~>name=="{inst_name}"))'
    param = f'car(setof(p cdfGetInstCDF({inst})~>parameters p~>name=="{param_name}"))'
    result = rb_exec(f'{param}~>value = "{param_value}"', timeout=15)
    return result.strip()


def create_wire(lib: str, cell: str, points: list) -> str:
    """Create wire. points = [(x1,y1), (x2,y2), ...]"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    pts_str = " ".join(f"list({x} {y})" for x, y in points)
    result = rb_exec(f'schCreateWire({cv} "route" "full" list({pts_str}) 0 0 0 nil nil)', timeout=15)
    return result.strip()


def create_wire_label(lib: str, cell: str, x: float, y: float,
                      text: str, align: str = "centerLeft") -> str:
    """Create net label at (x, y)."""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    result = rb_exec(f'schCreateWireLabel({cv} nil list({x} {y}) "{text}" "{align}" "0" "stick" 0.0625 nil)', timeout=15)
    return result.strip()


def create_pin(lib: str, cell: str, name: str, direction: str,
               x: float, y: float, orient: str = "left") -> str:
    """Create pin."""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    result = rb_exec(f'schCreatePin({cv} nil "{name}" "{direction}" nil list({x} {y}) "{orient}")', timeout=15)
    return result.strip()


def create_net(lib: str, cell: str, net_name: str) -> str:
    """Create net."""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    result = rb_exec(f'dbCreateNet({cv} "{net_name}")~>name', timeout=15)
    return result.strip().strip('"')


def save_cv(lib: str, cell: str, view: str = "schematic") -> str:
    """Save cellview."""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "{view}" "schematic" "a")'
    result = rb_exec(f'dbSave({cv})', timeout=15)
    return result.strip()


# ═══════════════════════════════════════════════════════════════
# Symbol view operations
# ═══════════════════════════════════════════════════════════════

def create_symbol_rect(lib: str, cell: str,
                       x1: float, y1: float, x2: float, y2: float,
                       layer: str = "instance", purpose: str = "drawing") -> str:
    """Draw rect in symbol view."""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "symbol" "schematicSymbol" "a")'
    result = rb_exec(
        f'dbCreateRect({cv} list("{layer}" "{purpose}") '
        f'list(list({x1} {y1}) list({x2} {y2})))~>objType', timeout=15)
    return result.strip()


def create_symbol_label(lib: str, cell: str, x: float, y: float,
                        text: str, align: str = "centerCenter",
                        layer: str = "device", purpose: str = "drawing") -> str:
    """Add label in symbol view."""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "symbol" "schematicSymbol" "a")'
    result = rb_exec(
        f'dbCreateLabel({cv} list("{layer}" "{purpose}") '
        f'list({x} {y}) "{text}" "{align}" "R0" "stick" 0.0625)', timeout=15)
    return result.strip()


def screenshot(local_path: str, remote_path: str = None) -> str:
    """Screenshot and download."""
    import time
    client = _get_client()
    if remote_path is None:
        remote_path = "/tmp/vb_screenshot.png"
    load_skill_file(str(_T28_ROOT / "skill_code" / "screenshot.il"))
    client.execute_skill(f'takeScreenshot("{remote_path}")', timeout=30)
    time.sleep(1)
    from io_ring.bridge.client import _get_ssh
    ssh = _get_ssh()
    ssh.download_file(remote_path, Path(local_path))
    return local_path
