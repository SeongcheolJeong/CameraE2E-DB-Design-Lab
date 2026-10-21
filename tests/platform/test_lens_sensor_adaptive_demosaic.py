"""A saved native RGGB Lens RAW can use pyisetcam's adaptive demosaic."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform.lens_sensor_adaptive_demosaic import (
    METHOD,
    evaluate_lens_sensor_adaptive_demosaic,
    run_lens_sensor_adaptive_demosaic,
)
from camera_platform.lens_sensor_raw_bridge import preview_lens_sensor_raw
from camera_platform.sensor_db_bridge import sensor_revision_catalog

WORKSPACE = Path(__file__).resolve().parents[2]
DB_ROOT = WORKSPACE / "camerae2e_db"
P0248_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    if not (DB_ROOT / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    sensor = next(
        row for row in sensor_revision_catalog(root=DB_ROOT)
        if row["report_code"] == "DEF-2512-801" and row["model"] == "db_estimate_v1"
    )
    preview = preview_lens_sensor_raw(
        P0248_REVISION, "DEF-2512-801", sensor_run_id=sensor["run_id"],
        root=DB_ROOT, workspace=WORKSPACE, scene_kind="slanted_edge",
        sensor_size=(16, 16), noise_flag=2, seed=23,
        accept_approximate_lens_radiometry=True,
    )
    folder = tmp_path_factory.mktemp("lens-adaptive-source")
    raw = np.asarray(preview["raw"]["values"], dtype=np.int32)
    binary = io.BytesIO()
    np.save(binary, raw, allow_pickle=False)
    raw_path = folder / "raw.npy"
    raw_path.write_bytes(binary.getvalue())
    preview["raw_artifact"] = {
        "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "shape": list(raw.shape), "dtype": "int32",
    }
    report = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1",
        "status": "completed",
        "specification": {"accept_approximate_lens_radiometry": True, "seed": 23},
        "source_identities": {
            "lens": preview["lens_identity"], "sensor": preview["sensor_revision"],
        },
        "preview": preview,
    }
    report_path = folder / "source.json"
    report_path.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    return folder, report_path, raw_path, raw, report


def _evaluate(source):
    _, report_path, raw_path, _, _ = source
    return evaluate_lens_sensor_adaptive_demosaic(
        report_path, raw_path,
        source_lens_sensor_job_id="saved-lens-raw-job",
        sensor_db_root=DB_ROOT,
    )


def test_adaptive_demosaic_uses_exact_saved_raw_and_explicit_readout(source):
    _, _, _, raw, source_report = source
    evaluated = _evaluate(source)
    summary, arrays = evaluated["summary"], evaluated["arrays"]
    assert summary["method"]["id"] == METHOD
    assert len(summary["method"]["pyisetcam_ip_source_sha256"]) == 64
    assert summary["native_cfa_contract"] == "three_channel_rggb_2x2"
    assert summary["native_cfa"]["pattern"] == [[1, 2], [2, 3]]
    assert summary["readout"] == source_report["preview"]["sensor_model"][
        "effective_readout"
    ]
    assert summary["calibration_provenance"]["black_level_dn"] == summary["readout"][
        "black_level_dn"
    ]
    assert summary["calibration_provenance"]["spatial_lsc"].startswith("bypass_")
    assert summary["calibration_provenance"]["raw_denoise"].startswith("bypass_")
    assert summary["before_after"]["valid_pixels"] > 0
    assert summary["before_after"]["quality_improvement_claimed"] is False
    assert arrays["source_raw_dn_frame"].tobytes(order="C") == raw.tobytes(order="C")
    assert arrays["reference_bilinear_sensor_rgb"].shape == (16, 16, 3)
    assert arrays["adaptive_laplacian_sensor_rgb"].shape == (16, 16, 3)
    assert arrays["comparison_valid_mask"].sum() > 0
    assert not arrays["comparison_valid_mask"][0].any()
    assert not arrays["comparison_valid_mask"][-1].any()
    assert not np.array_equal(
        arrays["reference_bilinear_sensor_rgb"],
        arrays["adaptive_laplacian_sensor_rgb"],
    )


def test_writer_hashes_evidence_and_preserves_original_raw(source, tmp_path):
    _, report_path, raw_path, raw, _ = source
    result = run_lens_sensor_adaptive_demosaic(
        report_path, raw_path, source_lens_sensor_job_id="saved-lens-raw-job",
        sensor_db_root=DB_ROOT, output_dir=tmp_path / "result",
    )
    for kind in ("json", "npz"):
        assert hashlib.sha256(Path(result["artifacts"][kind]).read_bytes()).hexdigest() == (
            result["artifact_sha256"][kind]
        )
    with np.load(result["artifacts"]["npz"], allow_pickle=False) as arrays:
        assert arrays["source_raw_dn_frame"].dtype == np.dtype("int32")
        assert arrays["source_raw_dn_frame"].tobytes(order="C") == raw.tobytes(order="C")
        assert arrays["adaptive_display_rgb"].shape == (16, 16, 3)


def test_changed_saved_readout_is_rejected_before_adaptive_processing(source, tmp_path):
    _, _, raw_path, _, report = source
    altered = copy.deepcopy(report)
    altered["preview"]["sensor_model"]["effective_readout"]["black_level_dn"] += 1
    report_path = tmp_path / "altered-source.json"
    report_path.write_text(json.dumps(altered, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(ValueError, match="Retained RAW readout differs"):
        evaluate_lens_sensor_adaptive_demosaic(
            report_path, raw_path, source_lens_sensor_job_id="saved-lens-raw-job",
            sensor_db_root=DB_ROOT,
        )
