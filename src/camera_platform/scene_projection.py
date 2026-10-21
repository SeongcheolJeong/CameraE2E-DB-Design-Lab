"""Reuse verified spectral radiance on its frozen ray grid without invoking a renderer."""

from __future__ import annotations

import hashlib
import io
import json
from copy import deepcopy

import numpy as np

from . import imaging, projection_radiometry, scene_camera, scene_render, subcell_optics
from .contracts import AssemblyCreate, SpectralImageImport
from .factory import application_domain
from .projection_contracts import SceneProjectionCreate, SceneProjectionSubmit
from .registry import ConflictError, canonical, digest
from .scene_contracts import RenderPublish, SceneRenderCreate
from .scene_templates import render_template
from .service import PreflightError

JOB_KIND = "spectral_scene_projection"
PLUGIN_ID = "stored_spectral_radiance_projection_v1"
MC_SCOPE = "scaled_sample_standard_error_only_not_model_or_total_uncertainty"


def _bytes(service, artifact):
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    if len(content) != artifact["bytes"] or artifact["id"] != artifact["sha256"]:
        raise ValueError("Artifact identity or size differs from the frozen evidence")
    return content


def _named(job, name):
    matches = [a for a in job["artifacts"] if a["name"] == name]
    if len(matches) != 1:
        raise ValueError(f"Source render requires exactly one {name} artifact")
    return matches[0]


def _verify_renderer_sampling(request, summary):
    """Bind a saved cell-mean radiance grid to its original ray quadrature."""
    quadrature = request.get("source_pixel_quadrature")
    if quadrature is None:
        if (summary.get("sampling") != "pixel_centre_pinhole_fixed_wavelength_path_samples"
                or "source_pixel_quadrature" in summary):
            raise ValueError("Stored radiance sampling differs from the frozen render request")
        return
    side = quadrature["grid_side"]
    expected = {
        **quadrature,
        "integrator_path_samples": (
            request["shape"][0] * request["shape"][1] * len(request["wavelength_nm"])
            * request["samples_per_pixel"] * side**2
        ),
        "stochastic_paths_per_subcell_wavelength": request["samples_per_pixel"],
        "sampler_seed": request["seed"],
        "sampler_order": "crop_row_column_subcell_row_column_wavelength_path",
        "subcell_centres_yx": [
            [(row + 0.5) / side, (col + 0.5) / side]
            for row in range(side) for col in range(side)
        ],
        "subcell_weight": 1.0 / side**2,
        "subcell_coordinates": "native_sensor_pixel_yx_from_top_left",
        "projection_geometry_interpretation": (
            "pinhole_pixel_centre_reference_with_subcell_offsets"
        ),
        "source_cell_representation": (
            "area_mean_with_retained_subcell_radiance"
            if quadrature["model"] == subcell_optics.MODEL
            else "area_mean_radiance_assigned_to_native_cell_centre"
        ),
        "first_hit_auxiliary": "pixel_centre_ray_only",
        "spatial_quadrature_error": "unknown",
        "continuous_scene_psf_integration": False,
        "receiver_aperture_filter_applied_here": False,
        "physical_validation": "not_established",
    }
    if quadrature["model"] == subcell_optics.MODEL:
        expected.update(
            retained_radiance_artifact="source-subcell-radiance.npy",
            retained_standard_error_artifact="source-subcell-mc-standard-error.npy",
            fractional_source_psf_model=subcell_optics.TRANSFER,
        )
    recorded = summary.get("source_pixel_quadrature")
    if (
        summary.get("sampling")
        != "native_sensor_area_midpoint_grid_v1_fixed_wavelength_path_samples"
        or not isinstance(recorded, dict)
        or (quadrature["model"] == subcell_optics.MODEL
            and set(recorded) != set(expected))
        or any(key not in recorded or canonical(recorded[key]) != canonical(value)
               for key, value in expected.items())
        or any(canonical(summary.get(key)) != canonical(request[key])
               for key in ("samples_per_pixel", "max_depth", "seed"))
    ):
        raise ValueError("Stored radiance quadrature differs from the frozen render request")


