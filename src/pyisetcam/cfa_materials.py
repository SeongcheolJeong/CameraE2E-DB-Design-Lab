"""Canonical CFA material bundles and coherent planar optical references.

CFA n,k come from each report's optical_qe_db, never the older FDTD CSV
proxies. Geometry and materials remain estimates unless individually sourced
otherwise. Planar Fresnel calculations verify numerical consistency, not
measured product QE or curved-microlens performance.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any

import numpy as np


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _number(value: Any, name: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite physical number.") from exc
    if not np.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(
            f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}."
        )
    return number


def _table(material: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    try:
        wave, n, k = (np.asarray(material[key], dtype=float) for key in ("wavelength_nm", "n", "k"))
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Material requires wavelength_nm, n and k arrays.") from exc
    if (
        wave.ndim != 1
        or wave.size < 2
        or n.shape != wave.shape
        or k.shape != wave.shape
        or not np.all(np.isfinite(wave))
        or not np.all(np.isfinite(n))
        or not np.all(np.isfinite(k))
        or np.any(wave <= 0)
        or np.any(np.diff(wave) <= 0)
        or np.any(n <= 0)
        or np.any(k < 0)
    ):
        raise ValueError(
            "Material table requires ordered positive wavelengths/n and finite passive k>=0."
        )
    return wave, n, k


def interpolate_nk(material: Mapping[str, Any], wavelength_nm: Any) -> tuple[Any, Any]:
    """Linearly interpolate complex-index components with strict spectral bounds."""
    wave, n, k = _table(material)
    target = np.asarray(wavelength_nm, dtype=float)
    if (
        target.size == 0
        or not np.all(np.isfinite(target))
        or np.any(target < wave[0])
        or np.any(target > wave[-1])
    ):
        raise ValueError(
            f"Requested wavelengths must lie in material range [{wave[0]}, {wave[-1]}] nm."
        )
    return np.interp(target, wave, n), np.interp(target, wave, k)


def complex_refractive_index(material: Mapping[str, Any], wavelength_nm: Any) -> Any:
    """Return passive n+ik using exp(+i*k_z*z-i*omega*t) convention."""
    n, k = interpolate_nk(material, wavelength_nm)
    return n + 1j * k


def absorption_coefficient_per_um(material: Mapping[str, Any], wavelength_nm: Any) -> Any:
    """Return intensity attenuation alpha=4*pi*k/lambda in inverse micrometres."""
    _, k = interpolate_nk(material, wavelength_nm)
    return 4 * np.pi * k / (np.asarray(wavelength_nm, dtype=float) / 1000)


def absorption_only_transmission(
    material: Mapping[str, Any],
    wavelength_nm: Any,
    thickness_um: float,
) -> Any:
    """Normal-path Beer–Lambert transmission, excluding interface reflection."""
    thickness = _number(thickness_um, "thickness_um", allow_zero=True)
    return np.exp(-absorption_coefficient_per_um(material, wavelength_nm) * thickness)


def _material_identity(material: dict[str, Any]) -> dict[str, Any]:
    wave, n, k = _table(material)
    material.update(wavelength_nm=wave.tolist(), n=n.tolist(), k=k.tolist())
    material["material_sha256"] = _digest(
        {key: material[key] for key in ("wavelength_nm", "n", "k")}
    )
    return material


def image_sensor_cfa_materials(
    sensor_id: str,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Return an auditable, JSON-safe material/geometry bundle for a DB report.

    Native CFA sampling is retained. CFA thickness uses the optical DB's
    reported representative/range; missing ranges use an explicit +/-20%
    sensitivity assumption. This includes IMX307 0.65 um +/-20%, IMX462 0.50 um
    [0.45,0.55], and IMX662 DEF-2206-805 0.475 um [0.42,0.53]. Their CFA n,k
    are common proxy constants with product-specific thickness, not measurements.
    """
    from .image_sensor_db import image_sensor_db_get
    from .sensor_estimate import _enriched_context, _local_generated_path, _optical_model

    record = image_sensor_db_get(sensor_id, root=root)
    enriched, records = _enriched_context(root)
    raw = records.get(str(record["code"]).lower())
    if raw is None:
        raise ValueError(f"{record['code']}: missing paired enriched record.")
    optical_path, optical = _optical_model(enriched, raw)
    stack_path = _local_generated_path(
        enriched,
        raw.get("generated_files", {}).get("stack_config"),
        "generated_stack_configs",
    )
    if stack_path is None:
        raise FileNotFoundError(
            f"{record['code']}: missing local generated optical stack geometry."
        )
    stack = json.loads(stack_path.read_text(encoding="utf-8"))
    if (
        str(stack.get("calibration_status", {}).get("source_report_code", "")).lower()
        != str(record["code"]).lower()
    ):
        raise ValueError("Generated stack report code does not match selected CFA materials.")
    proxy = optical.get("cfa_proxy_nk", {})
    native_channels = proxy.get("channels", {})
    if not proxy.get("enabled") or not all(
        name in native_channels for name in ("red", "green", "blue")
    ):
        raise ValueError(f"{record['code']}: canonical RGB CFA n,k curves are unavailable.")
    thickness_source = copy.deepcopy(proxy.get("thickness_um", {}))
    thickness = _number(thickness_source.get("value"), "CFA thickness_um")
    cfa = optical.get("optical", {}).get("cfa", {})
    low, high = (cfa.get(key, {}).get("value") for key in ("thickness_min_um", "thickness_max_um"))
    if low is None or high is None:
        low, high = 0.8 * thickness, 1.2 * thickness
        range_source = "assumed_plus_minus_20_percent_sensitivity"
    else:
        low, high = _number(low, "CFA thickness minimum"), _number(high, "CFA thickness maximum")
        range_source = "extracted_range_not_statistical_uncertainty"
    if not low <= thickness <= high:
        raise ValueError("Canonical CFA thickness must lie within its declared range.")
    source_hash = hashlib.sha256(optical_path.read_bytes()).hexdigest()
    channels = {}
    native_wave = None
    for name in ("red", "green", "blue"):
        channel = native_channels[name]
        rows = channel.get("data", [])
        material = _material_identity(
            {
                "wavelength_nm": [row["wavelength_nm"] for row in rows],
                "n": [row["n"] for row in rows],
                "k": [row["k"] for row in rows],
                "thickness_um": thickness,
                "thickness_range_um": [low, high],
                "thickness_source": thickness_source,
                "thickness_range_source": range_source,
                "source_kind": channel.get("source_kind", "inferred_proxy_nk"),
                "source_file": str(optical_path),
                "source_sha256": source_hash,
                "library_id": proxy.get("library_id"),
                "product_measured": False,
            }
        )
        if native_wave is not None and material["wavelength_nm"] != native_wave:
            raise ValueError("Canonical RGB CFA channels must share one native wavelength grid.")
        native_wave = material["wavelength_nm"]
        channels[name] = material
    shared = json.loads(
        files("pyisetcam")
        .joinpath("data/sensor_estimate/optical_materials_v1.json")
        .read_text(encoding="utf-8")
    )
    materials = {key: _material_identity(value) for key, value in shared["materials"].items()}
    geometry = copy.deepcopy(stack.get("geometry_um", {}))
    original_cfa_thickness = geometry.get("cfa_thickness")
    original_passivation_thickness = geometry.get("passivation_thickness")
    optical_height_field = (
        optical.get("optical", {}).get("geometry", {}).get("optical_stack_height_um", {})
    )
    optical_height = optical_height_field.get("value")
    height_source = optical_height_field.get("source_kind", "unavailable")
    if optical_height is None:
        optical_height = sum(
            _number(geometry.get(key), key)
            for key in (
                "lens_height",
                "cfa_thickness",
                "passivation_thickness",
            )
        )
        height_source = "generated_stack_sum"
    optical_height = _number(optical_height, "optical_stack_height_um")
    geometry["cfa_thickness"] = thickness
    for key in ("pitch", "lens_height", "lens_edge_gap", "passivation_thickness", "si_thickness"):
        geometry[key] = _number(
            geometry.get(key), f"geometry_um.{key}", allow_zero=key == "lens_edge_gap"
        )
    remainder = round(optical_height - geometry["lens_height"] - thickness, 12)
    if remainder <= 0:
        raise ValueError(
            "Canonical CFA/lens exceed optical stack height; nonpositive passivation remainder."
        )
    geometry["passivation_thickness"] = remainder
    return {
        "schema": "camerae2e_cfa_material_bundle_v1",
        "sensor_id": record["sensor_id"],
        "report_code": record["code"],
        "wavelength_nm": native_wave,
        "pixel_pitch_um": geometry["pitch"],
        "cfa_pattern": cfa.get("pattern", {}).get("value"),
        "geometry_um": geometry,
        "channels": channels,
        "materials": materials,
        "estimate_only": True,
        "bundle_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "material_bundle_sha256": _digest(
            {
                "channels": {key: value["material_sha256"] for key, value in channels.items()},
                "materials": {key: value["material_sha256"] for key, value in materials.items()},
            }
        ),
        "geometry_sha256": _digest(geometry),
        "stack_config": stack,
        "provenance": {
            "optical_model_path": str(optical_path),
            "optical_model_sha256": source_hash,
            "generated_stack_path": str(stack_path),
            "generated_stack_sha256": hashlib.sha256(stack_path.read_bytes()).hexdigest(),
            "original_generated_cfa_thickness_um": original_cfa_thickness,
            "canonical_cfa_thickness_um": thickness,
            "original_generated_passivation_thickness_um": original_passivation_thickness,
            "canonical_passivation_thickness_um": remainder,
            "optical_stack_height_um": optical_height,
            "optical_stack_height_source": height_source,
            "passivation_thickness_source": "inferred_lumped_remainder",
            "passivation_calculation": (
                "optical_stack_height_um - lens_height_um - canonical_cfa_thickness_um"
            ),
            "canonical_cfa_source": "per_report_optical_qe_db_common_material_proxy",
            "geometry_source": "generated_lens_with_canonical_CFA_and_preserved_optical_height",
            "legacy_fdtd_cfa_csv_used": False,
            "measured_accuracy_verified": False,
            "limitations": [
                "CFA n,k are shared material proxies, not sensor-specific measured dispersion.",
                "Extracted CFA ranges mix colors/locations; ranges are sensitivity bounds, "
                "not confidence intervals.",
                "Generated lens/passivation layer split is a geometry assumption even when "
                "total stack height is reported.",
                "Planar interfaces exclude curved-microlens focusing "
                "and spatial charge collection.",
            ],
        },
    }


