"""Structural validation of patent lens prescriptions, independent of ray tracing.

Passing these checks means that a prescription can be considered for geometric
model construction. It does not certify its PSF, radiometry, or image quality.
Both exported CSV rows and ``lens_surfaces`` SQLite dictionaries are accepted.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

VALIDATOR_VERSION = "1.0.1"
SCHEMA_VERSION = 1
_INFINITY = {"inf", "+inf", "infinity", "+infinity", "∞", "+∞"}
_VARIABLE = re.compile(r"variable|\bvariabl\b|^dd\d*$|^d\d*$|\bbf\b", re.IGNORECASE)
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?"
_MERGED_DESCRIPTOR = re.compile(
    rf"\s+\d+\*?\s+\([^)]*\)\s+(?:{_NUMBER}|Infinity|inf|∞)", re.IGNORECASE
)
_MERGED_ASPHERE_ROW = re.compile(rf"\s+\d+\*\s+{_NUMBER}\s+{_NUMBER}")


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _raw(row: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(row.get("raw")) or _mapping(row.get("raw_json"))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().replace("−", "-").replace("D", "E").replace("d", "e"))
    except (ValueError, TypeError):
        return None
    return (0.0 if number == 0 else number) if math.isfinite(number) else None


def _value(row: Mapping[str, Any], raw: Mapping[str, Any], *keys: str) -> Any:
    # SQLite encodes Infinity and failed numeric conversion as NULL. Recover
    # the original token before deciding which of those two states it means.
    for source in (row, raw):
        for key in keys:
            if source.get(key) is not None and str(source[key]).strip() != "":
                return source[key]
    return None


def _length(value: Any, *, radius: bool = False) -> tuple[float | None, str, str]:
    token = "" if value is None else str(value).strip()
    if token.lower() in _INFINITY or value == math.inf:
        return None, "plane" if radius else "infinite", ""
    number = _number(value)
    if number is not None:
        return number, "plane" if radius and number == 0 else "finite", ""
    if not token:
        return None, "unknown", ""
    state = "variable" if _VARIABLE.search(token) else "invalid"
    return None, state, token.lower()


def _coefficient_tokens(row: Mapping[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Collect all declared radial terms, including a2 and odd polynomial orders.

    Explicit zero terms retain their existing canonical representation. Empty
    CSV cells and absent keys are both omitted. A normalized coefficient mapping
    takes precedence over source cells, as it does for other resolved DB values.
    """
    result: dict[str, Any] = {}
    sources = (
        _mapping(row.get("coefficients")) or _mapping(row.get("coefficients_json")),
        row,
        raw,
    )
    for source in sources:
        for key, value in source.items():
            key = str(key).lower()
            if key != "k" and re.fullmatch(r"a\d+", key) is None:
                continue
            if key not in result and value is not None and str(value).strip():
                result[key] = value
    return result


