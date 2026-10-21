"""Versioned time truth through API, frozen Test, worker and cold readback."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

import httpx
import numpy as np
import pytest
from test_hdr_workflow import finish
from test_hdr_workflow import workflow as _hdr_workflow
from test_retained_charge_workflow import workflow as workflow
from test_retained_isp_workflow import acquisition, payload

from camera_platform import retained_isp_comparison as comparison
from camera_platform import temporal_signal_reference as reference
from camera_platform.backup import backup_state, restore_state
from camera_platform.registry import IntegrityError
from camera_platform.service import PlatformService

hdr_workflow = _hdr_workflow


def _spec(run, model, test, shape):
    return {
        "schema": comparison.SCHEMA_V2,
        "name": "Time-truth candidate comparison",
        "source_kind": "run", "source_id": run["id"],
        "selector": {"schema": "camera_retained_raw_selection_v1", "readout_id": "single"},
        "isp_model_id": model,
        "axes": [{"parameter": "temporal_filter.previous_weight", "values": [0, 0.5]}],
        "roi": {"row_start": 0, "row_end": shape[1],
                "column_start": 0, "column_end": shape[2]},
        "objectives": [
            {"metric": "mean_abs_baseline_delta", "direction": "minimize"},
            {"metric": "spatial_tv", "direction": "minimize"},
        ],
        "limits": {"max_frame_mean_abs_delta": 1,
                   "max_frame_temporal_change_distortion": 1},
        "temporal_test_id": test["id"],
        "temporal_objectives": [
            {"metric": "signal_change_rms_error", "direction": "minimize"}
        ],
        "temporal_constraints": [
            {"metric": "truth_code_mae", "operator": "<=", "value": 1,
             "unit": "normalized_code_rgb"},
            {"metric": "signal_amplitude_error", "operator": "<=", "value": 1,
             "unit": "1"},
            {"metric": "signal_change_rms_error", "operator": "<=", "value": 0.2,
             "unit": "1"},
        ],
    }


def test_v2_api_sdk_worker_reference_test_readback_and_bundle(workflow, tmp_path, monkeypatch):
    from camera_platform import retained_isp as processor

    service, sdk, run = acquisition(workflow)
    replay = payload(service, sdk, run, enabled=True)
    model = replay["isp_model_id"]
    metadata = sdk.temporal_signal_reference_template(run["id"], "single", model)
    frames, rows, columns = metadata["source_binding"]["raw_shape"]
    assert frames >= 2
    truth = np.full((frames, rows, columns, 3), 0.2, dtype=np.float64)
    truth[-1] = 0.8
    metadata.update(
        name="Independent authored step",
        source_uri="synthetic://fixed-step-generator-v1",
        method_and_version="analytic-fixed-step-v1",
        license="test-fixture",
        description="Analytic authored step; no candidate array was used.",
        roi={"row_start": 0, "row_end": rows, "column_start": 0, "column_end": columns},
        step={"onset_frame": frames - 1, "steady_start_frame": frames - 1,
              "onset_fraction": 0.5, "settling_fraction": 0.2},
    )
    path = tmp_path / "truth.npy"
    np.save(path, truth, allow_pickle=False)
    imported = sdk.import_temporal_signal_reference(metadata, path)
    assert imported["independence"] == "declared_not_independently_authenticated"
    assert imported["physical_validation"] == "not_established"
    assert sdk.temporal_signal_reference(imported["id"])["id"] == imported["id"]
    assert sdk.temporal_signal_references()[0]["id"] == imported["id"]
    test = sdk.post("tests", sdk.temporal_signal_test_template(imported["id"]))
    spec = _spec(run, model, test, (frames, rows, columns))
    with pytest.raises(ValueError):
        comparison.validate_specification({k: v for k, v in spec.items()
                                           if k != "temporal_test_id"})
    planned = sdk.plan_retained_isp_comparison(spec)
    bad = deepcopy(spec)
    bad["roi"]["column_start"] = 1
    with pytest.raises(httpx.HTTPStatusError):
        sdk.plan_retained_isp_comparison(bad)
    mutated = deepcopy(spec)
    mutated["temporal_constraints"][0]["value"] = 0.1
    with pytest.raises(httpx.HTTPStatusError):
        sdk.submit_retained_isp_comparison({"frozen_request": {"specification": mutated},
                                            "plan_hash": planned["plan_hash"]})

    original_processor = processor.process_raw
    calls = []

    def raw_only(*args, **kwargs):
        calls.append((args, kwargs))
        assert len(args) == 3
        assert set(kwargs) == {"geometry", "frame_records", "events"}
        assert args[0].dtype.kind in "iu"
        return original_processor(*args, **kwargs)

    monkeypatch.setattr(processor, "process_raw", raw_only)
    queued = sdk.submit_retained_isp_comparison(planned)
    result = finish(service, queued)
    assert result["status"] == "succeeded", result.get("error")
    assert len(calls) == 3
    summary = result["result"]["summary"]
    assert summary["schema"] == "camera_retained_code_comparison_result_v2"
    assert summary["temporal_validation"]["test_id"] == test["id"]
    assert summary["temporal_validation"]["processor_truth_arrays_used"] is False
    assert summary["ranking"]["selected_candidate_id"] is None
    assert summary["baseline"]["temporal_metrics"]["support_status"] == "ok"
    assert all("temporal_metrics" in case for case in
               [summary["baseline"], *summary["candidates"]])
    assert all("ccm_code_rgb_frames" in case["arrays"] for case in
               [summary["baseline"], *summary["candidates"]])
    cold = PlatformService(service.registry.directory)
    assert comparison.get(cold, result["id"])["id"] == result["id"]
    altered_test = deepcopy(result)
    altered_test["temporal_test_snapshot"]["observation_specification"]["roi"]["column_start"] = 1
    with pytest.raises(IntegrityError):
        comparison.verify(service, altered_test)
    altered_clock = deepcopy(result)
    altered_clock["temporal_reference_snapshot"]["row_transfer_ns"][0][0] += 1
    with pytest.raises(IntegrityError):
        comparison.verify(service, altered_clock)
    bundle = sdk.retained_isp_comparison_bundle(result["id"])
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["comparison"]["temporal_test_snapshot"] == test
        assert manifest["comparison"]["temporal_reference_snapshot"] == imported
        artifact = imported["array_artifact"]
        saved = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
        assert hashlib.sha256(saved).hexdigest() == artifact["sha256"]
        assert sdk.http.get(artifact["url"]).content == saved

    archive_path = tmp_path / "temporal-reference-backup.zip"
    backup_state(service.registry.directory, archive_path)
    restored_path = tmp_path / "restored-platform"
    restore_state(archive_path, restored_path)
    restored = PlatformService(restored_path)
    assert reference.get_reference(restored, imported["id"])["id"] == imported["id"]

    # A different authored truth changes the frozen plan and actual metrics,
    # while all independently processed RAW candidate arrays remain byte-identical.
    second_metadata = deepcopy(metadata)
    second_metadata["source_uri"] = "synthetic://second-independent-step-v1"
    second_truth = np.full_like(truth, 0.3)
    second_truth[-1] = 0.7
    second_path = tmp_path / "second-truth.npy"
    np.save(second_path, second_truth, allow_pickle=False)
    second_reference = sdk.import_temporal_signal_reference(second_metadata, second_path)
    second_test = sdk.post("tests", sdk.temporal_signal_test_template(second_reference["id"]))
    second_spec = _spec(run, model, second_test, (frames, rows, columns))
    second_plan = sdk.plan_retained_isp_comparison(second_spec)
    assert second_plan["plan_hash"] != planned["plan_hash"]
    second_result = finish(service, sdk.submit_retained_isp_comparison(second_plan))
    assert second_result["status"] == "succeeded", second_result.get("error")
    assert len(calls) == 6
    second_summary = second_result["result"]["summary"]
    assert (second_summary["baseline"]["temporal_metrics"]["metrics"]
            != summary["baseline"]["temporal_metrics"]["metrics"])
    for first_case, second_case in zip(
        [summary["baseline"], *summary["candidates"]],
        [second_summary["baseline"], *second_summary["candidates"]], strict=True,
    ):
        assert first_case["arrays"] == second_case["arrays"]


def test_reference_import_refuses_shifted_roi_and_row_clock(workflow, tmp_path):
    service, sdk, run = acquisition(workflow)
    replay = payload(service, sdk, run, enabled=True)
    metadata = sdk.temporal_signal_reference_template(
        run["id"], "single", replay["isp_model_id"]
    )
    frames, rows, columns = metadata["source_binding"]["raw_shape"]
    truth = np.full((frames, rows, columns, 3), 0.2, dtype=np.float64)
    truth[-1] = 0.8
    path = tmp_path / "truth.npy"
    np.save(path, truth, allow_pickle=False)
    with pytest.raises(httpx.HTTPStatusError):
        sdk.import_temporal_signal_reference(metadata, path)
    metadata.update(
        source_uri="synthetic://fixed-step-generator-v1",
        method_and_version="analytic-fixed-step-v1", license="test-fixture",
        description="Analytic authored step independent of ISP candidate outputs.",
        step={"onset_frame": frames - 1, "steady_start_frame": frames - 1,
              "onset_fraction": 0.5, "settling_fraction": 0.2},
    )
    tiny_change = deepcopy(metadata)
    tiny_change["minimum_reference_change"] = 1e-300
    with pytest.raises(httpx.HTTPStatusError):
        sdk.import_temporal_signal_reference(tiny_change, path)
    tiny_fraction = deepcopy(metadata)
    tiny_fraction["step"]["onset_fraction"] = 1e-300
    with pytest.raises(httpx.HTTPStatusError):
        sdk.import_temporal_signal_reference(tiny_fraction, path)
    false_window = deepcopy(metadata)
    false_window["step"] = {
        "onset_frame": 1, "steady_start_frame": 1,
        "onset_fraction": 0.5, "settling_fraction": 0.2,
    }
    with pytest.raises(httpx.HTTPStatusError):
        sdk.import_temporal_signal_reference(false_window, path)
    shifted = deepcopy(metadata)
    shifted["roi"]["column_start"] = 1
    # A changed ROI cannot be silently accepted by a Test tied to the original.
    imported = sdk.import_temporal_signal_reference(shifted, path)
    fixed = sdk.post("tests", sdk.temporal_signal_test_template(imported["id"]))
    assert fixed["observation_specification"]["roi"] == shifted["roi"]
    altered_clock = deepcopy(metadata)
    altered_clock["row_transfer_ns"][0][0] += 1
    with pytest.raises(httpx.HTTPStatusError):
        sdk.import_temporal_signal_reference(altered_clock, path)
    changed_binding = deepcopy(metadata)
    changed_binding["source_binding"]["selected_array_sha256"] = "0" * 64
    with pytest.raises(httpx.HTTPStatusError):
        sdk.import_temporal_signal_reference(changed_binding, path)
    phase_swapped = deepcopy(metadata)
    phase_swapped["source_binding"]["frame_ids"] = list(reversed(
        phase_swapped["source_binding"]["frame_ids"]
    ))
    with pytest.raises(httpx.HTTPStatusError):
        sdk.import_temporal_signal_reference(phase_swapped, path)
    changed_clock = deepcopy(metadata)
    changed_clock["source_binding"]["clock"] = {"wrong": "clock"}
    with pytest.raises(httpx.HTTPStatusError):
        sdk.import_temporal_signal_reference(changed_clock, path)
    bytes_path = service.registry.blob_path(imported["array_artifact"]["sha256"])
    bytes_path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        reference.get_reference(service, imported["id"])
