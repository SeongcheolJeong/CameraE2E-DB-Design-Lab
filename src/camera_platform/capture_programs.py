"""Immutable electrical, periodic-light and RAW HDR configuration with provenance."""

from .contracts import CaptureProgramCreate
from .registry import canonical, digest


def runtime_module(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("schema"), str):
        raise ValueError("Capture program requires an explicit string schema")
    if payload.get("schema") in {
        "camera_electrical_control_capture_program_v1",
        "camera_electrical_control_capture_program_v2",
        "camera_electrical_control_capture_program_v3",
    }:
        from . import electrical_control_runtime

        return electrical_control_runtime
    if payload.get("schema") in {
        "camera_retained_charge_capture_program_v1", "camera_retained_charge_capture_program_v2",
        "camera_retained_charge_capture_program_v3", "camera_retained_charge_capture_program_v4",
        "camera_retained_charge_capture_program_v5",
    }:
        from . import retained_charge_runtime

        return retained_charge_runtime
    if payload.get("schema") in {
        "camera_reference_hdr_capture_program_v1", "camera_reference_hdr_capture_program_v2"
    }:
        from . import reference_hdr_runtime

        return reference_hdr_runtime
    if payload.get("schema") in {"camera_dcg_capture_program_v1", "camera_dcg_capture_program_v2"}:
        from . import dcg_runtime

        return dcg_runtime
    from . import hdr_runtime

    return hdr_runtime


def runtime_for_payload(payload):
    module = runtime_module(payload)
    module.validate_capture_program(payload)
    return module.PLUGIN_ID


