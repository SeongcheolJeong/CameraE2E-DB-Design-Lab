"""Independent ambient + PWM LED charge arithmetic and frozen retained RAW binding."""

from copy import deepcopy

import numpy as np
import pytest
from httpx import HTTPStatusError
from test_charge_collection_runtime import _request as collection_request
from test_hdr_workflow import load_array
from test_native_temporal import expected_rate
from test_native_temporal import native_request as native_request
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow

from camera_platform import (
    capture_programs,
    retained_charge_runtime,
    retained_isp_source,
    sequences,
)
from camera_platform.registry import IntegrityError, canonical
from camera_platform.retained_charge_runtime import (
    RetainedChargeRuntime,
    default_capture_program,
    default_gain_merge_capture_program,
    default_photo_hdr_capture_program,
)


def _waveform(clock_id, phase_origin_ns=0):
    return {
        "schema": "camera_light_waveform_v1", "kind": "pwm", "clock_id": clock_id,
        "period_ns": 1_000_000, "on_time_ns": 500_000,
        "phase_origin_ns": phase_origin_ns, "low": 0, "high": 1,
    }


def _direct_request(data, paired=False):
    data = deepcopy(data)
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(full_sensor_shape=[8, 10], crop_origin=[0, 0])
    data["unmodulated_scene_sequence"] = deepcopy(data["scene_sequence"])
    for knot in data["unmodulated_scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] *= 2
    frames = data["control_sequence"]["frames"]
    for frame, exposure in zip(frames, (200_000, 400_000), strict=True):
        frame["applied"]["exposure_ns"] = exposure
    ids = [frame["frame_id"] for frame in frames]
    program = (default_gain_merge_capture_program(data["readout"], ids)
               if paired else default_capture_program(data["readout"], ids))
    program["waveform"] = _waveform(data["control_sequence"]["clock"]["clock_id"])
    program["retained_charge_model"]["transfer_efficiency"] = [0.5] * 4
    data["capture_program"] = program
    return data


def _on_seconds(start, end, phase):
    return sum(
        max(0, min(end, phase + cycle * 1_000_000 + 500_000)
            - max(start, phase + cycle * 1_000_000))
        for cycle in range(-2, 32)
    ) * 1e-9


@pytest.mark.parametrize("paired", [False, True])
def test_ambient_phase_invariant_and_one_retained_state_transition(native_request, paired):
    data = _direct_request(native_request, paired=paired)
    first = RetainedChargeRuntime().execute(data)
    data["capture_program"]["waveform"]["phase_origin_ns"] = 500_000
    shifted = RetainedChargeRuntime().execute(data)
    a, b = first["arrays"], shifted["arrays"]
    np.testing.assert_array_equal(a["unmodulated_channel_electrons"],
                                  b["unmodulated_channel_electrons"])
    assert not np.array_equal(a["modulated_channel_electrons"],
                              b["modulated_channel_electrons"])
    rate = expected_rate(data)
    sites = a["cfa_channel_index"]
    expected_photo = []
    for frame_index, frame in enumerate(data["control_sequence"]["frames"]):
        rows = []
        for row, start in enumerate(a["row_start_ns"][frame_index]):
            end = start + frame["applied"]["exposure_ns"]
            led = _on_seconds(start, end, 0) * rate[sites[row]]
            ambient = 2 * (end - start) * 1e-9 * rate[sites[row]]
            rows.append(led + ambient)
        expected_photo.append(np.stack(rows))
    expected_photo = np.stack(expected_photo)
    np.testing.assert_allclose(a["mean_electrons"], expected_photo, rtol=4e-13)
    np.testing.assert_allclose(a["channel_electrons"],
                               a["modulated_channel_electrons"]
                               + a["unmodulated_channel_electrons"], rtol=0, atol=0)
    residual = np.zeros((8, 10))
    for index, photo in enumerate(expected_photo):
        delivered = .5 * (residual + photo)
        residual = .5 * (residual + photo)
        np.testing.assert_allclose(a["retained_delivered_e"][index], delivered,
                                   rtol=4e-13, atol=1e-10)
        np.testing.assert_allclose(a["retained_residual_e"][index], residual,
                                   rtol=4e-13, atol=1e-10)
    assert first["summary"]["waveform"]["scope"] == "modulated_component_only"
    assert first["summary"]["hdr_merge_applied"] is False
    assert set(a) == set(first["summary"]["array_units"])
    assert a["raw_groups"].shape[1] == (2 if paired else 1)


@pytest.mark.parametrize("problem", ["clock", "geometry", "waveform", "v5"])
def test_dual_retained_rejects_unsupported_or_mismatched_inputs(native_request, problem):
    data = _direct_request(native_request)
    if problem == "clock":
        data["unmodulated_scene_sequence"]["clock"]["clock_id"] = "other-clock"
    elif problem == "geometry":
        for knot in data["unmodulated_scene_sequence"]["knots"]:
            knot["scene"]["sampling"]["full_sensor_shape"] = [10, 10]
    elif problem == "waveform":
        data["capture_program"]["waveform"] = None
    else:
        data["capture_program"] = default_photo_hdr_capture_program(
            data["readout"], [f["frame_id"] for f in data["control_sequence"]["frames"]])
        data["capture_program"]["waveform"] = _waveform(
            data["control_sequence"]["clock"]["clock_id"]
        )
    assert RetainedChargeRuntime().validate_request(data)


def test_second_collection_ledgers_count_against_bounded_output(native_request, monkeypatch):
    data = _direct_request(collection_request(native_request, mode="temporal"))
    runtime = RetainedChargeRuntime()
    dual_budget = runtime.estimate_resources(data)["output_array_bytes_estimate"]
    output = runtime.execute(data)["arrays"]
    extra = [array for name, array in output.items()
             if name.startswith("unmodulated_") or name == "modulated_channel_electrons"]
    assert any(name.startswith("unmodulated_collection_") for name in output)
    assert dual_budget >= sum(array.nbytes for array in output.values())
    single = deepcopy(data)
    single.pop("unmodulated_scene_sequence")
    single_budget = runtime.estimate_resources(single)["output_array_bytes_estimate"]
    assert dual_budget - single_budget >= sum(array.nbytes for array in extra)
    with monkeypatch.context() as patch:
        patch.setattr(retained_charge_runtime, "MAX_EVIDENCE_BYTES",
                      (dual_budget + single_budget) // 2)
        assert runtime.estimate_resources(single)
        assert any("evidence" in issue["message"].lower()
                   for issue in runtime.validate_request(data))


@pytest.mark.parametrize("mode,readout_id,paths", [
    ("retained_charge", "single", 1),
    ("retained_gain_merge", "HCG", 2),
])
def test_registered_dual_source_sdk_worker_and_isp_provenance(
    workflow, monkeypatch, mode, readout_id, paths,
):
    service, sdk, assembly, scene, control, parent, legacy_test, request = workflow
    ambient = sequences.create_fixture(
        service, sequences.fixture_template(service, assembly["id"])
        | {"name": "Retained ambient", "revision": "ambient-1",
           "shape": [16, 16], "knot_count": 2, "pattern": "static",
           "irradiance_scale": .25},
    )
    draft = capture_programs.template(
        service, assembly["id"], mode=mode, control_sequence_id=control["id"])
    draft.update(name="Retained PWM LED and ambient", revision="components-1",
                 parents=[parent["id"]])
    draft["payload"]["waveform"] = _waveform(control["payload"]["clock"]["clock_id"])
    program = capture_programs.create(service, draft)
    spec = sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
        unmodulated_scene_sequence_id=ambient["id"],
    )
    assert spec["stimulus"]["schema"] == "retained_charge_components_test_v1"
    test = service.create_test(spec)
    with pytest.raises(ValueError, match="exact sequence"):
        service.submit_run({**request, "test_id": legacy_test["id"],
                            "capture_program_id": program["id"],
                            "parameters": {**request["parameters"],
                                           "unmodulated_scene_sequence_id": ambient["id"]}})
    queued = sdk.submit_retained_charge(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=591, noise_enabled=False, unmodulated_scene_sequence_id=ambient["id"],
    )
    assert queued["unmodulated_scene_sequence_snapshot"] == ambient
    assert {a["sha256"] for a in ambient["artifacts"]} <= {
        a["sha256"] for a in queued["input_artifacts"]}
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    altered = deepcopy(job)
    altered["request"]["unmodulated_scene_sequence"]["knots"][0]["time_ns"] += 1
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(altered)
    altered = deepcopy(job)
    altered["unmodulated_scene_sequence_snapshot"]["content_hash"] = "0" * 64
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(altered)
    run = service.registry.finish(job["id"], result=service.execute_run(job))
    summary = run["result"]["summary"]
    assert summary["unmodulated_scene_sequence_id"] == ambient["id"]
    assert summary["prediction"]["included_effects"] == test["included_effects"]
    assert load_array(service, run, "raw_groups").shape == (4, paths, 16, 16)
    assert load_array(service, run, "unmodulated_channel_electrons").shape == (4, 16, 16, 3)
    retained_isp_source.resolve(service, run["id"], {
        "schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": readout_id})
    forged = deepcopy(run)
    forged["unmodulated_scene_sequence_snapshot"]["content_hash"] = "0" * 64
    original = service.registry.get_run
    with monkeypatch.context() as patch:
        patch.setattr(service.registry, "get_run", lambda ident: forged if ident == run["id"]
                      else original(ident))
        with pytest.raises(IntegrityError, match="Second retained illumination"):
            retained_isp_source.resolve(service, run["id"], {
                "schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": readout_id})
    forged = deepcopy(run)
    forged["result"]["summary"]["illumination_components"]["summation"] = "unbound"
    original_artifact_bytes = retained_isp_source.artifact_bytes
    with monkeypatch.context() as patch:
        patch.setattr(service.registry, "get_run", lambda ident: forged if ident == run["id"]
                      else original(ident))
        patch.setattr(retained_isp_source, "artifact_bytes", lambda svc, artifact: (
            canonical(forged["result"]["summary"]).encode()
            if artifact["name"] == "summary.json"
            else original_artifact_bytes(svc, artifact)
        ))
        with pytest.raises(IntegrityError, match="illumination summary"):
            retained_isp_source.resolve(service, run["id"], {
                "schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": readout_id})
    for field, forged_value in (
        ("specification", None),
        ("on_time_correction", True),
        ("scope", "uniform_modulation"),
        ("independent_ambient_component", False),
        ("effective_seconds_scope", "combined_light"),
        ("photo_hdr_row_integral_normalization", True),
    ):
        forged = deepcopy(run)
        forged["result"]["summary"]["waveform"][field] = forged_value
        with monkeypatch.context() as patch:
            patch.setattr(service.registry, "get_run", lambda ident, forged=forged: (
                forged if ident == run["id"] else original(ident)))
            patch.setattr(retained_isp_source, "artifact_bytes", lambda svc, artifact,
                          forged=forged: (
                canonical(forged["result"]["summary"]).encode()
                if artifact["name"] == "summary.json"
                else original_artifact_bytes(svc, artifact)
            ))
            with pytest.raises(IntegrityError, match="illumination summary"):
                retained_isp_source.resolve(service, run["id"], {
                    "schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": readout_id})


@pytest.mark.parametrize("mode", [
    "retained_stateful_hdr",
])
def test_registered_retained_dual_rejects_other_program_variants(workflow, mode):
    service, sdk, assembly, scene, control, parent, test, request = workflow
    draft = capture_programs.template(
        service, assembly["id"], mode=mode, control_sequence_id=control["id"])
    draft.update(name=f"Unsupported dual {mode}", revision="gate-1",
                 parents=[parent["id"]])
    draft["payload"]["waveform"] = _waveform(control["payload"]["clock"]["clock_id"])
    program = capture_programs.create(service, draft)
    with pytest.raises(ValueError, match="Two illumination components require"):
        sequences.test_template(
            service, assembly["id"], scene["id"], control["id"], program["id"],
            unmodulated_scene_sequence_id=scene["id"],
        )
    with pytest.raises(HTTPStatusError) as error:
        sdk.template(
            "temporal-test", assembly_id=assembly["id"],
            scene_sequence_id=scene["id"], control_sequence_id=control["id"],
            capture_program_id=program["id"],
            unmodulated_scene_sequence_id=scene["id"],
        )
    assert error.value.response.status_code == 422
    with pytest.raises(ValueError, match="Retained two-component acquisition supports"):
        service.submit_run({
            **request, "test_id": test["id"], "capture_program_id": program["id"],
            "parameters": {**request["parameters"],
                           "unmodulated_scene_sequence_id": scene["id"]},
        })


def test_legacy_sdk_omission_preserves_single_component_identity(workflow):
    service, sdk, assembly, scene, control, program, test, request = workflow
    direct = service.submit_run(request)
    sdk_run = sdk.submit_retained_charge(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=591, noise_enabled=False, unmodulated_scene_sequence_id=None,
    )
    assert sdk_run["id"] == direct["id"]
    assert "unmodulated_scene_sequence" not in sdk_run["request"]
    assert "unmodulated_scene_sequence_id" not in sdk_run["parameters"]
    assert test["stimulus"]["schema"] == "retained_charge_test_v1"
