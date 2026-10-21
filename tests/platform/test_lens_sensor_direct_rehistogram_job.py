"""The opt-in PSF derivation is bound to an explicit bounded optical grid."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_raw_bridge import preview_lens_sensor_raw
from camera_platform.lens_sensor_raw_job import (
    DIRECT_REHISTOGRAM_SCHEMA,
    default_direct_rehistogram_spec,
    specification,
)

WORKSPACE = Path(__file__).resolve().parents[2]
P0014_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"


def test_direct_psf_study_rejects_implicit_grid_or_silent_method_change():
    spec = default_direct_rehistogram_spec()
    spec.update(
        lens_revision_id=P0014_REVISION,
        sensor_revision={"identifier": "DEF-2512-801", "run_id": "saved-run"},
        accept_approximate_lens_radiometry=True,
    )
    assert specification(spec)["schema"] == DIRECT_REHISTOGRAM_SCHEMA
    with pytest.raises(ValueError, match="direct same-ray"):
        specification({**spec, "geometric_psf_sampling": "stored_interpolated"})
    with pytest.raises(ValueError, match="exactly"):
        specification({key: value for key, value in spec.items() if key != "scene_samples_row_col"})
    with pytest.raises(ValueError, match="explicit-FOV"):
        preview_lens_sensor_raw(
            P0014_REVISION, "DEF-2512-801", sensor_run_id="saved-run",
            root=WORKSPACE / "camerae2e_db", workspace=WORKSPACE,
            accept_approximate_lens_radiometry=True,
            psf_compute_mode="bounded_streaming",
            geometric_psf_sampling="direct_same_ray_rehistogram",
        )


def test_api_admits_bounded_diagnostic_and_refuses_oversize_before_queue(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = transport.get(
            "/api/v1/lens-sensor-raw/template",
            params={"scope": "central_bounded_direct_geometric_psf_diagnostic"},
        ).json()
        sensor = next(
            row for row in template["sensor_catalog"]
            if row["report_code"] == "DEF-2512-801"
        )
        spec = template["specification"]
        spec.update(
            lens_revision_id=P0014_REVISION,
            sensor_revision={"identifier": "DEF-2512-801", "run_id": sensor["run_id"]},
            sensor_size=[16, 16],
            accept_approximate_lens_radiometry=True,
        )
        queued = transport.post("/api/v1/lens-sensor-raw", json=spec)
        assert queued.status_code == 202, queued.text
        resource = queued.json()["resource_preflight"]
        assert resource["scope"] == "central_bounded_direct_same_ray_rehistogram_diagnostic"
        assert resource["status"] == "within_development_budget"
        assert resource["hard_memory_or_wall_time_bound"] is False
        assert queued.json()["specification"]["geometric_psf_sampling"] == (
            "direct_same_ray_rehistogram"
        )

        larger = {
            **spec,
            "scene_samples_row_col": [256, 256],
            "scene_horizontal_fov_deg": 0.06,
        }
        rejected = transport.post("/api/v1/lens-sensor-raw", json=larger)
        assert rejected.status_code == 422
        assert "resource_blocked" in rejected.text
        assert len(transport.get("/api/v1/lens-sensor-raw").json()) == 1
