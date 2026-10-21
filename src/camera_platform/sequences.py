"""Immutable timed ideal-image inputs and recorded applied camera settings."""

from __future__ import annotations

import copy
import io
import json

import numpy as np

from .contracts import ControlSequenceCreate, SceneSequenceCreate, TemporalFixtureCreate
from .imaging import (
    _current,
    _format,
    _native,
    _rendered_capture_crop,
    _scene_input,
    _verify_rendered_carrier_sensor,
)
from .registry import canonical

COLLECTIONS = {"scene_sequences": SceneSequenceCreate, "control_sequences": ControlSequenceCreate}


def _nonlinear_program(payload):
    return payload.get("schema") in (
        "camera_capture_program_v2", "camera_dcg_capture_program_v2",
        "camera_reference_hdr_capture_program_v2",
    )


def _nonlinear_valid_domain(domain, program_payload):
    """Intersect the retained fixed-model domain with curve and voltage-noise settings."""
    if not _nonlinear_program(program_payload):
        return domain
    model = program_payload["electrical_model"]
    paired = program_payload["schema"] == "camera_dcg_capture_program_v2"
    curves = ([p["charge_voltage_response"] for p in model["paths"]] if paired
              else [model["charge_voltage_response"]])
    temperatures = [curve["valid_domain"]["temperature_c"] for curve in curves]
    temperatures.append(model["read_noise"]["temperature_range_c"])
    bounds = {"temperature_c": {"min": max(p[0] for p in temperatures),
                                "max": min(p[1] for p in temperatures), "unit": "degC"}}
    if not paired:
        gains = [curves[0]["valid_domain"]["analog_gain"],
                 model["read_noise"]["analog_gain_range"]]
        bounds["gain"] = {"min": max(p[0] for p in gains),
                          "max": min(p[1] for p in gains), "unit": "1"}
    result = copy.deepcopy(domain)
    for key, limit in bounds.items():
        if key in result:
            if result[key]["unit"] != limit["unit"]:
                raise ValueError("Nonlinear program and model domain units disagree")
            limit["min"] = max(limit["min"], result[key]["min"])
            limit["max"] = min(limit["max"], result[key]["max"])
        if limit["min"] > limit["max"]:
            raise ValueError("Nonlinear program and model applicability domains do not intersect")
        result[key] = limit
    return result


def _dcg_valid_domain(domain, program_payload):
    """Intersect model applicability with the fixed paired-readout program domain."""
    if not isinstance(domain, dict) or not domain:
        raise ValueError("DCG requires declared model applicability before program intersection")
    from .dcg_runtime import validate_capture_program

    program = validate_capture_program(program_payload)
    low, high = program["electrical_model"]["read_noise"]["temperature_range_c"]
    result = _nonlinear_valid_domain(domain, program)
    result = copy.deepcopy(result)
    for key, bounds in {
        "temperature_c": {"min": low, "max": high, "unit": "degC"},
        "gain": {"min": 1, "max": 1, "unit": "1"},
    }.items():
        if key in result:
            current = result[key]
            if current["unit"] != bounds["unit"]:
                raise ValueError("DCG program and model domain units disagree")
            bounds["min"] = max(bounds["min"], current["min"])
            bounds["max"] = min(bounds["max"], current["max"])
        if bounds["min"] > bounds["max"]:
            raise ValueError("DCG program and model applicability domains do not intersect")
        result[key] = bounds
    return result


def _retained_valid_domain(domain, payload):
    """Apply electrical applicability without inheriting independent-reset semantics."""
    from .retained_charge_runtime import _paired, validate_capture_program

    program = validate_capture_program(payload)
    model = program["electrical_model"]
    paired, nonlinear = _paired(model), model["schema"].endswith("_v2")
    legacy_schema = ("camera_dcg_capture_program_v2" if paired else "camera_capture_program_v2")
    result = (
        _nonlinear_valid_domain(domain, {**program, "schema": legacy_schema})
        if nonlinear
        else copy.deepcopy(domain)
    )
    limits = {}
    if paired:
        low, high = model["read_noise"]["temperature_range_c"]
        limits = {"temperature_c": {"min": low, "max": high, "unit": "degC"},
                  "gain": {"min": 1, "max": 1, "unit": "1"}}
    for key, bound in limits.items():
        if key in result:
            if result[key]["unit"] != bound["unit"]:
                raise ValueError("Retained program and fixed model domain units disagree")
            bound["min"] = max(bound["min"], result[key]["min"])
            bound["max"] = min(bound["max"], result[key]["max"])
        if bound["min"] > bound["max"]:
            raise ValueError("Retained electrical applicability domains do not intersect")
        result[key] = bound
    return result


def _provenance():
    return {
        "source_kind": "estimated",
        "source_uri": "fixture://authored-temporal-camera",
        "license": "MIT; authored synthetic image-plane/control fixture",
        "method_and_version": "authored_temporal_fixture:1",
        "physical_instance": None,
        "parents": [],
    }


def _clock():
    return {
        "clock_id": "authored-simulation-clock",
        "unit": "ns",
        "reference": "relative",
        "origin": "Authored simulation time zero; not a hardware acquisition timestamp",
    }


def _images(service, payload):
    records = [
        service.registry.get("spectral_images", k["spectral_image_id"]) for k in payload["knots"]
    ]
    for knot, record in zip(payload["knots"], records, strict=True):
        direct = record.get("render_job_snapshot")
        projected = record.get("source_render_snapshot")
        projection = record.get("projection_job_snapshot")
        if direct is not None and (projected is not None or projection is not None):
            raise ValueError("Published 3D image has ambiguous render provenance")
        if (projected is None) != (projection is None):
            raise ValueError("Projected 3D image has incomplete render provenance")
        if projected is not None and (
            not isinstance(projection, dict)
            or projection.get("source_render_snapshot") != projected
        ):
            raise ValueError("Projected 3D image differs from its frozen source render")
        job = direct if direct is not None else projected
        if job is None:
            continue
        if not isinstance(job, dict):
            raise ValueError("Published 3D render provenance is not frozen metadata")
        request = job.get("request", {})
        if not isinstance(request, dict):
            raise ValueError("Published 3D render request is not frozen metadata")
        has_time = "render_time_ns" in request
        has_clock = "render_clock" in request
        if has_time != has_clock:
            raise ValueError("Published 3D render has incomplete frozen time binding")
        if has_time and (type(request["render_time_ns"]) is not int
                         or request["render_time_ns"] != knot["time_ns"]
                         or request["render_clock"] != payload["clock"]):
            raise ValueError("Published 3D render time/clock differs from its scene knot")
    fixed = ["wavelength_nm", "unit", "reference_plane", "sampling", "included_effects", "shape"]
    for record in records[1:]:
        if any(record[key] != records[0][key] for key in fixed):
            raise ValueError(
                "Timed spectral images require identical native sampling, axes and plane"
            )
    if np.prod(records[0]["shape"]) * len({r["id"] for r in records}) > 64_000_000:
        raise ValueError("Timed spectral input exceeds 64 million wavelength-pixel samples")
    return records


