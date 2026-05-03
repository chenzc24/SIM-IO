"""Symbol layout engine — pure Python redesign + SKILL generation.

Step 2: Parse extraction data, classify pins, calculate new body + positions.
Step 3: Generate a single SKILL script that applies the entire layout.

No SKILL calls, no network — all pure computation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import isfinite


# ── Enums ──────────────────────────────────────────────────────

class Side(Enum):
    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


# ── Data structures (extraction) ──────────────────────────────

@dataclass(frozen=True)
class RectData:
    layer: str
    purpose: str
    left: float
    bottom: float
    right: float
    top: float

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def cy(self) -> float:
        return (self.bottom + self.top) / 2.0


@dataclass(frozen=True)
class LineData:
    layer: str
    purpose: str
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True)
class LabelData:
    layer: str
    purpose: str
    text: str
    x: float
    y: float


@dataclass(frozen=True)
class TermData:
    index: int
    pin_index: int
    name: str
    direction: str
    cx: float
    cy: float


@dataclass
class SymbolInfo:
    rects: list[RectData] = field(default_factory=list)
    lines: list[LineData] = field(default_factory=list)
    labels: list[LabelData] = field(default_factory=list)
    terminals: list[TermData] = field(default_factory=list)


# ── Data structures (layout result) ───────────────────────────

@dataclass
class LayoutConfig:
    pin_margin: float = 0.075
    body_margin: float = 1.25
    pin_pitch: float = 0.5
    wire_length: float = 0.375
    end_margin: float = 2.0
    label_inset: float = 0.125
    center_x: float = 2.5
    center_y: float = -0.5
    min_body_half: float = 0.125


@dataclass(frozen=True)
class PinLayout:
    term_index: int
    pin_index: int
    name: str
    direction: str
    side: Side
    new_cx: float
    new_cy: float
    wire_x1: float
    wire_y1: float
    wire_x2: float
    wire_y2: float
    label_x: float
    label_y: float
    label_orig_x: float = 0.0
    label_orig_y: float = 0.0
    is_core: bool = False


@dataclass(frozen=True)
class BodyLayout:
    outer_left: float
    outer_bottom: float
    outer_right: float
    outer_top: float
    inner_left: float
    inner_bottom: float
    inner_right: float
    inner_top: float


@dataclass
class LayoutResult:
    body: BodyLayout
    pins: list[PinLayout] = field(default_factory=list)


# ── Parsing ────────────────────────────────────────────────────

def parse_symbol_info(raw_output: str) -> SymbolInfo:
    """Parse pipe-delimited output from extractSymbolInfo SKILL.

    Handles SKILL string escaping: output may be wrapped in quotes with
    ``\\n`` escape sequences instead of real newlines.
    """
    info = SymbolInfo()

    # SKILL string literal: strip surrounding quotes, unescape \n
    data = raw_output.strip()
    if data.startswith('"') and data.endswith('"'):
        data = data[1:-1]
    data = data.replace('\\n', '\n')

    for line in data.split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if not parts:
            continue
        kind = parts[0]
        try:
            if kind == "RECT" and len(parts) == 7:
                info.rects.append(RectData(
                    layer=parts[1], purpose=parts[2],
                    left=float(parts[3]), bottom=float(parts[4]),
                    right=float(parts[5]), top=float(parts[6]),
                ))
            elif kind == "LINE" and len(parts) == 7:
                info.lines.append(LineData(
                    layer=parts[1], purpose=parts[2],
                    x1=float(parts[3]), y1=float(parts[4]),
                    x2=float(parts[5]), y2=float(parts[6]),
                ))
            elif kind == "LABEL" and len(parts) == 6:
                info.labels.append(LabelData(
                    layer=parts[1], purpose=parts[2],
                    text=parts[3],
                    x=float(parts[4]), y=float(parts[5]),
                ))
            elif kind == "TERM" and len(parts) == 7:
                info.terminals.append(TermData(
                    index=int(parts[1]), pin_index=int(parts[2]),
                    name=parts[3], direction=parts[4],
                    cx=float(parts[5]), cy=float(parts[6]),
                ))
        except (ValueError, IndexError):
            continue
    return info


# ── Layout Engine ──────────────────────────────────────────────

class LayoutEngine:
    """Pure-Python layout engine for symbol pin redistribution."""

    def __init__(self, config: LayoutConfig | None = None):
        self.config = config or LayoutConfig()

    def redesign(self, info: SymbolInfo) -> LayoutResult:
        classified = self._classify_pins(info)
        body = self._calc_body(classified)
        label_map = self._build_label_map(info)
        pins = self._calc_pin_layouts(classified, body, label_map)
        return LayoutResult(body=body, pins=pins)

    # ── Classify pins by side ─────────────────────────────────

    def _classify_pins(self, info: SymbolInfo) -> dict[Side, list[TermData]]:
        if not info.terminals:
            return {s: [] for s in Side}

        body_rects = [r for r in info.rects
                      if r.layer == "instance" and r.purpose == "drawing"]
        if body_rects:
            body_L = min(r.left for r in body_rects)
            body_R = max(r.right for r in body_rects)
            body_B = min(r.bottom for r in body_rects)
            body_T = max(r.top for r in body_rects)
        else:
            all_xs = [r.left for r in info.rects] + [r.right for r in info.rects]
            all_ys = [r.bottom for r in info.rects] + [r.top for r in info.rects]
            body_L = min(all_xs) if all_xs else 0.0
            body_R = max(all_xs) if all_xs else 0.0
            body_B = min(all_ys) if all_ys else 0.0
            body_T = max(all_ys) if all_ys else 0.0

        classified: dict[Side, list[TermData]] = {s: [] for s in Side}

        for term in info.terminals:
            dists = {
                Side.LEFT: abs(term.cx - body_L),
                Side.RIGHT: abs(term.cx - body_R),
                Side.TOP: abs(term.cy - body_T),
                Side.BOTTOM: abs(term.cy - body_B),
            }
            side = min(dists, key=dists.__getitem__)
            classified[side].append(term)

        for side in (Side.LEFT, Side.RIGHT):
            classified[side].sort(key=lambda t: t.cy, reverse=True)
        for side in (Side.TOP, Side.BOTTOM):
            classified[side].sort(key=lambda t: t.cx)

        return classified

    # ── Build label position map (for duplicate-safe matching) ──

    @staticmethod
    def _build_label_map(info: SymbolInfo) -> dict[str, list[tuple[float, float]]]:
        """Map label text → list of (x, y) positions from extraction."""
        label_map: dict[str, list[tuple[float, float]]] = {}
        for lbl in info.labels:
            label_map.setdefault(lbl.text, []).append((lbl.x, lbl.y))
        return label_map

    @staticmethod
    def _find_label_pos(
        label_map: dict[str, list[tuple[float, float]]],
        name: str, pin_cx: float, pin_cy: float,
    ) -> tuple[float, float]:
        """Find the label position closest to a pin's current position."""
        positions = label_map.get(name, [])
        if not positions:
            return (pin_cx, pin_cy)
        return min(positions, key=lambda p: (p[0] - pin_cx) ** 2 + (p[1] - pin_cy) ** 2)

    # ── Calculate body dimensions ─────────────────────────────

    def _calc_body(self, classified: dict[Side, list[TermData]]) -> BodyLayout:
        cfg = self.config

        _CORE_SFX = "_CORE"
        n_vert = max(
            sum(1 for t in classified.get(Side.LEFT, []) if not t.name.endswith(_CORE_SFX)),
            sum(1 for t in classified.get(Side.RIGHT, []) if not t.name.endswith(_CORE_SFX)),
        )
        n_horiz = max(
            sum(1 for t in classified.get(Side.TOP, []) if not t.name.endswith(_CORE_SFX)),
            sum(1 for t in classified.get(Side.BOTTOM, []) if not t.name.endswith(_CORE_SFX)),
        )

        vert_span = (n_vert - 1) * cfg.pin_pitch if n_vert > 1 else 0.0
        body_height = max(vert_span + 2 * cfg.end_margin, 2 * cfg.min_body_half)

        horiz_span = (n_horiz - 1) * cfg.pin_pitch if n_horiz > 1 else 0.0
        body_width = max(horiz_span + 2 * cfg.end_margin, 2 * cfg.min_body_half)

        oL = cfg.center_x - body_width / 2
        oR = cfg.center_x + body_width / 2
        oB = cfg.center_y - body_height / 2
        oT = cfg.center_y + body_height / 2

        return BodyLayout(
            outer_left=oL, outer_bottom=oB, outer_right=oR, outer_top=oT,
            inner_left=oL + cfg.body_margin,
            inner_bottom=oB + cfg.body_margin,
            inner_right=oR - cfg.body_margin,
            inner_top=oT - cfg.body_margin,
        )

    # ── Calculate pin layouts (position + wire + label) ───────

    def _calc_pin_layouts(
        self, classified: dict[Side, list[TermData]], body: BodyLayout,
        label_map: dict[str, list[tuple[float, float]]],
    ) -> list[PinLayout]:
        cfg = self.config
        _CORE_SFX = "_CORE"

        # Separate CORE and non-CORE terminals
        non_core: dict[Side, list[TermData]] = {s: [] for s in Side}
        core_with_side: list[tuple[TermData, Side]] = []
        for side in Side:
            for t in classified.get(side, []):
                if t.name.endswith(_CORE_SFX):
                    core_with_side.append((t, side))
                else:
                    non_core[side].append(t)

        # Label consumption tracker — each label matched to at most one pin
        consumed: set[tuple[str, float, float]] = set()

        def claim_label(name: str, pin_cx: float, pin_cy: float) -> tuple[float, float]:
            positions = label_map.get(name, [])
            available = [(x, y) for x, y in positions
                         if (name, x, y) not in consumed]
            if not available:
                return (pin_cx, pin_cy)
            best = min(available,
                       key=lambda p: (p[0] - pin_cx) ** 2 + (p[1] - pin_cy) ** 2)
            consumed.add((name, best[0], best[1]))
            return best

        # Pass 1: distribute non-CORE pins evenly on outer rect
        layouts: list[PinLayout] = []
        edge_pos: dict[str, tuple[float, float, Side]] = {}

        for side in Side:
            terms = non_core[side]
            n = len(terms)
            if n == 0:
                continue
            pin_span = (n - 1) * cfg.pin_pitch if n > 1 else 0.0

            for i, term in enumerate(terms):
                frac = i / (n - 1) if n > 1 else 0.5
                if side in (Side.LEFT, Side.RIGHT):
                    cy = body.outer_top - cfg.end_margin - frac * pin_span
                    edge_pos[term.name] = (0.0, cy, side)
                else:
                    cx = body.outer_left + cfg.end_margin + frac * pin_span
                    edge_pos[term.name] = (cx, 0.0, side)

                # Outer layout: wire from outer edge outward, pin at far end, label inside
                if side == Side.LEFT:
                    wx1, wy1 = body.outer_left, cy
                    wx2, wy2 = body.outer_left - cfg.wire_length, cy
                    pin_cx, pin_cy = wx2, cy
                    lx, ly = body.outer_left + cfg.label_inset, cy
                elif side == Side.RIGHT:
                    wx1, wy1 = body.outer_right, cy
                    wx2, wy2 = body.outer_right + cfg.wire_length, cy
                    pin_cx, pin_cy = wx2, cy
                    lx, ly = body.outer_right - cfg.label_inset, cy
                elif side == Side.TOP:
                    wx1, wy1 = cx, body.outer_top
                    wx2, wy2 = cx, body.outer_top + cfg.wire_length
                    pin_cx, pin_cy = cx, wy2
                    lx, ly = cx, body.outer_top - cfg.label_inset
                else:  # BOTTOM
                    wx1, wy1 = cx, body.outer_bottom
                    wx2, wy2 = cx, body.outer_bottom - cfg.wire_length
                    pin_cx, pin_cy = cx, wy2
                    lx, ly = cx, body.outer_bottom + cfg.label_inset

                orig_lx, orig_ly = claim_label(term.name, term.cx, term.cy)

                layouts.append(PinLayout(
                    term_index=term.index, pin_index=term.pin_index,
                    name=term.name, direction=term.direction, side=side,
                    new_cx=pin_cx, new_cy=pin_cy,
                    wire_x1=wx1, wire_y1=wy1,
                    wire_x2=wx2, wire_y2=wy2,
                    label_x=lx, label_y=ly,
                    label_orig_x=orig_lx, label_orig_y=orig_ly,
                ))

        # Pass 2: place CORE pins on inner rect at their base signal's position
        for term, orig_side in core_with_side:
            base_name = term.name[:-len(_CORE_SFX)]
            base_info = edge_pos.get(base_name)

            if base_info:
                bx, by, base_side = base_info
                if base_side in (Side.LEFT, Side.RIGHT):
                    inner_cx = body.inner_left if base_side == Side.LEFT else body.inner_right
                    inner_cy = by
                else:
                    inner_cx = bx
                    inner_cy = body.inner_top if base_side == Side.TOP else body.inner_bottom
            else:
                inner_cx, inner_cy = body.inner_left, body.outer_top

            orig_lx, orig_ly = claim_label(term.name, term.cx, term.cy)

            layouts.append(PinLayout(
                term_index=term.index, pin_index=term.pin_index,
                name=term.name, direction=term.direction, side=orig_side,
                new_cx=inner_cx, new_cy=inner_cy,
                wire_x1=0.0, wire_y1=0.0,
                wire_x2=0.0, wire_y2=0.0,
                label_x=0.0, label_y=0.0,
                label_orig_x=orig_lx, label_orig_y=orig_ly,
                is_core=True,
            ))

        return layouts


