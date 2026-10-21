"""Actual retained-run provenance and RAW-only source boundary checks."""

import io
import json
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow

from camera_platform import dcg_electrical, nonlinear_examples
from camera_platform import retained_isp_source as source
from camera_platform.registry import IntegrityError, canonical


def selector(path="single"):
    return {"schema": source.SELECTION_SCHEMA, "readout_id": path}


def complete(workflow, family="linear"):
    service, sdk, assembly, scene, control, parent, _, request = workflow
    if family == "linear":
        return service, finish(service, service.submit_run(request))
    draft = sdk.template(
        "capture-program",
        assembly_id=assembly["id"],
        mode="retained_charge",
        control_sequence_id=control["id"],
    )
    models = {
        "nonlinear": nonlinear_examples.single_model,
        "paired": dcg_electrical.default_model,
        "paired_nonlinear": nonlinear_examples.shared_model,
    }
    draft.update(
        name="Independent retained source " + family, revision=family, parents=[parent["id"]]
    )
    draft["payload"]["electrical_model"] = models[family](
        parent["sensor_model_snapshot"]["payload"]["readout"]
    )
    program = sdk.create_capture_program(draft)
    test = service.create_test(
        sdk.template(
            "temporal-test",
            assembly_id=assembly["id"],
            scene_sequence_id=scene["id"],
            control_sequence_id=control["id"],
            capture_program_id=program["id"],
        )
    )
    request = {**request, "test_id": test["id"], "capture_program_id": program["id"]}
    return service, finish(service, service.submit_run(request))


@pytest.mark.parametrize("family", ["linear", "nonlinear", "paired", "paired_nonlinear"])
def test_all_families_and_paths_preserve_original_raw_and_history_without_decoder(workflow, family):
    service, run = complete(workflow, family)
    paths = ["HCG", "LCG"] if family.startswith("paired") else ["single"]
    container = load_array(service, run, "raw_groups")
    sources = []
    for index, path in enumerate(paths):
        resolved = source.resolve(service, run["id"], selector(path))
        actual = source.load_inputs(service, resolved)
        np.testing.assert_array_equal(actual, container[:, index])
        assert actual.shape == (4, 16, 16) and actual.dtype == np.int32
        assert resolved["raw_shape"] == [4, 16, 16]
        assert resolved["raw_container_shape"] == list(container.shape)
        assert resolved["selected_array_sha256"] == source.array_sha256(container[:, index])
        assert resolved["raw_format"] == run["result"]["summary"]["raw_formats"][index]
        assert resolved["raw_format"]["response_type"] == (
            "nonlinear_charge_voltage" if "nonlinear" in family else "linear"
        )
        assert resolved["retained_charge_events"] == run["request"]["retained_charge_events"]
        assert resolved["source_snapshot"] == run
        assert resolved["source_snapshot_role"] == "evidence_only_not_processing_inputs"
        assert resolved["processing_contract"] == source.PROCESSING_CONTRACT
        assert "normalization_e" not in resolved and "calibration_summaries" not in resolved
        assert {a["name"] for a in resolved["input_artifacts"]} == {
            "raw_groups.npy",
            "summary.json",
            "capture-program-source.json",
            "capture-program-normalized.json",
        }
        assert resolved["readout_id"] == path and resolved["readout_index"] == index
        assert [f["frame_id"] for f in resolved["frame_records"]] == run["result"]["summary"][
            "frame_ids"
        ]
        assert all(identity["readout_id"] == path for identity in resolved["acquisition_identity"])
        json.dumps(resolved, allow_nan=False)
        actual.fill(0)
        np.testing.assert_array_equal(source.load_inputs(service, resolved), container[:, index])
        sources.append(resolved)
    if len(paths) == 2:
        assert sources[0]["raw_sha256"] == sources[1]["raw_sha256"]
        assert sources[0]["selected_array_sha256"] != sources[1]["selected_array_sha256"]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"schema": source.SELECTION_SCHEMA},
        {"schema": source.SELECTION_SCHEMA, "readout_id": "hcg"},
        {"schema": source.SELECTION_SCHEMA, "readout_id": "lcg"},
        {"schema": source.SELECTION_SCHEMA, "readout_id": "Single"},
        {"schema": source.SELECTION_SCHEMA, "readout_id": ["HCG"]},
        {"schema": source.SELECTION_SCHEMA, "readout_id": "single", "mode": "sequential_frames"},
        {"schema": "camera_nonlinear_raw_selection_v1", "readout_id": "single"},
    ],
)
def test_selection_is_explicit_case_sensitive_and_has_no_legacy_mode(bad):
    with pytest.raises(ValueError, match="case-sensitive"):
        source.validate_selector(bad)