def _source(service, identity):
    """Verify historical numerical evidence without requiring its old executable."""
    job = scene_render.get_job(service, identity)
    if job["status"] != "succeeded" or job["plugin_id"] != scene_render.PLUGIN_ID:
        raise ValueError("Stored radiance requires a completed Mitsuba scene render")
    for artifact in [*job["input_artifacts"], *job["artifacts"]]:
        _bytes(service, artifact)
    req = job["request"]
    SceneRenderCreate.model_validate(
        {k: v for k, v in req.items() if k in SceneRenderCreate.model_fields}
    )
    scene = scene_render._asset(service, job["scene_asset_id"], "scene")
    if not (scene == req["scene"] == job["scene_asset_snapshot"]):
        raise ValueError("Source render scene differs from the frozen scene")
    if scene_render.dependencies(service, scene["payload"]) != req["assets"]:
        raise ValueError("Source render assets differ from their immutable versions")
    scene_render._verify_motion_request(service, job)
    source_assembly, sensor, pitch, full = imaging._native(service, job["assembly_id"])
    if source_assembly != job["assembly_snapshot"]:
        raise ValueError("Source camera differs from its immutable snapshot")
    lens = service.registry.get("models", job["lens_snapshot"]["id"])
    if lens != job["lens_snapshot"] or lens["id"] not in source_assembly["model_ids"]:
        raise ValueError("Source projection lens differs from its immutable snapshot")
    projection_radiometry.verify_job(service, job)
    sampling = req["sampling"]
    crop = req["crop_origin"] or [(f - n) // 2 for f, n in zip(full, req["shape"], strict=True)]
    expected_sampling = {
        "kind": "native_sensor",
        "full_sensor_shape": full,
        "crop_origin": crop,
        "pixel_pitch_um": pitch,
    }
    if sampling != expected_sampling or req["wavelength_nm"] != sensor["payload"]["wavelength_nm"]:
        raise ValueError("Source radiance coordinates differ from the frozen native sensor")
    if any(o < 0 or o + n > f for o, n, f in zip(crop, req["shape"], full, strict=True)):
        raise ValueError("Source radiance crop lies outside the frozen native sensor")
    for key, value, unit in [
        ("focal_length", req["projection"]["focal_length_mm"], "mm"),
        ("f_number", req["projection"]["f_number"], "1"),
    ]:
        declared = lens["properties"].get(key)
        if not declared or declared.get("value") != value or declared.get("unit") != unit:
            raise ValueError("Source projection differs from its pinned lens properties")
    if source_assembly["conditions"].get("f_number") != {
        "value": req["projection"]["f_number"],
        "unit": "1",
    } or source_assembly["conditions"].get("light_id") != {
        "value": scene["illumination_id"],
        "unit": "1",
    }:
        raise ValueError("Source projection conditions differ from the frozen camera and scene")
    source_input = json.loads(_bytes(service, _named(job, "scene-render-input.json")))
    if source_input != req:
        raise ValueError("Original scene request bytes differ from frozen coordinates")
    metadata = SpectralImageImport.model_validate(job["result"]["spectral_image_metadata"])
    if (
        metadata.unit != "W/m^2/nm"
        or metadata.reference_plane != "sensor_ideal_image_before_psf_ri"
        or metadata.included_effects != ["on_axis_lens_throughput"]
        or metadata.source_uri != "scene-render://" + identity
        or metadata.method_and_version != scene_render.PLUGIN_ID
        or metadata.wavelength_nm != req["wavelength_nm"]
        or metadata.sampling != sampling
        or metadata.conditions != source_assembly["conditions"]
    ):
        raise ValueError("Source image metadata changes the frozen radiometric contract")
    radiance = imaging.load_spectral_npy(_bytes(service, _named(job, "spectral-radiance.npy")))
    expected_shape = [*req["shape"], len(req["wavelength_nm"])]
    summary = json.loads(_bytes(service, _named(job, "renderer-summary.json")))
    if (
        summary != job["result"]["summary"]
        or summary["schema"] != "fixed_wavelength_path_radiance_v1"
        or summary["unit"] != "W/m^2/sr/nm"
        or summary["wavelength_nm"] != req["wavelength_nm"]
        or summary["shape"] != expected_shape
        or list(radiance.shape) != expected_shape
    ):
        raise ValueError("Stored radiance semantics or shape differ from frozen coordinates")
    if "scene_motion_evidence" in req:
        if summary.get("scene_motion") != req["scene_motion_evidence"]:
            raise ValueError("Stored radiance motion differs from the frozen render time")
    elif "scene_motion" in summary:
        raise ValueError("Untimed stored radiance has unexpected motion evidence")
    _verify_renderer_sampling(req, summary)
    scene_render._retained_subcells(service, job, radiance)
    error = imaging.load_spectral_npy(
        _bytes(service, _named(job, "radiance-mc-standard-error.npy"))
    )
    maps = [
        np.load(io.BytesIO(_bytes(service, _named(job, name))), allow_pickle=False)
        for name in ("first-hit-depth-m.npy", "first-hit-valid.npy", "first-hit-object-index.npy")
    ]
    scene_render.check_auxiliary_outputs(radiance, *maps, error, len(scene["payload"]["instances"]))
    original_e = imaging.load_spectral_npy(_bytes(service, _named(job, "spectral-image.npy")))
    if not np.array_equal(original_e, scene_render.project_radiance(radiance, req["projection"])):
        raise ValueError("Source irradiance no longer matches its declared radiance projection")
    return job, radiance, error


def template(service, source_render_id, assembly_id):
    imaging._current(service)
    source, _, _ = _source(service, source_render_id)
    prepared = render_template(service, source["scene_asset_id"], assembly_id)
    request = prepared["request"]
    return {
        "request": {
            "name": (request["name"][:160] + " · 저장 광휘 재사용"),
            "source_render_id": source_render_id,
            "assembly_id": assembly_id,
            "projection": request["projection"],
            "force": False,
        },
        "issues": prepared["issues"],
        "source": {
            "id": source_render_id,
            "scene_asset_id": source["scene_asset_id"],
            "assembly_id": source["assembly_id"],
            "sampling": source["request"]["sampling"],
            "shape": source["request"]["shape"],
            "pose": source["request"]["pose"],
            "wavelength_nm": source["request"]["wavelength_nm"],
        },
        "required_input": prepared["required_input"],
        "physical_validation": "not_established",
    }


def preview(service, payload):
    environment = imaging._current(service)
    data = SceneProjectionCreate.model_validate(payload).model_dump()
    source, _, _ = _source(service, data["source_render_id"])
    target = service.registry.get("assemblies", data["assembly_id"])
    prepared = scene_camera.preview(
        service,
        {
            "scene_asset_id": source["scene_asset_id"],
            "source_assembly_id": target["id"],
            "assembly": {k: deepcopy(target[k]) for k in AssemblyCreate.model_fields},
        },
    )
    issues = deepcopy(prepared["issues"])
    rerender = False

    def issue(code, message, path, *, geometry=False):
        nonlocal rerender
        rerender |= geometry
        issues.append({"code": code, "message": message, "path": path, "severity": "error"})

    req, projection = source["request"], data["projection"]
    lens = next(service.registry.get("models", identity) for identity in target["model_ids"]
                if service.registry.get("models", identity)["kind"] == "lens")
    projection_evidence = None
    try:
        projection_evidence = projection_radiometry.verify_projection(
            service, projection, lens, req["wavelength_nm"]
        )
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        issue("projection_radiometry_mismatch", str(exc), "projection")
    try:
        for problem in scene_camera.field_response_preflight(
            service, target, req["sampling"], req["shape"],
            capture_crop=req.get("capture_crop"),
        ):
            issue("projection_field_psf_coverage", problem["message"], "models.lens.psf")
        quadrature = req.get("source_pixel_quadrature")
        if quadrature and quadrature["model"] == subcell_optics.MODEL:
            from .spatial import validate_psf

            response = validate_psf(lens["payload"]["psf"])
            if (response["wavelength_nm"] != req["wavelength_nm"]
                    or response["sample_pitch_um"] != req["sampling"]["pixel_pitch_um"]):
                raise ValueError("Target subcell PSF differs from source wavelength/native pitch")
            subcell_optics.preflight(
                (*req["shape"], len(req["wavelength_nm"])),
                quadrature["grid_side"], response["values"],
                req["sampling"]["pixel_pitch_um"],
                req["sampling"]["full_sensor_shape"], req["sampling"]["crop_origin"],
                field_axes=(response["field_y_mm"], response["field_x_mm"])
                if response["schema"] == "field_intensity_psf_v1" else None,
            )
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        issue("projection_field_psf_coverage", str(exc), "models.lens.psf")
    target_sampling = prepared["sampling"]
    if target_sampling is not None:
        for key in ("full_sensor_shape", "pixel_pitch_um"):
            if target_sampling[key] != req["sampling"][key]:
                issue(
                    "rerender_" + key,
                    "센서의 광선 격자가 달라 다시 렌더링해야 합니다.",
                    "sampling." + key,
                    geometry=True,
                )
        if target_sampling["wavelength_nm"] != req["wavelength_nm"]:
            issue(
                "rerender_wavelengths",
                "파장 좌표 변경은 원본 광휘를 재사용할 수 없습니다.",
                "wavelength_nm",
                geometry=True,
            )
    focal = prepared["optics"]["focal_length"]
    if (
        focal and focal["value"] != req["projection"]["focal_length_mm"]
    ) or projection["focal_length_mm"] != req["projection"]["focal_length_mm"]:
        issue(
            "rerender_focal_length",
            "초점거리 변경은 광선 방향을 바꾸므로 다시 렌더링해야 합니다.",
            "projection.focal_length_mm",
            geometry=True,
        )
    for key, prop in [("focal_length_mm", "focal_length"), ("f_number", "f_number")]:
        expected = prepared["optics"][prop]
        if expected is None or projection[key] != expected["value"]:
            issue(
                "projection_lens_mismatch",
                "투영 값은 선택 Lens의 고정 특성과 같아야 합니다.",
                "projection." + key,
            )
    if projection_radiometry.wavelength_samples(projection) != req["wavelength_nm"]:
        issue(
            "projection_spectral_mismatch",
            "투과율은 원본 광휘와 같은 파장 좌표가 필요합니다.",
            "projection.on_axis_transmittance",
        )
    old_conditions, new_conditions = source["assembly_snapshot"]["conditions"], target["conditions"]
    try:
        application_domain(new_conditions)
    except ValueError as exc:
        issue("invalid_projection_conditions", str(exc), "conditions")
    for key in sorted(
        (old_conditions.keys() | new_conditions.keys()) - {"exposure_s", "gain", "f_number"}
    ):
        if (key in old_conditions) != (key in new_conditions) or old_conditions.get(
            key
        ) != new_conditions.get(key):
            issue(
                "rerender_condition_changed",
                f"{key} 조건 변경은 원본 장면 광휘 재사용 범위를 벗어납니다.",
                "conditions." + key,
                geometry=True,
            )
    result = {
        "schema": "camera_scene_projection_preview_v1",
        "draft": data,
        "status": "rerender_required" if rerender else "needs_input" if issues else "ready",
        "issues": issues,
        "source_render_sha256": source["content_hash"],
        "source_snapshot_sha256": digest(source),
        "source_result_sha256": digest(source["result"]),
        "target_assembly_sha256": target["content_hash"],
        "model_hashes": prepared["model_hashes"],
        "camera_preparation": prepared,
        "frozen_sampling": req["sampling"],
        "shape": req["shape"],
        "wavelength_nm": req["wavelength_nm"],
        "pose": req["pose"],
        "source_projection": req["projection"],
        "target_projection": projection,
        "environment_sha256": digest(environment),
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
    }
    if projection_evidence is not None:
        result["projection_evidence_sha256"] = digest(projection_evidence)
    return {**result, "preview_sha256": digest(result)}


def submit(service, payload):
    data = SceneProjectionSubmit.model_validate(payload).model_dump()
    plan = preview(service, data["draft"])
    if plan["preview_sha256"] != data["preview_sha256"]:
        raise ConflictError("Projection inputs changed; preview the exact draft again")
    if plan["status"] != "ready":
        raise PreflightError({"status": "FAIL", "issues": plan["issues"]})
    request = deepcopy(plan["draft"])
    force = request.pop("force")
    source, _, _ = _source(service, request["source_render_id"])
    target = service.registry.get("assemblies", request["assembly_id"])
    models = [service.registry.get("models", i) for i in target["model_ids"]]
    request.update(
        sampling=plan["frozen_sampling"],
        shape=plan["shape"],
        wavelength_nm=plan["wavelength_nm"],
        pose=plan["pose"],
    )
    original = service.registry.store_blob(
        canonical(request).encode(), "scene-projection-input.json", "application/json"
    )
    frozen = {
        "name": request["name"],
        "job_kind": JOB_KIND,
        "plugin_id": PLUGIN_ID,
        "source_render_id": source["id"],
        "assembly_id": target["id"],
        "scene_asset_id": source["scene_asset_id"],
        "request": request,
        "source_render_snapshot": source,
        "assembly_snapshot": target,
        "lens_snapshot": next(m for m in models if m["kind"] == "lens"),
        "model_snapshots": models,
        "scene_asset_snapshot": source["scene_asset_snapshot"],
        "preview": plan,
        "environment": imaging._current(service),
        "input_artifacts": [original, *source["artifacts"]],
    }
    evidence = projection_radiometry.verify_projection(
        service, request["projection"], frozen["lens_snapshot"], request["wavelength_nm"]
    )
    if evidence is not None:
        frozen["projection_evidence"] = evidence
        frozen["input_artifacts"].extend(evidence["artifacts"])
    # force is an execution request, not a physical input or cache identity.
    frozen["preview"] = {k: v for k, v in plan.items() if k not in {"draft", "preview_sha256"}}
    queued = service.registry.submit(frozen, digest(frozen), force=force)
    if queued["status"] == "succeeded" and (
        evidence is not None or source["request"].get("source_pixel_quadrature", {}).get(
            "model"
        ) == subcell_optics.MODEL
    ):
        cached = get_job(service, queued["id"])
        publication_evidence(
            service, cached["id"],
            SpectralImageImport.model_validate(cached["result"]["spectral_image_metadata"]).model_dump(),
            _bytes(service, cached["artifacts"][0]),
        )
    return queued


def get_job(service, identity):
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND or job.get("plugin_id") != PLUGIN_ID:
        raise ValueError("Expected a stored spectral radiance projection job")
    return job


def execute(service, job):
    if job["environment"] != imaging._current(service):
        raise ValueError("Projection Worker differs from the frozen execution environment")
    projection_radiometry.verify_job(service, job)
    source, radiance, error = _source(service, job["source_render_id"])
    if source != job["source_render_snapshot"]:
        raise ValueError("Stored source radiance differs from the frozen projection input")
    for artifact in job["input_artifacts"]:
        _bytes(service, artifact)
    request = job["request"]
    data = {k: v for k, v in request.items() if k in SceneProjectionCreate.model_fields}
    plan = preview(service, data)
    comparable = {k: v for k, v in plan.items() if k not in {"draft", "preview_sha256"}}
    if plan["status"] != "ready" or comparable != job["preview"]:
        raise ValueError("Frozen projection inputs no longer pass the same preflight")
    artifacts = []
    for name, array in [
        ("spectral-image.npy", scene_render.project_radiance(radiance, request["projection"])),
        (
            "irradiance-mc-standard-error.npy",
            scene_render.project_radiance(error, request["projection"]),
        ),
    ]:
        stream = io.BytesIO()
        np.save(stream, array, allow_pickle=False)
        artifacts.append(
            service.registry.store_blob(stream.getvalue(), name, "application/octet-stream")
        )
    # Keep original names, even the original E name: ZIP paths include (hash,name),
    # and the source snapshot must still resolve its original artifact paths.
    artifacts.extend(job["input_artifacts"])
    metadata = imaging.spectral_template(service, job["assembly_id"])
    metadata.update(
        name=job["name"],
        source_kind="simulated",
        source_uri="scene-projection://" + job["id"],
        method_and_version=PLUGIN_ID,
        sampling=request["sampling"],
        license="Derived from declared scene and projection rights; see frozen sources",
    )
    return {
        "summary": {
            "schema": "stored_radiance_projection_result_v1",
            "source_render_id": source["id"],
            "radiance_reused": True,
            "renderer_invoked": False,
            "radiance_sha256": _named(source, "spectral-radiance.npy")["sha256"],
            "shape": [*request["shape"], len(request["wavelength_nm"])],
            "unit": "W/m^2/nm",
            "mc_standard_error": MC_SCOPE,
            **projection_radiometry.summary(request["projection"]),
            **({
                "source_pixel_quadrature": deepcopy(
                    source["result"]["summary"]["source_pixel_quadrature"]
                ),
                "continuous_scene_psf_integration": False,
            } if source["request"].get("source_pixel_quadrature") else {}),
            **({
                "source_scene_motion": deepcopy(source["result"]["summary"]["scene_motion"]),
            } if "scene_motion_evidence" in source["request"] else {}),
        },
        "display": deepcopy(source["result"].get("display", {})),
        "spectral_image_metadata": metadata,
        "artifacts": artifacts,
        "projection": request["projection"],
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
    }


def publication_evidence(service, identity, metadata, content):
    job = get_job(service, identity)
    if job["status"] != "succeeded":
        raise ValueError("Only completed stored radiance projections can be published")
    projection_radiometry.verify_job(service, job)
    source, radiance, _ = _source(service, job["source_render_id"])
    if source != job["source_render_snapshot"]:
        raise ValueError("Projection source differs from its immutable snapshot")
    expected = {
        **job["result"]["spectral_image_metadata"],
        "name": metadata["name"],
        "revision": metadata["revision"],
    }
    if SpectralImageImport.model_validate(expected).model_dump() != metadata:
        raise ValueError("Projected image metadata must preserve the frozen physical contract")
    artifact = job["artifacts"][0]
    if (
        artifact["name"] != "spectral-image.npy"
        or hashlib.sha256(content).hexdigest() != artifact["sha256"]
    ):
        raise ValueError("Published irradiance differs from the new projection result")
    if not np.array_equal(
        imaging.load_spectral_npy(content),
        scene_render.project_radiance(radiance, job["request"]["projection"]),
    ):
        raise ValueError("Published irradiance does not project the original radiance")
    for artifact in job["artifacts"]:
        _bytes(service, artifact)
    return job


def publish(service, payload):
    imaging._current(service)
    data = RenderPublish.model_validate(payload).model_dump()
    job = get_job(service, data["job_id"])
    if job["status"] != "succeeded":
        raise ValueError("Only completed stored radiance projections can be published")
    metadata = {
        **job["result"]["spectral_image_metadata"],
        "name": data["name"],
        "revision": data["revision"],
    }
    return imaging.import_spectral(
        service, metadata, _bytes(service, job["artifacts"][0]), projection_job_id=job["id"]
    )
