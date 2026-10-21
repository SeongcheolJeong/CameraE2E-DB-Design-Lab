"""One frozen retained-code candidate must face independent repeats and time truth."""

import hashlib
import io
import json
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import finish
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow
from test_retained_isp import inputs

from camera_platform import retained_isp, sequences
from camera_platform import retained_isp_joint_study as joint
from camera_platform.contracts import SceneSequenceCreate
from camera_platform.isp_charge_source import array_sha256
from camera_platform.registry import IntegrityError, digest


def _scene(pattern="static", hashes=None):
    hashes = hashes or ["a" * 64, "a" * 64]
    return {
        "id": "scene-" + pattern,
        "content_hash": "b" * 64,
        "fixture_specification": {"parameters": {"pattern": pattern}},
        "payload": {"knots": [
            {"spectral_image_id": "image-0"}, {"spectral_image_id": "image-1"},
        ]},
        "image_snapshots": [
            {"id": f"image-{index}", "artifacts": [
                {"name": "spectral-image.npy", "sha256": sha}
            ]} for index, sha in enumerate(hashes)
        ],
    }


def _source(name, raw, *, seed, scene=None, waveform=None, clock=None):
    metadata = inputs(raw)
    test = {"id": "static-test" if scene is None else "moving-test",
            "content_hash": "test-hash"}
    scene = deepcopy(scene) if scene is not None else _scene()
    original = {
        "id": name, "assembly_id": "camera-a", "test_id": test["id"],
        "test_snapshot": test, "scene_sequence_snapshot": scene,
        "parameters": {"seed": seed, "noise_enabled": True},
    }
    return {
        "source_kind": "run", "source_id": name,
        "selector": {"schema": "camera_retained_raw_selection_v1", "readout_id": "single"},
        "raw_sha256": hashlib.sha256(name.encode()).hexdigest(),
        "selected_array_sha256": array_sha256(raw),
        "raw_shape": list(raw.shape),
        "raw_format": metadata["raw_format"],
        "geometry": metadata["geometry"], "pixel_pitch_um": 3.0,
        "frame_records": metadata["frame_records"],
        "retained_charge_events": metadata["events"],
        "frame_conditions": [{"frame_id": f"f{i}", "exposure_ns": 4_000_000,
                              "gain": 1.0} for i in range(len(raw))],
        "clock": clock or {"clock_id": "relative-test", "unit": "ns"},
        "timing": {"schema": "fixed-8ms"},
        "source_snapshot": original,
        "capture_program_snapshot": {"payload": {
            "electrical_model": {"id": "electrical-a"},
            "retained_charge_model": {"id": "retained-a"},
            "waveform": waveform,
        }},
        "source_provenance": {"source_kind": "simulated",
                              "source_uri": "run://" + name},
        "acquisition_identity": [
            {"source_id": name, "acquisition_id": f"f{i}"}
            for i in range(len(raw))
        ],
    }


def _reference(source, model, truth):
    stream = io.BytesIO()
    np.save(stream, truth, allow_pickle=False)
    content = stream.getvalue()
    original = source["source_snapshot"]
    test = original["test_snapshot"]
    fixed = {key: deepcopy(model["payload"][key])
             for key in ("demosaic", "white_balance", "ccm")}
    binding = {
        "source_id": source["source_id"], "selector": source["selector"],
        "raw_sha256": source["raw_sha256"],
        "selected_array_sha256": source["selected_array_sha256"],
        "source_snapshot_sha256": digest(original),
        "source_test_id": test["id"], "source_test_sha256": digest(test),
        "geometry": source["geometry"], "raw_shape": source["raw_shape"],
        "raw_format": source["raw_format"], "clock": source["clock"],
        "frame_ids": [r["frame_id"] for r in source["frame_records"]],
        "frame_conditions_sha256": digest(source["frame_conditions"]),
        "frame_records_sha256": digest(source["frame_records"]),
        "timing_sha256": digest(source["timing"]),
        "event_schedule_sha256": source["retained_charge_events"]["schedule_sha256"],
        "source_provenance": source["source_provenance"],
        "isp_model_id": model["id"], "isp_model_sha256": digest(model),
        "fixed_color_profile_sha256": digest(fixed),
    }
    reference = {
        "id": "temporal-reference-a",
        "schema": "camera_temporal_signal_reference_v1",
        "name": "Analytic independent step", "revision": "1",
        "derivation": "authored_independent", "source_kind": "simulated",
        "source_uri": "synthetic://joint-audit-step",
        "method_and_version": "analytic-step:1", "license": "test fixture",
        "description": "Generated from an analytic step independent of ISP candidates.",
        "uncertainty": {"kind": "unknown", "reason": "Authored fixture"},
        "source_id": source["source_id"], "selector": source["selector"],
        "isp_model_id": model["id"], "source_binding": binding,
        "fixed_color_profile": fixed,
        "roi": {"row_start": 1, "row_end": truth.shape[1] - 1,
                "column_start": 1, "column_end": truth.shape[2] - 1},
        "row_transfer_ns": [f["row_transfer_ns"] for f in
                            source["retained_charge_events"]["frames"]],
        "step": {"onset_frame": 2, "steady_start_frame": 2,
                 "onset_fraction": 0.5, "settling_fraction": 0.2},
        "flicker_window": None, "minimum_reference_change": 0.01,
        "reference_stage": "ccm_code_rgb_frames", "unit": "normalized_code_rgb",
        "array_artifact": {"sha256": hashlib.sha256(content).hexdigest(),
                           "bytes": len(content)},
    }
    return reference, content


