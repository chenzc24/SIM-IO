"""Maestro simulation runner — execute + read results.

Runs Maestro simulation in background mode (no GUI window),
waits for completion, then reads structured results.

Background mode is automation-safe: no modal dialogs can block
the SKILL channel during simulation.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

_PKG_DIR = Path(__file__).resolve().parent
_SIM_IO = _PKG_DIR.parent.parent
_BRIDGE_LITE = _SIM_IO.parent / "virtuoso-bridge-lite" / "src"
for p in (_BRIDGE_LITE,):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import (
    open_session,
    close_session,
    run_and_wait,
    read_results,
    export_waveform,
)


# ── Result Data Structure ──────────────────────────────────────

@dataclass
class MaestroSimResult:
    """Structured result from a Maestro simulation run."""
    lib: str
    tb_cell: str
    test_name: str
    history: str = ""
    sim_ok: bool = False
    overall_spec: Optional[str] = None
    overall_yield: Optional[str] = None
    points: list[dict] = field(default_factory=list)
    waveform_paths: list[str] = field(default_factory=list)
    run_dir: Optional[str] = None

    def save(self, run_dir: Path) -> None:
        data = asdict(self)
        data["run_dir"] = str(run_dir)
        (run_dir / "maestro_result.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


# ── Simulation Runner ─────────────────────────────────────────

def run_maestro_sim(
    client: VirtuosoClient,
    lib: str,
    tb_cell: str,
    *,
    test_name: str = "",
    timeout: int = 600,
    export_waves: bool = True,
    wave_signals: list[str] | None = None,
    wave_analysis: str = "tran",
    run_dir: Path | None = None,
) -> MaestroSimResult:
    """Run Maestro simulation and read results in background mode.

    Opens a background session, runs simulation with non-blocking
    callback polling, reads structured results, optionally exports
    waveforms, then closes the session.

    Parameters
    ----------
    client : VirtuosoClient
    lib, tb_cell : Library and testbench cell names
    test_name : Maestro test name (default: tb_cell + "_test")
    timeout : Maximum wait time for simulation (seconds)
    export_waves : if True, export waveforms for key signals
    wave_signals : list of signal paths to export (e.g. ["/VOUT"])
    wave_analysis : analysis type for waveform export (default: "tran")
    run_dir : output directory for saving results
    """
    tname = test_name or f"{tb_cell}_test"
    result = MaestroSimResult(lib=lib, tb_cell=tb_cell, test_name=tname)

    print(f"\n{'='*60}")
    print(f" Maestro Sim: {lib}/{tb_cell}")
    print(f" Test: {tname}")
    print(f" Timeout: {timeout}s")
    print(f"{'='*60}\n")

    # Step 1: Open background session
    session = open_session(client, lib, tb_cell)
    print(f"[maestro-sim] Session: {session} (background)")

    try:
        # Step 2: Run simulation + wait for completion
        print(f"[maestro-sim] Starting simulation...")
        history, status = run_and_wait(
            client, session=session, timeout=timeout
        )
        history_name = history.strip().strip('"') if history else ""
        result.history = history_name
        maestro_job_ok = status == "done"
        print(f"[maestro-sim] Maestro job {'completed' if maestro_job_ok else 'FAILED'}: "
              f"{history_name} (status={status})")

        if not maestro_job_ok:
            result.sim_ok = False
            return result

        # Step 3: Read structured results (per-point × per-output)
        print(f"[maestro-sim] Reading results...")
        results = read_results(client, session, lib=lib, cell=tb_cell)
        result.overall_spec = results.get("overall_spec")
        result.overall_yield = results.get("overall_yield")
        result.points = results.get("points", [])

        # Detect Spectre failure: Maestro job "done" ≠ Spectre converged.
        # If read_results returns empty points, Spectre likely errored.
        has_results = bool(result.points)
        if not has_results:
            print(f"[maestro-sim] WARNING: Maestro job completed but no result "
                  f"points — Spectre may have failed inside Maestro")
        result.sim_ok = maestro_job_ok and has_results
        print(f"[maestro-sim] Simulation {'OK' if result.sim_ok else 'FAILED'}: "
              f"job={'done' if maestro_job_ok else 'failed'}, "
              f"results={'present' if has_results else 'empty'}")

        if not result.sim_ok:
            # Try to extract Spectre error from the log
            _check_spectre_log(client, session, lib, tb_cell, history_name)
            return result

        # Print summary
        for pt in result.points:
            pn = pt.get("point", "?")
            params = pt.get("parameters", {}) or {}
            param_str = ", ".join(f"{k}={v}" for k, v in params.items())
            print(f"  Point {pn}" + (f"  ({param_str})" if param_str else ""))
            for out_name, info in (pt.get("outputs", {}) or {}).items():
                val = info.get("value", "")
                pf = info.get("pass_fail", "")
                tag = f" [{pf}]" if pf else ""
                print(f"    {out_name} = {val}{tag}")

        if result.overall_spec:
            print(f"  Overall spec: {result.overall_spec}")

        # Step 4: Export waveforms (optional)
        if export_waves and wave_signals and run_dir:
            _export_waveforms(
                client, session, lib, tb_cell,
                wave_signals, wave_analysis, history_name, run_dir, result,
            )

    except Exception as e:
        print(f"[maestro-sim] ERROR: {e}")
        result.sim_ok = False

    finally:
        # Step 5: Always close the session
        try:
            close_session(client, session)
            print(f"[maestro-sim] Session closed")
        except Exception as e:
            print(f"[maestro-sim] WARNING: close_session failed: {e}")

    # Save result metadata
    if run_dir:
        result.save(run_dir)

    _print_sim_summary(result)
    return result


def _export_waveforms(
    client: VirtuosoClient,
    session: str,
    lib: str,
    cell: str,
    signals: list[str],
    analysis: str,
    history: str,
    run_dir: Path,
    result: MaestroSimResult,
) -> None:
    """Export waveforms via OCEAN for specified signals."""
    waves_dir = run_dir / "maestro_waves"
    waves_dir.mkdir(parents=True, exist_ok=True)
    client.execute_skill(f'system("mkdir -p /tmp/vb_sim_waves")', timeout=10)

    for sig in signals:
        safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", sig).strip("_") or "sig"
        local_path = str(waves_dir / f"{safe_name}.txt")
        try:
            # OCEAN expression: quotes inside SKILL string must be escaped
            escaped_sig = sig.replace('"', '\\"')
            export_waveform(
                client, session,
                expression=f'v(\\"{escaped_sig}\\")',
                local_path=local_path,
                analysis=analysis,
                history=history,
            )
            result.waveform_paths.append(local_path)
            print(f"[maestro-sim] Waveform: {sig} → {local_path}")
        except Exception as e:
            print(f"[maestro-sim] WARNING: waveform export failed for {sig}: {e}")


def _print_sim_summary(result: MaestroSimResult) -> None:
    print(f"\n{'='*60}")
    print(f" Maestro Sim Summary")
    print(f"{'='*60}")
    print(f"  Cell:      {result.lib}/{result.tb_cell}")
    print(f"  Test:      {result.test_name}")
    print(f"  History:   {result.history}")
    print(f"  Status:    {'OK' if result.sim_ok else 'FAILED'}")
    print(f"  Points:    {len(result.points)}")
    if result.overall_spec:
        print(f"  Spec:      {result.overall_spec}")
    if result.waveform_paths:
        print(f"  Waveforms: {len(result.waveform_paths)} files")
    print(f"{'='*60}\n")


def _check_spectre_log(
    client: VirtuosoClient,
    session: str,
    lib: str,
    cell: str,
    history: str,
) -> None:
    """Attempt to read the Spectre log and print error summary.

    When Maestro job completes but Spectre fails inside, this reads
    the Spectre log to surface the actual error (e.g. SFE-23 undefined
    model) instead of silently reporting success.
    """
    try:
        # Find the Spectre log directory from the results path
        log_skill = (
            f'let((p) p = ddGetObj("{lib}")~>readPath '
            f'strcat(p "/{cell}/maestro/results/maestro/{history}/"'
            f' " Spectre/{history}/spectre.log"))'
        )
        r = client.execute_skill(log_skill, timeout=15)
        log_path = (r.output or "").strip().strip('"')
        if not log_path or log_path == "nil":
            # Try alternative: just grep the result dir for .log files
            return

        # Read first and last portions of the log for errors
        r = client.execute_skill(
            f'let((f lines) '
            f'f = infile("{log_path}") '
            f'lines = nil '
            f'when(f '
            f'  for(i 0 200 '
            f'    let((line) line = gets(line f) '
            f'    when(line lines = cons(line lines)))) '
            f'  closePort(f)) '
            f'nreverse(lines))',
            timeout=15,
        )
        log_head = r.output or ""
        errors = re.findall(r'(SFE-\d+.*|Error:.*|error:.*|FATAL.*)', log_head)
        if errors:
            print(f"[maestro-sim] Spectre errors detected:")
            for err in errors[:10]:
                print(f"  {err.strip()}")
    except Exception as e:
        # Best-effort — don't fail the whole flow if log reading fails
        print(f"[maestro-sim] NOTE: Could not read Spectre log: {e}")
