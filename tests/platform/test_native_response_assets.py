"""Source identity and runtime support gates for native sensor asset ingestion."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from camera_platform.native_response_assets import (
    inspect_native_response_bundle,
    load_eligible_native_response_bundle,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, payload: object) -> str:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n")
    return _sha(path)


def _response(*, pitch=3.0, theta=20.0, schema="camera_native_pixel_response_v1"):
    collected = schema == "camera_native_pixel_response_v1"
    values = [
        [
            [[0.30, 0.50, 0.45, 0.20]] * 4,
            [[0.25, 0.45, 0.40, 0.15]] * 4,
        ],
        [
            [[0.45, 0.40, 0.35, 0.10]] * 4,
            [[0.40, 0.35, 0.30, 0.08]] * 4,
        ],
    ]
    return {
        "schema": schema,
        "site_names": ["R", "Gr", "Gb", "B"],
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": pitch,
        "reference_plane": "sensor_surface_before_pixel_optics",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "polarization": "unpolarized",
        "response_basis": (
            "independent_site_collected_electrons_per_incident_pixel_photon"
            if collected else "independent_site_generated_electrons_per_incident_pixel_photon"
        ),
        "wavelength_nm": [550, 650],
        "theta_deg": [0, theta],
        "phi_deg": [0, 90, 180, 270],
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
        "values": values,
        "source": {"kind": "simulated",
                   "description": "Test solver output, not physical validation"},
    }


def _optics(*, pitch=3.0, theta=10.0):
    x = -math.sin(math.radians(theta))
    z = math.cos(math.radians(theta))
    psf = {
        "schema": "intensity_psf_v1", "wavelength_nm": [550, 650],
        "values": [[[1.0]], [[1.0]]], "sample_pitch_um": pitch,
        "normalization": "unit_sum", "centering": "array_center",
        "spatial_model": "locally_shift_invariant",
        "boundary": "zero_outside_supplied_image",
        "sampling_model": "pixel_integrated_optical_impulse",
        "source": {"kind": "simulated", "description": "Test pixel-integrated PSF"},
    }
    direction = {
        "schema": "camera_lens_directional_response_v1",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "reference_plane": "sensor_surface_before_pixel_optics",
        "field_reference": "positive_x_column",
        "symmetry": "centered_spherical_or_plane_prescription_radial_stop",
        "normalization": "conditional_transmitted_irradiance_fraction",
        "wavelength_nm": [550, 650], "field_radius": [0, 1],
        "image_height_mm": [0, 0.01], "field_reference_radius_mm": 0.01,
        "prescription_sha256": "a" * 64, "stop_transmission_sha256": "b" * 64,
        "directions_xyz": [[[x, 0, z]], [[x, 0, z]]],
        "conditional_weights": [[[1], [1]], [[1], [1]]],
        "on_axis_rotation_phi_deg": [i * 45 for i in range(8)],
    }
    return psf, direction


def _bundle(tmp_path: Path, *, response=None, status="passed"):
    response = response or _response()
    original = tmp_path / "original"
    original.mkdir(exist_ok=True)
    sources = []
    for role in ("solver_output", "geometry", "materials", "collection"):
        path = original / f"{role}.txt"
        path.write_text(f"Test original {role}\n")
        sources.append({"role": role, "path": f"original/{role}.txt", "sha256": _sha(path)})
    response_sha = _write(tmp_path / "response.json", response)
    qualification = {
        "schema": "camera_native_response_qualification_v1",
        "kind": "solver", "status": status,
        "response_sha256": response_sha,
        "source_files_sha256": {item["path"]: item["sha256"] for item in sources},
        "checks": {key: True for key in (
            "absolute_photon_normalization", "site_coordinate_mapping",
            "wavelength_and_angle_support", "independent_site_basis",
            "collection_accounted_once", "uncertainty_or_numerical_convergence"
        )},
        "limitations": ["Test-only declarations; no measured product accuracy"],
    }
    qualification_sha = _write(tmp_path / "qualification.json", qualification)
    manifest = {
        "schema": "camera_native_response_bundle_v1",
        "sensor": {"component_id": "test-sensor", "revision_id": "r1",
                   "pixel_pitch_um": response["pixel_pitch_um"],
                   "site_index_tile": [[0, 1], [2, 3]]},
        "response": {"path": "response.json", "sha256": response_sha},
        "source_files": sources,
        "qualification": {"path": "qualification.json", "sha256": qualification_sha},
    }
    _write(tmp_path / "manifest.json", manifest)
    return manifest


def test_source_bound_response_can_be_loaded_only_for_exact_lens_support(tmp_path):
    _bundle(tmp_path)
    psf, direction = _optics()
    response, inspection = load_eligible_native_response_bundle(
        tmp_path, psf=psf, directional_response=direction
    )
    assert inspection["status"] == "research_eligible"
    assert response["values"][0][0][0] == [0.30, 0.50, 0.45, 0.20]
    assert inspection["lens_support"]["max_active_theta_deg"] == pytest.approx(10)
    assert inspection["provenance"]["verification_level"] == "file_hash_and_contract_only"
    assert "Factory" in inspection["optical_parent_binding"]
    assert inspection["physical_validation"] == "not_established"
    assert inspection["product_ranking_allowed"] is False
    assert "do not multiply another QE" in inspection["no_double_count"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("pitch", "lens_psf_pixel_pitch_mismatch"),
        ("theta", "lens_active_ray_exceeds_sensor_angular_support"),
        ("wavelength", "lens_wavelength_axis_mismatch"),
        ("incomplete", "source_qualification_incomplete"),
    ],
)
def test_eligibility_refuses_unsupported_or_incomplete_asset(tmp_path, change, reason):
    response = _response(theta=5.0 if change == "theta" else 20.0)
    _bundle(
        tmp_path, response=response,
        status="incomplete" if change == "incomplete" else "passed",
    )
    psf, direction = _optics(pitch=2.9 if change == "pitch" else 3.0)
    if change == "wavelength":
        psf["wavelength_nm"] = [500, 600]
        direction["wavelength_nm"] = [500, 600]
    result = inspect_native_response_bundle(tmp_path, psf=psf, directional_response=direction)
    assert result["status"] == "refused"
    assert reason in result["reason_codes"]
    with pytest.raises(ValueError, match=reason):
        load_eligible_native_response_bundle(
            tmp_path, psf=psf, directional_response=direction
        )


def test_modified_source_or_qualification_cannot_keep_original_identity(tmp_path):
    _bundle(tmp_path)
    (tmp_path / "original/geometry.txt").write_text("changed geometry\n")
    with pytest.raises(ValueError, match="Source SHA-256 mismatch"):
        inspect_native_response_bundle(tmp_path)


def test_cell_normalized_unverified_fdtd_response_is_not_native_eqe(tmp_path):
    original = Path("reports/fdtd_sensor_e2e/solver_v3/responses/DEF-2011-804_unverified.json")
    payload = json.loads(original.read_text())
    assert payload["samples"] == []
    assert payload["validation"]["status"] == "unverified"
    _bundle(tmp_path)
    _write(tmp_path / "response.json", payload)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["response"]["sha256"] = _sha(tmp_path / "response.json")
    _write(tmp_path / "manifest.json", manifest)
    with pytest.raises(ValueError, match="exact native pixel response fields"):
        inspect_native_response_bundle(tmp_path)


def test_generation_cannot_be_treated_as_collected_response(tmp_path):
    response = _response(schema="camera_native_generation_response_v1")
    manifest = _bundle(tmp_path, response=response)
    manifest["source_files"] = [
        entry for entry in manifest["source_files"] if entry["role"] != "collection"
    ]
    report = json.loads((tmp_path / "qualification.json").read_text())
    report["source_files_sha256"] = {
        entry["path"]: entry["sha256"] for entry in manifest["source_files"]
    }
    manifest["qualification"]["sha256"] = _write(tmp_path / "qualification.json", report)
    _write(tmp_path / "manifest.json", manifest)
    psf, direction = _optics()
    result = inspect_native_response_bundle(tmp_path, psf=psf, directional_response=direction)
    assert result["reason_codes"] == ["separate_charge_collection_required"]
    assert "excludes collection" in result["no_double_count"]


def test_collected_solver_requires_explicit_collection_source(tmp_path):
    manifest = _bundle(tmp_path)
    manifest["source_files"] = [
        entry for entry in manifest["source_files"] if entry["role"] != "collection"
    ]
    report = json.loads((tmp_path / "qualification.json").read_text())
    report["source_files_sha256"] = {
        entry["path"]: entry["sha256"] for entry in manifest["source_files"]
    }
    manifest["qualification"]["sha256"] = _write(tmp_path / "qualification.json", report)
    _write(tmp_path / "manifest.json", manifest)
    psf, direction = _optics()
    result = inspect_native_response_bundle(tmp_path, psf=psf, directional_response=direction)
    assert "missing_required_original_sources" in result["reason_codes"]


def test_measured_bundle_needs_raw_calibration_and_matching_source_kind(tmp_path):
    response = _response()
    response["source"]["kind"] = "measured"
    manifest = _bundle(tmp_path, response=response)
    manifest["response"]["sha256"] = _sha(tmp_path / "response.json")
    source = []
    for role in ("raw_measurement", "calibration", "geometry"):
        path = tmp_path / "original" / f"{role}.txt"
        path.write_text(f"Test-only {role} data\n")
        source.append({"role": role, "path": f"original/{role}.txt",
                       "sha256": _sha(path)})
    manifest["source_files"] = source
    report = json.loads((tmp_path / "qualification.json").read_text())
    report["kind"] = "measurement"
    report["source_files_sha256"] = {
        item["path"]: item["sha256"] for item in source
    }
    manifest["qualification"]["sha256"] = _write(tmp_path / "qualification.json", report)
    _write(tmp_path / "manifest.json", manifest)
    psf, direction = _optics()
    result = inspect_native_response_bundle(tmp_path, psf=psf,
                                            directional_response=direction)
    assert result["status"] == "research_eligible"
    assert result["provenance"]["source_roles"] == [
        "calibration", "geometry", "raw_measurement"
    ]


def test_bundle_rejects_escape_and_response_source_kind_mismatch(tmp_path):
    manifest = _bundle(tmp_path)
    manifest["source_files"][0]["path"] = "../outside.bin"
    _write(tmp_path / "manifest.json", manifest)
    with pytest.raises((ValueError, FileNotFoundError), match="Bundle files|outside.bin"):
        inspect_native_response_bundle(tmp_path)

    manifest = _bundle(tmp_path)
    response = _response()
    response["source"]["kind"] = "estimated"
    manifest["response"]["sha256"] = _write(tmp_path / "response.json", response)
    report = json.loads((tmp_path / "qualification.json").read_text())
    report["response_sha256"] = manifest["response"]["sha256"]
    manifest["qualification"]["sha256"] = _write(tmp_path / "qualification.json", report)
    _write(tmp_path / "manifest.json", manifest)
    with pytest.raises(ValueError, match="source kind disagree"):
        inspect_native_response_bundle(tmp_path)


def test_omitting_actual_lens_does_not_approve_joint_rate(tmp_path):
    _bundle(tmp_path)
    result = inspect_native_response_bundle(tmp_path)
    assert result["status"] == "refused"
    assert result["reason_codes"] == ["lens_support_not_checked"]
    assert result["lens_support"] is None


def test_cli_returns_structured_refusal_for_bad_bundle(tmp_path):
    tool = Path("tools/inspect_camera_native_response_bundle.py")
    completed = subprocess.run(
        [sys.executable, str(tool), str(tmp_path / "missing")],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "invalid"
    assert payload["physical_validation"] == "not_established"
    assert completed.stderr == ""
