"""Full active-boundary geometry is distinct from a saved RAW crop or MTF."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_fov_feasibility import (
    MODE_SCHEMA,
    evaluate_full_sensor_fov,
    inspect_db_full_sensor_fov,
)
from camera_platform.sdk import PlatformClient

WORKSPACE = Path(__file__).resolve().parents[2]


def _inputs():
    preview = {
        "identity": {"asset_sha256": "asset", "prescription_sha256": "prescription"},
        "model": {
            "response_kind": "geometric_ray_histogram_psf",
            "object_distance_m": "infinity",
            "effective_focal_length_mm": 10.0,
            "max_fov_deg": 60.0,
            "field_samples_mm": [0.0, 2.0, 5.0],
        },
    }
    inspection = {
        "executable": True,
        "revision": {
            "run_id": "run-1", "report_code": "TEST-1", "result_sha256": "saved-result"
        },
        # A 32 x 32 saved run does not define product active rows/columns.
        "native_response": {"pixel_pitch_um": 5.0, "native_cfa": {"native_size": [32, 32]}},
    }
    raytrace = {
        "physics_policy": "strict", "contract_version": 2, "psf_kind": "geometric",
        "diffraction_included": False, "asset_sha256": "asset",
        "source_prescription_sha256": "prescription",
        "geometry": {
            "field_coordinate": "ideal_image_height",
            "field_height_mm": [0.0, 2.0, 5.0],
            "wavelength_nm": [450.0, 550.0],
            "function": [[0.0, 0.0], [1.8, 2.0], [4.5, 5.0]],
        },
        "psf": {
            "field_coordinate": "ideal_image_height",
            "field_height_mm": [0.0, 2.0, 5.0],
            "wavelength_nm": [450.0, 550.0],
        },
        "relative_illumination": {
            "field_coordinate": "ideal_image_height",
            "field_height_mm": [0.0, 2.0, 5.0],
            "wavelength_nm": [450.0, 550.0],
        },
    }
    mode = {
        "schema": MODE_SCHEMA, "mode_id": "declared-mode", "sensor_run_id": "run-1",
        "sensor_report_code": "TEST-1", "sensor_result_sha256": "saved-result",
        "active_rows": 1000, "active_cols": 1200, "pixel_pitch_um": 5.0,
        "sensor_center_offset_from_optical_axis_um_row_col": [0.0, 0.0],
        "sensor_plane": "perpendicular_to_optical_axis",
        "geometry_source": {
            "kind": "engineering_declaration", "reference": "unverified test declaration"
        },
    }
    return preview, inspection, raytrace, mode


def test_uses_outer_boundary_and_distortion_inverse_at_each_wavelength():
    preview, inspection, raytrace, mode = _inputs()
    result = evaluate_full_sensor_fov(preview, inspection, raytrace, mode)
    assert result["status"] == "within_saved_model_support"
    assert result["product_qualification_allowed"] is False
    assert result["image_quality_comparison_allowed"] is False
    blue = result["wavelengths"][0]["active_boundary_corners"][3]
    green = result["wavelengths"][1]["active_boundary_corners"][3]
    outer_radius = math.hypot(1000 * 5 / 2000, 1200 * 5 / 2000)
    last_pixel_center_radius = math.hypot(999 * 5 / 2000, 1199 * 5 / 2000)
    assert blue["distorted_sensor_radius_mm"] == pytest.approx(outer_radius)
    assert blue["distorted_sensor_radius_mm"] > last_pixel_center_radius
    expected_ideal_blue = np.interp(outer_radius, [1.8, 4.5], [2.0, 5.0])
    assert blue["ideal_image_radius_mm"] == pytest.approx(expected_ideal_blue)
    assert green["ideal_image_radius_mm"] == pytest.approx(outer_radius)
    assert blue["pinhole_equivalent_half_angle_deg"] > green[
        "pinhole_equivalent_half_angle_deg"
    ]


def test_one_wavelength_outside_geometry_refuses_whole_active_area():
    preview, inspection, raytrace, mode = _inputs()
    mode["active_cols"] = 1600
    result = evaluate_full_sensor_fov(preview, inspection, raytrace, mode)
    assert result["status"] == "outside_saved_model_support"
    assert result["wavelengths"][0]["within_saved_model_support"] is False
    assert result["wavelengths"][1]["within_saved_model_support"] is True
    assert result["wavelengths"][0]["active_boundary_corners"][0][
        "pinhole_equivalent_angle_deg_row_col"
    ] is None


def test_offset_can_make_one_corner_leave_the_saved_field():
    preview, inspection, raytrace, mode = _inputs()
    mode["sensor_center_offset_from_optical_axis_um_row_col"] = [1000.0, 0.0]
    result = evaluate_full_sensor_fov(preview, inspection, raytrace, mode)
    blue = result["wavelengths"][0]["active_boundary_corners"]
    assert result["status"] == "outside_saved_model_support"
    assert blue[0]["within_saved_model_support"] is True
    assert blue[2]["within_saved_model_support"] is False


def test_saved_crop_and_megapixels_are_never_inferred_as_active_mode():
    preview, inspection, raytrace, mode = _inputs()
    inspection["native_response"]["resolution_mp"] = 50.0
    result = evaluate_full_sensor_fov(preview, inspection, raytrace, None)
    assert result["status"] == "not_evaluable"
    assert result["reason"] == "full_sensor_active_mode_geometry_missing"
    assert result["active_mode"] is None
    assert "wavelengths" not in result
    with pytest.raises(ValueError, match="complete full-sensor"):
        evaluate_full_sensor_fov(preview, inspection, raytrace, {"active_rows": 32})


@pytest.mark.parametrize("change, error", [
    ({"sensor_run_id": "other"}, "bind to the selected"),
    ({"pixel_pitch_um": 4.9}, "pitch differs"),
    ({"sensor_plane": "tilted"}, "Tilted"),
    ({"sensor_center_offset_from_optical_axis_um_row_col": None}, "offsets"),
    ({"geometry_source": {"kind": "engineering_declaration", "reference": ""}}, "source"),
])
def test_active_mode_requires_pinned_sensor_and_explicit_mount(change, error):
    preview, inspection, raytrace, mode = _inputs()
    mode.update(change)
    with pytest.raises(ValueError, match=error):
        evaluate_full_sensor_fov(preview, inspection, raytrace, mode)


def test_invalid_or_missing_lens_geometry_is_not_extrapolated():
    preview, inspection, raytrace, mode = _inputs()
    changed = deepcopy(raytrace)
    changed["geometry"]["function"][2][0] = 1.0
    with pytest.raises(ValueError, match="strictly monotone"):
        evaluate_full_sensor_fov(preview, inspection, changed, mode)
    changed = deepcopy(raytrace)
    changed["psf"]["field_coordinate"] = "distorted_image_height"
    with pytest.raises(ValueError, match="ideal_image_height"):
        evaluate_full_sensor_fov(preview, inspection, changed, mode)


def test_real_db_pair_cannot_claim_full_active_fov_from_saved_32_crop():
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    result = inspect_db_full_sensor_fov(
        "6c48e967-74b4-5901-8186-ba6fafe4797b",
        "DEF-2512-801",
        sensor_run_id="360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601",
        root=WORKSPACE / "camerae2e_db",
    )
    assert result["status"] == "not_evaluable"
    assert result["active_mode"] is None
    assert result["product_qualification_allowed"] is False
    # This deliberately illustrative geometry exercises the pinned DB adapter;
    # it is not a claim that the catalog sensor has a 1000 x 1200 active mode.
    revision = result["sensor_revision"]
    mode = {
        "schema": MODE_SCHEMA, "mode_id": "illustrative-only",
        "sensor_run_id": revision["run_id"],
        "sensor_report_code": revision["report_code"],
        "sensor_result_sha256": revision["result_sha256"],
        "active_rows": 1000, "active_cols": 1200, "pixel_pitch_um": 0.61,
        "sensor_center_offset_from_optical_axis_um_row_col": [0.0, 0.0],
        "sensor_plane": "perpendicular_to_optical_axis",
        "geometry_source": {
            "kind": "engineering_declaration", "reference": "illustrative test, not product data"
        },
    }
    conditional = inspect_db_full_sensor_fov(
        "6c48e967-74b4-5901-8186-ba6fafe4797b",
        "DEF-2512-801",
        sensor_run_id=revision["run_id"],
        active_mode=mode,
        root=WORKSPACE / "camerae2e_db",
    )
    assert conditional["status"] == "within_saved_model_support"
    assert [row["wavelength_nm"] for row in conditional["wavelengths"]] == [
        450.0, 550.0, 650.0
    ]
    assert conditional["product_qualification_allowed"] is False


def test_read_only_fov_api_and_sdk_keep_missing_active_mode_inconclusive(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = transport
        result = sdk.inspect_lens_sensor_fov(
            "6c48e967-74b4-5901-8186-ba6fafe4797b",
            "DEF-2512-801",
            "360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601",
        )
        assert result["status"] == "not_evaluable"
        assert result["reason"] == "full_sensor_active_mode_geometry_missing"
        assert result["product_qualification_allowed"] is False
        rejected = transport.post(
            "/api/v1/lens-sensor-fov/inspect",
            json={"lens_revision_id": "lens", "sensor_identifier": "sensor",
                  "sensor_run_id": "run", "claimed_full_fov_deg": 75},
        )
        assert rejected.status_code == 422
