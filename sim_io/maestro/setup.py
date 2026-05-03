"""Maestro setup builder — SimDeckConfig → Maestro API calls.

Converts SIM-IO's SimDeckConfig into a sequence of Maestro SKILL calls
that create a fully configured simulation setup.  Uses background-mode
sessions (maeOpenSetup) for configuration — no GUI window required.

Typical usage::

    session = build_maestro_setup(client, lib, tb_cell, config)
    # ... run simulation ...
    close_session(client, session)

Or let build_maestro_setup manage the session lifecycle itself
(pass auto_close=True) and re-open for simulation later.
"""

from __future__ import annotations

import sys
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
    create_test,
    set_analysis,
    add_output,
    set_spec,
    set_var,
    set_env_option,
    set_sim_option,
    save_setup,
    set_current_run_mode,
)

from sim_io.sim.config import (
    SimDeckConfig,
    ModelInclude,
    AnalysisSpec,
    SweepSpec,
    DesignVar,
    SimOptions,
    OutputExpression,
    SaveSignal,
)
from sim_io.pin_types import PinInfo, classify_pin_heuristic


# ── Ensure Maestro View ────────────────────────────────────────

def ensure_maestro_view(client: VirtuosoClient, lib: str, cell: str) -> str:
    """Bootstrap the ``maestro`` cellview if it doesn't exist on disk.

    Freshly created testbench cells have no ``maestro`` view.  Calling
    ``deOpenCellView`` on a missing view pops a blocking dialog, but
    ``maeOpenSetup`` creates it in memory.  We open a background session,
    save to disk — idempotent if the view already exists.

    Returns the session string so callers can reuse it instead of
    opening a second session (avoids ASSEMBLER-8127 lock conflicts).

    Must be called BEFORE ``open_gui_session``; not needed before
    ``open_session`` (background mode handles missing views).
    """
    # maeOpenSetup creates the session regardless — attaches to
    # existing view or creates a new one in memory.
    r = client.execute_skill(
        f'maeOpenSetup("{lib}" "{cell}" "maestro")', timeout=60
    )
    if r.errors or not r.output or r.output.strip() in ("nil", ""):
        raise RuntimeError(
            f"maeOpenSetup failed for {lib}/{cell}: {r.errors}"
        )
    session = r.output.strip().strip('"')

    # Flush to disk — without this the view doesn't persist for
    # future open_gui_session calls.
    r = client.execute_skill(
        f'maeSaveSetup(?session "{session}")', timeout=30
    )
    if r.errors:
        # Don't fail — the save may fail if the view already exists
        # and is unchanged.  The session is still valid.
        print(f"[maestro] WARNING: maeSaveSetup in ensure_maestro_view: {r.errors}")

    return session


# ── Analysis Options Builder ───────────────────────────────────

def _build_tran_options(stop: str, errpreset: str = "",
                        extra: dict[str, str] | None = None) -> str:
    """Build SKILL alist string for tran analysis options."""
    parts = [f'("stop" "{stop}")']
    if errpreset:
        parts.append(f'("errpreset" "{errpreset}")')
    if extra:
        for k, v in extra.items():
            parts.append(f'("{k}" "{v}")')
    return "(" + " ".join(parts) + ")"


def _build_dc_options(sweep: Optional[SweepSpec] = None,
                      extra: dict[str, str] | None = None) -> str:
    """Build SKILL alist string for dc analysis options."""
    parts: list[str] = []
    if sweep and sweep.param:
        parts.append(f'("param" "{sweep.param}")')
    if sweep and sweep.start:
        parts.append(f'("start" "{sweep.start}")')
    if sweep and sweep.stop:
        parts.append(f'("stop" "{sweep.stop}")')
    if sweep and sweep.lin:
        parts.append(f'("lin" "{sweep.lin}")')
    if sweep and sweep.dec:
        parts.append(f'("dec" "{sweep.dec}")')
    if extra:
        for k, v in extra.items():
            parts.append(f'("{k}" "{v}")')
    return "(" + " ".join(parts) + ")" if parts else ""


