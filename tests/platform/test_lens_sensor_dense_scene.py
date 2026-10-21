"""The denser common input scene is a retained RAW source, not an MTF ranking."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_catalog_bridge import geometric_lens_preview
from camera_platform.lens_sensor_raw_bridge import bounded_psf_resource_preflight
from camera_platform.lens_sensor_raw_job import (
    DENSE_COMMON_SCENE_SCHEMA,
    default_dense_common_scene_spec,
    execute,
    get,
    specification,
)
from camera_platform.retained_raw_dataset import load_retained_lens_sensor_raw

WORKSPACE = Path(__file__).resolve().parents[2]
LENS = "6c48e967-74b4-5901-8186-ba6fafe4797b"
SENSOR = ("DEF-2512-801", "360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601")


def _spec() -> dict:
    spec = default_dense_common_scene_spec()
    spec.update(
        lens_revision_id=LENS,
        sensor_revision={"identifier": SENSOR[0], "run_id": SENSOR[1]},
        sensor_size=[8, 8],
        accept_approximate_lens_radiometry=True,
    )
    return spec


def test_dense_common_scene_spec_is_explicit_and_resource_bounded():
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    spec = _spec()
    assert specification(spec)["schema"] == DENSE_COMMON_SCENE_SCHEMA
    with pytest.raises(ValueError, match="scene_samples_row_col"):
        specification({**spec, "scene_samples_row_col": [31, 128]})
    with pytest.raises(ValueError, match="scene_samples_row_col"):
        specification({**spec, "scene_samples_row_col": [128, 257]})
    with pytest.raises(ValueError, match="exactly"):
        specification({**spec, "unexpected": True})
    lens = geometric_lens_preview(LENS, catalog_root=WORKSPACE / "camerae2e_db")
    preflight = bounded_psf_resource_preflight(
        lens["model"], 0.61, [8, 8], scene_horizontal_fov_deg=0.12,
        scene_samples_row_col=[128, 128],
    )
    assert preflight["scene_samples_row"] == 128
    assert preflight["scene_samples_col"] == 128
    assert preflight["status"] == "within_development_budget"


def test_dense_common_scene_retains_actual_raw_and_reloads_exact_artifact(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()["token"]
        template = client.get(
            "/api/v1/lens-sensor-raw/template",
            params={"scope": "central_bounded_dense_common_scene"},
        )
        assert template.status_code == 200
        assert template.json()["specification"]["schema"] == DENSE_COMMON_SCENE_SCHEMA
        submitted = client.post("/api/v1/lens-sensor-raw", json=_spec())
        assert submitted.status_code == 202, submitted.text
        service = app.state.service
        job_id = submitted.json()["id"]
        assert service.registry.claim()["id"] == job_id
        service.registry.finish(job_id, result=execute(service, service.registry.get_run(job_id)))
        saved = get(service, job_id)
        assert saved["status"] == "succeeded"
        report = saved["result"]["summary"]
        grid = report["preview"]["scene"]["angular_grid_contract"]
        output = report["preview"]["capture"]["native_output_angular_grid"]
        assert grid["source_shape"] == [128, 128]
        assert report["preview"]["raw"]["shape"] == [8, 8]
        point = output["distortion_aware_point_center"]
        assert point["cross_sensor_common_output_grid"] == "not_established"
        minimum = point["min_adjacent_angular_step_deg_row_col_by_wavelength"]
        assert max(
            grid["center_angular_step_deg_row_col"][axis] / row[axis]
            for row in minimum for axis in (0, 1)
        ) < 1.0
        result_file = next(
            item for item in saved["artifacts"]
            if item["name"] == "lens-sensor-raw-preview-result.json"
        )
        raw_file = report["preview"]["raw_artifact"]
        loaded, raw, binding = load_retained_lens_sensor_raw(
            service.registry.blob_path(result_file["sha256"]),
            service.registry.blob_path(raw_file["sha256"]),
            source_lens_sensor_job_id=job_id,
        )
        assert loaded == report
        assert raw.tolist() == report["preview"]["raw"]["values"]
        assert binding["source_raw_artifact_sha256"] == raw_file["sha256"]
