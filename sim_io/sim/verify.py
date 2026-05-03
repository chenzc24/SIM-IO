"""
Simulation Verification — Golden Mapping + Tolerance Comparison.

Compares measured simulation results against expected golden values
for each pin type. Generates a PASS/FAIL verification report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional


# ── Golden Mapping ─────────────────────────────────────────────
# Expected behavior per pin type, keyed by pad_type.
# Values are dicts of {metric: limit} where limit interpretation depends on tolerance rules.

def golden_for_pin_type(pad_type: str, vdd: float) -> dict:
    """Return golden metrics for a given pad type at the specified VDD.

    Golden values are derived from IO ring design specs:
      - Digital outputs must swing rail-to-rail (within 10% of VDD/GND)
      - Power pins should draw minimal quiescent current
      - Clock/reset pins follow digital input specs
    """
    voh_min = 0.9 * vdd   # Output high minimum
    vol_max = 0.1 * vdd   # Output low maximum

    golden = {
        "digital_input": {
            "vmax_min": voh_min,       # Input signal must reach VDD
            "vmin_max": vol_max,       # Input signal must reach GND
        },
        "digital_output": {
            "vmax_min": voh_min,       # Output must reach VDD
            "vmin_max": vol_max,       # Output must reach GND
        },
        "digital_bidirectional": {
            "vmax_min": voh_min,
            "vmin_max": vol_max,
        },
        "clock": {
            "vmax_min": voh_min,
            "vmin_max": vol_max,
        },
        "reset": {
            "vmax_min": voh_min,
            "vmin_max": vol_max,
        },
        "power": {
            "vmax_min": vdd * 0.99,    # Power rail should be close to VDD
            "vmax_max": vdd * 1.01,
            "iavg_max": 0.1,           # < 100 mA quiescent per power pin
            "pavg_max": vdd * 0.1,     # < 100mA * VDD power budget
        },
        "analog_input": {
            "vmax_min": vdd * 0.4,     # At minimum, DC bias should be present
        },
        "analog_output": {
            "vmax_min": vdd * 0.1,     # Some output swing expected
        },
        "analog_bidirectional": {
            "vmax_min": vdd * 0.1,
        },
    }
    return golden.get(pad_type, {})


# ── Tolerance Rules ────────────────────────────────────────────

@dataclass
class Tolerance:
    """Tolerance rule for a metric comparison."""
    absolute: float = 0.0   # Absolute tolerance (e.g., 50mV)
    relative: float = 0.0   # Relative tolerance (e.g., 0.03 = 3%)

    def check(self, measured: float, golden: float) -> bool:
        """Check if measured value is within tolerance of golden."""
        diff = abs(measured - golden)
        rel = diff / abs(golden) if golden != 0 else diff
        return diff <= self.absolute or rel <= self.relative


# Default tolerance rules by metric type
DEFAULT_TOLERANCES: dict[str, Tolerance] = {
    "voltage": Tolerance(absolute=0.050, relative=0.03),   # 50mV or 3%
    "current": Tolerance(absolute=0.0, relative=0.20),     # 20%
    "power":   Tolerance(absolute=0.0, relative=0.30),     # 30%
    "delay":   Tolerance(absolute=1e-9, relative=0.10),    # 1ns or 10%
    "default": Tolerance(absolute=0.050, relative=0.05),   # 5% fallback
}


def _tolerance_for_metric(metric: str) -> Tolerance:
    """Pick the tolerance rule for a metric name."""
    name = metric.lower()
    if "volt" in name or name.startswith("v"):
        return DEFAULT_TOLERANCES["voltage"]
    if "curr" in name or name.startswith("i"):
        return DEFAULT_TOLERANCES["current"]
    if name.startswith("p") and ("avg" in name or "max" in name or "static" in name or "dynamic" in name):
        return DEFAULT_TOLERANCES["power"]
    if "delay" in name or "slew" in name:
        return DEFAULT_TOLERANCES["delay"]
    return DEFAULT_TOLERANCES["default"]


# ── Verification ───────────────────────────────────────────────

@dataclass
class PinVerification:
    pin_name: str
    pad_type: str
    status: str           # "pass" | "fail" | "skip" | "error"
    metrics: dict         # {metric: {measured, golden, within_tolerance}}
    failures: list[str] = field(default_factory=list)


@dataclass
class VerifyReport:
    cell: str
    vdd: float
    analysis: str
    verdict: str          # "PASS" | "FAIL" | "INCOMPLETE"
    pins: list[PinVerification]
    num_pass: int = 0
    num_fail: int = 0
    num_skip: int = 0
    num_error: int = 0

    def save(self, path: str | Path) -> None:
        data = asdict(self)
        Path(path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def verify_pin(
    pin_name: str,
    pad_type: str,
    measurements: dict,
    vdd: float,
) -> PinVerification:
    """Verify a single pin's measurements against golden values."""
    if "error" in measurements:
        return PinVerification(
            pin_name=pin_name,
            pad_type=pad_type,
            status="error",
            metrics={},
            failures=[measurements["error"]],
        )

    golden = golden_for_pin_type(pad_type, vdd)
    if not golden:
        return PinVerification(
            pin_name=pin_name,
            pad_type=pad_type,
            status="skip",
            metrics={},
            failures=[f"No golden mapping for pad_type={pad_type}"],
        )

    metric_results = {}
    failures = []

    for golden_key, golden_val in golden.items():
        # Map golden key to measurement key
        # golden "vmax_min" → measured "vmax", golden "vmin_max" → measured "vmin"
        meas_key = golden_key.rsplit("_", 1)[0]  # "vmax_min" → "vmax"
        measured_val = measurements.get(meas_key)

        if measured_val is None:
            failures.append(f"{meas_key}: not measured (golden={golden_val:.4f})")
            metric_results[golden_key] = {
                "measured": None,
                "golden": golden_val,
                "within_tolerance": False,
            }
            continue

        tol = _tolerance_for_metric(meas_key)
        # Determine comparison direction from suffix
        suffix = golden_key.rsplit("_", 1)[-1]  # "min" or "max"
        if suffix == "min":
            ok = measured_val >= golden_val or tol.check(measured_val, golden_val)
        elif suffix == "max":
            ok = measured_val <= golden_val or tol.check(measured_val, golden_val)
        else:
            ok = tol.check(measured_val, golden_val)

        metric_results[golden_key] = {
            "measured": measured_val,
            "golden": golden_val,
            "within_tolerance": ok,
        }
        if not ok:
            failures.append(
                f"{meas_key}: measured={measured_val:.4f}, golden={golden_val:.4f}"
            )

    status = "pass" if not failures else "fail"
    return PinVerification(
        pin_name=pin_name,
        pad_type=pad_type,
        status=status,
        metrics=metric_results,
        failures=failures,
    )


