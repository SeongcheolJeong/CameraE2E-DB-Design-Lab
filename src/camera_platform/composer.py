"""Preflight physical and data-contract compatibility of pinned camera models."""

from __future__ import annotations

import math
from typing import Any

from .validation import check_domain

RAW_FIELDS = ("cfa", "bit_depth", "layout", "packing", "black_level", "hdr_streams")


def _issue(code: str, message: str, path: str, severity: str = "unknown") -> dict:
    return {"code": code, "message": message, "path": path, "severity": severity}


def _number(value: Any) -> bool:
    try:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    except OverflowError:
        return False


def _range(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _number(value.get("min"))
        and _number(value.get("max"))
        and value["min"] <= value["max"]
        and isinstance(value.get("unit"), str)
        and bool(value["unit"])
    )


def _coverage(required: Any, available: Any, path: str, code: str) -> list:
    if required is None or available is None:
        return [
            _issue(f"missing_{code}", "Both requested and available ranges must be declared.", path)
        ]
    if not _range(required) or not _range(available):
        return [
            _issue(
                f"invalid_{code}",
                "Ranges need finite ordered min/max bounds and a unit.",
                path,
                "error",
            )
        ]
    allowed_units = {"spectral": {"m", "cm", "mm", "um", "nm"}, "angular": {"deg", "rad"}}
    if code in allowed_units and (
        required["unit"] not in allowed_units[code] or available["unit"] not in allowed_units[code]
    ):
        return [
            _issue(
                f"invalid_{code}_unit",
                "Range unit does not express the required physical dimension.",
                path,
                "error",
            )
        ]
    if required["unit"] != available["unit"]:
        return [
            _issue(
                f"{code}_unit_mismatch",
                "Range units differ; convert explicitly upstream.",
                path,
                "error",
            )
        ]
    if required["min"] < available["min"] or required["max"] > available["max"]:
        return [
            _issue(
                f"{code}_coverage",
                "The requested range is not covered by this model.",
                path,
                "out_of_domain",
            )
        ]
    return []


def _size(properties: dict, field: str, path: str) -> tuple[Any, list]:
    value = properties.get(field)
    if value is None:
        return None, [_issue("missing_geometry", f"Declare {field}.", f"{path}.{field}")]
    if not (
        isinstance(value, dict)
        and _number(value.get("value"))
        and value["value"] > 0
        and isinstance(value.get("unit"), str)
        and value.get("unit") in {"m", "cm", "mm", "um", "nm"}
    ):
        return None, [
            _issue(
                "invalid_geometry",
                "Geometry requires a positive finite value and unit.",
                f"{path}.{field}",
                "error",
            )
        ]
    return value, []


def _raw_contract(value: Any, path: str) -> list:
    if not isinstance(value, dict):
        return [_issue("missing_raw_format", "An explicit RAW stream contract is required.", path)]
    issues = []
    for field in RAW_FIELDS:
        if field not in value or value[field] is None:
            issues.append(
                _issue("missing_raw_field", f"RAW format is missing {field}.", f"{path}.{field}")
            )
    depth = value.get("bit_depth")
    if depth is not None and (
        not isinstance(depth, int) or isinstance(depth, bool) or depth < 1 or depth > 64
    ):
        issues.append(
            _issue(
                "invalid_raw_depth", "RAW bit depth must be an integer from 1 to 64.", path, "error"
            )
        )
    black = value.get("black_level")
    if black is not None and (
        not _number(black)
        or black < 0
        or (isinstance(depth, int) and 1 <= depth <= 64 and black >= 2**depth)
    ):
        issues.append(
            _issue(
                "invalid_black_level",
                "Black level must lie inside the declared code range.",
                path,
                "error",
            )
        )
    for field in ("cfa", "layout", "packing"):
        if field in value and not (isinstance(value[field], str) and value[field]):
            issues.append(
                _issue(
                    "invalid_raw_field",
                    f"RAW {field} must be a nonempty identity string.",
                    f"{path}.{field}",
                    "error",
                )
            )
    streams = value.get("hdr_streams")
    if streams is not None and not (
        (isinstance(streams, int) and not isinstance(streams, bool) and streams > 0)
        or (
            isinstance(streams, list)
            and streams
            and all(isinstance(item, str) and item for item in streams)
            and len(streams) == len(set(streams))
        )
    ):
        issues.append(
            _issue(
                "invalid_hdr_streams",
                "HDR streams must be a positive count or a unique ordered stream identity list.",
                path,
                "error",
            )
        )
    return issues


