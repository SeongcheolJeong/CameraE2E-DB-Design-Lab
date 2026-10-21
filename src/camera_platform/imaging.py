"""Immutable spectral-image inputs and RAW-preserving ISP replay orchestration."""

from __future__ import annotations

import copy
import io
import math

import numpy as np

from .contracts import (
    AssetCreate,
    ModelCreate,
    ReplayCreate,
    SpectralFixtureCreate,
    SpectralImageImport,
)
from .registry import ConflictError, canonical, digest
from .runtime import sensor_response_fields, with_sensor_effects

MAX_IMAGE_BYTES = 64 * 1024 * 1024


def _current(service):
    from .service import environment_fingerprint

    if service.loaded_environment != environment_fingerprint():
        raise ConflictError("Executable source changed after startup; restart the API")
    return environment_fingerprint()


def _model_payload(record):
    return {k: copy.deepcopy(v) for k, v in record.items() if k in ModelCreate.model_fields}


def resolve_isp(model, *, allow_v2=False, allow_v3=False):
    from .isp import default_profile, validate_profile

    if model["kind"] != "isp":
        raise ValueError("Select an ISP model revision")
    if model["payload"].get("schema") == "camera_isp_profile_v3":
        if not allow_v3:
            raise ValueError("Charge-domain ISP v3 requires calibrated nonlinear RAW replay")
        from .isp_v3 import validate_profile as validate_v3

        return validate_v3(model["payload"])
    if model["payload"].get("schema") == "camera_isp_profile_v2":
        if not allow_v2:
            raise ValueError("Advanced ISP v2 requires retained native RAW replay")
        from .isp_v2 import validate_profile as validate_v2

        return validate_v2(model["payload"])
    legacy = {
        "implementation": "pyisetcam",
        "mode": "fixed",
        "awb": "fixed",
        "lens_shading_correction": "bypass",
    }
    return default_profile() if model["payload"] == legacy else validate_profile(model["payload"])


def isp_template(service, assembly_id=None):
    from .isp import default_profile

    assembly = (
        service.registry.get("assemblies", assembly_id)
        if assembly_id
        else service.registry.list("assemblies")[-1]
    )
    model = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "isp"
    )
    result = _model_payload(model)
    result.update(
        name="명시적 WB·CCM ISP 프로파일",
        revision="profile-1",
        parents=[model["id"]],
        payload=default_profile(),
        source_kind="estimated",
        source_uri="fixture://declared-software-isp-profile",
        method_and_version="camera_isp_profile_v1:declared_tuning",
    )
    return result


def _native(service, assembly_id):
    assembly = (
        service.registry.get("assemblies", assembly_id)
        if assembly_id
        else service.registry.list("assemblies")[-1]
    )
    sensor = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    pitch, shape = native_sensor_grid(sensor)
    return assembly, sensor, pitch, shape


def native_sensor_grid(sensor):
    """Resolve native sampling without requiring a persisted Assembly."""
    pitch = sensor["payload"]["pixel_pitch_um"]
    shape = []
    for dimension in ("active_height", "active_width"):
        quantity = sensor["properties"][dimension]
        if quantity["unit"] != "mm":
            raise ValueError("Sensor active dimensions must be declared in mm")
        count = quantity["value"] * 1000 / pitch
        if abs(count - round(count)) > 0.1:
            raise ValueError("Native sensor dimensions do not determine an integer raster")
        shape.append(round(count))
    return pitch, shape


