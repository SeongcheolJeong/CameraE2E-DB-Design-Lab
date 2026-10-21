"""Immutable feedback policies/programs, full-domain preflight and worker execution."""

import json
from copy import deepcopy
from decimal import Decimal
from itertools import product

from .closed_loop import ClosedLoopSceneRuntime
from .control_contracts import ClosedLoopProgram, ControlPolicyCreate, ControlProgramCreate
from .controls import default_policy
from .imaging import _current, _format, _native, _scene_input, resolve_isp
from .registry import canonical, digest
from .sequences import _images
from .temporal import TEMPORAL_EFFECTS

COLLECTIONS = {"control_policies": ControlPolicyCreate, "control_programs": ControlProgramCreate}


def _provenance():
    return {
        "source_kind": "estimated",
        "source_uri": "fixture://authored-camera-control",
        "license": "MIT; authored simulation control policy, no hardware accuracy claim",
        "method_and_version": "camera_control_policy_v1:authored",
        "physical_instance": None,
        "parents": [],
    }


def policy_template(service, scene_sequence_id):
    scene = service.registry.get("scene_sequences", scene_sequence_id)
    shape = _images(service, scene["payload"])[0]["shape"][:2]
    return {
        **_provenance(),
        "name": "RAW 통계 AE/AWB · 제어 범위를 확인하세요",
        "revision": "1",
        "payload": default_policy(shape),
    }


def _persist(service, kind, data, supplied, snapshots, original_text):
    for parent in data["parents"]:
        service.registry.get(kind, parent)
    if data["source_kind"] == "measured" and not data["physical_instance"]:
        raise ValueError("Measured source requires its physical sample identity")
    raw = (
        original_text.encode("utf-8") if original_text is not None else canonical(supplied).encode()
    )
    if len(raw) > 1_000_000:
        raise ValueError("Control source JSON exceeds 1 MB")
    prefix = "control-policy" if kind == "control_policies" else "control-program"
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


def create_policy(service, payload, *, original_text=None):
    data = ControlPolicyCreate.model_validate(payload).model_dump()
    return _persist(service, "control_policies", data, payload, {}, original_text)


def import_record(service, kind, document_text):
    if not isinstance(document_text, str) or len(document_text.encode("utf-8")) > 1_000_000:
        raise ValueError("Control source JSON must be at most 1 MB")
    if kind not in COLLECTIONS:
        raise ValueError("Unknown control collection")
    create = create_policy if kind == "control_policies" else create_program
    return create(service, json.loads(document_text), original_text=document_text)


def expand_program(schedule, policy):
    return ClosedLoopProgram.model_validate(
        {
            **{k: v for k, v in schedule.items() if k != "schema"},
            "schema": "closed_loop_program_v1",
            "policy": policy,
        }
    ).model_dump()


def program_template(service, assembly_id, scene_sequence_id, control_policy_id, frame_count=8):
    if type(frame_count) is not int or not 1 <= frame_count <= 64:
        raise ValueError("Control program requires 1..64 frames")
    assembly, sensor, _, _ = _native(service, assembly_id)
    scene = service.registry.get("scene_sequences", scene_sequence_id)
    policy = service.registry.get("control_policies", control_policy_id)
    isp = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "isp"
    )
    exposure = Decimal(str(assembly["conditions"]["exposure_s"]["value"])) * 1_000_000_000
    if assembly["conditions"]["exposure_s"]["unit"] != "s" or exposure != int(exposure):
        raise ValueError("Initial exposure must be an exact integer ns value expressed in seconds")
    maximum = (
        policy["payload"]["ae"]["exposure_ns"]["maximum"]
        if policy["payload"]["ae"]["enabled"]
        else int(exposure)
    )
    begin, end = [scene["payload"]["knots"][i]["time_ns"] for i in (0, -1)]
    period = (end - begin - maximum) // max(1, frame_count - 1)
    if end - begin < maximum or (frame_count > 1 and period < maximum):
        raise ValueError("Scene is too short for requested frame count and maximum AE exposure")
    return {
        **_provenance(),
        "name": "폐루프 촬영 프로그램",
        "revision": "1",
        "assembly_id": assembly_id,
        "scene_sequence_id": scene_sequence_id,
        "control_policy_id": control_policy_id,
        "payload": {
            "schema": "camera_control_schedule_v1",
            "clock": scene["payload"]["clock"],
            "mode": {
                "shutter": "global",
                "line_time_ns": 0,
                "hidden_top_rows": 0,
                "readout_basis": "full_sensor_rows",
            },
            "initial": {
                "exposure_ns": int(exposure),
                "gain": sensor["payload"]["readout"]["gain"],
                "isp_model_id": isp["id"],
                "wb_gains_rgb": resolve_isp(isp)["white_balance"]["gains_rgb"],
            },
            "frames": [
                {"frame_id": f"controlled-{i}", "frame_start_ns": begin + i * period}
                for i in range(frame_count)
            ],
        },
    }


