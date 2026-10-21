"""Declared pupil to finite-support pixel-integrated PSF using Prysm Fraunhofer propagation."""

from __future__ import annotations

import copy
import importlib.metadata

import numpy as np


def request_template():
    return {
        "schema": "declared_pupil_psf_v1",
        "wavelength_nm": [450, 500, 550, 600, 650],
        "pupil_diameter_mm": 4.0,
        "f_number": 8.0,
        "pixel_pitch_um": 3.0,
        "pupil_samples": 128,
        "kernel_size": 31,
        "subpixel_samples": 5,
        "opd_nm": {"defocus": 120.0, "astigmatism": 0.0, "coma_x": 40.0},
        "max_kernel_l1_change": 0.015,
        "minimum_retained_energy": 0.97,
        "application_basis": "independently_declared_pupil_not_rayoptics_wavefront",
    }


def validate_request(request):
    if not isinstance(request, dict) or set(request) != set(request_template()):
        raise ValueError("Declare the exact declared_pupil_psf_v1 fields")
    data = copy.deepcopy(request)
    if (
        data["schema"] != "declared_pupil_psf_v1"
        or data["application_basis"] != request_template()["application_basis"]
    ):
        raise ValueError(
            "Pupil is independently declared; RayOptics wavefront equivalence is not established"
        )
    wave = np.asarray(data["wavelength_nm"])
    if (
        wave.ndim != 1
        or wave.dtype.kind not in "uif"
        or not 2 <= len(wave) <= 16
        or not np.isfinite(wave).all()
        or not np.all(np.diff(wave) > 0)
        or wave[0] < 350
        or wave[-1] > 1100
    ):
        raise ValueError("Require 2..16 ordered wavelengths in [350,1100] nm")
    bounds = {
        "pupil_diameter_mm": (0.1, 100),
        "f_number": (2, 32),
        "pixel_pitch_um": (0.2, 20),
        "max_kernel_l1_change": (1e-6, 0.03),
        "minimum_retained_energy": (0.95, 0.9999),
    }
    for key, (low, high) in bounds.items():
        value = data[key]
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError(f"{key} must be a finite number in [{low},{high}]")
    for key, low, high in [
        ("pupil_samples", 32, 128),
        ("kernel_size", 3, 65),
        ("subpixel_samples", 3, 9),
    ]:
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise ValueError(f"{key} must be an integer in [{low},{high}]")
    if (
        data["pupil_samples"] % 2
        or data["kernel_size"] % 2 != 1
        or data["subpixel_samples"] % 2 != 1
    ):
        raise ValueError("Use even pupil samples and odd kernel/subpixel samples")
    if not isinstance(data["opd_nm"], dict) or set(data["opd_nm"]) != {
        "defocus",
        "astigmatism",
        "coma_x",
    }:
        raise ValueError("Declare defocus, astigmatism and coma_x OPD coefficients in nm")
    if any(type(v) not in (int, float) or not -500 <= v <= 500 for v in data["opd_nm"].values()):
        raise ValueError("Declared OPD coefficients must be finite and within +/-500 nm")
    if data["pixel_pitch_um"] / data["subpixel_samples"] > wave[0] * 1e-3 * data["f_number"] / 2:
        raise ValueError(
            "Subpixel image sampling exceeds the incoherent diffraction Nyquist spacing"
        )
    period = wave[0] * 1e-3 * data["f_number"] * data["pupil_samples"] / 1.2
    if data["kernel_size"] * data["pixel_pitch_um"] >= 0.8 * period:
        raise ValueError(
            "PSF support approaches the discrete pupil's periodic image replicas; refine pupil"
        )
    operations = (
        len(wave)
        * 4
        * (data["kernel_size"] * (2 * data["subpixel_samples"] + 1)) ** 2
        * (2 * data["pupil_samples"])
    )
    if operations > 1_000_000_000:
        raise ValueError("Declared pupil characterization exceeds its bounded DFT budget")
    return data


