"""Native-pitch reference pixels and fixed, baseline-only lens shading maps.

The existing planar response backend is reused. Reviewed structural facts are
kept separate from shared optical/electrical assumptions and design overrides.
LSC corrects signed RAW samples before demosaic, never the preserved RAW file.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import numpy as np

from . import design_workshop_sensor as _pixel
from .design_workshop_sensor import (  # noqa: F401 -- public response interface
    absolute_angular_response,
    build_response,
    interpolate_qe,
    photoelectron_rate_density,
)
from .registry import digest

SOURCE_REPORT = _pixel.SOURCE_REPORT
LSC_SCHEMA = "camera_reference_workshop_fixed_lsc_v1"
SITES = tuple(_pixel.SITES)


def build_cell(workspace, cfa_thickness_scale=1.0):
    """Clone the reviewed IMX462 structural reference at its original 2.9 µm pitch.

    No spectral QE, OCL/DTI response or readout calibration is implied by a report's
    structural observations. CFA n,k and its per-color thickness assignment remain
    explicit proxies. CFA variations replace passivation, conserving total height.
    """
    from pyisetcam import physical_estimate

    from .electrical import default_electrical_model

    scale = float(cfa_thickness_scale)
    if not np.isfinite(scale) or scale not in _pixel.SCALES:
        raise ValueError("CFA thickness scale must be one of 0.9, 1.0, 1.1")
    source = physical_estimate.image_sensor_physical_model(
        SOURCE_REPORT,
        root=Path(workspace) / "camerae2e_db/fdtd_tcad/image_sensor_db",
        collection_efficiency=0.75,
        optical_assumptions={
            "geometry_source": "reviewed_techinsights",
            "microlens_enabled": False,
            "routing_mode": "no_routing_loss",
        },
    )
    evidence = source["provenance"]["structural_evidence"]
    if not evidence.get("available"):
        raise ValueError("Reviewed structural evidence is unavailable for the fixed reference")
    model = copy.deepcopy(source)
    original = copy.deepcopy(source["geometry_um"])
    geometry = model["geometry_um"]
    if float(geometry["pitch"]) != 2.9 or float(source["pixel_pitch_um"]) != 2.9:
        raise ValueError("Reference pitch changed; review a new baseline before reuse")
    for suffix in ("", "_by_channel"):
        cfa_key, pass_key = "cfa_thickness" + suffix, "passivation_thickness" + suffix
        if suffix:
            for channel, thickness in original[cfa_key].items():
                geometry[cfa_key][channel] = round(float(thickness) * scale, 12)
                geometry[pass_key][channel] = round(
                    float(original[pass_key][channel]) + thickness - geometry[cfa_key][channel],
                    12,
                )
                if geometry[pass_key][channel] <= 0:
                    raise ValueError("CFA change leaves no positive passivation thickness")
        else:
            geometry[cfa_key] = round(float(original[cfa_key]) * scale, 12)
            geometry[pass_key] = round(
                float(original[pass_key]) + original[cfa_key] - geometry[cfa_key], 12
            )
            if geometry[pass_key] <= 0:
                raise ValueError("CFA change leaves no positive passivation thickness")
    model["wavelength_nm"] = np.arange(450.0, 651.0, 10.0).tolist()
    model["eqe"] = physical_estimate.physical_optical_response(model, model["wavelength_nm"])[
        "eqe"
    ].tolist()
    model["provenance"]["effective_geometry_sha256"] = digest(geometry)
    model["provenance"]["reference_workshop_derived_cell"] = True
    model["provenance"]["limitations"] = [
        text
        for text in model["provenance"]["limitations"]
        if not text.startswith("Independent geometry perturbations")
    ]
    material = source["material_bundle"]
    source_provenance = material["provenance"]
    readout = copy.deepcopy(source["readout"])
    overrides = {
        key: {"baseline": original[key], "value": copy.deepcopy(geometry[key])}
        for key in (
            "cfa_thickness",
            "passivation_thickness",
            "cfa_thickness_by_channel",
            "passivation_thickness_by_channel",
        )
        if original[key] != geometry[key]
    }
    classification = {
        "source_derived": copy.deepcopy(source["provenance"]["applied_structural_facts"]),
        "common_assumptions": {
            "readout": readout,
            "collection_efficiency": 0.75,
            "cfa_optical_constants": "shared material proxies, not measured product n,k",
            "baseline_cfa_thickness_by_channel_um": original["cfa_thickness_by_channel"],
            "cfa_assignment": (
                "0.5 um proxy per color; reported 0.45–0.55 um range has no color mapping"
            ),
            "lens_polymer_height_um": original["lens_height"],
            "passivation": "lumped remainder of reported total optical-stack height",
            "cfa_origin": "RGGB phase chosen as the shared simulation coordinate origin",
            "microlens_routing": "disabled; planar polymer layer only",
        },
        "user_overrides": overrides,
    }
    section = _pixel._cross_section(geometry)
    section["cfa_thickness_by_channel_um"] = copy.deepcopy(geometry["cfa_thickness_by_channel"])
    section["passivation_thickness_by_channel_um"] = copy.deepcopy(
        geometry["passivation_thickness_by_channel"]
    )
    cell = {
        "schema": _pixel.CELL_SCHEMA,
        "reference_profile": "reviewed_imx462_native_pitch_v1",
        "id": f"reference-imx462-cfa-{scale:.1f}",
        "label": f"IMX462 structural reference / 2.9 um / CFA {scale:.1f}x",
        "source_report": SOURCE_REPORT,
        "product_model": False,
        "cfa_thickness_scale": scale,
        "pixel_pitch_um": 2.9,
        "cfa_pattern": "RGGB",
        "sites": list(SITES),
        "geometry_um": copy.deepcopy(geometry),
        "cross_section": section,
        "collection_efficiency": 0.75,
        "readout": readout,
        "electrical_model": default_electrical_model(readout),
        "physical_model": model,
        "field_classification": classification,
        "provenance": {
            "database_modified": False,
            "source_basis": (
                "reviewed IMX462 structural facts with explicit shared material proxies"
            ),
            "source_pixel_pitch_um": 2.9,
            "source_geometry_um": original,
            "source_model_sha256": digest(source),
            "source_material_bundle_sha256": material["material_bundle_sha256"],
            "source_geometry_sha256": material["geometry_sha256"],
            "source_optical_model_sha256": source_provenance["optical_model_sha256"],
            "source_generated_stack_sha256": source_provenance["generated_stack_sha256"],
            "source_files": {
                "optical_model": source_provenance["optical_model_path"],
                "generated_stack": source_provenance["generated_stack_path"],
            },
            "structural_evidence": copy.deepcopy(evidence),
            "source_evidence_sha256": evidence["manifest_sha256"],
            "solver_sha256": hashlib.sha256(
                Path(physical_estimate.__file__).read_bytes()
            ).hexdigest(),
            "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "derived_geometry_sha256": digest(geometry),
            "geometry_policy": (
                "native pitch; fixed total height; CFA replaces passivation per channel"
            ),
            "electrical_basis": "fixed shared readout assumptions, not derived from layers",
            "qe_contract": "absolute collected electrons per sensor-plane incident photon",
            "excluded": ["OCL routing", "DTI", "spatial crosstalk", "TCAD collection"],
            "limitations": [
                "Structural reference at original pitch; not calibrated product "
                "optical/electrical performance.",
                "CFA n,k, per-color thickness assignment and polymer/passivation "
                "split remain proxies.",
                "Reported CFA 0.45–0.55 um observations are not a confidence interval "
                "or a color assignment.",
                "Incoherent planar Fresnel/Snell/Beer-Lambert model; no thin-film phase "
                "or resolved OCL/DTI.",
                "Active-silicon absorption is multiplied by fixed collection 0.75.",
                "No lens throughput, extra CFA, saved QE or second receiving aperture "
                "is applied here.",
                "Response is azimuth-independent; this is not a measured sensor MTF "
                "or product ranking.",
            ],
        },
    }
    # Existing absolute-response validation uses the canonical compact digest.
    cell["cell_sha256"] = _pixel._digest(cell)
    return cell


def _origin(value):
    array = np.asarray(value)
    if array.shape != (2,) or not np.isfinite(array.astype(float)).all():
        raise ValueError("CFA origin requires two finite integer coordinates")
    if not np.equal(array, array.astype(int)).all():
        raise ValueError("CFA origin requires two finite integer coordinates")
    return array.astype(int)


def _site_mask(shape, origin):
    row, col = np.indices(shape)
    return ((row + origin[0]) % 2) * 2 + ((col + origin[1]) % 2)


def build_lsc(field_rates, center_id="center", max_gain=4.0, *, cfa_origins=None, provenance=None):
    """Freeze maps from baseline noise-free uniform rates in electrons/pixel/s.

    Each native sample is normalized to the *same CFA site's* mean at the
    baseline center. A dim red site is not normalized to a brighter green site.
    Dark/nonpositive calibration samples retain gain 1 and an explicit invalid
    mask; they do not receive an invented gain or a candidate-derived repair.
    """
    if not isinstance(field_rates, dict) or center_id not in field_rates:
        raise ValueError("LSC requires a mapping including the baseline center field")
    max_gain = float(max_gain)
    if not np.isfinite(max_gain) or not 1 <= max_gain <= 4:
        raise ValueError("LSC maximum gain must be finite in [1,4]")
    if any(not isinstance(key, str) or not key for key in field_rates):
        raise ValueError("LSC field identifiers must be nonempty strings")
    origins, rates, site_masks = {}, {}, {}
    cfa_origins = {} if cfa_origins is None else cfa_origins
    if not isinstance(cfa_origins, dict) or set(cfa_origins) - set(field_rates):
        raise ValueError("CFA origins must refer only to supplied baseline fields")
    for key, value in field_rates.items():
        array = np.asarray(value, dtype=np.float64)
        if (
            array.ndim != 2
            or min(array.shape) < 2
            or not np.isfinite(array).all()
            or np.any(array < 0)
        ):
            raise ValueError("LSC uniform rates require finite nonnegative 2D native arrays")
        origins[key] = _origin(cfa_origins.get(key, (0, 0)))
        rates[key] = array
        site_masks[key] = _site_mask(array.shape, origins[key])
    center = rates[center_id]
    center_means = np.array([np.mean(center[site_masks[center_id] == site]) for site in range(4)])
    if np.any(center_means <= 0):
        raise ValueError("All four baseline center CFA sites require positive mean signal")
    result = {
        "schema": LSC_SCHEMA,
        "center_id": center_id,
        "sites": list(SITES),
        "max_gain": max_gain,
        "cfa_pattern": "RGGB",
        "rate_units": "electrons per native pixel per second",
        "center_site_mean_rate": dict(zip(SITES, center_means.tolist(), strict=True)),
        "gain_maps": {},
        "valid_masks": {},
        "field_statistics": {},
        "cfa_origins": {key: value.tolist() for key, value in origins.items()},
        "source_rate_sha256": {
            key: hashlib.sha256(np.ascontiguousarray(value, dtype="<f8").tobytes()).hexdigest()
            for key, value in rates.items()
        },
        "provenance": copy.deepcopy(provenance or {}),
        "policy": {
            "normalization": (
                "same-site baseline center mean divided by baseline uniform native rate"
            ),
            "candidate_refit": False,
            "source_input": (
                "baseline noise-free uniform electron rate before dark/noise/gain/clipping"
            ),
            "application": "signed black-subtracted RAW before demosaic; no clipping",
            "invalid_gain": 1.0,
            "variance_rule": "multiply input independent-sample variance by gain squared",
            "calibration_uncertainty": "not modeled; noiseless model-derived calibration",
        },
    }
    for key, rate in rates.items():
        valid = rate > 0
        ideal = np.divide(center_means[site_masks[key]], rate, out=np.ones_like(rate), where=valid)
        gains = np.minimum(ideal, max_gain)
        result["gain_maps"][key] = gains.tolist()
        result["valid_masks"][key] = valid.tolist()
        result["field_statistics"][key] = {
            "shape_row_col": list(rate.shape),
            "invalid_fraction": float(np.mean(~valid)),
            "capped_fraction": float(np.mean(valid & (ideal > max_gain))),
            "site_mean_rate": {
                site: float(np.mean(rate[site_masks[key] == i])) for i, site in enumerate(SITES)
            },
            "site_mean_gain": {
                site: float(np.mean(gains[site_masks[key] == i])) for i, site in enumerate(SITES)
            },
        }
    result["lsc_sha256"] = digest(result)
    return result


def lsc_gain_map(calibration, field_id, *, shape=None, crop_origin=(0, 0)):
    """Validate an immutable calibration and select the exact native field map."""
    if calibration.get("schema") != LSC_SCHEMA or calibration.get("lsc_sha256") != digest(
        {key: val for key, val in calibration.items() if key != "lsc_sha256"}
    ):
        raise ValueError("Fixed LSC calibration hash mismatch; cannot reuse altered maps")
    if field_id not in calibration["gain_maps"]:
        raise ValueError("No frozen LSC map exists for this field")
    if not np.array_equal(_origin(crop_origin), calibration["cfa_origins"][field_id]):
        raise ValueError("RAW CFA origin differs from the frozen LSC field origin")
    gains = np.asarray(calibration["gain_maps"][field_id], float)
    if shape is not None and tuple(shape) != gains.shape:
        raise ValueError("RAW shape differs from the frozen native LSC map")
    return gains


def apply_lsc(raw_dn, calibration, field_id, *, black_level_dn, crop_origin=(0, 0)):
    """Return signed black-subtracted DN × fixed gains, without RAW mutation."""
    raw = np.asarray(raw_dn)
    if raw.ndim != 2 or not np.isfinite(raw).all():
        raise ValueError("RAW must be a finite 2D native mosaic")
    black = np.asarray(black_level_dn, float)
    if black.ndim not in (0, 2) or (black.ndim == 2 and black.shape != raw.shape):
        raise ValueError("Black level must be scalar or exactly match the native RAW map")
    if not np.isfinite(black).all():
        raise ValueError("Black level must be finite")
    gains = lsc_gain_map(calibration, field_id, shape=raw.shape, crop_origin=crop_origin)
    return (raw.astype(float) - black) * gains
