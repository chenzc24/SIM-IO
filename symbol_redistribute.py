"""Redistribute symbol pins evenly on 4 sides.

3-Step Architecture:
  Step 0: TSG (fresh symbol from schematic)
  Step 1: Extract all symbol info via reusable SKILL extractor
  Step 2: Calculate new layout (body + pin positions) in pure Python
  Step 3: Generate + execute design-specific SKILL in one shot

Total network round-trips: 5 (down from 3+N+ceil(N/22)).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "virtuoso-bridge-lite" / "src"))

from virtuoso_bridge import VirtuosoClient

from symbol_layout_engine import (
    LayoutConfig,
    LayoutEngine,
    generate_apply_skill,
    parse_symbol_info,
)

_SKILL_DIR = Path(__file__).resolve().parent / "skill_code"


def run(lib: str, cell: str):
    client = VirtuosoClient.from_env()

    # ── Step 0: Fresh TSG ─────────────────────────────────────
    print("=== Step 0: Regenerate symbol via TSG ===")
    client.execute_skill(f'ddDeleteCellView("{lib}" "{cell}" "symbol")')
    client.execute_skill('schSetEnv("ssgSortPins" "geometric")')
    r = client.execute_skill(
        f'let((pl) '
        f'pl = schSchemToPinList("{lib}" "{cell}" "schematic") '
        f'schPinListToSymbol("{lib}" "{cell}" "symbol" pl))'
    )
    print(f"TSG: {'OK' if not r.errors else r.errors}")

    # ── Step 1: Extract ───────────────────────────────────────
    print("\n=== Step 1: Extract symbol info ===")
    load_r = client.load_il(str(_SKILL_DIR / "extract_symbol_info.il"))
    if not load_r.ok:
        print(f"  ERROR loading extractor: {load_r.errors}")
        return
    r = client.execute_skill(f'extractSymbolInfo("{lib}" "{cell}")', timeout=60)
    if r.errors:
        print(f"  ERROR extracting: {r.errors}")
        return

    info = parse_symbol_info(r.output)
    print(f"  Extracted: {len(info.rects)} rects, {len(info.lines)} lines, "
          f"{len(info.labels)} labels, {len(info.terminals)} terminals")

    # ── Step 2: Calculate layout ──────────────────────────────
    print("\n=== Step 2: Calculate layout ===")
    engine = LayoutEngine(LayoutConfig())
    result = engine.redesign(info)
    body = result.body
    print(f"  Outer: ({body.outer_left:.3f}, {body.outer_bottom:.3f}) "
          f"to ({body.outer_right:.3f}, {body.outer_top:.3f})  "
          f"({body.outer_right - body.outer_left:.3f}x"
          f"{body.outer_top - body.outer_bottom:.3f})")
    for side_name in ["left", "right", "top", "bottom"]:
        count = sum(1 for p in result.pins if p.side.value == side_name)
        print(f"  {side_name}: {count} pins")

    # ── Step 3: Apply ─────────────────────────────────────────
    print("\n=== Step 3: Apply layout ===")
    skill_code = generate_apply_skill(lib, cell, result, engine.config)

    # Write to temp .il file and upload+load via bridge (T28 proven pattern)
    apply_il = _SKILL_DIR / "_apply_layout.il"
    apply_il.write_text(skill_code, encoding="utf-8")
    load_r = client.load_il(str(apply_il), timeout=120)
    if not load_r.ok:
        print(f"  ERROR: {load_r.errors}")
        return
    print(f"  OK — applied via load_il")

    # ── Verify ────────────────────────────────────────────────
    print("\n=== Verify ===")
    sym = f'dbOpenCellViewByType("{lib}" "{cell}" "symbol" nil "r")'
    r = client.execute_skill(f'{sym}~>bBox')
    print(f"  Final bBox: {r.output}")
    r = client.execute_skill(f'length({sym}~>terminals)')
    print(f"  Terminals: {r.output}")
    r = client.execute_skill(f'length({sym}~>shapes)')
    print(f"  Shapes: {r.output}")

    print("\n=== DONE ===")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python symbol_redistribute.py <lib> <cell>")
        print("Example: python symbol_redistribute.py LLM_Layout_Design_Lab IO_RING_12x12")
        sys.exit(1)
    run(sys.argv[1], sys.argv[2])
