"""Independent LED/ambient trap chronology and frozen registered RAW evidence."""

import hashlib
import math
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import load_array
from test_native_temporal import expected_rate
from test_native_temporal import native_request as native_request
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow
from test_retained_two_component import _direct_request, _on_seconds, _waveform

from camera_platform import capture_programs, retained_isp_source, sequences
from camera_platform.evidence import build_bundle
from camera_platform.registry import IntegrityError, canonical
from camera_platform.retained_charge_runtime import (
    RetainedChargeRuntime,
    default_gain_merge_capture_program,
    default_trapped_capture_program,
)
from camera_platform.service import PlatformService


def _trapped_request(data, *, paired=False):
    data = _direct_request(data)
    frame_ids = [frame["frame_id"] for frame in data["control_sequence"]["frames"]]
    program = (
        default_gain_merge_capture_program(data["readout"], frame_ids, trapped=True)
        if paired else default_trapped_capture_program(data["readout"], frame_ids)
    )
    program["waveform"] = _waveform(data["control_sequence"]["clock"]["clock_id"])
    program["retained_charge_model"].update(
        transfer_efficiency=[0.5] * 4,
        trap_capacity_e=[100] * 4,
        capture_rate_s=[5.0] * 4,
        release_time_s=[0.01] * 4,
    )
    data["capture_program"] = program
    return data


def _scalar_trap_trace(arrays, data, row, col, phase_ns):
    """Reference the authored split law without product optical or trap helpers."""
    site = int(arrays["cfa_channel_index"][row, col])
    rate = float(expected_rate(data)[site])
    model = data["capture_program"]["retained_charge_model"]
    capacity = float(model["capacity_e"])
    trap_capacity = float(model["trap_capacity_e"][site])
    capture_rate = float(model["capture_rate_s"][site])
    lifetime = float(model["release_time_s"][site])
    efficiency = float(model["transfer_efficiency"][site])
    free = trapped = 0.0
    delivered_trace, trapped_trace = [], []
    starts, ends = arrays["trap_segment_start_ns"], arrays["trap_segment_end_ns"]
    phases, counts = arrays["trap_segment_phase"], arrays["trap_segment_count"]
    for frame in range(len(counts)):
        for index in range(int(counts[frame])):
            start, end = int(starts[frame, index, row]), int(ends[frame, index, row])
            duration = (end - start) * 1e-9
            if phases[frame, index] == 1:
                photo = rate * (_on_seconds(start, end, phase_ns) + 2 * duration)
            else:
                photo = 0.0
            free = min(capacity, free + photo)
            released = trapped * -math.expm1(-duration / lifetime)
            trapped -= released
            free = min(capacity, free + released)
            vacancy = trap_capacity - trapped
            captured = min(
                vacancy,
                free * -math.expm1(-capture_rate * vacancy / trap_capacity * duration),
            )
            free -= captured
            trapped += captured
        delivered = free * efficiency
        free -= delivered
        delivered_trace.append(delivered)
        trapped_trace.append(trapped)
    return delivered_trace, trapped_trace