def cfa_material_stack_config(bundle: Mapping[str, Any], wavelength_nm: float) -> dict[str, Any]:
    """Export a single-frequency stack with canonical n,k for standalone Meep.

    Each material uses scalar ``n``/``k`` understood by sensor_stack_config.py.
    No legacy CFA table paths survive in the exported optical roles.
    """
    wavelength = _number(wavelength_nm, "wavelength_nm")
    result = copy.deepcopy(bundle["stack_config"])
    result["geometry_um"] = copy.deepcopy(bundle["geometry_um"])
    replacements = {**bundle["materials"], **{f"cfa_{k}": v for k, v in bundle["channels"].items()}}
    for role, material in replacements.items():
        n, k = interpolate_nk(material, wavelength)
        result["materials"][role] = {
            "n": float(n),
            "k": float(k),
            "measured": False,
            "material_sha256": material["material_sha256"],
            "source": material["source_kind"],
        }
    result["canonical_material_bundle_sha256"] = bundle["material_bundle_sha256"]
    result["canonical_geometry_sha256"] = bundle["geometry_sha256"]
    result["monochromatic_wavelength_nm"] = wavelength
    return result


def planar_stack_response(
    wavelength_nm: float,
    layers: Sequence[Mapping[str, Any]],
    *,
    incident_n: float = 1.0,
    exit_n: float = 1.0,
    angle_deg: float = 0.0,
    polarization: str = "s",
) -> dict[str, Any]:
    """Coherent planar R/T/A and finite-layer absorption for passive isotropic media.

    ``layers`` contain complex ``n`` (n+ik) and ``thickness_um``. Incident and
    exit media are transparent, semi-infinite; flux is normal Poynting power
    divided by incident normal power. Backward reflection recursion uses only
    decaying propagators, remaining stable for optically thick lossy layers.
    Tangential electric amplitudes are used for both s and p polarization.
    Fresnel/Poynting conventions: https://arxiv.org/abs/1603.02720 .
    """
    wavelength = _number(wavelength_nm, "wavelength_nm") / 1000
    n_in = _number(incident_n, "incident_n")
    n_out = _number(exit_n, "exit_n")
    angle = float(angle_deg)
    if not np.isfinite(angle) or not 0 <= angle < 90:
        raise ValueError("angle_deg must be finite in [0,90).")
    if polarization not in {"s", "p"}:
        raise ValueError("polarization must be 's' or 'p'.")
    indices = [complex(n_in)]
    thicknesses = [0.0]
    for layer in layers:
        index = complex(layer["n"])
        if not np.isfinite(index) or index.real <= 0 or index.imag < 0:
            raise ValueError("Layer n+ik must be finite with n>0 and k>=0.")
        indices.append(index)
        thicknesses.append(_number(layer["thickness_um"], "thickness_um", allow_zero=True))
    indices.append(complex(n_out))
    thicknesses.append(0.0)
    indices = np.asarray(indices, dtype=complex)
    beta = n_in * np.sin(np.radians(angle))
    q = np.sqrt(indices * indices - beta * beta + 0j)
    q = np.where((q.imag < 0) | ((q.imag == 0) & (q.real < 0)), -q, q)
    if np.any(np.abs(q) < 1e-14):
        raise ValueError(
            "Exact critical-angle longitudinal wavevector is singular; offset the angle."
        )
    admittance = q if polarization == "s" else indices * indices / q
    propagation = np.exp(2j * np.pi * q * np.asarray(thicknesses) / wavelength)
    interface = (admittance[:-1] - admittance[1:]) / (admittance[:-1] + admittance[1:])
    effective = np.zeros(len(indices), dtype=complex)
    for j in range(len(indices) - 2, -1, -1):
        returned = effective[j + 1] * propagation[j + 1] ** 2
        effective[j] = (interface[j] + returned) / (1 + interface[j] * returned)
    reflectance = float(abs(effective[0]) ** 2)
    forward = 1 + 0j
    absorption = []
    boundary_flux = []

    def flux(a: complex, b: complex, medium: int) -> float:
        return float(np.real((a + b) * np.conj(admittance[medium] * (a - b))) / admittance[0].real)

    for j in range(len(indices) - 1):
        returned = effective[j + 1] * propagation[j + 1] ** 2
        forward = (1 + interface[j]) * forward * propagation[j] / (1 + interface[j] * returned)
        if j + 1 == len(indices) - 1:
            break
        backward = returned * forward
        right_forward = forward * propagation[j + 1]
        right_backward = effective[j + 1] * right_forward

        left = flux(forward, backward, j + 1)
        right = flux(right_forward, right_backward, j + 1)
        absorption.append(left - right)
        boundary_flux.append({"front": left, "back": right})
    transmittance = float(admittance[-1].real / admittance[0].real * abs(forward) ** 2)
    total_absorption = float(sum(absorption))
    residual = 1 - reflectance - transmittance - total_absorption
    powers = np.asarray([reflectance, transmittance, *absorption], dtype=float)
    if not np.all(np.isfinite(powers)) or np.min(powers) < -1e-9 or abs(residual) > 1e-9:
        raise ArithmeticError("Planar reference failed finite/passive normal-power conservation.")
    return {
        "wavelength_nm": wavelength * 1000,
        "angle_deg": angle,
        "polarization": polarization,
        "R": reflectance,
        "T": transmittance,
        "A": total_absorption,
        "layer_absorption": absorption,
        "layer_boundary_flux": boundary_flux,
        "energy_residual": float(residual),
        "power_roundoff_tolerance": 1e-9,
        "method": "coherent_fresnel_reflection_recursion_normal_poynting_flux",
        "measured_validation": False,
    }


