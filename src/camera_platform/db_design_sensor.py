"""Pinned, reviewed DB sensor snapshots for the native Reference design bench.

The saved physical response and six-field readout are reused without regenerating
the source DB. The bench's common FPN-off policy is explicit: saved bias DSNU in
electrons/capture cannot be relabeled as the electrical adapter's electrons/s.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np

from . import design_workshop_sensor as _pixel
from .electrical import default_electrical_model

SCHEMA = "camera_db_design_sensor_selection_v1"
_PINS = {
    "reference_imx462": {
        "label": "IMX462 structural Reference",
        "report_code": "DEF-2011-804",
        "asset_id": "sensor-e71903206aefdc62550da6f5",
        "revision_id": "0ccf0fae-faa3-5001-88d5-0e30d37bfec6",
        "content_sha256": "67bd5db3cfc8c86734ab1ee03eedfff72a87308a09d6a994d2e2c56d0621878b",
    },
    "imx662": {
        "label": "IMX662 · reviewed structure / ASI662MC readout proxy",
        "report_code": "DEF-2206-805",
        "asset_id": "sensor-1314761dde53465f2b5b00a7",
        "revision_id": "a75eaa44-2275-5723-a08b-0ee6f1f902e8",
        "content_sha256": "5e8f391c5835625b413438d77b48aafbd0fb9e0f8dd8274cdb34e51ce3535cd8",
        "run_id": "4cbab486b690b1d84352d7743aee3dd0d9c0e5582d45e9a38196e8c9677496a2",
        "result_sha256": "37aea8620d6562ed099721b6b002c2c7d6bcca1047ec9462a5a3d9b320d2599d",
    },
    "imx307": {
        "label": "IMX307 · reviewed structure / assumed readout",
        "report_code": "DEF-1807-801",
        "asset_id": "sensor-515882f9c5992fb29cb8a09e",
        "revision_id": "a8b9f174-88ed-5bde-94a6-b13791564a9a",
        "content_sha256": "a1955d0b28b78174c465412eb634e0b975b600cdd7a108f97b68ea5ef880f2ba",
        "run_id": "039268c3f0c905b0fb2f1ece05c68330362fc000b9746d8d5479f5e7e023cbaa",
        "result_sha256": "c479493f24a42ba4d921657dc84b4961738c178ceb0f3788ebf4ebd0c8fa5932",
    },
}
_UNITS = {
    "gain": "unitless",
    "full_well_e": "electron/pixel",
    "read_noise_e": "electron RMS/pixel",
    "dark_current_e_per_s": "electron/pixel/s",
    "bit_depth": "bit",
    "black_level_dn": "DN",
}


def _catalog_binding(workspace, pin):
    path = Path(workspace).resolve() / "camerae2e_db/catalog.sqlite"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT m.revision_id,m.component_id,m.content_sha256,m.snapshot_json,"
            "s.sha256 AS source_sha256 FROM model_revisions m "
            "LEFT JOIN sources s ON s.source_id=m.source_id WHERE m.revision_id=?",
            (pin["revision_id"],),
        ).fetchone()
    if row is None or row["content_sha256"] != pin["content_sha256"]:
        raise ValueError("Pinned sensor catalog revision changed or is unavailable")
    if json.loads(row["snapshot_json"]).get("metadata", {}).get("code") != pin["report_code"]:
        raise ValueError("Pinned sensor catalog report identity does not match")
    return {
        key: row[key] for key in ("revision_id", "component_id", "content_sha256", "source_sha256")
    }


def _load_saved(workspace, pin):
    from pyisetcam.sensor_simulation_db import (
        _load_runtime_snapshot,
        sensor_simulation_db_config,
        sensor_simulation_db_get,
    )

    root = Path(workspace).resolve() / "camerae2e_db"
    saved = sensor_simulation_db_get(pin["report_code"], root=root, run_id=pin["run_id"])
    if saved["db_entry"]["result_sha256"] != pin["result_sha256"]:
        raise ValueError("Pinned sensor result hash changed")
    config = sensor_simulation_db_config(pin["report_code"], root=root, run_id=pin["run_id"])
    # The saved snapshot runtime gate also verifies its arrays and result hashes.
    metadata = _load_runtime_snapshot(config["scenario"]["sensor_model"])
    model = metadata["model_snapshot"]
    _validate_native(model, metadata)
    return saved, metadata


def _validate_native(model, metadata):
    native = metadata.get("native_cfa", {})
    if (
        model.get("kind") != "physical_estimate_v1"
        or model.get("pixel_pitch_um") != 2.9
        or model.get("geometry_um", {}).get("pitch") != 2.9
        or model.get("filter_names") != _pixel.SITES
        or native.get("pattern") != [[1, 2], [3, 4]]
        or native.get("pattern_index_base") != 1
        or native.get("pixel_pitch_um") != 2.9
    ):
        raise ValueError("DB bench requires native 2.9 um RGGB R/Gr/Gb/B physical response")
    if model.get("optical_assumptions", {}).get("routing_mode") != "no_routing_loss":
        raise ValueError("DB bench does not compound microlens routing with nominal QE")
    if not model.get("provenance", {}).get("structural_evidence", {}).get("available"):
        raise ValueError("DB bench requires reviewed structural evidence")
    wave = np.asarray(model.get("wavelength_nm"), float)
    if (
        wave.ndim != 1
        or len(wave) < 2
        or not np.isfinite(wave).all()
        or (np.any(np.diff(wave) <= 0) or wave[0] > 450 or wave[-1] < 650)
    ):
        raise ValueError("DB sensor does not support the common 450-650 nm band")
    domain = np.asarray(model.get("angular_domain_deg", []), float)
    if domain.shape != (2,) or not np.isfinite(domain).all() or domain[0] > 0 or domain[1] < 60:
        raise ValueError("DB sensor does not support the common 0-60 degree CRA domain")


def _parameter_sources(cell, readout_profile=None):
    rows = []
    facts = cell["field_classification"]["source_derived"]
    for name, fact in facts.items():
        rows.append(
            {
                "parameter": name,
                "value": copy.deepcopy(fact["value"]),
                "unit": fact.get("unit"),
                "classification": fact.get("status"),
                "origin": "reported_structure",
                "source_id": fact.get("source_id"),
                "pdf_page_1_based": fact.get("pdf_page_1_based"),
                "notes": fact.get("notes", ""),
                "applied": True,
                "application": "geometry_diagnostic_only"
                if name in {"grid_aperture_area_fraction", "microlens_vertex_radius_um"}
                else "nominal_response_geometry",
            }
        )
    profile = readout_profile or {}
    fields = profile.get("provenance", {}).get("fields", {})
    for name, value in cell["readout"].items():
        fact = fields.get(name, {})
        classification = fact.get("classification", "assumption")
        rows.append(
            {
                "parameter": name,
                "value": value,
                "unit": _UNITS[name],
                "classification": classification,
                "origin": "assumption" if classification == "assumption" else "camera_proxy",
                "source_id": fact.get("source_id"),
                "notes": fact.get("notes")
                or "Shared Reference readout assumption; not a measured sensor specification.",
                "applied": True,
            }
        )
    for name, value, unit, note in (
        (
            "collection_efficiency",
            cell["collection_efficiency"],
            "electron/absorbed photon",
            "Common collection assumption, not TCAD or measured charge collection.",
        ),
        (
            "cfa_optical_constants",
            "shared n,k material proxies",
            None,
            "The sensor-specific structure does not establish measured product spectra.",
        ),
        (
            "fixed_pattern_noise",
            "disabled",
            None,
            "Common bench policy. Saved PRNU and electrons/capture bias DSNU are retained "
            "as evidence, not silently converted to electrons/s dark-current DSNU.",
        ),
    ):
        rows.append(
            {
                "parameter": name,
                "value": value,
                "unit": unit,
                "classification": "assumption",
                "origin": "assumption",
                "notes": note,
                "applied": True,
            }
        )
    geometry = cell["geometry_um"]
    for color, thickness in geometry["cfa_thickness_by_channel"].items():
        name = f"{color}_cfa_thickness_um"
        if name not in facts:
            rows.append(
                {
                    "parameter": name,
                    "value": thickness,
                    "unit": "um",
                    "classification": "assumption_with_reported_range",
                    "origin": "assumption",
                    "applied": True,
                    "notes": "Assigned color proxy; no separately reviewed channel measurement.",
                }
            )
    if "microlens_height_um" not in facts:
        rows.append(
            {
                "parameter": "microlens_height_um",
                "value": geometry["lens_height"],
                "unit": "um",
                "origin": "assumption",
                "classification": "geometry_proxy",
                "applied": True,
                "notes": "Planar polymer layer split, not measured lens shape.",
            }
        )
    rows.append(
        {
            "parameter": "passivation_thickness_by_channel_um",
            "value": copy.deepcopy(geometry["passivation_thickness_by_channel"]),
            "unit": "um",
            "origin": "derived",
            "classification": "inferred_lumped_remainder",
            "applied": True,
            "notes": "Reported total stack minus selected polymer and CFA thickness.",
        }
    )
    for row in rows:
        row["source"] = row.get("source_id") or (
            "Pinned model / explicit bench assumption"
            if row["origin"] == "assumption"
            else "Pinned model geometry derivation"
        )
    return rows


def build_cell(workspace, key):
    """Return a hash-pinned, native cell accepted by the existing Reference engine."""
    if key not in _PINS:
        raise ValueError(f"Unsupported DB design sensor key: {key}")
    pin = _PINS[key]
    binding = _catalog_binding(workspace, pin)
    profile = None
    if key == "reference_imx462":
        from .reference_workshop_sensor import build_cell as reference_cell

        cell = reference_cell(workspace)
        selection = {
            "schema": SCHEMA,
            "key": key,
            **copy.deepcopy(pin),
            **binding,
            "source_kind": "existing_reference_adapter",
            "saved_run_id": None,
            "physical_validation": "not_established",
        }
    else:
        saved, metadata = _load_saved(workspace, pin)
        model = copy.deepcopy(metadata["model_snapshot"])
        geometry = copy.deepcopy(model["geometry_um"])
        profile = copy.deepcopy(metadata["readout_profile"])
        evidence = copy.deepcopy(model["provenance"]["structural_evidence"])
        section = _pixel._cross_section(geometry)
        section["cfa_thickness_by_channel_um"] = copy.deepcopy(geometry["cfa_thickness_by_channel"])
        section["passivation_thickness_by_channel_um"] = copy.deepcopy(
            geometry["passivation_thickness_by_channel"]
        )
        cell = {
            "schema": _pixel.CELL_SCHEMA,
            "reference_profile": "pinned_db_native_pitch_v1",
            "id": f"db-design-{key}-{pin['run_id'][:12]}",
            "label": pin["label"],
            "source_report": pin["report_code"],
            "product_model": False,
            "cfa_thickness_scale": 1.0,
            "pixel_pitch_um": 2.9,
            "cfa_pattern": "RGGB",
            "sites": _pixel.SITES.copy(),
            "geometry_um": geometry,
            "cross_section": section,
            "collection_efficiency": model["collection_efficiency"],
            "readout": copy.deepcopy(model["readout"]),
            "electrical_model": default_electrical_model(model["readout"]),
            "physical_model": model,
            "field_classification": {
                "source_derived": copy.deepcopy(model["provenance"]["applied_structural_facts"]),
                "readout_fields": copy.deepcopy(profile["provenance"]["fields"]),
                "common_assumptions": {
                    "collection_efficiency": model["collection_efficiency"],
                    "cfa_optical_constants": "shared material proxies, not measured product n,k",
                    "cfa_thickness_by_channel_um": copy.deepcopy(
                        geometry["cfa_thickness_by_channel"]
                    ),
                    "passivation": "lumped remainder of reported total optical-stack height",
                    "cfa_origin": "RGGB phase chosen as the shared simulation coordinate origin",
                    "fixed_pattern": "disabled by common Reference bench policy",
                },
                "user_overrides": {},
            },
            "provenance": {
                "database_modified": False,
                "source_basis": "hash-pinned saved physical sensor snapshot",
                "source_model_sha256": _pixel._digest(model),
                "source_geometry_um": copy.deepcopy(geometry),
                "source_pixel_pitch_um": 2.9,
                "structural_evidence": evidence,
                "source_evidence_sha256": evidence["manifest_sha256"],
                "derived_geometry_sha256": _pixel._digest(geometry),
                "readout_profile": profile,
                "saved_fixed_pattern": copy.deepcopy(profile["fixed_pattern"]),
                "fixed_pattern_application": "not_applied_common_bench_policy",
                "electrical_basis": profile["provenance"]["classification"],
                "source_files": {"saved_result": saved["db_entry"]["result_path"]},
                "qe_contract": "absolute collected electrons per sensor-plane incident photon",
                "excluded": ["resolved OCL routing", "DTI", "spatial crosstalk", "TCAD collection"],
                "limitations": [
                    "Reviewed structure plus proxy response; product response is unmeasured.",
                    "Planar CRA with no_routing_loss; OCL overlap remains diagnostic.",
                    "CFA n,k, red/blue thickness, collection and unresolved layers remain proxies.",
                    "Camera-proxy electrical values are not bare-sensor silicon measurements.",
                    "Saved FPN profiles are not applied; all compared bench cells use FPN off.",
                    "No extra saved QE, second receiving aperture or pixel-area factor is applied.",
                ],
            },
        }
        selection = {
            "schema": SCHEMA,
            "key": key,
            **copy.deepcopy(pin),
            **binding,
            "source_kind": "saved_physical_snapshot",
            "saved_run_id": pin["run_id"],
            "arrays_sha256": saved["arrays_sha256"],
            "config_sha256": saved["db_entry"]["config_sha256"],
            "physical_validation": "not_established",
        }
    selection["parameter_sources"] = _parameter_sources(cell, profile)
    selection["adapter_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    cell["provenance"]["db_selection"] = selection
    cell["cell_sha256"] = _pixel._digest(
        {name: val for name, val in cell.items() if name != "cell_sha256"}
    )
    return cell


def candidates(workspace):
    """Return supported choices with live, fail-closed snapshot readiness checks."""
    choices = []
    for key, pin in _PINS.items():
        item = {
            "key": key,
            **copy.deepcopy(pin),
            "pixel_pitch_um": 2.9,
            "cfa_pattern": "RGGB",
            "physical_validation": "not_established",
        }
        try:
            cell = build_cell(workspace, key)
        except (ValueError, FileNotFoundError, KeyError, sqlite3.Error) as error:
            item.update(execution_ready=False, reason=str(error))
        else:
            item.update(
                execution_ready=True,
                reason=None,
                cell_sha256=cell["cell_sha256"],
                parameter_sources=cell["provenance"]["db_selection"]["parameter_sources"],
                readout=copy.deepcopy(cell["readout"]),
            )
        choices.append(item)
    return choices
