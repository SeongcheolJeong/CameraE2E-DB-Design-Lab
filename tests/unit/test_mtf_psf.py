"""Independent references for measured-MTF reconstruction and its error report."""

from __future__ import annotations

import numpy as np
import pytest

import pyisetcam
from pyisetcam.mtf_psf import measured_mtf_to_psf, otf_to_psf, psf_to_mtf
from pyisetcam.optics import lsf_to_circular_psf, oi_get, oi_set, optics_build_2d_otf
from pyisetcam.types import OpticalImage


def test_gaussian_measurements_recover_psf_and_report_small_roundtrip_error() -> None:
    frequency = np.linspace(0.0, 500.0, 513)
    sigma_um = 2.0
    measured = np.exp(-2.0 * np.pi**2 * sigma_um**2 * (frequency / 1000.0) ** 2)

    result = measured_mtf_to_psf(
        frequency, measured, sample_pitch_um=1.0, kernel_size=65
    )

    coordinate = np.arange(65) - 32
    radius_squared = coordinate[:, None] ** 2 + coordinate[None, :] ** 2
    reference = np.exp(-radius_squared / (2.0 * sigma_um**2))
    reference /= reference.sum()
    np.testing.assert_allclose(result["psf"], reference, atol=2e-5, rtol=0.0)
    assert np.min(result["psf"]) >= 0.0
    assert result["psf"].sum() == pytest.approx(1.0, abs=1e-12)
    assert result["physical_validation"] == "not_established"
    assert result["assumptions"]

    roundtrip = result["roundtrip"]
    assert roundtrip["mtf_by_angle"].shape == (4, frequency.size)
    np.testing.assert_array_equal(roundtrip["angles_deg"], [0, 45, 90, 135])
    np.testing.assert_allclose(roundtrip["mtf_by_angle"], measured[None, :] + np.zeros((4, 1)),
                               atol=5e-5, rtol=0.0)
    assert roundtrip["max_abs_error"] < 5e-5
    assert roundtrip["rmse"] < 2e-5
    assert roundtrip["passed"]


def test_psf_to_mtf_matches_independent_sparse_discrete_transform() -> None:
    # A deliberately asymmetric PSF tests phase as well as off-grid frequencies.
    psf = np.zeros((9, 9))
    samples = [(0, 0, 0.4), (1, 0, 0.3), (0, -2, 0.2), (-2, 1, 0.1)]
    for x, y, weight in samples:
        psf[4 + y, 4 + x] = weight
    frequency = np.array([0.0, 0.017, 0.08, 0.231, 0.5])
    angles = np.array([0.0, 30.0, 90.0, 150.0])

    result = psf_to_mtf(
        psf,
        sample_pitch_um=2.0,
        frequency=frequency,
        frequency_unit="cycles/pixel",
        angles_deg=angles,
    )

    expected = []
    for angle in np.deg2rad(angles):
        response = sum(
            weight * np.exp(-2j * np.pi * frequency * (x * np.cos(angle) + y * np.sin(angle)))
            for x, y, weight in samples
        )
        expected.append(np.abs(response))
    np.testing.assert_allclose(result["mtf_by_angle"], expected, atol=1e-13, rtol=0.0)
    fx, fy = np.meshgrid(result["frequency_x"], result["frequency_y"])
    expected_otf = sum(
        weight * np.exp(-2j * np.pi * (fx * x + fy * y)) for x, y, weight in samples
    )
    np.testing.assert_allclose(result["otf"], expected_otf, atol=1e-13, rtol=0.0)
    np.testing.assert_allclose(result["mtf_2d"], np.abs(expected_otf), atol=1e-13, rtol=0.0)
    assert result["frequency_unit"] == "cycles/pixel"


def test_otf_preserves_valid_complex_phase_and_dc_normalizes() -> None:
    psf = np.zeros((9, 9))
    psf[3, 6] = 0.75
    psf[5, 2] = 0.25
    otf = 3.0 * np.fft.fft2(np.fft.ifftshift(psf))
    assert np.max(np.abs(otf.imag)) > 0.1

    result = otf_to_psf(otf)

    np.testing.assert_allclose(result["psf"], psf, atol=1e-14, rtol=0.0)
    assert result["diagnostics"]["imaginary_max_abs"] < 1e-12


def test_nonhermitian_otf_is_rejected_even_with_projection() -> None:
    otf = np.ones((9, 9), dtype=complex)
    otf[1, 2] = 1.0j
    for policy in ("raise", "project"):
        with pytest.raises(ValueError):
            otf_to_psf(otf, negative_policy=policy)