def verify_results(
    measurements: dict,
    vdd: float = 1.8,
    cell: str = "",
    analysis: str = "tran",
) -> VerifyReport:
    """Verify all pin measurements against golden mapping.

    Parameters
    ----------
    measurements : Output from sim_run.parse_results()
    vdd : Supply voltage for golden value calculation
    cell : Cell name for the report
    analysis : Analysis type (tran/dc)

    Returns a VerifyReport with per-pin and overall verdict.
    """
    pins_data = measurements.get("pins", {})
    pin_verifications = []

    for pin_name, pin_meas in pins_data.items():
        pad_type = pin_meas.get("pad_type", "unknown")
        pv = verify_pin(pin_name, pad_type, pin_meas, vdd)
        pin_verifications.append(pv)

    num_pass = sum(1 for p in pin_verifications if p.status == "pass")
    num_fail = sum(1 for p in pin_verifications if p.status == "fail")
    num_skip = sum(1 for p in pin_verifications if p.status == "skip")
    num_error = sum(1 for p in pin_verifications if p.status == "error")

    if num_fail > 0:
        verdict = "FAIL"
    elif num_pass == 0:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"

    report = VerifyReport(
        cell=cell,
        vdd=vdd,
        analysis=analysis,
        verdict=verdict,
        pins=pin_verifications,
        num_pass=num_pass,
        num_fail=num_fail,
        num_skip=num_skip,
        num_error=num_error,
    )
    return report


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python sim_verify.py <measurements.json> [vdd]")
        sys.exit(1)

    meas_path = Path(sys.argv[1])
    vdd = float(sys.argv[2]) if len(sys.argv) > 2 else 1.8
    measurements = json.loads(meas_path.read_text(encoding="utf-8"))

    report = verify_results(measurements, vdd=vdd)
    out_path = meas_path.parent / "verify.json"
    report.save(out_path)

    print(f"Verdict: {report.verdict}")
    print(f"  Pass: {report.num_pass}  Fail: {report.num_fail}  "
          f"Skip: {report.num_skip}  Error: {report.num_error}")
    if report.num_fail:
        for pv in report.pins:
            if pv.status == "fail":
                print(f"  FAIL: {pv.pin_name} ({pv.pad_type}): {', '.join(pv.failures)}")
