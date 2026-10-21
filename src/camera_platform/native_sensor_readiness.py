"""Source-specific data request for a Lens and saved Sensor DB revision.

This is an inventory and acquisition contract, not an EQE converter. Existing
QE catalog points, fast physical estimates, cell-normalized FDTD absorption,
and proxy TCAD profiles cannot be silently promoted into independent-site
collected EQE. A later original-source bundle must pass native_response_assets.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .native_pixel_response import (
    DIRECTION_CONVENTION,
    REFERENCE_PLANE,
    RESPONSE_BASIS,
    SITE_NAMES,
)
from .native_response_assets import _lens_support
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camera_native_sensor_data_request_v1"
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_QE_FIELDS = {
    "measurement_data_status", "quantitative_points", "qualitative_mentions",
    "spectral_ranges", "filter_sets",
}
_QE_POINT_FIELDS = {
    "wavelength_nm", "qe_percent", "source_kind", "confidence",
    "method", "source_file", "context",
}
_CELL_NORMALIZATION = "whole 2x2 cell incident normal power in an empty reference cell"


def _source(path: Path, repository_root: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(repository_root) or not resolved.is_file():
        raise ValueError("Source must be a regular file under repository_root")
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return {
        "path": resolved.relative_to(repository_root).as_posix(),
        "sha256": digest.hexdigest(), "bytes": resolved.stat().st_size,
    }


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _local_sources(repository_root: Path, sensor_id: str, report_code: str) -> dict:
    if not _SAFE_ID.fullmatch(sensor_id) or not _SAFE_ID.fullmatch(report_code):
        raise ValueError("Saved Sensor DB source identifiers require safe file names")
    base = repository_root / "camerae2e_db" / "fdtd_tcad"
    qe_path = base / "image_sensor_db" / "optical_qe_db" / "models" / f"{sensor_id}.json"
    qe = None
    if qe_path.is_file():
        catalog = _json(qe_path)
        if catalog.get("code") != report_code:
            raise ValueError("QE catalog report code differs from Sensor DB revision")
        data = catalog.get("qe")
        if not isinstance(data, dict) or not set(data) <= _QE_FIELDS:
            raise ValueError("QE catalog schema changed; review native source classification")
        points = data.get("quantitative_points", [])
        if not isinstance(points, list) or any(
            not isinstance(point, dict) or not set(point) <= _QE_POINT_FIELDS
            for point in points
        ):
            raise ValueError("QE catalog points changed; review native source classification")
        qe = {
            **_source(qe_path, repository_root),
            "measurement_data_status": data.get("measurement_data_status"),
            "quantitative_point_count": len(points),
            "native_theta_phi_site_grid": False,
        }
    fdtd = []
    response_dir = repository_root / "reports" / "fdtd_sensor_e2e" / "solver_v3" / "responses"
    for path in sorted(response_dir.glob(f"{report_code}_*.json")):
        data = _json(path)
        if data.get("schema") != "camerae2e_fdtd_response_v1":
            raise ValueError("FDTD response schema changed; review native source classification")
        if data.get("sensor_id") != sensor_id or data.get("report_code") != report_code:
            raise ValueError("FDTD response identity differs from Sensor DB revision")
        normalization = data.get("provenance", {}).get("normalization")
        if normalization != _CELL_NORMALIZATION:
            raise ValueError("FDTD normalization changed; review native source classification")
        fdtd.append({
            **_source(path, repository_root),
            "pixel_pitch_um": data.get("pixel_pitch_um"),
            "expected_sample_count": data.get("expected_sample_count"),
            "completed_sample_count": data.get("completed_sample_count"),
            "validation_status": data.get("validation", {}).get("status"),
            "normalization": normalization,
            "independent_site_collected_eqe": False,
        })
    tcad = []
    for tree in ("sensor_db", "image_sensor_db"):
        path = base / tree / "generated_tcad_profiles" / sensor_id / "profile.json"
        if not path.is_file():
            continue
        data = _json(path)
        if data.get("schema") != "measured_tcad_profile_v1":
            raise ValueError("TCAD profile schema changed; review native source classification")
        calibration = data.get("calibration_status", {})
        if not isinstance(calibration, dict):
            raise ValueError("TCAD calibration_status must be an object")
        if calibration.get("source_report_code") != report_code:
            raise ValueError("TCAD profile identity differs from Sensor DB revision")
        tcad.append({
            **_source(path, repository_root),
            "profile_name": data.get("profile_name"),
            "is_measured": calibration.get("is_measured"),
            "geometry_measured": calibration.get("geometry_measured"),
            "calibration_mode": calibration.get("mode"),
            "independent_site_collected_eqe": False,
        })
    return {"qe_catalog": qe, "fdtd_responses": fdtd, "tcad_profiles": tcad}


def native_sensor_data_request(
    identifier: str, *, psf: dict, directional_response: dict,
    repository_root: str | Path, run_id: str | None = None,
) -> dict[str, Any]:
    """Pin a saved DB revision and state exactly what the Lens needs next.

    The caller must supply the actual PSF and joint directional payload of a
    separately verified Factory Lens; this function validates their numerical
    contract but does not establish their Factory publication authority.
    """
    root = Path(repository_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("repository_root must be a directory")
    lens = _lens_support(psf, directional_response)
    saved = inspect_sensor_revision(
        identifier, root=root / "camerae2e_db", run_id=run_id
    )
    if saved["status"] != "simulated":
        raise ValueError("Selected Sensor DB revision is not a saved simulation")
    revision = saved["revision"]
    response = saved["native_response"]
    local = _local_sources(root, revision["sensor_id"], revision["report_code"])
    pitch_matches = response["pixel_pitch_um"] == lens["pixel_pitch_um"]
    reasons = ["saved_db_response_is_estimate_not_native_collected_eqe_grid"]
    if not saved["executable"]:
        reasons.append("saved_sensor_runtime_incompatible")
    if not pitch_matches:
        reasons.append("lens_sensor_pixel_pitch_mismatch")
    native_cfa = response["native_cfa"] or {}
    if response["filter_names"] != list(SITE_NAMES) or native_cfa.get(
        "pattern"
    ) != [[1, 2], [3, 4]]:
        reasons.append("saved_sensor_is_not_native_r_gr_gb_b")
    if any(wave not in response["wavelength_nm"] for wave in lens["wavelength_nm"]):
        reasons.append("saved_sensor_wavelength_axis_missing_lens_sample")
    if local["qe_catalog"] is None:
        reasons.append("qe_catalog_source_missing")
    else:
        reasons.append("qe_catalog_lacks_native_angular_site_grid")
    if local["fdtd_responses"]:
        reasons.append("local_fdtd_not_independent_collected_eqe")
        if any(item["completed_sample_count"] != item["expected_sample_count"]
               for item in local["fdtd_responses"]):
            reasons.append("local_fdtd_samples_incomplete")
        if any(item["validation_status"] != "verified"
               for item in local["fdtd_responses"]):
            reasons.append("local_fdtd_numerical_validation_not_passed")
        if any("whole 2x2 cell" in str(item["normalization"])
               for item in local["fdtd_responses"]):
            reasons.append("local_fdtd_cell_normalization_incompatible")
    else:
        reasons.append("sensor_specific_fdtd_response_missing")
    if local["tcad_profiles"]:
        reasons.append("local_tcad_profile_not_collected_eqe")
        if any(item["is_measured"] is not True or item["geometry_measured"] is not True
               for item in local["tcad_profiles"]):
            reasons.append("local_tcad_profile_unmeasured_proxy")
    else:
        reasons.append("sensor_specific_tcad_profile_missing")
    if saved["readout"]["classification"] == "unmeasured_assumption":
        reasons.append("readout_is_unmeasured_assumption")
    return {
        "schema": SCHEMA,
        "status": "source_data_required",
        "reason_codes": reasons,
        "lens_support": lens,
        "geometry_prerequisite": {
            "status": (
                "pitch_contract_matches" if pitch_matches else "blocked_before_joint_execution"
            ),
            "lens_psf_sample_pitch_um": lens["pixel_pitch_um"],
            "sensor_native_pixel_pitch_um": response["pixel_pitch_um"],
            "resolution_paths": [] if pitch_matches else [
                "select_a_matching_native_sensor_revision",
                "qualified_optical_resampling_or_regeneration_at_sensor_pitch",
            ],
            "unqualified_resampling_allowed": False,
        },
        "saved_sensor_revision": revision,
        "saved_sensor_response": {
            "kind": response["kind"],
            "pixel_pitch_um": response["pixel_pitch_um"],
            "wavelength_nm": response["wavelength_nm"],
            "filter_names": response["filter_names"],
            "native_cfa": response["native_cfa"],
            "estimate_only": response["estimate_only"],
            "collection_parameter": response["parameter_sources"].get(
                "collection_efficiency"
            ),
            "readout_classification": saved["readout"]["classification"],
        },
        "local_sources": local,
        "required_response": {
            "schema": "camera_native_pixel_response_v1",
            "component_id": revision["sensor_id"],
            "revision_id": revision["run_id"],
            # A Sensor EQE request must retain that revision's actual native
            # geometry even when the supplied Lens PSF is sampled elsewhere.
            "pixel_pitch_um": response["pixel_pitch_um"],
            "compatible_with_supplied_lens_psf_pitch": pitch_matches,
            "wavelength_nm": lens["wavelength_nm"],
            "theta_deg": {
                "start": 0,
                "minimum_endpoint": lens["max_active_theta_deg"],
            },
            "phi_deg": {
                "start": 0, "periodic_degrees": 360,
                "maximum_gap_degrees": 90,
            },
            "site_names": list(SITE_NAMES),
            "site_index_tile": [[0, 1], [2, 3]],
            "reference_plane": REFERENCE_PLANE,
            "direction_convention": DIRECTION_CONVENTION,
            "polarization": "unpolarized",
            "response_basis": RESPONSE_BASIS,
            "normalization": (
                "Absolute collected electrons per photon incident on each independently "
                "illuminated native pixel site; do not use whole-cell-normalized absorption"
            ),
            "included_effects": [
                "CFA", "microlens", "silicon_absorption", "charge_collection"
            ],
            "source_bundle": "camera_native_response_bundle_v1",
            "qualification": "camera_native_response_qualification_v1",
            "required_original_source_roles": {
                "measurement": ["raw_measurement", "calibration", "geometry"],
                "solver": ["solver_output", "geometry", "materials", "collection"],
            },
            "qualification_checks": [
                "absolute_photon_normalization",
                "site_coordinate_mapping",
                "wavelength_and_angle_support",
                "independent_site_basis",
                "collection_accounted_once",
                "uncertainty_or_numerical_convergence",
            ],
            "same_component_revision_readout_required": True,
        },
        "evidence_level": "saved_file_hash_and_contract_inventory",
        "physical_validation": "not_established",
        "product_ranking_allowed": False,
    }
