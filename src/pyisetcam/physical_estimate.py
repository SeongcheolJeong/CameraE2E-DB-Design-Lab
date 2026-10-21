"""Fast, auditable visible-band sensor optics for camera performance studies.

This is an estimate, not a fit to measured product QE. Incoherent Fresnel power
transport separates CFA/Si absorption from a paraxial microlens own-site overlap
diagnostic and from charge collection. Nominal QE does not discard light that
leaves its original site, because a neighboring pixel may collect it. Silicon
continues beyond the active depth;
an invented Si/air back surface never creates product spectral fringes.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from scipy.special import erf

from .cfa_materials import image_sensor_cfa_materials, interpolate_nk
from .fdtd_estimate import _illumination_config, _incident_directions
from .sensor_capture import _DEFAULT_READOUT, _readout_values
from .types import OpticalImage, Sensor

_KIND = "physical_estimate_v1"
_SITES = ["R", "Gr", "Gb", "B"]
_CHANNELS = ("red", "green", "green", "blue")
_WAVE = np.arange(400.0, 701.0, 10.0)
_MAX_ANGLE = 60.0


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Physical estimate {name} must be finite.") from exc
    if not np.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"Physical estimate {name} must be finite and valid.")
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _assumptions(value: Mapping[str, Any] | None, geometry: Mapping[str, Any]) -> dict[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("optical_assumptions must be a mapping.")
    given = dict(value or {})
    defaults = {
        "geometry_source": "reviewed_techinsights",
        "microlens_enabled": True,
        "routing_mode": "no_routing_loss",
        "cfa_thickness_scale": 1.0,
        "cfa_k_scale": 1.0,
        "cfa_n_offset": 0.0,
        "lens_height_scale": 1.0,
        "lens_shift_um": [0.0, 0.0],
        "acceptance_width_fraction": geometry.get("acceptance_width_fraction", (
            1 - 2 * float(geometry.get("metal_edge_width", 0)) / float(geometry["pitch"])
        )),
        "diffraction_scale": 1.0,
    }
    unknown = set(given) - set(defaults)
    if unknown:
        raise ValueError(f"Unknown physical optical assumptions: {sorted(unknown)}")
    result = {**defaults, **copy.deepcopy(given)}
    if result["geometry_source"] not in {"reviewed_techinsights", "canonical_db"}:
        raise ValueError("geometry_source must be reviewed_techinsights or canonical_db.")
    if not isinstance(result["microlens_enabled"], bool):
        raise ValueError("microlens_enabled must be boolean.")
    if result["routing_mode"] not in {"no_routing_loss", "own_site_lower_bound"}:
        raise ValueError("routing_mode must be no_routing_loss or own_site_lower_bound.")
    for key in ("cfa_thickness_scale", "lens_height_scale", "diffraction_scale"):
        result[key] = _number(result[key], key, positive=True)
    result["cfa_n_offset"] = _number(result["cfa_n_offset"], "cfa_n_offset")
    scales = result["cfa_k_scale"]
    if not isinstance(scales, Mapping):
        scales = dict.fromkeys(("red", "green", "blue"), scales)
    if set(scales) != {"red", "green", "blue"}:
        raise ValueError("cfa_k_scale mapping requires red, green, blue.")
    result["cfa_k_scale"] = {
        key: _number(val, f"cfa_k_scale.{key}", positive=True) for key, val in scales.items()
    }
    fraction = _number(result["acceptance_width_fraction"], "acceptance_width_fraction")
    if not 0 < fraction <= 1:
        raise ValueError("acceptance_width_fraction must be in (0,1].")
    result["acceptance_width_fraction"] = fraction
    shift = np.asarray(result["lens_shift_um"], dtype=float)
    if shift.shape != (2,) or not np.all(np.isfinite(shift)):
        raise ValueError("lens_shift_um must contain finite x,y displacement in micrometres.")
    if np.any(np.abs(shift) > float(geometry["pitch"]) / 4):
        raise ValueError("Paraxial lens shift is limited to one quarter of a pixel pitch.")
    result["lens_shift_um"] = shift.tolist()
    result["acceptance_source"] = (
        "explicit_scenario_assumption"
        if "acceptance_width_fraction" in given
        else geometry.get("acceptance_source",
                          "inferred_generated_metal_edge_proxy_not_measured_photodiode_fill_factor")
    )
    return result


def incoherent_stack_response(
    wavelength_nm: Any,
    layers: Sequence[Mapping[str, Any]],
    *,
    incident_n: float = 1.0,
    exit_n: Any | None = None,
    angle_deg: Any = 0.0,
    polarization: str = "unpolarized",
) -> dict[str, Any]:
    """Incoherent planar power transport, with all internal intensity reflections.

    A layer has ``n``, ``k`` and ``thickness_um`` (scalars or wavelength arrays).
    Real-n Snell/Fresnel boundaries and alpha=4*pi*k/lambda propagation are an
    explicit weak-boundary-loss approximation; k does not enter Fresnel here.
    No phase or coherent interference is retained. ``exit_n=None`` continues
    the final layer, so light transmitted through active Si is uncollected
    deeper absorption, rather than transmission through a fabricated air back.
    Returns arrays broadcast over wavelength and angle, and a layer-last A.
    """
    wave = np.asarray(wavelength_nm, dtype=float)
    angle = np.asarray(angle_deg, dtype=float)
    wave, angle = np.broadcast_arrays(wave, angle)
    if (
        wave.size == 0
        or not np.all(np.isfinite(wave))
        or np.any(wave <= 0)
        or not np.all(np.isfinite(angle))
        or np.any((angle < 0) | (angle >= 90))
    ):
        raise ValueError("Incoherent stack needs positive wavelengths and angles in [0,90).")
    n_in = _number(incident_n, "incident_n", positive=True)
    if not layers:
        raise ValueError("Incoherent stack requires at least one layer.")
    thicknesses = [_number(layer["thickness_um"], "layer thickness") for layer in layers]
    if any(value < 0 for value in thicknesses):
        raise ValueError("Incoherent layer thickness must be nonnegative.")
    # A zero-width medium has no boundary pair. Collapse it explicitly rather
    # than applying a phase-averaged thick-layer formula to zero thickness.
    retained = [i for i, value in enumerate(thicknesses) if value > 0]
    if len(retained) != len(layers):
        terminal_n = layers[-1]["n"] if exit_n is None else exit_n
        if retained:
            result = incoherent_stack_response(
                wave,
                [layers[i] for i in retained],
                incident_n=n_in,
                exit_n=terminal_n,
                angle_deg=angle,
                polarization=polarization,
            )
            expanded = np.zeros((*wave.shape, len(layers)))
            expanded[..., retained] = result["layer_absorption"]
            return {**result, "layer_absorption": expanded}
        # A dummy zero-loss matched entrance region leaves only the required
        # physical entrance/continuation interface, and has zero absorption.
        result = incoherent_stack_response(
            wave,
            [{"n": n_in, "k": 0, "thickness_um": 1}],
            incident_n=n_in,
            exit_n=terminal_n,
            angle_deg=angle,
            polarization=polarization,
        )
        return {**result, "layer_absorption": np.zeros((*wave.shape, len(layers)))}
    if polarization == "unpolarized":
        parts = [
            incoherent_stack_response(
                wave,
                layers,
                incident_n=n_in,
                exit_n=exit_n,
                angle_deg=angle,
                polarization=pol,
            )
            for pol in ("s", "p")
        ]
        return {
            key: (parts[0][key] + parts[1][key]) / 2
            for key in ("R", "T", "A", "layer_absorption", "energy_residual")
        }
    if polarization not in {"s", "p"}:
        raise ValueError("Incoherent stack polarization must be s, p or unpolarized.")
    indices, attenuation = [np.full(wave.shape, n_in)], [np.ones(wave.shape)]
    transverse = n_in * np.sin(np.deg2rad(angle))
    for layer in layers:
        n, k = np.broadcast_arrays(
            np.asarray(layer["n"], float), np.asarray(layer.get("k", 0), float)
        )
        n, k, _ = np.broadcast_arrays(n, k, wave)
        thickness = _number(layer["thickness_um"], "layer thickness")
        if (
            np.any(~np.isfinite(n))
            or np.any(~np.isfinite(k))
            or np.any(n <= 0)
            or np.any(k < 0)
            or thickness < 0
            or np.any(n <= transverse)
        ):
            raise ValueError("Incoherent layers require passive n,k and propagating Snell rays.")
        cosine = np.sqrt(1 - (transverse / n) ** 2)
        indices.append(n)
        attenuation.append(np.exp(-4 * np.pi * k * thickness / (wave / 1000 * cosine)))
    output_n = (
        indices[-1] if exit_n is None else np.broadcast_to(np.asarray(exit_n, float), wave.shape)
    )
    if np.any(~np.isfinite(output_n)) or np.any(output_n <= transverse):
        raise ValueError("Incoherent exit medium must support a propagating ray.")
    indices.append(output_n)
    attenuation.append(np.ones(wave.shape))
    q = [np.sqrt(n * n - transverse * transverse) for n in indices]
    admittance = (
        q if polarization == "s" else [n * n / qi for n, qi in zip(indices, q, strict=True)]
    )
    interface = [
        ((a - b) / (a + b)) ** 2 for a, b in zip(admittance[:-1], admittance[1:], strict=True)
    ]
    effective = [np.zeros(wave.shape) for _ in indices]
    for j in range(len(interface) - 1, -1, -1):
        returned = attenuation[j + 1] ** 2 * effective[j + 1]
        effective[j] = interface[j] + (1 - interface[j]) ** 2 * returned / (
            1 - interface[j] * returned
        )
    forward, absorption = np.ones(wave.shape), []
    for j, reflectance in enumerate(interface):
        returned = attenuation[j + 1] ** 2 * effective[j + 1]
        entering = forward * (1 - reflectance) / (1 - reflectance * returned)
        if j < len(layers):
            a = attenuation[j + 1]
            absorption.append(entering * (1 - a) * (1 + a * effective[j + 1]))
            forward = entering * a
        else:
            transmission = entering
    layer_a = np.stack(absorption, axis=-1)
    total_a = np.sum(layer_a, axis=-1)
    residual = 1 - effective[0] - transmission - total_a
    if (
        not np.all(np.isfinite(layer_a))
        or np.any(layer_a < -1e-12)
        or np.max(np.abs(residual)) > 1e-10
    ):
        raise ArithmeticError("Incoherent stack failed passive energy conservation.")
    return {
        "R": effective[0],
        "T": transmission,
        "A": total_a,
        "layer_absorption": layer_a,
        "energy_residual": residual,
    }


def image_sensor_physical_model(
    sensor_id: str,
    *,
    root: str | Path | None = None,
    collection_efficiency: float = 0.75,
    optical_assumptions: Mapping[str, Any] | None = None,
    readout: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an uncalibrated RGB Bayer model from the existing canonical DB.

    Collection efficiency is the probability of collecting a carrier *after*
    optical absorption in active Si, exactly the meaning used by FDTD models.
    Microlens overlap uses inferred geometry and is a separate diagnostic by
    default. ``routing_mode='own_site_lower_bound'`` applies it to QE only as
    an explicit pessimistic sensitivity scenario excluding neighbor collection.
    """
    from .sensor_techinsights import _reviewed_material_bundle

    bundle = image_sensor_cfa_materials(sensor_id, root=root)
    # Validate before applying an overlay; explicit canonical_db reproduces the
    # previous physical estimate and leaves frozen FDTD bundles unaffected.
    requested = _assumptions(optical_assumptions, bundle["geometry_um"])
    if requested["geometry_source"] == "reviewed_techinsights":
        bundle = _reviewed_material_bundle(bundle, root=root)
    if readout is not None and not isinstance(readout, Mapping):
        raise ValueError("Physical estimate readout must be a mapping.")
    if str(bundle.get("cfa_pattern", "")).lower() not in {"bayer", "rggb"}:
        raise ValueError("Physical estimate v1 supports canonical RGB Bayer sensors only.")
    efficiency = _number(collection_efficiency, "collection_efficiency")
    if not 0 < efficiency <= 1:
        raise ValueError("collection_efficiency must be in (0,1].")
    geometry = copy.deepcopy(bundle["geometry_um"])
    assumptions = _assumptions(optical_assumptions, geometry)
    geometry["lens_height"] *= assumptions["lens_height_scale"]
    if "lens_vertex_radius_um" in geometry:
        geometry["lens_vertex_radius_um"] /= assumptions["lens_height_scale"]
    geometry["cfa_thickness"] *= assumptions["cfa_thickness_scale"]
    if "cfa_thickness_by_channel" in geometry:
        geometry["cfa_thickness_by_channel"] = {
            key: value * assumptions["cfa_thickness_scale"]
            for key, value in geometry["cfa_thickness_by_channel"].items()
        }
    radius = geometry["pitch"] / 2 - geometry["lens_edge_gap"]
    if radius <= 0 or geometry["lens_height"] <= 0:
        raise ValueError("Physical microlens requires positive aperture and height.")
    for material in bundle["channels"].values():
        if min(material["n"]) + assumptions["cfa_n_offset"] <= 1:
            raise ValueError("CFA refractive index offset must retain n>1 in visible band.")
    model = {
        "kind": _KIND,
        "sensor_id": bundle["sensor_id"],
        "report_code": bundle["report_code"],
        "estimate_only": True,
        "pixel_pitch_um": bundle["pixel_pitch_um"],
        "cell_pixels": [2, 2],
        "filter_names": _SITES.copy(),
        "wavelength_nm": _WAVE.tolist(),
        "collection_efficiency": efficiency,
        "geometry_um": geometry,
        "material_bundle": bundle,
        "optical_assumptions": assumptions,
        "angular_domain_deg": [0.0, _MAX_ANGLE],
        "readout": _readout_values({**_DEFAULT_READOUT, **dict(readout or {})}),
        "provenance": {
            "material_bundle_sha256": bundle["material_bundle_sha256"],
            "canonical_geometry_sha256": bundle["provenance"].get(
                "canonical_geometry_sha256", bundle["geometry_sha256"]
            ),
            "reviewed_bundle_geometry_sha256": bundle["geometry_sha256"],
            "effective_geometry_sha256": _digest(geometry),
            "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "model_assumptions_sha256": _digest(assumptions),
            "reviewed_evidence_sha256": bundle.get("techinsights_evidence", {}).get(
                "manifest_sha256"
            ),
            "structural_evidence": bundle.get("techinsights_evidence", {}),
            "applied_structural_facts": bundle["provenance"].get("applied_structural_facts", {}),
            "estimate_only": True,
            "measured_accuracy_verified": False,
            "collection_efficiency_definition": "carriers_collected_per_active_si_absorbed_photon",
            "optical_method": "incoherent_real_n_Fresnel_Snell_Beer_Lambert_multiple_reflections",
            "silicon_back_boundary": "same_index_nonreflecting_continuation",
            "microlens_method": (
                "reported_curvature_or_ellipsoid_proxy_paraxial_Gaussian_aperture_overlap"
            ),
            "routing_mode": assumptions["routing_mode"],
            "microlens_overlap_applied_to_qe": (
                assumptions["routing_mode"] == "own_site_lower_bound"
            ),
            "angular_weight_definition": "normalized_sensor_plane_photon_irradiance_fractions",
            "limitations": [
                "CFA n,k remain shared proxies. Only explicitly cited dimensions and "
                "curvature use reviewed structural measurements.",
                "Fresnel interfaces use real n; k enters attenuation only. "
                "Thin-film coherent phase is averaged out.",
                "Microlens uses paraxial curvature and a Gaussian moment approximation, "
                "not resolved wave optics.",
                "Own-site acceptance loss excludes neighboring recollection; "
                "it does not predict spatial crosstalk or sensor MTF.",
                "Nominal QE applies no unverified routing loss. Microlens overlap is diagnostic; "
                "nominal response does not predict microlens-induced color shading or crosstalk.",
                "own_site_lower_bound is a pessimistic engineering sensitivity scenario, "
                "not a validated physical or statistical lower bound.",
                "Accepting aperture is an equivalent square from reported grid area when "
                "available, otherwise a generated metal-edge proxy; neither is photodiode QE.",
                "Reported AR composition and thickness are retained as evidence only; "
                "coherent AR response, surface pyramids and active DTI optics are unresolved.",
                "Gapless microlens topology does not validate the circular-footprint model; "
                "a reported vertex curvature is not a full surface shape or spatial calibration.",
                "Independent geometry perturbations change optical stack height; "
                "they are sensitivity scenarios.",
                "Response outside 400–700 nm or above 60 degrees is unsupported.",
            ],
        },
    }
    model["eqe"] = physical_response(
        model,
        _WAVE,
        {
            "directions": [{"theta_deg": 0.0, "phi_deg": 0.0, "weight": 1.0}],
        },
    ).tolist()
    return model


