"""An explicit common input scene does not imply a common native RAW grid."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_catalog_bridge import geometric_lens_preview
from camera_platform.lens_sensor_raw_bridge import (
    bounded_psf_resource_preflight,
    preview_lens_sensor_raw,
)
from camera_platform.lens_sensor_raw_job import (
    COMMON_SCENE_SCHEMA,
    default_bounded_spec,
    default_common_scene_spec,
    execute,
    get,
    specification,
)
from camera_platform.registry import IntegrityError

WORKSPACE = Path(__file__).resolve().parents[2]
LENSES = (
    "6c48e967-74b4-5901-8186-ba6fafe4797b",  # P0014
    "4daeca8f-5d81-57b0-8ae3-928f23de2b36",  # P0248
)
SENSORS = (
    ("DEF-2512-801", "360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601"),
    ("DEF-2412-804", "89a3d6955daa5cbd5208c301e5e78c53a7397489c4e4d707f4d3cfab4c6bbde6"),
)


def test_common_scene_contract_is_opt_in_and_preflights_actual_scene_sampling(
    tmp_path, monkeypatch,
):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    spec = default_common_scene_spec()
    spec.update(
        lens_revision_id=LENSES[0],
        sensor_revision={"identifier": SENSORS[0][0], "run_id": SENSORS[0][1]},
        sensor_size=[32, 32],
        accept_approximate_lens_radiometry=True,
    )
    assert specification(spec)["schema"] == COMMON_SCENE_SCHEMA
    with pytest.raises(ValueError, match="exactly"):
        specification({**default_bounded_spec(), "scene_horizontal_fov_deg": 0.12})
    with pytest.raises(ValueError, match="scene_horizontal_fov_deg"):
        specification({**spec, "scene_horizontal_fov_deg": float("nan")})

    lens = geometric_lens_preview(LENSES[0], catalog_root=WORKSPACE / "camerae2e_db")
    budget = bounded_psf_resource_preflight(
        lens["model"], 0.61, [32, 32], scene_horizontal_fov_deg=0.12
    )
    assert budget["status"] == "within_development_budget"
    assert budget["scene_fov_source"] == "explicit_common_input_scene"
    assert budget["scene_horizontal_fov_deg"] == 0.12
    expected_spacing = (
        2.0 * lens["model"]["effective_focal_length_mm"]
        * math.tan(math.radians(0.12) / 2.0) * 1000.0 / 64
    )
    assert budget["scene_samples_col"] == 64
    assert budget["scene_sample_spacing_um"] == pytest.approx(expected_spacing)
    with pytest.raises(ValueError, match="does not cover the native sensor crop"):
        bounded_psf_resource_preflight(
            lens["model"], 0.61, [32, 32], scene_horizontal_fov_deg=0.01
        )

    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = transport.get(
            "/api/v1/lens-sensor-raw/template",
            params={"scope": "central_bounded_common_scene"},
        ).json()
        assert template["specification"]["schema"] == COMMON_SCENE_SCHEMA
        assert template["resource_contract"]["common_scene_fov_fixes_input_only"] is True
        queued = transport.post("/api/v1/lens-sensor-raw", json=spec)
        assert queued.status_code == 202, queued.text
        assert queued.json()["resource_preflight"] == budget
        service = app.state.service
        job_id = queued.json()["id"]
        assert service.registry.claim()["id"] == job_id
        service.registry.finish(
            job_id, result=execute(service, service.registry.get_run(job_id))
        )
        saved = get(service, job_id)
        assert saved["status"] == "succeeded"
        assert saved["result"]["summary"]["preview"]["scene"]["horizontal_fov_deg"] == 0.12
        assert saved["result"]["summary"]["preview"]["raw"]["shape"] == [32, 32]
        forged = deepcopy(saved)
        forged_grid = forged["result"]["summary"]["preview"]["capture"][
            "native_output_angular_grid"
        ]
        forged_grid["horizontal_fov_deg"] *= 2
        monkeypatch.setattr(service.registry, "get_run", lambda _identity: forged)
        with pytest.raises(IntegrityError, match="common input angular scene evidence"):
            get(service, job_id)


def test_source_direct_two_by_two_shares_input_grid_but_has_distinct_native_output_grids():
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    scenes = []
    output_steps = []
    for lens_id in LENSES:
        for sensor_id, run_id in SENSORS:
            report = preview_lens_sensor_raw(
                lens_id,
                sensor_id,
                sensor_run_id=run_id,
                root=WORKSPACE / "camerae2e_db",
                workspace=WORKSPACE,
                sensor_size=(8, 8),
                scene_horizontal_fov_deg=0.12,
                psf_compute_mode="bounded_streaming",
                accept_approximate_lens_radiometry=True,
            )
            scenes.append(report["scene"]["angular_grid_contract"])
            grid = report["capture"]["native_output_angular_grid"]
            assert grid["status"] == "paraxial_center_only_full_distortion_map_not_established"
            assert "legacy scalar FOV and center step" in grid["status_note"]
            point_map = grid["distortion_aware_point_center"]
            assert point_map["status"] == "model_distortion_aware_pixel_centers_only"
            assert point_map["pixel_integration_and_psf_effective_grid"] == "not_established"
            assert point_map["input_scene_support"]["status"] == (
                "all_model_point_centers_within_source_samples"
            )
            assert len(point_map["coordinates_deg_row_col_by_wavelength"]) == 3
            assert len(point_map["coordinates_deg_row_col_by_wavelength"][0]) == 8
            assert grid["sensor_resize"] is False
            output_steps.append(grid["center_angular_step_deg_row_col"][1])
            assert report["raw"]["dtype"] == "int32"
            assert report["raw"]["shape"] == [8, 8]
    assert all(scene == scenes[0] for scene in scenes)
    assert scenes[0]["source_shape"] == [32, 32]
    assert scenes[0]["horizontal_fov_deg"] == 0.12
    assert scenes[0]["center_angular_step_deg_row_col"][1] == pytest.approx(
        math.degrees(math.atan(2.0 * math.tan(math.radians(0.12) / 2.0) / 32))
    )
    assert len(set(output_steps)) == 4
