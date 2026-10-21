"""Reviewed local structural evidence, separate from measured optical response.

The packaged manifest contains page-level facts, not the licensed source PDFs.
It can be used offline; source availability and hashes can be checked explicitly.
Only selected geometry is used by the physical estimate. Neither structural
measurements nor promotional QE claims calibrate CFA dispersion or collection.
"""

from __future__ import annotations

import copy
import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import numpy as np


def _manifest() -> tuple[dict[str, Any], str]:
    data = (
        files("pyisetcam")
        .joinpath("data/sensor_estimate/techinsights_evidence_v1.json")
        .read_bytes()
    )
    value = json.loads(data)
    if value.get("schema") != "camerae2e_reviewed_techinsights_v1":
        raise ValueError("Unsupported reviewed TechInsights evidence schema.")
    return value, hashlib.sha256(data).hexdigest()


def sensor_techinsights_evidence(
    sensor_id: str,
    *,
    root: str | Path | None = None,
    source_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return reviewed facts and optional local-PDF integrity checks.

    ``source_root`` points to the user's TechInsights collection. No files are
    downloaded or changed. A missing PDF is reported as unavailable, never as
    a successful fresh verification. Unknown reports retain their DB fallback.
    """
    from .image_sensor_db import image_sensor_db_get

    record = image_sensor_db_get(sensor_id, root=root)
    manifest, digest = _manifest()
    code = str(record["code"])
    profile = copy.deepcopy(manifest["profiles"].get(code, {}))
    checks = {}
    for key, source in profile.get("sources", {}).items():
        checks[key] = {"status": "not_checked"}
        if source_root is not None:
            base = Path(source_root).expanduser().resolve()
            path = (base / source["relative_path"]).resolve()
            if not path.is_relative_to(base):
                raise ValueError("Evidence source must remain inside source_root.")
            if not path.is_file():
                checks[key] = {"status": "unavailable", "path": str(path)}
            else:
                with path.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                checks[key] = {
                    "status": "matched" if actual == source["sha256"] else "mismatch",
                    "path": str(path),
                    "actual_sha256": actual,
                }
    return {
        "schema": manifest["schema"],
        "report_code": code,
        "sensor_id": record["sensor_id"],
        "available": bool(profile),
        "manifest_sha256": digest,
        "source_checks": checks,
        "resolver_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "measured_response_available": False,
        "measured_accuracy_verified": False,
        **profile,
    }


def _reviewed_material_bundle(bundle: dict[str, Any], *, root: Any = None) -> dict[str, Any]:
    """Apply the small allowlist of reviewed geometry to a copy of a bundle."""
    result = copy.deepcopy(bundle)
    evidence = sensor_techinsights_evidence(bundle["report_code"], root=root)
    result["techinsights_evidence"] = evidence
    if not evidence["available"]:
        return result
    facts = evidence["selected_facts"]
    geometry = result["geometry_um"]
    applied = {}

    def number(key: str) -> float:
        fact = facts[key]
        source = evidence["sources"][fact["source_id"]]
        page = fact["pdf_page_1_based"]
        if not isinstance(page, int) or not 1 <= page <= source["page_count"]:
            raise ValueError(f"Invalid evidence page for {key}.")
        if fact["status"] != "reported_structural_measurement":
            raise ValueError(f"Unreviewed measurement cannot override {key}.")
        value = float(fact["value"])
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f"Invalid reviewed geometry {key}.")
        applied[key] = copy.deepcopy(fact)
        return value

    for fact_key, geometry_key in (
        ("pixel_pitch_um", "pitch"),
        ("active_si_thickness_um", "si_thickness"),
        ("microlens_height_um", "lens_height"),
        ("microlens_vertex_radius_um", "lens_vertex_radius_um"),
    ):
        if fact_key in facts:
            geometry[geometry_key] = number(fact_key)
    height = number("optical_stack_height_um")
    if "grid_aperture_area_fraction" in facts:
        fraction = number("grid_aperture_area_fraction")
        if fraction > 1:
            raise ValueError("Grid aperture area fraction must be in (0,1].")
        geometry["acceptance_width_fraction"] = float(np.sqrt(fraction))
        geometry["acceptance_source"] = (
            "equivalent_square_from_reported_grid_area_not_measured_photodiode_fill_factor"
        )
    cfa_by_channel = {}
    passivation_by_channel = {}
    for channel, material in result["channels"].items():
        field = f"{channel}_cfa_thickness_um"
        if field in facts:
            material["thickness_um"] = number(field)
            material["thickness_source"] = copy.deepcopy(facts[field])
            material["thickness_range_source"] = "reported_local_measurement_no_error_bar"
            material["thickness_range_um"] = None
        elif "cfa_range_conflict" in evidence:
            material["thickness_range_um"] = evidence["cfa_range_conflict"][
                "pooled_sensitivity_range_um"
            ].copy()
            material["thickness_range_source"] = (
                "pooled_DEF_DEP_local_observations_unidentified_color_not_confidence_interval"
            )
        # A measured dimension does not convert the shared n,k into measured n,k.
        material["product_measured"] = False
        cfa_by_channel[channel] = material["thickness_um"]
        remaining = round(height - geometry["lens_height"] - material["thickness_um"], 12)
        if remaining <= 0:
            raise ValueError("Reviewed lens/CFA exceed the reported optical stack height.")
        passivation_by_channel[channel] = remaining
    geometry["cfa_thickness_by_channel"] = cfa_by_channel
    geometry["passivation_thickness_by_channel"] = passivation_by_channel
    geometry["passivation_thickness"] = round(
        height - geometry["lens_height"] - geometry["cfa_thickness"], 12
    )
    result["pixel_pitch_um"] = geometry["pitch"]
    canonical_hash = bundle["geometry_sha256"]
    result["geometry_sha256"] = hashlib.sha256(
        json.dumps(geometry, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    result["provenance"].update(
        {
            "canonical_geometry_sha256": canonical_hash,
            "geometry_source": "reviewed_local_TechInsights_with_explicit_proxy_remainder",
            "reviewed_evidence_sha256": evidence["manifest_sha256"],
            "applied_structural_facts": applied,
            "canonical_passivation_thickness_um": bundle["geometry_um"]["passivation_thickness"],
            "reviewed_passivation_thickness_um": geometry["passivation_thickness"],
            "passivation_thickness_by_channel_um": passivation_by_channel,
            "passivation_thickness_source": "inferred_lumped_remainder_including_unresolved_AR",
            "unmodeled_structure": evidence.get("unmodeled_structure", []),
            "measured_accuracy_verified": False,
        }
    )
    return result
