"""Immutable canonical response blobs for native-site and joint-direction models."""

from __future__ import annotations

from .registry import canonical, digest

RESPONSE_FIELDS = {
    "native_pixel_response": "sensor",
    "directional_response": "lens",
    "charge_collection": "sensor",
}


def store_responses(service, data):
    """Persist normalized response fields after model validation, before publication."""
    return {
        field: service.registry.store_blob(
            canonical(data["payload"][field]).encode(), field + ".json", "application/json"
        )
        for field in RESPONSE_FIELDS
        if field in data["payload"]
    }


def verify_models(service, models):
    """Return verified response descriptors; legacy model snapshots are unchanged.

    New response models must match their exact current immutable registry record,
    response-field canonical bytes and server-created artifact descriptors.
    Directional lenses additionally retain actual Factory authority and evidence.
    """
    artifacts = []
    selected = list(models.values() if isinstance(models, dict) else models)
    for model in selected:
        payload = model.get("payload", {})
        fields = [field for field in RESPONSE_FIELDS if field in payload]
        if (model.get("method_and_version") == "native_source_bundle_import:1"
                and "native_pixel_response" not in fields):
            raise ValueError("Native source bundle model lacks its pinned native response")
        if not fields:
            continue
        native = payload.get("native_pixel_response", {})
        if ("charge_collection" in payload
                or (isinstance(native, dict)
                    and native.get("schema") == "camera_native_generation_response_v1")):
            from .charge_collection import validate_pair

            validate_pair(native, payload.get("charge_collection"))
        if service.registry.get("models", model["id"]) != model:
            raise ValueError("Native response model differs from its frozen registry record")
        recorded = model.get("response_artifacts")
        if not isinstance(recorded, dict) or set(recorded) != set(fields):
            raise ValueError("Native response requires its exact server-created response artifacts")
        for field in fields:
            if model.get("kind") != RESPONSE_FIELDS[field]:
                raise ValueError("Native response field is attached to the wrong model role")
            if field == "native_pixel_response":
                from .native_pixel_response import validate_response

                normalized = validate_response(payload[field])
                if (
                    payload.get("wavelength_nm") != normalized["wavelength_nm"]
                    or payload.get("pixel_pitch_um") != normalized["pixel_pitch_um"]
                ):
                    raise ValueError("Native response differs from model wavelength or pitch")
            elif field == "charge_collection":
                from .charge_collection import validate_collection

                normalized = validate_collection(payload[field])
            else:
                from .lens_direction_response import validate_directional_response

                normalized = validate_directional_response(payload[field])
                if (
                    payload.get("wavelength_nm") != normalized["wavelength_nm"]
                    or payload.get("field_radius") != normalized["field_radius"]
                    or payload.get("field_reference_radius_mm")
                    != normalized["field_reference_radius_mm"]
                ):
                    raise ValueError("Directional response differs from model axes")
            content = canonical(normalized).encode()
            sha = digest(normalized)
            expected = {
                "id": sha,
                "name": field + ".json",
                "sha256": sha,
                "bytes": len(content),
                "media_type": "application/json",
                "url": f"/api/v1/artifacts/{sha}",
            }
            if canonical(payload[field]).encode() != content or recorded[field] != expected:
                raise ValueError(
                    "Response artifact does not match its exact normalized model field"
                )
            if service.registry.blob_path(sha).read_bytes() != content:
                raise ValueError("Response canonical bytes differ from the preserved model field")
            artifacts.append(dict(expected))
        if "directional_response" in fields:
            from .factory import verify_model_link, verify_psf_model_link

            if model.get("psf_factory_job_id"):
                verify_psf_model_link(service, model)
                if len(model.get("parents", [])) != 1:
                    raise ValueError("Directional PSF model requires its single Factory parent")
                parent = service.registry.get("models", model["parents"][0])
                verify_model_link(service, parent)
            elif model.get("factory_job_id"):
                verify_model_link(service, model)
            else:
                raise ValueError("Directional response requires preserved Factory authority")
        if model.get("method_and_version") == "native_source_bundle_import:1":
            from .native_response_publication import verify_native_source_model

            verify_native_source_model(service, model, selected)
    return artifacts