def _case():
    profile = retained_isp.default_profile()
    profile["temporal_filter"].update(mode="causal_difference_gate_v1",
                                      threshold_dn=4095)
    model = {"id": "isp-model-a", "kind": "isp", "payload": profile,
             "input_raw_format": inputs(np.full((4, 8, 8), 100, dtype=np.int32))["raw_format"]}
    repeats = []
    for seed in (11, 12, 13, 14):
        jitter = np.random.default_rng(seed).integers(-24, 25, size=(4, 8, 8))
        raw = (np.full((4, 8, 8), 1600) + jitter).astype(np.int32)
        repeats.append((_source(f"static-{seed}", raw, seed=seed), raw))
    code = np.asarray([864, 864, 3289, 3289], dtype=np.int32)
    dynamic_raw = np.broadcast_to(code[:, None, None], (4, 8, 8)).copy()
    dynamic = (_source("dynamic-99", dynamic_raw, seed=99,
                       scene=_scene("moving")), dynamic_raw)
    truth = np.broadcast_to(((code - 64) / 4031.0)[:, None, None, None],
                            (4, 8, 8, 3)).copy()
    reference, content = _reference(dynamic[0], model, truth)
    axes = [{"parameter": "temporal_filter.previous_weight", "values": [0, 0.5]}]
    objectives = [
        {"metric": joint.NOISE, "direction": "minimize"},
        {"metric": "signal_change_rms_error", "direction": "minimize"},
    ]
    constraints = [
        {"metric": "truth_code_mae", "operator": "<=", "value": 1,
         "unit": "normalized_code_rgb"},
        {"metric": "signal_amplitude_error", "operator": "<=", "value": 1,
         "unit": "1"},
        {"metric": "signal_change_rms_error", "operator": "<=", "value": 1,
         "unit": "1"},
    ]
    kwargs = {"axes": axes,
              "noise_roi": {"row_start": 1, "row_end": 7,
                            "column_start": 1, "column_end": 7},
              "objectives": objectives, "constraints": constraints}
    return model, repeats, dynamic, reference, content, kwargs


def _run(case):
    model, repeats, dynamic, reference, content, kwargs = case
    spec = joint.make_specification(model, repeats, dynamic, reference, **kwargs)
    return joint.evaluate(spec, model, repeats, dynamic, reference, content)


def test_same_profile_is_processed_on_independent_repeats_and_dynamic_truth(monkeypatch):
    case = _case()
    original = joint.process_raw
    calls = []

    def actual_processor(raw, raw_format, profile, **kwargs):
        calls.append((array_sha256(raw), digest(profile)))
        return original(raw, raw_format, profile, **kwargs)

    monkeypatch.setattr(joint, "process_raw", actual_processor)
    result = _run(case)
    assert len(calls) == 10  # four independent runs plus dynamic, for two profiles
    assert result["truth_role"] == "evaluation_only_never_processor_input"
    assert result["physical_validation"] == "not_established"
    assert result["ranking"]["selected_candidate_id"] is None
    assert result["ranking"]["pareto_candidate_ids"] == ["candidate-000", "candidate-001"]
    first, second = result["cases"]
    assert first["metrics"][joint.NOISE]["value"] > second["metrics"][joint.NOISE]["value"]
    assert first["metrics"]["signal_change_rms_error"]["value"] == pytest.approx(0)
    assert second["metrics"]["signal_change_rms_error"]["value"] > 0
    assert first["dynamic_processing"]["temporal_gate_active_pixels"] == 0
    assert second["dynamic_processing"]["temporal_gate_active_pixels"] > 0
    assert all(item["temporal_gate_active_pixels"] > 0
               for item in second["repeat_processing"])
    for candidate in result["cases"]:
        assert len({item["processor_profile_sha256"] for item in candidate["repeat_processing"]}
                   | {candidate["dynamic_processing"]["processor_profile_sha256"]}) == 1
    json.dumps(result, allow_nan=False)