def test_single_source_refuses_a_nonexistent_paired_path(workflow):
    service, run = complete(workflow)
    for path in ("HCG", "LCG"):
        with pytest.raises(ValueError, match="does not exist"):
            source.resolve(service, run["id"], selector(path))


def test_completed_source_is_required_and_other_runtime_is_refused(workflow, monkeypatch):
    service, *_, request = workflow
    queued = service.submit_run(request)
    with pytest.raises(ValueError, match="completed retained"):
        source.resolve(service, queued["id"], selector())
    run = finish(service, queued)
    original_get = service.registry.get_run
    wrong = deepcopy(run)
    wrong["plugin_id"] = "hdr_bracket_v1"
    monkeypatch.setattr(
        service.registry,
        "get_run",
        lambda identity: wrong if identity == run["id"] else original_get(identity),
    )
    with pytest.raises(ValueError, match="completed retained"):
        source.resolve(service, run["id"], selector())


def test_older_environment_is_preserved_not_required_to_match_replay_environment(
    workflow, monkeypatch
):
    service, run = complete(workflow)
    older = deepcopy(run)
    older["environment"]["version"] = "0.1.6.dev15-retained-historical-source"
    original_get = service.registry.get_run
    monkeypatch.setattr(
        service.registry,
        "get_run",
        lambda identity: older if identity == run["id"] else original_get(identity),
    )
    resolved = source.resolve(service, run["id"], selector())
    assert resolved["source_snapshot"]["environment"] == older["environment"]
    np.testing.assert_array_equal(
        source.load_inputs(service, resolved), load_array(service, run, "raw_groups")[:, 0]
    )


def rewritten_summary(service, run):
    """Give altered metadata matching bytes, so deeper binding guards are exercised."""
    artifact = service.registry.store_blob(
        canonical(run["result"]["summary"]).encode(), "summary.json", "application/json"
    )
    run["artifacts"] = [artifact if a["name"] == "summary.json" else a for a in run["artifacts"]]


@pytest.mark.parametrize(
    "change",
    [
        "control_snapshot",
        "model_snapshot",
        "scene_snapshot",
        "test_snapshot",
        "program_snapshot",
        "summary_bytes",
        "events",
        "descriptor",
        "format",
        "frame_id",
        "noise",
    ],
)
def test_registered_snapshots_summary_and_history_receipts_cannot_be_relabelled(
    workflow, monkeypatch, change
):
    service, original = complete(workflow)
    run = deepcopy(original)
    summary = run["result"]["summary"]
    if change.endswith("_snapshot"):
        key = {
            "control_snapshot": "control_sequence_snapshot",
            "scene_snapshot": "scene_sequence_snapshot",
            "test_snapshot": "test_snapshot",
            "program_snapshot": "capture_program_snapshot",
        }.get(change)
        if change == "model_snapshot":
            run["model_snapshots"][0]["name"] += " altered"
        else:
            run[key]["name"] += " altered"
    elif change == "summary_bytes":
        summary["name"] = "not the saved summary"
    else:
        if change == "events":
            summary["retained_charge"]["events"]["frames"][0]["row_transfer_ns"][0] += 1
        elif change == "descriptor":
            summary["readout_descriptors"][0]["input_quantity"] = "current_exposure_photocharge_e"
        elif change == "format":
            summary["raw_formats"][0]["response_type"] = "nonlinear_charge_voltage"
        elif change == "frame_id":
            summary["frames"][0]["frame_id"] = "another-frame"
        else:
            summary["noise_enabled"] = True
        rewritten_summary(service, run)
    get = service.registry.get_run
    monkeypatch.setattr(
        service.registry,
        "get_run",
        lambda identity: run if identity == run["id"] else get(identity),
    )
    with pytest.raises(IntegrityError):
        source.resolve(service, run["id"], selector())