def template(service, assembly_id, mode="independent_bracket", control_sequence_id=None):
    nonlinear_modes = {
        "independent_bracket_nonlinear", "shared_charge_nonlinear", "reference_guarded_nonlinear"
    }
    paired_control_modes = {
        "electrical_control_hcg": ("HCG", False),
        "electrical_control_lcg": ("LCG", False),
        "electrical_control_hcg_nonlinear": ("HCG", True),
        "electrical_control_lcg_nonlinear": ("LCG", True),
    }
    trapped_control_modes = {
        "electrical_control_trapped": ("single", False),
        "electrical_control_trapped_pwm_period": ("single", False),
        "electrical_control_trapped_nonlinear": ("single", True),
        "electrical_control_trapped_hcg": ("HCG", False),
        "electrical_control_trapped_lcg": ("LCG", False),
        "electrical_control_trapped_hcg_nonlinear": ("HCG", True),
        "electrical_control_trapped_lcg_nonlinear": ("LCG", True),
    }
    allowed_modes = {
        "independent_bracket", "independent_bracket_cds", "shared_charge", "reference_guarded",
        "reference_guarded_translation",
        "retained_charge", "trapped_charge", "retained_gain_merge", "trapped_gain_merge",
        "retained_stateful_hdr",
        "electrical_control",
    } | nonlinear_modes | paired_control_modes.keys() | trapped_control_modes.keys()
    if mode not in allowed_modes:
        raise ValueError("Unknown electrical acquisition mode")
    from .hdr_runtime import default_capture_program

    assembly = service.registry.get("assemblies", assembly_id)
    sensor = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    result = {
        "name": "전기 모델 · 순차 HDR 촬영",
        "revision": "1",
        "assembly_id": assembly_id,
        "source_kind": "estimated",
        "source_uri": "fixture://authored-electrical-hdr-program",
        "license": "MIT; authored model assumptions",
        "method_and_version": "independent_reset_raw_bracket:1",
        "physical_instance": None,
        "parents": [],
        "payload": default_capture_program(sensor["payload"]["readout"]),
    }
    if mode == "independent_bracket_cds":
        from .electrical import default_cds_noise_profile
        from .hdr_runtime import CDS_PROGRAM_SCHEMA

        model = result["payload"]["electrical_model"]
        model["read_noise_e"] = 0.0
        model["post_gain_noise_v"] = 0.0
        result.update(
            name="리셋·신호 CDS · 행/열 상관 RAW",
            revision="cds-1",
            source_uri="fixture://authored-cds-correlated-readout-program",
            method_and_version="sampled_cds_native_correlated_readout:1",
            payload={**result["payload"], "schema": CDS_PROGRAM_SCHEMA,
                     "hdr_merge": None, "cds_read_noise": default_cds_noise_profile()},
        )
    if mode == "shared_charge":
        from .dcg_runtime import default_capture_program as dcg_default

        result.update(name="공유 전하 · HCG/LCG 두 읽기",
                      source_uri="fixture://authored-shared-charge-dcg-program",
                      method_and_version="ideal_shared_charge_two_conversion_paths:1",
                      payload=dcg_default(sensor["payload"]["readout"]))
    if mode == "reference_guarded":
        from .reference_hdr_runtime import default_capture_program as reference_default

        result.update(name="기준 노출 보존 합성 · 정상광",
                      source_uri="fixture://authored-reference-constrained-hdr-program",
                      method_and_version="reference_constrained_raw_merge:1",
                      payload=reference_default(sensor["payload"]["readout"]))
    if mode == "reference_guarded_translation":
        from .reference_hdr import default_translation_profile
        from .reference_hdr_runtime import default_capture_program as reference_default

        payload = reference_default(sensor["payload"]["readout"])
        payload["hdr_merge"] = default_translation_profile()
        result.update(
            name="기준 노출 보존 · RAW 전역 평행이동 HDR",
            revision="translation-1",
            source_uri="fixture://authored-reference-raw-global-translation-hdr-program",
            method_and_version="reference_raw_even_pixel_global_translation:1",
            payload=payload,
        )
    if mode in nonlinear_modes:
        from .nonlinear_examples import capture_program

        names = {
            "independent_bracket_nonlinear": "비선형 전기 응답 · 순차 RAW HDR",
            "shared_charge_nonlinear": "비선형 공유 전하 · HCG/LCG 두 읽기",
            "reference_guarded_nonlinear": "비선형 기준 노출 보존 합성 · 정상광",
        }
        result.update(
            name=names[mode], revision="nonlinear-2", source_kind="estimated",
            source_uri="fixture://authored-nonlinear-" + mode,
            license="MIT; explicitly authored nonlinear behavioral assumptions",
            method_and_version="authored_nonlinear_charge_voltage_example:1",
            payload=capture_program(sensor["payload"]["readout"], mode),
        )
    retained_modes = {
        "retained_charge", "trapped_charge", "retained_gain_merge", "trapped_gain_merge",
        "retained_stateful_hdr",
        "electrical_control",
    }
    if mode in retained_modes | paired_control_modes.keys() | trapped_control_modes.keys():
        from .retained_charge_runtime import default_capture_program as retained_default

        if not control_sequence_id:
            raise ValueError("Retained template requires its ordered control sequence")
        control = service.registry.get("control_sequences", control_sequence_id)
        if control["assembly_id"] != assembly_id:
            raise ValueError("Retained template control sequence belongs to another assembly")
        result.update(
            name="잔류 전하 · 행별 부분 전달",
            revision="retained-1",
            source_uri="fixture://authored-retained-charge-program",
            method_and_version="authored_retained_photodiode:1",
            payload=retained_default(
                sensor["payload"]["readout"], [f["frame_id"] for f in control["payload"]["frames"]]
            ),
        )
        if mode == "trapped_charge" or mode in trapped_control_modes:
            from .retained_charge_runtime import default_trapped_capture_program

            result.update(
                name="포획·방출 전하 · 시간 이력 촬영",
                revision="trapped-1",
                source_uri="fixture://authored-trapped-charge-program",
                method_and_version="authored_pd_trap_exchange:1",
                payload=default_trapped_capture_program(
                    sensor["payload"]["readout"],
                    [f["frame_id"] for f in control["payload"]["frames"]],
                ),
            )
        if mode in {"retained_gain_merge", "trapped_gain_merge"}:
            from .retained_charge_runtime import default_gain_merge_capture_program

            result.update(
                name="상태 이력 · HCG/LCG 전달 전하 결합",
                revision="gain-merge-1",
                source_uri="fixture://authored-retained-gain-merge-program",
                method_and_version="authored_stateful_delivered_charge_gain_merge:1",
                payload=default_gain_merge_capture_program(
                    sensor["payload"]["readout"],
                    [f["frame_id"] for f in control["payload"]["frames"]],
                    trapped=mode == "trapped_gain_merge",
                ),
            )
        if mode == "retained_stateful_hdr":
            from .retained_charge_runtime import default_photo_hdr_capture_program

            result.update(
                name="잔류 PD · 인과적 다중 노출 HDR",
                revision="retained-hdr-1",
                source_uri="fixture://authored-retained-photo-hdr-program",
                method_and_version="causal_retained_raw_photo_rate_selection:1",
                payload=default_photo_hdr_capture_program(
                    sensor["payload"]["readout"],
                    [f["frame_id"] for f in control["payload"]["frames"]],
                ),
            )
    if mode == "electrical_control" or mode in (
        paired_control_modes.keys() | trapped_control_modes.keys()
    ):
        from .electrical_control_workflow import template_program

        readout_id = None
        if mode in paired_control_modes:
            from .dcg_electrical import default_model
            from .nonlinear_examples import shared_model

            readout_id, nonlinear = paired_control_modes[mode]
            model = (shared_model if nonlinear else default_model)(sensor["payload"]["readout"])
            result["payload"]["electrical_model"] = model
            result["payload"]["retained_charge_model"]["capacity_e"] = int(
                model["shared_charge_capacity_e"]
            )
        if mode in trapped_control_modes:
            from .dcg_electrical import default_model
            from .nonlinear_examples import shared_model, single_model

            readout_id, nonlinear = trapped_control_modes[mode]
            if readout_id == "single":
                if nonlinear:
                    result["payload"]["electrical_model"] = single_model(
                        sensor["payload"]["readout"]
                    )
            else:
                model = (shared_model if nonlinear else default_model)(
                    sensor["payload"]["readout"]
                )
                result["payload"]["electrical_model"] = model
                result["payload"]["retained_charge_model"]["capacity_e"] = int(
                    model["shared_charge_capacity_e"]
                )
        result = template_program(service, result, control, observation_readout_id=readout_id)
        if mode == "electrical_control_trapped_pwm_period":
            from .electrical_control import PERIOD_POLICY_SCHEMA
            from .electrical_control_runtime import validate_capture_program

            initial = control["payload"]["frames"][0]["applied"]["exposure_ns"]
            if initial < 2:
                raise ValueError(
                    "PWM period template requires an initial exposure of at least 2 ns"
                )
            result.update(
                name="Trap 전하 · PWM 정수 주기 노출 AE",
                revision="electrical-trapped-pwm-ae-4",
                source_uri="fixture://authored-trapped-pwm-period-ae",
                method_and_version="authored_whole_pwm_period_code_feedback:1",
            )
            result["payload"]["waveform"] = {
                "schema": "camera_light_waveform_v1", "kind": "pwm",
                "clock_id": control["payload"]["clock"]["clock_id"],
                "period_ns": initial, "on_time_ns": initial // 2,
                "phase_origin_ns": 0, "low": 0.0, "high": 1.0,
            }
            result["payload"]["electrical_control_policy"].update(
                schema=PERIOD_POLICY_SCHEMA,
                period_lattice={"kind": "whole_pwm_period_v1", "period_ns": initial},
            )
            result["payload"] = validate_capture_program(result["payload"])
    return result