@pytest.mark.parametrize("paired", [False, True])
def test_two_component_traps_share_one_ordered_population(native_request, paired):
    data = _trapped_request(native_request, paired=paired)
    original = RetainedChargeRuntime().execute(data)
    shifted = deepcopy(data)
    shifted["capture_program"]["waveform"]["phase_origin_ns"] = 500_000
    phase_shifted = RetainedChargeRuntime().execute(shifted)
    assert original["summary"]["schema_version"] == (
        "camera_retained_charge_result_v3" if paired else "camera_retained_charge_result_v2"
    )
    assert original["summary"]["hdr_merge_applied"] is False
    assert original["summary"]["current_exposure_charge_estimation"] is False
    assert original["summary"]["physical_validation"] == "not_established"
    assert original["summary"]["retained_charge"]["trap_integration"]["exact_ctmc"] is False
    a, b = original["arrays"], phase_shifted["arrays"]
    np.testing.assert_array_equal(a["unmodulated_channel_electrons"],
                                  b["unmodulated_channel_electrons"])
    assert not np.array_equal(a["modulated_channel_electrons"],
                              b["modulated_channel_electrons"])
    np.testing.assert_allclose(a["channel_electrons"],
                               a["modulated_channel_electrons"]
                               + a["unmodulated_channel_electrons"], rtol=0, atol=0)
    np.testing.assert_allclose(a["retained_expected_exposure_e"], a["mean_electrons"],
                               rtol=1e-12, atol=1e-10)
    for output, phase in ((original, 0), (phase_shifted, 500_000)):
        arrays = output["arrays"]
        for row, col in ((0, 0), (0, 1), (1, 0), (1, 1)):
            delivered, trapped = _scalar_trap_trace(arrays, data, row, col, phase)
            np.testing.assert_allclose(arrays["retained_delivered_e"][:, row, col],
                                       delivered, rtol=2e-12, atol=2e-10)
            np.testing.assert_allclose(arrays["retained_trapped_e"][:, row, col],
                                       trapped, rtol=2e-12, atol=2e-10)
        assert arrays["raw_groups"].shape[1] == (2 if paired else 1)
        assert set(arrays) == set(output["summary"]["array_units"])
        estimate = RetainedChargeRuntime().estimate_resources(
            data if phase == 0 else shifted
        )
        assert estimate["output_array_bytes_estimate"] >= sum(
            value.nbytes for value in arrays.values()
        )
    assert not np.array_equal(a["retained_trapped_e"], b["retained_trapped_e"])


def test_ambient_knot_splits_trap_chronology_independently_of_pwm(native_request):
    data = _trapped_request(native_request)
    ambient = data["unmodulated_scene_sequence"]
    middle = deepcopy(ambient["knots"][0])
    middle["time_ns"] = 75_031
    middle["scene"]["spectral_irradiance"] *= 3
    ambient["knots"].insert(1, middle)
    arrays = RetainedChargeRuntime().execute(data)["arrays"]
    starts, ends = arrays["trap_segment_start_ns"], arrays["trap_segment_end_ns"]
    phases, counts = arrays["trap_segment_phase"], arrays["trap_segment_count"]
    intervals = [
        (int(starts[0, index, 0]), int(ends[0, index, 0]))
        for index in range(int(counts[0])) if phases[0, index] == 1
    ]
    assert any(end == 75_031 for _, end in intervals)
    assert all(not start < 75_031 < end for start, end in intervals)
    site = int(arrays["cfa_channel_index"][0, 0])
    rate = expected_rate(data)[site]
    last = 2 + (6 - 2) * (20_000_000 - 200_000) / (20_000_000 - 75_031)
    expected = rate * (
        (2 + 6) / 2 * 75_031 + (6 + last) / 2 * (200_000 - 75_031)
    ) * 1e-9
    np.testing.assert_allclose(arrays["unmodulated_channel_electrons"][0, 0, 0, site],
                               expected, rtol=2e-13)


