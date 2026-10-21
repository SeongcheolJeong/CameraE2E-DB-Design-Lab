"""Persisted results, restart semantics, corruption gates and frozen E2E use."""

import copy
import json
import shutil

import numpy as np
import pytest

from pyisetcam import (
    camera_get,
    camerae2e_run_scenario,
    image_sensor_db_config,
    sensor_simulation_db_config,
    sensor_simulation_db_get,
    sensor_simulation_db_path,
    sensor_simulation_db_search,
    sensor_simulation_db_summary,
    sensor_simulation_db_validate,
)
from pyisetcam.image_sensor_db import image_sensor_db_get
from pyisetcam.physical_estimate import image_sensor_physical_model
from pyisetcam.sensor_readout_profiles import resolve_sensor_readout
from pyisetcam.sensor_simulation_db import (
    _connect,
    _digest,
    _store_result,
    build_sensor_simulation_db,
)


@pytest.fixture(scope="module")
def sample():
    code = "DEF-2206-805"
    record = image_sensor_db_get(code)
    model = image_sensor_physical_model(code)
    metadata = {
        "status": "simulated",
        "model_kind": model["kind"],
        "config": image_sensor_db_config(code),
        "model_snapshot": model,
        "readout_profile": resolve_sensor_readout(code),
        "checks": {"numerical_passed": True},
        "simulation_settings": {},
    }
    return record, {
        "metadata": metadata,
        "arrays": {
            "wave_nm": np.array(model["wavelength_nm"]),
            "spectral_eqe": np.array(model["eqe"]),
        },
    }


def save(root, sample, run_id="run_one"):
    db = sensor_simulation_db_path(root)
    conn = _connect(db, write=True)
    manifest = {"source": "test fixture", "files": {}, "settings": {}}
    digest = _digest(manifest)
    (db.parent / "inputs").mkdir(exist_ok=True)
    (db.parent / "inputs" / f"{digest}.json").write_text(json.dumps(manifest))
    try:
        _store_result(conn, db, sample[0], sample[1], run_id=run_id, input_digest=digest)
    finally:
        conn.close()


def test_missing_readers_do_not_create_database(tmp_path):
    assert sensor_simulation_db_summary(root=tmp_path)["status"] == "missing"
    assert sensor_simulation_db_search(root=tmp_path) == []
    assert not list(tmp_path.iterdir())
    with pytest.raises(FileNotFoundError):
        sensor_simulation_db_get("DEF-2206-805", root=tmp_path)


def test_roundtrip_search_history_and_integrity(tmp_path, sample):
    save(tmp_path, sample)
    row = sensor_simulation_db_get("DEF-2206-805", root=tmp_path, load_arrays=True)
    np.testing.assert_array_equal(
        row["arrays"]["spectral_eqe"], sample[1]["arrays"]["spectral_eqe"]
    )
    assert "arrays" not in sensor_simulation_db_get(sample[0]["sensor_id"], root=tmp_path)
    assert len(sensor_simulation_db_search("imx662", root=tmp_path, status="simulated")) == 1
    assert not sensor_simulation_db_search("imx662", root=tmp_path, status="excluded")
    save(tmp_path, sample, "run_two")
    assert sensor_simulation_db_get("DEF-2206-805", root=tmp_path)["run_id"] == "run_two"
    assert (
        sensor_simulation_db_get("DEF-2206-805", root=tmp_path, run_id="run_one")["run_id"]
        == "run_one"
    )
    summary = sensor_simulation_db_summary(root=tmp_path)
    assert summary["counts"]["simulated"] == 1 and summary["history_count"] == 2
    assert sensor_simulation_db_validate(root=tmp_path)["status"] == "passed"


@pytest.mark.parametrize("filename", ["result.json", "arrays.npz"])
def test_corrupt_artifact_cannot_be_reused(tmp_path, sample, filename):
    save(tmp_path, sample)
    db = sensor_simulation_db_path(tmp_path)
    artifact = db.parent / "artifacts/DEF-2206-805/run_one" / filename
    artifact.write_bytes(b"corrupt data")
    with pytest.raises(ValueError, match="hash mismatch"):
        sensor_simulation_db_get("DEF-2206-805", root=tmp_path)
    assert sensor_simulation_db_validate(root=tmp_path)["status"] == "failed"


