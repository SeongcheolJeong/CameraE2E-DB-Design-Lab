"""Supplementary whole-chart serialization is lossless and changes no result."""

import gzip
import hashlib
import json
from copy import deepcopy

import pytest
from camera_platform.calibration_esfr_replay import write_replay_result
from camera_platform.esfr_report import _evidence_filename, _measurement_download

from camera_platform.registry import canonical, digest


def result_fixture():
    curve = {
        "frequency_cy_native_px": [0.0, 0.125, 0.25],
        "sfr": [1.0, 0.7357345599042863, 0.293832876177],
        "esf": [0.1, 0.3, 0.8],
        "lsf": [0.2, 0.5, 0.0],
        "esf_distance_native_px": [-1.0, 0.0, 1.0],
        "mtf50": 0.2,
        "status": "ok",
    }
    observations, comparisons = [], []
    for capture in range(6):
        role = "development" if capture < 3 else "validation"
        observations.append({"capture_id": str(capture), "role": role, "rois": []})
        for edge in range(112):
            identity = f"edge-{edge:03d}"
            observations[-1]["rois"].append(
                {"id": identity, "kind": "edge", "raw": {"Gr": deepcopy(curve)}}
            )
            comparisons.append(
                {
                    "capture_id": str(capture),
                    "roi_id": identity,
                    "role": role,
                    "raw": {"before_rmse": 0.2, "after_rmse": 0.002},
                    "curves": {
                        "raw": {k: deepcopy(curve) for k in ("measured", "before", "after")}
                    },
                }
            )
    profile = {"fields": {f"edge-{i:03d}": {"sigma_x_px": 1.4 + i / 1000} for i in range(112)}}
    return {
        "schema": "camera_esfr_study_v1",
        "observations": observations,
        "comparisons": comparisons,
        "patch_summary": [{"id": "gray-1", "value": 0.12345678901234567}],
        "candidate_profile": profile,
        "candidate_profile_sha256": digest(profile),
        "metrics": {"raw_sfr_rmse": {"before": 0.2, "after": 0.002, "count": 336}},
        "validation": {"status": "pass", "checks": [{"edge": i} for i in range(336)]},
        "edge_roi_count": 112,
        "unicode_provenance": "실측 대체 합성 · 모든 소수와 방향 보존",
    }


def test_all112_edges_336_holdouts_roundtrip_canonical_bytes_and_profile(tmp_path):
    source = result_fixture()
    original = deepcopy(source)
    compact = write_replay_result(source, tmp_path)
    descriptor = compact["full_result_artifact"]
    compressed = (tmp_path / descriptor["path"]).read_bytes()
    expanded = gzip.decompress(compressed)
    assert hashlib.sha256(compressed).hexdigest() == descriptor["sha256"]
    assert len(compressed) == descriptor["bytes"]
    assert hashlib.sha256(expanded).hexdigest() == descriptor["expanded_sha256"]
    assert len(expanded) == descriptor["expanded_bytes"]
    assert expanded == canonical(original).encode("utf-8")
    assert json.loads(expanded) == original == source
    assert compact["candidate_profile"] == original["candidate_profile"]
    assert compact["candidate_profile_sha256"] == digest(original["candidate_profile"])
    assert compact["metrics"] == original["metrics"]
    assert compact["validation"] == original["validation"]
    assert len(compact["observations"]) == 6
    assert all(len(o["rois"]) == 112 for o in compact["observations"])
    assert len(compact["comparisons"]) == 672
    assert sum(c["role"] == "validation" for c in compact["comparisons"]) == 336
    assert "measurement_details_file" not in compact
    assert json.loads((tmp_path / "result.json").read_text()) == compact


def test_gzip_is_deterministic_and_existing_evidence_cannot_be_overwritten(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    result = result_fixture()
    left = write_replay_result(result, first)
    right = write_replay_result(result, second)
    assert left["full_result_artifact"] == right["full_result_artifact"]
    assert (first / "full-result.json.gz").read_bytes() == (
        second / "full-result.json.gz"
    ).read_bytes()
    with pytest.raises(ValueError, match="new evidence paths"):
        write_replay_result(result, first)


def test_downloads_keep_standalone_defaults_and_bind_only_declared_replay_paths():
    assert _evidence_filename({}, "request_file") == "request.json"
    assert 'href="./measurement-details.json"' in _measurement_download({}, "전체 측정")
    replay = {"request_file": "../request.json", "measurement_details_file": "full-result.json.gz"}
    assert _evidence_filename(replay, "request_file") == "../request.json"
    assert 'href="./full-result.json.gz"' in _measurement_download(replay, "전체 측정")
    assert "JSON.GZ" in _measurement_download(replay, "전체 측정")
    assert "&lt;script&gt;" in _measurement_download(replay, "<script>")
    for unsafe in ("../../other.json", "javascript:alert(1)", '" onload="alert(1)', "/etc/passwd"):
        with pytest.raises(ValueError, match="evidence filename"):
            _evidence_filename({"measurement_details_file": unsafe}, "measurement_details_file")
