"""CFA-thickness research cells using CameraE2E's existing planar physics.

The absolute response replaces the sensor QE. It is not an additional filter on
a saved product QE. Spatial pixel aperture integration belongs to the optical
adapter; these functions never apply pixel area or a second receiving aperture.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np

CELL_SCHEMA = "camera_design_workshop_research_cell_v1"
RESPONSE_SCHEMA = "camera_design_workshop_absolute_qe_v1"
SITES = ["R", "Gr", "Gb", "B"]
SCALES = (0.9, 1.0, 1.1)
SOURCE_REPORT = "DEF-2011-804"
PITCH_UM = 3.45


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _axis(values, name, low, high, *, normal=False):
    axis = np.asarray(values, dtype=float)
    if (
        axis.ndim != 1
        or not 1 <= axis.size <= 256
        or not np.isfinite(axis).all()
        or np.any(np.diff(axis) <= 0)
        or axis[0] < low
        or axis[-1] > high
        or (normal and axis[0] != 0)
    ):
        raise ValueError(f"{name} requires an increasing finite axis in [{low}, {high}]")
    return axis


def _cross_section(geometry):
    layers, depth = [], 0.0
    for name, key, material in (
        ("fixed_top_polymer", "lens_height", "lens polymer planar equivalent"),
        ("cfa", "cfa_thickness", "separate red / green / blue material proxies"),
        ("passivation", "passivation_thickness", "lumped passivation proxy"),
        ("active_silicon", "si_thickness", "silicon with nonreflecting continuation"),
    ):
        thickness = float(geometry[key])
        layers.append(
            {
                "name": name,
                "material": material,
                "thickness_um": thickness,
                "depth_start_um": depth,
                "depth_end_um": depth + thickness,
                "varied": name in {"cfa", "passivation"},
            }
        )
        depth += thickness
    return {
        "coordinate": "depth below top polymer surface; positive toward silicon",
        "pixel_pitch_um": float(geometry["pitch"]),
        "bayer_sites": [["R", "Gr"], ["Gb", "B"]],
        "layers": layers,
        "top_stack_height_um": sum(
            float(geometry[k]) for k in ("lens_height", "cfa_thickness", "passivation_thickness")
        ),
        "scope": "planar layer equivalent; no resolved OCL shape, DTI or photodiode geometry",
    }


def build_cell(workspace, cfa_thickness_scale=1.0):
    """Clone canonical layer/material proxies into a hash-pinned research cell.

    The canonical IMX462 source pitch is 2.9 um. The workshop deliberately uses
    3.45 um for every variant: this is a derived research cell, not that product.
    CFA and passivation exchange thickness; the lens plane and silicon top stay
    fixed. No source DB file is modified.
    """
    from pyisetcam import physical_estimate

    from .electrical import default_electrical_model

    scale = float(cfa_thickness_scale)
    if not np.isfinite(scale) or scale not in SCALES:
        raise ValueError("CFA thickness scale must be one of 0.9, 1.0, 1.1")
    source = physical_estimate.image_sensor_physical_model(
        SOURCE_REPORT,
        root=Path(workspace) / "camerae2e_db/fdtd_tcad/image_sensor_db",
        collection_efficiency=0.75,
        optical_assumptions={
            "geometry_source": "canonical_db",
            "microlens_enabled": False,
            "routing_mode": "no_routing_loss",
        },
    )
    model = copy.deepcopy(source)
    geometry = model["geometry_um"]
    original = copy.deepcopy(geometry)
    if "cfa_thickness_by_channel" in geometry or "passivation_thickness_by_channel" in geometry:
        raise ValueError("Workshop baseline changed to per-channel thickness; review the cell")
    total = float(geometry["cfa_thickness"]) + float(geometry["passivation_thickness"])
    geometry["cfa_thickness"] = float(geometry["cfa_thickness"]) * scale
    geometry["passivation_thickness"] = total - geometry["cfa_thickness"]
    if geometry["passivation_thickness"] <= 0:
        raise ValueError("CFA change leaves no positive passivation thickness")
    geometry["pitch"] = PITCH_UM
    model["pixel_pitch_um"] = PITCH_UM
    model["wavelength_nm"] = np.arange(450.0, 651.0, 10.0).tolist()
    model["eqe"] = physical_estimate.physical_optical_response(model, model["wavelength_nm"])[
        "eqe"
    ].tolist()
    model["provenance"]["effective_geometry_sha256"] = _digest(geometry)
    model["provenance"]["workshop_derived_cell"] = True
    # Avoid carrying the generic solver's contradictory independent-height warning.
    model["provenance"]["limitations"] = [
        text
        for text in model["provenance"]["limitations"]
        if not text.startswith("Independent geometry perturbations")
    ]
    material = source["material_bundle"]
    source_provenance = material["provenance"]
    readout = copy.deepcopy(source["readout"])
    cell = {
        "schema": CELL_SCHEMA,
        "id": f"research-rggb-cfa-{scale:.1f}",
        "label": f"3.45 um RGB research cell / CFA {scale:.1f}x",
        "source_report": SOURCE_REPORT,
        "product_model": False,
        "cfa_thickness_scale": scale,
        "pixel_pitch_um": PITCH_UM,
        "cfa_pattern": "RGGB",
        "sites": SITES.copy(),
        "geometry_um": copy.deepcopy(geometry),
        "cross_section": _cross_section(geometry),
        "collection_efficiency": 0.75,
        "readout": readout,
        "electrical_model": default_electrical_model(readout),
        "physical_model": model,
        "provenance": {
            "database_modified": False,
            "source_basis": "canonical IMX462 layer geometry with shared material proxies",
            "source_pixel_pitch_um": float(source["pixel_pitch_um"]),
            "source_geometry_um": original,
            "source_model_sha256": _digest(source),
            "source_material_bundle_sha256": material["material_bundle_sha256"],
            "source_geometry_sha256": material["geometry_sha256"],
            "source_optical_model_sha256": source_provenance["optical_model_sha256"],
            "source_generated_stack_sha256": source_provenance["generated_stack_sha256"],
            "source_files": {
                "optical_model": source_provenance["optical_model_path"],
                "generated_stack": source_provenance["generated_stack_path"],
            },
            "solver_sha256": _file_sha(physical_estimate.__file__),
            "adapter_sha256": _file_sha(__file__),
            "derived_geometry_sha256": _digest(geometry),
            "geometry_policy": "fixed top-stack height; CFA increase replaces passivation",
            "electrical_basis": "fixed shared readout assumptions, not derived from layers",
            "qe_contract": "absolute collected electrons per sensor-plane incident photon",
            "excluded": ["OCL routing", "DTI", "spatial crosstalk", "TCAD collection"],
            "limitations": [
                "Research cell; source product pitch is replaced by 3.45 um.",
                "CFA n,k and the polymer/passivation split are uncalibrated proxies.",
                "Incoherent planar Fresnel/Snell/Beer-Lambert model; no thin-film phase.",
                "Fixed polymer layer remains; resolved microlens/OCL action is excluded.",
                "Absorption in active silicon is multiplied by fixed collection 0.75.",
                "No lens throughput, pixel aperture, extra CFA filter or saved QE is applied here.",
                "Response is azimuth-independent; no sensor MTF or product ranking claim.",
            ],
        },
    }
    cell["cell_sha256"] = _digest(cell)
    return cell


def build_response(cell, wavelength_nm=None, theta_deg=None):
    """Return absolute EQE and separate optical/collection loss budgets.

    Arrays use [theta, wavelength, R/Gr/Gb/B]. Theta is incidence on the sensor
    surface, not scene field angle. The layer model is azimuth-independent.
    """
    from pyisetcam.physical_estimate import physical_optical_response

    if cell.get("schema") != CELL_SCHEMA or cell.get("cell_sha256") != _digest(
        {key: value for key, value in cell.items() if key != "cell_sha256"}
    ):
        raise ValueError("Research cell hash mismatch; build a new immutable revision")
    wave = _axis(
        np.arange(450.0, 651.0, 10.0) if wavelength_nm is None else wavelength_nm,
        "wavelength_nm",
        450,
        650,
    )
    theta = _axis(
        np.arange(0.0, 61.0, 5.0) if theta_deg is None else theta_deg,
        "theta_deg",
        0,
        60,
        normal=True,
    )
    budget = physical_optical_response(
        cell["physical_model"], wave[None, :], theta_deg=theta[:, None], phi_deg=0
    )
    silicon = budget["layer_absorption"]["silicon"]
    response = {
        "schema": RESPONSE_SCHEMA,
        "cell_sha256": cell["cell_sha256"],
        "wavelength_nm": wave.tolist(),
        "theta_deg": theta.tolist(),
        "sites": SITES.copy(),
        "array_axes": ["theta_deg", "wavelength_nm", "site"],
        "qe_units": "electrons per sensor-plane incident photon",
        "eqe": budget["eqe"].tolist(),
        "normal_qe": budget["eqe"][0].tolist(),
        "reflection": budget["R"].tolist(),
        "uncollected_deep_transmission": budget["T"].tolist(),
        "layer_absorption": {key: val.tolist() for key, val in budget["layer_absorption"].items()},
        "collection_loss": (silicon * (1 - cell["collection_efficiency"])).tolist(),
        "energy_residual": budget["energy_residual"].tolist(),
        "cross_section": copy.deepcopy(cell["cross_section"]),
        "provenance": {
            **copy.deepcopy(cell["provenance"]),
            "qe_application": "absolute replacement; no multiplication by saved DB QE",
            "angular_model": "planar unpolarized; no azimuth variation",
            "angular_weights": "conditional sensor-plane incident-photon fractions",
            "interpolation": "bilinear wavelength/theta; no extrapolation",
            "pixel_aperture_owner": "optical adapter; already integrated receiving square",
            "electrical_model_sha256": _digest(cell["electrical_model"]),
        },
    }
    response["table_sha256"] = _digest(response)
    return response


def _indices(axis, query):
    upper = np.clip(np.searchsorted(axis, query, side="right"), 1, max(1, len(axis) - 1))
    if len(axis) == 1:
        return np.zeros(query.shape, int), np.zeros(query.shape, int), np.zeros(query.shape)
    lower = upper - 1
    weight = (query - axis[lower]) / (axis[upper] - axis[lower])
    return lower, upper, weight


def interpolate_qe(response, theta_deg, wavelength_nm):
    """Absolute EQE for broadcast theta/wavelength queries, returning (...,4)."""
    if response.get("schema") != RESPONSE_SCHEMA:
        raise ValueError("Expected a workshop absolute QE response")
    wave = np.asarray(response["wavelength_nm"], float)
    angles = np.asarray(response["theta_deg"], float)
    theta, query_wave = np.broadcast_arrays(
        np.asarray(theta_deg, float), np.asarray(wavelength_nm, float)
    )
    if (
        not np.isfinite(theta).all()
        or not np.isfinite(query_wave).all()
        or np.any(theta < angles[0])
        or np.any(theta > angles[-1])
        or np.any(query_wave < wave[0])
        or np.any(query_wave > wave[-1])
    ):
        raise ValueError("QE query outside declared wavelength/angle domain; no extrapolation")
    values = np.asarray(response["eqe"], float)
    if values.shape != (len(angles), len(wave), 4) or not np.isfinite(values).all():
        raise ValueError("QE response table shape or values are invalid")
    if np.any((values < 0) | (values > 1)):
        raise ValueError("QE response is outside [0,1]")
    t0, t1, ft = _indices(angles, theta)
    w0, w1, fw = _indices(wave, query_wave)
    ft, fw = ft[..., None], fw[..., None]
    return (1 - ft) * ((1 - fw) * values[t0, w0] + fw * values[t0, w1]) + ft * (
        (1 - fw) * values[t1, w0] + fw * values[t1, w1]
    )


def absolute_angular_response(response, theta_deg, phi_deg, wavelength_nm):
    """Optical adapter callback: absolute site QE, with explicit unused azimuth."""
    theta, phi, wave = np.broadcast_arrays(theta_deg, phi_deg, wavelength_nm)
    if not np.isfinite(phi).all():
        raise ValueError("Azimuth must be finite")
    return interpolate_qe(response, theta, wave)


def relative_angular_response(response, theta_deg, phi_deg, wavelength_nm):
    """Optional normal-QE × relative interface, exactly preserving absolute QE.

    Only pair this with this same response's normal_qe, never with a saved QE.
    Prefer absolute_angular_response where the optical adapter supports it.
    """
    absolute = absolute_angular_response(response, theta_deg, phi_deg, wavelength_nm)
    normal = interpolate_qe(response, 0, wavelength_nm)
    if np.any((normal == 0) & (absolute > 0)):
        raise ValueError("Zero normal QE cannot represent nonzero oblique QE as a ratio")
    return np.divide(absolute, normal, out=np.zeros_like(absolute), where=normal > 0)


def photoelectron_rate_density(
    photons_per_m2_s_nm, response, theta_deg=0.0, *, wavelength_bin_width_nm=None
):
    """Integrate photon irradiance density to electrons/m²/s at four CFA sites.

    Input's last axis follows response wavelengths. Default weights integrate
    piecewise-linear spectra over the declared band (trapezoidal endpoints).
    Explicit bin widths support callers whose samples are spectral-bin averages.
    No spatial pixel area or receiving aperture is applied by this helper.
    """
    photons = np.asarray(photons_per_m2_s_nm, float)
    wave = np.asarray(response["wavelength_nm"], float)
    if photons.ndim < 1 or photons.shape[-1] != len(wave):
        raise ValueError("Photon density must have the response wavelength as its last axis")
    if not np.isfinite(photons).all() or np.any(photons < 0):
        raise ValueError("Photon density must be finite and nonnegative")
    if wavelength_bin_width_nm is None:
        if len(wave) < 2:
            raise ValueError("Single-wavelength density needs an explicit spectral bin width")
        widths = np.zeros_like(wave)
        widths[:-1] += np.diff(wave) / 2
        widths[1:] += np.diff(wave) / 2
    else:
        widths = np.asarray(wavelength_bin_width_nm, float)
        if widths.shape != wave.shape or not np.isfinite(widths).all() or np.any(widths <= 0):
            raise ValueError("Spectral bin widths must be positive finite values for every wave")
    theta = np.broadcast_to(np.asarray(theta_deg, float), photons.shape[:-1])
    qe = interpolate_qe(response, theta[..., None], wave)
    return np.sum(photons[..., None] * qe * widths[:, None], axis=-2)
