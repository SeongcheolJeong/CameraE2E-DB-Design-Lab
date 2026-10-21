"""The candidate sensitivity Job binds one immutable source and one fixed grid."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_dense_psf_sensitivity_job import (
    SCHEMA,
    _fixed_grid_shift_geometry,
    default_spec,
    specification,
)
from camera_platform.sdk import PlatformClient

WORKSPACE = Path(__file__).resolve().parents[2]


def _spec():
    spec = default_spec()
    roi = spec["registered_roi"]
    roi["lens_revision_id"] = "6c48e967-74b4-5901-8186-ba6fafe4797b"
    roi["sensor_revision"] = {"identifier": "DEP-2209-801", "run_id": "a" * 64}
    roi["active_mode_declaration_id"] = "active_mode_declaration-" + "b" * 32
    roi["accept_approximate_lens_radiometry"] = True
    roi["acknowledge_unverified_mode_and_uniform_sensor_response"] = True
    spec["candidate_run_id"] = "run-" + "c" * 32
    spec["candidate_asset_sha256"] = "d" * 64
    return spec


def test_fixed_grid_off_axis_shift_uses_requested_tangent_plane_fraction():
    edge_offset_deg = 14.047293808135992
    image_distance_m = 0.014
    geometry = _fixed_grid_shift_geometry(
        edge_offset_deg=edge_offset_deg,
        native_pitch_um=1.22,
        requested_fraction=0.25,
        image_distance_m=image_distance_m,
    )
    shift_um = image_distance_m * 1e6 * (
        math.tan(math.radians(
            edge_offset_deg + geometry["planned_angular_shift_deg"]
        )) - math.tan(math.radians(edge_offset_deg))
    )
    assert geometry["requested_tangent_plane_um"] == pytest.approx(0.305)
    assert shift_um == pytest.approx(0.305, abs=1e-9)
    assert geometry["distorted_local_edge_shift_qualified"] is False


def test_explicit_candidate_and_single_grid_schema():
    checked = specification(_spec())
    assert checked["schema"] == SCHEMA
    assert checked["fixed_grid_goal"]["target_fractions_native_nyquist"] == [0.25, 0.9]
    invalid = deepcopy(checked)
    invalid["candidate_asset_sha256"] = "e" * 63
    with pytest.raises(ValueError, match="SHA-256"):
        specification(invalid)
    invalid = deepcopy(checked)
    invalid["fixed_grid_goal"]["target_fractions_native_nyquist"] = [0.9, 1.0]
    with pytest.raises(ValueError, match="below native Nyquist"):
        specification(invalid)
    invalid = deepcopy(checked)
    invalid["registered_roi"]["capture"]["noise_flag"] = 2
    with pytest.raises(ValueError, match="noise_flag=0"):
        specification(invalid)
    invalid = deepcopy(checked)
    invalid["candidate_asset_path"] = "/private/tmp/unpinned.npz"
    with pytest.raises(ValueError, match="exact"):
        specification(invalid)


def test_sdk_template_is_available_without_candidate_asset(tmp_path: Path):
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        template = sdk.lens_sensor_dense_psf_sensitivity_template()
        assert template["specification"]["schema"] == SCHEMA
        assert template["specification"]["fixed_grid_goal"][
            "optical_tile_estimated_working_budget_bytes"
        ] == 512 * 1024**2