def _native_response_issues(lens, sensor):
    """Require joint incident directions before accepting a native-site Sensor."""
    payload = sensor.get("payload", {})
    if "native_pixel_response" not in payload:
        if "charge_collection" in payload:
            return [_issue(
                "native_response_mismatch", "Charge collection requires its explicit native "
                "generation response; collected QE cannot be collected again.",
                "models.sensor.payload", "error",
            )]
        return []
    try:
        import numpy as np

        from .native_pixel_response import validate_response
        from .runtime import sensor_response_fields

        if sensor.get("method_and_version") == "native_source_bundle_import:1":
            source = sensor.get("properties", {}).get("native_response_bundle", {})
            if (
                source.get("qualified_lens_model_id") != lens.get("id")
                or source.get("qualified_lens_model_hash") != lens.get("content_hash")
            ):
                raise ValueError(
                    "Imported native response is bound to another exact Factory Lens; "
                    "review and publish a new Sensor model for this Lens"
                )
        sensor_response_fields(payload)
        response = validate_response(payload["native_pixel_response"])
        if response["schema"] == "camera_native_generation_response_v1":
            from .charge_collection import validate_pair

            validate_pair(response, payload.get("charge_collection"))
            expected_effects = {
                "sensor_charge_generation", "sensor_charge_collection",
                "sensor_angular_response", "mean_dark_charge", "readout_clipping",
            }
            effects = sensor.get("included_effects", [])
            if set(effects) != expected_effects or len(effects) != len(expected_effects):
                raise ValueError("Native generation Sensor must declare generation and "
                                 "collection effects exactly once, without effective QE")
        if (payload.get("wavelength_nm") != response["wavelength_nm"]
                or payload.get("pixel_pitch_um") != response["pixel_pitch_um"]):
            raise ValueError("Native Sensor response must retain exact wavelength and pixel pitch")
        lp = lens.get("payload", {})
        if "directional_response" not in lp:
            raise ValueError(
                "Native Sensor requires joint Lens directions; theta marginals lose phi"
            )
        from .lens_direction_response import validate_directional_response

        joint = validate_directional_response(lp["directional_response"])
        if (joint["wavelength_nm"] != response["wavelength_nm"]
                or lp.get("wavelength_nm") != response["wavelength_nm"]
                or lp.get("field_radius") != joint["field_radius"]
                or lp.get("field_reference_radius_mm") != joint["field_reference_radius_mm"]):
            raise ValueError("Joint Lens axes must match the native Sensor and Lens payload")
        if (joint["reference_plane"] != response["reference_plane"]
                or joint["direction_convention"] != response["direction_convention"]):
            raise ValueError("Joint Lens and native Sensor direction/reference plane differ")
        power = np.asarray(joint["conditional_weights"])
        directions = np.asarray(joint["directions_xyz"])
        active = np.any(power > 0, axis=0)
        angles = np.degrees(np.arccos(np.clip(directions[..., 2], -1, 1)))
        if np.any(angles[active] > response["theta_deg"][-1] + 1e-10):
            raise ValueError("Joint Lens active rays exceed native Sensor theta coverage")
        if "psf" in lp and lp["psf"].get("sample_pitch_um") != response["pixel_pitch_um"]:
            raise ValueError("PSF sampling pitch must match the native Sensor pitch")
    except (KeyError, TypeError, ValueError) as exc:
        return [_issue("native_response_mismatch", str(exc), "models.sensor.payload", "error")]
    return []


