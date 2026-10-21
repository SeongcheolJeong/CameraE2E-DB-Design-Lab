"""Planning and realized evidence boundaries for RAW-observed electrical AE."""

from copy import deepcopy

from .registry import digest


def template_program(service, retained_template, control, *, observation_readout_id=None):
    from .electrical_control import default_policy
    from .electrical_control_runtime import (
        PAIRED_PROGRAM_SCHEMA,
        PROGRAM_SCHEMA,
        TRAPPED_PROGRAM_SCHEMA,
        observation_path,
        validate_capture_program,
    )
    from .retained_charge_runtime import _raw_formats

    result = deepcopy(retained_template)
    trapped = result["payload"]["schema"] == "camera_retained_charge_capture_program_v2"
    scene = service.registry.get("scene_sequences", control["scene_sequence_id"])
    shape = scene["image_snapshots"][0]["shape"][:2]
    initial = control["payload"]["frames"][0]["applied"]["exposure_ns"]
    raw_format = _raw_formats(result["payload"]["electrical_model"])[0]
    if observation_readout_id is not None:
        result["payload"].update(
            schema=TRAPPED_PROGRAM_SCHEMA if trapped else PAIRED_PROGRAM_SCHEMA,
            observation_readout_id=observation_readout_id,
        )
        _, _, raw_format = observation_path(result["payload"])
    policy = default_policy(
        initial_exposure_ns=initial, shape=shape,
        raw_format=raw_format,
    )
    result.update(
        name="전달 RAW 관측 · 전기 모델 노출 AE",
        revision="electrical-ae-1",
        source_uri="fixture://authored-electrical-raw-exposure-control",
        method_and_version="authored_raw_observed_electrical_ae:1",
    )
    result["payload"].update(
        schema=(TRAPPED_PROGRAM_SCHEMA if trapped else
                PROGRAM_SCHEMA if observation_readout_id is None else PAIRED_PROGRAM_SCHEMA),
        electrical_control_policy=policy,
    )
    if observation_readout_id is not None:
        nonlinear = result["payload"]["electrical_model"]["schema"].endswith("_v2")
        result.update(
            name=(f"공유 전하 · {'비선형 ' if nonlinear else ''}"
                  f"{observation_readout_id} 고정 관측 노출 AE"),
            revision="electrical-paired-ae-2",
            source_uri="fixture://authored-electrical-paired-raw-exposure-control",
            method_and_version="authored_fixed_path_raw_observed_electrical_ae:2",
        )
    if trapped:
        result.update(
            name=f"포획·방출 전하 · {observation_readout_id} RAW 관측 노출 AE",
            revision="electrical-trapped-ae-3",
            source_uri="fixture://authored-trapped-raw-exposure-control",
            method_and_version="authored_trap_state_raw_observed_electrical_ae:3",
        )
    result["payload"] = validate_capture_program(result["payload"])
    return result


def update_test(result, program, control, scene):
    from .electrical_control_runtime import METRIC_ID, OBSERVATION
    from .retained_charge_runtime import _raw_formats

    payload = program["payload"]
    result.update(
        name="전달 RAW 피드백 · 노출과 전하 이력",
        revision="electrical-ae-1",
        metric_id=METRIC_ID,
        frame_conditions=None,
    )
    result["stimulus"].update(
        schema="electrical_control_test_v1",
        observation_method=OBSERVATION,
        dark_correction="not_applied_to_delivered_code_metric",
        sample_contract={
            "schema": "camera_electrical_control_plan_v1",
            "capture_program_id": program["id"],
            "capture_program_hash": program["content_hash"],
            "policy_sha256": digest(payload["electrical_control_policy"]),
            "control_sequence_hash": control["content_hash"],
            "schedule_role": "frame_starts_mode_and_initial_settings_only",
            "applied_exposures": "generated_causally_from_completed_integer_raw",
            "raw_formats": _raw_formats(payload["electrical_model"]),
            "sampling": scene["image_snapshots"][0]["sampling"],
            "raster_shape": scene["image_snapshots"][0]["shape"][:2],
            "observation": OBSERVATION,
        },
    )
    if "observation_readout_id" in payload:
        from .electrical_control_runtime import observation_path

        index, readout_id, _ = observation_path(payload)
        result["stimulus"]["sample_contract"].update(
            observation_readout_id=readout_id, readout_index=index,
        )


def check_exposure_domains(assembly, models, conditions, program, domain):
    """Check both extrema of the bounded grid against every model interval."""
    from .composer import check_assembly
    from .service import PreflightError

    policy = program["electrical_control_policy"]
    from .electrical_control import effective_exposure_grid

    grid = effective_exposure_grid(policy)
    for model in models:
        if "exposure_s" not in model["valid_domain"]:
            raise PreflightError({"status": "FAIL", "issues": [{
                "code": "control_domain_missing", "severity": "unknown",
                "path": f"models.{model['id']}.valid_domain.exposure_s",
                "message": "Declare model applicability for the complete AE exposure range",
            }]})
    exposures = {grid["minimum"] / 1e9, grid["maximum"] / 1e9} if policy["enabled"] else {
        conditions["exposure_s"]["value"]
    }
    for exposure_s in exposures:
        current = {**conditions, "exposure_s": {"value": exposure_s, "unit": "s"}}
        report = check_assembly({**assembly, "conditions": current}, models)
        if report["status"] != "PASS":
            raise PreflightError(report)
        bound = domain.get("exposure_s")
        if bound and (bound["unit"] != "s" or not bound["min"] <= exposure_s <= bound["max"]):
            raise ValueError("AE exposure grid leaves the fixed model applicability domain")


def attach_realized_contract(summary, job):
    """Never present the planning scaffold as acquired exposure or row events."""
    plan = deepcopy(job["test_snapshot"]["stimulus"]["sample_contract"])
    realized = {
        "schema": "camera_electrical_control_realized_samples_v1",
        "plan": plan,
        "control_sequence": deepcopy(summary["control_sequence"]),
        "events": deepcopy(summary["retained_charge"]["events"]),
        "observation": summary["prediction"]["method_and_version"],
    }
    summary["acquisition_mode"] = "closed_loop"
    summary["planning_contract"] = plan
    summary["prediction"].update(
        acquisition_mode="closed_loop", sample_geometry=realized,
    )
