"""Site configuration for SIM-IO — loads from .env and env vars.

Loading order (highest priority first):
  1. SIM_* environment variables (if already set in the shell)
  2. SIM-IO/.env file (via dotenv)
  3. SKILL auto-discovery for license vars (fallback, done in sim_run.py)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
_SIM_IO = _PKG_DIR.parent


def _load_sim_env() -> None:
    """Load SIM-IO/.env into os.environ (does not override existing vars)."""
    env_file = _SIM_IO / ".env"
    if env_file.is_file():
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)


@dataclass
class SiteConfig:
    """Site-specific configuration for netlist export and simulation.

    Populated from SIM_* env vars (shell > .env).  License vars fall back
    to SKILL auto-discovery in sim_run.py when left empty here.
    """

    cds_lib: str
    ic_root: str
    pdk_spectre_include: str
    lm_license_file: str = ""
    cds_lic_file: str = ""

    @property
    def si_bin(self) -> str:
        return f"{self.ic_root}/tools/dfII/bin/si"

    @classmethod
    def from_env(cls) -> "SiteConfig":
        """Create SiteConfig from SIM_* env vars (.env loaded automatically)."""
        _load_sim_env()

        cds_lib = os.getenv("SIM_CDS_LIB", "")
        ic_root = os.getenv("SIM_IC_ROOT", "")

        if not cds_lib:
            raise ValueError(
                "SIM_CDS_LIB not set. Add it to SIM-IO/.env or set the env var."
            )
        if not ic_root:
            raise ValueError(
                "SIM_IC_ROOT not set. Add it to SIM-IO/.env or set the env var."
            )

        return cls(
            cds_lib=cds_lib,
            ic_root=ic_root,
            pdk_spectre_include=os.getenv("SIM_PDK_SPECTRE_INCLUDE", ""),
            lm_license_file=os.getenv("SIM_LM_LICENSE_FILE", ""),
            cds_lic_file=os.getenv("SIM_CDS_LIC_FILE", ""),
        )
