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
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_PKG_DIR = Path(__file__).resolve().parent
_SIM_IO = _PKG_DIR.parent.parent
_BRIDGE_LITE = _SIM_IO.parent / "virtuoso-bridge-lite" / "src"
for p in (_BRIDGE_LITE,):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.models import ExecutionStatus, SimulationResult
from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

from sim_io.sim.deck import SimConfig, build_sim_deck_from_file
from sim_io.sim.config import (
    SimDeckConfig, summarize_netlist, write_sim_config_input,
    resolve_sim_config, SPECTRE_BIN, SPECTRE_LICENSE,
)
from sim_io.site_config import SiteConfig
from sim_io.pin_types import PinInfo, classify_pin_heuristic

_OUTPUT_ROOT = _SIM_IO / "output"

# Remote directory for si batch netlist export
_SI_REMOTE_DIR = "/tmp/sim_io_si_run"

# si.env template path
_SI_ENV_TEMPLATE = _SIM_IO / "templates" / "si_spectre.env"


# ── Template Helpers ────────────────────────────────────────────

def _load_si_env_template() -> str:
    """Load the si.env template from SIM-IO/templates/si_spectre.env."""
    if not _SI_ENV_TEMPLATE.is_file():
        raise FileNotFoundError(f"si.env template not found: {_SI_ENV_TEMPLATE}")
    return _SI_ENV_TEMPLATE.read_text(encoding="utf-8")


def _substitute_si_env(template: str, *, library: str, top_cell: str, run_dir: str) -> str:
    """Replace @PLACEHOLDER@ patterns in the si.env template."""
    return (
        template
        .replace("@LIBRARY@", library)
        .replace("@TOP_CELL@", top_cell)
        .replace("@SI_RUN_DIR@", run_dir)
    )


# ── License Discovery (fallback) ───────────────────────────────

def _discover_license_from_virtuoso(client: VirtuosoClient) -> dict[str, str]:
    """Discover license env vars from the running Virtuoso session.

    Only used as a fallback when SiteConfig doesn't have them set.
    """
    env = {}
    for var in ("LM_LICENSE_FILE", "CDS_LIC_FILE"):
        r = client.execute_skill(f'getShellEnvVar("{var}")', timeout=10)
        val = (r.output or "").strip('"')
        if val and val.lower() != "nil":
            env[var] = val
    return env


# ── Step 3a: Netlist Export ────────────────────────────────────