def test_saved_candidate_arrays_recompute_metrics_without_processor(monkeypatch):
    model, repeats, dynamic, reference, content, kwargs = _case()
    specification = joint.make_specification(model, repeats, dynamic, reference, **kwargs)
    blobs = {}

    def retain(name, array):
        stream = io.BytesIO()
        np.save(stream, array, allow_pickle=False)
        data = stream.getvalue()
        blobs[name] = data
        return {"name": name, "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data), "media_type": "application/x-npy"}

    result = joint.evaluate(
        specification, model, repeats, dynamic, reference, content,
        retain_array=retain,
    )
    assert result["schema"] == joint.RESULT_SCHEMA
    assert len(blobs) == 30  # pre-tone, gate and delivered display RGB per invocation
    display_name = result["cases"][0]["dynamic_processing"]["display_rgb_artifact"]["name"]
    pre_tone_name = result["cases"][0]["dynamic_processing"]["pre_tone_artifact"]["name"]
    display = np.load(io.BytesIO(blobs[display_name]), allow_pickle=False)
    pre_tone = np.load(io.BytesIO(blobs[pre_tone_name]), allow_pickle=False)
    clipped = np.clip(pre_tone, 0, 1)
    expected_display = np.where(clipped <= 0.0031308, 12.92 * clipped,
                                1.055 * np.power(clipped, 1 / 2.4) - 0.055)
    np.testing.assert_allclose(display, expected_display, rtol=0, atol=1e-15)
    ambiguous_repeats = deepcopy(repeats)
    ambiguous_repeats[1][0]["source_snapshot"]["parameters"]["seed"] = 11
    ambiguous_specification = joint.make_specification(
        model, ambiguous_repeats, dynamic, reference, **kwargs,
    )
    ambiguous_result = joint.evaluate(
        ambiguous_specification, model, ambiguous_repeats, dynamic, reference, content,
        retain_array=retain,
    )
    assert all(case["metrics"][joint.NOISE]["status"] == "unknown"
               for case in ambiguous_result["cases"])

    def load(artifact, shape, dtype):
        data = blobs[artifact["name"]]
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
        value = np.load(io.BytesIO(data), allow_pickle=False)
        assert value.shape == shape and value.dtype == dtype
        return value

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Saved evidence verification reran the processor")

    monkeypatch.setattr(joint, "process_raw", forbidden)
    joint.verify_saved_evidence(
        specification, model, repeats, dynamic, reference, content, result, load,
    )
    joint.verify_saved_evidence(
        ambiguous_specification, model, ambiguous_repeats, dynamic, reference, content,
        ambiguous_result, load,
    )
    altered = deepcopy(result)
    altered["cases"][0]["metrics"][joint.NOISE]["value"] += 0.01
    with pytest.raises(IntegrityError, match="saved metrics differ from retained arrays"):
        joint.verify_saved_evidence(
            specification, model, repeats, dynamic, reference, content, altered, load,
        )
    altered = deepcopy(result)
    altered["cases"][0]["repeat_processing"][0]["pre_tone_artifact"]["name"] = (
        "candidate-001-stationary-00-pre-tone.npy"
    )
    with pytest.raises(IntegrityError, match="array/source alias differs"):
        joint.verify_saved_evidence(
            specification, model, repeats, dynamic, reference, content, altered, load,
        )
    altered_display = deepcopy(result)
    display_record = altered_display["cases"][0]["dynamic_processing"]
    display_name = display_record["display_rgb_artifact"]["name"]
    original_display_bytes = blobs[display_name]
    wrong_display = np.zeros_like(display)
    stream = io.BytesIO()
    np.save(stream, wrong_display, allow_pickle=False)
    blobs[display_name] = stream.getvalue()
    display_record["display_rgb_artifact"]["sha256"] = hashlib.sha256(
        blobs[display_name]
    ).hexdigest()
    display_record["display_rgb_artifact"]["bytes"] = len(blobs[display_name])
    display_record["display_rgb_sha256"] = joint._sha_array(wrong_display)
    with pytest.raises(IntegrityError, match="display RGB differs from pre-tone"):
        joint.verify_saved_evidence(
            specification, model, repeats, dynamic, reference, content, altered_display, load,
        )
    blobs[display_name] = original_display_bytes
    altered_unknown = deepcopy(ambiguous_result)
    altered_unknown["cases"][0]["metrics"][joint.NOISE]["reason"] = "another reason"
    with pytest.raises(IntegrityError, match="saved metrics differ from retained arrays"):
        joint.verify_saved_evidence(
            ambiguous_specification, model, ambiguous_repeats, dynamic, reference,
            content, altered_unknown, load,
        )


def test_legacy_v2_retained_arrays_remain_readable_without_final_rgb():
    model, repeats, dynamic, reference, content, kwargs = _case()
    specification = joint.make_specification(model, repeats, dynamic, reference, **kwargs)
    blobs = {}

    def retain(name, array):
        stream = io.BytesIO()
        np.save(stream, array, allow_pickle=False)
        data = stream.getvalue()
        blobs[name] = data
        return {"name": name, "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data), "media_type": "application/x-npy"}

    result = joint.evaluate(
        specification, model, repeats, dynamic, reference, content,
        retain_array=retain, output_version=2,
    )
    assert result["schema"] == joint.RESULT_SCHEMA_V2
    assert len(blobs) == 20
    assert all("display_rgb_artifact" not in record
               for case in result["cases"]
               for record in [*case["repeat_processing"], case["dynamic_processing"]])

    def load(artifact, shape, dtype):
        value = np.load(io.BytesIO(blobs[artifact["name"]]), allow_pickle=False)
        assert value.shape == shape and value.dtype == dtype
        return value

    joint.verify_saved_evidence(
        specification, model, repeats, dynamic, reference, content, result, load,
    )


@pytest.mark.parametrize("change", ["moving_knot", "periodic_waveform", "duplicate_seed"])
def test_ambiguous_static_repetition_is_unknown_and_excluded(change):
    case = _case()
    model, repeats, dynamic, reference, content, kwargs = case
    if change == "moving_knot":
        repeats[0][0]["source_snapshot"]["scene_sequence_snapshot"]["image_snapshots"][1][
            "artifacts"][0]["sha256"] = "c" * 64
    elif change == "periodic_waveform":
        repeats[0][0]["capture_program_snapshot"]["payload"]["waveform"] = {
            "kind": "pwm"}
    else:
        repeats[1][0]["source_snapshot"]["parameters"]["seed"] = 11
    spec = joint.make_specification(model, repeats, dynamic, reference, **kwargs)
    result = joint.evaluate(spec, model, repeats, dynamic, reference, content)
    assert result["ranking"]["pareto_candidate_ids"] == []
    assert result["ranking"]["excluded_candidate_ids"] == ["candidate-000", "candidate-001"]
    assert all(row["noise_support_status"] == "unknown" for row in result["cases"])


def test_manual_static_sequence_without_fixture_marker_is_accepted(workflow):
    service, _, _, fixture, _, _, _, _ = workflow
    request = {key: deepcopy(fixture[key]) for key in SceneSequenceCreate.model_fields}
    request.update(
        name="Manually registered byte-identical static source",
        revision="static-manual-1",
        source_uri="fixture://registered-static-republication",
        method_and_version="manual_static_sequence_registration:1",
        parents=[fixture["id"]],
        fixture_specification=None,
    )
    registered = sequences.create_sequence(service, "scene_sequences", request)
    assert service.registry.get("scene_sequences", registered["id"]) == registered
    assert registered["fixture_specification"] is None
    assert joint._constant_scene(registered) is not None
    assert len({image["id"] for image in registered["image_snapshots"]}) == 2

    # The RAW samples below are synthetic. This checks acceptance of a real
    # registered immutable scene snapshot, not physical camera accuracy.
    case = _case()
    for source, _ in case[1]:
        source["source_snapshot"]["scene_sequence_snapshot"] = deepcopy(registered)
    result = _run(case)
    assert result["physical_validation"] == "not_established"
    assert result["ranking"]["pareto_candidate_ids"]
    assert all(item["noise_support_status"] == "ok" for item in result["cases"])


def test_identical_published_image_artifact_aliases_are_one_static_source():
    scene = _scene()
    for image in scene["image_snapshots"]:
        image["artifacts"][0]["bytes"] = 128
        image["artifacts"].append(deepcopy(image["artifacts"][0]))
    assert joint._constant_scene(scene) == "a" * 64

    changed = deepcopy(scene)
    changed["image_snapshots"][0]["artifacts"][1]["bytes"] = 129
    assert joint._constant_scene(changed) is None
    changed = deepcopy(scene)
    changed["image_snapshots"][0]["artifacts"][1]["sha256"] = "c" * 64
    assert joint._constant_scene(changed) is None


def test_nonstatic_fixture_marker_still_excludes_even_if_images_alias():
    case = _case()
    for source, _ in case[1]:
        source["source_snapshot"]["scene_sequence_snapshot"]["fixture_specification"][
            "parameters"]["pattern"] = "moving_spot"
    result = _run(case)
    assert result["ranking"]["pareto_candidate_ids"] == []
    assert all(item["noise_support_reason"] ==
               "Authored fixture does not declare a static scene" for item in result["cases"])


@pytest.mark.parametrize("missing_secondary", [False, True])
def test_manual_static_primary_does_not_hide_unverified_secondary(missing_secondary):
    case = _case()
    for source, _ in case[1]:
        original = source["source_snapshot"]
        original["scene_sequence_snapshot"].pop("fixture_specification")
        original["unmodulated_scene_sequence_id"] = "secondary-scene"
        if not missing_secondary:
            original["unmodulated_scene_sequence_snapshot"] = _scene(
                "secondary", ["a" * 64, "c" * 64],
            )
    result = _run(case)
    assert result["ranking"]["pareto_candidate_ids"] == []
    assert all(item["noise_support_status"] == "unknown" for item in result["cases"])


def test_frozen_raw_profile_and_truth_cannot_be_replaced():
    model, repeats, dynamic, reference, content, kwargs = _case()
    spec = joint.make_specification(model, repeats, dynamic, reference, **kwargs)
    altered = deepcopy(repeats)
    altered[0][1][0, 0, 0] += 1
    with pytest.raises(IntegrityError, match="selected RAW bytes"):
        joint.evaluate(spec, model, altered, dynamic, reference, content)
    changed_model = deepcopy(model)
    changed_model["payload"]["temporal_filter"]["threshold_dn"] = 4000
    with pytest.raises(IntegrityError, match="frozen specification"):
        joint.evaluate(spec, changed_model, repeats, dynamic, reference, content)
    with pytest.raises(IntegrityError, match="truth bytes"):
        joint.evaluate(spec, model, repeats, dynamic, reference, content[:-1] + b"0")


def test_joint_plan_refuses_charge_axis_and_duplicate_acquisition():
    model, repeats, dynamic, reference, _, kwargs = _case()
    bad = {**kwargs, "axes": [{"parameter": "raw_denoise.strength", "values": [0, 0.5]}]}
    with pytest.raises(ValueError, match="outside retained-code ISP"):
        joint.make_specification(model, repeats, dynamic, reference, **bad)
    duplicate = deepcopy(repeats)
    duplicate[1][0]["source_id"] = duplicate[0][0]["source_id"]
    for acquisition in duplicate[1][0]["acquisition_identity"]:
        acquisition["source_id"] = duplicate[0][0]["source_id"]
    with pytest.raises(ValueError, match="distinct source IDs"):
        joint.make_specification(model, duplicate, dynamic, reference, **kwargs)


def test_joint_plan_refuses_minimizing_signal_amplitude_retention():
    model, repeats, dynamic, reference, _, kwargs = _case()
    bad = {**kwargs, "constraints": [*kwargs["constraints"], {
        "metric": "signal_amplitude_retention", "operator": "<=", "value": 1,
        "unit": "1",
    }]}
    with pytest.raises(ValueError, match="target metric is unsupported"):
        joint.make_specification(model, repeats, dynamic, reference, **bad)


def test_registered_retained_sources_have_usable_static_repeat_support(workflow):
    from camera_platform.retained_isp_source import load_inputs, resolve

    service, _, _, _, _, _, _, request = workflow
    repeats = []
    profile = retained_isp.default_profile()
    for seed in (601, 602, 603, 604):
        submitted = service.submit_run({
            **request, "parameters": {"seed": seed, "noise_enabled": True},
        })
        run = finish(service, submitted)
        selector = {"schema": "camera_retained_raw_selection_v1", "readout_id": "single"}
        source = resolve(service, run["id"], selector)
        raw = load_inputs(service, source)
        repeats.append((source, raw))
        assert source["source_snapshot"]["scene_sequence_snapshot"][
            "fixture_specification"]["parameters"]["pattern"] == "static"
        assert joint._constant_scene(source["source_snapshot"]["scene_sequence_snapshot"])
        assert source["capture_program_snapshot"]["payload"]["waveform"] is None
    bindings = [joint._source_binding(source, raw, profile) for source, raw in repeats]
    assert joint._stationarity(repeats, bindings, profile) is None
