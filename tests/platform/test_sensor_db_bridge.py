"""A saved sensor revision remains identifiable through a real RAW capture."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform.sensor_db_bridge import (
    compare_sensor_revisions,
    inspect_sensor_revision,
    run_sensor_revision,
    sensor_revision_catalog,
)
from pyisetcam.assets import AssetStore
from pyisetcam.image_sensor_db import image_sensor_db_config, image_sensor_db_get
from pyisetcam.physical_estimate import image_sensor_physical_model
from pyisetcam.sensor_readout_profiles import resolve_sensor_readout
from pyisetcam.sensor_simulation_db import (
    _connect,
    _digest,
    _store_result,
    sensor_simulation_db_path,
)

CODE = "DEF-2206-805"
WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture
def saved_revision(tmp_path):
    record = image_sensor_db_get(CODE)
    model = image_sensor_physical_model(CODE)
    profile = resolve_sensor_readout(CODE)
    result = {
        "metadata": {
            "status": "simulated",
            "model_kind": model["kind"],
            "config": image_sensor_db_config(CODE),
            "model_snapshot": model,
            "readout_profile": profile,
            "checks": {"numerical_passed": True},
            "native_cfa": {
                "filter_names": model["filter_names"],
                "pattern": [[1, 2], [3, 4]],
                "pattern_index_base": 1,
                "pixel_pitch_um": model["pixel_pitch_um"],
            },
        },
        "arrays": {"spectral_eqe": np.asarray(model["eqe"])},
    }
    db = sensor_simulation_db_path(tmp_path)
    manifest = {"source": "bridge test", "files": {}, "settings": {}}
    fingerprint = _digest(manifest)
    (db.parent / "inputs").mkdir(parents=True)
    (db.parent / "inputs" / f"{fingerprint}.json").write_text(json.dumps(manifest))
    with _connect(db, write=True) as conn:
        _store_result(conn, db, record, result, run_id="revision_one", input_digest=fingerprint)
    return tmp_path, record, result, db, fingerprint


def test_pinned_revision_reaches_raw_with_readout_source_labels(saved_revision):
    root, record, stored, _, _ = saved_revision
    catalog = sensor_revision_catalog(root=root)
    assert len(catalog) == 1
    assert catalog[0]["run_id"] == "revision_one"
    inspection = inspect_sensor_revision(CODE, root=root, run_id="revision_one")
    assert inspection["executable"] is True
    assert inspection["native_response"]["filter_names"] == ["R", "Gr", "Gb", "B"]
    assert (
        inspection["native_response"]["parameter_sources"]["collection_efficiency"][
            "classification"
        ]
        == "assumption"
    )
    assert (
        inspection["readout"]["saved_values"]["full_well_e"]["classification"]
        == (
            stored["metadata"]["readout_profile"]["provenance"]["fields"]["full_well_e"][
                "classification"
            ]
        )
    )
    assert inspection["r1_component_compatibility"]["directly_compatible"] is False

    result = run_sensor_revision(
        CODE,
        root=root,
        workspace=WORKSPACE,
        run_id="revision_one",
        scene={"type": "uniform ee", "args": [[8, 8]]},
        readout_overrides={"gain": 2.0},
        exposure_s=0.004,
        seed=7,
    )
    assert result["revision"]["sensor_id"] == record["sensor_id"]
    assert result["revision"]["run_id"] == "revision_one"
    assert result["raw"]["dtype"] == "int32"
    assert np.asarray(result["raw"]["values"]).shape == (8, 8)
    assert result["capture"]["saved_readout"]["gain"]["value"] == 1.0
    assert result["capture"]["readout_overrides"] == {"gain": 2.0}
    assert result["capture"]["effective_readout"]["gain"] == 2.0
    assert result["evidence"]["fdtd_attached"] is False
    assert result["evidence"]["tcad_attached"] is False
    json.dumps(result, allow_nan=False)


def test_saved_run_selection_and_active_lut_mismatch_fail_closed(saved_revision):
    root, record, result, db, fingerprint = saved_revision
    second = copy.deepcopy(result)
    second["metadata"]["config"]["scenario"]["fdtd"] = {
        "enabled": True,
        "lut": "/not/a/sensor-specific-lut.npz",
    }
    with _connect(db, write=True) as conn:
        _store_result(conn, db, record, second, run_id="revision_two", input_digest=fingerprint)
    assert sensor_revision_catalog(root=root)[0]["run_id"] == "revision_two"
    assert inspect_sensor_revision(CODE, root=root)["executable"] is False
    assert inspect_sensor_revision(CODE, root=root)["physics_binding"]["fdtd_attached"] is True
    with pytest.raises(ValueError, match="cannot execute"):
        run_sensor_revision(CODE, root=root)
    old = run_sensor_revision(CODE, root=root, workspace=WORKSPACE, run_id="revision_one")
    assert old["revision"]["run_id"] == "revision_one"
    assert old["evidence"]["fdtd_attached"] is False


def test_runtime_hash_mismatch_is_rejected_before_queueing(saved_revision):
    root, record, stored, db, fingerprint = saved_revision
    incompatible = copy.deepcopy(stored)
    incompatible["metadata"]["implementation_sha256"] = {"ip.py": "0" * 64}
    with _connect(db, write=True) as conn:
        _store_result(
            conn, db, record, incompatible,
            run_id="revision_incompatible", input_digest=fingerprint,
        )
    inspection = inspect_sensor_revision(CODE, root=root, run_id="revision_incompatible")
    assert inspection["status"] == "simulated"
    assert inspection["executable"] is False
    assert inspection["runtime_compatibility"] == "incompatible"
    assert "ip.py" in inspection["reason"]
    with pytest.raises(ValueError, match="Saved runtime incompatible"):
        run_sensor_revision(CODE, root=root, run_id="revision_incompatible")


def test_saved_fdtd_revision_is_not_advertised_as_frozen_execution(saved_revision):
    root, record, stored, db, fingerprint = saved_revision
    fdtd = copy.deepcopy(stored)
    fdtd["metadata"]["model_kind"] = "fdtd_estimate_v1"
    fdtd["metadata"]["model_snapshot"]["kind"] = "fdtd_estimate_v1"
    with _connect(db, write=True) as conn:
        _store_result(conn, db, record, fdtd, run_id="revision_fdtd", input_digest=fingerprint)
    inspection = inspect_sensor_revision(CODE, root=root, run_id="revision_fdtd")
    assert inspection["status"] == "simulated"
    assert inspection["executable"] is False
    assert "frozen-snapshot" in inspection["reason"]
    with pytest.raises(ValueError, match="cannot execute"):
        run_sensor_revision(CODE, root=root, run_id="revision_fdtd", workspace=WORKSPACE)


def test_scene_size_and_unknown_readout_rejected_before_execution(saved_revision):
    root, _, _, _, _ = saved_revision
    with pytest.raises(ValueError, match="scene size must equal sensor_size"):
        run_sensor_revision(CODE, root=root, scene={"type": "uniform ee", "args": [[16, 16]]})
    with pytest.raises(ValueError, match="six-field readout"):
        run_sensor_revision(CODE, root=root, readout_overrides={"hdr_mode": 1})


def test_two_saved_revisions_require_common_readout_and_keep_both_raws(saved_revision):
    root, record, stored, db, fingerprint = saved_revision
    second = copy.deepcopy(stored)
    second["metadata"]["model_snapshot"]["readout"]["gain"] = 2.0
    second["metadata"]["readout_profile"]["readout"]["gain"] = 2.0
    second["metadata"]["readout_profile"]["provenance"]["fields"]["gain"]["value"] = 2.0
    with _connect(db, write=True) as conn:
        _store_result(conn, db, record, second, run_id="revision_two", input_digest=fingerprint)
    revisions = [
        {"identifier": CODE, "run_id": "revision_one"},
        {"identifier": CODE, "run_id": "revision_two"},
    ]
    with pytest.raises(ValueError, match="Effective readout differs.*gain"):
        compare_sensor_revisions(revisions, root=root)
    comparison = compare_sensor_revisions(
        revisions,
        root=root,
        workspace=WORKSPACE,
        readout_overrides={"gain": 1.0},
        scene={"type": "uniform ee", "args": [[8, 8]]},
        exposure_s=0.004,
    )
    assert comparison["schema"] == "camerae2e_sensor_revision_comparison_v1"
    assert len(comparison["captures"]) == 2
    assert [item["revision"]["run_id"] for item in comparison["captures"]] == [
        "revision_one",
        "revision_two",
    ]
    assert all(np.asarray(item["raw"]["values"]).shape == (8, 8) for item in comparison["captures"])
    assert comparison["conditions"]["effective_readout"]["gain"] == 1.0
    assert comparison["source_differences"]["run_id"]["different"] is True
    assert comparison["source_differences"]["sensor_id"]["different"] is False
    assert comparison["differences"]["mean_raw_dn"]["second_minus_first"] == 0.0
    json.dumps(comparison, allow_nan=False)


def test_comparison_requires_exactly_two_distinct_revisions(saved_revision):
    root, _, _, _, _ = saved_revision
    choice = {"identifier": CODE, "run_id": "revision_one"}
    with pytest.raises(ValueError, match="exactly two"):
        compare_sensor_revisions([choice, choice, choice], root=root)
    with pytest.raises(ValueError, match="same saved revision"):
        compare_sensor_revisions([choice, choice], root=root)


def test_capture_uses_explicit_offline_snapshot_without_default_or_download(
    saved_revision, monkeypatch
):
    root, _, _, _, _ = saved_revision

    def forbidden(*args, **kwargs):
        raise AssertionError("implicit asset download is forbidden")

    original_init = AssetStore.__init__

    def guarded_init(self, snapshot_root=None, **kwargs):
        if snapshot_root is None:
            raise AssertionError("implicit AssetStore construction is forbidden")
        original_init(self, snapshot_root, **kwargs)

    monkeypatch.setattr(AssetStore, "__init__", guarded_init)
    monkeypatch.setattr("pyisetcam.assets.ensure_upstream_snapshot", forbidden)
    result = run_sensor_revision(CODE, root=root, workspace=WORKSPACE)
    assert result["raw"]["shape"] == [8, 8]
    assert result["evidence"]["upstream_assets"]["upstream_revision"]
    assert result["evidence"]["upstream_assets"]["required_files_sha256"]
    with pytest.raises(AssertionError, match="implicit AssetStore construction"):
        AssetStore.default()


def test_missing_explicit_workspace_fails_without_download(saved_revision, tmp_path, monkeypatch):
    root, _, _, _, _ = saved_revision

    def forbidden(*args, **kwargs):
        raise AssertionError("network fallback must not run")

    original_init = AssetStore.__init__

    def guarded_init(self, snapshot_root=None, **kwargs):
        if snapshot_root is None:
            raise AssertionError("implicit AssetStore construction is forbidden")
        original_init(self, snapshot_root, **kwargs)

    monkeypatch.setattr(AssetStore, "__init__", guarded_init)
    monkeypatch.setattr("pyisetcam.assets.ensure_upstream_snapshot", forbidden)
    empty_workspace = tmp_path / "empty"
    empty_workspace.mkdir()
    with pytest.raises(ValueError, match="Pinned upstream ISETCam assets are unavailable"):
        run_sensor_revision(CODE, root=root, workspace=empty_workspace)