def test_exclusion_is_searchable_but_not_a_runtime_model(tmp_path, sample):
    record = sample[0]
    save(tmp_path, (record, {"metadata": {"status": "excluded", "reason": "UV unsupported"}}))
    assert sensor_simulation_db_get(record["code"], root=tmp_path, load_arrays=True)["arrays"] == {}
    with pytest.raises(ValueError, match="excluded.*UV"):
        sensor_simulation_db_config(record["code"], root=tmp_path)


def test_frozen_runtime_survives_relocation_without_source_catalog(tmp_path, sample, monkeypatch):
    original, moved = tmp_path / "old", tmp_path / "moved"
    save(original, sample)
    shutil.copytree(original, moved)
    shutil.rmtree(original)
    config = sensor_simulation_db_config("DEF-2206-805", root=moved)

    def unavailable(*args, **kwargs):
        raise AssertionError("Stored model must not rebuild from source catalog")

    monkeypatch.setattr("pyisetcam.image_sensor_db.image_sensor_db_get", unavailable)
    monkeypatch.setattr("pyisetcam.physical_estimate.image_sensor_physical_model", unavailable)
    monkeypatch.setattr("pyisetcam.sensor_readout_profiles.resolve_sensor_readout", unavailable)
    scenario = {
        **config["scenario"],
        "scene": {"type": "uniform ee", "args": [8]},
        "sensor": {"size": [8, 8], "noise_flag": 0, "integration_time": 0.004},
        "sensor_resize": False,
    }
    result = camerae2e_run_scenario(scenario, include_arrays=False)
    sensor = camera_get(result["camera"], "sensor")
    assert sensor.data["raw_dn"].dtype == np.int32
    assert sensor.metadata["sensor_simulation_db"]["run_id"] == "run_one"
    assert (
        sensor.fields["physical_estimate"]["geometry_um"]["cfa_thickness_by_channel"]["green"]
        == 0.5
    )


def test_frozen_descriptor_parameter_edits_are_not_silently_ignored(tmp_path, sample):
    save(tmp_path, sample)
    config = sensor_simulation_db_config("DEF-2206-805", root=tmp_path)
    config["scenario"]["sensor_model"]["collection_efficiency"] = 0.1
    with pytest.raises(ValueError, match="frozen"):
        camerae2e_run_scenario(config["scenario"], include_arrays=False)


def test_resume_and_new_settings_keep_previous_runs(tmp_path, sample, monkeypatch):
    calls = []

    def simulate(*args, **kwargs):
        calls.append(kwargs["settings"]["seed"])
        return copy.deepcopy(sample[1])

    monkeypatch.setattr("pyisetcam.sensor_simulation_batch.simulate_sensor_record", simulate)
    options = {"root": tmp_path, "sensor_ids": ["DEF-2206-805"]}
    first = build_sensor_simulation_db(**options)
    second = build_sensor_simulation_db(**options)
    third = build_sensor_simulation_db(**options, settings={"seed": 1})
    assert first["build_counts"]["computed"] == 1
    assert second["build_counts"]["reused"] == 1
    assert third["history_count"] == 2
    assert calls == [0, 1]
    assert len(list((sensor_simulation_db_path(tmp_path).parent / "inputs").glob("*.json"))) == 2
    json.dumps(third, allow_nan=False)


def test_changed_runtime_code_is_not_claimed_as_the_saved_implementation(tmp_path, sample):
    changed = copy.deepcopy(sample)
    changed[1]["metadata"]["implementation_sha256"] = {"physical_estimate.py": "0" * 64}
    save(tmp_path, changed)
    config = sensor_simulation_db_config("DEF-2206-805", root=tmp_path)
    with pytest.raises(ValueError, match="implementation changed"):
        camerae2e_run_scenario(config["scenario"], include_arrays=False)


def test_replacing_a_db_sensor_does_not_keep_its_run_id(sample):
    from pyisetcam.physical_estimate import apply_physical_estimate
    from pyisetcam.sensor import sensor_create

    sensor = sensor_create()
    sensor.metadata["sensor_simulation_db"] = {"run_id": "obsolete"}
    updated = apply_physical_estimate(
        sensor,
        sample[1]["metadata"]["model_snapshot"],
        {"directions": [{"theta_deg": 0.0, "phi_deg": 0.0, "weight": 1.0}]},
    )
    assert "sensor_simulation_db" not in updated.metadata