def test_projection_removes_negative_mass_without_absolute_value_folding() -> None:
    signed_psf = np.zeros((9, 9))
    signed_psf[4, 4] = 1.25
    signed_psf[4, 5] = -0.25
    otf = np.fft.fft2(np.fft.ifftshift(signed_psf))
    with pytest.raises(ValueError):
        otf_to_psf(otf)

    result = otf_to_psf(otf, negative_policy="project")

    expected = np.zeros_like(signed_psf)
    expected[4, 4] = 1.0
    np.testing.assert_allclose(result["psf"], expected, atol=1e-14, rtol=0.0)
    diagnostics = result["diagnostics"]
    assert diagnostics["projection_applied"]
    assert diagnostics["negative_mass"] == pytest.approx(0.25)
    assert diagnostics["correction_l1"] == pytest.approx(0.5)


def test_step_mtf_reports_projection_and_failed_roundtrip() -> None:
    frequency = np.array([0.0, 0.199, 0.2, 0.5])
    measured = np.array([1.0, 1.0, 0.0, 0.0])
    with pytest.raises(ValueError):
        measured_mtf_to_psf(
            frequency, measured, sample_pitch_um=1.0,
            frequency_unit="cycles/pixel", negative_policy="raise",
        )

    result = measured_mtf_to_psf(
        frequency, measured, sample_pitch_um=1.0,
        frequency_unit="cycles/pixel", negative_policy="project",
    )

    diagnostics = result["diagnostics"]
    assert diagnostics["negative_mass"] > 0.01
    assert diagnostics["negative_mass_fraction"] > 0.0
    assert diagnostics["projection_applied"]
    assert diagnostics["correction_l1"] > 0.01
    assert 0.0 <= diagnostics["edge_energy_fraction"] <= 1.0
    assert not result["roundtrip"]["passed"]
    residual = result["roundtrip"]["mtf_by_angle"] - measured[None, :]
    np.testing.assert_allclose(result["roundtrip"]["residual_by_angle"], residual)
    assert result["roundtrip"]["max_abs_error"] == pytest.approx(np.max(np.abs(residual)))
    assert result["roundtrip"]["rmse"] == pytest.approx(np.sqrt(np.mean(residual**2)))
    assert np.all(result["psf"] >= 0.0)
    assert result["psf"].sum() == pytest.approx(1.0, abs=1e-12)


def test_input_units_are_equivalent_and_input_arrays_are_copied() -> None:
    frequency_mm = np.linspace(0.0, 250.0, 129)
    frequency_px = frequency_mm * 0.002
    measured = np.exp(-2.0 * np.pi**2 * (2.0 * frequency_px) ** 2)
    original_frequency = frequency_mm.copy()
    original_measured = measured.copy()

    per_mm = measured_mtf_to_psf(frequency_mm, measured, sample_pitch_um=2.0)
    per_px = measured_mtf_to_psf(
        frequency_px, measured, sample_pitch_um=2.0, frequency_unit="cycles/pixel"
    )

    np.testing.assert_allclose(per_mm["psf"], per_px["psf"], atol=1e-13, rtol=0.0)
    np.testing.assert_allclose(
        per_mm["roundtrip"]["mtf_by_angle"], per_px["roundtrip"]["mtf_by_angle"],
        atol=1e-13, rtol=0.0,
    )
    np.testing.assert_array_equal(frequency_mm, original_frequency)
    np.testing.assert_array_equal(measured, original_measured)
    frequency_mm[1] = -5.0
    measured[1] = -5.0
    np.testing.assert_array_equal(per_mm["input"]["frequency"], original_frequency)
    np.testing.assert_array_equal(per_mm["input"]["mtf"], original_measured)
    np.testing.assert_array_equal(per_mm["roundtrip"]["frequency"], original_frequency)


