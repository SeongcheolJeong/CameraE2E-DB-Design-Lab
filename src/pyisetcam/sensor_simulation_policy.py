"""Source-qualified scope exceptions for batch sensor simulation.

An ``allow`` result is not optical validation. The caller must still apply the
model's ordinary modality, geometry and wavelength-domain checks. This policy
never edits source databases or fills missing product measurements.
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any


@lru_cache(maxsize=1)
def _manifest() -> dict[str, Any]:
    path = files("pyisetcam").joinpath("data/sensor_estimate/simulation_scope_v1.json")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "camerae2e_sensor_simulation_scope_v1":
        raise ValueError("Unsupported sensor simulation scope manifest.")
    for code, entry in manifest.get("entries", {}).items():
        if not re.fullmatch(r"[A-Z]{3}-\d{4}-\d{3}", code):
            raise ValueError(f"Invalid simulation-policy report code: {code!r}.")
        if entry.get("action") not in {"allow", "exclude", "override"}:
            raise ValueError(f"Invalid simulation-policy action for {code}.")
        if not entry.get("reason") or not entry.get("evidence"):
            raise ValueError(f"Missing simulation-policy reason/evidence for {code}.")
        if entry["action"] == "override":
            if entry.get("model_override") != "db_estimate_v1":
                raise ValueError(f"Unsupported simulation-policy model override for {code}.")
            if entry.get("cfa_pattern_override") not in {None, "quad_bayer", "monochrome"}:
                raise ValueError(f"Unsupported simulation-policy CFA override for {code}.")
        recovery = entry.get("missing_cfa_recovery")
        if recovery is not None and (
            code != "DEP-1902-801"
            or entry.get("action") != "override"
            or entry.get("source_reviewed") is not True
            or entry.get("cfa_pattern_override") != "monochrome"
            or recovery.get("transmission_model") != "unity_color_filter_absorption_only"
        ):
            raise ValueError(f"Unsupported missing-CFA recovery for {code}.")
    return manifest


def sensor_simulation_policy(report_code: str) -> dict[str, Any]:
    """Return an independent, JSON-safe scope decision for an exact report code.

    ``exclude`` blocks this visible native-RAW batch, not every possible future
    use of a sensor. ``override`` requests a lumped DB estimate and, only where
    supported by explicit source evidence, a corrected CFA pattern. The sole
    missing-CFA recovery is the explicitly monochromatic DEP-1902-801 report.
    Unknown codes remain subject to the normal model eligibility gate.
    """
    if not isinstance(report_code, str):
        raise ValueError("report_code must be a TechInsights report-code string.")
    code = report_code.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}-\d{4}-\d{3}", code):
        raise ValueError("report_code must use the form DEF-2206-805.")
    manifest = _manifest()
    entry = manifest["entries"].get(code)
    if entry is None:
        entry = {
            "action": "allow",
            "reason": manifest["default_action_meaning"],
            "source_reviewed": False,
            "evidence": [],
        }
    return {
        **copy.deepcopy(entry),
        "report_code": code,
        "policy_schema": manifest["schema"],
        "policy_version": manifest["version"],
        "measured_accuracy_verified": False,
    }
