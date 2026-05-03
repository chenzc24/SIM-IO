"""Maestro (ADE Assembler) integration for SIM-IO.

Builds a fully configured Maestro test from SimDeckConfig, runs
simulation, and reads results — all in background mode (no GUI window).

Public API:
    ensure_maestro_view  — bootstrap maestro cellview if it doesn't exist
    build_maestro_setup  — SimDeckConfig → Maestro test configuration
    run_maestro_sim      — run simulation + read results
"""

from sim_io.maestro.setup import (
    ensure_maestro_view,
    build_maestro_setup,
    teardown_maestro_setup,
    discover_io_model_file,
)
from sim_io.maestro.run import (
    run_maestro_sim,
    MaestroSimResult,
)

__all__ = [
    "ensure_maestro_view",
    "build_maestro_setup",
    "teardown_maestro_setup",
    "discover_io_model_file",
    "run_maestro_sim",
    "MaestroSimResult",
]
