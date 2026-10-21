from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import camera_platform.quality_comparison_bridge as quality_bridge
import pyisetcam.assets as assets_module
from camera_platform.api import create_app
from camera_platform.quality_comparison_bridge import (
    compare_catalog_sensor_quality,
    default_spec,
    execute,
    get,
    specification,
    submit,
)
from camera_platform.service import PlatformService
from pyisetcam.assets import AssetStore

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sensor_ids": ["DEF-1807-801"]},
        {"sensor_ids": ["DEF-1807-801", "DEF-2011-804"], "frames": 9},
        {"sensor_ids": ["DEF-1807-801", "DEF-2011-804"], "output_size": (128, 128)},
        {"sensor_ids": ["DEF-1807-801", "DEF-2011-804"], "scenes": ["moving"]},
        {
            "sensor_ids": ["DEF-1807-801", "DEF-2011-804"],
            "readout_controls": {"full_well_e": 20000},
        },
    ],
)
def test_quality_comparison_rejects_unbounded_or_unsupported_scope(kwargs):
    with pytest.raises(ValueError):
        compare_catalog_sensor_quality(**kwargs)


def test_two_catalog_sensors_have_real_quality_metrics_and_preserved_artifacts(
    tmp_path, monkeypatch
):
    def forbid_download(*args, **kwargs):
        raise AssertionError("Quality comparison tried to fetch upstream assets")

    original_default = AssetStore.default.__func__
    resolved_defaults = []

    def observed_default(cls):
        resolved = original_default(cls)
        resolved_defaults.append(resolved)
        return resolved

    monkeypatch.setattr(assets_module, "_download", forbid_download)
    monkeypatch.setattr(assets_module, "ensure_upstream_snapshot", forbid_download)
    monkeypatch.setattr(AssetStore, "default", classmethod(observed_default))
    comparison = compare_catalog_sensor_quality(
        ["DEF-1807-801", "DEF-2011-804"],
        root=WORKSPACE / "camerae2e_db/fdtd_tcad/sensor_db",
        scenes=("uniform", "slanted_edge", "macbeth_d65"),
        output_size=(32, 32),
        frames=2,
        luminance_cd_m2=10,
        exposure_s=0.004,
        output_dir=tmp_path / "comparison",
        seed=7,
    )
    assert comparison["schema"] == "camerae2e_catalog_quality_comparison_v1"
    assert comparison["status"] == "completed"
    assert comparison["estimate_only"] is True
    assert comparison["source_boundary"]["saved_sensor_simulation_run_used"] is False
    assert comparison["upstream_assets"]["required_files_sha256"]
    expected_snapshot = (
        WORKSPACE / ".cache/upstream/isetcam" / assets_module.DEFAULT_UPSTREAM_SHA
    ).resolve()
    assert resolved_defaults
    assert all(item.snapshot_root.resolve() == expected_snapshot for item in resolved_defaults)
    assert "not_catalog_lens_revision" in comparison["source_boundary"]["lens_model"]
    assert len(comparison["selected_sensors"]) == 2
    assert all(item["model_provenance"] for item in comparison["selected_sensors"])
    assert comparison["sample_count"] == 12
    assert comparison["optical_solve_count"] == 6
    assert len(comparison["cases"]) == 6
    assert {item["scene"] for item in comparison["cases"]} == {
        "uniform",
        "slanted_edge",
        "macbeth_d65",
    }
    assert all(case["metrics"]["native_raw_snr_db"]["available"] for case in comparison["cases"])
    assert all(case["metrics"]["common_output_snr_db"]["available"] for case in comparison["cases"])
    edge = [
        case["metrics"]["edge_mtf50"]
        for case in comparison["cases"]
        if case["scene"] == "slanted_edge"
    ]
    assert len(edge) == 2 and any(item["available"] for item in edge)
    color = [
        case["metrics"]["macbeth_cie76_mean"]
        for case in comparison["cases"]
        if case["scene"] == "macbeth_d65"
    ]
    assert len(color) == 2
    assert all(not item.get("independent_color_accuracy", False) for item in color)
    assert comparison["method"]["color_evaluation"]["held_out_surfaces"] is False
    paths = {key: Path(value) for key, value in comparison["artifacts"].items()}
    assert set(paths) == {"json", "npz", "html"}
    assert all(path.is_file() and path.stat().st_size > 0 for path in paths.values())
    assert set(comparison["artifact_sha256"]) == set(paths)
    source = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert source["comparison_contract"] == comparison["comparison_contract"]
    with np.load(paths["npz"]) as arrays:
        for case in comparison["cases"]:
            raw = arrays[f"{case['array_prefix']}_raw_dn_frame"]
            assert raw.dtype == np.int32
            assert np.any(raw > 0)