def check_control_domain(assembly, models, program):
    """Prove interval containment for every model; corners also check fixed conditions.

    The supported valid_domain contract is an axis-aligned box. Interval inclusion
    proves coverage of this entire declared control box, not only sampled settings.
    Other model applicability constraints are fixed-condition equality checks.
    """
    from .composer import check_assembly
    from .service import PreflightError
    from .validation import check_domain

    initial, ae = program["initial"], program["policy"]["ae"]
    exposures = (
        [ae["exposure_ns"][k] for k in ("minimum", "maximum")]
        if ae["enabled"]
        else [initial["exposure_ns"]] * 2
    )
    gains = (
        [ae["readout_gain"][k] for k in ("minimum", "maximum")]
        if ae["enabled"]
        else [initial["gain"]] * 2
    )
    bounds = {
        "exposure_s": {"min": exposures[0] * 1e-9, "max": exposures[1] * 1e-9, "unit": "s"},
        "gain": {"min": gains[0], "max": gains[1], "unit": "1"},
    }
    issues, evidence = [], []
    static = {k: v for k, v in assembly["conditions"].items() if k not in bounds}
    axes = [
        {"name": k, "unit": v["unit"], "values": [v["min"], v["max"]]} for k, v in bounds.items()
    ]
    for model in models:
        for axis in bounds:
            if axis not in model["valid_domain"]:
                issues.append(
                    {
                        "code": "control_domain_missing",
                        "severity": "unknown",
                        "path": f"models.{model['id']}.valid_domain.{axis}",
                        "message": "Declare model applicability for the complete control range",
                    }
                )
        issues += check_domain(
            static, model["valid_domain"], axes=axes, path=f"models.{model['id']}.valid_domain"
        )
        evidence.append(
            {
                "model_id": model["id"],
                "model_hash": model["content_hash"],
                "valid_domain": model["valid_domain"],
            }
        )
    corners = []
    for exposure, gain in product(sorted(set(exposures)), sorted(set(gains))):
        conditions = {
            **static,
            "exposure_s": {"value": exposure * 1e-9, "unit": "s"},
            "gain": {"value": gain, "unit": "1"},
        }
        report = check_assembly({**assembly, "conditions": conditions}, models)
        corners.append({"conditions": conditions, "report": report})
        if report["status"] != "PASS":
            issues += report["issues"]
    result = {
        "status": "FAIL" if issues else "PASS",
        "issues": issues,
        "control_bounds": bounds,
        "domain_proof": "axis_aligned_interval_containment",
        "models": evidence,
        "corner_compatibility": corners,
        "physical_validation": "not_established",
    }
    if issues:
        raise PreflightError(result)
    return result


def _prepare_program(service, payload):
    data = ControlProgramCreate.model_validate(payload).model_dump()
    assembly, sensor_record, pitch, full = _native(service, data["assembly_id"])
    scene = service.registry.get("scene_sequences", data["scene_sequence_id"])
    policy = service.registry.get("control_policies", data["control_policy_id"])
    program = expand_program(data["payload"], policy["payload"])
    images = _images(service, scene["payload"])
    models = [service.registry.get("models", i) for i in assembly["model_ids"]]
    roles = {m["kind"]: m for m in models}
    if program["initial"]["isp_model_id"] != roles["isp"]["id"]:
        raise ValueError("Closed-loop program must use the ISP pinned by its camera assembly")
    compatibility = check_control_domain(assembly, models, program)
    sensor = sensor_record["payload"]
    sampling = images[0]["sampling"]
    if sampling["full_sensor_shape"] != full or sampling["pixel_pitch_um"] != pitch:
        raise ValueError("Control scene sampling differs from the native camera sensor")
    static = {k: v for k, v in assembly["conditions"].items() if k not in {"exposure_s", "gain"}}
    for record in images:
        optical = {k: v for k, v in record["conditions"].items() if k not in {"exposure_s", "gain"}}
        if optical != static:
            raise ValueError(
                "Every control scene image must preserve all optical/environment conditions"
            )
    if static.get("temperature_c", {}).get("unit") != "degC":
        raise ValueError("Closed-loop temperature_c requires degC")
    raw = {
        "cfa": "RGGB",
        "bit_depth": sensor["readout"]["bit_depth"],
        "packing": "unpacked",
        "black_level_dn": sensor["readout"]["black_level_dn"],
        "layout": "bayer",
        "hdr_streams": 1,
    }
    if _format(assembly["raw_format"]) != _format(sensor_record["raw_format"]) or _format(
        assembly["raw_format"]
    ) != _format(raw):
        raise ValueError("Pinned RAW contract differs from the implemented native readout")
    lens = deepcopy(roles["lens"]["payload"])
    psf = lens.pop("psf", None)
    if psf is None:
        raise ValueError("Closed-loop camera requires an explicit pixel-integrated PSF")
    request = {
        "wavelength_nm": sensor["wavelength_nm"],
        "qe": sensor["qe"],
        "pixel_pitch_um": pitch,
        "readout": sensor["readout"],
        "lens": lens,
        "psf": psf,
        "sensor_angular_response": sensor["sensor_angular_response"],
        "temperature_c": static["temperature_c"]["value"],
        "control_program": program,
        "isp_profiles": {roles["isp"]["id"]: resolve_isp(roles["isp"])},
        "scene_sequence": {
            **scene["payload"],
            "knots": [
                {
                    "time_ns": k["time_ns"],
                    "scene": _scene_input(service, image, lens_model_id=roles["lens"]["id"]),
                }
                for k, image in zip(scene["payload"]["knots"], images, strict=True)
            ],
        },
    }
    runtime = ClosedLoopSceneRuntime()
    from .runtime import RuntimeRequestError
    from .service import PreflightError

    try:
        resources = runtime.estimate_resources(request)
    except RuntimeRequestError as exc:
        raise PreflightError({"status": "FAIL", "issues": exc.issues}) from exc
    request["scene_sequence"] = deepcopy(scene["payload"])
    snapshots = {
        "assembly_snapshot": assembly,
        "model_snapshots": models,
        "scene_sequence_snapshot": scene,
        "control_policy_snapshot": policy,
        "compatibility": compatibility,
        "resources": resources,
        "expanded_program_sha256": digest(program),
    }
    return data, request, snapshots, images


