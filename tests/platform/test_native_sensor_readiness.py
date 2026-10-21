"""Real saved-asset data request for the restricted p0014 optical candidate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform.native_sensor_readiness import native_sensor_data_request

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / "reports" / "camera_platform" / "integrated-workflows-20260928"


@pytest.fixture(scope="module", name="p0014_candidate")
def _p0014_candidate():
    psf_root = WORKFLOW / "dev29-p0014-restricted-band"
    verification = json.loads((psf_root / "verification.json").read_text())
    archive = psf_root / verification["arrays"]["path"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == verification["arrays"][
        "sha256"
    ]
    with np.load(archive, allow_pickle=False) as arrays:
        psf = {
            "schema": "field_intensity_psf_v1",
            "wavelength_nm": arrays["wavelength_nm"].tolist(),
            "values": arrays["psf_kernel"].tolist(),
            "sample_pitch_um": verification["spatial_support"]["pixel_pitch_um"],
            "normalization": "unit_sum",
            "centering": "array_center",
            "spatial_model": "field_grid",
            "boundary": "zero_outside_supplied_image",
            "sampling_model": "pixel_integrated_optical_impulse",
            "field_y_mm": arrays["field_y_mm"].tolist(),
            "field_x_mm": arrays["field_x_mm"].tolist(),
            "field_coordinates": "sensor_center_yx",
            "interpolation": "bilinear_source_position",
            "source": {
                "kind": "simulated",
                "description": (
                    "Saved p0014 restricted-band optical candidate; "
                    "not physical validation"
                ),
            },
        }
    directional_path = WORKFLOW / "dev30-planar-tail-directional-final" / "result.json"
    directional = json.loads(directional_path.read_text())[
        "model_candidate"
    ]["directional_response"]
    return psf, directional


def test_p0014_pitch_matched_saved_sensor_is_an_actionable_refusal(p0014_candidate):
    psf, directional = p0014_candidate
    request = native_sensor_data_request(
        "DEF-1803-805", psf=psf, directional_response=directional,
        repository_root=ROOT,
    )
    assert request["status"] == "source_data_required"
    assert request["lens_support"]["wavelength_nm"] == [550, 650]
    assert request["lens_support"]["pixel_pitch_um"] == 3
    assert request["lens_support"]["max_active_theta_deg"] == pytest.approx(
        8.0910189, rel=1e-5
    )
    assert "lens_sensor_pixel_pitch_mismatch" not in request["reason_codes"]
    assert request["geometry_prerequisite"]["status"] == "pitch_contract_matches"
    assert request["geometry_prerequisite"]["resolution_paths"] == []
    assert "saved_db_response_is_estimate_not_native_collected_eqe_grid" in request[
        "reason_codes"
    ]
    assert "readout_is_unmeasured_assumption" in request["reason_codes"]
    assert request["saved_sensor_response"]["filter_names"] == ["R", "Gr", "Gb", "B"]
    assert request["required_response"]["response_basis"].startswith(
        "independent_site_collected_electrons"
    )
    assert request["required_response"]["wavelength_nm"] == [550, 650]
    assert request["required_response"]["pixel_pitch_um"] == 3.0
    assert request["required_response"]["compatible_with_supplied_lens_psf_pitch"]
    assert request["required_response"]["theta_deg"]["minimum_endpoint"] > 8
    qe = request["local_sources"]["qe_catalog"]
    assert qe["native_theta_phi_site_grid"] is False
    assert len(qe["sha256"]) == 64
    assert len(request["local_sources"]["tcad_profiles"]) == 2
    assert all(not item["is_measured"] for item in request["local_sources"][
        "tcad_profiles"
    ])
    assert request["physical_validation"] == "not_established"
    assert request["product_ranking_allowed"] is False


def test_p0014_imx662_evidence_is_not_cross_spliced_into_3um_sensor(p0014_candidate):
    psf, directional = p0014_candidate
    request = native_sensor_data_request(
        "DEF-2206-805", psf=psf, directional_response=directional,
        repository_root=ROOT,
    )
    assert "lens_sensor_pixel_pitch_mismatch" in request["reason_codes"]
    assert request["lens_support"]["pixel_pitch_um"] == 3.0
    assert request["saved_sensor_response"]["pixel_pitch_um"] == 2.9
    assert request["required_response"]["pixel_pitch_um"] == 2.9
    assert request["required_response"]["compatible_with_supplied_lens_psf_pitch"] is False
    geometry = request["geometry_prerequisite"]
    assert geometry["status"] == "blocked_before_joint_execution"
    assert geometry["sensor_native_pixel_pitch_um"] == 2.9
    assert geometry["lens_psf_sample_pitch_um"] == 3.0
    assert geometry["resolution_paths"] == [
        "select_a_matching_native_sensor_revision",
        "qualified_optical_resampling_or_regeneration_at_sensor_pitch",
    ]
    assert geometry["unqualified_resampling_allowed"] is False
    fdtd = request["local_sources"]["fdtd_responses"]
    assert len(fdtd) == 1
    assert fdtd[0]["completed_sample_count"] == 0
    assert fdtd[0]["expected_sample_count"] == 4030
    assert "whole 2x2 cell" in fdtd[0]["normalization"]
    assert fdtd[0]["independent_site_collected_eqe"] is False
    assert request["status"] == "source_data_required"
