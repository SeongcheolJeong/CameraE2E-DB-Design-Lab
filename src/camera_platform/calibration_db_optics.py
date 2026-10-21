"""Immutable, explicitly selected DB optical PSF initializations.

Conversions retain their modeled source and approximation report. They initialize a
new campaign draft only; no existing campaign, active profile, or DB run is changed.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from copy import deepcopy

from .calibration_db import _current, _identity, _relative, source_selection
from .registry import IntegrityError, canonical, digest

ROUTE = "/api/v1/calibration-lab/db-optical-profiles"
COLLECTION = "calibration_db_optical_profiles"
SCHEMA = "camera_calibration_db_optical_profile_v1"
MAX_SOURCE_BYTES = 256 * 1024**2
MAX_DERIVED_BYTES = 16 * 1024**2


def _field_mapping(value):
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= 25:
        raise ValueError("field_mapping requires 1..25 explicit source/target field pairs")
    sources, targets, result = set(), set(), []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"source_field_id", "field_xy"}:
            raise ValueError("Each field mapping requires source_field_id and field_xy")
        identity = _identity(item["source_field_id"], "source field ID", r"[a-z][a-z0-9-]{0,39}")
        xy = item["field_xy"]
        if (
            not isinstance(xy, list)
            or len(xy) != 2
            or any(type(v) not in (int, float) or not -1 <= v <= 1 for v in xy)
        ):
            raise ValueError("Target field_xy requires finite normalized coordinates in [-1,1]")
        if identity in sources or tuple(xy) in targets:
            raise ValueError("Source and target field mappings must be unique")
        sources.add(identity)
        targets.add(tuple(xy))
        result.append({"source_field_id": identity, "field_xy": [float(v) for v in xy]})
    return result


def _freeze_sources(service, converted, source_context):
    """Bind each original to its exact top-level or declared ancestor manifest."""
    frozen = converted["frozen_files"]
    bindings = converted.get("manifest_bindings", {"": source_context["manifest_sha256"]})
    if not isinstance(frozen, dict) or not 1 <= len(frozen) <= 512:
        raise ValueError("Optical conversion requires 1..512 retained source files")
    if (
        not isinstance(bindings, dict)
        or not 1 <= len(bindings) <= 8
        or bindings.get("") != source_context["manifest_sha256"]
    ):
        raise IntegrityError("Optical source manifest bindings are invalid")
    manifests = {}
    ancestry = {
        item["run_id"]: item["manifest_sha256"]
        for item in converted["source_identity"].get("ancestors", [])
    }
    for prefix, checksum in bindings.items():
        if prefix:
            if not isinstance(prefix, str) or not prefix.endswith("/"):
                raise IntegrityError("Ancestor manifest prefixes require a trailing slash")
            _relative(prefix[:-1])
            parts = prefix[:-1].split("/")
            if len(parts) != 2 or parts[0] != "ancestors":
                raise IntegrityError("Ancestor source prefixes require ancestors/<run_id>/")
            _identity(parts[1], "ancestor run ID", r"run-[0-9a-f]{32}")
            if ancestry.get(parts[1]) != checksum:
                raise IntegrityError("Ancestor manifest lacks a matching declared source lineage")
        data = frozen.get(prefix + "manifest.json")
        if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != checksum:
            raise IntegrityError("Retained optical source manifest does not match its pin")
        manifest = json.loads(data)
        if manifest.get("schema") != "camera_reference_workshop_files_v1":
            raise IntegrityError("Unsupported optical source manifest schema")
        manifests[prefix] = manifest["files"]
    files, artifacts, total = {}, [], 0
    for relative, data in sorted(frozen.items()):
        _relative(relative)
        if not isinstance(data, bytes):
            raise IntegrityError("Optical originals must be retained as exact bytes")
        prefix = max((p for p in bindings if relative.startswith(p)), key=len)
        name = relative[len(prefix) :]
        checksum = hashlib.sha256(data).hexdigest()
        if name != "manifest.json" and manifests[prefix].get(name) != checksum:
            raise IntegrityError("Optical original differs from its bound manifest: " + relative)
        total += len(data)
        if total > MAX_SOURCE_BYTES:
            raise ValueError("Optical source bundle exceeds 256 MiB")
        artifact = service.registry.store_blob(
            data,
            "source/" + relative,
            mimetypes.guess_type(relative)[0] or "application/octet-stream",
        )
        artifacts.append(artifact)
        files[relative] = {"sha256": checksum, "bytes": len(data)}
    return files, artifacts, bindings


def _profile_patch(profile, converted):
    from .calibration_lab_contracts import conditions_spec
    from .calibration_lab_engine import validate_profile

    patch = deepcopy(converted.get("profile_patch", {}))
    if not isinstance(patch, dict) or set(patch) - {"blur", "conditions"}:
        raise IntegrityError("Optical converter may patch blur and known model conditions only")
    conditions = patch.get("conditions", {})
    if not isinstance(conditions, dict) or set(conditions) - {"aperture_f_number", "light_id"}:
        raise IntegrityError("Optical converter cannot infer camera or focus metadata")
    canonical(patch)  # Refuse nonfinite values even in explanatory fields.
    if converted["can_apply"] is not True:
        return patch, None
    if "blur" not in patch:
        raise IntegrityError("An applicable optical conversion requires its blur initialization")
    result = deepcopy(profile)
    result["blur"] = patch["blur"]
    result["conditions"] = conditions_spec({**profile["conditions"], **conditions})
    result["physical_validation"] = "not_established"
    return patch, validate_profile(result)


def create(service, payload):
    from . import calibration_db_optics_engine as engine
    from .calibration_lab import _unique_artifacts
    from .calibration_lab_contracts import conditions_spec
    from .calibration_lab_engine import validate_profile
    from .reference_workshop_job import _directory

    code = _current(service)
    if (
        not isinstance(payload, dict)
        or set(payload) - {"source", "profile", "field_mapping", "field_mapping_mode"}
        or not {"source", "profile"} <= payload.keys()
    ):
        raise ValueError(
            "Optical conversion requires source and explicit profile; field_mapping is optional"
        )
    if not isinstance(payload["profile"], dict):
        raise ValueError("Optical conversion requires an explicit complete calibration profile")
    original_profile_bytes = canonical(payload["profile"]).encode()
    if len(original_profile_bytes) > 2 * 1024**2:
        raise ValueError("Optical conversion input profile exceeds 2 MiB")
    profile = validate_profile(payload["profile"])
    profile["conditions"] = conditions_spec(profile["conditions"])
    mapping = _field_mapping(payload.get("field_mapping"))
    mapping_mode = payload.get("field_mapping_mode")
    if mapping_mode is not None and mapping_mode != "reference_model":
        raise ValueError("field_mapping_mode must be reference_model or omitted")
    if mapping_mode is not None and mapping is not None:
        raise ValueError("Choose reference_model or explicit field_mapping, not both")
    run, context = source_selection(service, payload["source"])
    normalized_profile_sha256 = digest(profile)
    converted = engine.convert_source(
        _directory(service, run["id"]),
        run["result"]["summary"],
        context["candidate_id"],
        profile,
        condition_id=context["condition_id"],
        expected_manifest_sha256=context["manifest_sha256"],
        field_mapping=mapping,
        field_mapping_mode=mapping_mode,
    )
    if digest(profile) != normalized_profile_sha256:
        raise IntegrityError("Optical converter changed the explicit input profile")
    identity = converted.get("source_identity")
    if not isinstance(identity, dict) or any(
        identity.get(key) != context[key]
        for key in ("run_id", "candidate_id", "condition_id", "manifest_sha256", "candidate_sha256")
    ):
        raise IntegrityError("Optical conversion differs from its selected DB source")
    if type(converted.get("can_apply")) is not bool or not isinstance(
        converted.get("report"), dict
    ):
        raise IntegrityError("Optical converter must return explicit applicability and diagnostics")
    patch, proposed = _profile_patch(profile, converted)
    source_files, artifacts, bindings = _freeze_sources(service, converted, context)
    if _current(service) != code:
        raise IntegrityError("Optical conversion code changed during preparation")
    again, again_context = source_selection(service, payload["source"])
    if again != run or again_context != context:
        raise IntegrityError("DB source changed while deriving optical initialization")
    context = {
        **context,
        "role": "frozen_db_optical_initialization",
        "profile_conversion": "effective_optical_psf_initialization",
        "blur_family": patch.get("blur", {}).get("family"),
    }
    report = {
        **converted["report"],
        "schema": "camera_calibration_db_optical_report_v1",
        "source_identity": converted["source_identity"],
        "source_binding": {k: v for k, v in context.items() if k != "candidate_snapshot"},
        "input_profile_sha256": hashlib.sha256(original_profile_bytes).hexdigest(),
        "profile_patch": patch,
        "conditions_mapping": converted.get("conditions_mapping", {}),
        "can_apply": converted["can_apply"],
        "status": "qualified" if converted["can_apply"] else "rejected",
        "engine_status": converted.get("status"),
        "evidence_kind": "model_derived_profile_initialization",
        "physical_validation": "not_established",
    }
    outputs = {
        "input-profile.json": original_profile_bytes,
        "conversion-report.json": canonical(report).encode(),
    }
    if proposed is not None:
        outputs["converted-profile.json"] = canonical(proposed).encode()
    for name, data in converted.get("derived_files", {}).items():
        _relative(name)
        if name in outputs or not isinstance(data, bytes):
            raise IntegrityError("Derived optical files must have unique names and byte content")
        outputs[name] = data
    if sum(len(data) for data in outputs.values()) > MAX_DERIVED_BYTES:
        raise ValueError("Optical conversion outputs exceed 16 MiB")
    output_files = {}
    for name, data in sorted(outputs.items()):
        artifact = service.registry.store_blob(
            data, name, mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        artifacts.append(artifact)
        output_files[name] = {"sha256": artifact["sha256"], "bytes": len(data)}
    data = {
        "schema": SCHEMA,
        "source_context": context,
        "source_identity": converted["source_identity"],
        "source_files": source_files,
        "manifest_bindings": bindings,
        "output_files": output_files,
        "artifacts": _unique_artifacts(artifacts),
        "input_profile_sha256": report["input_profile_sha256"],
        "normalized_input_profile_sha256": normalized_profile_sha256,
        "profile_patch": patch,
        "converted_profile_sha256": digest(proposed) if proposed is not None else None,
        "target_binding": {
            k: deepcopy(profile[k]) for k in ("native_shape", "pixel_pitch_um", "cfa")
        },
        "required_conditions": proposed["conditions"] if proposed is not None else None,
        "field_mapping": mapping,
        "field_mapping_mode": mapping_mode or ("explicit_sensor" if mapping else "center_only"),
        "resolved_field_mapping": [
            {"source_field_id": n.get("source_field_id"), "field_xy": n["field_xy"]}
            for n in patch.get("blur", {}).get("field_nodes", [])
        ],
        "conditions_mapping": report["conditions_mapping"],
        "report": report,
        "can_apply": report["can_apply"],
        "status": report["status"],
        "adapter_source_code": code,
        "physical_validation": "not_established",
    }
    return _present(service.registry.put(COLLECTION, data))


def _present(record):
    return {
        **record,
        "report_url": f"{ROUTE}/{record['id']}/files/conversion-report.json",
        "files_base": f"{ROUTE}/{record['id']}/files/",
    }


def get_record(service, identity):
    _identity(identity, "DB optical conversion ID")
    record = service.registry.get(COLLECTION, identity)
    if record.get("schema") != SCHEMA:
        raise IntegrityError("Invalid DB optical conversion schema")
    descriptors = {
        row["sha256"]: row
        for row in [*record["source_files"].values(), *record["output_files"].values()]
    }
    for checksum, row in descriptors.items():
        binding = service.registry.artifact_binding(
            checksum, owner_kind=COLLECTION, owner_id=identity
        )
        if binding["path"].stat().st_size != row["bytes"]:
            raise IntegrityError("DB optical conversion artifact size changed")
    return record


def get(service, identity):
    return _present(get_record(service, identity))


def campaign_conversion(service, campaign):
    identity = campaign.get("db_optical_profile_id")
    if identity is None:
        return None
    record = get_record(service, identity)
    if record["can_apply"] is not True:
        raise ValueError("Rejected optical conversion cannot initialize a campaign")
    profile = campaign["baseline_profile"]
    if profile["blur"] != record["profile_patch"].get("blur"):
        raise ValueError("Campaign baseline blur differs from the selected optical conversion")
    for key, value in record["target_binding"].items():
        if profile[key] != value:
            raise ValueError(f"Campaign {key} differs from the explicit optical conversion target")
    if profile["conditions"] != record["required_conditions"]:
        raise ValueError("Campaign conditions differ from the selected optical initialization")
    source = campaign.get("source")
    if source and any(
        source.get(k) != record["source_context"].get(k)
        for k in ("run_id", "candidate_id", "condition_id")
    ):
        raise ValueError("Campaign source conflicts with the selected optical conversion")
    return record


def _file(service, identity, relative):
    # A download verifies its exact immutable owner and selected blob. Campaign
    # admission and record retrieval still verify the complete retained bundle.
    _identity(identity, "DB optical conversion ID")
    record = service.registry.get(COLLECTION, identity)
    if record.get("schema") != SCHEMA:
        raise IntegrityError("Invalid DB optical conversion schema")
    row = record["output_files"].get(_relative(relative))
    if row is None:
        raise KeyError("File is not a retained optical conversion output")
    binding = service.registry.artifact_binding(
        row["sha256"], owner_kind=COLLECTION, owner_id=identity
    )
    if binding["path"].stat().st_size != row["bytes"]:
        raise IntegrityError("DB optical conversion artifact size changed")
    return binding["path"]


def install_routes(app, service):
    from fastapi.responses import FileResponse

    @app.post(ROUTE, status_code=201, tags=["calibration_lab"])
    def optical_create(payload: dict):
        return create(service, payload)

    @app.get(ROUTE + "/{identity}", tags=["calibration_lab"])
    def optical_get(identity: str):
        return get(service, identity)

    @app.get(ROUTE + "/{identity}/files/{relative:path}", tags=["calibration_lab"])
    def optical_file(identity: str, relative: str):
        return FileResponse(
            _file(service, identity, relative),
            media_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
        )