def _build_ac_options(sweep: Optional[SweepSpec] = None,
                      extra: dict[str, str] | None = None) -> str:
    """Build SKILL alist string for ac analysis options.

    AC sweeps require incrType and stepTypeLog to specify logarithmic
    sweep correctly — these are not in SimDeckConfig and must be inferred.
    """
    parts: list[str] = []
    if sweep:
        if sweep.start:
            parts.append(f'("start" "{sweep.start}")')
        if sweep.stop:
            parts.append(f'("stop" "{sweep.stop}")')
        if sweep.dec:
            parts.append(f'("incrType" "Logarithmic")')
            parts.append(f'("stepTypeLog" "Points Per Decade")')
            parts.append(f'("dec" "{sweep.dec}")')
        elif sweep.lin:
            parts.append(f'("incrType" "Linear")')
            parts.append(f'("lin" "{sweep.lin}")')
    if extra:
        for k, v in extra.items():
            parts.append(f'("{k}" "{v}")')
    return "(" + " ".join(parts) + ")" if parts else ""


def _build_analysis_options(a: AnalysisSpec) -> str:
    """Convert an AnalysisSpec into a Maestro options alist string."""
    if a.name == "tran":
        return _build_tran_options(
            stop=a.stop or "10u",
            errpreset=a.errpreset,
            extra=a.extra_options,
        )
    elif a.name == "dc":
        return _build_dc_options(sweep=a.sweep, extra=a.extra_options)
    elif a.name == "ac":
        return _build_ac_options(sweep=a.sweep, extra=a.extra_options)
    else:
        # Generic: dump extra_options as alist
        if a.extra_options:
            parts = [f'("{k}" "{v}")' for k, v in a.extra_options.items()]
            return "(" + " ".join(parts) + ")"
        return ""


# ── Simulator Options Alist ────────────────────────────────────

def _build_sim_option_alist(opts: SimOptions) -> str:
    """Convert SimOptions to Maestro simulator options alist string.

    All values MUST be strings — integer/float values silently fail.
    """
    parts = [
        f'("temp" "{opts.temp}")',
        f'("reltol" "{opts.reltol}")',
        f'("vabstol" "{opts.vabstol}")',
        f'("iabstol" "{opts.iabstol}")',
        f'("gmin" "{opts.gmin}")',
        f'("tnom" "{opts.tnom}")',
        f'("pivrel" "{opts.pivrel}")',
    ]
    for k, v in opts.extra.items():
        parts.append(f'("{k}" "{v}")')
    return "(" + " ".join(parts) + ")"


# ── OCEAN Expression Escaping ──────────────────────────────────

def _escape_ocean_expr(expr: str) -> str:
    """Escape an OCEAN expression for embedding in a SKILL string.

    Maestro add_output(?expr "...") sends the expression as a SKILL
    string.  OCEAN expressions like V("/VOUT") contain double quotes
    that must be backslash-escaped inside the SKILL string, otherwise
    SKILL sees unmatched quotes and silently ignores the expression.

    Example::
        V("/VOUT")          →  V(\\"/VOUT\\")
        dB20(mag(VF("/OUT"))) → dB20(mag(VF(\\"/OUT\\")))

    The 06a_rc_create example uses this pattern:
        expr=r'bandwidth(mag(VF(\\"/OUT\\")) 3 \\"low\\")'
    """
    return expr.replace('"', '\\"')


# ── Signal Path Convention ─────────────────────────────────────

