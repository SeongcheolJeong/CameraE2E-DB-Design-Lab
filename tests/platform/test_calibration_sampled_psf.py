"""Native sampled PSF transfer, single-aperture rendering and selective refitting."""

from copy import deepcopy

import numpy as np
import pytest
from scipy.signal import convolve2d

from camera_platform import calibration_lab_engine as engine


def sampled_node(*, field=(0.0, 0.0)):
    # Deliberately asymmetric, with a nonzero phase and unequal x/y responses.
    kernel = np.array([[0, 1, 0], [1, 5, 2], [0, 0, 1]], dtype=float)
    kernel /= kernel.sum()
    return {
        "family": "sampled_native_kernel_v1",
        "field_xy": list(field),
        "sigma_x_px": 0.8,
        "sigma_y_px": 0.7,
        "theta_deg": 10.0,
        "kernel": kernel.tolist(),
        "kernel_pitch_px": 1.0,
        "sampling": "native_cell_center_pixel_integrated",
    }


def sampled_profile():
    profile = engine.default_profile()
    profile["blur"].update(
        family="field_effective_psf_v1",
        field_application="explicit_nodes_only",
        field_nodes=[sampled_node(), sampled_node(field=(0.5, 0.5))],
        included_effects=["lens_blur", "pixel_aperture"],
        charge_diffusion_assumption="zero_not_identified",
    )
    return profile


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("kernel", [[1.0, 0.0], [0.0, 0.0]]),
        ("kernel", [[0.0] * 3] * 3),
        ("kernel", [[0.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 0.0]]),
        ("kernel", [[0.0, 0.0, 0.0], [0.0, 1.1, -0.1], [0.0, 0.0, 0.0]]),
        ("kernel", [[float("nan")]]),
        ("kernel", [[1.0, 0.0, 0.0]]),
        ("kernel", [[1.0 / 259**2] * 259 for _ in range(259)]),
        ("kernel_pitch_px", 0.5),
        ("sampling", "optical_point_samples"),
        ("family", "unknown_kernel"),
    ],
)
def test_invalid_sampled_kernel_is_rejected_before_rendering(key, value):
    profile = sampled_profile()
    profile["blur"]["field_nodes"][0][key] = value
    with pytest.raises(ValueError):
        engine.validate_profile(profile)


def test_sampled_profile_validation_preserves_exact_nodes_and_no_field_extrapolation():
    profile = sampled_profile()
    original = deepcopy(profile)
    assert engine.validate_profile(profile) == original
    assert profile == original
    with pytest.raises(ValueError, match="No effective PSF"):
        engine._field_node(profile, {"field_xy": [-0.5, 0.5]})


def test_sampled_mtf_is_native_discrete_fourier_magnitude_with_asymmetry():
    node = sampled_node()
    kernel = np.asarray(node["kernel"])
    yy, xx = np.indices(kernel.shape, dtype=float)
    yy -= 1
    xx -= 1
    frequencies = np.array([0.0, 0.03, 0.125, 0.25, 0.5])
    phase_observed = False
    for angle in (0.0, 27.0, 90.0, 147.0):
        direction = xx * np.cos(np.radians(angle)) + yy * np.sin(np.radians(angle))
        otf = np.array(
            [
                np.sum(kernel * np.exp(-2j * np.pi * frequency * direction))
                for frequency in frequencies
            ]
        )
        phase_observed |= np.max(np.abs(otf.imag)) > 0.01
        np.testing.assert_allclose(engine._mtf(node, frequencies, angle), np.abs(otf), atol=1e-14)
    assert phase_observed
    assert engine._mtf(node, [0.25], 0)[0] != pytest.approx(engine._mtf(node, [0.25], 90)[0])


def test_sampled_render_decimates_then_convolves_without_second_receiving_aperture():
    node = sampled_node()
    yy, xx = np.indices((24, 28))
    high_resolution = np.stack(
        [((3 * xx + 5 * yy) % 11) / 11, ((xx + yy) % 3) / 3, (xx % 4) / 4],
        axis=-1,
    )
    native = high_resolution[2::4, 2::4]
    kernel = np.asarray(node["kernel"])
    expected = np.stack(
        [
            convolve2d(np.pad(native[..., channel], 1, mode="edge"), kernel, mode="valid")
            for channel in range(3)
        ],
        axis=-1,
    )
    actual, used_kernel = engine._effective_blur(high_resolution, node)
    np.testing.assert_allclose(actual, expected, atol=1e-14)
    np.testing.assert_array_equal(used_kernel, kernel)
    # Quarter-grid area averaging would add a second pixel aperture and must differ.
    averaged = high_resolution.reshape(6, 4, 7, 4, 3).mean(axis=(1, 3))
    assert np.max(np.abs(native - averaged)) > 0.1


def test_mtf_refit_replaces_only_observed_node_and_preserves_inherited_sampled_kernel():
    base = sampled_profile()
    original = deepcopy(base)
    frequency = np.linspace(0.0, 0.5, 32)
    true_sigma = 1.3
    observations = {
        "frequency_cy_px": frequency.tolist(),
        "mtf": np.exp(-2 * np.pi**2 * true_sigma**2 * frequency**2).tolist(),
        "orientation_deg": 0.0,
        "field_xy": [0.0, 0.0],
    }
    row = {
        "id": "measured-center",
        "chart": "mtf",
        "observations": observations,
        "role": "training",
        "conditions": deepcopy(base["conditions"]),
        "source_kind": "synthetic",
        "measurement_domain": "camera_raw",
    }
    result = engine.fit_profile(base, [row])
    fitted = result["profile"]
    assert fitted["blur"]["family"] == "field_effective_psf_v1"
    center = engine._field_node(fitted, {"field_xy": [0.0, 0.0]})
    assert center["family"] == "centered_elliptical_gaussian_v1"
    assert "kernel" not in center
    assert center["sigma_x_px"] == pytest.approx(true_sigma, abs=1e-7)
    assert center["sigma_y_px"] == pytest.approx(true_sigma, abs=1e-7)
    assert (
        engine._field_node(fitted, {"field_xy": [0.5, 0.5]}) == original["blur"]["field_nodes"][1]
    )
    assert base == original
    engine.validate_profile(fitted)
    assert result["physical_validation"] == "not_established"