def spectral_template(service, assembly_id=None):
    assembly, sensor, pitch, full = _native(service, assembly_id)
    return {
        "name": "분광 이상 영상 · 원본 단위와 기준면을 확인하세요",
        "revision": "1",
        "asset_id": None,
        "source_kind": "estimated",
        "source_uri": "fixture://spectral-image-input",
        "license": "Declare actual source rights",
        "method_and_version": "declared-spectral-ideal-image:1",
        "physical_instance": None,
        "parents": [],
        "wavelength_nm": sensor["payload"]["wavelength_nm"],
        "unit": "W/m^2/nm",
        "reference_plane": "sensor_ideal_image_before_psf_ri",
        "sampling": {
            "kind": "native_sensor",
            "full_sensor_shape": full,
            "crop_origin": [max(0, (n - 64) // 2) for n in full],
            "pixel_pitch_um": pitch,
        },
        "included_effects": ["on_axis_lens_throughput"],
        "conditions": copy.deepcopy(assembly["conditions"]),
    }


def fixture_template(service, assembly_id=None):
    assembly = (
        service.registry.get("assemblies", assembly_id)
        if assembly_id
        else service.registry.list("assemblies")[-1]
    )
    lens = next(service.registry.get("models", i) for i in assembly["model_ids"]
                if service.registry.get("models", i)["kind"] == "lens")
    if lens["payload"].get("radiometry", {}).get("object_conjugate"):
        from .finite_plane import template

        return template(service, assembly["id"])
    return SpectralFixtureCreate(assembly_id=assembly["id"]).model_dump()


def load_spectral_npy(content):
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError("Spectral NPY must be nonempty and no larger than 64 MiB")
    stream = io.BytesIO(content)
    version = np.lib.format.read_magic(stream)
    if version == (1, 0):
        shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
    elif version == (2, 0):
        shape, fortran, dtype = np.lib.format.read_array_header_2_0(stream)
    else:
        raise ValueError("Spectral input supports NPY versions 1 and 2")
    if (
        dtype.hasobject
        or dtype.fields
        or dtype.kind != "f"
        or dtype.itemsize not in (4, 8)
        or fortran
    ):
        raise ValueError(
            "Spectral image requires C-order float32/64 samples, never objects or RGB inference"
        )
    if (
        len(shape) != 3
        or not 2 <= shape[2] <= 16
        or not all(8 <= n <= 512 and n % 2 == 0 for n in shape[:2])
    ):
        raise ValueError(
            "Spectral image shape must be [8..512 even rows,8..512 even columns,2..16 wavelengths]"
        )
    count = math.prod(shape) * dtype.itemsize
    if count > MAX_IMAGE_BYTES or stream.tell() + count != len(content):
        raise ValueError("Spectral NPY bytes disagree with its bounded header")
    array = np.load(io.BytesIO(content), allow_pickle=False)
    if not np.isfinite(array).all() or np.any(array < 0):
        raise ValueError("Spectral irradiance must be finite and nonnegative")
    return array


def import_spectral(service, metadata, content, *, render_job_id=None, projection_job_id=None):
    data = SpectralImageImport.model_validate(metadata).model_dump()
    if (data["source_uri"].startswith("finite-plane://")
            or data["method_and_version"].startswith("authored_finite_plane_projection_v1")):
        raise ValueError("Finite plane provenance requires the verified finite-plane creation path")
    image = load_spectral_npy(content)
    render_job = None
    projection_job = None
    if render_job_id and projection_job_id:
        raise ValueError("Select one verified image production path")
    if not projection_job_id and (
        data["source_uri"].startswith("scene-projection://")
        or data["method_and_version"].startswith("stored_spectral_radiance_projection_v1")
    ):
        raise ValueError("Projection provenance requires a verified radiance projection job")
    if projection_job_id:
        from .scene_projection import publication_evidence as projection_evidence

        projection_job = projection_evidence(service, projection_job_id, data, content)
    if not render_job_id and (
        data["source_uri"].startswith("scene-render://")
        or data["method_and_version"].startswith("mitsuba_fixed_wavelength_scene_v1")
    ):
        raise ValueError("Platform renderer provenance requires a verified scene rendering job")
    if render_job_id:
        from .scene_render import publication_evidence

        render_job = publication_evidence(service, render_job_id, data, content)
    wave = np.asarray(data["wavelength_nm"])
    if (
        len(wave) != image.shape[2]
        or not np.isfinite(wave).all()
        or not np.all(np.diff(wave) > 0)
        or wave[0] < 200
        or wave[-1] > 2500
    ):
        raise ValueError(
            "Spectral axis must match samples and strictly increase within [200,2500] nm"
        )
    sampling = data["sampling"]
    if (
        set(sampling) != {"kind", "full_sensor_shape", "crop_origin", "pixel_pitch_um"}
        or sampling["kind"] != "native_sensor"
    ):
        raise ValueError("Declare native sensor shape, crop origin and pixel pitch")
    full, origin = sampling["full_sensor_shape"], sampling["crop_origin"]
    if not all(
        isinstance(a, list) and len(a) == 2 and all(type(n) is int for n in a)
        for a in (full, origin)
    ):
        raise ValueError("Native geometry requires integer coordinate pairs")
    if any(
        f < 2 or f > 1_000_000 or o < 0 or o + n > f
        for f, o, n in zip(full, origin, image.shape[:2], strict=True)
    ):
        raise ValueError("Spectral crop lies outside native sensor")
    pitch = sampling["pixel_pitch_um"]
    if type(pitch) not in (int, float) or not 0.1 <= pitch <= 1000:
        raise ValueError("Pixel pitch must be finite in [.1,1000] um")
    if data["included_effects"] != ["on_axis_lens_throughput"]:
        raise ValueError(
            "Ideal image must include only on-axis throughput, before PSF and relative illumination"
        )
    from .factory import application_domain

    application_domain(data["conditions"])
    if projection_job:
        if data["asset_id"]:
            raise ValueError("Projection publication creates its own immutable derived scene")
        original = service.registry.store_blob(
            content, "spectral-image.npy", "application/octet-stream"
        )
        artifacts = [original, service.registry.store_blob(
            canonical(data).encode(), "spectral-image-metadata.json", "application/json"
        ), *projection_job["artifacts"]]
        asset_data = AssetCreate.model_validate({
            **{k: data[k] for k in (
                "name", "revision", "source_kind", "source_uri", "license",
                "method_and_version", "physical_instance", "parents",
            )},
            "kind": "scene",
            "description": "Stored spectral radiance with a new explicit optical projection",
        }).model_dump() | {"approval_status": "draft"}
        image_data = {
            **data, "shape": list(image.shape), "artifacts": artifacts,
            "projection_job_snapshot": projection_job,
            "source_render_snapshot": projection_job["source_render_snapshot"],
            "projection_lens_model_id": projection_job["lens_snapshot"]["id"],
            "approval_status": "draft", "physical_validation": "not_established",
        }
        publication_key = digest({
            "metadata": data, "image_sha256": original["sha256"],
            "projection_job_sha256": digest(projection_job),
        })
        return service.registry.publish_spectral_image(publication_key, asset_data, image_data)
    if data["asset_id"]:
        asset = service.registry.get("assets", data["asset_id"])
        if asset["kind"] != "scene":
            raise ValueError("Spectral image requires a scene source asset")
    else:
        asset = service.create_asset(
            {
                k: data[k]
                for k in (
                    "name",
                    "revision",
                    "source_kind",
                    "source_uri",
                    "license",
                    "method_and_version",
                    "physical_instance",
                    "parents",
                )
            }
            | {
                "kind": "scene",
                "description": "Mitsuba spectral radiance with declared paraxial projection"
                if render_job
                else "Supplied spectral ideal-image irradiance; "
                "no reconstructed RGB spectrum or 3D renderer",
            }
        )
        data["asset_id"] = asset["id"]
    artifacts = [
        service.registry.store_blob(content, "spectral-image.npy", "application/octet-stream"),
        service.registry.store_blob(
            canonical(data).encode(), "spectral-image-metadata.json", "application/json"
        ),
    ]
    return service.registry.put(
        "spectral_images",
        {
            **data,
            "shape": list(image.shape),
            "artifacts": artifacts + (render_job["artifacts"] if render_job else []),
            "asset_snapshot": asset,
            **(
                {
                    "render_job_snapshot": render_job,
                    "projection_lens_model_id": render_job["lens_snapshot"]["id"],
                }
                if render_job
                else {}
            ),
            "approval_status": "draft",
            "physical_validation": "not_established",
        },
    )


def create_fixture(service, payload):
    data = SpectralFixtureCreate.model_validate(payload).model_dump()
    metadata = spectral_template(service, data["assembly_id"])
    rows, cols = data["shape"]
    if any(n < 8 or n > 256 or n % 2 for n in (rows, cols)):
        raise ValueError("Fixture shape requires even sides in [8,256]")
    full = metadata["sampling"]["full_sensor_shape"]
    if any(n > f for n, f in zip((rows, cols), full, strict=True)):
        raise ValueError("Fixture exceeds native sensor")
    metadata["sampling"]["crop_origin"] = [
        (f - n) // 2 for f, n in zip(full, (rows, cols), strict=True)
    ]
    metadata.update(
        name=data["name"],
        revision=data["revision"],
        source_uri="fixture://authored-spectral-" + data["pattern"],
        license="MIT; authored synthetic image-plane fixture",
        method_and_version="native-spectral-fixture:1",
    )
    wave = np.asarray(metadata["wavelength_nm"])
    y, x = np.indices((rows, cols))
    if data["pattern"] == "spectral_chart":
        reflectance = np.asarray(
            [
                np.full_like(wave, 0.75, dtype=float),
                0.05 + 0.7 * np.exp(-(((wave - 620) / 45) ** 2)),
                0.05 + 0.7 * np.exp(-(((wave - 540) / 40) ** 2)),
                0.05 + 0.7 * np.exp(-(((wave - 460) / 35) ** 2)),
            ]
        )
        image = reflectance[((x // max(1, cols // 4)) + (y // max(1, rows // 4))) % 4]
    else:
        mask = np.ones((rows, cols))
        if data["pattern"] == "slanted_edge":
            mask = np.where(x > cols / 2 + 0.1 * (y - rows / 2), 0.9, 0.03)
        if data["pattern"] == "point":
            mask = ((x == cols // 2) & (y == rows // 2)).astype(float)
        image = np.broadcast_to(mask[..., None], (rows, cols, len(wave))).copy()
    image = np.asarray(image * data["irradiance_scale"], dtype="<f8")
    stream = io.BytesIO()
    np.save(stream, image, allow_pickle=False)
    return import_spectral(service, metadata, stream.getvalue())


def _format(raw):
    return {
        "cfa": raw["cfa"],
        "bit_depth": raw["bit_depth"],
        "black_level_dn": raw.get("black_level_dn", raw.get("black_level")),
        "response_type": raw.get("response_type", "linear"),
        "packing": raw.get("packing", "unpacked"),
        "layout": raw.get("layout", "bayer"),
        "hdr_streams": raw.get("hdr_streams", 1),
    }


def replay_source(service, source_kind, identity):
    temporal = {}
    if source_kind == "capture":
        record = service.registry.get("captures", identity)
        if record.get("schema") == "camera_nonlinear_capture_v1":
            raise ValueError(
                "Imported nonlinear RAW requires an explicit response calibration and ISP v3"
            )
        artifact = next(a for a in record["artifacts"] if a["name"] == "raw-capture.npy")
        raw_format = _format(record["observation"]["raw_format"])
        geometry = record["observation"]["geometry"]
        conditions = record.get("conditions", record["observation"].get("conditions"))
        shape = record["raw_shape"]
        if record.get("capture_kind") == "control_response_observation_v1":
            acquisition = record["acquisition"]
            observed = [f for f in acquisition["frames"] if f["raw_index"] is not None]
            temporal = {
                "clock": copy.deepcopy(acquisition["clock"]),
                "frame_records": copy.deepcopy(observed),
                "frame_conditions": [
                    {
                        "frame_id": f["frame_id"],
                        "frame_start_ns": f["frame_start_ns"],
                        "applied_at_ns": f["latch_at_ns"],
                        **(f["applied"] or {}),
                    }
                    for f in observed
                ],
                "acquisition_mode": "closed_loop_observed",
                "response_acquisition": copy.deepcopy(acquisition),
            }
        provenance = {
            k: record.get(k)
            for k in (
                "source_kind",
                "source_uri",
                "physical_instance",
                "method_and_version",
                "license",
            )
        }
    elif source_kind == "run":
        record = service.registry.get_run(identity)
        if record.get("plugin_id") in {"retained_charge_v1", "electrical_control_v1"}:
            raise ValueError(
                "Retained photodiode RAW represents delivered total charge with frame history; "
                "legacy current-exposure ISP replay is unsupported"
            )
        if record.get("plugin_id") == "shared_charge_dcg_v1":
            raise ValueError(
                "DCG retains paired readouts; replay requires explicit single-stream "
                "extraction with its own calibration, which is not implemented"
            )
        if record["status"] != "succeeded" or record.get("job_kind") in {
            "characterization",
            "isp_replay",
        }:
            raise ValueError("Replay source must be a completed camera RAW run")
        artifact = next((a for a in record["artifacts"] if a["name"] == "raw_frames.npy"), None)
        if not artifact:
            raise ValueError("Selected camera run has no preserved RAW frames")
        raw_format = _format(record["assembly_snapshot"]["raw_format"])
        summary = record["result"].get("summary", record["result"])
        program_schema = record.get("capture_program_snapshot", {}).get("payload", {}).get("schema")
        nonlinear_programs = {
            "camera_capture_program_v2", "camera_reference_hdr_capture_program_v2"
        }
        if (program_schema in nonlinear_programs
                or summary.get("raw_format", {}).get("response_type")
                == "nonlinear_charge_voltage"):
            raise ValueError(
                "Nonlinear RAW requires its bound charge-voltage calibration decoder; "
                "direct linear ISP replay is unsupported. Use the calibrated capture preview."
            )
        shape = artifact.get("shape")
        if shape is None:
            from .observations import load_raw_npy

            shape = list(
                load_raw_npy(service.registry.blob_path(artifact["sha256"]).read_bytes()).shape
            )
        geometry = summary.get("geometry") or {
            "kind": "representative_normalized_field_raster",
            "raster_shape": shape[-2:],
            "mapping": "center_to_corner_radius_v1",
        }
        conditions = record["test_snapshot"]["conditions"]
        if record.get("plugin_id") in {"temporal_scene_v1", "hdr_bracket_v1", "reference_hdr_v1"}:
            temporal = {
                "clock": copy.deepcopy(summary["clock"]),
                "frame_conditions": copy.deepcopy(summary["frame_conditions"]),
                "frame_records": copy.deepcopy(summary["frames"]),
                "timing": copy.deepcopy(summary["timing"]),
            }
            if record.get("capture_program_id"):
                temporal["capture_program_id"] = record["capture_program_id"]
        elif record.get("plugin_id") == "closed_loop_scene_v1":
            temporal = {
                "clock": copy.deepcopy(summary["clock"]),
                "frame_conditions": copy.deepcopy(summary["frame_conditions"]),
                "frame_records": copy.deepcopy(summary["frames"]),
                "acquisition_mode": "closed_loop",
                "control_trace": copy.deepcopy(summary["control_trace"]),
                "control_program_id": summary["control_program_id"],
                "control_policy_id": summary["control_policy_id"],
            }
        provenance = {
            "source_kind": "simulated",
            "source_uri": "run://" + identity,
            "physical_instance": None,
            "method_and_version": record["plugin_id"],
        }
    else:
        raise ValueError("Unknown RAW source kind")
    service.registry.blob_path(artifact["sha256"])
    return {
        "source_kind": source_kind,
        "source_id": identity,
        "name": record["name"],
        "raw_shape": shape,
        "raw_sha256": artifact["sha256"],
        "raw_format": raw_format,
        "geometry": geometry,
        "conditions": conditions,
        "source_provenance": provenance,
        "raw_artifact": artifact,
        "source_snapshot": record,
        **temporal,
    }


def replay_sources(service):
    result = []
    for kind, records in (
        ("capture", service.registry.list("captures")),
        ("run", service.list_simulation_runs()),
    ):
        for record in records:
            if kind == "capture" and record.get("schema") == "camera_nonlinear_capture_v1":
                from .isp_capture_source import choices

                result.extend(choices(service, record))
                continue
            if kind == "run" and (
                record["status"] != "succeeded"
                or not any(a["name"] in {"raw_frames.npy", "raw_groups.npy"}
                           for a in record["artifacts"])
            ):
                continue
            if kind == "run":
                if record.get("plugin_id") in {"retained_charge_v1", "electrical_control_v1"}:
                    # Mixed-history charge has no current-exposure replay contract.
                    continue
                schema = record.get("capture_program_snapshot", {}).get("payload", {}).get("schema")
                if schema in {
                    "camera_capture_program_v2", "camera_reference_hdr_capture_program_v2",
                    "camera_dcg_capture_program_v2",
                }:
                    # Discovery describes choices without decoding every retained run.
                    # The selected source is fully verified at template/prepare/readback.
                    summary = record["result"].get("summary", record["result"])
                    paired = schema == "camera_dcg_capture_program_v2"
                    name = "raw_groups.npy" if paired else "raw_frames.npy"
                    artifact = next(a for a in record["artifacts"] if a["name"] == name)
                    shape = artifact.get("shape", [])
                    family_label = {
                        "camera_capture_program_v2": "순차 HDR",
                        "camera_reference_hdr_capture_program_v2": "기준 노출 HDR",
                        "camera_dcg_capture_program_v2": "DCG",
                    }[schema]
                    for path in (["hcg", "lcg"] if paired else [None]):
                        selector = {"schema": "camera_nonlinear_raw_selection_v1",
                                    "mode": "shared_charge_path" if paired else "sequential_frames"}
                        if path is not None:
                            selector["readout_id"] = path
                        result.append({
                            "source_kind": kind, "source_id": record["id"],
                            "name": record["name"] + " · " + family_label
                            + (" · " + path.upper() if path else ""),
                            "raw_shape": [shape[0], *shape[-2:]] if shape else [],
                            "raw_container_shape": shape, "raw_sha256": artifact["sha256"],
                            "raw_format": summary["raw_formats"][0 if path == "hcg" else 1]
                            if paired else summary.get("raw_format"),
                            "geometry": summary.get("geometry"),
                            "conditions": record["test_snapshot"]["conditions"],
                            "source_provenance": {"source_kind": "simulated",
                                                  "source_uri": "run://" + record["id"]},
                            "selector": selector, "replay_profile_version": 3,
                            "integrity": "verified_on_selection",
                        })
                    continue
                if record.get("plugin_id") == "shared_charge_dcg_v1":
                    # Linear DCG remains outside the single-stream replay contract.
                    continue
            source = replay_source(service, kind, record["id"])
            result.append(
                {k: v for k, v in source.items() if k not in {"source_snapshot", "raw_artifact"}}
            )
    return result


def submit_replay(service, payload):
    if payload.get("schema") == "camera_retained_code_isp_replay_v1":
        from .retained_isp_replay import submit

        return submit(service, payload)
    if payload.get("schema") == "camera_isp_replay_v3":
        from .isp_replay_v3 import submit

        return submit(service, payload)
    selected_model = service.registry.get("models", payload["isp_model_id"])
    if selected_model["payload"].get("schema") == "camera_isp_profile_v2":
        from .isp_replay_v2 import submit

        return submit(service, payload)
    environment = _current(service)
    data = ReplayCreate.model_validate(payload).model_dump()
    force = data.pop("force")
    source = replay_source(service, data["source_kind"], data["source_id"])
    model = service.registry.get("models", data["isp_model_id"])
    profile = resolve_isp(model)
    expected = model["input_raw_format"]
    for key in ("cfa", "bit_depth", "packing", "hdr_streams", "black_level_dn"):
        if _format(expected).get(key) != source["raw_format"].get(key):
            raise ValueError("ISP input RAW format differs from preserved acquisition")
    from .validation import check_domain

    acquisition = [source["conditions"]]
    if source.get("frame_conditions"):
        if any(
            f.get("exposure_ns") is None or f.get("gain") is None
            for f in source["frame_conditions"]
        ):
            raise ValueError(
                "ISP applicability requires the actual exposure and gain for every RAW frame"
            )
        acquisition = [
            {
                **source["conditions"],
                "exposure_s": {"value": frame["exposure_ns"] * 1e-9, "unit": "s"},
                "gain": {"value": frame["gain"], "unit": "1"},
            }
            for frame in source["frame_conditions"]
        ]
    if any(check_domain(conditions, model["valid_domain"]) for conditions in acquisition):
        raise ValueError("ISP model applicability does not cover original acquisition conditions")
    from .isp import _input
    from .observations import load_raw_npy

    raw = load_raw_npy(service.registry.blob_path(source["raw_sha256"]).read_bytes())
    _input(raw, source["raw_format"], source["geometry"])
    frozen = {
        **data,
        "job_kind": "isp_replay",
        "plugin_id": "isp_replay_v1",
        "source": source,
        "isp_model_snapshot": model,
        "profile": profile,
        "environment": environment,
        "input_artifacts": [source["raw_artifact"]],
    }
    return service.registry.submit(frozen, digest(frozen), force=force)


def store_frame_previews(service, output):
    """Attach an explicit artifact identity to each already identified frame."""
    from PIL import Image

    records = output["summary"]["frames"]
    frames = np.asarray(output["arrays"]["rgb_frames"])
    if not isinstance(records, list) or len(records) != len(frames):
        raise ValueError("Frame preview records must match rendered frame count")
    artifacts = []
    for index, (record, pixels) in enumerate(zip(records, frames, strict=True)):
        stream = io.BytesIO()
        Image.fromarray((np.clip(pixels, 0, 1) * 255).astype(np.uint8)).save(stream, format="PNG")
        artifact = service.registry.store_blob(
            stream.getvalue(), f"sensor-rgb-frame-{index:03d}.png", "image/png"
        )
        artifact.update(frame_id=record["frame_id"], frame_index=index)
        record.update(preview_artifact_id=artifact["id"], preview_url=artifact["url"])
        artifacts.append(artifact)
    return artifacts


def store_dcg_previews(service, output):
    """Retain acquisition and readout identities in all paired preview artifacts."""
    from PIL import Image

    records = output["summary"]["acquisitions"]
    groups = np.asarray(output["arrays"]["rgb_groups"])
    if groups.ndim != 5 or groups.shape[1] != 2 or len(records) != len(groups):
        raise ValueError("DCG previews require acquisition × two readout RGB arrays")
    artifacts = []

    def store(pixels, index, acquisition_id, stream_id):
        stream = io.BytesIO()
        Image.fromarray((np.clip(pixels, 0, 1) * 255).astype(np.uint8)).save(stream, format="PNG")
        artifact = service.registry.store_blob(
            stream.getvalue(), f"dcg-{index:03d}-{stream_id}.png", "image/png"
        )
        artifact.update(acquisition_id=acquisition_id, acquisition_index=index, stream_id=stream_id)
        artifacts.append(artifact)
        return {"preview_artifact_id": artifact["id"], "preview_url": artifact["url"]}

    merged = output["arrays"].get("merged_rgb")
    if merged is not None and len(merged) != len(records):
        raise ValueError("DCG merged previews must retain every acquisition")
    for index, record in enumerate(records):
        if [r["stream_id"] for r in record["readouts"]] != ["HCG", "LCG"]:
            raise ValueError("DCG preview readout order differs from HCG/LCG")
        for readout, pixels in zip(record["readouts"], groups[index], strict=True):
            readout.update(store(pixels, index, record["acquisition_id"], readout["stream_id"]))
        if merged is not None:
            preview = store(merged[index], index, record["acquisition_id"], "merged")
            record.update(merged_preview_url=preview["preview_url"],
                          merged_preview_artifact_id=preview["preview_artifact_id"])
    return artifacts


def store_output(service, output, *, source_kind, geometry, input_artifacts=()):
    from PIL import Image

    summary = output["summary"]
    artifacts = list(input_artifacts)
    for name, values in output["arrays"].items():
        array = np.asarray(values)
        if array.dtype.hasobject or array.nbytes > 128 * 1024 * 1024:
            raise ValueError("Output arrays exceed bounded non-object storage contract")
        stream = io.BytesIO()
        np.save(stream, array, allow_pickle=False)
        artifact = service.registry.store_blob(
            stream.getvalue(), name + ".npy", "application/octet-stream"
        )
        artifact.update(
            shape=list(array.shape),
            dtype=str(array.dtype),
            unit=summary.get("array_units", {}).get(name, "1"),
            source_kind=source_kind,
            spatial_sampling=geometry["kind"],
        )
        artifacts.append(artifact)
    if "rgb" in output["arrays"]:
        stream = io.BytesIO()
        Image.fromarray((np.clip(output["arrays"]["rgb"], 0, 1) * 255).astype(np.uint8)).save(
            stream, format="PNG"
        )
        artifacts.append(
            service.registry.store_blob(stream.getvalue(), "sensor-rgb.png", "image/png")
        )
    artifacts.append(
        service.registry.store_blob(canonical(summary).encode(), "summary.json", "application/json")
    )
    return {
        **summary,
        "summary": summary,
        "artifacts": artifacts,
        "verification": {
            "implementation_verification": "runtime_completed",
            "physical_validation": "not_established",
        },
    }


def execute_replay(service, job):
    if job.get("plugin_id") == "retained_isp_replay_v1":
        from .retained_isp_replay import execute

        return execute(service, job)
    if job.get("plugin_id") == "isp_replay_v3":
        from .isp_replay_v3 import execute

        return execute(service, job)
    if job.get("plugin_id") == "isp_replay_v2":
        from .isp_replay_v2 import execute

        return execute(service, job)
    if job["environment"] != _current(service):
        raise ValueError("Worker differs from frozen replay environment")
    from .isp import process_raw
    from .observations import load_raw_npy

    source = job["source"]
    raw = load_raw_npy(service.registry.blob_path(source["raw_sha256"]).read_bytes())
    output = process_raw(raw, source["raw_format"], job["profile"], geometry=source["geometry"])
    output["summary"].update(
        plugin_id="isp_replay_v1",
        job_kind="isp_replay",
        source_kind=source["source_provenance"]["source_kind"],
        acquisition_conditions=source["conditions"],
        conditions=source["conditions"],
        source_id=job["source_id"],
        raw_sha256=source["raw_sha256"],
        physical_validation="not_established",
        uncertainty={"kind": "unknown"},
        replay_isp_model_id=job["isp_model_snapshot"]["id"],
    )
    artifacts = list(job["input_artifacts"])
    if source.get("frame_records"):
        summary = output["summary"]
        summary.update(
            frame_count=len(source["frame_records"]),
            frames=copy.deepcopy(source["frame_records"]),
            frame_conditions=copy.deepcopy(source["frame_conditions"]),
            clock=copy.deepcopy(source["clock"]),
            **{
                k: copy.deepcopy(source[k])
                for k in (
                    "timing",
                    "control_trace",
                    "acquisition_mode",
                    "control_program_id",
                    "capture_program_id",
                    "control_policy_id",
                    "response_acquisition",
                )
                if k in source
            },
        )
        for frame in summary["frames"]:
            frame["replay_isp_model_id"] = job["isp_model_snapshot"]["id"]
        artifacts.extend(store_frame_previews(service, output))
    return store_output(
        service,
        output,
        source_kind=source["source_provenance"]["source_kind"],
        geometry=source["geometry"],
        input_artifacts=artifacts,
    )


def spatial_test_template(service, assembly_id, scene_id):
    from .spatial import EFFECTS

    assembly = service.registry.get("assemblies", assembly_id)
    scene = service.registry.get("spectral_images", scene_id)
    sensor = next(model for model in (service.registry.get("models", ident)
                  for ident in assembly["model_ids"]) if model["kind"] == "sensor")
    return {
        "name": "분광 장면 · Native RAW 채널 평균",
        "revision": "spatial-1",
        "validation_kind": "implementation_verification",
        "metric_id": "raw.channel_mean_dn",
        "unit": "DN",
        "reference_plane": "sensor_raw",
        "included_effects": with_sensor_effects(EFFECTS, sensor["payload"]),
        "conditions": assembly["conditions"],
        "required_conditions": list(assembly["conditions"]),
        "absolute_tolerance": {"value": 1.0, "unit": "DN"},
        "uncertainty_policy": "bounded_interval",
        "calibration_dataset_ids": [],
        "stimulus": {
            "schema": "spectral_image_test_v1",
            "scene_id": scene["id"],
            "scene_content_hash": scene["content_hash"],
        },
    }


def _scene_input(service, record, *, lens_model_id=None):
    if (
        record.get("projection_lens_model_id")
        and record["projection_lens_model_id"] != lens_model_id
    ):
        raise ValueError("Rendered input requires its pinned projection lens in every runtime")
    if lens_model_id:
        lens = service.registry.get("models", lens_model_id)
        if lens["payload"].get("radiometry", {}).get("object_conjugate"):
            from .finite_plane import verify_scene

            verify_scene(service, record, lens)
    artifact = next(a for a in record["artifacts"] if a["name"] == "spectral-image.npy")
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    image = load_spectral_npy(content)
    subcell_fields = {}
    source_snapshot = record.get("source_render_snapshot", record.get("render_job_snapshot"))
    if source_snapshot is not None:
        from . import scene_projection, scene_render, subcell_optics

        quadrature = source_snapshot["request"].get("source_pixel_quadrature")
        if quadrature and quadrature["model"] == subcell_optics.MODEL:
            source, _, _ = scene_projection._source(service, source_snapshot["id"])
            if source != source_snapshot:
                raise ValueError("Retained subcell source differs from immutable render snapshot")
            projected = record.get("projection_job_snapshot")
            if projected is None:
                scene_render.publication_evidence(
                    service, source["id"], source["result"]["spectral_image_metadata"], content
                )
                projection = source["request"]["projection"]
            else:
                if (service.registry.get_run(projected["id"]) != projected
                        or projected["source_render_snapshot"] != source
                        or projected["lens_snapshot"]["id"] != lens_model_id):
                    raise ValueError("Retained subcell projection differs from immutable snapshot")
                scene_projection.publication_evidence(
                    service, projected["id"], projected["result"]["spectral_image_metadata"],
                    content,
                )
                projection = projected["request"]["projection"]
            subcells = scene_render._retained_subcells(service, source)
            subcell_fields = {
                "source_subcell_irradiance": scene_render.project_radiance(subcells, projection),
                "source_subcell_evidence": {
                    "model": subcell_optics.TRANSFER,
                    "grid_side": quadrature["grid_side"],
                    "radiance_sha256": next(
                        a["sha256"] for a in source["artifacts"]
                        if a["name"] == "source-subcell-radiance.npy"
                    ),
                    "source_render_id": source["id"],
                    "projection_job_id": projected["id"] if projected else None,
                },
            }
    return {
        "schema": "spectral_ideal_image_v1",
        "wavelength_nm": record["wavelength_nm"],
        "unit": record["unit"],
        "reference_plane": record["reference_plane"],
        "spectral_irradiance": image,
        **subcell_fields,
        "sampling": record["sampling"],
        "included_effects": record["included_effects"],
        "source": {"kind": record["source_kind"], "description": record["source_uri"]},
    }


def _spatial_scene_with_capture_crop(scene_input, parameters):
    """Bind a requested receiver ROI to the frozen, larger spectral source.

    The registered image retains its own native origin and immutable content;
    the spatial runtime validates complete PSF halo support before submission.
    """
    if "capture_crop" not in parameters:
        return scene_input
    result = copy.deepcopy(scene_input)
    result["sampling"] = {
        **result["sampling"], "capture_crop": copy.deepcopy(parameters["capture_crop"]),
    }
    return result


def _rendered_capture_crop(record):
    """Recover the receiver ROI frozen by a published halo-render job."""
    job = record.get("render_job_snapshot")
    if job is None:
        return None
    request = job.get("request", {})
    capture = request.get("capture_crop")
    if capture is None:
        return None
    if (
        job.get("result", {}).get("capture_crop") != capture
        or request.get("sampling") != record["sampling"]
        or request.get("shape") != record["shape"][:2]
    ):
        raise ValueError("Published halo-render capture ROI differs from its frozen source")
    return copy.deepcopy(capture)


def _verify_rendered_carrier_sensor(record, sensor):
    """A carrier-expanded render is bound to the Sensor used for its support."""
    job = record.get("render_job_snapshot") or {}
    carrier = job.get("request", {}).get("carrier_halo")
    if carrier is None:
        return
    collection_artifact = sensor.get("response_artifacts", {}).get("charge_collection", {})
    if (
        carrier.get("sensor_model_id") != sensor["id"]
        or carrier.get("sensor_model_sha256") != sensor["content_hash"]
        or carrier.get("charge_collection_sha256") != collection_artifact.get("sha256")
    ):
        raise ValueError(
            "Rendered carrier halo belongs to a different pinned Sensor revision"
        )


def prepare_spatial(service, data):
    from .composer import check_assembly
    from .service import PreflightError
    from .spatial import EFFECTS, SpatialSceneRuntime

    environment = _current(service)
    if not data["scene_id"]:
        raise ValueError("Spatial simulation requires a registered spectral image")
    scene = service.registry.get("spectral_images", data["scene_id"])
    assembly = service.registry.get("assemblies", data["assembly_id"])
    test = service.registry.get("tests", data["test_id"])
    sensor_record = next(model for model in (service.registry.get("models", ident)
                         for ident in assembly["model_ids"]) if model["kind"] == "sensor")
    expected_effects = with_sensor_effects(EFFECTS, sensor_record["payload"])
    if (
        test["metric_id"] != "raw.channel_mean_dn"
        or test["unit"] != "DN"
        or test["reference_plane"] != "sensor_raw"
        or set(test["included_effects"]) != set(expected_effects)
        or test.get("observation_method")
    ):
        raise ValueError(
            "Spatial simulation requires the native RAW mean Test contract; "
            "radial or MTF tests are not interchangeable"
        )
    if test["stimulus"] != {
        "schema": "spectral_image_test_v1",
        "scene_id": scene["id"],
        "scene_content_hash": scene["content_hash"],
    }:
        raise ValueError("Selected spectral image differs from the frozen spatial Test stimulus")
    models = [service.registry.get("models", i) for i in assembly["model_ids"]]
    by_kind = {m["kind"]: m for m in models}
    from .native_model_evidence import verify_models

    response_artifacts = verify_models(service, models)
    _verify_rendered_carrier_sensor(scene, by_kind["sensor"])
    if (
        scene.get("projection_lens_model_id")
        and scene["projection_lens_model_id"] != by_kind["lens"]["id"]
    ):
        raise ValueError(
            "Rendered image projection belongs to a different lens; reproject the scene"
        )
    lens = copy.deepcopy(by_kind["lens"]["payload"])
    if "psf" not in lens:
        raise ValueError("The selected Lens model has no explicit 2D intensity PSF")
    psf = lens.pop("psf")
    sensor = by_kind["sensor"]["payload"]
    parameters = copy.deepcopy(data["parameters"])
    if set(parameters) - {"exposure_ms", "seed", "frames", "capture_crop",
                          "field_radiometry_mode"}:
        raise ValueError(
            "Spatial simulation accepts exposure/seed/frames/capture_crop/field_radiometry_mode; "
            "never resize a native scene"
        )
    rendered_capture = _rendered_capture_crop(scene)
    if rendered_capture is not None and "capture_crop" not in parameters:
        parameters["capture_crop"] = rendered_capture
    conditions = copy.deepcopy(test["conditions"])
    if "exposure_ms" in parameters:
        value = parameters["exposure_ms"]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Exposure must be a positive finite number in ms")
        conditions["exposure_s"] = {"value": value / 1000, "unit": "s"}
    for name, expected in scene["conditions"].items():
        # Sensor exposure/gain do not alter a supplied ideal optical image.
        if name not in {"exposure_s", "gain"} and conditions.get(name) != expected:
            raise ValueError(
                "Spectral image optical/environment conditions differ from selected Test"
            )
    required_optical = set(test["required_conditions"]) - {"exposure_s", "gain"}
    if required_optical - scene["conditions"].keys():
        raise ValueError("Spectral image is missing required optical/environment conditions")
    report = check_assembly({**assembly, "conditions": conditions}, models)
    if report["status"] != "PASS":
        raise PreflightError(report)
    for key, unit in {"exposure_s": "s", "temperature_c": "degC", "gain": "1"}.items():
        if conditions.get(key, {}).get("unit") != unit:
            raise ValueError(f"{key} must use {unit}")
    if conditions["gain"]["value"] != sensor["readout"]["gain"]:
        raise ValueError("Fixed sensor gain differs from Test")
    if (
        assembly["raw_format"]["bit_depth"] != sensor["readout"]["bit_depth"]
        or assembly["raw_format"]["black_level"] != sensor["readout"]["black_level_dn"]
        or assembly["raw_format"]["cfa"] != "RGGB"
        or assembly["raw_format"]["packing"] != "unpacked"
        or assembly["raw_format"]["hdr_streams"] != 1
    ):
        raise ValueError("Sensor readout differs from declared RAW format")
    _, _, pitch, full = _native(service, assembly["id"])
    if (
        scene["sampling"]["pixel_pitch_um"] != pitch
        or scene["sampling"]["full_sensor_shape"] != full
    ):
        raise ValueError("Spectral image native sampling differs from sensor geometry")
    request = {
        "wavelength_nm": sensor["wavelength_nm"],
        **sensor_response_fields(sensor),
        "pixel_pitch_um": pitch,
        "readout": sensor["readout"],
        "exposure_s": conditions["exposure_s"]["value"],
        "temperature_c": conditions["temperature_c"]["value"],
        "lens": lens,
        "psf": psf,
        "seed": parameters.get("seed", 42),
        "frames": parameters.get("frames", 4),
        **({"field_radiometry_mode": parameters["field_radiometry_mode"]}
           if "field_radiometry_mode" in parameters else {}),
        "isp_profile": resolve_isp(by_kind["isp"]),
        "scene": _spatial_scene_with_capture_crop(
            _scene_input(service, scene, lens_model_id=by_kind["lens"]["id"]), parameters
        ),
    }
    plugin = SpatialSceneRuntime()
    issues = plugin.validate_request(request)
    if issues:
        raise PreflightError({"status": "FAIL", "issues": issues})
    resources = plugin.estimate_resources(request)
    request["scene"].pop("spectral_irradiance")
    request["scene"].pop("source_subcell_irradiance", None)
    effective_test = {**copy.deepcopy(test), "conditions": conditions}
    if conditions != test["conditions"]:
        effective_test.update(
            derived_from_test=test["id"], id=test["id"] + ":" + digest(conditions)[:12]
        )
    frozen = {
        "name": assembly["name"],
        "assembly_id": assembly["id"],
        "test_id": test["id"],
        "scene_id": scene["id"],
        "plugin_id": "spatial_scene_v1",
        "job_kind": "camera_simulation",
        "parameters": parameters,
        "request": request,
        "assembly_snapshot": assembly,
        "model_snapshots": models,
        "test_snapshot": effective_test,
        "scene_snapshot": scene,
        "input_artifacts": [*scene["artifacts"], *response_artifacts],
        "compatibility": report,
        "environment": environment,
        "resources": resources,
    }
    return frozen, data["force"]


def execute_spatial(service, job):
    from .native_model_evidence import verify_models
    from .spatial import SpatialSceneRuntime

    if job["environment"] != _current(service):
        raise ValueError("Worker differs from frozen spatial environment")
    if job["request"].get("field_radiometry_mode") != job["parameters"].get(
        "field_radiometry_mode"
    ):
        raise ValueError("Frozen spatial field radiometry mode differs from run parameters")
    verify_models(service, job["model_snapshots"])
    roles = {model["kind"]: model for model in job["model_snapshots"]}
    _verify_rendered_carrier_sensor(job["scene_snapshot"], roles["sensor"])
    if "native_pixel_response" in roles["sensor"]["payload"]:
        expected = sensor_response_fields(roles["sensor"]["payload"])
        if any(job["request"].get(k) != v for k, v in expected.items()):
            raise ValueError("Native response request differs from the frozen Sensor model")
        for key in ("wavelength_nm", "pixel_pitch_um", "readout"):
            if job["request"][key] != roles["sensor"]["payload"][key]:
                raise ValueError("Native request metadata differs from the frozen Sensor model")
        lens = copy.deepcopy(roles["lens"]["payload"])
        psf = lens.pop("psf")
        if job["request"]["lens"] != lens or job["request"]["psf"] != psf:
            raise ValueError("Native optical request differs from the frozen Lens model")
        for artifact in job["input_artifacts"]:
            service.registry.blob_path(artifact["sha256"])
    request = copy.deepcopy(job["request"])
    scene_input = _spatial_scene_with_capture_crop(_scene_input(
        service,
        job["scene_snapshot"],
        lens_model_id=next(m["id"] for m in job["model_snapshots"] if m["kind"] == "lens"),
    ), job["parameters"])
    frozen_scene = copy.deepcopy(scene_input)
    frozen_scene.pop("spectral_irradiance")
    frozen_scene.pop("source_subcell_irradiance", None)
    if request["scene"] != frozen_scene:
        raise ValueError("Frozen spatial source/capture geometry differs from the saved scene")
    request["scene"] = scene_input
    output = SpatialSceneRuntime().execute(request)
    summary = output["summary"]
    summary["conditions"] = job["test_snapshot"]["conditions"]
    summary["prediction"].update(
        id=job["id"],
        conditions=summary["conditions"],
        valid_domain=service._domain_intersection(job["model_snapshots"]),
        calibration_dataset_ids=sorted(
            {i for m in job["model_snapshots"] for i in m["calibration_dataset_ids"]}
        ),
        dataset_ids=[job["id"]],
        assembly_id=job["assembly_id"],
        test_id=job["test_id"],
    )
    summary.update(scene_id=job["scene_id"], spatial_sampling="native_sensor_crop")
    from .uncertainty import attach, unsupported_budget

    budget = unsupported_budget(job["model_snapshots"], job["test_snapshot"], job["plugin_id"])
    if budget:
        attach(summary, budget)
    return store_output(
        service,
        output,
        source_kind="simulated",
        geometry=summary["geometry"],
        input_artifacts=job["input_artifacts"],
    )
