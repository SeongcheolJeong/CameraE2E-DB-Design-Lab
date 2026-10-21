"""Trap-bearing causal acquisition is evidence; ISP consumes original codes only."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_electrical_control_workflow import workflow as _workflow
from test_electrical_isp_paired import replay_request, selector
from test_electrical_isp_source import return_record, rewrite_summary
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as _hdr_workflow
from test_retained_charge_workflow import workflow as _retained_workflow
from test_retained_isp_comparison import specification

from camera_platform import dcg_electrical, nonlinear_examples, retained_isp, trapped_charge
from camera_platform import retained_isp_comparison as comparison
from camera_platform import retained_isp_replay as replay
from camera_platform import retained_isp_source as source
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.retained_charge_runtime import default_trapped_capture_program
from camera_platform.service import PlatformService

hdr_workflow = _hdr_workflow
retained_workflow = _retained_workflow
workflow = _workflow

TIMING_NAMES = {
    "applied_exposure_ns.npy",
    "row_start_ns.npy",
    "row_end_ns.npy",
    "row_gap_ns.npy",
    "trap_segment_start_ns.npy",
    "trap_segment_end_ns.npy",
    "trap_segment_duration_ns.npy",
    "trap_segment_phase.npy",
    "trap_segment_count.npy",
}


def acquisition(workflow, family="single", *, pwm=False):
    service, sdk, assembly, scene, control, parent, _, request = workflow
    legacy = parent["sensor_model_snapshot"]["payload"]["readout"]
    draft = sdk.template(
        "capture-program",
        assembly_id=assembly["id"],
        mode="electrical_control",
        control_sequence_id=control["id"],
    )
    draft.update(name="Trap control ISP " + family, revision="trapped-isp-" + family)
    program = default_trapped_capture_program(
        legacy, [f["frame_id"] for f in control["payload"]["frames"]]
    )
    program.update(
        schema="camera_electrical_control_capture_program_v3",
        electrical_control_policy=deepcopy(parent["payload"]["electrical_control_policy"]),
        observation_readout_id="single",
    )
    factories = {
        "nonlinear": nonlinear_examples.single_model,
        "paired": dcg_electrical.default_model,
        "paired_nonlinear": nonlinear_examples.shared_model,
    }
    if family in factories:
        program["electrical_model"] = factories[family](legacy)
    if family.startswith("paired"):
        program["observation_readout_id"] = "HCG" if family == "paired" else "LCG"
        for index, path in enumerate(program["electrical_model"]["paths"]):
            path["analog_gain"] = 1.25 if index == 0 else 2
            path["digital_gain"] = 1 if index == 0 else 1.5
    program["retained_charge_model"]["transfer_efficiency"] = [0.4, 0.5, 0.6, 0.7]
    if pwm:
        program["waveform"] = {
            "schema": "camera_light_waveform_v1",
            "kind": "pwm",
            "clock_id": control["payload"]["clock"]["clock_id"],
            "period_ns": 731_000,
            "on_time_ns": 173_000,
            "phase_origin_ns": 317_000,
            "low": 0,
            "high": 1,
        }
    draft["payload"] = program
    saved = sdk.create_capture_program(draft)
    test = service.create_test(
        sdk.template(
            "temporal-test",
            assembly_id=assembly["id"],
            scene_sequence_id=scene["id"],
            control_sequence_id=control["id"],
            capture_program_id=saved["id"],
        )
    )
    run = finish(
        service,
        service.submit_run(
            {
                **request,
                "capture_program_id": saved["id"],
                "test_id": test["id"],
            }
        ),
    )
    return service, sdk, run


def verify_bundle(content, records):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for record in records:
            for artifact in record["artifacts"]:
                value = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
                assert len(value) == artifact["bytes"]
                assert hashlib.sha256(value).hexdigest() == artifact["sha256"]
        return json.loads(archive.read("manifest.json"))


@pytest.mark.parametrize("family", ["single", "nonlinear", "paired", "paired_nonlinear"])
def test_real_trap_control_to_replay_and_comparison_preserves_code_boundary(
    workflow,
    family,
    monkeypatch,
):
    service, sdk, original = acquisition(workflow, family, pwm="nonlinear" in family)
    before = deepcopy(original)
    summary = original["result"]["summary"]
    meter = original["request"]["capture_program"]["observation_readout_id"]
    readout = {"single": "single", "HCG": "LCG", "LCG": "HCG"}[meter]
    index = summary["readout_ids"].index(readout)
    raw = load_array(service, original, "raw_groups")
    assert np.any(load_array(service, original, "retained_trapped_e") > 0)
    exposures = [f["applied"]["exposure_ns"] for f in summary["frames"]]
    assert len(set(exposures)) > 1
    assert summary["schema_version"] == "camera_electrical_control_result_v2"
    assert summary["retained_charge"]["events"]["schema"] == "camera_retained_row_events_v2"

    def no_physics(*args, **kwargs):
        pytest.fail("Selecting/replaying RAW must not rerun trap dynamics")

    monkeypatch.setattr(trapped_charge, "step", no_physics)
    selected = source.resolve(service, original["id"], selector(readout))
    assert selected["capture_program_snapshot"] == original["capture_program_snapshot"]
    assert selected["control_trace"] == summary["electrical_control"]
    assert selected["control_trace"]["readout_id"] == meter
    assert selected["control_trace"]["observation_readout_index"] == summary["readout_ids"].index(
        meter
    )
    assert selected["retained_charge_events"] == summary["retained_charge"]["events"]
    assert selected["realized_acquisition"] == summary["prediction"]["sample_geometry"]
    names = {a["name"] for a in selected["input_artifacts"]}
    assert names == TIMING_NAMES | {
        "raw_groups.npy",
        "summary.json",
        "capture-program-source.json",
        "capture-program-normalized.json",
    }
    np.testing.assert_array_equal(source.load_inputs(service, selected), raw[:, index])
    assert selected["raw_format"]["response_type"] == (
        "nonlinear_charge_voltage" if "nonlinear" in family else "linear"
    )
    choices = [c for c in sdk.retained_isp_sources() if c["source_id"] == original["id"]]
    assert len(choices) == len(summary["readout_ids"])
    assert all(c["observation_readout_id"] == meter for c in choices)
    assert all(c["integrity"] == "verified_on_selection" for c in choices)

    process, calls = retained_isp.process_raw, []

    def only_original_codes(codes, raw_format, profile, **kwargs):
        np.testing.assert_array_equal(codes, raw[:, index])
        assert raw_format == summary["raw_formats"][index]
        assert set(kwargs) == {"geometry", "frame_records", "events"}
        assert kwargs["events"] == summary["retained_charge"]["events"]
        assert not any(
            key in canonical(kwargs)
            for key in (
                "trapped_e",
                "delivered_e",
                "captured_e",
                "released_e",
                "frame_ledger",
            )
        )
        calls.append(codes.copy())
        return process(codes, raw_format, profile, **kwargs)

    monkeypatch.setattr(retained_isp, "process_raw", only_original_codes)
    request = replay_request(service, sdk, original, readout)
    result = finish(service, sdk.post("replays", request))
    assert len(calls) == 1
    np.testing.assert_array_equal(load_array(service, result, "raw_frames"), raw[:, index])
    np.testing.assert_array_equal(
        load_array(service, result, "black_relative_dn_frames"),
        raw[:, index].astype(float) - selected["raw_format"]["black_level_dn"],
    )
    after = result["result"]["summary"]
    for key in (
        "control_trace",
        "planning_contract",
        "realized_acquisition",
        "retained_charge_events",
    ):
        assert after[key] == selected[key]
    assert not after["source_truth_arrays_used"] and not after["charge_inversion"]
    assert not after["dark_subtraction"]
    reasons = load_array(service, result, "temporal_reset_reason_frames")
    spatial = load_array(service, result, "spatial_dn_frames")
    temporal = load_array(service, result, "temporal_dn_frames")
    for frame in range(1, len(exposures)):
        if exposures[frame] != exposures[frame - 1]:
            assert np.all(reasons[frame] & retained_isp.RESET_REASONS["settings"])
            np.testing.assert_array_equal(temporal[frame], spatial[frame])
    assert sdk.post("replays", request)["id"] == result["id"]
    if family == "paired_nonlinear":
        forced = finish(service, sdk.post("replays", {**request, "force": True}))
        assert forced["id"] != result["id"]
        np.testing.assert_array_equal(
            load_array(service, forced, "rgb_frames"), load_array(service, result, "rgb_frames")
        )
        manifest = verify_bundle(sdk.replay_bundle(result["id"]), [original, result])
        assert manifest["source"] == original
        spec = specification(
            source_id=original["id"],
            isp_model_id=request["isp_model_id"],
            selector=selector(readout),
            roi={"row_start": 0, "row_end": 16, "column_start": 0, "column_end": 16},
        )
        planned = sdk.plan_retained_isp_comparison(spec)
        compared = finish(service, sdk.submit_retained_isp_comparison(planned))
        compared_summary = compared["result"]["summary"]
        for key in (
            "control_trace",
            "planning_contract",
            "realized_acquisition",
            "retained_charge_events",
        ):
            assert compared_summary[key] == selected[key]
        assert len(compared_summary["candidates"]) == 2
        assert sdk.submit_retained_isp_comparison(planned)["id"] == compared["id"]
        verify_bundle(sdk.retained_isp_comparison_bundle(compared["id"]), [original, compared])
        cold = PlatformService(service.registry.directory)
        assert replay.verify(cold, cold.registry.get_run(result["id"])) == result
        assert comparison.get(cold, compared["id"])["id"] == compared["id"]
    assert service.registry.get_run(original["id"]) == before


def test_forged_trace_event_schema_and_segment_evidence_are_refused(workflow, monkeypatch):
    service, _, original = acquisition(workflow)
    for edit in ("result_schema", "events", "trace", "schedule", "envelope", "segment_array"):
        altered = deepcopy(original)
        summary = altered["result"]["summary"]
        if edit == "result_schema":
            summary["schema_version"] = "camera_electrical_control_result_v1"
        elif edit == "events":
            event = summary["retained_charge"]["events"]
            event["frames"][1]["row_gap_ns"][0] += 1
            event["schedule_sha256"] = digest(
                {k: v for k, v in event.items() if k != "schedule_sha256"}
            )
        elif edit == "trace":
            summary["electrical_control"]["observation_readout_index"] = 1
        elif edit == "schedule":
            summary["retained_charge"]["trap_integration"]["schedule_sha256"] = "0" * 64
        elif edit == "envelope":
            summary["trap_control_work_envelope"]["maximum_exposure_ns"] += 1
        else:
            artifact = next(
                a for a in altered["artifacts"] if a["name"] == "trap_segment_end_ns.npy"
            )
            values = np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)
            values[0, 0, 0] += 1
            stream = io.BytesIO()
            np.save(stream, values, allow_pickle=False)
            artifact.update(
                service.registry.store_blob(
                    stream.getvalue(), artifact["name"], artifact["media_type"]
                )
            )
        rewrite_summary(service, altered)
        with monkeypatch.context() as patch:
            return_record(patch, service, altered)
            with pytest.raises(IntegrityError, match="Electrical"):
                source.resolve(service, altered["id"], selector("single"))


def test_same_size_timing_and_unselected_raw_corruption_refuse_cached_replay(workflow):
    service, sdk, original = acquisition(workflow, "paired")
    request = replay_request(service, sdk, original, "LCG")
    replayed = finish(service, sdk.post("replays", request))
    for name in ("trap_segment_duration_ns.npy", "raw_groups.npy"):
        artifact = next(a for a in original["artifacts"] if a["name"] == name)
        path = service.registry.blob_path(artifact["sha256"])
        before = path.read_bytes()
        # RAW byte at header end is on HCG, unselected for LCG ISP. A change in
        # either path invalidates the original shared acquisition container.
        data = bytearray(before)
        if name == "raw_groups.npy":
            stream = io.BytesIO(before)
            np.lib.format.read_magic(stream)
            np.lib.format.read_array_header_1_0(stream)
            offset = stream.tell()
        else:
            offset = len(data) - 1
        data[offset] ^= 1
        path.write_bytes(data)
        try:
            with pytest.raises(IntegrityError):
                replay.submit(service, request)
        finally:
            path.write_bytes(before)
    assert replay.submit(service, request)["id"] == replayed["id"]