def _to_maestro_signal_path(signal: str) -> str:
    """Convert a signal name to Maestro's net path convention.

    SIM-IO's testbench uses label-based wiring: each DUT pin gets a
    net label with the pin name (e.g., "VDD", "D0").  These are
    top-level nets in the testbench, so Maestro references them as
    "/VDD", "/D0" — NOT "/DUT/VDD" (which would be the instance
    terminal path used by Spectre save statements).

    Handles input formats:
        "/DUT/VOUT"  → "/VOUT"   (strip DUT hierarchy — top-level net)
        "DUT.VOUT"   → "/VOUT"
        "/VOUT"      → "/VOUT"   (already correct)
        "VOUT"       → "/VOUT"   (bare name → top-level)
    """
    sig = signal.strip()
    # Strip leading /
    if sig.startswith("/"):
        sig = sig[1:]
    # Handle DUT.VOUT or DUT/VOUT → VOUT
    if sig.startswith("DUT.") or sig.startswith("DUT/"):
        sig = sig[4:]  # strip "DUT." or "DUT/"
    # Ensure leading /
    if not sig.startswith("/"):
        sig = "/" + sig
    return sig


# ── Main Builder ───────────────────────────────────────────────

def build_maestro_setup(
    client: VirtuosoClient,
    lib: str,
    tb_cell: str,
    config: SimDeckConfig,
    *,
    pins: list[PinInfo] | None = None,
    test_name: str = "",
    auto_close: bool = True,
) -> str:
    """Build a complete Maestro test setup from SimDeckConfig.

    Opens a background session, creates a test, configures analyses,
    variables, model files, simulator options, outputs, and saves.
    Returns the session string (or empty string if auto_close=True).

    If ``save_signals`` and ``outputs`` in config are both empty,
    auto-generates Maestro outputs from the ``pins`` list so that
    ``read_results`` returns meaningful data.

    Parameters
    ----------
    client : VirtuosoClient
    lib, tb_cell : Library and testbench cell names
    config : SimDeckConfig — the simulation configuration
    pins : Pin info list from Step 4b (for auto-generating outputs)
    test_name : Maestro test name (default: tb_cell + "_test")
    auto_close : if True, close session after setup (re-open later for sim)
    """
    tname = test_name or f"{tb_cell}_test"

    print(f"\n{'='*60}")
    print(f" Maestro Setup: {lib}/{tb_cell}")
    print(f" Test: {tname}")
    print(f"{'='*60}\n")

    # Step 1: Ensure maestro view exists on disk + get session
    # Reuse the session from ensure_maestro_view to avoid opening
    # a second session (which can cause ASSEMBLER-8127 lock conflicts).
    session = ensure_maestro_view(client, lib, tb_cell)
    print(f"[maestro] Session: {session} (background, reused from ensure)")

    try:
        # Step 3: Create test — points to the testbench schematic
        create_test(client, tname, lib=lib, cell=tb_cell,
                    view="schematic", simulator="spectre", session=session)
        print(f"[maestro] Created test: {tname} → {lib}/{tb_cell}/schematic")

        # Step 4: Disable default analyses (Maestro creates tran by default)
        for default_a in ("tran", "dc", "ac"):
            set_analysis(client, tname, default_a, enable=False, session=session)

        # Step 5: Enable + configure requested analyses
        for a in config.analyses:
            if not a.enabled:
                continue
            options = _build_analysis_options(a)
            set_analysis(client, tname, a.name, enable=True,
                         options=options, session=session)
            sweep_info = ""
            if a.sweep and a.sweep.param:
                sweep_info = f" sweep={a.sweep.param}"
            print(f"[maestro] Analysis: {a.name}{sweep_info} "
                  f"stop={a.stop or '(sweep)'}")

        # Step 6: Design variables — CRITICAL: must set VDD for IO ring
        for v in config.design_vars:
            set_var(client, v.name, v.expression, session=session)
            print(f"[maestro] Variable: {v.name} = {v.expression}")

        # Step 7: Model files + save signals config
        # CRITICAL: simulation fails without model files.
        # Also set saveSignals to "allpub" (equivalent to Spectre's
        # "saveOptions options save=allpub") so raw waveforms are
        # available for any signal, not just declared outputs.
        env_parts: list[str] = []
        if config.model_includes:
            model_entries = []
            for mi in config.model_includes:
                section_str = f' "{mi.section}"' if mi.section else ' ""'
                model_entries.append(f'("{mi.path}"{section_str})')
            model_entries_str = " ".join(model_entries)
            env_parts.append(f'("modelFiles" ({model_entries_str}))')

        # Save config: allpub = save all public signals
        # This ensures raw PSF data is available for waveform export
        # even if the signal isn't declared as a Maestro output.
        save_val = config.save_default or "allpub"
        env_parts.append(f'("saveSignals" "{save_val}")')

        # switchViewList: hierarchy traversal order for the netlister.
        # TSMC28 IO pad cells have empty spectre views but may have
        # valid hspiceD views. Including hspiceD before spectre lets
        # the netlister use SPICE-format subcircuit definitions for
        # IO pads. Spectre can parse SPICE format via lang=spice.
        # Order: hspiceD (IO pad fallback) → spectre (standard) →
        # cmos_sch → schematic → veriloga
        env_parts.append(
            '("switchViewList" "hspiceD spectre cmos_sch schematic veriloga")'
        )

        env_alist = "(" + " ".join(env_parts) + ")"
        set_env_option(client, tname, env_alist, session=session)
        print(f"[maestro] Env options: {len(config.model_includes)} model includes, "
              f"save={save_val}")

        # Step 8: Simulator options
        sim_alist = _build_sim_option_alist(config.sim_options)
        set_sim_option(client, tname, sim_alist, session=session)
        print(f"[maestro] Sim options: temp={config.sim_options.temp}, "
              f"reltol={config.sim_options.reltol}")

        # Step 9: Add outputs
        # If save_signals and outputs are both empty, auto-generate from pins.
        # Maestro only computes what's declared as outputs — unlike Spectre's
        # "save allpub", Maestro needs explicit output declarations.
        has_explicit_outputs = bool(config.save_signals) or bool(config.outputs)

        if has_explicit_outputs:
            # Explicit outputs from SimDeckConfig
            for sig in config.save_signals:
                sig_path = _to_maestro_signal_path(sig.signal)
                out_name = sig_path.lstrip("/").replace("/", "_")
                add_output(client, out_name, tname,
                           output_type="net", signal_name=sig_path,
                           session=session)
                print(f"[maestro] Output (net): {out_name} → {sig_path}")

            for out in config.outputs:
                escaped_expr = _escape_ocean_expr(out.expression)
                add_output(client, out.name, tname,
                           output_type="point", expr=escaped_expr,
                           session=session)
                print(f"[maestro] Output (expr): {out.name}")
        else:
            # Auto-generate: add a net output for every non-ground pin
            auto_pins = pins or []
            n_added = 0
            for pin in auto_pins:
                pad_type = classify_pin_heuristic(pin)
                if pad_type in ("ground", "no_connect"):
                    continue
                sig_path = f"/{pin.name}"  # top-level net label
                add_output(client, pin.name, tname,
                           output_type="net", signal_name=sig_path,
                           session=session)
                n_added += 1
            print(f"[maestro] Auto-generated {n_added} outputs from pin list")

        # Step 11: Set run mode
        set_current_run_mode(
            client, "Single Run, Sweeps and Corners", session=session
        )

        # Step 12: Save to disk
        save_setup(client, lib, tb_cell, session=session)
        print(f"[maestro] Setup saved: {lib}/{tb_cell}/maestro")

        print(f"\n[maestro] Setup complete: {tname}")
        _print_setup_summary(config)

    except Exception:
        # Always try to save what we have before raising
        try:
            save_setup(client, lib, tb_cell, session=session)
        except Exception:
            pass
        if auto_close:
            try:
                close_session(client, session)
            except Exception:
                pass
        raise

    if auto_close:
        close_session(client, session)
        print(f"[maestro] Session closed (auto_close)")
        return ""

    return session