@pytest.mark.parametrize(
    ("frequency", "mtf"),
    [
        ([0.0], [1.0]),
        ([0.0, 0.1], [1.0]),
        ([0.01, 0.1], [1.0, 0.5]),
        ([-0.1, 0.0], [1.0, 0.5]),
        ([0.0, 0.1, 0.1], [1.0, 0.7, 0.5]),
        ([0.0, 0.2, 0.1], [1.0, 0.7, 0.5]),
        ([0.0, np.nan], [1.0, 0.5]),
        ([0.0, np.inf], [1.0, 0.5]),
        ([0.0, 0.1], [0.9, 0.5]),
        ([0.0, 0.1], [1.0, -0.1]),
        ([0.0, 0.1], [1.0, 1.1]),
        ([0.0, 0.1], [1.0, np.nan]),
        ([0.0, 0.1], [1.0, np.inf]),
        ([[0.0, 0.1]], [[1.0, 0.5]]),
        ([0.0, 0.501], [1.0, 0.5]),
    ],
)
def test_invalid_measurements_are_rejected(frequency: list, mtf: list) -> None:
    with pytest.raises(ValueError):
        measured_mtf_to_psf(
            frequency, mtf, sample_pitch_um=1.0, frequency_unit="cycles/pixel"
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_pitch_um": 0.0},
        {"sample_pitch_um": -1.0},
        {"sample_pitch_um": np.inf},
        {"sample_pitch_um": np.nan},
        {"kernel_size": 1},
        {"kernel_size": 64},
        {"kernel_size": 1027},
        {"kernel_size": 65.5},
        {"frequency_unit": "cycles/metre"},
        {"high_frequency": "invent"},
        {"negative_policy": "abs"},
        {"max_abs_error": -0.1},
        {"rmse_tolerance": -0.1},
    ],
)
def test_invalid_reconstruction_options_are_rejected(kwargs: dict) -> None:
    options = {"sample_pitch_um": 1.0, **kwargs}
    with pytest.raises(ValueError):
        measured_mtf_to_psf([0.0, 100.0], [1.0, 0.5], **options)


def test_physical_frequency_nyquist_uses_sample_pitch() -> None:
    with pytest.raises(ValueError):
        measured_mtf_to_psf([0.0, 250.1], [1.0, 0.2], sample_pitch_um=2.0)


@pytest.mark.parametrize(
    "otf",
    [np.zeros((5, 5)), np.ones(5), np.full((5, 5), np.nan), np.full((5, 5), np.inf)],
)
def test_invalid_otf_is_rejected(otf: np.ndarray) -> None:
    with pytest.raises(ValueError):
        otf_to_psf(otf)


@pytest.mark.parametrize(
    "psf",
    [np.zeros((5, 5)), -np.ones((5, 5)), np.ones(5), np.full((5, 5), np.nan)],
)
def test_invalid_psf_is_rejected(psf: np.ndarray) -> None:
    with pytest.raises(ValueError):
        psf_to_mtf(psf, sample_pitch_um=1.0)


def _optical_image_with_psf() -> OpticalImage:
    oi = OpticalImage(fields={"wave": np.array([550.0]), "optics": {}})
    psf = np.zeros((9, 9))
    psf[4, 4] = 1.0
    return oi_set(oi, "psf data", {"psf": psf, "wave": [550.0], "umPerSamp": 1.0})


def test_reconstruction_functions_are_publicly_exported() -> None:
    for function in (measured_mtf_to_psf, otf_to_psf, psf_to_mtf):
        assert getattr(pyisetcam, function.__name__) is function
        assert function.__name__ in pyisetcam.__all__


def test_setting_psf_keeps_new_data_and_replaces_prior_wavefront() -> None:
    oi = _optical_image_with_psf()
    oi.fields["optics"]["wavefront"] = {"stale": True}
    new_psf = np.zeros((9, 9))
    new_psf[4, 3] = 0.25
    new_psf[4, 5] = 0.75

    oi_set(oi, "psf data", {"psf": new_psf, "wave": [550.0], "umPerSamp": 2.0})

    assert "wavefront" not in oi.fields["optics"]
    np.testing.assert_array_equal(oi.fields["optics"]["psf_data"]["psf"][:, :, 0], new_psf)
    np.testing.assert_array_equal(oi_get(oi, "psf data")["psf"][:, :, 0], new_psf)
    np.testing.assert_array_equal(oi_get(oi, "psf data", 550.0, "um")["psf"], new_psf)
    assert oi_get(oi, "psf data")["sample_spacing_m"] == pytest.approx(2e-6)


