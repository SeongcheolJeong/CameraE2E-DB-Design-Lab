"""Scene asset graph, durable spectral rendering and traceable camera projection."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path

import numpy as np

from . import imaging, projection_radiometry, scene_motion, subcell_optics
from .contracts import SpectralImageImport
from .registry import canonical, digest
from .scene_contracts import RenderPublish, SceneAssetCreate, SceneRenderCreate

JOB_KIND = "spectral_scene_render"
PLUGIN_ID = "mitsuba_fixed_wavelength_scene_v1"


def illumination_id(payload, related):
    lights = [r for r in related if r["payload"]["kind"] == "light"]
    area = [i for i in payload["instances"] if i["area_light_id"]]
    return "scene-lighting-sha256:" + digest(
        {
            "lights": lights,
            "area_placements": area,
            "environment": payload["environment_light_id"],
            "point_lights": payload["point_lights"],
        }
    )


def placed_light_ids(payload):
    """All installed emitters, grouped by immutable Light asset identity."""
    return {
        *[item["area_light_id"] for item in payload["instances"]
          if item["area_light_id"]],
        *([payload["environment_light_id"]] if payload["environment_light_id"] else []),
        *[item["light_id"] for item in payload["point_lights"]],
    }


def _light_component_evidence(request):
    """The selector changes emission only; the whole scene still owns light_id."""
    identity = request.get("light_component_id")
    if identity is None:
        return None
    scene = request["scene"]
    assets = request["assets"]
    if (not isinstance(identity, str) or not identity
            or identity not in placed_light_ids(scene["payload"])):
        raise ValueError("Selected Light must be placed in the frozen 3D scene")
    matches = [asset for asset in assets if asset["id"] == identity]
    if len(matches) != 1 or matches[0]["payload"]["kind"] != "light":
        raise ValueError("Selected Light differs from the frozen dependency snapshot")
    if scene["illumination_id"] != illumination_id(scene["payload"], assets):
        raise ValueError("Frozen scene illumination identity differs from its Light assets")
    return {
        "model": "one_frozen_scene_light_asset_v1",
        "light_asset_id": identity,
        "light_asset_sha256": matches[0]["content_hash"],
        "whole_scene_illumination_id": scene["illumination_id"],
        "other_emitters": "disabled_for_this_render_geometry_retained",
        "superposition": "linear_scene_radiance_expectation_not_finite_sample_identity",
    }


def _asset(service, identity, kind):
    record = service.registry.get("scene_assets", identity)
    if record["payload"]["kind"] != kind:
        raise ValueError(f"Scene graph reference must select a {kind} revision")
    return record


def dependencies(service, payload):
    records = {}

    def add(identity, kind, distribution=None):
        record = _asset(service, identity, kind)
        if distribution and record["payload"]["distribution"] != distribution:
            raise ValueError("Light distribution does not match its scene placement")
        records[identity] = record

    for item in payload["instances"]:
        add(item["geometry_id"], "geometry")
        add(item["material_id"], "material")
        if records[item["geometry_id"]]["payload"]["primitive"] == "sphere":
            scale = np.linalg.svd(np.asarray(item["to_world"])[:3, :3], compute_uv=False)
            if not np.allclose(scale, scale[0], rtol=1e-6, atol=0):
                raise ValueError("Mitsuba spheres require uniform scale; use a mesh for ellipsoids")
        if item["area_light_id"]:
            add(item["area_light_id"], "light", "area")
    if payload["environment_light_id"]:
        add(payload["environment_light_id"], "light", "constant_environment")
    for item in payload["point_lights"]:
        add(item["light_id"], "light", "point")
    return [records[k] for k in sorted(records)]


def create_asset(service, payload):
    imaging._current(service)
    data = SceneAssetCreate.model_validate(payload).model_dump()
    # Optional motion fields are an opt-in. Keep historical static asset JSON
    # byte-for-byte stable under the extended validation model.
    if data["payload"]["kind"] == "scene":
        if data["payload"]["clock"] is None:
            data["payload"].pop("clock")
        for item in data["payload"]["instances"]:
            if item["motion"] is None:
                item.pop("motion")
    for identity in data["parents"]:
        _asset(service, identity, data["payload"]["kind"])
    related = dependencies(service, data["payload"]) if data["payload"]["kind"] == "scene" else []
    if len(canonical(data).encode()) > 1_048_576:
        raise ValueError("Scene asset JSON exceeds 1 MiB")
    original = service.registry.store_blob(
        canonical(data).encode(), "scene-asset.json", "application/json"
    )
    return service.registry.put(
        "scene_assets",
        {
            **data,
            "dependency_snapshots": related,
            **(
                {"illumination_id": illumination_id(data["payload"], related)}
                if data["payload"]["kind"] == "scene"
                else {}
            ),
            "artifacts": [original],
            "approval_status": "draft",
        },
    )


def backend_identity():
    configured = os.environ.get("CAMERA_PLATFORM_MITSUBA_PYTHON")
    if not configured:
        raise ValueError(
            "Configure CAMERA_PLATFORM_MITSUBA_PYTHON in the trusted server environment"
        )
    # Resolving the executable symlink discards pyvenv.cfg and selects the base
    # interpreter's packages. Preserve the configured virtual-environment path.
    executable = Path(os.path.abspath(Path(configured).expanduser()))
    if not executable.is_file():
        raise ValueError("Configured Mitsuba interpreter does not exist")
    script = Path(__file__).with_name("mitsuba_scene.py")
    result = subprocess.run(
        [str(executable), "-I", str(script), "--probe"],
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )
    return {
        "kind": "isolated_interpreter",
        "executable": str(executable),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "adapter_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "versions": json.loads(result.stdout),
    }


def catalog():
    try:
        backend, error = backend_identity(), None
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        backend, error = None, str(exc)
    return {
        "plugin_id": PLUGIN_ID,
        "role": "scene_rendering",
        "available": backend is not None,
        "backend": backend,
        "reason": error,
        "supports": [
            "reusable_triangle_geometry",
            "spectral_diffuse_and_rough_conductor",
            "environment_point_area_lights",
            "depth_occlusion",
            "spectral_path_radiance",
            "native_capture_roi_with_psf_source_halo",
            "native_capture_roi_with_psf_and_carrier_source_halo",
            "instantaneous_3d_linear_translation_at_explicit_relative_ns",
            "retained_native_source_subcells_for_approximate_fractional_psf",
            "opt_in_frozen_single_light_component_render_with_all_geometry_retained",
        ],
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
    }


def _carrier_generation_crop(full, capture_origin, capture_shape, destination_offsets):
    """Find source wells that can route generated charge into the receiver ROI."""
    origin, shape = [], []
    for axis, (native, start, side) in enumerate(
        zip(full, capture_origin, capture_shape, strict=True)
    ):
        offsets = [offset[axis] for offset in destination_offsets]
        lower = max(0, start - max(offsets))
        upper = min(native, start + side - min(offsets))
        origin.append(lower)
        shape.append(upper - lower)
    return origin, shape


def _halo_source_crop(full, capture_origin, capture_shape, kernel_shape,
                      generation_crop=None):
    """Cover optical sources for generation plus the actual receiver ROI."""
    source_origin, source_shape = [], []
    generated_origin, generated_shape = generation_crop or (capture_origin, capture_shape)
    for native, start, side, generated_start, generated_side, kernel in zip(
        full, capture_origin, capture_shape, generated_origin, generated_shape,
        kernel_shape, strict=True
    ):
        radius = kernel // 2
        lower = min(start, max(0, generated_start - radius))
        upper = max(start + side, min(native, generated_start + generated_side + radius))
        # A stored spectral image must retain even Bayer sides. Add an actual
        # native source pixel when the clipped halo has an odd extent.
        if (upper - lower) % 2:
            if upper < native:
                upper += 1
            elif lower > 0:
                lower -= 1
            else:
                raise ValueError("PSF halo cannot fit an even native source crop")
        if upper - lower > 256:
            raise ValueError("PSF halo source crop exceeds the 256-pixel render side limit")
        source_origin.append(lower)
        source_shape.append(upper - lower)
    return source_origin, source_shape


def submit(service, payload):
    environment = imaging._current(service)
    data = SceneRenderCreate.model_validate(payload).model_dump()
    if data["pose"]["motion"] is None:
        data["pose"].pop("motion")
    if data["render_time_ns"] is None:
        data.pop("render_time_ns")
        data.pop("render_clock")
    # Preserve the historical frozen request bytes when the opt-in is absent.
    if data["source_pixel_quadrature"] is None:
        data.pop("source_pixel_quadrature")
    if data["light_component_id"] is None:
        data.pop("light_component_id")
    force = data.pop("force")
    render_psf_halo = data.pop("render_psf_halo")
    render_charge_collection_halo = data.pop("render_charge_collection_halo")
    scene = _asset(service, data["scene_asset_id"], "scene")
    resolved_motion = scene_motion.resolve(data, scene)
    assets = dependencies(service, scene["payload"])
    if assets != scene["dependency_snapshots"]:
        raise ValueError("Scene dependency graph differs from its immutable snapshots")
    assembly, sensor, pitch, full = imaging._native(service, data["assembly_id"])
    if assembly["conditions"].get("light_id") != {"value": scene["illumination_id"], "unit": "1"}:
        raise ValueError("Assembly light_id must name the scene's frozen illumination_id")
    lens = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "lens"
    )
    projection = data["projection"]
    wave = sensor["payload"]["wavelength_nm"]
    projection_evidence = projection_radiometry.verify_projection(service, projection, lens, wave)
    for record in assets:
        spec = record["payload"].get("spectrum")
        if spec and (spec["wavelength_nm"][0] > wave[0] or spec["wavelength_nm"][-1] < wave[-1]):
            raise ValueError("Material/light wavelength coverage cannot be extrapolated")
    for prop, value, unit in [
        ("focal_length", projection["focal_length_mm"], "mm"),
        ("f_number", projection["f_number"], "1"),
    ]:
        declared = lens["properties"].get(prop)
        if not declared or declared.get("unit") != unit or declared.get("value") != value:
            raise ValueError(f"Projection {prop} must match the pinned lens property in {unit}")
    if assembly["conditions"].get("f_number") != {"value": projection["f_number"], "unit": "1"}:
        raise ValueError("Projection aperture must match assembly operating conditions")
    capture_shape = data["shape"]
    capture_origin = data["crop_origin"] or [
        (f - n) // 2 for f, n in zip(full, capture_shape, strict=True)
    ]
    if any(o < 0 or o + n > f for o, n, f in
           zip(capture_origin, capture_shape, full, strict=True)):
        raise ValueError("Render crop lies outside the native sensor")
    carrier_artifacts = []
    if render_psf_halo or render_charge_collection_halo:
        from .spatial import validate_psf, validate_psf_coverage

        if "psf" not in lens["payload"]:
            raise ValueError("PSF halo rendering requires a pinned Lens intensity PSF")
        psf = validate_psf(lens["payload"]["psf"])
        if psf["wavelength_nm"] != wave or psf["sample_pitch_um"] != pitch:
            raise ValueError("PSF halo requires the pinned Sensor wavelength grid and pixel pitch")
        kernel_shape = list(np.shape(psf["values"])[-2:])
        generation_crop = None
        if render_charge_collection_halo:
            from .native_model_evidence import verify_models
            from .runtime import sensor_response_fields

            sensor_response = sensor_response_fields(sensor["payload"])
            if "charge_collection" not in sensor_response:
                raise ValueError("Carrier halo requires pinned Sensor charge_collection")
            carrier_artifacts = verify_models(service, [sensor])
            collection = sensor_response["charge_collection"]
            generation_crop = _carrier_generation_crop(
                full, capture_origin, capture_shape,
                collection["destination_offsets_rc"],
            )
        retained = data.get("source_pixel_quadrature", {}).get("model") == subcell_optics.MODEL
        support_shape = [side + 2 for side in kernel_shape] if retained else kernel_shape
        source_origin, source_shape = _halo_source_crop(
            full, capture_origin, capture_shape, support_shape, generation_crop
        )
        validate_psf_coverage(psf, full, source_origin, source_shape, pitch)
        data.update(
            shape=source_shape,
            crop_origin=source_origin,
            capture_crop={"crop_origin": capture_origin, "shape": capture_shape},
            psf_halo={
                "kernel_shape": kernel_shape,
                "kernel_source": psf["source"],
                "support_policy": (
                    "native_sensor_clipped_subcell_shift_psf_and_carrier_support"
                    if retained and render_charge_collection_halo else
                    "native_sensor_clipped_subcell_shift_psf_support"
                    if retained else
                    "native_sensor_clipped_psf_and_carrier_support_with_even_source_extent"
                    if render_charge_collection_halo else
                    "native_sensor_clipped_psf_radius_with_even_source_extent"
                ),
            },
        )
        if render_charge_collection_halo:
            data["carrier_halo"] = {
                "sensor_model_id": sensor["id"],
                "sensor_model_sha256": sensor["content_hash"],
                "charge_collection_sha256": sensor["response_artifacts"][
                    "charge_collection"
                ]["sha256"],
                "destination_offsets_rc": collection["destination_offsets_rc"],
                "generation_crop": {
                    "crop_origin": generation_crop[0], "shape": generation_crop[1],
                },
                "boundary": collection["boundary"],
            }
    shape = data["shape"]
    crop = data["crop_origin"] or capture_origin
    quadrature = data.get("source_pixel_quadrature")
    grid_side = quadrature["grid_side"] if quadrature else 1
    if np.prod(shape) * len(wave) * data["samples_per_pixel"] * grid_side**2 > 262144:
        raise ValueError("Render exceeds 262144 fixed-wavelength primary paths")
    if quadrature and quadrature["model"] == subcell_optics.MODEL:
        from .spatial import validate_psf

        if "psf" not in lens["payload"]:
            raise ValueError("Retained subcells require a pinned receiver-integrated Lens PSF")
        response = validate_psf(lens["payload"]["psf"])
        if response["wavelength_nm"] != wave or response["sample_pitch_um"] != pitch:
            raise ValueError("Retained subcell PSF differs from native sensor grid")
        subcell_optics.preflight(
            (*shape, len(wave)), grid_side, response["values"], pitch, full, crop,
            field_axes=(response["field_y_mm"], response["field_x_mm"])
            if response["schema"] == "field_intensity_psf_v1" else None,
        )
    from .scene_camera import field_response_preflight
    from .service import PreflightError

    sampling = {
        "kind": "native_sensor", "full_sensor_shape": full,
        "crop_origin": crop, "pixel_pitch_um": pitch,
    }
    issues = field_response_preflight(
        service, assembly, sampling, shape, capture_crop=data.get("capture_crop")
    )
    if issues:
        raise PreflightError({"status": "FAIL", "issues": issues})
    by_id = {a["id"]: a for a in assets}
    if (
        sum(
            len(by_id[i["geometry_id"]]["payload"].get("faces", []))
            for i in scene["payload"]["instances"]
        )
        > 100000
    ):
        raise ValueError("Scene exceeds 100000 instantiated mesh triangles")
    backend = backend_identity()
    request = {
        **data,
        "scene": scene,
        "assets": assets,
        "wavelength_nm": wave,
        "sampling": sampling,
    }
    _light_component_evidence(request)
    if resolved_motion is not None:
        request.update(
            resolved_scene=resolved_motion["scene"],
            resolved_pose=resolved_motion["pose"],
            scene_motion_evidence=resolved_motion["evidence"],
        )
    original = service.registry.store_blob(
        canonical(request).encode(), "scene-render-input.json", "application/json"
    )
    frozen = {
        "name": data["name"],
        "job_kind": JOB_KIND,
        "scene_asset_id": data["scene_asset_id"],
        "assembly_id": data["assembly_id"],
        "plugin_id": PLUGIN_ID,
        "request": request,
        "scene_asset_snapshot": scene,
        "assembly_snapshot": assembly,
        "lens_snapshot": lens,
        **({"sensor_snapshot": sensor} if render_charge_collection_halo else {}),
        "environment": environment,
        "backend": backend,
        "input_artifacts": ([original] + [a for r in [scene, *assets]
                                           for a in r["artifacts"]] + carrier_artifacts),
    }
    if projection_evidence is not None:
        frozen["projection_evidence"] = projection_evidence
        frozen["input_artifacts"].extend(projection_evidence["artifacts"])
    queued = service.registry.submit(frozen, digest(frozen), force=force)
    if queued["status"] == "succeeded":
        cached = get_job(service, queued["id"])
        _verify_light_component_job(service, cached)
        _verify_motion_request(service, cached)
        if projection_evidence is not None:
            _verify_cached_projection(service, cached)
        if render_charge_collection_halo:
            _verify_carrier_halo(service, cached)
        _retained_subcells(service, cached)
    return queued


def get_job(service, identity):
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Expected a spectral scene rendering job")
    return job


def _verify_motion_request(service, job):
    """Refuse a changed source/clock/resolved pose on timed render replay."""
    request = job["request"]
    if "render_time_ns" not in request:
        if any(key in request for key in (
            "render_clock", "resolved_scene", "resolved_pose", "scene_motion_evidence"
        )):
            raise ValueError("Untimed render carries timed scene fields")
        return
    scene = _asset(service, job["scene_asset_id"], "scene")
    if scene != job["scene_asset_snapshot"] or scene != request["scene"]:
        raise ValueError("Timed render source differs from its immutable scene asset")
    assets = dependencies(service, scene["payload"])
    if assets != request["assets"] or assets != scene["dependency_snapshots"]:
        raise ValueError("Timed render dependencies differ from frozen asset versions")
    resolved = scene_motion.resolve(request, scene)
    if resolved is None or any(
        request.get(key) != resolved[source]
        for key, source in (
            ("resolved_scene", "scene"),
            ("resolved_pose", "pose"),
            ("scene_motion_evidence", "evidence"),
        )
    ):
        raise ValueError("Timed render pose differs from its frozen motion equation")


def _verify_light_component_job(service, job):
    """Rebind opt-in rendering to the original graph and exact renderer record."""
    request = job["request"]
    expected = _light_component_evidence(request)
    if expected is None:
        return
    scene = _asset(service, job["scene_asset_id"], "scene")
    if (scene != request["scene"] or scene != job["scene_asset_snapshot"]
            or dependencies(service, scene["payload"]) != request["assets"]
            or request["assets"] != scene["dependency_snapshots"]):
        raise ValueError("Selected Light render differs from frozen scene dependencies")
    assembly = service.registry.get("assemblies", job["assembly_id"])
    if (assembly != job["assembly_snapshot"]
            or assembly["conditions"].get("light_id")
            != {"value": scene["illumination_id"], "unit": "1"}):
        raise ValueError("Selected Light render differs from frozen whole-scene Assembly")
    if job["status"] == "succeeded":
        records = [artifact for artifact in job["artifacts"]
                   if artifact["name"] == "renderer-summary.json"]
        if len(records) != 1:
            raise ValueError("Selected Light render lacks one renderer summary")
        summary = json.loads(service.registry.blob_path(records[0]["sha256"]).read_bytes())
        if (summary.get("light_component") != expected
                or job["result"]["summary"] != summary):
            raise ValueError("Rendered Light component differs from frozen selection")


def verify_light_component_pair(service, first_job_id, second_job_id, *,
                                all_light_job_id=None):
    """Bind two actual renders to one two-Light scene, camera and instant.

    A finite-spp all-light render is a diagnostic, not an exact sum oracle.
    The two selected Light assets must account for every placed emitter.
    """
    jobs = [get_job(service, identity) for identity in (first_job_id, second_job_id)]
    if all_light_job_id is not None:
        jobs.append(get_job(service, all_light_job_id))
    if len({job["id"] for job in jobs}) != len(jobs):
        raise ValueError("Dual-light comparison requires distinct render jobs")
    if any(job["status"] != "succeeded" for job in jobs):
        raise ValueError("Dual-light comparison requires completed 3D renders")
    for job in jobs:
        _verify_motion_request(service, job)
        _verify_light_component_job(service, job)
        projection_radiometry.verify_job(service, job)
    selected = [job["request"].get("light_component_id") for job in jobs[:2]]
    scene = jobs[0]["request"]["scene"]
    if (None in selected or selected[0] == selected[1]
            or set(selected) != placed_light_ids(scene["payload"])):
        raise ValueError("Dual-light pair must account for exactly two placed Light assets")
    if len(jobs) == 3 and "light_component_id" in jobs[2]["request"]:
        raise ValueError("All-light comparison must retain every emitter")
    def common_request(job):
        request = deepcopy(job["request"])
        request.pop("name", None)
        request.pop("light_component_id", None)
        return canonical(request)

    base = common_request(jobs[0])
    for job in jobs[1:]:
        if (common_request(job) != base
                or job["environment"] != jobs[0]["environment"]
                or job["backend"] != jobs[0]["backend"]
                or job["assembly_snapshot"] != jobs[0]["assembly_snapshot"]
                or job["lens_snapshot"] != jobs[0]["lens_snapshot"]
                or job.get("projection_evidence") != jobs[0].get("projection_evidence")):
            raise ValueError("Dual-light renders must share one frozen scene, camera and time")
    for name in ("first-hit-depth-m.npy", "first-hit-valid.npy",
                 "first-hit-object-index.npy"):
        hashes = []
        for job in jobs:
            matching = [a for a in job["artifacts"] if a["name"] == name]
            if len(matching) != 1:
                raise ValueError("Dual-light first-hit evidence is missing or ambiguous")
            service.registry.blob_path(matching[0]["sha256"])
            hashes.append(matching[0]["sha256"])
        if len(set(hashes)) != 1:
            raise ValueError("Dual-light first-hit geometry differs between render passes")
    result = {
        "schema": "registered_3d_light_component_pair_v1",
        "scene_asset_id": scene["id"],
        "whole_scene_illumination_id": scene["illumination_id"],
        "render_time_ns": jobs[0]["request"].get("render_time_ns"),
        "common_request_sha256": hashlib.sha256(base.encode()).hexdigest(),
        "components": [
            {"light_asset_id": identity, "render_job_id": job["id"],
             "render_job_sha256": job["content_hash"]}
            for identity, job in zip(selected, jobs[:2], strict=True)
        ],
        "all_light_job_id": jobs[2]["id"] if len(jobs) == 3 else None,
        "finite_sample_superposition_error": "not_bounded_by_this_pair_check",
        "physical_validation": "not_established",
    }
    result["pair_sha256"] = digest(result)
    return result


def verify_light_component_sequences(service, first_sequence_id, second_sequence_id):
    """Reconstruct pair provenance from two immutable registered 3D timelines."""
    from . import scene_motion_sampling

    if first_sequence_id == second_sequence_id:
        raise ValueError("Dual-light sequences must be distinct")
    records = [service.registry.get("scene_sequences", identity)
               for identity in (first_sequence_id, second_sequence_id)]
    series = [scene_motion_sampling._verified_series(service, record)
              for record in records]
    times = [[item["time_ns"] for item in record["payload"]["knots"]]
             for record in records]
    if (times[0] != times[1]
            or records[0]["payload"]["clock"] != records[1]["payload"]["clock"]):
        raise ValueError("Dual-light timelines must share exact render times and clock")
    pairs = [verify_light_component_pair(
        service, first["render_job_snapshot"]["id"],
        second["render_job_snapshot"]["id"],
    ) for first, second in zip(*series, strict=True)]
    first_components = pairs[0]["components"]
    for pair in pairs[1:]:
        if ([part["light_asset_id"] for part in pair["components"]]
                != [part["light_asset_id"] for part in first_components]
                or pair["scene_asset_id"] != pairs[0]["scene_asset_id"]):
            raise ValueError("Dual-light timelines switch Light assets or scene")
    result = {
        "schema": "registered_3d_dual_light_timelines_v1",
        "scene_asset_id": pairs[0]["scene_asset_id"],
        "whole_scene_illumination_id": pairs[0]["whole_scene_illumination_id"],
        "sequence_ids": [first_sequence_id, second_sequence_id],
        "light_asset_ids": [part["light_asset_id"] for part in first_components],
        "render_times_ns": times[0],
        "pair_sha256": [pair["pair_sha256"] for pair in pairs],
        "inter_sample_occlusion_error": "unknown; compare a refined registered series",
        "physical_validation": "not_established",
    }
    result["timelines_sha256"] = digest(result)
    return result


def project_radiance(radiance, projection):
    """Apply the declared on-axis projection once; PSF and RI remain separate."""
    return projection_radiometry.project_radiance(radiance, projection)


def _verify_cached_projection(service, job):
    """New absolute projections must not return missing or stale cached irradiance."""
    projection_radiometry.verify_job(service, job)
    named = {}
    for artifact in job["artifacts"]:
        service.registry.blob_path(artifact["sha256"])
        named.setdefault(artifact["name"], []).append(artifact)
    for name in ("spectral-radiance.npy", "spectral-image.npy"):
        if len(named.get(name, [])) != 1:
            raise ValueError("Rendered projection requires exactly one " + name)
    radiance, irradiance = [imaging.load_spectral_npy(
        service.registry.blob_path(named[name][0]["sha256"]).read_bytes()
    ) for name in ("spectral-radiance.npy", "spectral-image.npy")]
    if not np.array_equal(irradiance, project_radiance(radiance, job["request"]["projection"])):
        raise ValueError("Rendered irradiance differs from its verified absolute projection")


def _verify_carrier_halo(service, job):
    """Check that expanded native support still names its exact Sensor response pair."""
    request = job["request"]
    recorded = request.get("carrier_halo")
    if recorded is None:
        return
    from .native_model_evidence import verify_models
    from .runtime import sensor_response_fields
    from .spatial import validate_psf, validate_psf_coverage

    sensor = job.get("sensor_snapshot")
    if not sensor or service.registry.get("models", sensor["id"]) != sensor:
        raise ValueError("Carrier halo Sensor differs from its frozen model revision")
    assembly = job["assembly_snapshot"]
    lens = job["lens_snapshot"]
    if (service.registry.get("assemblies", assembly["id"]) != assembly
            or service.registry.get("models", lens["id"]) != lens
            or sensor["id"] not in assembly["model_ids"]
            or lens["id"] not in assembly["model_ids"]):
        raise ValueError("Carrier halo models differ from the pinned Assembly")
    artifacts = verify_models(service, [sensor])
    if any(artifact not in job["input_artifacts"] for artifact in artifacts):
        raise ValueError("Carrier halo response artifacts are missing from the frozen render")
    response = sensor_response_fields(sensor["payload"])
    if "charge_collection" not in response:
        raise ValueError("Carrier halo requires pinned Sensor charge_collection")
    collection = response["charge_collection"]
    psf = validate_psf(lens["payload"]["psf"])
    capture = request["capture_crop"]
    full = request["sampling"]["full_sensor_shape"]
    pitch, native = imaging.native_sensor_grid(sensor)
    if (full != native or request["sampling"]["pixel_pitch_um"] != pitch
            or request["wavelength_nm"] != sensor["payload"]["wavelength_nm"]
            or psf["wavelength_nm"] != request["wavelength_nm"]):
        raise ValueError("Carrier halo native sampling differs from the pinned Sensor")
    generation_crop = _carrier_generation_crop(
        full, capture["crop_origin"], capture["shape"],
        collection["destination_offsets_rc"],
    )
    shape = list(np.shape(psf["values"])[-2:])
    retained = request.get("source_pixel_quadrature", {}).get("model") == subcell_optics.MODEL
    support_shape = [side + 2 for side in shape] if retained else shape
    origin, source_shape = _halo_source_crop(
        full, capture["crop_origin"], capture["shape"], support_shape, generation_crop
    )
    expected = {
        "sensor_model_id": sensor["id"],
        "sensor_model_sha256": sensor["content_hash"],
        "charge_collection_sha256": sensor["response_artifacts"]["charge_collection"]["sha256"],
        "destination_offsets_rc": collection["destination_offsets_rc"],
        "generation_crop": {
            "crop_origin": generation_crop[0], "shape": generation_crop[1],
        },
        "boundary": collection["boundary"],
    }
    expected_psf_halo = {
        "kernel_shape": shape,
        "kernel_source": psf["source"],
        "support_policy": (
            "native_sensor_clipped_subcell_shift_psf_and_carrier_support"
            if retained else
            "native_sensor_clipped_psf_and_carrier_support_with_even_source_extent"
        ),
    }
    if (recorded != expected or request["crop_origin"] != origin
            or request["shape"] != source_shape
            or request["sampling"]["crop_origin"] != origin
            or request["psf_halo"] != expected_psf_halo):
        raise ValueError("Carrier/PSF source support differs from the frozen model pair")
    validate_psf_coverage(psf, full, origin, source_shape,
                          request["sampling"]["pixel_pitch_um"])


def store_previews(service, radiance, depth, valid, wave):
    """Display-only fixed scale across wavelengths; floating arrays stay authoritative."""
    from PIL import Image

    maximum = float(radiance.max())
    near = float(depth[valid].min()) if valid.any() else None
    far = float(depth[valid].max()) if valid.any() else None
    previews, artifacts = [], []
    for index, wavelength in enumerate(wave):
        normalized = radiance[..., index] / maximum if maximum else radiance[..., index]
        image = np.rint(np.clip(normalized, 0, 1) * 255).astype(np.uint8)
        stream = io.BytesIO()
        Image.fromarray(image).save(stream, format="PNG")
        artifact = service.registry.store_blob(
            stream.getvalue(), f"radiance-band-{index:02d}.png", "image/png"
        )
        artifacts.append(artifact)
        previews.append(
            {
                "kind": "spectral_radiance",
                "wavelength_nm": wavelength,
                "artifact": artifact,
                "display_min": 0.0,
                "display_max": maximum,
                "unit": "W/m^2/sr/nm",
            }
        )
    normalized = np.zeros_like(depth)
    if valid.any():
        normalized[valid] = (depth[valid] - near) / (far - near) if far > near else 0.5
    rgb = np.zeros((*depth.shape, 3), dtype=np.uint8)
    rgb[..., 0] = np.rint(normalized * 255).astype(np.uint8)
    rgb[..., 1] = np.rint((1 - normalized) * 200).astype(np.uint8)
    rgb[..., 2] = 160
    rgb[~valid] = 0
    stream = io.BytesIO()
    Image.fromarray(rgb).save(stream, format="PNG")
    artifact = service.registry.store_blob(
        stream.getvalue(), "first-hit-depth-preview.png", "image/png"
    )
    artifacts.append(artifact)
    previews.append(
        {
            "kind": "first_hit_depth",
            "artifact": artifact,
            "display_min": near,
            "display_max": far,
            "unit": "m",
            "missing_colour": "black",
        }
    )
    return {"scale": "linear_display_only", "previews": previews}, artifacts


def check_auxiliary_outputs(radiance, depth, valid, objects, error, instance_count):
    """Reject malformed auxiliary maps before displaying or publishing evidence."""
    shape = radiance.shape[:2]
    if (
        depth.shape != shape
        or valid.shape != shape
        or objects.shape != shape
        or error.shape != radiance.shape
        or valid.dtype not in (np.dtype(bool), np.dtype("uint8"))
        or not np.isin(valid, [0, 1]).all()
        or not np.issubdtype(objects.dtype, np.integer)
    ):
        raise ValueError("Renderer auxiliary maps differ from the frozen grid or types")
    valid = valid.astype(bool)
    if (
        not np.isfinite(depth).all()
        or not np.isfinite(error).all()
        or np.any(error < 0)
        or np.any(depth[valid] <= 0)
        or np.any(depth[~valid] != 0)
        or np.any(objects[~valid] != -1)
        or np.any(objects[valid] < 0)
        or np.any(objects[valid] >= instance_count)
    ):
        raise ValueError("Renderer auxiliary maps contain invalid distances, objects or errors")
    return valid


def _retained_subcells(service, job, radiance=None):
    """Read bounded immutable subcell evidence; legacy renders have none."""
    quadrature = job["request"].get("source_pixel_quadrature")
    if not quadrature or quadrature["model"] != subcell_optics.MODEL:
        return None
    named = {name: [a for a in job["artifacts"] if a["name"] == name] for name in (
        "source-subcell-radiance.npy", "source-subcell-mc-standard-error.npy",
    )}
    if any(len(found) != 1 for found in named.values()):
        raise ValueError("Retained source-subcell artifacts are missing or ambiguous")
    values = []
    for found in named.values():
        artifact = found[0]
        if artifact["bytes"] > 8 * 1024 * 1024:
            raise ValueError("Retained source-subcell artifact exceeds 8 MiB")
        content = service.registry.blob_path(artifact["sha256"]).read_bytes()
        if len(content) != artifact["bytes"]:
            raise ValueError("Retained source-subcell artifact size differs")
        values.append(np.load(io.BytesIO(content), allow_pickle=False))
    if radiance is None:
        primary = next(a for a in job["artifacts"] if a["name"] == "spectral-radiance.npy")
        radiance = imaging.load_spectral_npy(service.registry.blob_path(
            primary["sha256"]).read_bytes())
    subcells = subcell_optics.validate_subcells(values[0], radiance, quadrature["grid_side"])
    error = values[1]
    if (error.shape != subcells.shape or error.dtype.kind not in "uif"
            or not np.isfinite(error).all() or np.any(error < 0)):
        raise ValueError("Retained source-subcell standard error has invalid shape or values")
    standard = next(a for a in job["artifacts"]
                    if a["name"] == "radiance-mc-standard-error.npy")
    aggregate = imaging.load_spectral_npy(service.registry.blob_path(
        standard["sha256"]).read_bytes())
    reconstructed = np.sqrt(np.sum((error / quadrature["grid_side"]**2)**2, axis=(2, 3)))
    if not np.allclose(reconstructed, aggregate, rtol=2e-14, atol=1e-300):
        raise ValueError("Retained source-subcell standard error differs from aggregate")
    return subcells


def execute(service, job):
    if job["environment"] != imaging._current(service) or job["backend"] != backend_identity():
        raise ValueError("Scene Worker source/backend differs from the frozen execution")
    projection_radiometry.verify_job(service, job)
    _verify_motion_request(service, job)
    _verify_light_component_job(service, job)
    _verify_carrier_halo(service, job)
    for a in job["input_artifacts"]:
        service.registry.blob_path(a["sha256"])
    artifacts = list(job["input_artifacts"])
    script = Path(__file__).with_name("mitsuba_scene.py")
    with tempfile.TemporaryDirectory(prefix="camera-scene-") as work:
        directory = Path(work)
        source = directory / "input.json"
        source.write_text(canonical(job["request"]))
        process = subprocess.run(
            [job["backend"]["executable"], "-I", str(script), str(source), str(directory)],
            capture_output=True,
            timeout=110,
        )
        log = service.registry.store_blob(
            process.stdout + b"\n" + process.stderr, "renderer.log", "text/plain"
        )
        artifacts.append(log)
        if process.returncode:
            from .factory import FactoryExecutionError

            raise FactoryExecutionError(
                "Mitsuba scene rendering failed; see renderer.log", artifacts
            )
        radiance = imaging.load_spectral_npy((directory / "spectral-radiance.npy").read_bytes())
        request = job["request"]
        if list(radiance.shape) != [*request["shape"], len(request["wavelength_nm"])]:
            raise ValueError("Renderer output shape differs from frozen coordinates")
        depth = np.load(directory / "first-hit-depth-m.npy", allow_pickle=False)
        valid = np.load(directory / "first-hit-valid.npy", allow_pickle=False)
        objects = np.load(directory / "first-hit-object-index.npy", allow_pickle=False)
        standard_error = np.load(directory / "radiance-mc-standard-error.npy", allow_pickle=False)
        valid = check_auxiliary_outputs(
            radiance,
            depth,
            valid,
            objects,
            standard_error,
            len(request["scene"]["payload"]["instances"]),
        )
        retained = request.get("source_pixel_quadrature", {}).get("model") == subcell_optics.MODEL
        if retained:
            subcell_optics.validate_subcells(
                np.load(directory / "source-subcell-radiance.npy", allow_pickle=False),
                radiance, request["source_pixel_quadrature"]["grid_side"],
            )
            sub_error = np.load(
                directory / "source-subcell-mc-standard-error.npy", allow_pickle=False
            )
            if (sub_error.dtype.kind not in "uif"
                    or sub_error.shape != (
                        *request["shape"], request["source_pixel_quadrature"]["grid_side"],
                        request["source_pixel_quadrature"]["grid_side"],
                        len(request["wavelength_nm"]),
                    )
                    or not np.isfinite(sub_error).all() or np.any(sub_error < 0)
                    or not np.allclose(
                        np.sqrt(np.sum((sub_error / request["source_pixel_quadrature"][
                            "grid_side"]**2)**2, axis=(2, 3))),
                        standard_error, rtol=2e-14, atol=1e-300,
                    )):
                raise ValueError("Renderer retained subcell standard error differs from mean")
        previews, preview_artifacts = store_previews(
            service, radiance, depth, valid, request["wavelength_nm"]
        )
        artifacts.extend(preview_artifacts)
        irradiance = project_radiance(radiance, request["projection"])
        stream = io.BytesIO()
        np.save(stream, irradiance, allow_pickle=False)
        artifacts.append(
            service.registry.store_blob(
                stream.getvalue(), "spectral-image.npy", "application/octet-stream"
            )
        )
        for name in [
            "spectral-radiance.npy",
            "radiance-mc-standard-error.npy",
            "first-hit-depth-m.npy",
            "first-hit-valid.npy",
            "first-hit-object-index.npy",
            "renderer-summary.json",
            *(["source-subcell-radiance.npy", "source-subcell-mc-standard-error.npy"]
              if retained else []),
        ]:
            artifacts.append(
                service.registry.store_blob(
                    (directory / name).read_bytes(),
                    name,
                    "application/json" if name.endswith(".json") else "application/octet-stream",
                )
            )
        summary = json.loads((directory / "renderer-summary.json").read_text())
        if summary.get("light_component") != _light_component_evidence(request):
            raise ValueError("Renderer Light component differs from frozen scene selection")
        if (request.get("scene_motion_evidence") is not None
                and summary.get("scene_motion") != request["scene_motion_evidence"]):
            raise ValueError("Renderer motion evidence differs from frozen scene time")
        quadrature = request.get("source_pixel_quadrature")
        if quadrature:
            measured = summary.get("source_pixel_quadrature")
            side = quadrature["grid_side"]
            expected_paths = (
                int(np.prod(request["shape"])) * len(request["wavelength_nm"])
                * request["samples_per_pixel"] * side**2
            )
            expected_centres = [
                [(sub_y + 0.5) / side, (sub_x + 0.5) / side]
                for sub_y in range(side) for sub_x in range(side)
            ]
            if (
                not isinstance(measured, dict)
                or measured.get("model") != quadrature["model"]
                or measured.get("grid_side") != side
                or measured.get("integrator_path_samples") != expected_paths
                or measured.get("stochastic_paths_per_subcell_wavelength")
                != request["samples_per_pixel"]
                or measured.get("sampler_seed") != request["seed"]
                or measured.get("sampler_order")
                != "crop_row_column_subcell_row_column_wavelength_path"
                or measured.get("subcell_centres_yx") != expected_centres
                or measured.get("subcell_weight") != 1.0 / side**2
                or measured.get("subcell_coordinates")
                != "native_sensor_pixel_yx_from_top_left"
                or measured.get("projection_geometry_interpretation")
                != "pinhole_pixel_centre_reference_with_subcell_offsets"
                or measured.get("source_cell_representation") != (
                    "area_mean_with_retained_subcell_radiance" if retained
                    else "area_mean_radiance_assigned_to_native_cell_centre"
                )
                or measured.get("first_hit_auxiliary") != "pixel_centre_ray_only"
                or measured.get("spatial_quadrature_error") != "unknown"
                or measured.get("continuous_scene_psf_integration") is not False
                or measured.get("receiver_aperture_filter_applied_here") is not False
            ):
                raise ValueError("Renderer source-pixel quadrature differs from frozen request")
            if retained and any(measured.get(key) != value for key, value in {
                "retained_radiance_artifact": "source-subcell-radiance.npy",
                "retained_standard_error_artifact": "source-subcell-mc-standard-error.npy",
                "fractional_source_psf_model": subcell_optics.TRANSFER,
            }.items()):
                raise ValueError("Renderer retained-subcell semantics differ from request")
    metadata = imaging.spectral_template(service, job["assembly_snapshot"]["id"])
    metadata.update(
        name=job["name"],
        source_kind="simulated",
        source_uri="scene-render://" + job["id"],
        license="Derived from declared scene asset rights; see frozen sources",
        method_and_version=PLUGIN_ID,
        sampling=request["sampling"],
    )
    return {
        "summary": summary,
        "display": previews,
        "spectral_image_metadata": metadata,
        "artifacts": artifacts,
        "projection": request["projection"],
        **({
            "capture_crop": request["capture_crop"],
            "psf_source_support": {
                "sampling": request["sampling"],
                "shape": request["shape"],
                "psf_halo": request["psf_halo"],
                **({"carrier_halo": request["carrier_halo"]}
                   if "carrier_halo" in request else {}),
            },
        } if "capture_crop" in request else {}),
        **projection_radiometry.summary(request["projection"]),
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
    }


def publication_evidence(service, identity, metadata, content):
    job = get_job(service, identity)
    if job["status"] != "succeeded":
        raise ValueError("Only completed scene renders can be published")
    projection_radiometry.verify_job(service, job)
    _verify_motion_request(service, job)
    _verify_light_component_job(service, job)
    _verify_carrier_halo(service, job)
    if job["request"]["projection"]["projection_model"] == projection_radiometry.MODEL:
        _verify_cached_projection(service, job)
    if job["request"].get("source_pixel_quadrature", {}).get("model") == subcell_optics.MODEL:
        from .scene_projection import _verify_renderer_sampling

        recorded = next(a for a in job["artifacts"] if a["name"] == "renderer-summary.json")
        summary = json.loads(service.registry.blob_path(recorded["sha256"]).read_bytes())
        if summary != job["result"]["summary"]:
            raise ValueError("Retained subcell renderer summary differs from immutable result")
        _verify_renderer_sampling(job["request"], summary)
    _retained_subcells(service, job)
    expected = job["result"]["spectral_image_metadata"]
    expected = {**expected, "name": metadata["name"], "revision": metadata["revision"]}
    if SpectralImageImport.model_validate(expected).model_dump() != metadata:
        raise ValueError("Rendered image metadata must preserve the frozen physical contract")
    artifact = next(a for a in job["artifacts"] if a["name"] == "spectral-image.npy")
    if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
        raise ValueError("Published irradiance differs from the actual rendering")
    for a in job["artifacts"]:
        service.registry.blob_path(a["sha256"])
    return job


def publish(service, payload):
    imaging._current(service)
    data = RenderPublish.model_validate(payload).model_dump()
    job = get_job(service, data["job_id"])
    if job["status"] != "succeeded":
        raise ValueError("Only completed scene renders can be published")
    metadata = {
        **job["result"]["spectral_image_metadata"],
        "name": data["name"],
        "revision": data["revision"],
    }
    artifact = next(a for a in job["artifacts"] if a["name"] == "spectral-image.npy")
    return imaging.import_spectral(
        service,
        metadata,
        service.registry.blob_path(artifact["sha256"]).read_bytes(),
        render_job_id=job["id"],
    )