def normalize_lens_surfaces(surfaces: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return representation-independent, JSON-safe prescription properties.

    This intentionally excludes database IDs and raw text. Its output is the
    canonical input to the prescription fingerprint used by DB and CSV paths.
    """
    result: list[dict[str, Any]] = []
    for index, source in enumerate(surfaces, 1):
        row = dict(source)
        raw = _raw(row)
        order = _number(_value(row, raw, "surface_order"))
        radius, radius_state, radius_expression = _length(
            _value(row, raw, "radius_mm", "radius"), radius=True
        )
        thickness, thickness_state, thickness_expression = _length(
            _value(row, raw, "thickness_mm", "thickness")
        )
        coefficients = _coefficient_tokens(row, raw)
        conic = _number(_value(row, raw, "conic"))
        if conic is None:
            conic = _number(coefficients.get("k"))
        normalized_coefficients = {
            str(key).lower(): _number(value)
            for key, value in sorted(coefficients.items())
            if _number(value) is not None
        }
        # k is represented by conic once, regardless of which input column held it.
        normalized_coefficients.pop("k", None)
        label = str(_value(row, raw, "surface_key", "surface_label") or index).strip(" *")
        result.append(
            {
                "surface_order": int(order) if order is not None and order.is_integer() else index,
                "surface_key": label,
                "surface_kind": str(_value(row, raw, "surface_kind") or "surface").strip().lower(),
                "radius_mm": 0.0 if radius_state == "plane" else radius,
                "radius_state": radius_state,
                "radius_expression": radius_expression,
                "thickness_mm": thickness,
                "thickness_state": thickness_state,
                "thickness_expression": thickness_expression,
                "nd": _number(_value(row, raw, "nd")),
                "vd": _number(_value(row, raw, "vd")),
                "material": str(_value(row, raw, "material") or "").strip(),
                "effective_aperture_mm": _number(
                    _value(row, raw, "effective_aperture_mm", "effective_aperture")
                ),
                "conic": conic,
                "is_aspheric": str(_value(row, raw, "is_aspheric") or "0").strip().lower()
                in {"1", "true"},
                "coefficients": normalized_coefficients,
            }
        )
    return result


def validate_lens_prescription(
    surfaces: Iterable[Mapping[str, Any]],
    *,
    focal_length_mm: float | None = None,
    f_number: float | None = None,
    configuration: str | None = None,
) -> dict[str, Any]:
    """Check source structure and values; ``valid`` means no structural errors.

    Unknown aperture/material information is reported as warning rather than
    invented. A downstream tracer may require stricter completeness. Inputs are
    not changed and the result is JSON serializable with no NaN/Infinity values.
    """
    source_rows = [dict(row) for row in surfaces]
    normalized = normalize_lens_surfaces(source_rows)
    issues: list[dict[str, Any]] = []

    def issue(code: str, severity: str, message: str, order: int | None = None) -> None:
        item: dict[str, Any] = {"code": code, "severity": severity, "message": message}
        if order is not None:
            item["surface_order"] = order
        issues.append(item)

    if not normalized:
        issue("empty_prescription", "error", "The prescription has no surfaces.")
    key_counts = Counter(
        row["surface_key"] for row in normalized if row["surface_kind"] == "surface"
    )
    order_counts = Counter(row["surface_order"] for row in normalized)
    for key, count in key_counts.items():
        if count > 1:
            issue(
                "duplicate_surface_key",
                "error",
                f"Optical surface key {key!r} occurs {count} times.",
            )
    if any(count > 1 for count in order_counts.values()):
        issue("duplicate_surface_order", "error", "Surface ordering contains duplicate positions.")

    powered_count = 0
    for row, source in zip(normalized, source_rows, strict=True):
        raw = _raw(source)
        order = row["surface_order"]
        kind = row["surface_kind"]
        physical = kind not in {"object", "image"}
        raw_surface = str(_value(source, raw, "raw_surface") or "")
        thickness_token = str(_value(source, raw, "thickness_mm", "thickness") or "")
        if thickness_token.strip().lower() == "k" or re.match(r"^\s*\d+\s+\d+\s+K\b", raw_surface):
            issue(
                "spurious_asphere_surface",
                "error",
                "An asphere table header was parsed as an optical surface.",
                order,
            )
        if _MERGED_DESCRIPTOR.search(raw_surface) or _MERGED_ASPHERE_ROW.search(raw_surface):
            issue(
                "merged_surface_rows",
                "error",
                "Raw text contains an additional numbered surface that was not segmented.",
                order,
            )
        if physical and row["radius_state"] not in {"plane", "finite"}:
            if kind == "stop" and row["radius_state"] == "unknown":
                issue(
                    "plane_radius_inferred",
                    "warning",
                    "The explicit stop has no radius; a plane must be declared by the tracer.",
                    order,
                )
            else:
                issue(
                    "unresolved_radius",
                    "error",
                    "Optical radius is missing or nonnumeric; an explicit plane is required.",
                    order,
                )
        if physical and row["thickness_state"] != "finite":
            issue(
                "unresolved_thickness",
                "error",
                "A required optical/stop gap is missing, infinite, or unresolved.",
                order,
            )
        if physical and row["thickness_mm"] is not None and row["thickness_mm"] < 0:
            issue(
                "negative_thickness",
                "warning",
                "Negative axial gap requires a documented virtual/folded coordinate convention.",
                order,
            )
        if kind == "surface" and row["radius_state"] in {"finite", "plane"}:
            base_curvature = 1.0 / row["radius_mm"] if row["radius_mm"] else 0.0
            # z(r) = c*r^2/2 + a2*r^2 + higher orders near the axis.
            # A plane-base quadratic can therefore have paraxial power, whereas
            # nonzero a4 and higher terms alone do not establish a finite EFL.
            effective_curvature = base_curvature + 2.0 * row["coefficients"].get("a2", 0.0)
            if effective_curvature != 0.0:
                powered_count += 1
        nd, vd = row["nd"], row["vd"]
        nd_token = _value(source, raw, "nd")
        vd_token = _value(source, raw, "vd")
        if nd_token is not None and (nd is None or nd < 1 or nd > 3):
            issue(
                "invalid_refractive_index",
                "error",
                "Ordinary material needs a finite index in [1, 3]; special media need a model.",
                order,
            )
        if nd is not None and nd > 1:
            if vd is None or vd <= 0:
                issue(
                    "invalid_abbe_number",
                    "error",
                    "Refractive material needs a positive finite Abbe number for dispersion.",
                    order,
                )
            if not row["material"] or row["material"].lower() in {"glass", "plastic", "cement"}:
                issue(
                    "material_identity_unknown",
                    "warning",
                    "Only nd/vd or a generic name is available; dispersion is approximate.",
                    order,
                )
        elif vd_token is not None and (vd is None or vd < 0):
            issue("invalid_abbe_number", "error", "Abbe number is nonnumeric or negative.", order)
        if nd is None and row["material"] and row["material"].lower() != "air":
            issue(
                "missing_refractive_index",
                "error",
                "A named material has no refractive index or explicit catalog model.",
                order,
            )
        if physical and (row["effective_aperture_mm"] is None or row["effective_aperture_mm"] <= 0):
            issue(
                "aperture_unknown",
                "warning",
                "Physical clear aperture is unknown; clipping needs an explicit assumption.",
                order,
            )
        has_terms = any(value != 0 for value in row["coefficients"].values()) or bool(row["conic"])
        if has_terms and not row["is_aspheric"]:
            issue(
                "asphere_flag_conflict",
                "warning",
                "Nonzero profile terms conflict with is_aspheric=0; apply the explicit terms.",
                order,
            )
        if row["is_aspheric"] and not row["coefficients"] and row["conic"] is None:
            issue(
                "missing_asphere_coefficients",
                "error",
                "The surface is marked aspheric but has no profile coefficients.",
                order,
            )
        for key, value in _coefficient_tokens(source, raw).items():
            if _number(value) is None:
                issue(
                    "invalid_asphere_coefficient",
                    "error",
                    f"Asphere coefficient {key} is not a finite number.",
                    order,
                )
        conic_token = _value(source, raw, "conic")
        if conic_token is not None and _number(conic_token) is None:
            issue(
                "invalid_asphere_coefficient",
                "error",
                "Conic constant is not a finite number.",
                order,
            )

    if normalized and powered_count == 0:
        issue(
            "no_powered_surface",
            "error",
            "No nonzero paraxial surface curvature remains to reproduce a finite-EFL lens.",
        )
    stop_count = sum(row["surface_kind"] == "stop" for row in normalized)
    if stop_count == 0:
        issue(
            "stop_unknown",
            "warning",
            "No explicit stop is present; entrance pupil modeling needs an explicit assumption.",
        )
    elif stop_count > 1:
        issue(
            "multiple_stops",
            "error",
            "Multiple stop records require disambiguation to construct one optical state.",
        )
    if normalized and not any(row["surface_kind"] == "image" for row in normalized):
        issue(
            "image_plane_implicit",
            "warning",
            "No explicit image plane exists; the tracer must document how it selects focus.",
        )
    if any(row["effective_aperture_mm"] is not None for row in normalized):
        issue(
            "aperture_convention_unverified",
            "warning",
            "effective_aperture has no explicit radius/diameter convention in the source schema.",
        )
    for label, value in (("focal_length_mm", focal_length_mm), ("f_number", f_number)):
        if value is not None and (_number(value) is None or _number(value) <= 0):
            issue(
                "invalid_optical_spec",
                "error",
                f"{label} must be finite and positive when supplied.",
            )
    if _number(focal_length_mm) is not None and 0 < _number(focal_length_mm) < 0.001:
        issue(
            "implausible_focal_length",
            "error",
            "Focal length below 0.001 mm requires source review for this camera-lens database.",
        )
    if _number(f_number) is not None and 0 < _number(f_number) < 0.5:
        issue(
            "implausible_f_number",
            "error",
            "F-number below 0.5 requires a separately documented pupil/conjugate model.",
        )
    if configuration and re.search(r"near|close|macro|finite", configuration, re.IGNORECASE):
        issue(
            "object_distance_unspecified",
            "warning",
            "Configuration suggests finite conjugates; tracing needs a numerical object distance.",
        )

    canonical = {
        "schema_version": SCHEMA_VERSION,
        "surfaces": normalized,
        "configuration": str(configuration or ""),
        "focal_length_mm": _number(focal_length_mm),
        "f_number": _number(f_number),
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    errors = sum(item["severity"] == "error" for item in issues)
    return {
        "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "validation_scope": "prescription_structure_only",
        "image_quality_validated": False,
        "valid": errors == 0,
        "prescription_sha256": fingerprint,
        "issues": issues,
        "metrics": {
            "surface_count": len(normalized),
            "powered_surface_count": powered_count,
            "stop_count": stop_count,
            "error_count": errors,
            "warning_count": len(issues) - errors,
        },
    }