@pytest.mark.parametrize("setter", ["otf", "otfstruct"])
def test_setting_otf_invalidates_stale_psf_and_wavefront_and_preserves_dc(setter: str) -> None:
    oi = _optical_image_with_psf()
    oi.fields["optics"]["wavefront"] = {"stale": True}
    new_psf = np.zeros((9, 9))
    new_psf[3, 5] = 0.2
    new_psf[4, 4] = 0.1
    otf = np.fft.fft2(np.fft.ifftshift(new_psf))
    if setter == "otfstruct":
        frequencies = np.fft.fftshift(np.fft.fftfreq(9, d=0.001))
        value = {"OTF": otf, "fx": frequencies, "fy": frequencies, "wave": [550.0]}
    else:
        value = otf

    oi_set(oi, setter, value)

    assert "psf_data" not in oi.fields["optics"]
    assert "wavefront" not in oi.fields["optics"]
    result = oi_get(oi, "psf data", 550.0, "um")
    np.testing.assert_allclose(result["psf"], new_psf, atol=1e-14, rtol=0.0)
    assert result["psf"].sum() == pytest.approx(0.3)
    assert result["inverse_diagnostics"]["input_dc"] == pytest.approx(0.3)


def test_legacy_radial_otf_builder_discards_prior_psf_and_wavefront() -> None:
    oi = _optical_image_with_psf()
    oi.fields["optics"]["wavefront"] = {"stale": True}

    updated = optics_build_2d_otf(oi, [1.0, 0.7, 0.0], [0.0, 2.0, 4.0])

    assert "wavefront" not in updated
    assert "psf_data" not in updated
    assert "otf_data" in updated
    assert updated["otf_function"] == "custom"


def test_legacy_psf_retrieval_warns_and_exposes_negative_inverse_correction() -> None:
    oi = _optical_image_with_psf()
    signed_psf = np.zeros((9, 9))
    signed_psf[4, 4] = 1.25
    signed_psf[4, 5] = -0.25
    oi_set(oi, "otf", np.fft.fft2(np.fft.ifftshift(signed_psf)))

    with pytest.warns(RuntimeWarning, match="negative intensity"):
        result = oi_get(oi, "psf data", 550.0, "um")

    expected = np.zeros_like(signed_psf)
    expected[4, 4] = 1.0
    np.testing.assert_allclose(result["psf"], expected, atol=1e-14, rtol=0.0)
    assert result["inverse_diagnostics"]["projection_applied"]
    assert result["inverse_diagnostics"]["negative_mass"] == pytest.approx(0.25)
    assert result["inverse_diagnostics"]["correction_l1"] == pytest.approx(0.5)


def test_legacy_psf_retrieval_rejects_nonhermitian_otf() -> None:
    oi = _optical_image_with_psf()
    otf = np.ones((9, 9), dtype=complex)
    otf[1, 2] = 1.0j
    oi_set(oi, "otf", otf)

    with pytest.raises(ValueError, match="real PSF"):
        oi_get(oi, "psf data", 550.0, "um")


def test_measurement_pitch_can_differ_from_psf_pitch_without_changing_physical_result() -> None:
    frequency_mm = np.linspace(0.0, 250.0, 257)
    frequency_pixel = frequency_mm * 0.002
    measured = np.exp(-2.0 * np.pi**2 * (4.0 * frequency_mm / 1000.0) ** 2)

    physical = measured_mtf_to_psf(frequency_mm, measured, sample_pitch_um=1.0)
    pixel = measured_mtf_to_psf(
        frequency_pixel, measured, sample_pitch_um=1.0,
        frequency_unit="cycles/pixel", measurement_pixel_pitch_um=2.0,
    )

    np.testing.assert_allclose(physical["psf"], pixel["psf"], atol=1e-13, rtol=0.0)
    np.testing.assert_allclose(
        physical["roundtrip"]["mtf_by_angle"], pixel["roundtrip"]["mtf_by_angle"],
        atol=1e-13, rtol=0.0,
    )
    physical_back = psf_to_mtf(
        physical["psf"], sample_pitch_um=1.0, frequency=frequency_mm,
    )
    pixel_back = psf_to_mtf(
        physical["psf"], sample_pitch_um=1.0, frequency=frequency_pixel,
        frequency_unit="cycles/pixel", measurement_pixel_pitch_um=2.0,
    )
    np.testing.assert_allclose(
        physical_back["mtf_by_angle"], pixel_back["mtf_by_angle"], atol=1e-13, rtol=0.0
    )
    np.testing.assert_allclose(pixel_back["frequency_x"], physical_back["frequency_x"] * 0.002)
    np.testing.assert_allclose(pixel_back["frequency_y"], physical_back["frequency_y"] * 0.002)