def check_assembly(assembly: dict, models: dict | list) -> dict:
    """Check a camera before execution; unknown evidence is never compatible.

    Assembly pins ``model_ids`` and declares ``conditions`` and a ``spectral_range``.
    Models use ``kind`` (lens/sensor/isp), ``included_effects``, ``valid_domain`` and
    explicit optical ``reference_plane``. Lens/sensor properties declare image
    circle diameter, active width/height and incident/accepted angle ranges with
    units. Sensor raw_format must match ISP input_raw_format exactly. All declared
    models are checked for duplicate effects, so a composed response cannot be
    silently applied alongside the individual effects it already includes.
    """
    issues: list[dict] = []
    ids = assembly.get("model_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or not all(isinstance(item, str) and item for item in ids)
    ):
        issues.append(
            _issue(
                "missing_model_ids",
                "Assembly must pin nonempty model revision IDs.",
                "model_ids",
                "error",
            )
        )
        ids = []
    if len(ids) != len(set(ids)):
        issues.append(
            _issue(
                "duplicate_model",
                "A model revision is applied more than once.",
                "model_ids",
                "error",
            )
        )
    model_map = (
        models
        if isinstance(models, dict)
        else {
            item["id"]: item
            for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    )
    selected = []
    for model_id in ids:
        model = model_map.get(model_id)
        if not isinstance(model, dict):
            issues.append(
                _issue(
                    "missing_model",
                    f"Pinned model {model_id} is unavailable.",
                    "model_ids",
                    "error",
                )
            )
        else:
            selected.append((model_id, model))
    conditions = assembly.get("conditions")
    if not isinstance(conditions, dict):
        issues.append(
            _issue("missing_conditions", "Declare the intended operating conditions.", "conditions")
        )
        conditions = {}
    effect_owners: dict[str, list] = {}
    by_kind: dict[str, list] = {}
    for model_id, model in selected:
        path = f"models.{model_id}"
        kind = model.get("kind")
        if kind == "isp" and model.get("payload", {}).get("schema") == "camera_isp_profile_v2":
            issues.append(_issue(
                "isp_replay_only", "Advanced ISP v2 processes retained native RAW; "
                "camera acquisition with this backend is not yet supported.", path, "error",
            ))
        if (kind == "isp" and model.get("payload", {}).get("schema")
                == "camera_retained_code_isp_profile_v1"):
            issues.append(_issue(
                "isp_replay_only", "Retained code ISP requires an explicit saved readout "
                "and original row event history; camera acquisition is unsupported.", path, "error",
            ))
        if isinstance(kind, str):
            by_kind.setdefault(kind, []).append(model)
        else:
            issues.append(
                _issue("missing_model_kind", "Model role is not declared.", f"{path}.kind")
            )
        effects = model.get("included_effects")
        if not isinstance(effects, list):
            issues.append(
                _issue(
                    "missing_effects",
                    "Declare the effects this model already includes.",
                    f"{path}.included_effects",
                )
            )
        elif not all(isinstance(effect, str) and effect for effect in effects):
            issues.append(
                _issue(
                    "invalid_effects",
                    "Effects must be nonempty identity strings.",
                    f"{path}.included_effects",
                    "error",
                )
            )
        else:
            for effect in effects:
                effect_owners.setdefault(effect, []).append(model_id)
        issues.extend(
            check_domain(conditions, model.get("valid_domain"), path=f"{path}.valid_domain")
        )
        from .model_conditions import fixed_factory_conditions

        application = fixed_factory_conditions(model)
        if model.get("psf_factory_job_id") and "f_number" in conditions:
            pupil = model.get("properties", {}).get("psf_pupil_f_number")
            if pupil is None:
                # Old immutable candidates did not promote the pupil declaration.
                # An explicit new aperture claim needs a newly published revision.
                pupil = (model.get("factory_application_conditions") or {}).get("f_number")
            if pupil is None or conditions["f_number"] != pupil:
                issues.append(
                    _issue(
                        "psf_pupil_condition_mismatch",
                        "Requested f_number must match the preserved PSF pupil declaration; "
                        "publish a new candidate if the legacy model has no explicit declaration.",
                        f"{path}.properties.psf_pupil_f_number",
                        "error",
                    )
                )
        if application is not None:
            for name, expected in application.items():
                if conditions.get(name) != expected:
                    issues.append(
                        _issue(
                            "factory_application_condition_mismatch",
                            f"Factory draft application condition {name} "
                            "differs from this assembly.",
                            f"{path}.factory_application_conditions.{name}",
                            "error",
                        )
                    )
        if kind in {"lens", "sensor", "scene", "material", "light"}:
            issues.extend(
                _coverage(
                    assembly.get("spectral_range"),
                    model.get("spectral_range"),
                    f"{path}.spectral_range",
                    "spectral",
                )
            )
    # A quantitative operating condition with no model owner is not evidence of
    # applicability. Text identities (for example light_id) are provenance.
    # A fixed Lens f-number is an exact property binding, even in legacy scene
    # cameras whose numeric valid_domain predates aperture as an axis.
    declared_axes = {
        name
        for _, model in selected
        if isinstance(model.get("valid_domain"), dict)
        for name in model["valid_domain"]
    }
    lenses_with_stop = [model for _, model in selected
                        if model.get("kind") == "lens"
                        and "f_number" in model.get("properties", {})]
    if "f_number" in conditions and lenses_with_stop:
        declared_axes.add("f_number")
        if len(lenses_with_stop) == 1:
            stop = lenses_with_stop[0]["properties"]["f_number"]
            if (not isinstance(stop, dict) or not _number(stop.get("value"))
                    or stop["value"] <= 0 or stop.get("unit") != "1"):
                issues.append(_issue(
                    "invalid_lens_f_number",
                    "Fixed Lens f_number must be a positive finite value with unit 1.",
                    "lens.properties.f_number", "error",
                ))
            elif conditions["f_number"] != stop:
                issues.append(_issue(
                    "lens_f_number_mismatch",
                    "Requested f_number differs from the pinned fixed Lens property.",
                    "conditions.f_number", "error",
                ))
    for name, value in conditions.items():
        if (name not in declared_axes and isinstance(value, dict)
                and "value" in value and not isinstance(value["value"], str)):
            issues.append(_issue(
                "unclaimed_condition_axis",
                f"No pinned model declares applicability for quantitative condition {name}.",
                f"conditions.{name}",
            ))
    for effect, owners in effect_owners.items():
        if len(owners) > 1:
            issues.append(
                _issue(
                    "duplicate_effect",
                    f"{effect} is counted by {', '.join(owners)}.",
                    "included_effects",
                    "error",
                )
            )
    for kind in ("lens", "sensor"):
        if not by_kind.get(kind):
            issues.append(
                _issue("missing_component", f"A {kind} model is required.", f"models.{kind}")
            )
        elif len(by_kind[kind]) > 1:
            issues.append(
                _issue(
                    "ambiguous_component",
                    f"Multiple {kind} models need an explicit "
                    "composition adapter; the flat assembly cannot select one.",
                    f"models.{kind}",
                    "error",
                )
            )
    if len(by_kind.get("isp", [])) > 1:
        issues.append(
            _issue(
                "ambiguous_component",
                "Multiple ISP models need an explicit processing graph.",
                "models.isp",
                "error",
            )
        )

    lenses, sensors = by_kind.get("lens", []), by_kind.get("sensor", [])
    if len(lenses) == 1 and len(sensors) == 1:
        lens, sensor = lenses[0], sensors[0]
        issues.extend(_native_response_issues(lens, sensor))
        lp = lens.get("properties") if isinstance(lens.get("properties"), dict) else {}
        sp = sensor.get("properties") if isinstance(sensor.get("properties"), dict) else {}
        circle, found = _size(lp, "image_circle_diameter", "lens.properties")
        issues.extend(found)
        width, found = _size(sp, "active_width", "sensor.properties")
        issues.extend(found)
        height, found = _size(sp, "active_height", "sensor.properties")
        issues.extend(found)
        if circle and width and height:
            if len({circle["unit"], width["unit"], height["unit"]}) != 1:
                issues.append(
                    _issue(
                        "geometry_unit_mismatch",
                        "Image circle and sensor dimensions need the same explicit unit.",
                        "properties",
                        "error",
                    )
                )
            elif math.hypot(width["value"], height["value"]) > circle["value"]:
                issues.append(
                    _issue(
                        "image_circle_too_small",
                        "Sensor diagonal exceeds the lens image circle diameter.",
                        "lens.properties.image_circle_diameter",
                        "error",
                    )
                )
        lens_plane = lens.get("output_reference_plane", lens.get("reference_plane"))
        sensor_plane = sensor.get("input_reference_plane", sensor.get("reference_plane"))
        if not lens_plane or not sensor_plane:
            issues.append(
                _issue(
                    "missing_reference_plane",
                    "Lens output and sensor optical input planes must be explicit.",
                    "reference_plane",
                )
            )
        elif lens_plane != sensor_plane:
            issues.append(
                _issue(
                    "reference_plane_mismatch",
                    "Lens output and sensor input refer to different physical planes.",
                    "reference_plane",
                    "error",
                )
            )
        issues.extend(
            _coverage(
                lp.get("incident_angle_range"),
                sp.get("accepted_angle_range"),
                "sensor.properties.accepted_angle_range",
                "angular",
            )
        )
        if assembly.get("evaluation_intent") == "lens_sensor_angular_matching":
            if lens.get("angular_representation") != "distribution":
                issues.append(
                    _issue(
                        "missing_angular_distribution",
                        "Lens-sensor angular matching requires "
                        "a weighted incident-angle distribution; a chief ray alone is an "
                        "explicit lower-fidelity approximation.",
                        "lens.angular_representation",
                    )
                )

    if len(sensors) == 1:
        raw = sensors[0].get("raw_format")
        issues.extend(_raw_contract(raw, "sensor.raw_format"))
        raw_comparisons = []
        if "raw_format" in assembly:
            raw_comparisons.append(("assembly.raw_format", assembly["raw_format"]))
        if len(by_kind.get("isp", [])) == 1:
            raw_comparisons.append(
                ("isp.input_raw_format", by_kind["isp"][0].get("input_raw_format"))
            )
        for path, expected in raw_comparisons:
            issues.extend(_raw_contract(expected, path))
            if isinstance(raw, dict) and isinstance(expected, dict):
                for field in RAW_FIELDS:
                    if field in raw and field in expected and raw[field] != expected[field]:
                        issues.append(
                            _issue(
                                "raw_format_mismatch",
                                f"Sensor and consumer disagree on RAW {field}.",
                                f"{path}.{field}",
                                "error",
                            )
                        )

    severities = {issue["severity"] for issue in issues}
    status = (
        "FAIL"
        if "error" in severities
        else "OUT_OF_DOMAIN"
        if "out_of_domain" in severities
        else "INCONCLUSIVE"
        if "unknown" in severities
        else "PASS"
    )
    return {
        "schema_version": "1.0",
        "assembly_id": assembly.get("id"),
        "status": status,
        "compatible": status == "PASS",
        "model_ids": ids,
        "issues": issues,
        "included_effects": sorted(effect_owners),
        "claim": "Preflight checks cover declared contracts only; physical accuracy requires "
        "independent measurement validation.",
    }