def test_combined_trap_output_is_not_sum_of_two_separate_histories(native_request):
    data = _trapped_request(native_request)
    runtime = RetainedChargeRuntime()
    combined = runtime.execute(data)["arrays"]
    led_only = deepcopy(data)
    for knot in led_only["unmodulated_scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] *= 0
    ambient_only = deepcopy(data)
    for knot in ambient_only["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] *= 0
    led = runtime.execute(led_only)["arrays"]
    ambient = runtime.execute(ambient_only)["arrays"]
    np.testing.assert_allclose(
        combined["mean_electrons"], led["mean_electrons"] + ambient["mean_electrons"],
        rtol=3e-15, atol=1e-12,
    )
    assert np.max(np.abs(
        combined["retained_delivered_e"] - led["retained_delivered_e"]
        - ambient["retained_delivered_e"]
    )) > 0.01


def test_sampled_dual_trap_exactly_conserves_two_reservoirs(native_request):
    data = _trapped_request(native_request)
    data["noise_enabled"] = True
    arrays = RetainedChargeRuntime().execute(data)["arrays"]
    for frame in range(arrays["retained_delivered_e"].shape[0]):
        incoming = sum(arrays["retained_" + name][frame] for name in (
            "initial_residual_e", "initial_trapped_e", "gap_generated_e",
            "exposure_generated_e",
        ))
        outgoing = sum(arrays["retained_" + name][frame] for name in (
            "gap_overflow_e", "exposure_overflow_e", "clear_loss_e",
            "delivered_e", "residual_e", "trapped_e",
        ))
        assert incoming.dtype.kind in "iu"
        np.testing.assert_array_equal(incoming, outgoing)


@pytest.mark.parametrize("mode,readout_id,path_count", [
    ("trapped_charge", "single", 1),
    ("trapped_gain_merge", "HCG", 2),
])
def test_registered_trapped_dual_worker_isp_and_tamper(
    workflow, monkeypatch, mode, readout_id, path_count,
):
    service, sdk, assembly, scene, control, parent, legacy_test, request = workflow
    ambient = sequences.create_fixture(
        service, sequences.fixture_template(service, assembly["id"])
        | {"name": "Trapped ambient", "revision": "ambient-trap-1",
           "shape": [16, 16], "knot_count": 2, "pattern": "static",
           "irradiance_scale": .25},
    )
    draft = capture_programs.template(
        service, assembly["id"], mode=mode, control_sequence_id=control["id"],
    )
    draft.update(name="Trapped LED and ambient", revision="dual-trap-1",
                 parents=[parent["id"]])
    draft["payload"]["waveform"] = _waveform(control["payload"]["clock"]["clock_id"])
    program = capture_programs.create(service, draft)
    spec = sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
        unmodulated_scene_sequence_id=ambient["id"],
    )
    assert spec["stimulus"]["schema"] == "retained_charge_components_test_v1"
    assert "combined_photo_generation_before_single_trap_state" in spec["included_effects"]
    test = service.create_test(spec)
    with pytest.raises(ValueError, match="exact sequence"):
        service.submit_run({
            **request, "test_id": legacy_test["id"], "capture_program_id": program["id"],
            "parameters": {**request["parameters"],
                           "unmodulated_scene_sequence_id": ambient["id"]},
        })
    queued = sdk.submit_retained_charge(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=591, noise_enabled=False, unmodulated_scene_sequence_id=ambient["id"],
    )
    assert queued["unmodulated_scene_sequence_snapshot"] == ambient
    assert {item["sha256"] for item in ambient["artifacts"]} <= {
        item["sha256"] for item in queued["input_artifacts"]
    }
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    altered = deepcopy(job)
    altered["request"]["unmodulated_scene_sequence"]["knots"][0]["time_ns"] += 1
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(altered)
    run = service.registry.finish(job["id"], result=service.execute_run(job))
    summary = run["result"]["summary"]
    assert summary["prediction"]["included_effects"] == test["included_effects"]
    assert summary["physical_validation"] == "not_established"
    assert summary["hdr_merge_applied"] is False
    assert summary["unmodulated_scene_sequence_id"] == ambient["id"]
    assert load_array(service, run, "raw_groups").shape == (4, path_count, 16, 16)
    assert np.any(load_array(service, run, "retained_trapped_e") > 0)
    cold = PlatformService(service.registry.directory)
    selector = {"schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": readout_id}
    assert retained_isp_source.resolve(cold, run["id"], selector)["source_id"] == run["id"]
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        raw_artifact = next(item for item in run["artifacts"]
                            if item["name"] == "raw_groups.npy")
        path = f"artifacts/{raw_artifact['sha256']}/{raw_artifact['name']}"
        assert hashlib.sha256(archive.read(path)).hexdigest() == raw_artifact["sha256"]
    forged = deepcopy(run)
    forged["result"]["summary"]["retained_charge"]["trap_integration"][
        "schedule_sha256"
    ] = "0" * 64
    original_get = service.registry.get_run
    original_bytes = retained_isp_source.artifact_bytes
    with monkeypatch.context() as patch:
        patch.setattr(service.registry, "get_run", lambda ident: (
            forged if ident == run["id"] else original_get(ident)))
        patch.setattr(retained_isp_source, "artifact_bytes", lambda svc, artifact: (
            canonical(forged["result"]["summary"]).encode()
            if artifact["name"] == "summary.json" else original_bytes(svc, artifact)
        ))
        with pytest.raises(IntegrityError, match="trap chronology"):
            retained_isp_source.resolve(service, run["id"], selector)
