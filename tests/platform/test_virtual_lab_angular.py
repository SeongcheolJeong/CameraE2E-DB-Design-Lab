import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform.virtual_lab_angular import (
    build_response,
    cosine_power_response,
    interpolate_relative,
)


def test_cosine_model_normal_identity_and_broadcast_native_sites():
    response = cosine_power_response([450, 550, 650], power=4)
    theta = np.array([[0, 10], [20, 30]])
    result = interpolate_relative(response, theta, 0, 550)
    assert result.shape == (2, 2, 4)
    assert result == pytest.approx(np.repeat(np.cos(np.deg2rad(theta))[..., None] ** 4, 4, axis=-1))
    assert interpolate_relative(response, 0, 181, 550).shape == (4,)
    assert interpolate_relative(response, 0, 181, 550) == pytest.approx(np.ones(4))
    json.dumps(response, allow_nan=False)


def test_periodic_phi_and_wavelength_interpolation():
    response = cosine_power_response([450, 650], power=0)
    values = np.asarray(response["relative_response"])
    values[:, 1:, :, :] *= np.array([1, 0.9, 0.8, 0.9])[None, None, :, None]
    values[1, 1:] *= 0.8
    response["relative_response"] = values.tolist()
    assert interpolate_relative(response, 10, -45, 550) == pytest.approx(
        interpolate_relative(response, 10, 315, 550)
    )
    assert interpolate_relative(response, 10, 0, 550) == pytest.approx(np.full(4, 0.9))


@pytest.mark.parametrize(
    "theta, wave", [(-1, 550), (60.01, 550), (10, 449), (10, 651), (np.nan, 550)]
)
def test_queries_never_extrapolate_or_silently_fallback(theta, wave):
    response = cosine_power_response([450, 550, 650])
    with pytest.raises(ValueError, match="domain"):
        interpolate_relative(response, theta, 0, wave)


def test_single_wavelength_table_is_supported_at_its_declared_wave():
    response = cosine_power_response([550], power=0)
    assert interpolate_relative(response, np.array([0, 30, 60]), 45, 550) == pytest.approx(
        np.ones((3, 4))
    )


def test_nonidentity_normal_table_is_rejected():
    response = cosine_power_response([450, 550, 650])
    response["relative_response"][0][0][0][0] = 0.5
    with pytest.raises(ValueError, match="normal response"):
        interpolate_relative(response, 0, 0, 450)


@pytest.mark.parametrize("sensor_id, expected_pitch", [("S1", 3.45), ("S2", 2.1)])
def test_local_database_physical_ratio_preserves_saved_assets(sensor_id, expected_pitch):
    workspace = Path(__file__).resolve().parents[2]
    root = workspace / "camerae2e_db/fdtd_tcad/image_sensor_db"
    if not (root / "sensor_catalog.json").exists():
        pytest.skip("Local sensor DB required for this integration check")
    name = (
        "def_2509_801_omnivision_og05c10_cis"
        if sensor_id == "S1"
        else "def_2504_802_omnivision_ox12a10"
    )
    optical = root / "optical_qe_db/models" / (name + ".json")
    before = hashlib.sha256(optical.read_bytes()).hexdigest()
    response = build_response(workspace, sensor_id, [450, 550, 650])
    provenance = response["provenance"]
    assert provenance["pixel_pitch_um"] == expected_pitch
    assert provenance["database_modified"] is False
    assert hashlib.sha256(optical.read_bytes()).hexdigest() == before
    assert interpolate_relative(response, 0, 45, 550) == pytest.approx(np.ones(4))
    oblique = interpolate_relative(response, 40, 0, 550)
    assert 0 < oblique[0] < 1 and 0 < oblique[3] < 1
    assert oblique[1] == pytest.approx(oblique[2])
    if sensor_id == "S1":
        assert provenance["derived_geometry_um"]["cfa_thickness"] == 0.775
        assert provenance["derived_overlays"][0]["original"] == 3.6
    else:
        assert provenance["derived_overlays"][0]["original"] == "rggb_bayer"
        assert provenance["derived_overlays"][0]["derived"] == "rggb"
    assert "once only" in provenance["combination"]
    unsigned = {key: value for key, value in response.items() if key != "table_sha256"}
    assert response["table_sha256"] == hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    assert len(provenance["solver_sha256"]) == len(provenance["angular_adapter_sha256"]) == 64
    assert "in-memory" in provenance["source_basis"]
    json.dumps(response, allow_nan=False)
