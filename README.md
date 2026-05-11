# SIM-IO

> **AI Agent:** Skip to [Agent Setup Guide](#agent-setup-guide) below for
> executable installation steps with concrete commands and ask/write tables.

A Claude Code skill for automated simulation testbench generation for IO Ring /
mixed-signal designs in Cadence Virtuoso - from symbol export to pin classification,
testbench construction, source/load placement, direct Spectre simulation, and
Maestro setup sync.

---

<!--=======================================================================-->
<!-- PART 1 - HUMAN GUIDE                                                  -->
<!-- Quick orientation, prerequisites, config reference, usage              -->
<!--=======================================================================-->

## Overview

`SIM-IO` depends on **virtuoso-bridge-lite** for all Virtuoso
communication (TCP + SSH). The project layout after setup:

```
<project-root>/
|-- .venv/                          -> one shared Python env (bridge + all skills)
|-- virtuoso-bridge-lite/           -> bridge source
`-- .claude/skills/
    `-- SIM-IO/
        |-- .env                    -> SIM-IO skill config (CDS path, PDK models, license)
        |-- sim_io/                 -> core Python package
        |-- scripts/                -> CLI entry points
        |-- skill_code/             -> SKILL scripts for Virtuoso operations
        `-- references/             -> classification rules and sim config rules
```

### How the system works

```
Claude Code (your machine)
       |       | 1. symbol_export: export symbol + redistribute pins
       | 2. LLM classifies pins (deliberate pause)
       | 3. testbench_build: build TB, place sources/loads
       | 4. Run Spectre directly and sync Maestro
       |       >virtuoso-bridge-lite
       |       |-- TCP socket -------------->Virtuoso daemon (EDA server)
       |                             loads .il, returns results
       |       `-- SSH tunnel -------------->EDA server
              |              |-- uploads .il files -> Virtuoso load()
              |-- runs Spectre directly
              `-- downloads measurements / plots
```

**Workflow pipeline with an LLM pause between symbol_export and testbench_build:**

| Phase | What happens | Entry point |
|---|---|---|
| symbol_export | Symbol export, pin redistribution, pin info extraction | `scripts/symbol_export.py` |
| pin_intent_authoring | Read `pin_info.json` + rules -> write `pin_classifications.json` + `sim_config.json` | You (the LLM) |
| testbench_build | Create TB, place DUT, wire labels, place sources/loads, Maestro setup | `scripts/tb_builder.py` |
| Sim | Direct Spectre simulation, results parsing, plots, Maestro setup sync | `scripts/spectre_runner.py` |

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.9+ | Local machine |
| Git | For cloning repos |
| Cadence Virtuoso (IC618+) | On the EDA server - required for SKILL execution, symbol export, schematic editing |
| Spectre (MMSIM 21+) | On the EDA server - required for simulation |
| Cadence Maestro | On the EDA server - required for GUI test setup sync |
| TSMC 28nm PDK | On the EDA server - IO pad models, core models, cds.lib |

---

## Quick Setup (Human)

**1. Clone and install:**
```bash
# At your project root:
git clone https://github.com/chenzc24/virtuoso-bridge-lite.git
mkdir -p .claude/skills
git clone https://github.com/chenzc24/SIM-IO.git .claude/skills/SIM-IO
```
```bash
# Create venv and install (Linux/Mac/Git Bash):
python -m venv .venv && source .venv/bin/activate
# Windows PowerShell:  python -m venv .venv; .venv\Scripts\Activate.ps1

pip install -e virtuoso-bridge-lite
```

> SIM-IO has no additional `requirements.txt` - it uses only `virtuoso-bridge-lite`
> and Python standard library.

**2. Configure bridge connection:**

The bridge `.env` is created by `virtuoso-bridge init`. See
[`virtuoso-bridge-lite/README.md`](https://github.com/chenzc24/virtuoso-bridge-lite#quick-start)
for full details (jump hosts, multi-profile, local mode).

> Make sure the `.venv` is activated before running any `virtuoso-bridge` commands.

```bash
virtuoso-bridge init <username>@<eda-server>    # creates ~/.virtuoso-bridge/.env
# With jump host:
# virtuoso-bridge init <username>@<eda-server> -J <username>@<jump-host>
```

**3. Configure SIM-IO skill `.env`:**

Edit `.claude/skills/SIM-IO/.env` - the fields marked `# -> CHANGE`:

| Variable | Required | What to set |
|---|---|---|
| `SIM_CDS_LIB` | Yes | Remote Linux path to your `cds.lib` |
| `SIM_IC_ROOT` | Yes | Remote path to Cadence IC installation root |
| `SIM_MMSIM_ROOT` | Optional | Remote path to Spectre/MMSIM root (auto-discovered if unset) |
| `SIM_LM_LICENSE_FILE` | Optional | License server (auto-discovered from Virtuoso if unset) |
| `SIM_CDS_LIC_FILE` | Optional | License server (auto-discovered from Virtuoso if unset) |
| `SIM_PDK_IO_SPECTRE_INCLUDE` | Yes | Remote path to TSMC28 IO pad SPICE model file |
| `SIM_PDK_CORE_SPECTRE_INCLUDE` | Yes | Remote path to TSMC28 core Spectre model file |
| `SIM_PDK_CORE_SPECTRE_SECTIONS` | Optional | Comma-separated model sections (default: TT corner sections) |

**4. Start bridge and verify:**
```bash
virtuoso-bridge start
virtuoso-bridge status                  # tunnel OK, daemon OK
```
In Virtuoso CIW, load the daemon SKILL file once per session (path printed by `start`):
```skill
load("/tmp/virtuoso_bridge_<user>/virtuoso_bridge/virtuoso_setup.il")
```

**Auto-activate `.venv`:** Set VS Code to use `.venv` as the interpreter, or add
`echo 'source .venv/bin/activate' > .envrc && direnv allow` (Linux/Mac).
Claude Code finds `.venv` automatically - no manual activation needed for skill runs.

---

## Workflow

The skill runs a multi-phase pipeline with a deliberate LLM pause:

```
1.  symbol_export         -> symbol export from schematic
2.                        -> pin redistribution (left/right layout)
3.                        -> pin info extraction -> pin_info.json
4.  pin_intent_authoring  -> classify pins -> pin_classifications.json
5.                        -> configure simulation -> sim_config.json
6.  testbench_build       -> create TB cellview
7.                        -> place DUT instance
8.                        -> add wire labels (label-based wiring)
9.                        -> place sources/loads per classification
10.                       -> Maestro test setup
11. Sim                   -> run direct Spectre
12.                       -> parse measurements -> measurements.json
13.                       -> sync Maestro setup without running Maestro simulation
14.                       -> plot waveforms -> plots/*.svg
```

Output files land in `SIM-IO/output/<YYYYMMDD_HHMMSS>/`:
`pin_info.json`, `pin_classifications.json`, `sim_config.json`,
`dut_context.json`, `result.json`, `measurements.json`, `sim_run_result.json`,
and `plots/*.svg`.

---

## Pin Classification (pin_intent_authoring)

Between symbol_export and testbench_build, the LLM reads `pin_info.json` and produces two JSON
files. This is the core intelligence of the pipeline.

### `pin_classifications.json`

Each outer (left-side) pin is assigned a `device_class` that determines what
stimulus/load devices are placed:

| `device_class` | How to identify | Stimulus |
|---|---|---|
| `analog_power` | VDD* in analog domain | Outer `vdc`, inner `idc` |
| `analog_ground` | VSS*/GND* in analog domain | Outer `vdc=0` only |
| `analog_current` | IB*/IBUF* prefix (PDB3AC) | Outer `idc` (inverted), inner `vdc` |
| `dig_hv_power` | PVDD2POC (exact name) | Outer `vdc=1.8V` |
| `dig_hv_ground` | PVSS2DGZ (exact name) | Outer `vdc=0` |
| `dig_lv_power` | VIOL etc. (PVDD1DGZ) | Outer `vdc=0.9V` |
| `dig_lv_ground` | GIOL etc. (PVSS1DGZ) | Outer `vdc=0V` |
| `digital_io_input` | RST, SCK, SDI, SLP (direction=input) | Outer `vpulse`, inner `cap` |
| `digital_io_output` | D*, SDO (direction=output) | Outer `cap`, shared inner `vpulse` |

**Key concepts:**
- **Analog local ground zones** - each `analog_ground` pin defines a zone; all
  `analog_power` and `analog_current` pins in that zone reference it as `local_pvss`
- **Digital supply pairs** - current sources (`idc`) placed between digital
  supply/ground pairs (e.g., PVDD2POC->PVSS2DGZ, VIOL->GIOL)
- **Shared output vpulse** - all `digital_io_output` pins share one inner vpulse
- **Non-round values** - all stimulus parameters use non-round numbers (e.g., `0.87`
  not `0.9`, `2.3m` not `2m`) for realistic simulation stress

Full rules in [`references/pin_classification.md`](references/pin_classification.md).

### `sim_config.json`

Specifies analyses and per-pin measurement intent (the code translates intent into
correct OCEAN expressions):

- **Always DC + TRAN** (no AC, no sweep)
- `tstop = 10 x max(per)` across all vpulse sources (clamped to `[100n, 10u]`)
- `pin_measurements` - declare what to measure per pin: `voltage`, `current`, `power`
- Never write raw OCEAN expressions - specify intent, code handles syntax

Full rules in [`references/sim_config_rules.md`](references/sim_config_rules.md).

---

## Usage

**Via Claude Code (natural language):**
```
Create a simulation testbench for IO_RING_test in library LLM_Layout_Design.
```
Or explicitly: `Use SIM-IO to build a testbench for cell X in library Y.`

### Writing Effective Prompts

**Required in every prompt:**
- Library name (`lib`)
- Cell name (`cell`) - must have an existing schematic view in Virtuoso
- Optional: `--vdd <volts>` (default 1.8)

**Example prompt:**
```
Build a simulation testbench for cell IO_RING_4x4_mixed in library LLM_Layout_Design.
VDD is 0.9V. Run simulation after building.
```

### Step-by-step CLI usage

```bash
# symbol_export: symbol export + pin extraction
.venv/bin/python .claude/skills/SIM-IO/scripts/symbol_export.py <lib> <cell>

# -> LLM classifies pins (writes pin_classifications.json + sim_config.json)

# testbench_build: TB build + source/load placement
.venv/bin/python .claude/skills/SIM-IO/scripts/tb_builder.py

# Simulation (optional)
.venv/bin/python .claude/skills/SIM-IO/scripts/spectre_runner.py
```

---

## Configuration Reference

### Bridge `.env` variables

Bridge connection is configured via `virtuoso-bridge init`. See
[`virtuoso-bridge-lite/README.md`](https://github.com/chenzc24/virtuoso-bridge-lite#quick-start)
for the full reference (`VB_REMOTE_HOST`, `VB_REMOTE_USER`, jump hosts, multi-profile, etc.).

### SIM-IO skill `.env` variables

| Variable | Description | Required |
|---|---|---|
| `SIM_CDS_LIB` | Remote path to `cds.lib` | Yes |
| `SIM_IC_ROOT` | Remote Cadence IC installation root | Yes |
| `SIM_MMSIM_ROOT` | Remote Spectre/MMSIM root (auto-discovered if unset) | No |
| `SIM_LM_LICENSE_FILE` | License server (auto-discovered if unset) | No |
| `SIM_CDS_LIC_FILE` | License server (auto-discovered if unset) | No |
| `SIM_PDK_IO_SPECTRE_INCLUDE` | Remote path to TSMC28 IO pad SPICE model | Yes |
| `SIM_PDK_CORE_SPECTRE_INCLUDE` | Remote path to TSMC28 core Spectre model | Yes |
| `SIM_PDK_CORE_SPECTRE_SECTIONS` | Model sections to include (default: TT corner) | No |

These live in `.claude/skills/SIM-IO/.env`.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Virtuoso connection fails | `virtuoso-bridge status` -> `restart`; confirm daemon `.il` loaded in CIW |
| symbol_export "not found in Virtuoso" | Check `lib`/`cell` spelling; verify cds.lib is loaded |
| symbol_export "no schematic view" | Cell exists but has no schematic - open schematic in Virtuoso first |
| `pin_classifications.json not found` | WARNING only - testbench_build runs with heuristic fallback; write the file for accurate placement |
| Wrong device placed | Re-check `pin_classifications.json`; verify `device_class`, `domain`, `stimulus`, `inner_stimulus` |
| Spectre: license error | Set `SIM_LM_LICENSE_FILE` and `SIM_CDS_LIC_FILE` in `.env` |
| Spectre: no convergence | Check stimulus values - ensure `vdc`/`vpulse` params are within PDK operating range |
| Maestro eval error | Maestro dialog may require manual confirmation in Virtuoso GUI |
| `si` netlist export hangs | Confirmation dialog opened in Virtuoso GUI - dismiss it manually |
| `import virtuoso_bridge` fails | Run `pip install -e virtuoso-bridge-lite` inside the project `.venv` |

---

## Related Documentation

| Document | Description |
|---|---|
| `SKILL.md` | Full workflow contract - entry points, workflow steps, troubleshooting |
| `references/pin_classification.md` | Pin classification rules, device classes, topology tables, value selection |
| `references/sim_config_rules.md` | Simulation config rules - analyses, measurements, tstop computation |
| `scripts/pin_classify_schema.json` | JSON schema for `pin_classifications.json` |
| `scripts/sim_config_schema.json` | JSON schema for `sim_config.json` |
| `virtuoso-bridge-lite/README.md` | Bridge CLI, SSH tunnel, daemon, multi-profile setup |

---

<!--=======================================================================-->
<!-- PART 2 - AI AGENT SETUP GUIDE                                         -->
<!-- Executable steps. No placeholders. [run] = run directly, [ask] = ask user. -->
<!--=======================================================================-->

## Agent Setup Guide

> **Human readers:** This section is written for AI agents performing automated
> setup. You can read it as a detailed reference, but the [Human Guide](#quick-setup-human)
> above covers everything you need more concisely.

Steps marked **[run]** can be executed with the exact commands shown.
Steps marked **[ask] -> [run]** require collecting values from the user first, then
writing them into the specified file. Every repo URL and command is concrete.

---

### Step 0 - Clone repositories [run]

```bash
# Run from the project root directory:
git clone https://github.com/chenzc24/virtuoso-bridge-lite.git
mkdir -p .claude/skills
git clone https://github.com/chenzc24/SIM-IO.git .claude/skills/SIM-IO
```

The skill lands in `.claude/skills/SIM-IO/` - Claude Code
discovers skills in `.claude/skills/` automatically. Bridge repo stays at project root.

---

### Step 1 - Create project `.venv` and install packages [run]

```bash
python -m venv .venv

# Activate (choose by OS):
source .venv/bin/activate          # Linux / Mac / Git Bash
# .venv\Scripts\Activate.ps1       # Windows PowerShell
# .venv\Scripts\activate.bat       # Windows CMD

pip install -e virtuoso-bridge-lite

# Verify:
python -c "import virtuoso_bridge; print('ok:', virtuoso_bridge.__version__)"
virtuoso-bridge --version          # expect: 0.6.x
```

One `.venv` serves all skills. SIM-IO has no additional requirements - `virtuoso-bridge-lite` is the only dependency.

---

### Step 2 - Initialize bridge config [ask] -> [run]

> All subsequent steps require the `.venv` to be active. If in a new terminal, run:
> `source .venv/bin/activate` (Linux/Mac) or `.venv\Scripts\Activate.ps1` (Windows).

**Ask user - required:**

| Question to ask user |
|---|
| "Hostname or IP of your EDA server?" |
| "SSH username on that server?" |

Then run `virtuoso-bridge init` to create the bridge `.env` with correct format and defaults:

```bash
virtuoso-bridge init <username>@<eda-server>
# With jump host:
# virtuoso-bridge init <username>@<eda-server> -J <username>@<jump-host>
```

This writes `~/.virtuoso-bridge/.env` with all bridge variables
(`VB_REMOTE_HOST`, `VB_REMOTE_USER`, ports, jump host, etc.) in the correct format.
**Do not** write the bridge `.env` manually - always use `virtuoso-bridge init`.

For advanced options (multi-profile, local mode, custom ports), see
[`virtuoso-bridge-lite/README.md`](https://github.com/chenzc24/virtuoso-bridge-lite#quick-start).

---

### Step 3 - Configure SIM-IO skill `.env` [ask] -> [run]

**Ask user - required:**

| Variable | Question to ask user |
|---|---|
| `SIM_CDS_LIB` | "Remote Linux path to your `cds.lib`? (e.g. `/home/youruser/TSMC28/cds.lib`)" |
| `SIM_IC_ROOT` | "Remote path to Cadence IC installation? (e.g. `/home/cadence/ic618/IC618Hotfix4`)" |
| `SIM_PDK_IO_SPECTRE_INCLUDE` | "Remote path to TSMC28 IO pad SPICE model? (e.g. `/home/process/tsmc28n/.../tphn28hpcpgv18.spi`)" |
| `SIM_PDK_CORE_SPECTRE_INCLUDE` | "Remote path to TSMC28 core Spectre model? (e.g. `/home/process/tsmc28n/.../crn28ull_*.scs`)" |

**Ask user - optional:**

| Variable | Question to ask user |
|---|---|
| `SIM_MMSIM_ROOT` | "Remote path to Spectre/MMSIM? (leave blank for auto-discovery from Virtuoso)" |
| `SIM_LM_LICENSE_FILE` | "License server? (e.g. `5280@server`; leave blank for auto-discovery)" |

Write values into `.claude/skills/SIM-IO/.env`. The file ships pre-filled with
defaults and example paths - use the Edit tool to update only the lines marked
`# -> CHANGE`, replacing example paths with the user's actual paths.

---

### Step 4 - Start bridge and verify [run]

```bash
virtuoso-bridge start         # opens SSH tunnel + deploys daemon on EDA server
virtuoso-bridge status        # expect: tunnel OK, daemon OK
```

Instruct user to load the daemon SKILL file in Virtuoso CIW once per Virtuoso session.
`virtuoso-bridge start` prints the exact path to load:
```skill
load("/tmp/virtuoso_bridge_<user>/virtuoso_bridge/virtuoso_setup.il")
```

Verify end-to-end - ask user to confirm that Virtuoso CIW shows no errors after loading
the daemon `.il` file.

---

### Setup complete OK
```
<project-root>/
|-- .venv/                                         -> shared env (bridge + all skills)
|-- virtuoso-bridge-lite/                          -> bridge source (editable install)
`-- .claude/skills/SIM-IO/
    |-- .env                                       -> SIM-IO config (CDS paths, PDK models, license)
    |-- sim_io/                                    -> core Python package
    |-- scripts/                                   -> CLI entry points
    |-- skill_code/                                -> SKILL scripts (.il files)
    `-- references/                                -> pin classification + sim config rules
```

Bridge config lives in `~/.virtuoso-bridge/.env` (created by `virtuoso-bridge init` in Step 2).
`AMS_PYTHON` in `SKILL.md` Step 0 finds `.venv` at project root automatically - no manual activation needed when Claude Code runs scripts.