def test_frozen_selection_detects_metadata_and_selected_byte_hash_tampering(workflow):
    service, run = complete(workflow)
    original = source.resolve(service, run["id"], selector())
    for field, value in [
        ("selected_array_sha256", "0" * 64),
        ("pixel_pitch_um", 99),
        ("readout_index", 1),
        ("normalization_e", 10000),
    ]:
        altered = deepcopy(original)
        altered[field] = value
        with pytest.raises(IntegrityError, match="frozen evidence"):
            source.load_inputs(service, altered)


def test_another_registered_control_with_same_timing_cannot_replace_original_test_binding(
    workflow, monkeypatch
):
    from camera_platform.contracts import ControlSequenceCreate

    service, original = complete(workflow)
    control = original["control_sequence_snapshot"]
    draft = {k: deepcopy(v) for k, v in control.items() if k in ControlSequenceCreate.model_fields}
    draft.update(name="A different registered control", revision="source-binding-check")
    replacement = workflow[1].create_control_sequence(draft)
    assert replacement["id"] != control["id"]
    assert replacement["payload"] == control["payload"]
    altered = deepcopy(original)
    altered.update(control_sequence_id=replacement["id"], control_sequence_snapshot=replacement)
    get = service.registry.get_run
    monkeypatch.setattr(
        service.registry,
        "get_run",
        lambda ident: altered if ident == original["id"] else get(ident),
    )
    with pytest.raises(IntegrityError):
        source.resolve(service, original["id"], selector())


def test_no_reservoir_truth_output_array_is_read_or_needed_for_processing(workflow, monkeypatch):
    service, run = complete(workflow)
    required = {a["sha256"] for a in run["input_artifacts"]}
    diagnostic = {
        a["sha256"] for a in run["artifacts"] if a["name"].startswith("retained_")
    } - required
    assert diagnostic
    original = service.registry.blob_path
    seen = []

    def forbid_truth(identity):
        assert identity not in diagnostic, (
            "Reservoir truth arrays must not enter the RAW source resolver"
        )
        seen.append(identity)
        return original(identity)

    monkeypatch.setattr(service.registry, "blob_path", forbid_truth)
    resolved = source.resolve(service, run["id"], selector())
    actual = source.load_inputs(service, resolved)
    assert actual.shape == (4, 16, 16)
    assert resolved["raw_sha256"] in seen
    assert resolved["summary_artifact"]["sha256"] in seen
    assert not set(seen) & diagnostic


@pytest.mark.parametrize(
    "change", ["shape", "dtype", "unit", "bytes", "range", "paths", "trailing", "fortran"]
)
def test_original_raw_descriptor_header_and_code_bounds_are_verified(workflow, monkeypatch, change):
    service, original = complete(workflow)
    run = deepcopy(original)
    old = next(a for a in run["artifacts"] if a["name"] == "raw_groups.npy")
    if change in ("shape", "dtype", "unit", "bytes"):
        replacement = deepcopy(old)
        replacement[change] = {
            "shape": [4, 2, 16, 16],
            "dtype": "uint16",
            "unit": "electron",
            "bytes": source.MAX_INPUT_BYTES + 1,
        }[change]
    else:
        array = load_array(service, original, "raw_groups")
        if change == "range":
            array[0, 0, 0, 0] = -1
        elif change == "paths":
            array = np.repeat(array, 3, axis=1)
        elif change == "fortran":
            array = np.asfortranarray(array)
        stream = io.BytesIO()
        np.save(stream, array, allow_pickle=False)
        content = stream.getvalue() + (b"extra" if change == "trailing" else b"")
        replacement = service.registry.store_blob(
            content, "raw_groups.npy", "application/octet-stream"
        )
        replacement.update(
            shape=list(array.shape),
            dtype=str(array.dtype),
            unit="DN",
            spatial_sampling="native_sensor",
        )
    run["artifacts"] = [
        replacement if a["name"] == "raw_groups.npy" else a for a in run["artifacts"]
    ]
    get = service.registry.get_run
    monkeypatch.setattr(
        service.registry,
        "get_run",
        lambda identity: run if identity == run["id"] else get(identity),
    )
    with pytest.raises((ValueError, IntegrityError)):
        source.resolve(service, run["id"], selector())