def check_program(service, payload):
    data, request, snapshots, _ = _prepare_program(service, payload)
    return {
        "specification": data,
        "expanded_program": request["control_program"],
        **{k: snapshots[k] for k in ("compatibility", "resources", "expanded_program_sha256")},
    }


def create_program(service, payload, *, original_text=None):
    data, _, snapshots, _ = _prepare_program(service, payload)
    return _persist(service, "control_programs", data, payload, snapshots, original_text)


def test_template(service, control_program_id):
    record = service.registry.get("control_programs", control_program_id)
    assembly = record["assembly_snapshot"]
    scene, policy = record["scene_sequence_snapshot"], record["control_policy_snapshot"]
    static = {k: v for k, v in assembly["conditions"].items() if k not in {"exposure_s", "gain"}}
    return {
        "name": "폐루프 촬영 · 생성된 RAW와 제어 이력",
        "revision": "closed-loop-1",
        "validation_kind": "implementation_verification",
        "metric_id": "raw.channel_mean_dn_sequence",
        "unit": "DN",
        "reference_plane": "sensor_raw",
        "included_effects": TEMPORAL_EFFECTS,
        "conditions": static,
        "required_conditions": list(static),
        "absolute_tolerance": {"value": 1.0, "unit": "DN"},
        "uncertainty_policy": "bounded_interval",
        "calibration_dataset_ids": [],
        "clock": record["payload"]["clock"],
        "frame_conditions": None,
        "stimulus": {
            "schema": "closed_loop_scene_test_v1",
            "acquisition_mode": "closed_loop",
            "scene_sequence_id": scene["id"],
            "scene_sequence_hash": scene["content_hash"],
            "control_program_id": record["id"],
            "control_program_hash": record["content_hash"],
            "control_policy_id": policy["id"],
            "control_policy_hash": policy["content_hash"],
            "expanded_program_sha256": record["expanded_program_sha256"],
            "planned_frames": record["payload"]["frames"],
            "applied_conditions": "generated_by_runtime; not known before acquisition",
        },
    }


