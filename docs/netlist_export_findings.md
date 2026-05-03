# Netlist Export — Debug Findings

> Date: 2026-05-01
> Tested on: `LLM_Layout_Design_Lab / IO_RING_12x12_tb`

## Summary

Netlist export via `si` batch netlister works, but requires 4 prerequisites that were not in the original implementation:

1. **License env vars** must be set explicitly (si doesn't inherit Virtuoso's license)
2. **`analogLib`/`basic` libraries** must be resolvable (not in user's cds.lib)
3. **`schCheck + dbSave`** must run before si (OSSHNL-109 otherwise)
4. **Complete `si.env`** must be written (simInitEnvWithArgs produces incomplete output)

## Issue 1: License Not Available

**Symptom**:
```
ERROR (LMF-02001): License call failed for feature 111, version 6.180
FLEXnet ERROR(-1, 0, 0): Cannot find license file.
```

**Root cause**: `si` is a standalone binary. It does NOT inherit Virtuoso's license. The SSH shell session doesn't have `LM_LICENSE_FILE` or `CDS_LIC_FILE` set.

**Discovery**: Queried Virtuoso's own env via SKILL:
```python
client.execute_skill('getShellEnvVar("LM_LICENSE_FILE")')
# → "1717@lic_server:5280@thu-han"

client.execute_skill('getShellEnvVar("CDS_LIC_FILE")')
# → "5280@thu-han"
```

**Fix**: Set license env vars explicitly before running si:
```bash
export LM_LICENSE_FILE=1717@lic_server:5280@thu-han
export CDS_LIC_FILE=5280@thu-han
```

**Code**: `_discover_cadence_env()` queries Virtuoso and returns the license vars. `export_netlist()` injects them into the si command.

## Issue 2: analogLib/basic Not Resolved

**Symptom**:
```
ERROR (OSSHNL-366): Netlisting failed because the instance 'GND0' is bound to
an invalid placed master 'analogLib/gnd/symbol'. Ensure that the specified placed
master exists and is included in the list of reference libraries in the cds.lib file.
```

**Root cause**: The user's `~/cds.lib` only contains project library definitions (`DEFINE LLM_Layout_Design_Lab ...`) and an `INCLUDE` of the TSMC cds.lib. Neither includes `analogLib` or `basic` definitions.

In Virtuoso GUI, these are resolved via the IC618 default cds.lib chain:
```
~/cds.lib
  → INCLUDE /home/dmanager/shared_lib/TSMC28/cds.lib (no analogLib/basic)

Virtuoso also loads (internally):
  → SOFTINCLUDE .../share/cdssetup/cds.lib
    → SOFTINCLUDE .../cdsDotLibs/composer/cds.lib   → DEFINE basic ...
    → SOFTINCLUDE .../cdsDotLibs/artist/cds.lib      → DEFINE analogLib ...
```

The `si` batch tool only reads the cds.lib you pass via `-cdslib`, without Virtuoso's internal chain.

**Fix**: Create a local cds.lib that SOFTINCLUDEs the IC618 default cds.lib:
```
SOFTINCLUDE /home/cadence/ic618/IC618Hotfix4/share/cdssetup/cds.lib
INCLUDE /home/chenzc_intern25/cds.lib
```

**Actual library paths** (resolved from IC618 chain):
| Library | Path |
|---------|------|
| analogLib | `/home/cadence/ic618/IC618Hotfix4/tools/dfII/etc/cdslib/artist/analogLib` |
| basic | `/home/cadence/ic618/IC618Hotfix4/tools/dfII/etc/cdslib/basic` |

## Issue 3: schCheck Required

**Symptom**:
```
ERROR (OSSHNL-109): The cellview has been modified since the last extraction.
Validate that the schematic is correct and run Check and Save.
```

**Root cause**: After placing instances, labels, and setting CDF params via Python, the schematic is "modified" but not "checked". The `si` netlister requires a clean schCheck state.

**Fix**: Run `schCheck(cv) + dbSave(cv)` on the _tb schematic before running si.

## Issue 4: Incomplete si.env

**Symptom**: `simInitEnvWithArgs()` produces a truncated si.env missing `simViewList` and `simStopList`.

**Observed si.env from simInitEnvWithArgs**:
```
simLibName = "LLM_Layout_Design_Lab"
simCellName = "IO_RING_12x12_tb"
simViewName = "schematic"
simSimulator = "spectre"
simNotIncremental = 't        ← missing closing quote
simReNetlistAll = nil
```

Missing: `simViewList`, `simStopList`, `simNetlistHier`

**Fix**: Write a complete si.env manually after calling simInitEnvWithArgs:
```
simLibName = "LLM_Layout_Design_Lab"
simCellName = "IO_RING_12x12_tb"
simViewName = "schematic"
simSimulator = "spectre"
simViewList = '("spectre" "cmos_sch" "schematic" "veriloga")
simStopList = '("spectre")
simNetlistHier = t
simNotIncremental = t
simReNetlistAll = nil
```

## Full Working Command

```bash
export PATH=/home/cadence/ic618/IC618Hotfix4/tools/bin:/home/cadence/ic618/IC618Hotfix4/tools/dfII/bin:$PATH
export LD_LIBRARY_PATH=/home/cadence/ic618/IC618Hotfix4/tools/lib/64bit:$LD_LIBRARY_PATH
export LM_LICENSE_FILE=1717@lic_server:5280@thu-han
export CDS_LIC_FILE=5280@thu-han

cd /tmp/sim_io_si_run
/home/cadence/ic618/IC618Hotfix4/tools/dfII/bin/si \
    -batch \
    -cdslib /tmp/sim_io_si_run/cds.lib \
    -command nl
```

## Netlist Output

Successfully exported 174-line Spectre netlist (8697 bytes) containing:
- `IO_RING_12x12` subcircuit with all TSMC28 IO pad instances (PDDW16SDGZ, PDB3AC, PVDD1AC, etc.)
- `IO_RING_12x12_tb` top-level with DUT instance + all sources/loads
- `global 0` and `simulator lang=spectre` headers

## Key Paths Discovered

| Item | Path |
|------|------|
| si binary | `/home/cadence/ic618/IC618Hotfix4/tools/dfII/bin/si` |
| IC618 root | `/home/cadence/ic618/IC618Hotfix4` |
| IC618 default cds.lib | `/home/cadence/ic618/IC618Hotfix4/share/cdssetup/cds.lib` |
| User cds.lib | `/home/chenzc_intern25/cds.lib` |
| License server | `LM_LICENSE_FILE=1717@lic_server:5280@thu-han` |
| License server | `CDS_LIC_FILE=5280@thu-han` |
| Virtuoso working dir | `/home/chenzc_intern25/TSMC28/llm_IO` |