def _capture_crop(images, unmodulated_images=(), requested=None):
    """Bind one receiver ROI without changing immutable source-image sampling.

    A rendered halo source has a fixed intended receiver. A manually imported
    larger source may select one explicitly; the spatial runtime checks the
    complete optical and carrier halo for each component before submission.
    """
    records = [*images, *unmodulated_images]
    pinned = [_rendered_capture_crop(record) for record in records]
    frozen = [value for value in pinned if value is not None]
    if frozen and any(value != frozen[0] for value in frozen[1:]):
        raise ValueError("Timed source images have different frozen receiver ROIs")
    if frozen and requested is not None and requested != frozen[0]:
        raise ValueError("Requested receiver ROI differs from the frozen halo render")
    capture = requested if requested is not None else (frozen[0] if frozen else None)
    if capture is None:
        return None
    if (not isinstance(capture, dict) or set(capture) != {"crop_origin", "shape"}
            or any(not isinstance(capture.get(key), list)
                   or len(capture[key]) != 2
                   or any(type(value) is not int for value in capture[key])
                   for key in ("crop_origin", "shape"))):
        raise ValueError("capture_crop requires exact integer crop_origin and shape pairs")
    full = records[0]["sampling"]["full_sensor_shape"]
    if any(origin < 0 or size < 8 or size > 512 or size % 2
           or origin + size > native
           for origin, size, native in zip(capture["crop_origin"], capture["shape"],
                                           full, strict=True)):
        raise ValueError("capture_crop requires even 8..512 Bayer sides within the native sensor")
    return copy.deepcopy(capture)


def _capture_scene(service, record, lens_model_id, capture_crop):
    source = _scene_input(service, record, lens_model_id=lens_model_id)
    if capture_crop is None:
        return source
    return {**source, "sampling": {**source["sampling"], "capture_crop": capture_crop}}


def create_sequence(service, kind, payload, *, original_text=None):
    from .timing import validate_control_sequence, validate_timed_scene, validate_timing_coverage

    if kind not in COLLECTIONS:
        raise ValueError("Unknown sequence kind")
    data = COLLECTIONS[kind].model_validate(payload).model_dump()
    if data["source_kind"] == "measured" and not data["physical_instance"]:
        raise ValueError("Measured sequence requires its physical camera/sample identity")
    for parent in data["parents"]:
        service.registry.get(kind, parent)
    if kind == "scene_sequences":
        data["payload"] = validate_timed_scene(data["payload"])
        records = _images(service, data["payload"])
        snapshots = {"image_snapshots": records}
    else:
        scene = service.registry.get("scene_sequences", data["scene_sequence_id"])
        records = _images(service, scene["payload"])
        assembly, _, pitch, full = _native(service, data["assembly_id"])
        sampling = records[0]["sampling"]
        if sampling["full_sensor_shape"] != full or sampling["pixel_pitch_um"] != pitch:
            raise ValueError("Timed scene sampling differs from selected native sensor")
        geometry = {k: sampling[k] for k in ["kind", "full_sensor_shape", "crop_origin"]}
        data["payload"] = validate_control_sequence(
            data["payload"], geometry=geometry, frame_shape=records[0]["shape"][:2]
        )
        validate_timing_coverage(
            scene["payload"],
            data["payload"],
            geometry=geometry,
            frame_shape=records[0]["shape"][:2],
        )
        for frame in data["payload"]["frames"]:
            model = service.registry.get("models", frame["applied"]["isp_model_id"])
            if model["kind"] != "isp":
                raise ValueError("Applied ISP identity must refer to an ISP model")
        snapshots = {"scene_sequence_snapshot": scene, "assembly_snapshot": assembly}
    raw = (
        original_text.encode("utf-8") if original_text is not None else canonical(payload).encode()
    )
    if len(raw) > 1_000_000:
        raise ValueError("Sequence source JSON exceeds 1 MB")
    prefix = "scene-sequence" if kind == "scene_sequences" else "control-sequence"
    artifacts = [
        service.registry.store_blob(raw, prefix + "-source.json", "application/json"),
        service.registry.store_blob(
            canonical(data).encode(), prefix + "-normalized.json", "application/json"
        ),
    ]
    return service.registry.put(
        kind,
        {
            **data,
            **snapshots,
            "artifacts": artifacts,
            "approval_status": "draft",
            "physical_validation": "not_established",
        },
    )


