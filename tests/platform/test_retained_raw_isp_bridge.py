"""Native CFA and exact-RAW boundaries for the two retained ISP input paths."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import isp_v2
from camera_platform.lens_catalog_bridge import lens_revision_catalog
from camera_platform.lens_sensor_raw_bridge import preview_lens_sensor_raw
from camera_platform.retained_raw_isp_bridge import (
    inspect_lens_sensor_isp_input,
    replay_lens_sensor_raw,
    validate_native_isp_source,
)
from camera_platform.saved_sensor_isp_replay import inspect_saved_quality_isp_input
from camera_platform.saved_sensor_quality import evaluate_saved_sensor_quality
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from pyisetcam.sensor_simulation_db import sensor_simulation_db_get

WORKSPACE = Path(__file__).resolve().parents[2]
DB_ROOT = WORKSPACE / "camerae2e_db"


def _selected_sensor(report_code):
    rows = sensor_revision_catalog(root=DB_ROOT, query=report_code)
    selected = next((row for row in rows if row["report_code"] == report_code), None)
    if selected is None:
        pytest.skip(f"{report_code} saved sensor run is not installed")
    return selected


def _lens_report(tmp_path, report_code):
    if not (DB_ROOT / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    catalog = lens_revision_catalog(catalog_root=DB_ROOT)
    lens = next((row for row in catalog["revisions"] if row["preview_status"] == "ready"), None)
    if lens is None:
        pytest.skip("No pinned geometric Lens DB revision is executable")
    sensor = _selected_sensor(report_code)
    preview = preview_lens_sensor_raw(
        lens["revision_id"],
        report_code,
        sensor_run_id=sensor["run_id"],
        root=DB_ROOT,
        workspace=WORKSPACE,
        scene_kind="uniform",
        sensor_size=(8, 8),
        noise_flag=2,
        seed=7,
        accept_approximate_lens_radiometry=True,
    )
    raw = np.asarray(preview["raw"]["values"], dtype=np.int32)
    stream = io.BytesIO()
    np.save(stream, raw, allow_pickle=False)
    raw_path = tmp_path / "source_raw.npy"
    raw_path.write_bytes(stream.getvalue())
    preview["raw_artifact"] = {
        "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "shape": list(raw.shape),
        "dtype": "int32",
    }
    summary = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v1",
        "status": "completed",
        "specification": {
            "accept_approximate_lens_radiometry": True,
            "seed": 7,
        },
        "source_identities": {
            "lens": preview["lens_identity"],
            "sensor": preview["sensor_revision"],
        },
        "preview": preview,
    }
    report_path = tmp_path / "source_report.json"
    report_path.write_text(json.dumps(summary, allow_nan=False), encoding="utf-8")
    return report_path, raw_path, raw


def test_exact_native_cfa_contract_accepts_only_two_supported_2x2_tiles():
    raw = np.full((8, 8), 100, dtype=np.int32)
    readout = {
        "bit_depth": 12, "black_level_dn": 64.0, "gain": 1.0,
        "full_well_e": 10000.0, "read_noise_e": 10.0,
        "dark_current_e_per_s": 10.0,
    }
    three = {
        "filter_names": ["red", "green", "blue"], "pattern": [[1, 2], [2, 3]],
        "pattern_index_base": 1, "native_size": [32, 32], "pixel_pitch_um": 0.61,
    }
    model = {
        "kind": "db_estimate_v1", "filter_names": three["filter_names"],
        "cfa_pattern": "rggb_bayer", "pixel_pitch_um": 0.61, "readout": readout,
    }
    checked = validate_native_isp_source(
        raw, native_cfa=three, model=model, captured_readout=readout,
        profile=isp_v2.default_profile(),
    )
    assert checked["native_cfa_contract"] == "three_channel_rggb_2x2"
    assert checked["raw_format"]["cfa"] == "RGGB"
    quad = dict(three, pattern=[[1, 1, 2, 2], [1, 1, 2, 2], [2, 2, 3, 3], [2, 2, 3, 3]])
    with pytest.raises(ValueError, match="native CFA"):
        validate_native_isp_source(
            raw, native_cfa=quad, model=model, captured_readout=readout,
            profile=isp_v2.default_profile(),
        )
    with pytest.raises(ValueError, match="readout differs"):
        validate_native_isp_source(
            raw, native_cfa=three, model=model,
            captured_readout=dict(readout, black_level_dn=63.0),
            profile=isp_v2.default_profile(),
        )


def test_real_lens_sensor_bayer_replay_preserves_raw_and_declares_approximation(tmp_path):
    report_path, raw_path, original = _lens_report(tmp_path, "DEF-2512-801")
    profile = isp_v2.default_profile()
    profile["white_balance"]["gains_rgb"] = [1.2, 1.0, 0.8]
    checked = inspect_lens_sensor_isp_input(
        report_path, raw_path, source_lens_sensor_job_id="lens-job-1",
        sensor_db_root=DB_ROOT, profile=profile,
    )
    assert checked["native_cfa_contract"] == "three_channel_rggb_2x2"
    report = replay_lens_sensor_raw(
        report_path, raw_path, source_lens_sensor_job_id="lens-job-1",
        sensor_db_root=DB_ROOT, profile=profile, output_dir=tmp_path / "replay",
    )
    assert report["raw_values_byte_equal"] is True
    assert report["source_binding"]["lens_radiometry"] == "assumed_unity_not_validated"
    assert report["before_after"]["default_vs_candidate"]["available"]
    with np.load(report["artifacts"]["npz"], allow_pickle=False) as arrays:
        assert arrays["source_raw_dn_frame"].dtype == np.int32
        assert arrays["source_raw_dn_frame"].tobytes(order="C") == original.tobytes(order="C")
        assert arrays["candidate_rgb"].shape == (8, 8, 3)
    assert Path(report["artifacts"]["json"]).is_file()


def test_real_quad_bayer_lens_source_is_refused_before_isp(tmp_path):
    report_path, raw_path, _ = _lens_report(tmp_path, "DEP-2209-801")
    with pytest.raises(ValueError, match="native CFA"):
        inspect_lens_sensor_isp_input(
            report_path, raw_path, source_lens_sensor_job_id="quad-lens-job",
            sensor_db_root=DB_ROOT, profile=isp_v2.default_profile(),
        )


def test_saved_quality_three_channel_bayer_uses_same_source_contract(tmp_path):
    selected = _selected_sensor("DEF-2512-801")
    report = evaluate_saved_sensor_quality(
        "DEF-2512-801", run_id=selected["run_id"], root=DB_ROOT,
        workspace=WORKSPACE, output_dir=tmp_path / "quality",
        scenes=("uniform",), frames=2, output_size=(16, 16),
    )
    checked = inspect_saved_quality_isp_input(
        report["artifacts"]["json"], report["artifacts"]["npz"],
        source_quality_job_id="quality-job-1", case_index=0,
        sensor_db_root=DB_ROOT, profile=isp_v2.default_profile(),
    )
    assert checked["native_cfa_contract"] == "three_channel_rggb_2x2"
    assert checked["raw_format"]["cfa"] == "RGGB"
    saved = sensor_simulation_db_get("DEF-2512-801", root=DB_ROOT, run_id=selected["run_id"])
    assert checked["native_cfa"] == saved["metadata"]["native_cfa"]
