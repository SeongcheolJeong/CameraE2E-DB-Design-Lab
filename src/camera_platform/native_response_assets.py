"""Hash-pinned ingestion gate for independent-site angular sensor responses.

This module accepts an explicitly authored bundle of original measurement or
solver files and a native response table. It checks file identity and the
runtime contract; source-reported qualification is never physical validation.
Cell-normalized FDTD absorption and relative angular corrections are not
translated into independent-site EQE here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

from .lens_direction_response import validate_directional_response
from .native_pixel_response import (
    GENERATION_SCHEMA,
    REFERENCE_PLANE,
    SCHEMA,
    SITE_NAMES,
    validate_response,
)
from .spatial import validate_psf

BUNDLE_SCHEMA = "camera_native_response_bundle_v1"
QUALIFICATION_SCHEMA = "camera_native_response_qualification_v1"
MAX_MANIFEST_BYTES = 256_000
MAX_RESPONSE_BYTES = 32_000_000
MAX_QUALIFICATION_BYTES = 2_000_000
MAX_SOURCE_BYTES = 2_147_483_648
MAX_SOURCE_FILES = 128
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CHECKS = {
    "absolute_photon_normalization",
    "site_coordinate_mapping",
    "wavelength_and_angle_support",
    "independent_site_basis",
    "collection_accounted_once",
    "uncertainty_or_numerical_convergence",
}
_ROLES = {
    "raw_measurement",
    "calibration",
    "solver_output",
    "geometry",
    "materials",
    "collection",
}


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{name} requires lowercase SHA-256")
    return value


def _name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or not value.strip():
        raise ValueError(f"{name} requires a nonempty string of at most 200 characters")
    return value


def _keys(value: Any, expected: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{name} requires exactly {sorted(expected)}")
    return value


def _bundled_file(root: Path, relative: Any, max_bytes: int) -> tuple[Path, str, int]:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Bundle file path must be a nonempty relative POSIX path")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Bundle files must stay under the bundle root")
    source = (root / relative).resolve(strict=True)
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError("Bundle files must be regular files under the bundle root")
    size = source.stat().st_size
    if size <= 0 or size > max_bytes:
        raise ValueError(f"Bundle file {relative} is empty or exceeds {max_bytes} bytes")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return source, digest.hexdigest(), size


def _json(path: Path, max_bytes: int, name: str) -> Mapping[str, Any]:
    if path.stat().st_size > max_bytes:
        raise ValueError(f"{name} exceeds {max_bytes} bytes")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} must be UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _artifact(root: Path, descriptor: Any, limit: int, name: str) -> tuple[Path, str]:
    item = _keys(descriptor, {"path", "sha256"}, name)
    path, observed, _ = _bundled_file(root, item["path"], limit)
    if observed != _sha(item["sha256"], f"{name}.sha256"):
        raise ValueError(f"{name} SHA-256 mismatch")
    return path, observed


def _lens_support(psf: Mapping[str, Any], directional: Mapping[str, Any]) -> dict[str, Any]:
    optical = validate_psf(psf)
    joint = validate_directional_response(directional)
    if optical["wavelength_nm"] != joint["wavelength_nm"]:
        raise ValueError("Lens PSF and directions have different wavelength axes")
    power = np.asarray(joint["conditional_weights"])
    directions = np.asarray(joint["directions_xyz"])
    active = np.any(power > 0, axis=0)
    theta = np.degrees(np.arccos(np.clip(directions[..., 2], -1, 1)))
    return {
        "wavelength_nm": joint["wavelength_nm"],
        "pixel_pitch_um": optical["sample_pitch_um"],
        "max_active_theta_deg": float(theta[active].max()),
        "reference_plane": joint["reference_plane"],
        "direction_convention": joint["direction_convention"],
    }


def inspect_native_response_bundle(
    bundle_dir: str | Path,
    *,
    psf: Mapping[str, Any] | None = None,
    directional_response: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect immutable source identity and exact current Lens/Sensor support.

    A research-eligible result licenses a *model calculation*, not a product
    accuracy or independent validation claim. A Lens comparison requires both
    actual PSF and joint directional payloads; metadata-only angles are not
    accepted. Source qualification remains a statement supplied by the source.
    """
    root = Path(bundle_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Native response bundle must be a directory")
    manifest_path, manifest_sha, _ = _bundled_file(root, "manifest.json", MAX_MANIFEST_BYTES)
    manifest = _keys(
        _json(manifest_path, MAX_MANIFEST_BYTES, "manifest"),
        {"schema", "sensor", "response", "source_files", "qualification"},
        "manifest",
    )
    if manifest["schema"] != BUNDLE_SCHEMA:
        raise ValueError(f"Native response bundle requires {BUNDLE_SCHEMA}")
    sensor = _keys(
        manifest["sensor"],
        {"component_id", "revision_id", "pixel_pitch_um", "site_index_tile"},
        "sensor",
    )
    component_id = _name(sensor["component_id"], "sensor.component_id")
    revision_id = _name(sensor["revision_id"], "sensor.revision_id")
    pitch = sensor["pixel_pitch_um"]
    if type(pitch) not in (int, float) or not math.isfinite(pitch) or not 0.05 <= pitch <= 100:
        raise ValueError("sensor.pixel_pitch_um requires a finite physical pitch")
    if sensor["site_index_tile"] != [[0, 1], [2, 3]]:
        raise ValueError("sensor.site_index_tile requires native R/Gr/Gb/B row-major sites")
    response_path, response_sha = _artifact(
        root, manifest["response"], MAX_RESPONSE_BYTES, "response"
    )
    response = validate_response(_json(response_path, MAX_RESPONSE_BYTES, "response"))
    if (response["pixel_pitch_um"] != float(pitch)
            or response["site_index_tile"] != sensor["site_index_tile"]):
        raise ValueError("Native response geometry differs from the declared sensor revision")

    files = manifest["source_files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_SOURCE_FILES:
        raise ValueError(f"source_files requires 1..{MAX_SOURCE_FILES} pinned original files")
    source_hashes: dict[str, str] = {}
    source_roles: set[str] = set()
    for index, descriptor in enumerate(files):
        item = _keys(descriptor, {"path", "sha256", "role"}, f"source_files[{index}]")
        role = item["role"]
        if role not in _ROLES:
            raise ValueError(f"Unsupported original-source role: {role!r}")
        path, observed, _ = _bundled_file(root, item["path"], MAX_SOURCE_BYTES)
        relative = path.relative_to(root).as_posix()
        if relative in source_hashes or relative in {"manifest.json", manifest["response"]["path"]}:
            raise ValueError("Source files must have distinct paths from manifest and response")
        if observed != _sha(item["sha256"], f"source_files[{index}].sha256"):
            raise ValueError(f"Source SHA-256 mismatch: {relative}")
        source_hashes[relative] = observed
        source_roles.add(role)

    report_path, report_sha = _artifact(
        root, manifest["qualification"], MAX_QUALIFICATION_BYTES, "qualification"
    )
    if report_path.relative_to(root).as_posix() in source_hashes or report_path == response_path:
        raise ValueError("Qualification must be a separate immutable file")
    report = _keys(
        _json(report_path, MAX_QUALIFICATION_BYTES, "qualification"),
        {"schema", "kind", "status", "response_sha256", "source_files_sha256",
         "checks", "limitations"},
        "qualification",
    )
    if report["schema"] != QUALIFICATION_SCHEMA:
        raise ValueError(f"Qualification requires {QUALIFICATION_SCHEMA}")
    if report["response_sha256"] != response_sha or report["source_files_sha256"] != source_hashes:
        raise ValueError("Qualification report does not bind the exact response and source bytes")
    kind = report["kind"]
    if kind not in {"measurement", "solver"} or response["source"]["kind"] != {
        "measurement": "measured", "solver": "simulated"
    }[kind]:
        raise ValueError("Qualification kind and native response source kind disagree")
    if not isinstance(report["limitations"], list) or any(
        not isinstance(item, str) or not item.strip() for item in report["limitations"]
    ):
        raise ValueError("Qualification limitations require a list of nonempty statements")
    checks = report["checks"]
    if not isinstance(checks, Mapping) or set(checks) != _CHECKS or any(
        type(value) is not bool for value in checks.values()
    ):
        raise ValueError("Qualification requires explicit boolean evidence checks")
    required_roles = (
        {"raw_measurement", "calibration", "geometry"}
        if kind == "measurement"
        else {"solver_output", "geometry", "materials"}
    )
    if response["schema"] == SCHEMA and kind == "solver":
        required_roles.add("collection")
    reasons = []
    if not required_roles <= source_roles:
        reasons.append("missing_required_original_sources")
    if report["status"] != "passed" or not all(checks.values()):
        reasons.append("source_qualification_incomplete")
    if response["schema"] == GENERATION_SCHEMA:
        reasons.append("separate_charge_collection_required")
    if response["schema"] == SCHEMA and response["response_basis"] != (
        "independent_site_collected_electrons_per_incident_pixel_photon"
    ):
        reasons.append("not_independent_collected_eqe")
    if (psf is None) != (directional_response is None):
        raise ValueError("Lens support requires both actual PSF and directional response")
    lens = None
    if psf is not None and directional_response is not None:
        lens = _lens_support(psf, directional_response)
        if lens["wavelength_nm"] != response["wavelength_nm"]:
            reasons.append("lens_wavelength_axis_mismatch")
        if lens["pixel_pitch_um"] != response["pixel_pitch_um"]:
            reasons.append("lens_psf_pixel_pitch_mismatch")
        if lens["max_active_theta_deg"] > response["theta_deg"][-1] + 1e-10:
            reasons.append("lens_active_ray_exceeds_sensor_angular_support")
        if lens["reference_plane"] != REFERENCE_PLANE or lens["direction_convention"] != (
            response["direction_convention"]
        ):
            reasons.append("lens_sensor_direction_or_reference_plane_mismatch")
    else:
        reasons.append("lens_support_not_checked")
    return {
        "schema": "camera_native_response_bundle_inspection_v1",
        "status": "research_eligible" if not reasons else "refused",
        "reason_codes": reasons,
        "sensor": {"component_id": component_id, "revision_id": revision_id,
                   "pixel_pitch_um": float(pitch), "site_names": list(SITE_NAMES)},
        "response": {"schema": response["schema"], "sha256": response_sha,
                     "wavelength_nm": response["wavelength_nm"],
                     "theta_domain_deg": [response["theta_deg"][0], response["theta_deg"][-1]],
                     "phi_deg": response["phi_deg"], "source": response["source"]},
        "lens_support": lens,
        "optical_parent_binding": (
            "not_checked_here; use the Factory PSF/direction publication gate"
        ),
        "provenance": {"manifest_sha256": manifest_sha,
                       "qualification_sha256": report_sha,
                       "original_source_sha256": source_hashes,
                       "source_roles": sorted(source_roles),
                       "source_reported_status": report["status"],
                       "source_reported_checks": dict(checks),
                       "limitations": report["limitations"],
                       "verification_level": "file_hash_and_contract_only"},
        "physical_validation": "not_established",
        "product_ranking_allowed": False,
        "no_double_count": (
            "collected EQE includes CFA, microlens, absorption and collection; "
            "do not multiply another QE, angular response, fill factor, or collection"
            if response["schema"] == SCHEMA else
            "generation excludes collection; a separately qualified "
            "charge-collection model is required"
        ),
    }


def load_eligible_native_response_bundle(
    bundle_dir: str | Path,
    *,
    psf: Mapping[str, Any],
    directional_response: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return original native response only after a fresh full eligibility check."""
    inspection = inspect_native_response_bundle(
        bundle_dir, psf=psf, directional_response=directional_response
    )
    if inspection["status"] != "research_eligible":
        raise ValueError("Native response bundle refused: " + ", ".join(inspection["reason_codes"]))
    root = Path(bundle_dir).expanduser().resolve(strict=True)
    manifest = _json(root / "manifest.json", MAX_MANIFEST_BYTES, "manifest")
    path, observed = _artifact(root, manifest["response"], MAX_RESPONSE_BYTES, "response")
    if observed != inspection["response"]["sha256"]:
        raise ValueError("Native response changed after eligibility inspection")
    response = validate_response(_json(path, MAX_RESPONSE_BYTES, "response"))
    second = inspect_native_response_bundle(
        bundle_dir, psf=psf, directional_response=directional_response
    )
    if second != inspection:
        raise ValueError("Native response bundle changed during loading")
    return response, inspection