def import_sequence(service, kind, text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > 1_000_000:
        raise ValueError("Sequence source JSON must be at most 1 MB")
    return create_sequence(service, kind, json.loads(text), original_text=text)


def scene_template(service, assembly_id=None):
    _, _, pitch, full = _native(service, assembly_id)
    matching = [
        r
        for r in service.registry.list("spectral_images")
        if r["sampling"]["pixel_pitch_um"] == pitch and r["sampling"]["full_sensor_shape"] == full
    ]
    identity = matching[0]["id"] if matching else "REGISTER_SPECTRAL_IMAGE_ID"
    return {
        **_provenance(),
        "name": "시간 분광 영상 · 원본 시각과 보간을 확인하세요",
        "revision": "1",
        "payload": {
            "schema": "timed_spectral_scene_v1",
            "clock": _clock(),
            "interpolation": "linear_irradiance",
            "knots": [{"time_ns": t, "spectral_image_id": identity} for t in [0, 80_000_000]],
        },
    }


def fixture_template(service, assembly_id=None):
    assembly, _, _, _ = _native(service, assembly_id)
    return TemporalFixtureCreate(assembly_id=assembly["id"]).model_dump()


def create_fixture(service, payload):
    from .imaging import import_spectral, spectral_template

    data = TemporalFixtureCreate.model_validate(payload).model_dump()
    rows, cols = data["shape"]
    if any(n < 8 or n > 256 or n % 2 for n in [rows, cols]):
        raise ValueError("Temporal fixture requires even native dimensions in [8,256]")
    metadata = spectral_template(service, data["assembly_id"])
    full = metadata["sampling"]["full_sensor_shape"]
    if rows > full[0] or cols > full[1]:
        raise ValueError("Temporal fixture crop exceeds native sensor")
    metadata["sampling"]["crop_origin"] = [
        (f - n) // 2 for f, n in zip(full, [rows, cols], strict=True)
    ]
    count = data["knot_count"]
    if data["pattern"] == "flicker" and (
        data["frequency_hz"] * data["duration_ns"] * 1e-9 * 8 > count - 1
    ):
        raise ValueError(
            "Flicker fixture requires at least 8 samples per period; "
            "increase knots or shorten duration"
        )
    if rows * cols * len(metadata["wavelength_nm"]) * count > 64_000_000:
        raise ValueError("Temporal fixture exceeds the declared source sample budget")
    times = [(data["duration_ns"] * i) // (count - 1) for i in range(count)]
    y, x = np.indices((rows, cols))
    wave = np.asarray(metadata["wavelength_nm"])
    spectrum = 0.4 + 0.6 * np.exp(-(((wave - 540) / 80) ** 2))
    knots = []
    for i, t in enumerate(times):
        fraction = t / data["duration_ns"]
        if data["pattern"] == "moving_spot":
            center = 0.15 * cols + 0.7 * cols * fraction
            intensity = 0.03 + 0.9 * np.exp(
                -((x - center) ** 2 + (y - rows * 0.5) ** 2) / (2 * (rows * 0.07) ** 2)
            )
        elif data["pattern"] == "flicker":
            intensity = np.full(
                (rows, cols),
                0.05
                + 0.9
                * (
                    0.5
                    + 0.5 * np.sin(2 * np.pi * data["frequency_hz"] * t * 1e-9 + data["phase_rad"])
                ),
            )
        else:
            intensity = np.full((rows, cols), 0.5)
        values = np.asarray(intensity[..., None] * spectrum * data["irradiance_scale"], dtype="<f8")
        stream = io.BytesIO()
        np.save(stream, values, allow_pickle=False)
        meta = copy.deepcopy(metadata)
        meta.update(
            name=data["name"] + f" · 시각 {t} ns",
            revision=data["revision"] + f"-knot-{i}",
            source_uri="fixture://authored-temporal/" + data["pattern"],
            method_and_version="sampled_ideal_image_" + data["pattern"] + ":1",
            license="MIT; authored synthetic image-plane fixture",
        )
        record = import_spectral(service, meta, stream.getvalue())
        metadata["asset_id"] = record["asset_id"]
        knots.append({"time_ns": t, "spectral_image_id": record["id"]})
    payload = {
        **_provenance(),
        "name": data["name"],
        "revision": data["revision"],
        "fixture_specification": {
            "schema": "authored_temporal_fixture_v1",
            "parameters": data,
            "interpolation": "linear_irradiance",
            "accuracy": "unknown; sampled analytic ideal-image fixture, "
            "no geometric or physical validation",
        },
        "payload": {
            "schema": "timed_spectral_scene_v1",
            "clock": _clock(),
            "interpolation": "linear_irradiance",
            "knots": knots,
        },
    }
    return create_sequence(service, "scene_sequences", payload)


def control_template(service, assembly_id, scene_sequence_id):
    assembly, sensor, _, full = _native(service, assembly_id)
    sequence = service.registry.get("scene_sequences", scene_sequence_id)
    isp = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "isp"
    )
    knots = sequence["payload"]["knots"]
    start = knots[0]["time_ns"]
    end = knots[-1]["time_ns"]
    exposure = int(round(assembly["conditions"]["exposure_s"]["value"] * 1e9))
    line = 5000
    span = exposure + (full[0] - 1) * line
    if end - start < span:
        raise ValueError("Scene timeline is too short for default native rolling exposure")
    count = min(4, max(1, (end - start) // max(span, 20_000_000)))
    step = max(span, 20_000_000)
    frames = []
    for i in range(count):
        frame_start = start + i * step
        frames.append(
            {
                "frame_id": f"frame-{i:04d}",
                "frame_start_ns": frame_start,
                "applied": {
                    "applied_at_ns": frame_start,
                    "exposure_ns": exposure,
                    "gain": sensor["payload"]["readout"]["gain"],
                    "isp_model_id": isp["id"],
                    "command_id": None,
                },
            }
        )
    return {
        **_provenance(),
        "name": "명시적 적용 설정 · 합성 기록 예제",
        "revision": "1",
        "assembly_id": assembly_id,
        "scene_sequence_id": scene_sequence_id,
        "payload": {
            "schema": "recorded_control_sequence_v1",
            "clock": sequence["payload"]["clock"],
            "mode": {
                "shutter": "rolling",
                "line_time_ns": line,
                "hidden_top_rows": 0,
                "readout_basis": "full_sensor_rows",
            },
            "frames": frames,
        },
    }


def frame_conditions(control):
    return [
        {
            "frame_id": f["frame_id"],
            "frame_start_ns": f["frame_start_ns"],
            "applied_at_ns": f["applied"]["applied_at_ns"],
            "exposure_ns": f["applied"]["exposure_ns"],
            "gain": f["applied"]["gain"],
            "isp_model_id": f["applied"]["isp_model_id"],
        }
        for f in control["frames"]
    ]


def test_template(
    service, assembly_id, scene_sequence_id, control_sequence_id, capture_program_id=None,
    unmodulated_scene_sequence_id=None, capture_crop=None,
):
    from .runtime import with_sensor_effects
    from .temporal import TEMPORAL_EFFECTS as EFFECTS

    assembly = service.registry.get("assemblies", assembly_id)
    scene = service.registry.get("scene_sequences", scene_sequence_id)
    control = service.registry.get("control_sequences", control_sequence_id)
    primary_images = _images(service, scene["payload"])
    if control["assembly_id"] != assembly_id or control["scene_sequence_id"] != scene_sequence_id:
        raise ValueError(
            "Control sequence is associated with a different assembly or scene sequence"
        )
    unmodulated_scene = None
    if unmodulated_scene_sequence_id is not None:
        if (not isinstance(unmodulated_scene_sequence_id, str)
                or not unmodulated_scene_sequence_id):
            raise ValueError("Unmodulated scene requires a registered sequence ID")
        unmodulated_scene = service.registry.get("scene_sequences", unmodulated_scene_sequence_id)
        unmodulated_images = _images(service, unmodulated_scene["payload"])
        fixed = ("wavelength_nm", "unit", "reference_plane", "sampling",
                 "included_effects", "shape")
        if (unmodulated_scene["payload"]["clock"] != scene["payload"]["clock"]
                or any(primary_images[0][key] != unmodulated_images[0][key] for key in fixed)):
            raise ValueError("Illumination components require the same clock, native sampling, "
                             "wavelengths, unit, reference plane and input effects")
    else:
        unmodulated_images = []
    capture_crop = _capture_crop(primary_images, unmodulated_images, capture_crop)
    source_sampling = primary_images[0]["sampling"]
    receiver_sampling = (source_sampling if capture_crop is None else {
        **source_sampling, "crop_origin": capture_crop["crop_origin"],
    })
    receiver_shape = (primary_images[0]["shape"][:2] if capture_crop is None
                      else capture_crop["shape"])
    conditions = {
        k: v for k, v in assembly["conditions"].items() if k not in {"exposure_s", "gain"}
    }
    result = {
        "name": "시간 분광 장면 · 프레임별 Native RAW",
        "revision": "temporal-1",
        "validation_kind": "implementation_verification",
        "metric_id": "raw.channel_mean_dn_sequence",
        "unit": "DN",
        "reference_plane": "sensor_raw",
        "included_effects": EFFECTS,
        "conditions": conditions,
        "required_conditions": list(conditions),
        "absolute_tolerance": {"value": 1.0, "unit": "DN"},
        "uncertainty_policy": "bounded_interval",
        "calibration_dataset_ids": [],
        "clock": control["payload"]["clock"],
        "frame_conditions": frame_conditions(control["payload"]),
        "stimulus": {
            "schema": "temporal_scene_test_v1",
            "scene_sequence_id": scene["id"],
            "scene_sequence_hash": scene["content_hash"],
            "control_sequence_id": control["id"],
            "control_sequence_hash": control["content_hash"],
            **({"capture_crop": capture_crop,
                "source_support": {
                    "kind": source_sampling["kind"],
                    "full_sensor_shape": source_sampling["full_sensor_shape"],
                    "crop_origin": source_sampling["crop_origin"],
                    "shape": primary_images[0]["shape"][:2],
                }} if capture_crop is not None else {}),
        },
    }
    if capture_program_id:
        from .capture_programs import runtime_module

        program = service.registry.get("capture_programs", capture_program_id)
        if program["assembly_id"] != assembly_id:
            raise ValueError("Capture program is pinned to a different assembly")
        module = runtime_module(program["payload"])
        if capture_crop is not None and module.PLUGIN_ID not in {
            "hdr_bracket_v1", "shared_charge_dcg_v1", "reference_hdr_v1",
            "retained_charge_v1",
        }:
            raise ValueError(
                "Registered receiver ROI supports temporal, HDR, reference HDR, DCG "
                "and retained capture"
            )
        if (capture_crop is not None and module.PLUGIN_ID == "retained_charge_v1"
                and program["payload"]["schema"] ==
                "camera_retained_charge_capture_program_v5"):
            raise ValueError("Retained photo HDR inverse requires a full-native sensor")
        if unmodulated_scene is not None and (
            (module.PLUGIN_ID != "hdr_bracket_v1" and not (
                module.PLUGIN_ID == "retained_charge_v1"
                and program["payload"]["schema"] in {
                    "camera_retained_charge_capture_program_v1",
                    "camera_retained_charge_capture_program_v2",
                    "camera_retained_charge_capture_program_v3",
                    "camera_retained_charge_capture_program_v4",
                }
            )) or program["payload"].get("waveform") is None
        ):
            raise ValueError("Two illumination components require HDR or retained "
                             "v1/v2/v3/v4 with an explicit waveform")
        dcg = module.PLUGIN_ID == "shared_charge_dcg_v1"
        reference_hdr = module.PLUGIN_ID == "reference_hdr_v1"
        result.update(
            name="공유 전하 · HCG/LCG RAW" if dcg else "전기 읽기 · LED/PWM · 순차 RAW HDR",
            revision="shared-charge-dcg-1" if dcg else "electrical-hdr-1",
            included_effects=(module.observation_effects(program["payload"]) if dcg
                              else module.capture_effects(program["payload"])),
        )
        result["stimulus"].update(
            schema="shared_charge_dcg_test_v1" if dcg else "electrical_hdr_test_v1",
            capture_program_id=program["id"],
            capture_program_hash=program["content_hash"],
            observation_method="sampled_raw_dark_corrected_v1",
        )
        if reference_hdr:
            result.update(name="기준 프레임 제한 · 움직임 RAW 합성", revision="reference-hdr-1")
            result["stimulus"]["schema"] = "reference_hdr_test_v1"
        if dcg:
            from .dcg_runtime import _raw_formats

            result["metric_id"] = "raw.channel_mean_dn_shared_charge"
            result["stimulus"].update(
                observation_method="sampled_shared_charge_raw_dark_corrected_v1",
                acquisition_topology="ideal_shared_charge_two_conversion_paths",
                readout_ids=["HCG", "LCG"],
                sample_contract={
                    "schema": "camera_shared_charge_samples_v1",
                    "topology": "ideal_shared_charge_two_conversion_paths",
                    "capture_program_id": program["id"],
                    "capture_program_hash": program["content_hash"],
                    "readout_ids": ["HCG", "LCG"],
                    "channels": (
                        ["R", "Gr", "Gb", "B"]
                        if "native_pixel_response" in program["sensor_model_snapshot"]["payload"]
                        else ["R", "G", "B"]
                    ),
                    "raw_formats": _raw_formats(program["payload"]["electrical_model"]),
                    "sampling": receiver_sampling,
                    "raster_shape": receiver_shape,
                    "observation": "sampled_shared_charge_raw_dark_corrected_v1",
                },
            )
        if _nonlinear_program(program["payload"]):
            from .hdr_runtime import NONLINEAR_OBSERVATION, _raw_format

            observation = (module.NONLINEAR_OBSERVATION if dcg else NONLINEAR_OBSERVATION)
            result["revision"] += "-nonlinear-v2"
            result["stimulus"].update(
                schema=("shared_charge_dcg_test_v2" if dcg else
                        "reference_hdr_test_v2" if reference_hdr else "electrical_hdr_test_v2"),
                observation_method=observation,
                dark_correction="not_applied_to_raw_code_metric",
            )
            if dcg:
                result["stimulus"]["sample_contract"].update(
                    schema="camera_shared_charge_samples_v2", observation=observation)
            else:
                result["stimulus"]["sample_contract"] = {
                    "schema": "camera_nonlinear_raw_samples_v1",
                    "capture_program_id": program["id"],
                    "capture_program_hash": program["content_hash"],
                    "raw_format": _raw_format(program["payload"]["electrical_model"]),
                    "sampling": receiver_sampling,
                    "raster_shape": receiver_shape,
                    "observation": observation,
                    "channels": (["R", "Gr", "Gb", "B"] if "native_pixel_response"
                                 in program["sensor_model_snapshot"]["payload"]
                                 else ["R", "G", "B"]),
                }
        if module.PLUGIN_ID == "retained_charge_v1":
            from .retained_charge_runtime import (
                METRIC_ID,
                OBSERVATION,
                _paired,
                _raw_formats,
                event_schedule,
            )

            sampling = receiver_sampling
            shape = receiver_shape
            geometry = {key: sampling[key] for key in ("kind", "full_sensor_shape", "crop_origin")}
            events = event_schedule(control["payload"], program["payload"], geometry, shape)
            model = program["payload"]["electrical_model"]
            result.update(
                name="잔류 전하 · 전달 RAW 신호", revision="retained-charge-1", metric_id=METRIC_ID
            )
            result["stimulus"].update(
                schema="retained_charge_test_v1",
                observation_method=OBSERVATION,
                dark_correction="not_applied_to_delivered_code_metric",
                sample_contract={
                    "schema": "camera_retained_delivered_samples_v1",
                    "capture_program_id": program["id"],
                    "capture_program_hash": program["content_hash"],
                    "topology": "retained_pd_transfer_to_fresh_read_node",
                    "readout_ids": ["HCG", "LCG"] if _paired(model) else ["single"],
                    "channels": (
                        ["R", "Gr", "Gb", "B"]
                        if "native_pixel_response" in program["sensor_model_snapshot"]["payload"]
                        else ["R", "G", "B"]
                    ),
                    "raw_formats": _raw_formats(model),
                    "sampling": sampling,
                    "raster_shape": shape,
                    "observation": OBSERVATION,
                    "events": events,
                },
            )
    elif unmodulated_scene is not None:
        raise ValueError("Two illumination components require a fixed capture program")
    if unmodulated_scene is not None:
        from .hdr_runtime import ILLUMINATION_COMPONENT_SCHEMA

        retained_components = module.PLUGIN_ID == "retained_charge_v1"
        nonlinear = _nonlinear_program(program["payload"])
        result["name"] = ("독립 비변조·PWM 분광 영상 · 잔류 전하 RAW" if retained_components
                          else "독립 비변조·PWM 분광 영상 · 순차 RAW")
        result["revision"] = (
            "retained-charge-components-1" if retained_components
            else "electrical-hdr-components-2" if nonlinear
            else "electrical-hdr-components-1"
        )
        result["stimulus"].update(
            schema="retained_charge_components_test_v1" if retained_components
                   else "electrical_hdr_components_test_v2" if nonlinear
                   else "electrical_hdr_components_test_v1",
            unmodulated_scene_sequence_id=unmodulated_scene["id"],
            unmodulated_scene_sequence_hash=unmodulated_scene["content_hash"],
            illumination_component_schema=ILLUMINATION_COMPONENT_SCHEMA,
        )
        result["included_effects"].append(
            "independent_unmodulated_and_modulated_ideal_image_components"
        )
        if retained_components and program["payload"]["schema"] in {
            "camera_retained_charge_capture_program_v2",
            "camera_retained_charge_capture_program_v4",
        }:
            result["included_effects"].append(
                "combined_photo_generation_before_single_trap_state"
            )
    if capture_program_id and module.PLUGIN_ID == "electrical_control_v1":
        from .electrical_control_workflow import update_test

        update_test(result, program, control, scene)
    sensor = next(model for model in (service.registry.get("models", ident)
                  for ident in assembly["model_ids"]) if model["kind"] == "sensor")
    result["included_effects"] = with_sensor_effects(result["included_effects"], sensor["payload"])
    return result


def prepare_run(service, data):
    from .composer import check_assembly
    from .imaging import resolve_isp
    from .native_model_evidence import verify_models
    from .runtime import get_plugin, sensor_response_fields
    from .service import PreflightError
    from .temporal import TemporalSceneRuntime
    from .timing import validate_control_sequence, validate_timing_coverage

    environment = _current(service)
    if (
        data["scene_id"] is not None
        or not data["scene_sequence_id"]
        or not data["control_sequence_id"]
    ):
        raise ValueError("Temporal simulation requires scene/control sequences, never single Scene")
    retained = data["plugin_id"] == "retained_charge_v1"
    feedback = data["plugin_id"] == "electrical_control_v1"
    field_radiometry_mode = data["parameters"].get("field_radiometry_mode")
    if "field_radiometry_mode" in data["parameters"]:
        from .spatial import SOURCE_RI_MODE

        if (data["plugin_id"] not in {
            "temporal_scene_v1", "hdr_bracket_v1", "shared_charge_dcg_v1",
            "retained_charge_v1",
        }
                or type(field_radiometry_mode) is not str
                or field_radiometry_mode != SOURCE_RI_MODE):
            raise ValueError(
                "Source-position RI supports temporal_scene_v1, hdr_bracket_v1, "
                "shared_charge_dcg_v1, or the declared retained_charge_v1 "
                "trapped paired PWM two-component path"
            )
    allowed = {"seed", "noise_enabled"} if retained or feedback else {"seed"}
    if data["plugin_id"] in {
        "temporal_scene_v1", "hdr_bracket_v1", "shared_charge_dcg_v1",
        "reference_hdr_v1", "retained_charge_v1",
    }:
        allowed.add("capture_crop")
    if data["plugin_id"] in {
        "temporal_scene_v1", "hdr_bracket_v1", "shared_charge_dcg_v1",
        "retained_charge_v1",
    }:
        allowed.add("field_radiometry_mode")
    if data["plugin_id"] in {"hdr_bracket_v1", "retained_charge_v1"}:
        allowed.add("unmodulated_scene_sequence_id")
    if (
        "noise_enabled" in data["parameters"]
        and type(data["parameters"]["noise_enabled"]) is not bool
    ):
        raise ValueError("Retained noise_enabled must be an explicit boolean")
    if set(data["parameters"]) - allowed:
        raise ValueError(
            "Recorded settings determine exposure/gain/frame count; only noise seed is configurable"
        )
    unmodulated_scene_id = data["parameters"].get("unmodulated_scene_sequence_id")
    if unmodulated_scene_id is not None and (
        data["plugin_id"] not in {"hdr_bracket_v1", "retained_charge_v1"}
        or not isinstance(unmodulated_scene_id, str) or not unmodulated_scene_id
    ):
        raise ValueError("Unmodulated scene requires a registered HDR/retained sequence ID")
    assembly = service.registry.get("assemblies", data["assembly_id"])
    scene = service.registry.get("scene_sequences", data["scene_sequence_id"])
    unmodulated_scene = (service.registry.get("scene_sequences", unmodulated_scene_id)
                         if unmodulated_scene_id else None)
    dual_3d_light_evidence = None
    if unmodulated_scene is not None and all(
        isinstance(record.get("fixture_specification"), dict)
        and record["fixture_specification"].get("schema")
        == "registered_3d_temporal_quadrature_v1"
        for record in (scene, unmodulated_scene)
    ):
        # Two actual 3D components must partition the same frozen two-Light
        # Scene. Otherwise an all-light timeline could silently double-count
        # one emitter, or unrelated cameras could be combined as LED+ambient.
        from .scene_render import verify_light_component_sequences

        dual_3d_light_evidence = verify_light_component_sequences(
            service, scene["id"], unmodulated_scene["id"]
        )
    images = _images(service, scene["payload"])
    unmodulated_images = (_images(service, unmodulated_scene["payload"])
                          if unmodulated_scene is not None else [])
    capture_crop = _capture_crop(
        images, unmodulated_images, data["parameters"].get("capture_crop")
    )
    if capture_crop is not None:
        if data["plugin_id"] not in {
            "temporal_scene_v1", "hdr_bracket_v1", "shared_charge_dcg_v1",
            "reference_hdr_v1", "retained_charge_v1",
        }:
            raise ValueError(
                "Registered receiver ROI supports temporal, HDR, reference HDR, DCG "
                "and retained capture"
            )
        data["parameters"] = {**data["parameters"], "capture_crop": capture_crop}
    control = service.registry.get("control_sequences", data["control_sequence_id"])
    test = service.registry.get("tests", data["test_id"])
    capture_program = None
    if data["plugin_id"] in {
        "hdr_bracket_v1",
        "shared_charge_dcg_v1",
        "reference_hdr_v1",
        "retained_charge_v1",
        "electrical_control_v1",
    }:
        if not data.get("capture_program_id"):
            raise ValueError("Electrical/HDR simulation requires a fixed capture program")
        capture_program = service.registry.get("capture_programs", data["capture_program_id"])
        from .capture_programs import runtime_for_payload, verify_program

        if runtime_for_payload(capture_program["payload"]) != data["plugin_id"]:
            raise ValueError("Capture program topology differs from selected runtime")
        if (capture_crop is not None and retained and capture_program["payload"]["schema"]
                == "camera_retained_charge_capture_program_v5"):
            raise ValueError("Retained photo HDR inverse requires a full-native sensor")
        if (data["plugin_id"] in {"hdr_bracket_v1", "shared_charge_dcg_v1",
                                  "reference_hdr_v1", "retained_charge_v1",
                                  "electrical_control_v1"}
                or _nonlinear_program(capture_program["payload"])):
            verify_program(service, capture_program, data["plugin_id"])
        # A cached result still depends on preserved source/normalized evidence.
        for artifact in capture_program["artifacts"]:
            service.registry.blob_path(artifact["sha256"])
    if field_radiometry_mode is not None and retained:
        from .retained_charge_runtime import TRAPPED_GAIN_MERGE_PROGRAM_SCHEMA

        program = capture_program["payload"]
        if (program["schema"] != TRAPPED_GAIN_MERGE_PROGRAM_SCHEMA
                or not program["electrical_model"]["schema"].startswith(
                    "camera_shared_charge_readout_")
                or program["waveform"] is None
                or program["waveform"]["kind"] != "pwm"
                or unmodulated_scene is None):
            raise ValueError(
                "Retained source-position RI requires a trapped HCG/LCG capture program, "
                "explicit PWM and two independent scene sequences"
            )
    if unmodulated_scene is not None and retained and (
        capture_program["payload"]["schema"] not in {
            "camera_retained_charge_capture_program_v1",
            "camera_retained_charge_capture_program_v2",
            "camera_retained_charge_capture_program_v3",
            "camera_retained_charge_capture_program_v4",
        }
        or capture_program["payload"].get("waveform") is None
    ):
        raise ValueError("Retained two-component acquisition supports v1/v2/v3/v4 "
                         "with an explicit waveform only")
    expected = test_template(
        service, assembly["id"], scene["id"], control["id"],
        capture_program["id"] if capture_program else None,
        unmodulated_scene_id, capture_crop,
    )
    for key in [
        "metric_id",
        "unit",
        "reference_plane",
        "stimulus",
        "clock",
        "frame_conditions",
        "conditions",
    ]:
        if test.get(key) != expected[key]:
            raise ValueError(
                "Temporal Test must preserve the exact sequence, clock, and applied conditions"
            )
    if (set(test["included_effects"]) != set(expected["included_effects"])
            or test.get("observation_method")):
        raise ValueError("Temporal Test must use the implemented frame observation effects")
    models = [service.registry.get("models", i) for i in assembly["model_ids"]]
    response_artifacts = verify_models(service, models)
    roles = {m["kind"]: m for m in models}
    for image in [*images, *unmodulated_images]:
        _verify_rendered_carrier_sensor(image, roles["sensor"])
    sensor = roles["sensor"]["payload"]
    lens = copy.deepcopy(roles["lens"]["payload"])
    psf = lens.pop("psf", None)
    if psf is None:
        raise ValueError("Temporal camera requires an explicit pixel-integrated PSF")
    sampling = images[0]["sampling"]
    geometry = {k: sampling[k] for k in ["kind", "full_sensor_shape", "crop_origin"]}
    frame_shape = images[0]["shape"][:2]
    if capture_crop is not None:
        geometry["crop_origin"] = capture_crop["crop_origin"]
        frame_shape = capture_crop["shape"]
    _, _, pitch, full = _native(service, assembly["id"])
    if sampling["full_sensor_shape"] != full or sampling["pixel_pitch_um"] != pitch:
        raise ValueError("Temporal scene and sensor native geometry differ")
    for record in images:
        optical = {k: v for k, v in record["conditions"].items() if k not in {"exposure_s", "gain"}}
        if optical != test["conditions"]:
            raise ValueError(
                "Every temporal image must retain all Test optical/environment conditions"
            )
    for record in unmodulated_images:
        optical = {k: v for k, v in record["conditions"].items()
                   if k not in {"exposure_s", "gain"}}
        if optical != test["conditions"]:
            raise ValueError(
                "Every unmodulated image must retain Test optical/environment conditions"
            )
    controls = validate_control_sequence(
        control["payload"], geometry=geometry, frame_shape=frame_shape
    )
    validate_timing_coverage(
        scene["payload"], controls, geometry=geometry, frame_shape=frame_shape
    )
    if unmodulated_scene is not None:
        validate_timing_coverage(
            unmodulated_scene["payload"], controls, geometry=geometry,
            frame_shape=frame_shape,
        )
    if _format(assembly["raw_format"]) != _format(roles["sensor"]["raw_format"]):
        raise ValueError("Native sensor RAW format differs from the pinned assembly")
    expected_raw = {
        "cfa": "RGGB",
        "bit_depth": sensor["readout"]["bit_depth"],
        "packing": "unpacked",
        "black_level_dn": sensor["readout"]["black_level_dn"],
        "layout": "bayer",
        "hdr_streams": 1,
    }
    if _format(assembly["raw_format"]) != _format(expected_raw):
        raise ValueError("Temporal runtime RAW contract differs from declared sensor readout")
    if test["conditions"].get("temperature_c", {}).get("unit") != "degC":
        raise ValueError("Temporal temperature_c must use degC")
    profiles = {}
    frame_domains = []
    all_models = {m["id"]: m for m in models}
    reports = []
    for frame in controls["frames"]:
        applied = frame["applied"]
        isp = service.registry.get("models", applied["isp_model_id"])
        conditions = {
            **test["conditions"],
            "exposure_s": {"value": applied["exposure_ns"] * 1e-9, "unit": "s"},
            "gain": {"value": applied["gain"], "unit": "1"},
        }
        frame_models = [roles["lens"], roles["sensor"], isp]
        composed = {
            **assembly,
            "model_ids": [m["id"] for m in frame_models],
            "conditions": conditions,
        }
        report = check_assembly(composed, frame_models)
        if report["status"] != "PASS":
            raise PreflightError(report)
        if feedback and applied["gain"] != assembly["conditions"]["gain"]["value"]:
            from .gain_semantics import model_declarations

            unresolved = [item for item in model_declarations(frame_models)
                          if item["status"] != "declared"]
            if unresolved:
                raise PreflightError({
                    "status": "INCONCLUSIVE",
                    "issues": [{
                        "code": "analog_gain_model_role_undeclared",
                        "severity": "unknown",
                        "path": f"models.{item['model_id']}.properties.analog_gain_applicability",
                        "message": (
                            "Changing electrical analog gain needs this model's explicit role"
                        ),
                    } for item in unresolved],
                })
        profiles[isp["id"]] = resolve_isp(isp)
        all_models[isp["id"]] = isp
        frame_domain = service._domain_intersection(frame_models)
        if data["plugin_id"] == "shared_charge_dcg_v1":
            frame_domain = _dcg_valid_domain(frame_domain, capture_program["payload"])
        elif retained or feedback:
            domain_payload = capture_program["payload"]
            if feedback:
                from .electrical_control_runtime import retained_program

                domain_payload = retained_program(domain_payload)
            frame_domain = _retained_valid_domain(frame_domain, domain_payload)
        elif capture_program:
            frame_domain = _nonlinear_valid_domain(frame_domain, capture_program["payload"])
        frame_domains.append(
            {
                "frame_id": frame["frame_id"],
                "valid_domain": frame_domain,
            }
        )
        reports.append({"frame_id": frame["frame_id"], "report": report})
        if feedback:
            from .electrical_control_workflow import check_exposure_domains

            check_exposure_domains(
                assembly, frame_models, conditions, capture_program["payload"], frame_domain
            )
    for frame in controls["frames"]:
        if frame.get("commanded"):
            identity = frame["commanded"]["isp_model_id"]
            if identity not in profiles:
                isp = service.registry.get("models", identity)
                profiles[identity] = resolve_isp(isp)
                all_models[identity] = isp
    request = {
        "wavelength_nm": sensor["wavelength_nm"],
        **sensor_response_fields(sensor),
        "pixel_pitch_um": pitch,
        "readout": sensor["readout"],
        "lens": lens,
        "psf": psf,
        "temperature_c": test["conditions"]["temperature_c"]["value"],
        "seed": data["parameters"].get("seed", 42),
        **({"field_radiometry_mode": field_radiometry_mode}
           if field_radiometry_mode is not None else {}),
        "isp_profiles": profiles,
        "control_sequence": controls,
        "scene_sequence": {
            **{k: v for k, v in scene["payload"].items() if k != "knots"},
            "knots": [
                {
                    "time_ns": k["time_ns"],
                    "scene": _capture_scene(
                        service, record, roles["lens"]["id"], capture_crop),
                }
                for k, record in zip(scene["payload"]["knots"], images, strict=True)
            ],
        },
    }
    if unmodulated_scene is not None:
        request["unmodulated_scene_sequence"] = {
            **{k: v for k, v in unmodulated_scene["payload"].items() if k != "knots"},
            "knots": [
                {"time_ns": k["time_ns"],
                 "scene": _capture_scene(
                     service, record, roles["lens"]["id"], capture_crop)}
                for k, record in zip(unmodulated_scene["payload"]["knots"],
                                     unmodulated_images, strict=True)
            ],
        }
    runtime = TemporalSceneRuntime()
    if capture_program:
        runtime = get_plugin(data["plugin_id"])
        request["capture_program"] = capture_program["payload"]
    if feedback:
        request["noise_enabled"] = data["parameters"].get("noise_enabled", True)
    if retained:
        from .retained_charge_runtime import event_schedule

        request["noise_enabled"] = data["parameters"].get("noise_enabled", True)
        request["retained_charge_events"] = event_schedule(
            controls, capture_program["payload"], geometry, frame_shape)
    issues = runtime.validate_request(request)
    if issues:
        raise PreflightError({"status": "FAIL", "issues": issues})
    resources = runtime.estimate_resources(request)
    # Preserve IDs and checked source artifacts, not duplicate large arrays in SQLite.
    request["scene_sequence"] = scene["payload"]
    if unmodulated_scene is not None:
        request["unmodulated_scene_sequence"] = unmodulated_scene["payload"]
    artifacts = {
        (a["sha256"], a["name"]): a
        for record in [scene, control, *images,
                       *([unmodulated_scene, *unmodulated_images]
                         if unmodulated_scene is not None else []),
                       *([capture_program] if capture_program else [])]
        for a in record["artifacts"]
    }
    artifacts.update({(a["sha256"], a["name"]): a for a in response_artifacts})
    frozen = {
        "name": assembly["name"],
        "job_kind": "camera_simulation",
        "plugin_id": runtime.plugin_id,
        "assembly_id": assembly["id"],
        "test_id": test["id"],
        "scene_id": None,
        "scene_sequence_id": scene["id"],
        "control_sequence_id": control["id"],
        "parameters": data["parameters"],
        "request": request,
        "environment": environment,
        "assembly_snapshot": assembly,
        "model_snapshots": list(all_models.values()),
        "test_snapshot": test,
        "scene_sequence_snapshot": scene,
        "control_sequence_snapshot": control,
        "frame_valid_domains": frame_domains,
        "input_artifacts": list(artifacts.values()),
        "compatibility": {"status": "PASS", "frames": reports},
        "resources": resources,
    }
    if unmodulated_scene is not None:
        frozen.update(
            unmodulated_scene_sequence_id=unmodulated_scene["id"],
            unmodulated_scene_sequence_snapshot=unmodulated_scene,
        )
    if dual_3d_light_evidence is not None:
        frozen["dual_3d_light_evidence"] = dual_3d_light_evidence
    if capture_program:
        frozen.update(
            capture_program_id=capture_program["id"],
            capture_program_snapshot=capture_program,
        )
    return frozen, data["force"]


def execute_run(service, job):
    from .imaging import store_dcg_previews, store_frame_previews, store_output
    from .native_model_evidence import verify_models
    from .runtime import get_plugin, sensor_response_fields

    if job["environment"] != _current(service):
        raise ValueError("Worker differs from frozen temporal environment")
    dcg = job["plugin_id"] == "shared_charge_dcg_v1"
    retained = job["plugin_id"] == "retained_charge_v1"
    feedback = job["plugin_id"] == "electrical_control_v1"
    temporal = job["plugin_id"] == "temporal_scene_v1"
    if (dcg or retained or feedback or temporal
            or job["plugin_id"] in {"hdr_bracket_v1", "reference_hdr_v1"}
            or _nonlinear_program(job.get("capture_program_snapshot", {}).get("payload", {}))):
        # Rebuild the entire submission from the fixed registry sources. This binds
        # every runtime field, model, sequence and artifact before any acquisition.
        from .contracts import RunCreate

        data = RunCreate.model_validate({
            key: job[key] for key in RunCreate.model_fields if key in job
        }).model_dump()
        expected, _ = prepare_run(service, data)
        mismatch = (any(canonical(job.get(key)) != canonical(value)
                        for key, value in expected.items()) if retained or feedback
                    else any(job.get(key) != value for key, value in expected.items()))
        if mismatch:
            label = "DCG" if dcg else "Temporal" if temporal else "Electrical HDR"
            raise ValueError(f"{label} queued request differs from its fixed source evidence")
    verify_models(service, job["model_snapshots"])
    for artifact in job["input_artifacts"]:
        service.registry.blob_path(artifact["sha256"])
    native_sensor = next((m for m in job["model_snapshots"]
                          if m["kind"] == "sensor" and "native_pixel_response" in m["payload"]),
                         None)
    if native_sensor is not None:
        sensor = native_sensor["payload"]
        optical = sensor_response_fields(job["request"])
        if optical != sensor_response_fields(sensor):
            raise ValueError("Temporal request native response differs from the fixed sensor")
        for field in ("wavelength_nm", "pixel_pitch_um", "readout"):
            if job["request"][field] != sensor[field]:
                raise ValueError("Temporal request sensor contract differs from the fixed model")
        lens = copy.deepcopy(next(m["payload"] for m in job["model_snapshots"]
                                  if m["kind"] == "lens"))
        psf = lens.pop("psf", None)
        if job["request"]["lens"] != lens or job["request"]["psf"] != psf:
            raise ValueError("Temporal request optics differ from the fixed native lens")
    if job["plugin_id"] in {
        "hdr_bracket_v1",
        "shared_charge_dcg_v1",
        "reference_hdr_v1",
        "retained_charge_v1",
        "electrical_control_v1",
    }:
        # Submission checks cannot protect a queued job from later blob damage.
        # Validate its frozen evidence before loading images or acquiring RAW.
        for artifact in job["capture_program_snapshot"]["artifacts"]:
            service.registry.blob_path(artifact["sha256"])
    request = copy.deepcopy(job["request"])
    capture_crop = job["parameters"].get("capture_crop")
    scene = job["scene_sequence_snapshot"]
    images = scene["image_snapshots"]
    request["scene_sequence"] = {
        **{k: v for k, v in scene["payload"].items() if k != "knots"},
        "knots": [
            {
                "time_ns": k["time_ns"],
                "scene": _capture_scene(
                    service, r,
                    next(m["id"] for m in job["model_snapshots"] if m["kind"] == "lens"),
                    capture_crop,
                ),
            }
            for k, r in zip(scene["payload"]["knots"], images, strict=True)
        ],
    }
    if job.get("unmodulated_scene_sequence_id"):
        unmodulated_scene = job["unmodulated_scene_sequence_snapshot"]
        unmodulated_images = unmodulated_scene["image_snapshots"]
        request["unmodulated_scene_sequence"] = {
            **{k: v for k, v in unmodulated_scene["payload"].items() if k != "knots"},
            "knots": [
                {"time_ns": k["time_ns"],
                 "scene": _capture_scene(
                     service, record,
                     next(m["id"] for m in job["model_snapshots"] if m["kind"] == "lens"),
                     capture_crop,
                 )}
                for k, record in zip(unmodulated_scene["payload"]["knots"],
                                     unmodulated_images, strict=True)
            ],
        }
    runtime = get_plugin(job["plugin_id"])
    output = runtime.execute(request)
    summary = output["summary"]
    realized_control = summary["control_sequence"] if feedback else request["control_sequence"]
    if not dcg:
        summary["frame_count"] = len(request["control_sequence"]["frames"])
    from .uncertainty import attach, unsupported_budget

    budget = unsupported_budget(job["model_snapshots"], job["test_snapshot"], job["plugin_id"])
    if budget:
        attach(summary, budget)
    timing = {f["frame_id"]: f for f in summary["timing"]["frames"]}
    if not dcg:
        summary["frames"] = [
            {**copy.deepcopy(frame), "frame_index": index, "timing": timing[frame["frame_id"]]}
            for index, frame in enumerate(realized_control["frames"])
        ]
    summary.update(
        conditions=job["test_snapshot"]["conditions"],
        clock=request["control_sequence"]["clock"],
        frame_conditions=frame_conditions(realized_control),
        scene_sequence_id=job["scene_sequence_id"],
        control_sequence_id=job["control_sequence_id"],
    )
    if job.get("unmodulated_scene_sequence_id"):
        summary.update(
            unmodulated_scene_sequence_id=job["unmodulated_scene_sequence_id"],
            unmodulated_scene_sequence_hash=(
                job["unmodulated_scene_sequence_snapshot"]["content_hash"]),
        )
    if job.get("dual_3d_light_evidence") is not None:
        summary["dual_3d_light_evidence"] = copy.deepcopy(job["dual_3d_light_evidence"])
    if job.get("capture_program_id"):
        summary.update(
            capture_program_id=job["capture_program_id"],
            capture_program_hash=job["capture_program_snapshot"]["content_hash"],
            electrical_model_authority=job["capture_program_snapshot"]["electrical_model_authority"],
        )
    prediction_domain = service._domain_intersection(job["model_snapshots"])
    if dcg:
        prediction_domain = _dcg_valid_domain(
            prediction_domain, job["capture_program_snapshot"]["payload"])
    elif retained or feedback:
        domain_payload = job["capture_program_snapshot"]["payload"]
        if feedback:
            from .electrical_control_runtime import retained_program

            domain_payload = retained_program(domain_payload)
        prediction_domain = _retained_valid_domain(prediction_domain, domain_payload)
    elif job.get("capture_program_snapshot"):
        prediction_domain = _nonlinear_valid_domain(
            prediction_domain, job["capture_program_snapshot"]["payload"])
    summary["prediction"].update(
        id=job["id"],
        conditions=summary["conditions"],
        clock=summary["clock"],
        frame_conditions=summary["frame_conditions"],
        frame_valid_domains=job["frame_valid_domains"],
        valid_domain=prediction_domain,
        calibration_dataset_ids=sorted(
            {i for m in job["model_snapshots"] for i in m["calibration_dataset_ids"]}
        ),
        dataset_ids=[job["id"]],
        assembly_id=job["assembly_id"],
        test_id=job["test_id"],
    )
    if (
        dcg
        or retained
        or _nonlinear_program(job.get("capture_program_snapshot", {}).get("payload", {}))
    ):
        summary["prediction"]["sample_geometry"] = copy.deepcopy(
            job["test_snapshot"]["stimulus"]["sample_contract"]
        )
    elif (
        job["test_snapshot"]["stimulus"].get("schema") in {
            "temporal_scene_test_v1",
            "electrical_hdr_test_v1",
            "electrical_hdr_components_test_v1",
            "reference_hdr_test_v1",
            "reference_hdr_test_v2",
        }
        and "capture_crop" in job["test_snapshot"]["stimulus"]
    ):
        stimulus = job["test_snapshot"]["stimulus"]
        summary["prediction"]["sample_geometry"] = {
            key: copy.deepcopy(stimulus[key]) for key in ("capture_crop", "source_support")
        }
    if feedback:
        from .electrical_control_workflow import attach_realized_contract

        attach_realized_contract(summary, job)
    previews = store_dcg_previews(service, output) if dcg else store_frame_previews(service, output)
    return store_output(
        service,
        output,
        source_kind="simulated",
        geometry=summary["geometry"],
        input_artifacts=[*job["input_artifacts"], *previews],
    )