def planar_cfa_reference(
    bundle: Mapping[str, Any],
    wavelength_nm: float,
    *,
    channel: str = "green",
    angle_deg: float = 0.0,
    polarization: str = "s",
) -> dict[str, Any]:
    """Air / CFA / lumped passivation / finite Si / air planar solver benchmark.

    The curved microlens is excluded. This deliberately simple planar problem
    checks source normalization, complex material conversion and layer power
    integration; agreement does not validate a product's geometry or QE.
    """
    if channel not in bundle["channels"]:
        raise ValueError(f"Unknown CFA channel: {channel!r}.")
    geometry = bundle["geometry_um"]
    layer_specs = [
        ("cfa", bundle["channels"][channel], geometry["cfa_thickness"]),
        ("passivation", bundle["materials"]["passivation"], geometry["passivation_thickness"]),
        ("silicon", bundle["materials"]["silicon"], geometry["si_thickness"]),
    ]
    layers = [
        {"n": complex_refractive_index(material, wavelength_nm), "thickness_um": thickness}
        for _, material, thickness in layer_specs
    ]
    result = planar_stack_response(
        wavelength_nm, layers, angle_deg=angle_deg, polarization=polarization
    )
    result.update(
        sensor_id=bundle["sensor_id"],
        report_code=bundle["report_code"],
        channel=channel,
        layer_names=[name for name, _, _ in layer_specs],
        layers=[
            {
                "name": name,
                "n": float(layer["n"].real),
                "k": float(layer["n"].imag),
                "thickness_um": layer["thickness_um"],
            }
            for (name, _, _), layer in zip(layer_specs, layers, strict=True)
        ],
        incident_n=1.0,
        exit_n=1.0,
        curved_microlens_included=False,
        material_bundle_sha256=bundle["material_bundle_sha256"],
        geometry_sha256=bundle["geometry_sha256"],
    )
    return result
