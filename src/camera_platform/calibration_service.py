"""Resolve registered training evidence and persist proposed calibration as a new model."""

from __future__ import annotations

import copy
import uuid

from .contracts import CalibrationCreate, ModelCreate
from .registry import ConflictError


def propose_calibration(service, payload):
    from .calibration import propose_angular_calibration
    from .measurement import observation_sampling_weights
    from .observations import capture_content_ids, verify_capture_measurement

    data = CalibrationCreate.model_validate(payload).model_dump()
    if len(set(data["measurement_ids"])) != len(data["measurement_ids"]):
        raise ValueError("Training measurement IDs must be unique")
    base = service.registry.get("models", data["base_sensor_model_id"])
    if base["kind"] != "sensor":
        raise ValueError("This calibration adapter adjusts a sensor angular response only")
    observations, evidence, content_ids = [], [], set()
    for identifier in data["measurement_ids"]:
        measurement = service.registry.get("measurements", identifier)
        service.registry.blob_path(measurement["original_artifact"]["sha256"])
        assembly = service.registry.get("assemblies", measurement["assembly_id"])
        models = [service.registry.get("models", i) for i in assembly["model_ids"]]
        sensor = next(m for m in models if m["kind"] == "sensor")
        lens = next(m for m in models if m["kind"] == "lens")
        test = service.registry.get("tests", measurement["test_id"])
        if sensor["id"] != base["id"]:
            raise ValueError("Training capture must reference the exact base sensor model")
        if measurement["conditions"] != test["conditions"]:
            raise ValueError("Training measurement conditions differ from its registered test")
        capture_id = measurement.get("raw_capture_id")
        if not capture_id:
            raise ValueError(
                "Calibration requires a registered RAW capture and frozen observation operator"
            )
        capture = verify_capture_measurement(service, measurement)
        content_ids.update(capture_content_ids(capture))
        if (test.get("observation_method") or {}).get("id") != "raw_roi_observation_v1":
            raise ValueError("Angular calibration requires the matched RAW ROI observation adapter")
        if capture["observation_method"] != test.get("observation_method"):
            raise ValueError("Capture and test use different observation procedures")
        if test["observation_method"].get("dark_correction") != "mean_dark_frame":
            raise ValueError(
                "Angular calibration requires matched dark frames to isolate photo-signal ratios"
            )
        sampling = observation_sampling_weights(
            test["observation_specification"],
            test["observation_shape"],
            lens["payload"]["field_radius"],
        )
        saturation = measurement["instrument"].get("saturation_diagnostics")
        observations.append(
            {
                "measurement": measurement,
                "lens": lens["payload"],
                "stimulus": test["stimulus"],
                "conditions": test["conditions"],
                "sampling": sampling,
                "linear_unsaturated": bool(saturation and saturation.get("linear_unsaturated")),
                "saturation_diagnostics": saturation,
            }
        )
        evidence.append(
            {
                "measurement_id": identifier,
                "measurement_hash": measurement["content_hash"],
                "capture_id": capture_id,
                "capture_hash": capture["content_hash"],
                "test_id": test["id"],
                "test_hash": test["content_hash"],
                "lens_model_id": lens["id"],
                "lens_hash": lens["content_hash"],
            }
        )
    proposal = propose_angular_calibration(base, observations, data["parameter_bounds"])
    proposal["training_content_ids"] = sorted(content_ids)
    if proposal["status"] == "PROPOSED":
        proposal["calibration_dataset_ids"] = sorted(
            set(proposal["calibration_dataset_ids"]) | content_ids
        )
    if proposal["status"] != "PROPOSED":
        return service.registry.put(
            "calibrations",
            {
                **data,
                **proposal,
                "training_evidence": evidence,
                "candidate_model_id": None,
                "physical_validation": "not_established",
            },
        )
    report_id = "calibration-" + uuid.uuid4().hex
    model_payload = {k: copy.deepcopy(v) for k, v in base.items() if k in ModelCreate.model_fields}
    model_payload.update(
        name=data["name"],
        revision=data["revision"],
        source_kind="estimated",
        source_uri="calibration://" + report_id,
        method_and_version="bounded_angular_scale_fit_v1",
        parents=[base["id"]],
        payload=proposal["candidate_payload"],
        uncertainty=proposal["uncertainty"],
        calibration_dataset_ids=proposal["calibration_dataset_ids"],
    )
    changed_path = "sensor_angular_response.relative_response"
    if changed_path in model_payload.get("parameter_uncertainty", {}):
        # Fitted response values need a newly established bound; the parent
        # parameter interval cannot be shifted onto a newly fitted model.
        model_payload["parameter_uncertainty"][changed_path] = {
            "kind": "unknown",
            "unit": "1",
            "value": None,
            "source_kind": "estimated",
            "source_uri": "calibration://" + report_id,
            "method_and_version": "bounded_angular_scale_fit_v1",
            "reason": "Fitted angular response has no quantified parameter bound",
        }
    model = service.create_model(model_payload)
    return service.registry.put(
        "calibrations",
        {
            **data,
            **proposal,
            "training_evidence": evidence,
            "candidate_model_id": model["id"],
            "candidate_model_hash": model["content_hash"],
            "physical_validation": "not_established",
        },
        identity=report_id,
    )


def calibrated_assembly_equivalence(service, prediction_assembly_id, measured_assembly_id):
    """Explicitly allow only a registered calibration descendant on otherwise identical hardware."""
    target = service.registry.get("assemblies", prediction_assembly_id)
    original = service.registry.get("assemblies", measured_assembly_id)

    def by_role(assembly):
        return {
            m["kind"]: m for m in (service.registry.get("models", i) for i in assembly["model_ids"])
        }

    target_models, original_models = by_role(target), by_role(original)
    for role in ("lens", "isp"):
        if target_models[role]["id"] != original_models[role]["id"]:
            raise ValueError("Calibrated comparison must retain the same lens and ISP revisions")
    for key in ("raw_format", "conditions", "spectral_range"):
        if target[key] != original[key]:
            raise ValueError("Calibrated comparison changed the camera configuration contract")
    new_sensor, old_sensor = target_models["sensor"], original_models["sensor"]
    if new_sensor["asset_id"] != old_sensor["asset_id"]:
        raise ValueError("Calibrated comparison changed the physical sensor asset")
    if new_sensor["id"] == old_sensor["id"]:
        raise ValueError("Use the original assembly when no calibrated model revision was changed")
    pending, seen = list(new_sensor["parents"]), set()
    while pending:
        identity = pending.pop()
        if identity in seen:
            continue
        seen.add(identity)
        parent = service.registry.get("models", identity)
        pending.extend(parent["parents"])
    if old_sensor["id"] not in seen:
        raise ValueError("Prediction model is not a descendant of the measured camera's model")
    reports = [
        r
        for r in service.registry.list("calibrations")
        if r.get("candidate_model_id") == new_sensor["id"] and r["status"] == "PROPOSED"
    ]
    if not reports:
        raise ConflictError("No registered calibration proposal produced this sensor revision")
    return {
        "scope": "same lens/ISP/configuration; calibrated sensor model only",
        "measured_assembly_id": original["id"],
        "prediction_assembly_id": target["id"],
        "original_sensor_model_id": old_sensor["id"],
        "candidate_sensor_model_id": new_sensor["id"],
        "calibration_id": reports[0]["id"],
        "physical_identity": "measured serial remains in measurement",
    }