# ── SKILL Generation (Step 3) ─────────────────────────────────

def generate_apply_skill(
    lib: str, cell: str, result: LayoutResult,
    config: LayoutConfig | None = None,
) -> str:
    """Generate ONE SKILL script that applies the entire layout.

    Each statement on its own line (T28 proven pattern for load() compatibility).
    Handles two pin types:
      - *_CORE: pin moved to inner rect at base signal position; label deleted, no wire
      - non-CORE: pin+wire+label on outer rect (CORE signals excluded from distribution)
    """
    cfg = config or LayoutConfig()
    body = result.body
    pins = result.pins

    lines: list[str] = []

    # Open cellview
    lines.append('let((cv term pin fig bb w h lbl shapes)')
    lines.append(f'  cv = dbOpenCellViewByType("{lib}" "{cell}" "symbol" "schematicSymbol" "a")')
    lines.append(f'  unless(cv error("APPLY-LAYOUT: cannot open cellview"))')

    # Delete old body rects (instance/drawing + device/drawing for backward compat)
    lines.append('  shapes = setof(s cv~>shapes s~>objType == "rect" &&')
    lines.append('    ((s~>layerName == "instance" && s~>purpose == "drawing") ||')
    lines.append('     (s~>layerName == "device"   && s~>purpose == "drawing")))')
    lines.append('  foreach(s shapes dbDeleteObject(s))')

    # Delete old wire lines (device/drawing lines)
    lines.append('  shapes = setof(s cv~>shapes')
    lines.append('    s~>objType == "line" && s~>layerName == "device" && s~>purpose == "drawing")')
    lines.append('  foreach(s shapes dbDeleteObject(s))')

    # Create new outer body rect (device/drawing — same format as inner)
    lines.append(
        f'  dbCreateRect(cv list("device" "drawing")'
        f' list(list({body.outer_left:g} {body.outer_bottom:g})'
        f' list({body.outer_right:g} {body.outer_top:g})))')

    # Create new inner body rect (device/drawing)
    lines.append(
        f'  dbCreateRect(cv list("device" "drawing")'
        f' list(list({body.inner_left:g} {body.inner_bottom:g})'
        f' list({body.inner_right:g} {body.inner_top:g})))')

    # Process each pin
    for pin in pins:
        tidx = pin.term_index
        pidx = pin.pin_index
        ncx = pin.new_cx
        ncy = pin.new_cy
        name = pin.name
        olx, oly = pin.label_orig_x, pin.label_orig_y

        # Move pin figure (terminal index + pin sub-index for duplicate safety)
        lines.append(f'  term = nth({tidx} cv~>terminals)')
        lines.append(f'  when(term')
        lines.append(f'    pin = nth({pidx} term~>pins)')
        lines.append(f'    fig = car(pin~>figs)')
        lines.append(f'    bb = fig~>bBox')
        lines.append(f'    w = car(cadr(bb)) - car(car(bb))')
        lines.append(f'    h = cadr(cadr(bb)) - cadr(car(bb))')
        lines.append(
            f'    fig~>bBox = list('
            f'list({ncx:g} - w/2.0 {ncy:g} - h/2.0)'
            f' list({ncx:g} + w/2.0 {ncy:g} + h/2.0))')
        lines.append(f'  )')

        if pin.is_core:
            # CORE: delete label matched by original position
            lines.append(
                f'  lbl = car(setof(s cv~>shapes'
                f' s~>objType == "label" && s~>theLabel == "{name}"'
                f' && abs(car(s~>xy) - {olx:g}) < 0.01'
                f' && abs(cadr(s~>xy) - {oly:g}) < 0.01))')
            lines.append(f'  when(lbl dbDeleteObject(lbl))')
        else:
            # Non-CORE: create wire + move label matched by original position
            wx1, wy1 = pin.wire_x1, pin.wire_y1
            wx2, wy2 = pin.wire_x2, pin.wire_y2
            lx, ly = pin.label_x, pin.label_y

            lines.append(
                f'  dbCreateLine(cv list("device" "drawing")'
                f' list(list({wx1:g} {wy1:g}) list({wx2:g} {wy2:g})))')

            lines.append(
                f'  lbl = car(setof(s cv~>shapes'
                f' s~>objType == "label" && s~>theLabel == "{name}"'
                f' && abs(car(s~>xy) - {olx:g}) < 0.01'
                f' && abs(cadr(s~>xy) - {oly:g}) < 0.01))')
            lines.append(f'  when(lbl lbl~>xy = list({lx:g} {ly:g}))')

    # Save + return
    lines.append(f'  dbSave(cv)')
    n_core = sum(1 for p in pins if p.is_core)
    lines.append(f'  printf("APPLY-LAYOUT: OK  pins={len(pins)}  core={n_core}")')
    lines.append(f'  t')
    lines.append(f')')

    return "\n".join(lines) + "\n"
