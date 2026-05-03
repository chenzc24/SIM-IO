# SIM-IO To-Do

## Priority 1 — Core Flow Completeness

### 1.1 Extract schematic internal info for pin classification
**Problem**: Pin classification is purely naming-based (prefix matching). Without schematic-level info, pins like `PAD1` or `IO_A` cannot be accurately typed.

**Plan**: Extend `pin_info.json` with schematic connectivity data:
- What cell each pin connects to internally (e.g., `tpdi28` = digital input pad)
- Which pins share the same internal net (helps identify power/ground groups)
- What cell types are instantiated (pad cells, buffers, level shifters)

**Requires**: New SKILL code to query:
- `cv~>instances~>cellName` — internal instance cell names
- `cv~>nets~>terminals` — net→terminal connectivity
- `cv~>terminals~>nets~>name` — terminal→net mapping

**Files**: `skill_code/extract_schematic_info.il` (new), `sim_io/flow.py` (update `extract_dut_pins`)

---

### 1.2 End-to-end test on real IO ring
**Status**: Pipeline verified on `5T_AMP_dc` only. Never tested on actual IO ring design.

**Blocked by**: PDK IO pad cell models on remote server (TSMC28 `tphn28hpcpgv18` etc.)

**Actions**:
- Locate TSMC28 IO pad spectre model files on remote
- Run full pipeline on `IO_RING_12x12` with dual-side topology
- Verify PVSS placement, inner devices, ground domain wiring
- Validate LVS correctness (noConn, ground sharing)

---

### 1.3 noConn terminal name verification
**Risk**: `analogLib/noConn` terminal name assumed as `"PLUS"`. Different PDKs may use different names.

**Action**: Check actual terminal name of `noConn` symbol in target PDK. If not `"PLUS"`, add a configurable terminal name lookup.

---

## Priority 2 — Robustness

### 2.1 Partial LLM classification handling
**Current**: If some pins have LLM classification and others don't, fallback pins use `gnd!` directly (bypass PVSS). This is inconsistent — analog fallback pins should still connect through block-local ground.

**Plan**: When LLM classifications exist for ANY pin, infer ground_net for unclassified pins from their block suffix (e.g., unclassified `IB_DAT` → `gnd_DAT` from existing PVSS_DAT).

---

### 2.2 Right-side pin device placement (edge case)
**Current**: Only left-side pins get outer/inner devices. Right-side pins are assumed to be `_CORE` or duplicate variants handled by left-side inner devices.

**Gap**: If a right-side pin has its own LLM classification with `stimulus`/`load`, it won't get devices placed.

**Plan**: Add Phase 2b — iterate right-side pins that have LLM classification but are NOT handled by any left-side pin's inner device. Place their outer devices on the right side.

---

### 2.3 CDF param value validation
**Risk**: LLM-generated param values are passed as-is to SKILL `setInstParams`. Invalid values (wrong units, typos) cause silent SKILL errors.

**Plan**: Add basic validation before SKILL call — check value format against known patterns (`"0.9"`, `"10p"`, `"100n"`, `"2.7m"`). Log warnings for suspicious values.

---

### 2.4 Instance name collision prevention
**Risk**: Pin names like `SRC_D0` would create instance `SRC_SRC_D0`. Unlikely but possible with unusual naming.

**Plan**: Add collision check in `place_sources_and_loads()`. If generated name already exists in `placed`, append numeric suffix.

---

## Priority 3 — Feature Enhancements

### 3.1 ADE Assembler integration
**Status**: Deferred (requires ADE permission in bridge).

**Plan**: After TB build (Step 4), open ADE Assembler, configure analyses from `SimDeckConfig`, and optionally run simulation from GUI.

---

### 3.2 Interactive pin classification review
**Current**: LLM writes `pin_classifications.json` and pipeline uses it blindly.

**Plan**: Add a review step — after LLM classification, print summary table (pin name, type, domain, confidence). Let user approve/edit before proceeding with placement.

---

### 3.3 Multi-vdd support
**Current**: Single `vdd_value` parameter for the entire design.

**Reality**: IO rings have multiple supply domains (core 0.9V, IO low 0.9V, IO high 1.8V, HV 3.3V). The `vio_low`/`vio_high` fields in `ClassificationResult` exist but aren't used by the code.

**Plan**: Read `vdd_value`, `vio_low`, `vio_high` from `ClassificationResult`. Use them for VDD substitution in each domain (e.g., `VIOL` pins get `vio_low`, `VIOH` pins get `vio_high`).

---

### 3.4 Layout tuning for different designs
**Current**: `LayoutConfig` defaults are tuned for one design.

**Plan**: Add design-specific layout presets. Auto-detect from pin count / cell name.

---

## Priority 4 — Code Quality

### 4.1 Update progress.md
**Current**: `docs/progress.md` has old file paths (`src/` instead of `sim_io/`) and doesn't reflect the dual-side topology changes.

**Plan**: Update file map, pin classification rules, wiring pattern, and changelog.

---

### 4.2 Unit tests for pin classification
**Current**: No tests for `pin_types.py` or `place_sources_and_loads()`.

**Plan**: Add tests for:
- `classify_pin_heuristic()` with various pin names
- `load_pin_classifications()` with valid/invalid JSON
- `_find_core_pin_name()` with _CORE, duplicate, and missing pins
- `_resolve_param_value()` with VDD, VDD/2, plain values
- Ground domain collection from LLM classifications

---

### 4.3 Type annotations audit
**Current**: Some functions use `dict`/`list` without element types.

**Plan**: Add full type annotations to `pin_types.py` and `flow.py`. Run mypy or pyright.