def export_netlist(
    client: VirtuosoClient,
    lib: str,
    tb_cell: str,
    run_dir: Path,
    *,
    site: SiteConfig,
) -> Optional[Path]:
    """Export Spectre netlist from _tb schematic via si batch netlister.

    Uses SiteConfig for cds.lib, IC root, and license vars.
    License vars fall back to SKILL auto-discovery from Virtuoso.

    Steps:
      0. schCheck + dbSave on the _tb schematic (si requires it)
      1. Create remote run directory
      2. Write si.env from template with placeholder substitution
      3. Resolve license vars (SiteConfig > SKILL discovery)
      4. Run si -batch -command nl with user's cds.lib
      5. Download netlist to local run_dir

    Returns the path to the downloaded netlist file, or None on failure.
    """
    print(f"[step3a] Exporting netlist for {lib}/{tb_cell}")

    # 0. schCheck + dbSave — si refuses to netlist if cellview is modified
    r = client.execute_skill(
        f'let((cv) cv = dbOpenCellViewByType("{lib}" "{tb_cell}" "schematic" "schematic" "a") '
        f'schCheck(cv) dbSave(cv) dbClose(cv) t)'
    )
    if r.errors:
        print(f"[step3a] WARNING: schCheck/save failed: {r.errors}")

    # 1. Create remote run directory
    r = client.execute_skill(f'sh("mkdir -p {_SI_REMOTE_DIR}")')

    # 2. Write si.env from template
    template = _load_si_env_template()
    si_env_content = _substitute_si_env(
        template,
        library=lib,
        top_cell=tb_cell,
        run_dir=_SI_REMOTE_DIR,
    )

    # simInitEnvWithArgs primes the directory; then we overwrite with our template
    r = client.execute_skill(
        f'simInitEnvWithArgs("{_SI_REMOTE_DIR}" "{lib}" "{tb_cell}" '
        f'"schematic" "spectre" nil)'
    )
    if r.errors:
        print(f"[step3a] WARNING: simInitEnvWithArgs had errors: {r.errors}")

    tunnel = client._tunnel
    if tunnel is not None:
        tunnel.upload_text(si_env_content, f"{_SI_REMOTE_DIR}/si.env")
    else:
        client.execute_skill(
            f'csh("echo \'{si_env_content}\' > {_SI_REMOTE_DIR}/si.env")'
        )

    # 3. Resolve license vars (SiteConfig > SKILL discovery)
    license_env: dict[str, str] = {}
    if site.lm_license_file:
        license_env["LM_LICENSE_FILE"] = site.lm_license_file
    if site.cds_lic_file:
        license_env["CDS_LIC_FILE"] = site.cds_lic_file

    # Fallback: discover missing license vars from Virtuoso
    missing = [v for v in ("LM_LICENSE_FILE", "CDS_LIC_FILE") if v not in license_env]
    if missing:
        discovered = _discover_license_from_virtuoso(client)
        for v in missing:
            if v in discovered:
                license_env[v] = discovered[v]

    print(f"[step3a] License: LM_LICENSE_FILE={license_env.get('LM_LICENSE_FILE', '(missing)')}, "
          f"CDS_LIC_FILE={license_env.get('CDS_LIC_FILE', '(missing)')}")

    # 4. Run si batch netlister via SSH shell
    ic_root = site.ic_root
    export_lines = [
        f"export PATH={ic_root}/tools/bin:{ic_root}/tools/dfII/bin:{ic_root}/tools/bin/64bit:$PATH",
        f"export LD_LIBRARY_PATH={ic_root}/tools/lib/64bit:{ic_root}/tools/dfII/lib/64bit:$LD_LIBRARY_PATH",
    ]
    for var, val in license_env.items():
        export_lines.append(f"export {var}={val}")

    env_setup = "; ".join(export_lines)
    si_cmd = (
        f'{env_setup}; '
        f'cd {_SI_REMOTE_DIR}; '
        f'{site.si_bin} -batch -cdslib {site.cds_lib} -command nl 2>&1 | tail -20'
    )

    if tunnel is not None:
        r = tunnel.run_command(si_cmd, timeout=300)
        output = r.stdout or ""
        if "ERROR" in output:
            print(f"[step3a] si output:\n{output}")
    else:
        r = client.run_shell_command(
            f'cd {_SI_REMOTE_DIR}; {site.si_bin} -batch -cdslib {site.cds_lib} -command nl',
            timeout=300,
        )
        if r.errors:
            print(f"[step3a] WARNING: si returned errors: {r.errors}")

    # 5. Download netlist
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
    config,
    run_dir: Path,
) -> Path:
    """Build a complete Spectre deck from si netlist + config.

    Accepts SimConfig (legacy) or SimDeckConfig (new).
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
    Sets LM_LICENSE_FILE from SPECTRE_LICENSE if not already in environment.
    """
    if not spectre_cmd:
        spectre_cmd = os.getenv("SPECTRE_CMD", SPECTRE_BIN)

    # Ensure license env var is set for spectre
    if SPECTRE_LICENSE and "LM_LICENSE_FILE" not in os.environ:
        os.environ["LM_LICENSE_FILE"] = SPECTRE_LICENSE
        print(f"[step3c] Set LM_LICENSE_FILE={SPECTRE_LICENSE}")

    print(f"[step3c] Running Spectre (mode={mode}, timeout={timeout}s)")
    print(f"[step3c] Command: {spectre_cmd}")

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


