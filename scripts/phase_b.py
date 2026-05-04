#!/usr/bin/env python3
"""Phase B: TB creation → source/load placement → Maestro → optional simulation.

Reads pin_classifications.json written by the LLM between phases.
Falls back to heuristic classification if the file is absent (warning only).

Usage:
    python scripts/phase_b.py                   # uses .latest_run
    python scripts/phase_b.py --run-dir <path>  # explicit run dir
    python scripts/phase_b.py --run-sim         # build TB then run Maestro simulation
    python scripts/phase_b.py --intent "DC sweep VDD 0→3"

Exit codes:
    0  success
    1  error (printed to stderr)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Path setup — scripts/ lives one level below SIM-IO root
_SIM_IO = Path(__file__).resolve().parent.parent
for _p in (
    _SIM_IO,
    _SIM_IO.parent / "virtuoso-bridge-lite" / "src",
    _SIM_IO.parent / "io-ring-orchestrator-T28",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from virtuoso_bridge import VirtuosoClient
from sim_io.flow import (
    PhaseAResult,
    SimFlowResult,
    load_llm_classifications,
    classify_pin,
    create_tb_cellview,
    place_dut,
    add_wire_labels,
    place_sources_and_loads,
)
from sim_io.pin_types import PinClassification, PinInfo


def run_phase_b(
    phase_a: PhaseAResult,
    *,
    run_sim: bool = False,
    client: VirtuosoClient | None = None,
    user_intent: str = "",
) -> SimFlowResult:
    """Phase B: TB build + source/load placement + Maestro + optional simulation.

    Steps:
      4a. Create {primary_cell}_tb schematic cellview
      4b. Place DUT symbol instance
      4c. Add wire labels (label-based wiring — no explicit wires drawn)
      4d. Place sources, loads, PVSS, GND_REF; set CDF parameters
      4e. Maestro test setup (always runs)
       5. Maestro simulation (if run_sim=True)
          → measurements.json, verify.json, plots/tran_maestro.svg
    """
    if client is None:
        client = VirtuosoClient.from_env()

    lib = phase_a.lib
    primary_cell = phase_a.primary_cell
    tb_cell = phase_a.tb_cell
    pins = phase_a.pins
    run_dir = phase_a.run_dir
    vdd_value = phase_a.vdd_value

    # Load LLM classifications from run_dir/pin_classifications.json
    classifications: dict[str, PinClassification] = load_llm_classifications(
        run_dir, cell=primary_cell
    )

    print(f"\n{'='*60}")
    print(f" Phase B: {lib}/{tb_cell}  (LLM={bool(classifications)})")
    print(f"{'='*60}\n")

    # Step 4a: Create _tb cellview
    create_tb_cellview(client, lib, primary_cell)

    # Step 4b: Place DUT instance
    place_dut(lib, tb_cell, primary_cell)

    # Step 4c: Add wire labels on DUT pins
    labels = add_wire_labels(lib, tb_cell, pins) if pins else []

    # Step 4d: Place sources & loads based on LLM classification
    sources = place_sources_and_loads(
        lib, tb_cell, pins,
        classifications=classifications,
        vdd_value=vdd_value,
        client=client,
    ) if pins else []

    # Step 4e: Maestro setup (always — configures cellview for GUI use too)
    from sim_io.maestro import build_maestro_setup
    from sim_io.site_config import SiteConfig
    from sim_io.sim.config import resolve_sim_config, sim_config_from_site

    site = SiteConfig.from_env()
    deck_config = resolve_sim_config(
        run_dir=run_dir, lib=lib, cell=tb_cell,
        vdd_value=vdd_value, user_intent=user_intent,
    )
    if not deck_config.model_includes:
        deck_config.model_includes = sim_config_from_site(
            vdd_value=vdd_value
        ).model_includes
        print(f"[sim-config] Injected {len(deck_config.model_includes)} model includes from .env")
    try:
        build_maestro_setup(client, lib, tb_cell, deck_config, pins=pins,
                            auto_close=True, classifications=classifications)
        print("[step4e] Maestro setup saved")
    except Exception as exc:
        print(f"[step4e] WARNING: Maestro setup failed: {exc}")

    # Step 5: Maestro simulation (optional)
    sim_run_ok = None
    sim_verdict = None
    plot_paths: list[Path] = []

    if run_sim and pins:
        from sim_io.maestro import run_maestro_sim, parse_maestro_measurements, plot_maestro_waves
        from sim_io.sim.verify import verify_results

        wave_signals = [
            f"/{p.name}" for p in pins
            if classify_pin(p, classifications) not in ("ground", "no_connect")
        ]

        mae_result = run_maestro_sim(
            client, lib, tb_cell,
            test_name=f"{tb_cell}_test",
            timeout=600,
            export_waves=True,
            wave_signals=wave_signals,
            run_dir=run_dir,
        )
        sim_run_ok = mae_result.sim_ok

        if mae_result.sim_ok:
            # Extract Python-accessible measurements from Maestro scalar outputs
            measurements = parse_maestro_measurements(
                mae_result, pins,
                classifications=classifications,
                vdd=vdd_value,
            )
            (run_dir / "measurements.json").write_text(
                json.dumps(measurements, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"[step5] measurements.json written "
                  f"({measurements.get('num_pins_measured', 0)}/{measurements.get('num_pins_total', 0)} pins)")

            # Generate SVG plots from exported waveform text files
            plot_paths = plot_maestro_waves(
                run_dir / "maestro_waves",
                run_dir / "plots",
            )

            # Verify against golden specs → verify.json
            report = verify_results(measurements, vdd=vdd_value, cell=tb_cell)
            report.save(run_dir / "verify.json")
            sim_verdict = report.verdict
            print(f"[step5] verify.json: {sim_verdict} "
                  f"(pass={report.num_pass}, fail={report.num_fail})")

    result = SimFlowResult(
        lib=lib,
        primary_cell=primary_cell,
        tb_cell=tb_cell,
        symbol_exported=phase_a.symbol_exported,
        redistributed=phase_a.redistributed,
        tb_created=True,
        dut_placed=True,
        pins=pins,
        labels_added=labels,
        sources_placed=sources,
        sim_run_ok=sim_run_ok,
        sim_verdict=sim_verdict,
    )
    result.save(run_dir)

    # Summary
    print(f"\n{'='*60}")
    print(f" Phase B Result Summary")
    print(f"{'='*60}")
    print(f"  Output dir:        {run_dir}")
    print(f"  TB cellview:       {lib}/{tb_cell}/schematic")
    print(f"  Pins extracted:    {len(pins)}")
    print(f"  DUT labels added:  {len(labels)}")
    print(f"  Sources placed:    {len(sources)}")
    print(f"  LLM classified:    {bool(classifications)}")
    types: dict[str, int] = {}
    for p in pins:
        t = classify_pin(p, classifications)
        types[t] = types.get(t, 0) + 1
    print(f"  Pin types:         {dict(sorted(types.items()))}")
    if sim_run_ok is not None:
        print(f"  Sim run:           {'OK' if sim_run_ok else 'FAILED'} (maestro)")
    if sim_verdict is not None:
        print(f"  Verify verdict:    {sim_verdict}")
    if plot_paths:
        print(f"  SVG plots:         {len(plot_paths)} file(s) in {run_dir / 'plots'}")
    print(f"{'='*60}\n")

    return result


def _resolve_run_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    latest = _SIM_IO / ".latest_run"
    if not latest.exists():
        raise FileNotFoundError(
            ".latest_run not found — run Phase A first or pass --run-dir"
        )
    return Path(latest.read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SIM-IO Phase B — TB build, source/load placement, Maestro, simulation"
    )
    parser.add_argument("--run-dir", metavar="PATH",
                        help="Run directory from Phase A (default: reads .latest_run)")
    parser.add_argument("--run-sim", action="store_true",
                        help="Run Maestro simulation after TB build")
    parser.add_argument("--intent", default="", metavar="TEXT",
                        help="Free-text simulation intent for deck configuration")
    args = parser.parse_args()

    try:
        run_dir = _resolve_run_dir(args.run_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    phase_a_json = run_dir / "phase_a_result.json"
    if not phase_a_json.exists():
        print(f"ERROR: {phase_a_json} not found — run Phase A first.", file=sys.stderr)
        sys.exit(1)

    classif_json = run_dir / "pin_classifications.json"
    if not classif_json.exists():
        print(f"WARNING: {classif_json} not found — falling back to heuristic classification.",
              file=sys.stderr)

    try:
        phase_a = PhaseAResult.load(phase_a_json)
        result = run_phase_b(
            phase_a,
            run_sim=args.run_sim,
            user_intent=args.intent,
        )
        print(f"\nPhase B complete.")
        print(f"  TB cellview : {result.lib}/{result.tb_cell}/schematic")
        print(f"  result.json : {run_dir / 'result.json'}")
        if args.run_sim and result.sim_run_ok is not None:
            status = "OK" if result.sim_run_ok else "FAILED"
            print(f"  Simulation  : {status} (maestro)")
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