def test_measurement_pixel_pitch_rejects_curves_beyond_psf_grid_nyquist() -> None:
    # 0.3 cycles / 1-um camera pixel = 300 cycles/mm, beyond the 2-um PSF grid.
    with pytest.raises(ValueError):
        measured_mtf_to_psf(
            [0.0, 0.3], [1.0, 0.1], sample_pitch_um=2.0,
            frequency_unit="cycles/pixel", measurement_pixel_pitch_um=1.0,
        )
    psf = np.zeros((9, 9))
    psf[4, 4] = 1.0
    with pytest.raises(ValueError):
        psf_to_mtf(
            psf, sample_pitch_um=2.0, frequency=[0.0, 0.3],
            frequency_unit="cycles/pixel", measurement_pixel_pitch_um=1.0,
        )


def test_measurement_pixel_pitch_cannot_silently_modify_physical_units() -> None:
    with pytest.raises(ValueError):
        measured_mtf_to_psf(
            [0.0, 100.0], [1.0, 0.5], sample_pitch_um=1.0, measurement_pixel_pitch_um=2.0,
        )
    psf = np.zeros((9, 9))
    psf[4, 4] = 1.0
    with pytest.raises(ValueError):
        psf_to_mtf(psf, sample_pitch_um=1.0, measurement_pixel_pitch_um=2.0)


def test_large_finite_psf_is_normalized_without_overflow() -> None:
    psf = np.full((3, 3), 1e308)

    with np.errstate(over="raise", invalid="raise"):
        result = psf_to_mtf(psf, sample_pitch_um=1.0, frequency=[0.0, 100.0])

    assert np.isfinite(result["otf"]).all()
    assert result["mtf_2d"][1, 1] == pytest.approx(1.0)
    np.testing.assert_allclose(result["mtf_by_angle"][:, 0], 1.0, atol=1e-14)
    np.testing.assert_array_equal(psf, np.full((3, 3), 1e308))


def test_otf_normalization_overflow_is_rejected_explicitly() -> None:
    otf = np.ones((3, 3), dtype=complex)
    otf[0, 0] = 1e-320

    with pytest.raises(ValueError):
        otf_to_psf(otf, negative_policy="project")


def test_lsf_symmetry_validation_is_independent_of_absolute_scale() -> None:
    asymmetric = np.array([0.0, 0.1, 0.8, 0.0, 0.1])
    for scale in (1.0, 1e-30):
        with pytest.raises(ValueError, match="symmetric"):
            lsf_to_circular_psf(asymmetric * scale)


def test_hold_extension_preserves_flat_mtf_and_declares_its_assumption() -> None:
    held = measured_mtf_to_psf(
        [0.0, 100.0], [1.0, 1.0], sample_pitch_um=1.0, kernel_size=65,
        high_frequency="hold", negative_policy="raise",
    )
    truncated = measured_mtf_to_psf(
        [0.0, 100.0], [1.0, 1.0], sample_pitch_um=1.0, kernel_size=65,
        high_frequency="zero",
    )

    expected = np.zeros((65, 65))
    expected[32, 32] = 1.0
    np.testing.assert_allclose(held["psf"], expected, atol=1e-14, rtol=0.0)
    np.testing.assert_allclose(held["roundtrip"]["mtf_by_angle"], 1.0, atol=1e-14)
    assert held["roundtrip"]["passed"]
    assert held["assumptions"]["high_frequency"] == "hold"
    assert truncated["assumptions"]["high_frequency"] == "zero"
    assert truncated["psf"][32, 32] < 0.5


def test_even_rectangular_psf_retains_centering_and_complex_phase() -> None:
    psf = np.zeros((4, 6))
    psf[2, 3] = 0.75
    psf[1, 4] = 0.25
    otf = np.fft.fft2(np.fft.ifftshift(psf))

    inverted = otf_to_psf(otf)
    forward = psf_to_mtf(psf, sample_pitch_um=1.0)

    np.testing.assert_allclose(inverted["psf"], psf, atol=1e-14, rtol=0.0)
    np.testing.assert_allclose(forward["otf"], np.fft.fftshift(otf), atol=1e-14, rtol=0.0)
    assert forward["frequency_x"].shape == (6,)
    assert forward["frequency_y"].shape == (4,)


def test_measurement_source_provenance_is_deep_copied() -> None:
    source = {"capture": {"id": "capture-001", "conditions": ["raw", "550nm"]}}
    result = measured_mtf_to_psf(
        [0.0, 100.0], [1.0, 0.5], sample_pitch_um=1.0, source=source,
    )

    source["capture"]["id"] = "changed"
    source["capture"]["conditions"].append("changed")

    assert result["input"]["source"] == {
        "capture": {"id": "capture-001", "conditions": ["raw", "550nm"]}
    }
