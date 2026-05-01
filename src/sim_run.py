"""
Simulation Run Pipeline — Netlist Export → Spectre Execution → Result Parsing.

Step 3a: export_netlist()  — si batch netlist export from _tb schematic
Step 3b: build_sim_deck() — append model include + analysis + options
Step 3c: run_spectre()    — wrapper around SpectreSimulator
Step 3d: parse_results()  — measurement extraction from PSF data
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

_SRC_DIR = Path(__file__).resolve().parent
_SIM_IO = _SRC_DIR.parent
_BRIDGE_LITE = _SIM_IO.parent / "virtuoso-bridge-lite" / "src"
for p in (_SRC_DIR, _BRIDGE_LITE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.models import ExecutionStatus, SimulationResult
from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

from sim_deck_template import SimConfig, build_sim_deck_from_file
from pin_types import PinInfo, classify_pin_heuristic

_OUTPUT_ROOT = _SIM_IO / "output"

# Remote directory for si batch netlist export
_SI_REMOTE_DIR = "/tmp/sim_io_si_run"


# ── Step 3a: Export Netlist ────────────────────────────────────

def export_netlist(
    client: VirtuosoClient,
    lib: str,
    tb_cell: str,
    run_dir: Path,
    *,
    cds_lib: str = "",
) -> Optional[Path]:
    """Export Spectre netlist from _tb schematic via si batch netlister.

    Two-phase:
      1. SKILL: simInitEnvWithArgs() generates si.env on remote
      2. Shell: si -batch -command nl produces the netlist

    Returns the path to the downloaded netlist file, or None on failure.
    """
    print(f"[step3a] Exporting netlist for {lib}/{tb_cell}")

    # 1. Generate si.env on remote
    r = client.execute_skill(f'sh("mkdir -p {_SI_REMOTE_DIR}")')
    r = client.execute_skill(
        f'simInitEnvWithArgs("{_SI_REMOTE_DIR}" "{lib}" "{tb_cell}" '
        f'"schematic" "spectre" nil)'
    )
    if r.errors:
        print(f"[step3a] ERROR: simInitEnvWithArgs failed: {r.errors}")
        return None

    # 2. Run si batch netlister via shell command
    #    Must use shell (not SKILL system()) to avoid CIW deadlock
    cds_arg = f" -cdslib {shlex.quote(cds_lib)}" if cds_lib else ""
    si_cmd = f"cd {_SI_REMOTE_DIR} ; si -batch{cds_arg} -command nl"

    r = client.run_shell_command(si_cmd, timeout=120)
    if r.errors:
        print(f"[step3a] WARNING: si returned errors: {r.errors}")

    # 3. Download netlist
    local_netlist = run_dir / "netlist.scs"
    r = client.download_file(f"{_SI_REMOTE_DIR}/netlist", str(local_netlist))
    if not r.ok:
        # Try alternate output location
        alt_path = f"{_SI_REMOTE_DIR}/netlist/netlist"
        r = client.download_file(alt_path, str(local_netlist))

    if not local_netlist.exists():
        print(f"[step3a] ERROR: Failed to download netlist")
        return None

    print(f"[step3a] Netlist exported: {local_netlist} ({local_netlist.stat().st_size} bytes)")
    return local_netlist


# ── Step 3b: Build Sim Deck ────────────────────────────────────

def build_deck(
    netlist_path: Path,
    config: SimConfig,
    run_dir: Path,
) -> Path:
    """Build a complete Spectre deck from si netlist + SimConfig.

    Returns the path to the complete deck file.
    """
    deck_text = build_sim_deck_from_file(netlist_path, config)
    deck_path = run_dir / "deck.scs"
    deck_path.write_text(deck_text, encoding="utf-8")
    print(f"[step3b] Deck built: {deck_path}")
    return deck_path


# ── Step 3c: Run Spectre ──────────────────────────────────────

def run_spectre(
    deck_path: Path,
    run_dir: Path,
    *,
    spectre_cmd: str = "",
    mode: str = "spectre",
    timeout: int = 600,
) -> SimulationResult:
    """Run Spectre simulation on a complete deck.

    Wraps SpectreSimulator.from_env() — handles local vs remote automatically.
    """
    if not spectre_cmd:
        spectre_cmd = os.getenv("SPECTRE_CMD", "spectre")

    print(f"[step3c] Running Spectre (mode={mode}, timeout={timeout}s)")

    sim = SpectreSimulator.from_env(
        spectre_cmd=spectre_cmd,
        spectre_args=spectre_mode_args(mode),
        timeout=timeout,
        work_dir=run_dir,
        output_format="psfascii",
    )

    result = sim.run_simulation(deck_path, {})

    if result.ok:
        signals = list(result.data.keys())
        print(f"[step3c] Spectre OK — {len(signals)} signals")
    else:
        print(f"[step3c] Spectre FAILED: {result.errors[:3]}")

    # Save result metadata
    result_meta = {
        "status": result.status.value,
        "tool_version": result.tool_version,
        "errors": result.errors,
        "warnings": result.warnings[:5],
        "num_signals": len(result.data) if result.data else 0,
    }
    (run_dir / "spectre_result.json").write_text(
        json.dumps(result_meta, indent=2), encoding="utf-8"
    )

    return result


# ── Step 3d: Parse Results ────────────────────────────────────

def _measure_tran(data: dict, signal: str) -> dict:
    """Extract key metrics from a transient signal."""
    try:
        import numpy as np
    except ImportError:
        # Fallback without numpy
        values = data.get(signal, [])
        if not values:
            return {"signal": signal, "error": "no data"}
        vmax = max(values)
        vmin = min(values)
        return {
            "signal": signal,
            "vmax": vmax,
            "vmin": vmin,
            "vavg": sum(values) / len(values),
            "vpp": vmax - vmin,
        }

    time = np.array(data.get("time", []), dtype=float)
    v = np.array(data.get(signal, []), dtype=float)
    if len(v) == 0:
        return {"signal": signal, "error": "no data"}

    metrics = {
        "signal": signal,
        "vmax": float(np.max(v)),
        "vmin": float(np.min(v)),
        "vavg": float(np.mean(v)),
        "vpp": float(np.max(v) - np.min(v)),
    }

    # Slew rate (rising): dv/dt between 10% and 90% of vpp
    vpp = metrics["vpp"]
    if vpp > 0 and len(time) > 1:
        v_lo = metrics["vmin"] + 0.1 * vpp
        v_hi = metrics["vmin"] + 0.9 * vpp
        # Find first rising edge crossing 10% and 90%
        rising = False
        t_lo, t_hi = None, None
        for i in range(1, len(v)):
            if not rising and v[i] >= v_lo and v[i - 1] < v_lo:
                t_lo = float(time[i])
                rising = True
            if rising and v[i] >= v_hi:
                t_hi = float(time[i])
                break
        if t_lo is not None and t_hi is not None and t_hi > t_lo:
            dt = t_hi - t_lo
            metrics["slew_rate"] = 0.8 * vpp / dt

    return metrics


def parse_results(
    result: SimulationResult,
    pins: list[PinInfo],
    dut_instance: str = "DUT",
) -> dict:
    """Extract measurements from Spectre results, organized by pin.

    Maps PSF signal names (e.g., "DUT.D0") back to pin names.
    Returns a dict with per-pin measurements and summary.
    """
    if not result.ok or not result.data:
        return {"status": "error", "errors": result.errors}

    data = result.data
    pin_measurements = {}

    for pin in pins:
        pad_type = classify_pin_heuristic(pin)
        # Skip ground — no meaningful measurement
        if pad_type == "ground":
            continue

        # Try multiple PSF signal naming patterns
        candidates = [
            f"{dut_instance}.{pin.name}",
            pin.name,
            f"/{dut_instance}/{pin.name}",
            f"{dut_instance}/{pin.name}",
        ]
        signal_key = None
        for c in candidates:
            if c in data:
                signal_key = c
                break

        if signal_key is None:
            # Fuzzy match: find any key ending with this pin name
            for key in data:
                if key.endswith(f".{pin.name}") or key.endswith(f"/{pin.name}"):
                    signal_key = key
                    break

        if signal_key and signal_key in data:
            metrics = _measure_tran(data, signal_key)
            metrics["pad_type"] = pad_type
            metrics["psf_key"] = signal_key
            pin_measurements[pin.name] = metrics
        else:
            pin_measurements[pin.name] = {
                "pad_type": pad_type,
                "error": "signal not found in PSF data",
            }

    return {
        "status": "ok",
        "num_pins_measured": sum(
            1 for m in pin_measurements.values() if "error" not in m
        ),
        "num_pins_total": len(pins),
        "pins": pin_measurements,
    }


# ── Orchestrator ───────────────────────────────────────────────

@dataclass
class SimRunResult:
    lib: str
    tb_cell: str
    netlist_path: Optional[str]
    deck_path: Optional[str]
    spectre_ok: bool
    measurements: dict
    run_dir: Optional[str] = None

    def save(self, run_dir: Path) -> None:
        data = asdict(self)
        data["run_dir"] = str(run_dir)
        (run_dir / "sim_run_result.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def run_sim_run(
    lib: str,
    tb_cell: str,
    pins: list[PinInfo],
    run_dir: Path,
    *,
    config: Optional[SimConfig] = None,
    client: Optional[VirtuosoClient] = None,
    cds_lib: str = "",
    spectre_mode: str = "spectre",
    spectre_timeout: int = 600,
) -> SimRunResult:
    """Run the full simulation pipeline (Steps 3a-3d).

    Parameters
    ----------
    lib, tb_cell : Library and testbench cell names
    pins : Pin info list from Step 4b (for result mapping)
    run_dir : Output directory (typically from sim_flow)
    config : SimConfig (default: model_include from env)
    client : VirtuosoClient (default: from env)
    cds_lib : Path to cds.lib on remote (empty = auto-detect)
    spectre_mode : Spectre execution mode
    spectre_timeout : Timeout in seconds
    """
    if client is None:
        client = VirtuosoClient.from_env()
    if config is None:
        config = SimConfig(
            model_include=os.getenv("VB_PDK_SPECTRE_INCLUDE", ""),
        )

    print(f"\n{'='*60}")
    print(f" Sim Run: {lib}/{tb_cell}")
    print(f" Config:  analysis={config.analysis} stop={config.stop}")
    print(f" Model:   {config.model_include or '(none)'}")
    print(f"{'='*60}\n")

    # Step 3a: Export netlist
    netlist_path = export_netlist(client, lib, tb_cell, run_dir, cds_lib=cds_lib)

    # Step 3b: Build deck
    deck_path = None
    if netlist_path is not None:
        deck_path = build_deck(netlist_path, config, run_dir)

    # Step 3c: Run spectre
    spectre_ok = False
    sim_result = None
    if deck_path is not None:
        sim_result = run_spectre(
            deck_path, run_dir,
            mode=spectre_mode,
            timeout=spectre_timeout,
        )
        spectre_ok = sim_result.ok

    # Step 3d: Parse results
    measurements = {}
    if sim_result is not None and sim_result.ok:
        measurements = parse_results(sim_result, pins)
        # Save measurements
        (run_dir / "measurements.json").write_text(
            json.dumps(measurements, indent=2, default=str), encoding="utf-8"
        )

    result = SimRunResult(
        lib=lib,
        tb_cell=tb_cell,
        netlist_path=str(netlist_path) if netlist_path else None,
        deck_path=str(deck_path) if deck_path else None,
        spectre_ok=spectre_ok,
        measurements=measurements,
    )
    result.save(run_dir)

    print(f"\n{'='*60}")
    print(f" Sim Run Summary")
    print(f"{'='*60}")
    print(f"  Netlist:  {'OK' if netlist_path else 'FAILED'}")
    print(f"  Deck:     {'OK' if deck_path else 'FAILED'}")
    print(f"  Spectre:  {'OK' if spectre_ok else 'FAILED'}")
    if measurements.get("pins"):
        ok_pins = sum(1 for m in measurements["pins"].values() if "error" not in m)
        print(f"  Measured: {ok_pins}/{measurements['num_pins_total']} pins")
    print(f"{'='*60}\n")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: python sim_run.py <lib> <tb_cell> [model_include]")
        print(f"Example: python sim_run.py LLM_Layout_Design_Lab IO_RING_12x12_tb")
        sys.exit(1)

    _lib = sys.argv[1]
    _tb_cell = sys.argv[2]
    _model = sys.argv[3] if len(sys.argv) > 3 else os.getenv("VB_PDK_SPECTRE_INCLUDE", "")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _run_dir = _OUTPUT_ROOT / ts
    _run_dir.mkdir(parents=True, exist_ok=True)

    _cfg = SimConfig(model_include=_model)
    run_sim_run(_lib, _tb_cell, [], _run_dir, config=_cfg)