def _measure_dc(data: dict, signal: str, vdd: float = 1.8) -> dict:
    """Extract key metrics from a DC sweep signal.

    Finds the sweep variable (first non-signal key) and extracts
    voltage at nominal VDD, voltage range, and DC gain.
    """
    try:
        import numpy as np
    except ImportError:
        values = data.get(signal, [])
        if not values:
            return {"signal": signal, "error": "no data"}
        return {
            "signal": signal,
            "v_at_vdd": None,
            "vmin": min(values),
            "vmax": max(values),
            "vrange": max(values) - min(values),
        }

    v = np.array(data.get(signal, []), dtype=float)
    if len(v) == 0:
        return {"signal": signal, "error": "no data"}

    # Find sweep variable — first key that isn't the signal or "time"
    sweep_key = None
    for key in data:
        if key not in (signal, "time"):
            sweep_key = key
            break

    metrics: dict = {
        "signal": signal,
        "vmin": float(np.min(v)),
        "vmax": float(np.max(v)),
        "vrange": float(np.max(v) - np.min(v)),
    }

    if sweep_key:
        sweep = np.array(data.get(sweep_key, []), dtype=float)
        if len(sweep) == len(v) and vdd > 0:
            idx = np.argmin(np.abs(sweep - vdd))
            metrics["v_at_vdd"] = float(v[idx])
            # DC gain: dVout/dVdd near operating point
            if len(v) > 2:
                window = max(1, len(v) // 20)
                lo = max(0, idx - window)
                hi = min(len(v), idx + window + 1)
                dv = v[hi - 1] - v[lo]
                ds = sweep[hi - 1] - sweep[lo]
                if abs(ds) > 1e-12:
                    metrics["dc_gain"] = float(dv / ds)

    return metrics


def _measure_power(data: dict, v_signal: str, i_signal: str) -> dict:
    """Compute power from voltage and current waveforms.

    Returns average, peak, and static/dynamic power breakdown.
    Static power uses the DC operating point (first sample or average
    of the first 10% of the transient).
    """
    try:
        import numpy as np
    except ImportError:
        return {"error": "numpy required for power calculation"}

    v = np.array(data.get(v_signal, []), dtype=float)
    i = np.array(data.get(i_signal, []), dtype=float)
    if len(v) == 0 or len(i) == 0:
        return {"error": "missing voltage or current data"}

    n = min(len(v), len(i))
    v, i = v[:n], i[:n]
    p = v * i

    # Static power: average of first 10% (before switching activity)
    n_static = max(1, n // 10)
    p_static = float(np.mean(np.abs(p[:n_static])))

    p_avg = float(np.mean(np.abs(p)))
    p_max = float(np.max(np.abs(p)))

    return {
        "pavg": p_avg,
        "pmax": p_max,
        "pstatic": p_static,
        "pdynamic": p_avg - p_static if p_avg > p_static else 0.0,
    }


def _find_signal(data: dict, name: str, dut_instance: str = "DUT") -> str | None:
    """Find a signal in PSF data by trying multiple naming conventions."""
    candidates = [
        f"{dut_instance}.{name}",
        name,
        f"/{dut_instance}/{name}",
        f"{dut_instance}/{name}",
    ]
    for c in candidates:
        if c in data:
            return c
    # Fuzzy match
    for key in data:
        if key.endswith(f".{name}") or key.endswith(f"/{name}"):
            return key
    return None


def _detect_analysis_type(data: dict) -> str:
    """Detect the primary analysis type from PSF data keys."""
    if "time" in data:
        return "tran"
    # DC sweep: has a sweep variable but no "time"
    keys = [k for k in data if k != "time"]
    if len(keys) >= 2:
        return "dc"
    return "unknown"


def parse_results(
    result: SimulationResult,
    pins: list[PinInfo],
    dut_instance: str = "DUT",
    vdd_value: float = 1.8,
) -> dict:
    """Extract measurements from Spectre results, organized by pin.

    Detects analysis type (DC/tran) and extracts appropriate metrics.
    For power pins, also computes current and power measurements.
    """
    if not result.ok or not result.data:
        return {"status": "error", "errors": result.errors}

    data = result.data
    analysis = _detect_analysis_type(data)
    pin_measurements = {}

    for pin in pins:
        pad_type = classify_pin_heuristic(pin)
        if pad_type == "ground":
            continue

        signal_key = _find_signal(data, pin.name, dut_instance)

        if signal_key and signal_key in data:
            if analysis == "tran":
                metrics = _measure_tran(data, signal_key)
            elif analysis == "dc":
                metrics = _measure_dc(data, signal_key, vdd=vdd_value)
            else:
                metrics = _measure_tran(data, signal_key)

            metrics["pad_type"] = pad_type
            metrics["psf_key"] = signal_key

            # Power pin: add current + power measurements
            if pad_type == "power":
                src_name = f"SRC_{pin.name}"
                # Spectre PSF uses various formats for branch currents
                i_candidates = [
                    f"{src_name}:p",        # Spectre short form (PLUS)
                    f"{src_name}:PLUS",     # Full terminal name
                    src_name,               # Total source current
                    f"{src_name}.p",        # Dot notation variant
                ]
                i_key = None
                for cand in i_candidates:
                    i_key = _find_signal(data, cand, dut_instance)
                    if i_key is not None:
                        break

                if i_key and i_key in data:
                    if analysis == "tran":
                        i_vals = data.get(i_key, [])
                        try:
                            import numpy as np
                            i_arr = np.array(i_vals, dtype=float)
                            if len(i_arr) > 0:
                                metrics["iavg"] = float(np.mean(np.abs(i_arr)))
                                metrics["imax"] = float(np.max(np.abs(i_arr)))
                        except (ImportError, ValueError):
                            pass

                    power = _measure_power(data, signal_key, i_key)
                    if "error" not in power:
                        metrics.update(power)

            pin_measurements[pin.name] = metrics
        else:
            pin_measurements[pin.name] = {
                "pad_type": pad_type,
                "error": "signal not found in PSF data",
            }

    return {
        "status": "ok",
        "analysis": analysis,
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
    plot_paths: list[str] = field(default_factory=list)
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
    config=None,
    deck_config: Optional[SimDeckConfig] = None,
    site: Optional[SiteConfig] = None,
    client: Optional[VirtuosoClient] = None,
    spectre_mode: str = "spectre",
    spectre_timeout: int = 600,
    user_intent: str = "",
    vdd_value: float = 1.8,
) -> SimRunResult:
    """Run the full simulation pipeline (Steps 3a-3d).

    Parameters
    ----------
    lib, tb_cell : Library and testbench cell names
    pins : Pin info list from Step 4b (for result mapping)
    run_dir : Output directory (typically from sim_flow)
    config : SimConfig (legacy, used if deck_config is None)
    deck_config : SimDeckConfig (new, takes priority over config)
    site : SiteConfig (default: loaded from SIM-IO/.env)
    client : VirtuosoClient (default: from env)
    spectre_mode : Spectre execution mode
    spectre_timeout : Timeout in seconds
    user_intent : Free-text simulation intent for LLM config generation
    vdd_value : Supply voltage for default config
    """
    if client is None:
        client = VirtuosoClient.from_env()
    if site is None:
        site = SiteConfig.from_env()
    if config is None:
        config = SimConfig(
            model_include=site.pdk_spectre_include,
        )

    print(f"\n{'='*60}")
    print(f" Sim Run: {lib}/{tb_cell}")
    print(f" Cds.lib: {site.cds_lib}")
    print(f" IC root: {site.ic_root}")
    print(f"{'='*60}\n")

    # Step 3a: Export netlist
    netlist_path = export_netlist(client, lib, tb_cell, run_dir, site=site)

    # Step 3a.5: Write LLM input and resolve sim config
    resolved_config = deck_config
    if netlist_path is not None and resolved_config is None:
        # Write sim_config_input.json for LLM
        try:
            netlist_text = netlist_path.read_text(encoding="utf-8")
            netlist_summary = summarize_netlist(netlist_text)
            pin_classes = None
            pin_class_path = run_dir / "pin_classifications.json"
            if not pin_class_path.exists():
                pin_class_path = _SIM_IO / "pin_classifications.json"
            if pin_class_path.exists():
                import json as _json
                pin_classes = _json.loads(pin_class_path.read_text(encoding="utf-8")).get("pins", [])
            write_sim_config_input(
                netlist_summary=netlist_summary,
                pin_classifications=pin_classes,
                user_intent=user_intent,
                lib=lib, cell=tb_cell, vdd_value=vdd_value,
                path=run_dir / "sim_config_input.json",
            )
            print(f"[step3a.5] Wrote sim_config_input.json")
        except Exception as e:
            print(f"[step3a.5] WARNING: Failed to write sim config input: {e}")

        # Resolve: LLM > active.state > legacy > site default
        resolved_config = resolve_sim_config(
            run_dir=run_dir,
            lib=lib, cell=tb_cell,
            vdd_value=vdd_value,
            user_intent=user_intent,
            legacy_config=config,
        )

    if resolved_config is None:
        resolved_config = config  # fallback to legacy SimConfig

    # Step 3b: Build deck
    deck_path = None
    if netlist_path is not None:
        deck_path = build_deck(netlist_path, resolved_config, run_dir)

    # Step 3c: Run spectre
    spectre_ok = False
    sim_result = None
    if deck_path is not None:
        spectre_cmd = os.getenv("SPECTRE_CMD", SPECTRE_BIN)
        sim_result = run_spectre(
            deck_path, run_dir,
            spectre_cmd=spectre_cmd,
            mode=spectre_mode,
            timeout=spectre_timeout,
        )
        spectre_ok = sim_result.ok

    # Step 3d: Parse results + visualize
    measurements = {}
    plot_paths = []
    if sim_result is not None and sim_result.ok:
        measurements = parse_results(sim_result, pins, vdd_value=vdd_value)
        # Save measurements
        (run_dir / "measurements.json").write_text(
            json.dumps(measurements, indent=2, default=str), encoding="utf-8"
        )

        # Step 3e: Generate SVG plots from PSF results
        try:
            from sim_viz import visualize_run, parse_psf_ascii, extract_dc_metrics, extract_ac_metrics

            psf_dir = run_dir / f"{deck_path.stem}.raw"
            if not psf_dir.exists():
                # Try nested .raw/.raw
                nested = psf_dir / psf_dir.name
                if nested.exists():
                    psf_dir = nested

            if psf_dir.exists():
                plots_dir = run_dir / "plots"
                plot_paths = visualize_run(str(psf_dir), str(plots_dir))

                # Extract key metrics from viz parser
                viz_metrics = {}
                for psf_file in psf_dir.glob("*.dc"):
                    try:
                        dc = parse_psf_ascii(str(psf_file))
                        viz_metrics["dc"] = extract_dc_metrics(dc)
                    except Exception:
                        pass
                    break
                for psf_file in psf_dir.glob("*.ac"):
                    try:
                        ac = parse_psf_ascii(str(psf_file))
                        viz_metrics["ac"] = extract_ac_metrics(ac)
                    except Exception:
                        pass
                    break
                if viz_metrics:
                    (run_dir / "viz_metrics.json").write_text(
                        json.dumps(viz_metrics, indent=2), encoding="utf-8"
                    )
                    print(f"[step3e] Metrics: {viz_metrics}")
        except Exception as e:
            print(f"[step3e] Visualization skipped: {e}")

    result = SimRunResult(
        lib=lib,
        tb_cell=tb_cell,
        netlist_path=str(netlist_path) if netlist_path else None,
        deck_path=str(deck_path) if deck_path else None,
        spectre_ok=spectre_ok,
        measurements=measurements,
        plot_paths=[str(p) for p in plot_paths],
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
    if plot_paths:
        print(f"  Plots:    {len(plot_paths)} SVG files in {run_dir / 'plots'}")
    print(f"{'='*60}\n")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: python sim_run.py <lib> <tb_cell> [model_include]")
        print(f"Example: python sim_run.py LLM_Layout_Design_Lab IO_RING_12x12_tb")
        sys.exit(1)

    _lib = sys.argv[1]
    _tb_cell = sys.argv[2]
    _model = sys.argv[3] if len(sys.argv) > 3 else ""

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _run_dir = _OUTPUT_ROOT / ts
    _run_dir.mkdir(parents=True, exist_ok=True)

    _site = SiteConfig.from_env()
    _cfg = SimConfig(model_include=_model or _site.pdk_spectre_include)
    run_sim_run(_lib, _tb_cell, [], _run_dir, config=_cfg, site=_site)
