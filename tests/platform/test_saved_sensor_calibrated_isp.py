"""Declared native calibration applies only to its exact retained simulation RAW."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import isp_v2
from camera_platform import saved_sensor_calibrated_isp as calibrated
from camera_platform.saved_sensor_quality import evaluate_saved_sensor_quality
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from pyisetcam.dataset import _sha256_array

WORKSPACE = Path(__file__).resolve().parents[2]


def _json(path, payload):
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _numeric(size):
    return {
        "schema": "camera_isp_calibration_v1",
        "sites": ["R", "Gr", "Gb", "B"],
        "lsc": {
            "schema": "native_four_site_grid_v1",
            "row_nodes": [0, size[0] - 1],
            "column_nodes": [0, size[1] - 1],
            "gains": [
                [[1.0, 1.1, 1.2, 1.3], [1.1, 1.2, 1.3, 1.4]],
                [[1.2, 1.3, 1.4, 1.5], [1.3, 1.4, 1.5, 1.6]],
            ],
            "valid_mask": [[[True] * 4 for _ in range(2)] for _ in range(2)],
            "interpolation": "bilinear",
            "normalization": "declared_per_site_reference",
            "reference_roi": {
                "row_start": 0,
                "row_end": size[0],
                "column_start": 0,
                "column_end": size[1],
            },
        },
        "noise": {
            "schema": "native_four_site_noise_v1",
            "slope_dn": [0.5, 0.6, 0.7, 0.8],
            "intercept_dn2": [4.0, 5.0, 6.0, 7.0],
            "signal_interval_dn": [-64, 4000],
            "reference": "black_subtracted_dn_before_lsc",
            "variance_model": "slope_times_max_signal_zero_plus_intercept",
            "spatial_independence": "assumed",
        },
    }


def _record(binding, shape):
    return {
        "schema": calibrated.CALIBRATION_SCHEMA,
        "provenance": {
            "source_kind": "estimated",
            "source_uri": "fixture://independent-declared-calibration",
            "method_and_version": "authored_numeric_fixture:1",
            "source_dataset_id": "separate-calibration-fixture",
            "source_dataset_sha256": "a" * 64,
            "target_raw_used_for_fitting": False,
            "uncertainty": {
                "kind": "unknown",
                "reason": "Authored numbers, not fitted or measured.",
            },
        },
        "binding": binding,
        "payload": _numeric(shape),
    }


def _profile(calibration_sha):
    result = isp_v2.default_profile(f"sha256:{calibration_sha}")
    result["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
    result["raw_denoise"].update(
        mode="calibrated_same_site_filter_v1", strength=0.8, range_floor_dn2=3.0
    )
    return result


@pytest.fixture
def source(tmp_path, monkeypatch):
    raw = np.full((12, 12), 164, dtype=np.int32)
    raw[2, 2] = 300
    raw[:, 6:] += 80
    source_arrays = tmp_path / "quality.npz"
    np.savez_compressed(source_arrays, case_000_raw_dn_frame=raw)
    arrays_sha = hashlib.sha256(source_arrays.read_bytes()).hexdigest()
    revision = {
        "run_id": "fixed-run",
        "report_code": "TEST-1",
        "result_sha256": "1" * 64,
        "config_sha256": "2" * 64,
    }
    case = {
        "scene": "uniform",
        "luminance_cd_m2": 1000.0,
        "exposure_s": 0.004,
        "frame_seeds": [17, 18],
        "native_size": [12, 12],
        "readout": {"bit_depth": 12, "black_level_dn": 64.0},
    }
    report = {
        "schema": "camerae2e_saved_sensor_quality_v1",
        "status": "completed",
        "revision": revision,
        "cases": [case],
        "controls": {
            "seed": 7,
            "saved_fixed_pattern_seed": 9,
            "fnumber": 4.0,
            "focal_length_m": 0.004,
            "output_size": [8, 8],
            "readout_overrides": {},
        },
        "artifacts": {"npz_name": source_arrays.name, "npz_sha256": arrays_sha},
    }
    source_report = tmp_path / "quality.json"
    report_sha = _json(source_report, report)
    source_binding = {
        "source_quality_job_id": "quality-job-1",
        "source_report_sha256": report_sha,
        "source_arrays_sha256": arrays_sha,
        "source_case_index": 0,
        "source_raw_content_sha256": _sha256_array(raw),
        "saved_sensor_revision": revision,
        "source_frame_seed": 17,
    }
    checked = {
        "raw": raw,
        "source_binding": source_binding,
        "native_cfa": {
            "filter_names": ["R", "Gr", "Gb", "B"],
            "pattern": [[1, 2], [3, 4]],
            "pattern_index_base": 1,
        },
        "readout": case["readout"],
        "raw_format": {
            "cfa": "RGGB",
            "bit_depth": 12,
            "black_level_dn": 64.0,
            "response_type": "linear",
            "packing": "unpacked",
            "layout": "bayer",
            "hdr_streams": 1,
        },
        "geometry": {
            "kind": "native_sensor",
            "full_sensor_shape": [12, 12],
            "crop_origin": [0, 0],
        },
    }
    monkeypatch.setattr(
        calibrated,
        "inspect_saved_quality_isp_input",
        lambda *_args, **_kwargs: checked,
    )
    binding = calibrated._binding(report, case, checked)
    calibration_path = tmp_path / "calibration.json"
    record = _record(binding, raw.shape)
    calibration_sha = _json(calibration_path, record)
    kwargs = {
        "source_quality_job_id": "quality-job-1",
        "case_index": 0,
        "sensor_db_root": tmp_path / "db",
        "calibration_path": calibration_path,
        "calibration_sha256": calibration_sha,
        "profile": _profile(calibration_sha),
    }
    return source_report, source_arrays, raw, record, kwargs


def _evaluate(source, **changes):
    report, arrays, _, _, options = source
    return calibrated.evaluate_saved_sensor_calibrated_isp(
        report, arrays, **(options | changes)
    )


def test_pure_evaluator_preserves_exact_raw_and_separates_stage_diagnostics(source):
    report, arrays_path, raw, _, options = source
    before = raw.tobytes(order="C")
    evaluated = _evaluate(source)
    summary, arrays = evaluated["summary"], evaluated["arrays"]
    assert raw.tobytes(order="C") == before
    assert arrays["source_raw_dn_frame"].dtype == np.int32
    assert arrays["source_raw_dn_frame"].tobytes(order="C") == before
    assert summary["raw_content_sha256"] == _sha256_array(raw)
    assert summary["calibration"]["file_sha256"] == options["calibration_sha256"]
    assert summary["calibration"]["provenance_verification"].startswith("declared_only")
    assert summary["candidate_isp"]["stage_outputs"]["spatial_lsc"]["applied"]
    assert summary["candidate_isp"]["stage_outputs"]["raw_denoise"]["applied"]
    assert summary["before_after"]["default_vs_candidate"]["available"]
    assert summary["before_after"]["default_vs_candidate"][
        "mean_absolute_rgb_difference"
    ] > 0
    assert summary["before_after"]["default_vs_candidate"]["quality_improvement_claimed"] is False
    assert arrays["candidate_lsc_gain_map"].max() > 1
    assert arrays["candidate_noise_variance_hint_dn2"].max() > 0
    assert not (report.parent / "saved_sensor_calibrated_isp_report.json").exists()
    assert arrays_path.exists()
    json.dumps(summary, allow_nan=False)


def test_calibration_hash_profile_and_source_binding_fail_closed(source, tmp_path):
    _, _, _, record, options = source
    with pytest.raises(ValueError, match="calibration file differs|Calibration file differs"):
        _evaluate(source, calibration_sha256="0" * 64, profile=_profile("0" * 64))
    wrong_profile = _profile("0" * 64)
    with pytest.raises(ValueError, match="calibration_id"):
        _evaluate(source, profile=wrong_profile)
    for mutation in (
        lambda value: value["binding"]["readout"].update(bit_depth=10),
        lambda value: value["binding"]["native_cfa"].update(pattern=[[2, 1], [4, 3]]),
        lambda value: value["binding"]["acquisition"].update(exposure_s=0.01),
        lambda value: value["binding"]["geometry"].update(crop_origin=[2, 2]),
        lambda value: value["binding"]["saved_sensor_revision"].update(run_id="other"),
        lambda value: value["binding"].update(source_raw_content_sha256="sha256:" + "0" * 64),
    ):
        changed = copy.deepcopy(record)
        mutation(changed)
        path = tmp_path / "other-calibration.json"
        digest = _json(path, changed)
        with pytest.raises(ValueError, match="Calibration binding differs"):
            _evaluate(
                source,
                calibration_path=path,
                calibration_sha256=digest,
                profile=_profile(digest),
            )
    assert options["calibration_path"].exists()


def test_target_derived_claim_and_invalid_numeric_calibration_are_refused(source, tmp_path):
    _, _, _, original, _ = source
    changes = []
    target_claim = copy.deepcopy(original)
    target_claim["provenance"]["target_raw_used_for_fitting"] = True
    changes.append((target_claim, "target RAW"))
    target_dataset = copy.deepcopy(original)
    target_dataset["provenance"]["source_dataset_sha256"] = original["binding"][
        "source_raw_content_sha256"
    ].removeprefix("sha256:")
    changes.append((target_dataset, "Calibration dataset cannot"))
    bad_numeric = copy.deepcopy(original)
    bad_numeric["payload"]["noise"]["intercept_dn2"][0] = -1
    changes.append((bad_numeric, "finite number"))
    out_of_domain = copy.deepcopy(original)
    out_of_domain["payload"]["lsc"]["column_nodes"] = [1, 11]
    out_of_domain["payload"]["lsc"]["reference_roi"]["column_start"] = 1
    changes.append((out_of_domain, "LSC grid must cover"))
    for index, (record, reason) in enumerate(changes):
        path = tmp_path / f"calibration-{index}.json"
        digest = _json(path, record)
        with pytest.raises(ValueError, match=reason):
            _evaluate(
                source,
                calibration_path=path,
                calibration_sha256=digest,
                profile=_profile(digest),
            )


def test_duplicate_calibration_json_key_and_changed_source_report_are_refused(source, tmp_path):
    report, _, _, _, _ = source
    calibration_path = tmp_path / "duplicate.json"
    calibration_path.write_text('{"schema":"x","schema":"y"}')
    digest = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="Duplicate calibration JSON key"):
        _evaluate(
            source,
            calibration_path=calibration_path,
            calibration_sha256=digest,
            profile=_profile(digest),
        )
    payload = json.loads(report.read_text())
    payload["controls"]["fnumber"] = 8.0
    _json(report, payload)
    with pytest.raises(ValueError, match="Calibration binding differs"):
        _evaluate(source)


def test_writer_retains_raw_and_hashes_artifacts(source, tmp_path):
    report, arrays, raw, _, options = source
    output = tmp_path / "result"
    completed = calibrated.run_saved_sensor_calibrated_isp(
        report, arrays, **options, output_dir=output
    )
    assert completed["schema"] == calibrated.SCHEMA
    assert completed["artifact_sha256"]["npz"] == hashlib.sha256(
        Path(completed["artifacts"]["npz"]).read_bytes()
    ).hexdigest()
    with np.load(completed["artifacts"]["npz"], allow_pickle=False) as saved:
        assert saved["source_raw_dn_frame"].tobytes(order="C") == raw.tobytes(order="C")
    with pytest.raises(FileExistsError, match="not empty"):
        calibrated.run_saved_sensor_calibrated_isp(
            report, arrays, **options, output_dir=output
        )


def test_real_saved_quality_artifact_uses_exact_executed_mosaic(tmp_path):
    catalog = sensor_revision_catalog(root=WORKSPACE / "camerae2e_db", query="DEF-1801-802")
    selected = next((row for row in catalog if row["report_code"] == "DEF-1801-802"), None)
    if selected is None:
        pytest.skip("DEF-1801-802 saved sensor run is not installed")
    captured = evaluate_saved_sensor_quality(
        "DEF-1801-802",
        run_id=selected["run_id"],
        root=WORKSPACE / "camerae2e_db",
        workspace=WORKSPACE,
        output_dir=tmp_path / "quality",
        scenes=("uniform",),
        frames=2,
        output_size=(16, 16),
    )
    from camera_platform.saved_sensor_isp_replay import inspect_saved_quality_isp_input

    report_path = captured["artifacts"]["json"]
    arrays_path = captured["artifacts"]["npz"]
    checked = inspect_saved_quality_isp_input(
        report_path,
        arrays_path,
        source_quality_job_id="integration-quality-job",
        case_index=0,
        sensor_db_root=WORKSPACE / "camerae2e_db",
        profile=isp_v2.default_profile(),
    )
    report = json.loads(Path(report_path).read_text())
    binding = calibrated._binding(report, report["cases"][0], checked)
    record = _record(binding, checked["raw"].shape)
    calibration_path = tmp_path / "independent-declared-calibration.json"
    digest = _json(calibration_path, record)
    evaluated = calibrated.evaluate_saved_sensor_calibrated_isp(
        report_path,
        arrays_path,
        source_quality_job_id="integration-quality-job",
        case_index=0,
        sensor_db_root=WORKSPACE / "camerae2e_db",
        calibration_path=calibration_path,
        calibration_sha256=digest,
        profile=_profile(digest),
    )
    assert evaluated["arrays"]["source_raw_dn_frame"].shape == (20, 20)
    assert evaluated["summary"]["source_binding"]["saved_sensor_revision"][
        "run_id"
    ] == selected["run_id"]
    assert evaluated["summary"]["geometry_interpretation"].startswith("Full executed")