def test_quality_comparison_does_not_overwrite_previous_evidence(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep.txt").write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        compare_catalog_sensor_quality(
            ["DEF-1807-801", "DEF-2011-804"],
            output_dir=output,
        )
    assert (output / "keep.txt").read_text(encoding="utf-8") == "existing"


def test_worker_job_freezes_source_and_preserves_quality_evidence(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = default_spec()
    spec.update(
        sensor_ids=["DEF-1807-801", "DEF-2011-804"],
        scenes=["uniform"],
        frames=2,
        output_size=[16, 16],
        acknowledge_simulation_limits=True,
    )
    assert specification(spec)["sensor_ids"] == spec["sensor_ids"]
    job = submit(service, spec)
    assert job["status"] == "queued"
    assert len(job["source_identities"]) == 2
    assert job["upstream_assets"]["required_files_sha256"]
    assert service.registry.claim()["id"] == job["id"]
    result = execute(service, job)
    assert result["summary"]["sample_count"] == 4
    assert len(result["artifacts"]) == 4
    finished = service.registry.finish(job["id"], result=result)
    assert finished["status"] == "succeeded"
    checked = get(service, job["id"])
    assert checked["result"]["summary"] == result["summary"]
    assert all(
        service.registry.blob_path(artifact["sha256"]).is_file()
        for artifact in checked["artifacts"]
    )
    source_report = next(
        artifact
        for artifact in checked["artifacts"]
        if artifact["name"] == "catalog-quality-source-report.json"
    )
    source = json.loads(service.registry.blob_path(source_report["sha256"]).read_text())
    assert source["artifacts"]["storage"] == "platform_registry_blobs"
    assert "/private/tmp/" not in json.dumps(source["artifacts"])


def test_missing_offline_workspace_assets_fail_before_simulation(tmp_path, monkeypatch):
    def forbid_download(*args, **kwargs):
        raise AssertionError("No upstream download is permitted")

    monkeypatch.setattr(AssetStore, "default", forbid_download)
    monkeypatch.setattr(quality_bridge, "camerae2e_compare_sensors", forbid_download)
    with pytest.raises(ValueError, match="Pinned upstream ISETCam assets are unavailable"):
        compare_catalog_sensor_quality(
            ["DEF-1807-801", "DEF-2011-804"],
            workspace=tmp_path / "missing-workspace",
            scenes=("uniform",),
            output_size=(16, 16),
            frames=2,
        )


def test_missing_catalog_sensor_is_an_explicit_api_422(tmp_path):
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()["token"]
        spec = default_spec()
        spec.update(
            sensor_ids=["missing-sensor", "DEF-2011-804"],
            acknowledge_simulation_limits=True,
        )
        response = client.post("/api/v1/catalog-quality-comparisons", json=spec)
        assert response.status_code == 422
        assert "Catalog sensor source unavailable" in response.json()["error"]


def test_missing_catalog_files_are_an_explicit_api_422(tmp_path, monkeypatch):
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    monkeypatch.setattr(quality_bridge, "_catalog_root", lambda service: tmp_path / "missing-db")
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()["token"]
        spec = default_spec()
        spec.update(
            sensor_ids=["DEF-1807-801", "DEF-2011-804"],
            acknowledge_simulation_limits=True,
        )
        response = client.post("/api/v1/catalog-quality-comparisons", json=spec)
        assert response.status_code == 422
        assert "Catalog sensor source unavailable" in response.json()["error"]
        template = client.get("/api/v1/catalog-quality-comparisons/template")
        assert template.status_code == 422