def prepare_run(service, data):
    environment = _current(service)
    if (
        not data["control_program_id"]
        or data["scene_id"] is not None
        or data["control_sequence_id"] is not None
    ):
        raise ValueError("Closed-loop run requires a control_program_id, never recorded controls")
    if set(data["parameters"]) - {"seed"}:
        raise ValueError(
            "The program fixes all controls and frames; only the noise seed is configurable"
        )
    record = service.registry.get("control_programs", data["control_program_id"])
    if record["assembly_id"] != data["assembly_id"] or data["scene_sequence_id"] not in {
        None,
        record["scene_sequence_id"],
    }:
        raise ValueError("Control program belongs to a different camera or scene sequence")
    test = service.registry.get("tests", data["test_id"])
    response_test = (test.get("observation_method") or {}).get(
        "id"
    ) == "control_response_observation_v1"
    if response_test:
        from .control_response_service import validate_registered_test

        validate_registered_test(service, test)
    expected = test_template(service, record["id"])
    for key in (
        "metric_id",
        "unit",
        "reference_plane",
        "conditions",
        "required_conditions",
        "clock",
        "frame_conditions",
        "stimulus",
    ):
        if response_test and key in {"metric_id", "unit"}:
            continue
        if test.get(key) != expected[key]:
            raise ValueError(
                "Closed-loop Test must pin the program, policy, scene, clock and planned frames"
            )
    if set(test["included_effects"]) != set(TEMPORAL_EFFECTS) or (
        test.get("observation_method") and not response_test
    ):
        raise ValueError("Closed-loop Test must retain the implemented RAW observation effects")
    payload = {k: v for k, v in record.items() if k in ControlProgramCreate.model_fields}
    _, request, snapshots, images = _prepare_program(service, payload)
    if snapshots["expanded_program_sha256"] != record["expanded_program_sha256"]:
        raise ValueError("Expanded control program changed from its registered source")
    request["seed"] = data["parameters"].get("seed", 42)
    # Revalidate seed without storing expanded spectral arrays in the queue.
    seed = request["seed"]
    if type(seed) is not int or not 0 <= seed <= 2**31 - 1:
        raise ValueError("Noise seed must be an integer in [0, 2**31-1]")
    sources = [
        record,
        snapshots["control_policy_snapshot"],
        snapshots["scene_sequence_snapshot"],
        *images,
    ]
    artifacts = {(a["sha256"], a["name"]): a for r in sources for a in r["artifacts"]}
    return {
        "name": record["name"],
        "job_kind": "camera_simulation",
        "plugin_id": "closed_loop_scene_v1",
        "assembly_id": data["assembly_id"],
        "test_id": test["id"],
        "scene_id": None,
        "scene_sequence_id": record["scene_sequence_id"],
        "control_sequence_id": None,
        "control_program_id": record["id"],
        "control_policy_id": record["control_policy_id"],
        "parameters": data["parameters"],
        "request": request,
        "environment": environment,
        **snapshots,
        "control_program_snapshot": record,
        "test_snapshot": test,
        "input_artifacts": list(artifacts.values()),
    }, data["force"]


def execute_run(service, job):
    from .imaging import store_frame_previews, store_output

    if job["environment"] != _current(service):
        raise ValueError("Worker differs from frozen closed-loop environment")
    request = deepcopy(job["request"])
    check_control_domain(
        job["assembly_snapshot"], job["model_snapshots"], request["control_program"]
    )
    scene = job["scene_sequence_snapshot"]
    request["scene_sequence"] = {
        **scene["payload"],
        "knots": [
            {
                "time_ns": k["time_ns"],
                "scene": _scene_input(
                    service,
                    image,
                    lens_model_id=next(
                        m["id"] for m in job["model_snapshots"] if m["kind"] == "lens"
                    ),
                ),
            }
            for k, image in zip(scene["payload"]["knots"], scene["image_snapshots"], strict=True)
        ],
    }
    output = ClosedLoopSceneRuntime().execute(request)
    summary = output["summary"]
    frames = summary["control_trace"]["frames"]
    summary.update(
        frame_count=len(frames),
        frames=deepcopy(frames),
        conditions=job["test_snapshot"]["conditions"],
        clock=request["control_program"]["clock"],
        scene_sequence_id=job["scene_sequence_id"],
        control_program_id=job["control_program_id"],
        control_policy_id=job["control_policy_id"],
    )
    summary["frame_conditions"] = [
        {
            "frame_id": f["frame_id"],
            "frame_start_ns": f["frame_start_ns"],
            "applied_at_ns": f["latch_at_ns"],
            **f["applied"],
        }
        for f in frames
    ]
    summary["prediction"].update(
        id=job["id"],
        conditions=summary["conditions"],
        clock=summary["clock"],
        frame_conditions=summary["frame_conditions"],
        control_program_id=job["control_program_id"],
        acquisition_mode="closed_loop",
        comparison_support="recorded_settings_or_trajectory_operator_required",
        valid_domain=service._domain_intersection(job["model_snapshots"]),
        calibration_dataset_ids=sorted(
            {i for m in job["model_snapshots"] for i in m["calibration_dataset_ids"]}
        ),
        dataset_ids=[job["id"]],
        assembly_id=job["assembly_id"],
        test_id=job["test_id"],
    )
    from .uncertainty import attach, unsupported_budget

    if (job["test_snapshot"].get("observation_method") or {}).get(
        "id"
    ) == "control_response_observation_v1":
        from .control_response_service import observe_run

        observe_run(job, output)
    budget = unsupported_budget(job["model_snapshots"], job["test_snapshot"], job["plugin_id"])
    if budget:
        attach(summary, budget)
    previews = store_frame_previews(service, output)
    return store_output(
        service,
        output,
        source_kind="simulated",
        geometry=summary["geometry"],
        input_artifacts=[*job["input_artifacts"], *previews],
    )