def _propagate(data, samples, subpixels, wavelength):
    from prysm.propagation import focus_fixed_sampling

    diameter = data["pupil_diameter_mm"]
    dx = 1.2 * diameter / samples
    coordinate = (np.arange(samples) - samples // 2) * dx / (diameter / 2)
    x, y = np.meshgrid(coordinate, coordinate)
    r2 = x * x + y * y
    aperture = r2 <= 1
    coefficients = data["opd_nm"]
    opd = (
        coefficients["defocus"] * (2 * r2 - 1)
        + coefficients["astigmatism"] * (x * x - y * y)
        + coefficients["coma_x"] * (3 * r2 - 2) * x
    )
    pupil = aperture * np.exp(2j * np.pi * opd / wavelength)
    field = focus_fixed_sampling(
        pupil,
        dx,
        diameter * data["f_number"],
        wavelength * 1e-3,
        data["pixel_pitch_um"] / subpixels,
        data["kernel_size"] * subpixels,
    )
    # Prysm's unitary MDFT includes output-sample spacing. Dividing the
    # sampled intensity by pupil energy gives receiving-bin energy fractions.
    sampled = np.abs(field) ** 2 / np.sum(np.abs(pupil) ** 2)
    size = data["kernel_size"]
    mass = sampled.reshape(size, subpixels, size, subpixels).sum(axis=(1, 3))
    return mass, aperture.astype(float), np.where(aperture, opd, 0.0)


def characterize(request):
    data = validate_request(request)
    if importlib.metadata.version("prysm") != "0.21.1":
        raise ValueError("This checked adapter requires Prysm 0.21.1")
    n, s = data["pupil_samples"], data["subpixel_samples"]
    configurations = [(n, s), (2 * n, s), (n, 2 * s + 1), (2 * n, 2 * s + 1)]
    all_mass = []
    for samples, subpixels in configurations:
        masses = []
        for wave in data["wavelength_nm"]:
            mass, aperture, opd = _propagate(data, samples, subpixels, wave)
            masses.append(mass)
        all_mass.append(masses)
    mass = np.asarray(all_mass)
    retained = mass.sum(axis=(-1, -2))
    if not np.isfinite(mass).all() or np.any(retained <= 0):
        raise ValueError("Prysm produced invalid optical energy")
    kernels = mass / retained[..., None, None]
    # Compare independent pupil and receiving-bin sampling refinements;
    # these observed differences are not a certified absolute error bound.
    changes = np.abs(kernels[1:] - kernels[0]).sum(axis=(-1, -2))
    accepted = bool(
        np.max(changes) <= data["max_kernel_l1_change"]
        and np.min(retained) >= data["minimum_retained_energy"]
        and np.max(retained) <= 1.001
    )
    kernel = kernels[-1]
    candidate = {
        "schema": "intensity_psf_v1",
        "wavelength_nm": data["wavelength_nm"],
        "values": kernel.tolist(),
        "sample_pitch_um": data["pixel_pitch_um"],
        "normalization": "unit_sum",
        "centering": "array_center",
        "spatial_model": "locally_shift_invariant",
        "boundary": "zero_outside_supplied_image",
        "sampling_model": "pixel_integrated_optical_impulse",
        "source": {
            "kind": "simulated",
            "description": "Declared pupil Fraunhofer PSF with one receiving-pixel "
            "aperture integral; finite support renormalized. Independent of RayOptics "
            "prescription; physical equivalence unverified.",
        },
    }
    discrete_otf = np.fft.fftshift(
        np.fft.fft2(np.fft.ifftshift(kernel, axes=(-2, -1))), axes=(-2, -1)
    )
    return {
        "summary": {
            "status": "PROPOSED" if accepted else "REFUSED",
            "plugin_id": "prysm_psf_v1",
            "prysm_version": "0.21.1",
            "request": data,
            "configurations": [
                {"pupil_samples": a, "subpixel_samples": b} for a, b in configurations
            ],
            "retained_energy_fraction": retained.tolist(),
            "kernel_l1_changes": changes.tolist(),
            "max_kernel_l1_change_observed": float(np.max(changes)),
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "approval_status": "unapproved",
            "included_effects": ["lens_psf", "pixel_aperture"],
            "opd_basis": "defocus*(2*r^2-1) + astigmatism*(x^2-y^2) + coma_x*(3*r^2-2)*x; "
            "unit pupil radius; coefficients nm, not RMS",
            "limitations": [
                "Paraxial scalar incoherent Fraunhofer model; "
                "uniform circular amplitude pupil in air.",
                "Pupil phase is independently declared, not extracted from a RayOptics lens.",
                "Finite kernel is explicitly renormalized; excluded tails and "
                "numerical differences are not prediction uncertainty.",
                "Kernel OTF includes pixel aperture and finite support; "
                "it is not optical lens MTF or measured SFR.",
                "Local stationary PSF; no distortion, field-dependent aberrations, "
                "vignetting, occlusion or polarization.",
            ],
        },
        "arrays": {
            "psf_bin_energy": mass,
            "psf_kernel": kernel,
            "retained_energy_fraction": retained,
            "kernel_otf_real": discrete_otf.real,
            "kernel_otf_imag": discrete_otf.imag,
            "pupil_amplitude": aperture,
            "pupil_opd_nm": opd,
        },
        "model_candidate": candidate if accepted else None,
    }