def _wave_angle(
    wavelength_nm: Any, theta_deg: Any, phi_deg: Any
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    wave, theta, phi = np.broadcast_arrays(
        np.asarray(wavelength_nm, float),
        np.asarray(theta_deg, float),
        np.asarray(phi_deg, float),
    )
    if (
        wave.size == 0
        or not np.all(np.isfinite(wave))
        or np.any((wave < 400) | (wave > 700))
        or not np.all(np.isfinite(theta))
        or np.any((theta < 0) | (theta > _MAX_ANGLE))
        or not np.all(np.isfinite(phi))
    ):
        raise ValueError(
            "Physical response requires 400–700 nm and angles in [0,60]; no extrapolation."
        )
    return wave, theta, np.mod(phi, 360)


def _microlens_acceptance(
    model: Mapping[str, Any],
    wave: np.ndarray,
    theta: np.ndarray,
    phi: np.ndarray,
    indices: Sequence[np.ndarray],
    silicon_k: np.ndarray,
    thicknesses: Sequence[float] | None = None,
) -> np.ndarray:
    settings, geometry = model["optical_assumptions"], model["geometry_um"]
    if not settings["microlens_enabled"]:
        return np.ones_like(wave)
    pitch, height = float(geometry["pitch"]), float(geometry["lens_height"])
    radius = pitch / 2 - float(geometry["lens_edge_gap"])
    # Upper-half ellipsoid z=h*sqrt(1-r^2/a^2): vertex R=a^2/h.
    curvature = geometry.get("lens_vertex_radius_um", radius**2 / height)
    power = (indices[0] - 1) / curvature
    transverse = np.sin(np.deg2rad(theta))
    cosines = [np.sqrt(1 - (transverse / n) ** 2) for n in indices]
    alpha_z = 4 * np.pi * silicon_k / (wave / 1000 * cosines[-1])
    depth = float(geometry["si_thickness"])
    optical_depth = alpha_z * depth
    # Mean absorption depth of a truncated exponential; transparent limit d/2.
    mean_depth = np.full_like(wave, depth / 2)
    finite_absorption = optical_depth > 1e-5
    a = alpha_z[finite_absorption]
    tau = optical_depth[finite_absorption]
    mean_depth[finite_absorption] = 1 / a - depth * np.exp(-tau) / (-np.expm1(-tau))
    distances = (
        (height, geometry["cfa_thickness"], geometry["passivation_thickness"], mean_depth)
        if thicknesses is None else (*thicknesses[:3], mean_depth)
    )
    reduced_distance = sum(d / n for d, n in zip(distances, indices, strict=True))
    lateral = sum(
        d * transverse / (n * cosine)
        for d, n, cosine in zip(distances, indices, cosines, strict=True)
    )
    # Circular lens aperture second moment a^2/4; diffraction Gaussian matches
    # the central Airy-lobe FWHM (sigma~0.218*lambda*reduced_distance/a).
    defocus = 1 - reduced_distance * power
    diffraction = settings["diffraction_scale"] * 0.218 * (wave / 1000) * reduced_distance / radius
    sigma_lens = np.sqrt((defocus * radius / 2) ** 2 + diffraction**2)
    lens_fraction = np.pi * radius**2 / pitch**2
    gap_variance = (pitch**2 / 12 - lens_fraction * radius**2 / 4) / (1 - lens_fraction)
    sigma_gap = np.sqrt(gap_variance + diffraction**2)
    half_width = settings["acceptance_width_fraction"] * pitch / 2
    sx, sy = settings["lens_shift_um"]
    dx, dy = lateral * np.cos(np.deg2rad(phi)), lateral * np.sin(np.deg2rad(phi))

    def overlap(x: np.ndarray, y: np.ndarray, sigma: np.ndarray) -> np.ndarray:
        scale = np.maximum(sigma, 1e-12) * np.sqrt(2)
        return (
            0.25
            * (erf((half_width - x) / scale) + erf((half_width + x) / scale))
            * (erf((half_width - y) / scale) + erf((half_width + y) / scale))
        )

    lens = overlap(
        dx + reduced_distance * power * sx, dy + reduced_distance * power * sy, sigma_lens
    )
    gap = overlap(dx, dy, sigma_gap)
    return np.clip(lens_fraction * lens + (1 - lens_fraction) * gap, 0, 1)


def physical_optical_response(
    model: Mapping[str, Any],
    wavelength_nm: Any,
    *,
    theta_deg: Any = 0.0,
    phi_deg: Any = 0.0,
) -> dict[str, Any]:
    """Return planar power budgets, own-site overlap diagnostics and chosen EQE.

    Arrays broadcast wavelength/theta/phi inputs and append four native sites.
    ``R+T+sum(layer_absorption)=1`` is the planar budget. Light leaving one site
    can be recollected at another. The nominal response therefore applies no
    routing loss; a four-site redistribution/crosstalk solution is not invented.
    Only explicit ``own_site_lower_bound`` uses inferred overlap as net loss.
    """
    if model.get("kind") != _KIND:
        raise ValueError("physical_optical_response requires physical_estimate_v1.")
    wave, theta, phi = _wave_angle(wavelength_nm, theta_deg, phi_deg)
    bundle, geometry, assumptions = (
        model["material_bundle"],
        model["geometry_um"],
        model["optical_assumptions"],
    )
    results, acceptance = [], []
    for channel in _CHANNELS:
        roles = (
            bundle["materials"]["lens"],
            bundle["channels"][channel],
            bundle["materials"]["passivation"],
            bundle["materials"]["silicon"],
        )
        thicknesses = (
            geometry["lens_height"],
            geometry.get("cfa_thickness_by_channel", {}).get(channel, geometry["cfa_thickness"]),
            geometry.get("passivation_thickness_by_channel", {}).get(
                channel, geometry["passivation_thickness"]
            ),
            geometry["si_thickness"],
        )
        layers = []
        for index, (material, thickness) in enumerate(zip(roles, thicknesses, strict=True)):
            n, k = interpolate_nk(material, wave)
            if index == 1:
                n = n + assumptions["cfa_n_offset"]
                k = k * assumptions["cfa_k_scale"][channel]
            layers.append({"n": n, "k": k, "thickness_um": thickness})
        results.append(incoherent_stack_response(wave, layers, angle_deg=theta))
        acceptance.append(
            _microlens_acceptance(
                model, wave, theta, phi, [layer["n"] for layer in layers], layers[-1]["k"],
                thicknesses,
            )
        )
    budgets = {
        name: np.stack([part[name] for part in results], axis=-1)
        for name in ("R", "T", "A", "energy_residual")
    }
    budgets["layer_absorption"] = {
        name: np.stack([part["layer_absorption"][..., index] for part in results], axis=-1)
        for index, name in enumerate(("lens", "cfa", "passivation", "silicon"))
    }
    budgets["microlens_acceptance"] = np.stack(acceptance, axis=-1)
    budgets["own_site_acceptance"] = budgets["microlens_acceptance"]
    silicon = budgets["layer_absorption"]["silicon"]
    efficiency = float(model["collection_efficiency"])
    budgets["own_site_routing_loss_bound"] = silicon * (1 - budgets["own_site_acceptance"])
    budgets["own_site_eqe_lower_bound"] = silicon * budgets["own_site_acceptance"] * efficiency
    applied = (
        budgets["own_site_acceptance"]
        if assumptions["routing_mode"] == "own_site_lower_bound"
        else np.ones_like(silicon)
    )
    budgets["applied_routing_acceptance"] = applied
    budgets["routing_loss"] = silicon * (1 - applied)
    budgets["accepted_silicon_absorption"] = silicon * applied
    budgets["eqe"] = budgets["accepted_silicon_absorption"] * efficiency
    return budgets


def physical_response(
    model: Mapping[str, Any], wavelength_nm: Any, illumination: Mapping[str, Any]
) -> np.ndarray:
    """Effective per-pixel QE [wave,R/Gr/Gb/B] for normalized incident directions."""
    config = _illumination_config(illumination)
    wave = np.asarray(wavelength_nm, float).reshape(-1)
    zero = np.zeros((1, 1))
    response = np.zeros((wave.size, 4))
    for theta, phi, weight in _incident_directions(config, zero, zero):
        response += physical_optical_response(
            model,
            wave,
            theta_deg=float(theta[0, 0]),
            phi_deg=float(phi[0, 0]),
        )["eqe"] * float(weight[0, 0])
    return response


def apply_physical_estimate(
    sensor: Sensor, model: Mapping[str, Any], illumination: Mapping[str, Any]
) -> Sensor:
    """Apply replacement native-site optics; invalidate previous capture caches."""
    if model.get("kind") != _KIND or model.get("estimate_only") is not True:
        raise ValueError("apply_physical_estimate requires an explicit physical estimate model.")
    for name in ("fdtd_sensor", "tcad_sensor"):
        value = sensor.fields.get(name)
        if value is not None and (not isinstance(value, Mapping) or value.get("enabled", True)):
            raise ValueError(f"Physical estimate cannot overlap active {name}.")
    if not sensor.fields.get("mosaic", True):
        raise ValueError("Physical estimate v1 requires a native mosaic sensor.")
    configured = _illumination_config(illumination)
    # Validate explicit directions before a sensor object can be returned.
    physical_response(model, model["wavelength_nm"], configured)
    result = sensor.clone()
    result.data.clear()
    result.metadata.pop("sensor_capture", None)
    result.metadata.pop("sensor_readout_profile", None)
    result.metadata.pop("sensor_simulation_db", None)
    result.fields.pop("e2e_readout_profile", None)
    for key in ("fdtd_estimate", "fdtd_illumination"):
        result.fields.pop(key, None)
    wave, pitch = np.asarray(model["wavelength_nm"], float), float(model["pixel_pitch_um"]) * 1e-6
    result.fields.update(
        wave=wave.copy(),
        spectrum={"wave": wave.copy()},
        filter_spectra=np.asarray(model["eqe"], float),
        filter_names=_SITES.copy(),
        pattern=np.array([[1, 2], [3, 4]], int),
        pixel_qe=np.ones(wave.size),
        ir_filter=np.ones(wave.size),
        etendue=None,
        vignetting=0,
        ocl_group_proxy=None,
        fdtd_sensor={"enabled": False},
        tcad_sensor={"enabled": False},
        physical_estimate=copy.deepcopy(dict(model)),
        physical_illumination=configured,
        e2e_model_readout=copy.deepcopy(model["readout"]),
    )
    result.fields["pixel"].update(
        size_m=np.full(2, pitch),
        width_gap_m=0.0,
        height_gap_m=0.0,
        fill_factor=1.0,
        pd_size_m=np.full(2, pitch),
        pd_position_m=np.zeros(2),
        spectrum={"wave": wave.copy()},
    )
    summary = {
        key: copy.deepcopy(value) for key, value in model.items() if key != "material_bundle"
    }
    summary["illumination"] = configured
    result.metadata["sensor_estimate"] = summary
    return result


def _validate_runtime_optics(sensor: Sensor) -> None:
    model, pixel = sensor.fields["physical_estimate"], sensor.fields["pixel"]
    pitch = float(model["pixel_pitch_um"]) * 1e-6
    for name in ("fdtd_sensor", "tcad_sensor"):
        layer = sensor.fields.get(name)
        if layer is not None and (not isinstance(layer, Mapping) or layer.get("enabled", True)):
            raise ValueError(f"Physical estimate cannot overlap active {name} during capture.")
    if sensor.fields.get("fdtd_estimate") is not None:
        raise ValueError("Physical estimate cannot overlap a spectral FDTD model.")
    if (
        not sensor.fields.get("mosaic", True)
        or not np.array_equal(sensor.fields["pattern"], [[1, 2], [3, 4]])
        or sensor.fields["filter_names"] != _SITES
        or any(
            not np.allclose(np.asarray(pixel[key]), pitch, rtol=1e-9, atol=0)
            for key in ("size_m", "pd_size_m")
        )
        or float(pixel["fill_factor"]) != 1
        or float(pixel.get("width_gap_m", 0)) != 0
        or float(pixel.get("height_gap_m", 0)) != 0
        or np.any(np.asarray(pixel.get("pd_position_m", [0, 0])) != 0)
    ):
        raise ValueError(
            "Physical capture must retain the model's native CFA and pixel optical area."
        )
    if (
        not np.array_equal(sensor.fields["filter_spectra"], model["eqe"])
        or not np.array_equal(sensor.fields["wave"], model["wavelength_nm"])
        or any(
            not np.all(np.asarray(sensor.fields[name], float) == 1)
            for name in ("pixel_qe", "ir_filter")
        )
        or sensor.fields.get("ocl_group_proxy") is not None
        or sensor.fields.get("vignetting", 0) != 0
        or (
            sensor.fields.get("etendue") is not None
            and not np.all(np.asarray(sensor.fields["etendue"]) == 1)
        )
    ):
        raise ValueError(
            "Physical capture cannot compound or replace generic spectral/angular factors."
        )


def physical_estimate_current_density(oi: OpticalImage, sensor: Sensor) -> np.ndarray:
    """Integrate spectral/angle-dependent QE before charge/readout, in A/m²."""
    from .optics import oi_get
    from .sensor import _ELEMENTARY_CHARGE_C

    _validate_runtime_optics(sensor)
    photons, wave = np.asarray(oi.data["photons"], float), np.asarray(oi.fields["wave"], float)
    if (
        photons.ndim != 3
        or photons.shape[2] != wave.size
        or not np.all(np.isfinite(photons))
        or np.any(photons < 0)
        or wave.size == 0
        or np.any(np.diff(wave) <= 0)
    ):
        raise ValueError(
            "Physical illumination requires finite nonnegative spectral photon irradiance."
        )
    model, illumination = sensor.fields["physical_estimate"], sensor.fields["physical_illumination"]
    rows, cols = photons.shape[:2]
    widths = np.gradient(wave) if wave.size > 1 else np.ones_like(wave)
    if illumination["mode"] == "weighted_directions":
        # Uniform angular distributions need only one spectral evaluation, not
        # an expensive repeated optical calculation for every image pixel.
        qe = physical_response(model, wave, illumination)
        return np.einsum("ijw,wc,w->ijc", photons, qe, widths) * _ELEMENTARY_CHARGE_C
    xs = (np.arange(cols) - (cols - 1) / 2) * float(oi_get(oi, "wspatialresolution"))
    ys = (np.arange(rows) - (rows - 1) / 2) * float(oi_get(oi, "hspatialresolution"))
    x, y = np.meshgrid(xs, ys)
    directions = _incident_directions(illumination, x, y)
    rate = np.zeros((rows, cols, 4))
    for wi, wavelength in enumerate(wave):
        qe = np.zeros_like(rate)
        for theta, phi, weight in directions:
            qe += (
                physical_optical_response(model, wavelength, theta_deg=theta, phi_deg=phi)["eqe"]
                * weight[..., None]
            )
        rate += photons[:, :, wi, None] * widths[wi] * qe
    return rate * _ELEMENTARY_CHARGE_C