def create(service, payload):
    from .imaging import _format

    data = CaptureProgramCreate.model_validate(payload).model_dump()
    normalized = runtime_module(data["payload"]).validate_capture_program(data["payload"])
    assembly = service.registry.get("assemblies", data["assembly_id"])
    sensor = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    if data["source_kind"] == "measured" and not data["physical_instance"]:
        raise ValueError("Measured model provenance requires a physical sample identity")
    for identity in data["parents"]:
        service.registry.get("capture_programs", identity)
    raw = _format(assembly["raw_format"])
    dcg = normalized["schema"] in {
        "camera_dcg_capture_program_v1",
        "camera_dcg_capture_program_v2",
    } or (
        normalized["schema"] in {
            "camera_retained_charge_capture_program_v1",
            "camera_retained_charge_capture_program_v2",
            "camera_retained_charge_capture_program_v3",
            "camera_retained_charge_capture_program_v4",
            "camera_electrical_control_capture_program_v2",
            "camera_electrical_control_capture_program_v3",
        }
        and normalized["electrical_model"]["schema"].startswith("camera_shared_charge_readout_")
    )
    adcs = ([p["adc"] for p in normalized["electrical_model"]["paths"]] if dcg
            else [normalized["electrical_model"]["adc"]])
    if raw != _format(sensor["raw_format"]) or any(
        adc["bit_depth"] != raw["bit_depth"]
        or (not dcg and adc["black_level_dn"] != raw["black_level_dn"])
        for adc in adcs
    ):
        raise ValueError("Electrical ADC must match the fixed assembly and sensor RAW format")
    data["payload"] = normalized
    artifacts = [
        service.registry.store_blob(
            canonical(payload).encode(), "capture-program-source.json", "application/json"
        ),
        service.registry.store_blob(
            canonical(data).encode(), "capture-program-normalized.json", "application/json"
        ),
    ]
    return service.registry.put("capture_programs", {
        **data,
        "assembly_snapshot": assembly,
        "sensor_model_snapshot": sensor,
        "artifacts": artifacts,
        "approval_status": "draft",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "electrical_model_authority": (
            "Explicit program override; base sensor readout is not applied"
        ),
    })