def teardown_maestro_setup(
    client: VirtuosoClient,
    session: str,
    lib: str,
    cell: str,
    *,
    save: bool = True,
) -> None:
    """Close a Maestro background session, optionally saving first."""
    if save:
        try:
            save_setup(client, lib, cell, session=session)
        except Exception:
            pass
    close_session(client, session)


# ── IO Pad Model Discovery ─────────────────────────────────────

def discover_io_model_file(client: VirtuosoClient, io_lib: str = "tphn28hpcpgv18") -> str:
    """Search the PDK for IO pad model include files.

    TSMC28 IO pad cells (tphn28hpcpgv18) need their subcircuit
    definitions included separately. This function searches common
    PDK paths for the IO model file.

    Returns the first found model file path, or empty string.
    """
    # Get the PDK root from the library's physical path
    r = client.execute_skill(f'ddGetObj("{io_lib}")~>readPath', timeout=15)
    lib_path = (r.output or "").strip().strip('"')
    if not lib_path or lib_path == "nil":
        print(f"[maestro] Cannot find library path for {io_lib}")
        return ""

    # The IO library path typically looks like:
    # /home/process/tsmc28n/PDK_mmWave/iPDK_.../tphn28hpcpgv18
    # We need to find the models directory relative to it.
    # Common patterns:
    #   {lib_path}/models/spectre/*.scs
    #   {lib_path}/../models/spectre/*.scs
    #   {lib_path}/../../models/spectre/io*.scs

    # Search candidate paths for spectre model files
    search_patterns = [
        f'fileSearch("{lib_path}/models/spectre" "*.scs")',
        f'fileSearch("{lib_path}/../models/spectre" "*.scs")',
        f'fileSearch("{lib_path}/../../models/spectre" "*io*.scs")',
        f'fileSearch("{lib_path}" "*.scs")',
    ]

    for pattern in search_patterns:
        try:
            r = client.execute_skill(pattern, timeout=15)
            output = (r.output or "").strip()
            if output and output != "nil":
                # Parse the file list and look for IO-related model files
                files = re.findall(r'"([^"]*\.scs)"', output)
                for f in files:
                    lower = f.lower()
                    if any(kw in lower for kw in ("io", "pad", "gpio", "iopad")):
                        print(f"[maestro] Found IO model file: {f}")
                        return f
                # Return first .scs file if no IO-specific match
                if files:
                    print(f"[maestro] Found candidate model file: {files[0]}")
                    return files[0]
        except Exception:
            continue

    print(f"[maestro] No IO model files found for {io_lib}")
    return ""


# ── Summary Printer ────────────────────────────────────────────

def _print_setup_summary(cfg: SimDeckConfig) -> None:
    print(f"\n{'='*60}")
    print(f" Maestro Setup Summary")
    print(f"{'='*60}")
    print(f"  Analyses:       {len([a for a in cfg.analyses if a.enabled])}")
    for a in cfg.analyses:
        if a.enabled:
            sw = f" sweep({a.sweep.param})" if a.sweep and a.sweep.param else ""
            print(f"    {a.name}: stop={a.stop or '?'}{sw}")
    print(f"  Design vars:    {len(cfg.design_vars)}")
    for v in cfg.design_vars:
        print(f"    {v.name} = {v.expression}")
    print(f"  Model includes: {len(cfg.model_includes)}")
    print(f"  Sim options:    temp={cfg.sim_options.temp}, "
          f"reltol={cfg.sim_options.reltol}")
    print(f"  Save signals:   {len(cfg.save_signals)}")
    print(f"  Output exprs:   {len(cfg.outputs)}")
    print(f"  Source:         {cfg.source or 'unknown'}")
    print(f"{'='*60}\n")
