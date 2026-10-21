"""Real RAW files, held-out acquisition isolation and training-only recovery."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import calibration_lab_engine as engine
from camera_platform.calibration_lab_contracts import campaign_spec, import_measurement
from camera_platform.calibration_synthetic_raw import generate_dataset


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    return generate_dataset(tmp_path_factory.mktemp("synthetic-raw") / "dataset", seed=1729)


def _observations(manifest, role):
    rows = []
    for entry in manifest["entries"]:
        if entry["role"] != role:
            continue
        path = Path(entry["path"])
        row = import_measurement(entry["metadata"], path.read_bytes(), path.name)
        row["id"] = entry["id"]
        rows.append(row)
    return rows


def _at(value, path):
    for component in path.split("."):
        value = value[int(component)] if isinstance(value, list) else value[component]
    return np.asarray(value, dtype=float)


def test_dataset_is_integer_raw_with_upload_metadata_and_separate_truth(dataset):
    assert campaign_spec(dataset["campaign"])["camera_id"] == "synthetic-raw-camera-1729"
    assert len(dataset["entries"]) == 18
    assert dataset["physical_validation"] == "not_established"
    protocol = json.loads(Path(dataset["artifact_paths"]["protocol"]).read_text())
    assert protocol["recovery_tolerances"] == dataset["recovery_tolerances"]
    assert "truth_profile" not in protocol
    hashes, sessions, acquisitions = set(), {}, set()
    for entry in dataset["entries"]:
        content = Path(entry["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        assert entry["sha256"] not in hashes
        hashes.add(entry["sha256"])
        image = np.load(entry["path"], allow_pickle=False)
        assert image.dtype == np.uint16 and list(image.shape) == entry["shape"]
        assert image.min() >= 0 and image.max() < 4095
        meta = entry["metadata"]
        assert meta["source_kind"] == "synthetic" and meta["observations"] == {}
        assert "truth" not in json.dumps(meta)
        assert json.loads(Path(entry["metadata_path"]).read_text()) == meta
        assert meta["raw_format"]["black_level_dn"] == 64
        assert (
            meta["raw_format"]["black_level_dn"]
            != dataset["truth_profile"]["electronics"]["black_level_dn"]
        )
        assert meta["acquisition_id"] not in acquisitions
        acquisitions.add(meta["acquisition_id"])
        sessions.setdefault(entry["role"], set()).add(meta["session_id"])
        assert Path(entry["preview"]).is_file()
    assert sessions["training"].isdisjoint(sessions["final_validation"])


def test_imported_role_frames_and_observations_do_not_overlap(dataset):
    # Only admission/lineage is checked; no final candidate acceptance is evaluated here.
    groups = {role: _observations(dataset, role) for role in ("training", "final_validation")}
    for rows in groups.values():
        assert {row["chart"] for row in rows} == set(engine.CHARTS)
        assert all(row["import_status"] == "ready" for row in rows)
        assert all(row["quality"]["not_fit_reasons"] == [] for row in rows)
    hashes = {
        role: {h for row in rows for h in row["source_frame_hashes"]}
        for role, rows in groups.items()
    }
    assert hashes["training"].isdisjoint(hashes["final_validation"])
    observations = {
        role: {json.dumps(row["observations"], sort_keys=True) for row in rows}
        for role, rows in groups.items()
    }
    assert observations["training"].isdisjoint(observations["final_validation"])


def test_training_raw_recovers_declared_parameters_without_truth_in_fitting(dataset):
    rows = _observations(dataset, "training")
    result = engine.fit_profile(dataset["campaign"]["baseline_profile"], rows)
    fitted = result["profile"]
    assert set(result["training_measurement_ids"]) == {row["id"] for row in rows}
    for path, limit in dataset["recovery_tolerances"].items():
        observed, expected = _at(fitted, path), _at(dataset["truth_profile"], path)
        error = float(np.max(np.abs(observed - expected)))
        allowance = limit.get("absolute", limit.get("max_absolute"))
        if allowance is None:
            allowance = float(np.max(np.abs(expected))) * limit["relative"]
        assert error <= allowance, (path, error, allowance)
    assert fitted["isp"] == dataset["campaign"]["baseline_profile"]["isp"]
    assert fitted["electronics"]["full_well_e"] == 10000
    assert any(
        row["chart"] == "texture" and row["status"] == "validation_only"
        for row in result["diagnostics"]
    )
    assert result["physical_validation"] == "not_established"


def test_identical_seed_reproduces_original_bytes_and_refuses_overwrite(dataset, tmp_path):
    other = generate_dataset(tmp_path / "reproduced", seed=1729)
    assert [entry["sha256"] for entry in dataset["entries"]] == [
        entry["sha256"] for entry in other["entries"]
    ]
    assert [entry["metadata"] for entry in dataset["entries"]] == [
        entry["metadata"] for entry in other["entries"]
    ]
    with pytest.raises(ValueError, match="empty directory"):
        generate_dataset(tmp_path / "reproduced", seed=1729)
    for invalid in (True, -1, 2**31, 1.5):
        with pytest.raises(ValueError, match="seed"):
            generate_dataset(tmp_path / "invalid", seed=invalid)
