"""Explicit model roles for candidate analog readout gain changes.

The legacy ``valid_domain.gain`` axis has no path identity. A numeric interval
alone cannot prove that changing the electrical readout's analog gain leaves
the optical response and ISP calibration applicable.
"""

from __future__ import annotations

import math
from copy import deepcopy

PROPERTY_KEY = "analog_gain_applicability"
SCHEMA = "camera_analog_gain_applicability_v1"
ROLES = {
    "lens": "optical_response_independent",
    "sensor": "sensor_model_readout_condition",
    "isp": "raw_input_processing_condition",
}


def validate_model_declaration(model):
    """Validate an authored applicability claim without granting physical status."""
    properties = model.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Model properties must be an object")
    declaration = properties.get(PROPERTY_KEY)
    if declaration is None:
        return None
    if (not isinstance(declaration, dict)
            or set(declaration) != {"schema", "role", "basis"}
            or declaration["schema"] != SCHEMA
            or declaration["role"] != ROLES.get(model.get("kind"))
            or declaration["basis"] != "authored_model_scope"):
        raise ValueError("Analog gain applicability needs its exact model role and authored scope")
    bounds = model.get("valid_domain", {}).get("gain")
    if (not isinstance(bounds, dict) or set(bounds) != {"min", "max", "unit"}
            or bounds["unit"] != "1"
            or type(bounds["min"]) not in (int, float)
            or type(bounds["max"]) not in (int, float)
            or not math.isfinite(bounds["min"])
            or not math.isfinite(bounds["max"])
            or not 0 < bounds["min"] <= bounds["max"]):
        raise ValueError("Declared analog gain applicability requires a dimensionless gain domain")
    return deepcopy(declaration)


def model_declarations(models):
    """Report role ownership; an absent declaration remains unresolved."""
    result = []
    for model in models:
        declaration = validate_model_declaration(model)
        result.append({
            "model_id": model["id"], "kind": model["kind"],
            "status": "declared" if declaration is not None else "undeclared",
            "role": declaration["role"] if declaration else None,
            "basis": declaration["basis"] if declaration else None,
        })
    return result