def verify_program(service, program, plugin_id):
    """Bind a shared-charge acquisition to its registered program and both source blobs."""
    if runtime_for_payload(program["payload"]) != plugin_id:
        raise ValueError("Capture program acquisition topology differs from the selected runtime")
    if service.registry.get("capture_programs", program["id"]) != program:
        raise ValueError("Capture program differs from its frozen registry record")
    if (service.registry.get("assemblies", program["assembly_id"])
            != program["assembly_snapshot"]):
        raise ValueError("Capture program assembly snapshot differs from its registered source")
    sensor = program["sensor_model_snapshot"]
    if service.registry.get("models", sensor["id"]) != sensor:
        raise ValueError("Capture program sensor snapshot differs from its registered source")
    data = CaptureProgramCreate.model_validate({
        k: v for k, v in program.items() if k in CaptureProgramCreate.model_fields
    }).model_dump()
    normalized = runtime_module(data["payload"]).validate_capture_program(data["payload"])
    if normalized != data["payload"]:
        raise ValueError("Capture program is not its canonical normalized model")
    entries = program["artifacts"]
    if len(entries) != 2 or {a["name"] for a in entries} != {
        "capture-program-source.json", "capture-program-normalized.json"
    }:
        raise ValueError("Capture program requires source and normalized evidence")
    import json

    for entry in entries:
        content = service.registry.blob_path(entry["sha256"]).read_bytes()
        value = json.loads(content)
        expected = {"id": entry["sha256"], "sha256": entry["sha256"],
                    "name": entry["name"], "bytes": len(content),
                    "media_type": "application/json",
                    "url": f"/api/v1/artifacts/{entry['sha256']}"}
        if entry != expected or digest(value) != entry["sha256"]:
            raise ValueError("Capture program artifact descriptor differs from canonical evidence")
        if entry["name"] == "capture-program-source.json":
            value = CaptureProgramCreate.model_validate(value).model_dump()
            value["payload"] = runtime_module(value["payload"]).validate_capture_program(
                value["payload"]
            )
        if value != data:
            raise ValueError("Capture program source/normalized evidence differs from its payload")
    return program
