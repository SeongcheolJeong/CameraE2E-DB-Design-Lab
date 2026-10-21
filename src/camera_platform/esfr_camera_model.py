"""Explicit physical-camera identity -> immutable DB numerical model binding.

The legacy design-reference link remains readable. New model workflows require
this contract and actually replay its sampled four-site PSFs. Spatial assignment
is declared, never inferred from a DB virtual probe or a camera product name.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import mimetypes
import re

import numpy as np

from . import calibration_lab as lab, esfr_lab
from .registry import ConflictError, IntegrityError, digest

KIND = "camera_esfr_camera_models"
ROOT = esfr_lab.ROOT + "/camera-models"
CONTEXT = ("camera_id", "source_kind", "raw_format", "geometry", "pixel_pitch_um", "conditions", "isp", "sampling", "raw_processing")


def contract(metadata):
    return {k: deepcopy(metadata.get(k)) for k in CONTEXT}


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("Declare " + name)
    return value.strip()


def identity_spec(value, camera_id):
    keys = {"camera_id", "manufacturer", "model", "serial", "lens", "sensor", "mode"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Camera identity requires camera_id/manufacturer/model/serial/lens/sensor/mode")
    result = {k: _text(value[k], "physical camera " + k) for k in sorted(keys)}
    if result["camera_id"] != camera_id:
        raise ValueError("Physical camera_id differs from uploaded captures")
    return result


def spatial_spec(value, available):
    if not isinstance(value, dict) or value.get("acknowledged") is not True:
        raise ValueError("Explicitly acknowledge the source-field spatial assumption")
    if value.get("mode") == "uniform_selected_db_field":
        if set(value) != {"mode", "source_field_id", "acknowledged"} or value["source_field_id"] not in available:
            raise ValueError("Select an available DB PSF field")
    elif value.get("mode") == "explicit_roi_assignment":
        from .esfr_chart import chart_rois
        expected = {r["id"] for r in chart_rois() if r["kind"] == "edge"}
        mapping = value.get("roi_fields")
        if set(value) != {"mode", "roi_fields", "acknowledged"} or not isinstance(mapping, dict) or set(mapping) != expected:
            raise ValueError("Explicit assignment requires all 112 ROI IDs exactly once")
        if not set(mapping.values()) <= set(available):
            raise ValueError("ROI assignment references unavailable DB fields")
    else:
        raise ValueError("Unsupported PSF spatial assignment; no inferred sensor positions")
    return deepcopy(value)


def source_options(service):
    from .reference_workshop_job import get
    rows = []
    for stub in service.registry.list_runs(job_kinds={"reference_workshop"}):
        if stub.get("plugin_id") != "camera_db_design_study_v1" or stub.get("status") != "succeeded":
            continue
        run = get(service, stub["id"])
        summary = run["result"]["summary"]
        selections = summary.get("aperture_conditions", {}).get("conditions", []) if summary.get("comparison_mode") == "aperture_conditions" else [summary]
        for selection in selections:
            for c in selection.get("candidates", []):
                if c.get("status") != "succeeded":
                    continue
                source = {"run_id": run["id"], "candidate_id": c["id"]}
                if selection is not summary:
                    source["condition_id"] = selection["id"]
                ids = c.get("source_identities", {})
                rows.append({"source": source, "label": " · ".join([ids.get("lens", {}).get("label", c.get("lens_key", "lens")), ids.get("sensor", {}).get("label", c.get("sensor_key", "sensor")), run["id"][-8:], c["id"], source.get("condition_id", "")]).strip(" ·"),
                    "lens": ids.get("lens"), "sensor": ids.get("sensor")})
    return rows


def _artifact(service, model, relative):
    row = model["files"][relative]
    binding = service.registry.artifact_binding(row["sha256"], owner_kind=KIND, owner_id=model["id"])
    data = binding["path"].read_bytes()
    if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise IntegrityError("Selected camera model bytes changed: " + relative)
    return data


def get(service, identity):
    if not isinstance(identity, str) or not re.fullmatch(r"esfr-camera-[0-9a-f]{32}", identity):
        raise ValueError("Invalid camera model identity")
    row = service.registry.get(KIND, identity)
    if digest({k: v for k, v in row.items() if k != "content_hash"}) != row["content_hash"]:
        raise IntegrityError("Selected camera model record changed")
    return row


def kernels(service, model):
    with np.load(io.BytesIO(_artifact(service, model, "model/broadband-kernels.npz")), allow_pickle=False) as z:
        result = {k.removesuffix("_rgba_sites"): z[k] for k in z.files if k.endswith("_rgba_sites")}
    for array in result.values():
        if array.ndim != 3 or array.shape[0] != 4 or not np.isfinite(array).all() or np.any(array < 0) or not np.allclose(array.sum(axis=(1, 2)), 1, atol=1e-10):
            raise IntegrityError("Selected camera model has invalid four-site PSFs")
    spatial_spec(model["spatial"], result)
    return result


def check_captures(service, identity, captures, source):
    model = get(service, identity)
    if model["source"] != source:
        raise ValueError("Selected camera model belongs to another DB run/candidate/condition")
    for c in captures:
        if contract(c["metadata"]) != model["input_contract"]:
            raise ValueError("Selected camera model and uploaded camera/format/geometry/conditions/ISP differ")
    # Binding may be old, but exact source artifacts are verified on every use.
    kernels(service, model)
    return model


def create(service, payload):
    from .calibration_db import source_selection
    from . import calibration_db_engine as electrical, calibration_db_optics_engine as optics
    from .reference_workshop_job import _directory
    from .esfr_bridge import baseline_profile
    if not isinstance(payload, dict) or set(payload) != {"source", "development_capture_id", "camera", "spatial"}:
        raise ValueError("Camera model requires source, development_capture_id, camera and spatial")
    code = lab._source_code()
    if service.loaded_environment != code:
        raise ConflictError("Restart current model adapter before registering")
    capture = esfr_lab.capture(service, payload["development_capture_id"])
    meta = capture["metadata"]
    if meta["role"] != "development":
        raise ValueError("Camera model must be frozen from development, never validation")
    physical = identity_spec(payload["camera"], meta["camera_id"])
    if physical["mode"] != meta["conditions"].get("mode_id"):
        raise ValueError("Physical camera mode differs from capture mode_id")
    run, context = source_selection(service, payload["source"])
    directory = _directory(service, run["id"])
    args = dict(condition_id=context["condition_id"], expected_manifest_sha256=context["manifest_sha256"])
    prepared = electrical.prepare_source(directory, run["result"]["summary"], context["candidate_id"], **args)
    projection = electrical.baseline_projection(prepared)
    profile = baseline_profile(meta)
    if profile["pixel_pitch_um"] != projection["pixel_pitch_um"] or profile["cfa"] != projection["cfa"] or profile["pixel_pitch_um"] != 2.9:
        raise ValueError("DB sampled transport requires exact 2.9 um RGGB; no pitch resampling")
    if meta["raw_format"]["bit_depth"] != projection["electronics"]["bit_depth"]:
        raise ValueError("Capture bit depth differs from selected DB ADC")
    for key, value in projection["conditions"].items():
        if meta["conditions"].get(key) != value:
            raise ValueError("DB model condition differs: " + key)
    optical = optics.prepare_optical_source(directory, run["result"]["summary"], context["candidate_id"], **args)
    converted = optics.project_optical_profile(optical, profile, field_mapping_mode="reference_model", allow_geometric=True)
    if not converted["can_apply"]:
        raise ValueError("DB sampled PSF conversion did not qualify")
    required = converted["profile_patch"]["conditions"]
    for key, value in required.items():
        if meta["conditions"].get(key) != value:
            raise ValueError("DB spectral/aperture condition differs: " + key)
    available = [f["source_field_id"] for f in converted["report"]["applicable_fields"]]
    spatial = spatial_spec(payload["spatial"], available)
    frozen = {**{"source/" + k: v for k, v in prepared["frozen_files"].items()},
              **{"optical/" + k: v for k, v in optical["frozen_files"].items()},
              **{"model/" + k: v for k, v in converted["derived_files"].items()}}
    artifacts, files = [], {}
    if sum(map(len, frozen.values())) > 256 * 1024**2:
        raise ValueError("Camera model source exceeds 256 MiB budget")
    for name, data in sorted(frozen.items()):
        a = service.registry.store_blob(data, name, mimetypes.guess_type(name)[0] or "application/octet-stream")
        artifacts.append(a)
        files[name] = {"sha256": a["sha256"], "bytes": len(data)}
    from .calibration_lab import _unique_artifacts
    if lab._source_code() != code or source_selection(service, payload["source"])[1] != context:
        raise IntegrityError("DB model changed during registration")
    context.update(role="numerical_prediction_source", profile_conversion="exact_sampled_four_site_PSF_and_DB_electronics")
    document = {"schema": "camera_esfr_camera_model_v1", "camera": physical,
        "source": deepcopy(payload["source"]), "source_context": context,
        "input_contract": contract(meta), "spatial": spatial, "available_fields": available,
        "electronics": projection, "optical_qualification": converted["report"],
        "files": files, "artifacts": _unique_artifacts(artifacts), "source_code": code,
        "scope": {"mtf_prediction": "selected_DB_native_pixel_integrated_four_site_PSF",
            "source_optical_profile": optical["optical"]["specification"]["optical_profile"],
            "diffraction_included": optical["optical"]["specification"]["optical_profile"] == "exit_pupil_diffraction",
            "geometry_and_plateaus": "development_only_frozen_nuisance_not_absolute_radiometry",
            "spatial_assignment": "declared_assumption_not_verified_sensor_field_correspondence",
            "noise_prediction": "not_inferred_from_chart_texture",
            "absolute_color_tone_prediction": "source_data_required",
            "isp": "bilinear_reference_not_vendor_ISP", "physical_identity": "operator_declared"},
        "physical_validation": "not_established", "db_active_model_updated": False}
    identity = "esfr-camera-" + digest(document)[:32]
    try:
        return get(service, identity)
    except KeyError:
        return service.registry.put_verified_record(KIND, document, [(esfr_lab.CAPTURES, capture)], identity=identity)


def install_routes(app, service):
    @app.get(ROOT)
    def listing():
        return {"models": [{k: r[k] for k in ("id", "content_hash", "camera", "source", "spatial", "created_at")} for r in service.registry.list(KIND)]}

    @app.get(ROOT + "/sources")
    def options():
        return {"sources": source_options(service)}

    @app.get(ROOT + "/source-fields")
    def fields(run_id: str, candidate_id: str, condition_id: str | None = None):
        from .calibration_db import source_selection
        source = {"run_id": run_id, "candidate_id": candidate_id}
        if condition_id is not None:
            source["condition_id"] = condition_id
        run, context = source_selection(service, source)
        candidate = context["candidate_snapshot"]
        from .reference_workshop_job import _directory
        from .calibration_db_optics_engine import prepare_optical_source
        prepared = prepare_optical_source(_directory(service, run["id"]), run["result"]["summary"], candidate_id,
            condition_id=condition_id, expected_manifest_sha256=context["manifest_sha256"])
        from .calibration_db_engine import prepare_source, baseline_projection
        projection = baseline_projection(prepare_source(_directory(service, run["id"]), run["result"]["summary"], candidate_id,
            condition_id=condition_id, expected_manifest_sha256=context["manifest_sha256"]))
        return {"source": source, "optical_profile": prepared["optical"]["specification"]["optical_profile"],
            "fields": [{"id": f["id"], "image_xy_mm": f.get("image_xy_mm"), "field_xy": f.get("field_xy")} for f in prepared["optical"]["fields"] if f["id"] in prepared["dc"]],
            "lens": candidate["source_identities"]["lens"], "sensor": candidate["source_identities"]["sensor"],
            "requirements": {"pixel_pitch_um": projection["pixel_pitch_um"], "cfa": projection["cfa"], "bit_depth": projection["electronics"]["bit_depth"],
                "gain": projection["conditions"]["gain"], "temperature_c": projection["conditions"]["temperature_c"],
                "aperture_f_number": projection["applied_f_number"], "light_id": "D65"}}

    @app.post(ROOT, status_code=201)
    def register(payload: dict):
        return create(service, payload)

    @app.get(ROOT + "/{identity}")
    def show(identity: str):
        return get(service, identity)

