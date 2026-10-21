"""A separate calibration declaration is pinned to one approximate Lens RAW."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import isp_v2
from camera_platform.lens_catalog_bridge import lens_revision_catalog
from camera_platform.lens_sensor_calibrated_isp import (
    CALIBRATION_SCHEMA,
    REUSABLE_CALIBRATION_SCHEMA,
    calibration_binding,
    evaluate_lens_sensor_calibrated_isp,
    reusable_calibration_binding,
    run_lens_sensor_calibrated_isp,
    validate_lens_calibration_bytes,
)
from camera_platform.lens_sensor_raw_bridge import preview_lens_sensor_raw
from camera_platform.retained_raw_isp_bridge import inspect_lens_sensor_isp_input
from camera_platform.sensor_db_bridge import sensor_revision_catalog

WORKSPACE = Path(__file__).resolve().parents[2]
DB_ROOT = WORKSPACE / "camerae2e_db"


def _write(path, document):
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _numeric(shape):
    rows, columns = shape
    return {
        "schema": "camera_isp_calibration_v1",
        "sites": ["R", "Gr", "Gb", "B"],
        "lsc": {
            "schema": "native_four_site_grid_v1",
            "row_nodes": [0, rows - 1],
            "column_nodes": [0, columns - 1],
            "gains": [
                [[1.0, 1.1, 1.1, 1.2], [1.1, 1.2, 1.2, 1.3]],
                [[1.2, 1.3, 1.3, 1.4], [1.3, 1.4, 1.4, 1.5]],
            ],
            "valid_mask": [[[True] * 4 for _ in range(2)] for _ in range(2)],
            "interpolation": "bilinear",
            "normalization": "declared_per_site_reference",
            "reference_roi": {
                "row_start": 0, "row_end": rows,
                "column_start": 0, "column_end": columns,
            },
        },
        "noise": {
            "schema": "native_four_site_noise_v1",
            "slope_dn": [0.5] * 4,
            "intercept_dn2": [4.0] * 4,
            "signal_interval_dn": [-64, 4000],
            "reference": "black_subtracted_dn_before_lsc",
            "variance_model": "slope_times_max_signal_zero_plus_intercept",
            "spatial_independence": "assumed",
        },
    }


def _record(binding):
    return {
        "schema": CALIBRATION_SCHEMA,
        "provenance": {
            "source_kind": "simulated",
            "source_uri": "fixture://independent-lens-calibration",
            "method_and_version": "authored_numeric_fixture:1",
            "source_dataset_id": "separate-flat-dark-series",
            "source_dataset_sha256": "a" * 64,
            "target_raw_used_for_fitting": False,
            "uncertainty": {
                "kind": "unknown",
                "reason": "Declared numeric fixture; independence not independently proven.",
            },
        },
        "binding": copy.deepcopy(binding),
        "payload": _numeric(binding["geometry"]["full_sensor_shape"]),
    }


def _profile(calibration_sha):
    profile = isp_v2.default_profile()
    profile["calibration_id"] = "sha256:" + calibration_sha
    profile["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
    profile["raw_denoise"].update(mode="calibrated_same_site_filter_v1", strength=0.8)
    return profile


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    if not (DB_ROOT / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens and Sensor DB are unavailable")
    lens = next(
        (row for row in lens_revision_catalog(catalog_root=DB_ROOT)["revisions"]
         if row["preview_status"] == "ready"), None,
    )
    sensor = next(
        (row for row in sensor_revision_catalog(root=DB_ROOT, query="DEF-2512-801")
         if row["report_code"] == "DEF-2512-801"), None,
    )
    if lens is None or sensor is None:
        pytest.skip("Required pinned 2x2 Bayer Lens/Sensor revisions are unavailable")
    preview = preview_lens_sensor_raw(
        lens["revision_id"], "DEF-2512-801", sensor_run_id=sensor["run_id"],
        root=DB_ROOT, workspace=WORKSPACE, scene_kind="uniform", sensor_size=(8, 8),
        noise_flag=2, seed=7, accept_approximate_lens_radiometry=True,
    )
    folder = tmp_path_factory.mktemp("lens-calibration-source")
    raw = np.asarray(preview["raw"]["values"], dtype=np.int32)
    binary = io.BytesIO()
    np.save(binary, raw, allow_pickle=False)
    raw_path = folder / "raw.npy"
    raw_path.write_bytes(binary.getvalue())
    preview["raw_artifact"] = {
        "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "shape": list(raw.shape), "dtype": "int32",
    }
    summary = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1",
        "status": "completed",
        "specification": {"accept_approximate_lens_radiometry": True, "seed": 7},
        "source_identities": {
            "lens": preview["lens_identity"], "sensor": preview["sensor_revision"],
        },
        "preview": preview,
    }
    report_path = folder / "source.json"
    _write(report_path, summary)
    checked = inspect_lens_sensor_isp_input(
        report_path, raw_path, source_lens_sensor_job_id="lens-target-job",
        sensor_db_root=DB_ROOT, profile=isp_v2.default_profile(),
    )
    binding = calibration_binding(summary, checked)
    return folder, report_path, raw_path, raw, binding


def _evaluate(source, *, record=None, profile=None, path=None):
    folder, report_path, raw_path, _, binding = source
    supplied = _record(binding) if record is None else record
    cal_path = folder / "calibration.json" if path is None else path
    sha = _write(cal_path, supplied)
    return evaluate_lens_sensor_calibrated_isp(
        report_path, raw_path, source_lens_sensor_job_id="lens-target-job",
        sensor_db_root=DB_ROOT, calibration_path=cal_path, calibration_sha256=sha,
        profile=_profile(sha) if profile is None else profile,
    )


def test_real_lens_raw_uses_separate_hash_bound_lsc_and_noise_without_reacquisition(source):
    _, _, _, raw, binding = source
    evaluated = _evaluate(source)
    summary, arrays = evaluated["summary"], evaluated["arrays"]
    assert summary["source_binding"] == binding
    assert summary["native_cfa_contract"] == "three_channel_rggb_2x2"
    assert summary["calibration"]["schema"] == CALIBRATION_SCHEMA
    assert summary["calibration"]["provenance_verification"].startswith("declared_only")
    assert summary["candidate_isp"]["stage_outputs"]["spatial_lsc"]["applied"]
    assert summary["candidate_isp"]["stage_outputs"]["raw_denoise"]["applied"]
    assert summary["before_after"]["default_vs_candidate"]["quality_improvement_claimed"] is False
    assert arrays["candidate_lsc_gain_map"].max() > 1
    assert arrays["candidate_noise_variance_hint_dn2"].max() > 0
    assert arrays["source_raw_dn_frame"].tobytes(order="C") == raw.tobytes(order="C")


@pytest.mark.parametrize("field,change", [
    ("lens_model", lambda b: b["lens_model"].update(f_number=8.0)),
    ("field_psf", lambda b: b["source_binding"].update(lens_field_psf_selection={"mode": "other"})),
    ("radiometry", lambda b: b["lens_model"].update(relative_illumination="validated")),
    ("cfa", lambda b: b["native_cfa"].update(pattern=[[2, 1], [3, 2]])),
    ("readout", lambda b: b["readout"].update(bit_depth=10)),
    ("geometry", lambda b: b["geometry"].update(crop_origin=[2, 2])),
    ("exposure", lambda b: b["capture"].update(exposure_s=0.01)),
    ("raw_hash", lambda b: b["source_binding"].update(
        source_raw_content_sha256="sha256:" + "0" * 64
    )),
])
def test_target_binding_mismatch_is_refused(source, field, change, tmp_path):
    record = _record(source[4])
    change(record["binding"])
    with pytest.raises(ValueError, match="Calibration binding differs|calibration lacks"):
        _evaluate(source, record=record, path=tmp_path / f"{field}.json")


def test_saved_quality_calibration_and_target_derived_source_are_refused(source, tmp_path):
    record = _record(source[4])
    old_schema = copy.deepcopy(record)
    old_schema["schema"] = "camerae2e_saved_sensor_isp_calibration_v1"
    content = json.dumps(old_schema).encode()
    with pytest.raises(ValueError, match="Saved-quality or other calibration"):
        validate_lens_calibration_bytes(content, hashlib.sha256(content).hexdigest())
    target_derived = copy.deepcopy(record)
    target_derived["provenance"]["target_raw_used_for_fitting"] = True
    with pytest.raises(ValueError, match="target Lens RAW"):
        _evaluate(source, record=target_derived, path=tmp_path / "target-derived.json")
    same_dataset = copy.deepcopy(record)
    same_dataset["provenance"]["source_dataset_sha256"] = source[4][
        "source_binding"
    ]["source_raw_artifact_sha256"]
    with pytest.raises(ValueError, match="cannot be the target Lens RAW"):
        _evaluate(source, record=same_dataset, path=tmp_path / "same-dataset.json")


def test_writer_hashes_evidence_and_retains_exact_raw(source, tmp_path):
    _, report_path, raw_path, raw, binding = source
    record = _record(binding)
    cal_path = tmp_path / "independent.json"
    sha = _write(cal_path, record)
    result = run_lens_sensor_calibrated_isp(
        report_path, raw_path, source_lens_sensor_job_id="lens-target-job",
        sensor_db_root=DB_ROOT, calibration_path=cal_path,
        calibration_sha256=sha, profile=_profile(sha), output_dir=tmp_path / "result",
    )
    assert result["artifact_sha256"]["json"] == hashlib.sha256(
        Path(result["artifacts"]["json"]).read_bytes()
    ).hexdigest()
    with np.load(result["artifacts"]["npz"], allow_pickle=False) as evidence:
        assert evidence["source_raw_dn_frame"].dtype == np.int32
        assert evidence["source_raw_dn_frame"].tobytes(order="C") == raw.tobytes(order="C")


def test_reusable_calibration_applies_to_distinct_scene_and_seed_without_target_fit(
    source, tmp_path,
):
    _, report_path, raw_path, first_raw, first_binding = source
    first_report = json.loads(report_path.read_text())
    first_checked = inspect_lens_sensor_isp_input(
        report_path, raw_path, source_lens_sensor_job_id="lens-target-job",
        sensor_db_root=DB_ROOT, profile=isp_v2.default_profile(),
    )
    reusable = reusable_calibration_binding(first_report, first_checked)
    assert "source_binding" not in reusable
    assert "scene" not in reusable
    assert "seed" not in reusable
    record = _record(first_binding)
    record["schema"] = REUSABLE_CALIBRATION_SCHEMA
    record["binding"] = reusable
    cal_path = tmp_path / "reusable.json"
    sha = _write(cal_path, record)

    lens = next(row for row in lens_revision_catalog(catalog_root=DB_ROOT)["revisions"]
                if row["preview_status"] == "ready")
    sensor = next(row for row in sensor_revision_catalog(root=DB_ROOT, query="DEF-2512-801")
                  if row["report_code"] == "DEF-2512-801")
    second_preview = preview_lens_sensor_raw(
        lens["revision_id"], "DEF-2512-801", sensor_run_id=sensor["run_id"],
        root=DB_ROOT, workspace=WORKSPACE, scene_kind="slanted_edge", sensor_size=(8, 8),
        noise_flag=2, seed=17, accept_approximate_lens_radiometry=True,
    )
    second_raw = np.asarray(second_preview["raw"]["values"], dtype=np.int32)
    second_raw_path = tmp_path / "second-raw.npy"
    with second_raw_path.open("wb") as stream:
        np.save(stream, second_raw, allow_pickle=False)
    second_preview["raw_artifact"] = {
        "sha256": hashlib.sha256(second_raw_path.read_bytes()).hexdigest(),
        "shape": list(second_raw.shape), "dtype": "int32",
    }
    second_report = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1", "status": "completed",
        "specification": {"accept_approximate_lens_radiometry": True, "seed": 17},
        "source_identities": {
            "lens": second_preview["lens_identity"],
            "sensor": second_preview["sensor_revision"],
        },
        "preview": second_preview,
    }
    second_report_path = tmp_path / "second-source.json"
    _write(second_report_path, second_report)
    second_checked = inspect_lens_sensor_isp_input(
        second_report_path, second_raw_path,
        source_lens_sensor_job_id="different-target-job",
        sensor_db_root=DB_ROOT, profile=isp_v2.default_profile(),
    )
    assert calibration_binding(second_report, second_checked) != first_binding
    assert reusable_calibration_binding(second_report, second_checked) == reusable
    assert second_checked["raw_content_sha256"] != first_checked["raw_content_sha256"]

    for report, raw_path_, job_id, original in (
        (report_path, raw_path, "lens-target-job", first_raw),
        (second_report_path, second_raw_path, "different-target-job", second_raw),
    ):
        evaluated = evaluate_lens_sensor_calibrated_isp(
            report, raw_path_, source_lens_sensor_job_id=job_id,
            sensor_db_root=DB_ROOT, calibration_path=cal_path,
            calibration_sha256=sha, profile=_profile(sha),
        )
        assert evaluated["summary"]["calibration"]["schema"] == REUSABLE_CALIBRATION_SCHEMA
        assert evaluated["summary"]["calibration"]["applicability_binding"] == reusable
        assert evaluated["summary"]["calibration"]["applicability_binding_sha256"]
        assert evaluated["arrays"]["source_raw_dn_frame"].tobytes() == original.tobytes()

    wrong = copy.deepcopy(record)
    wrong["binding"]["operating_point"]["exposure_s"] *= 2
    with pytest.raises(ValueError, match="Calibration binding differs"):
        _evaluate(source, record=wrong, path=tmp_path / "wrong-exposure.json")
    derived = copy.deepcopy(record)
    derived["provenance"]["source_dataset_sha256"] = second_checked[
        "source_binding"
    ]["source_raw_artifact_sha256"]
    derived_path = tmp_path / "target-derived.json"
    derived_sha = _write(derived_path, derived)
    with pytest.raises(ValueError, match="cannot be the target Lens RAW"):
        evaluate_lens_sensor_calibrated_isp(
            second_report_path, second_raw_path,
            source_lens_sensor_job_id="different-target-job",
            sensor_db_root=DB_ROOT, calibration_path=derived_path,
            calibration_sha256=derived_sha, profile=_profile(derived_sha),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b["scene_model"].update(illuminant_family="D50"),
        lambda b: b["operating_point"].update(exposure_s=True),
        lambda b: b["lens_model"].update(f_number=8.0),
        lambda b: b["native_cfa"].update(pattern=[[2, 1], [3, 2]]),
        lambda b: b["readout"].update(bit_depth=10),
    ],
)
def test_reusable_calibration_refuses_changed_model_or_operating_domain(source, tmp_path, mutation):
    _, report_path, _, _, binding = source
    checked = inspect_lens_sensor_isp_input(
        report_path, source[2], source_lens_sensor_job_id="lens-target-job",
        sensor_db_root=DB_ROOT, profile=isp_v2.default_profile(),
    )
    reusable = reusable_calibration_binding(json.loads(report_path.read_text()), checked)
    record = _record(binding)
    record["schema"] = REUSABLE_CALIBRATION_SCHEMA
    record["binding"] = reusable
    mutation(record["binding"])
    with pytest.raises(ValueError, match="binding differs|lacks an exact|lacks its approximate"):
        _evaluate(source, record=record, path=tmp_path / "wrong-domain.json")
