"""Independent receiver-driven checks of retained-subcell optical transfer."""

import numpy as np
import pytest

from camera_platform import subcell_optics
from camera_platform.spatial import SpatialSceneRuntime, request_template


def _receiver_oracle(subcells, kernel, *, field_axes=None, pitch_um=10, full=(8, 8),
                     origin=(0, 0)):
    """Evaluate each receiver's tent response, not the implementation's scatter."""
    rows, cols, side, _, bands = subcells.shape
    ky, kx = kernel.shape[-2] // 2, kernel.shape[-1] // 2
    result = np.zeros((rows, cols, bands))
    for receiver_y in range(rows):
        for receiver_x in range(cols):
            for source_y in range(rows):
                for source_x in range(cols):
                    for sub_y in range(side):
                        for sub_x in range(side):
                            dy = (sub_y + 0.5) / side - 0.5
                            dx = (sub_x + 0.5) / side - 0.5
                            if field_axes is None:
                                local = kernel
                            else:
                                fy = (origin[0] + source_y + dy - (full[0] - 1) / 2)
                                fx = (origin[1] + source_x + dx - (full[1] - 1) / 2)
                                fy *= pitch_um / 1000
                                fx *= pitch_um / 1000
                                y_fraction = (fy - field_axes[0][0]) / (
                                    field_axes[0][1] - field_axes[0][0]
                                )
                                x_fraction = (fx - field_axes[1][0]) / (
                                    field_axes[1][1] - field_axes[1][0]
                                )
                                local = sum(
                                    wy * wx * kernel[:, iy, ix]
                                    for iy, wy in enumerate((1 - y_fraction, y_fraction))
                                    for ix, wx in enumerate((1 - x_fraction, x_fraction))
                                )
                            for tap_y in range(kernel.shape[-2]):
                                overlap_y = max(0.0, 1 - abs(
                                    receiver_y - (source_y + tap_y - ky + dy)
                                ))
                                for tap_x in range(kernel.shape[-1]):
                                    overlap_x = max(0.0, 1 - abs(
                                        receiver_x - (source_x + tap_x - kx + dx)
                                    ))
                                    result[receiver_y, receiver_x] += (
                                        subcells[source_y, source_x, sub_y, sub_x]
                                        * local[:, tap_y, tap_x] * overlap_y * overlap_x
                                        / side**2
                                    )
    return result


def test_subcell_position_changes_receiver_response_and_flux_ledger():
    values = np.zeros((8, 8, 2, 2, 2))
    values[0, 0, 0, 0] = [1.0, 2.0]
    values[4, 4, 1, 0] = [3.0, 4.0]
    kernel = np.array([
        [[0.0, 0.1, 0.0], [0.2, 0.5, 0.1], [0.0, 0.1, 0.0]],
        [[0.0, 0.0, 0.2], [0.1, 0.4, 0.1], [0.1, 0.1, 0.0]],
    ])
    result, ledger = subcell_optics.transfer(values, kernel, 10, (8, 8), (0, 0))
    np.testing.assert_allclose(result, _receiver_oracle(values, kernel), rtol=0, atol=1e-15)
    np.testing.assert_allclose(
        ledger["input_spectral_flux"], ledger["full_convolution_spectral_flux"],
        rtol=1e-14,
    )
    assert np.all(np.asarray(ledger["outside_native_sensor_spectral_flux"]) > 0)
    assert np.all(np.asarray(ledger["outside_crop_spectral_flux"]) > 0)
    assert ledger["source_subcell_transfer"]["model"] == subcell_optics.TRANSFER
    # A one-cell *receiver-integrated* impulse is already aperture-integrated.
    # Its fractional-source interpolation alone moves flux to neighbors.
    identity = np.ones((2, 1, 1))
    moved, _ = subcell_optics.transfer(values, identity, 10, (8, 8), (0, 0))
    np.testing.assert_allclose(moved, _receiver_oracle(values, identity), atol=1e-15)
    assert moved[4, 3, 0] > 0
    assert moved[4, 4, 0] < values[4, 4].mean(axis=(0, 1))[0]


def test_field_psf_uses_fractional_source_field_coordinate():
    values = np.zeros((8, 8, 2, 2, 2))
    values[3, 3, 0, 0] = [1.0, 2.0]
    values[3, 3, 1, 1] = [2.0, 1.0]
    kernel = np.zeros((2, 2, 2, 1, 3))
    kernel[:, 0, 0, 0, 0] = 1
    kernel[:, 0, 1, 0, 1] = 1
    kernel[:, 1, 0, 0, 1] = 1
    kernel[:, 1, 1, 0, 2] = 1
    axes = (np.array([-0.04, 0.04]), np.array([-0.04, 0.04]))
    result, ledger = subcell_optics.transfer(
        values, kernel, 10, (8, 8), (0, 0), field_axes=axes
    )
    expected = _receiver_oracle(values, kernel, field_axes=axes)
    np.testing.assert_allclose(result, expected, rtol=0, atol=1e-15)
    np.testing.assert_allclose(
        ledger["input_spectral_flux"], ledger["full_convolution_spectral_flux"],
        rtol=1e-14,
    )


def test_rejects_mean_forgery_missing_field_support_and_excess_work():
    values = np.zeros((8, 8, 2, 2, 2))
    values[3, 3, 0, 0] = 1
    with pytest.raises(ValueError, match="differs from native-cell mean"):
        subcell_optics.validate_subcells(values, np.zeros((8, 8, 2)), 2)
    field = np.ones((2, 2, 2, 1, 1))
    with pytest.raises(ValueError, match="fractional source positions"):
        subcell_optics.preflight((8, 8, 2), 2, field, 10, (8, 8), (0, 0),
                                 field_axes=([-0.01, 0.01], [-0.01, 0.01]))
    with pytest.raises(ValueError, match="ordered finite nodes"):
        subcell_optics.preflight((8, 8, 2), 2, field, 10, (8, 8), (0, 0),
                                 field_axes=([0.0, 0.0], [-0.1, 0.1]))
    with pytest.raises(ValueError, match="work budget"):
        subcell_optics.preflight((64, 64, 5), 4, np.ones((5, 5, 5, 65, 65)),
                                 10, (64, 64), (0, 0),
                                 field_axes=(np.linspace(-1, 1, 5),
                                             np.linspace(-1, 1, 5)))
    with pytest.raises(ValueError, match="8 MiB artifact budget"):
        subcell_optics.preflight((256, 256, 5), 4, np.ones((5, 1, 1)),
                                 10, (256, 256), (0, 0))


def test_registered_runtime_halo_with_large_psf_preserves_capture_from_full_source():
    """40² source and 31² PSF exceed the former direct-scatter work cap."""
    data = request_template()
    data.update(frames=1, noise_enabled=False)
    data["lens"]["relative_illumination"] = [[1, 1]] * 5
    data["sensor_angular_response"]["relative_response"] = [[[1, 1]] * 5] * 3
    kernel_1d = np.exp(-np.arange(-15, 16)**2 / (2 * 4.0**2))
    kernel = np.outer(kernel_1d, kernel_1d)
    kernel /= kernel.sum()
    data["psf"]["values"] = np.repeat(kernel[None], 5, axis=0)
    subcells = np.full((40, 40, 2, 2, 5), 1e-5)
    subcells[19, 19, 0, 0] += 2e-5
    scene = data["scene"]
    scene["spectral_irradiance"] = subcells.mean(axis=(2, 3))
    scene["source_subcell_irradiance"] = subcells
    scene["source_subcell_evidence"] = {
        "model": subcell_optics.TRANSFER, "grid_side": 2,
        "radiance_sha256": "a" * 64, "source_render_id": "fixture-source",
        "projection_job_id": None,
    }
    scene["sampling"] = {
        "kind": "native_sensor", "full_sensor_shape": [80, 80],
        "crop_origin": [20, 20], "pixel_pitch_um": data["pixel_pitch_um"],
        "capture_crop": {"crop_origin": [36, 36], "shape": [8, 8]},
    }
    resources = SpatialSceneRuntime().estimate_resources(data)
    assert resources["source_subcell_transfer"]["fft_convolution_upper_bound"] == 20
    assert 40 * 40 * 4 * 5 * 31**2 * 4 > 10_000_000
    result = SpatialSceneRuntime().execute(data)
    whole, ledger = subcell_optics.transfer(
        subcells, np.asarray(data["psf"]["values"]), data["pixel_pitch_um"],
        [80, 80], [20, 20],
    )
    np.testing.assert_allclose(
        result["arrays"]["blurred_spectral_irradiance"], whole[16:24, 16:24],
        rtol=1e-13, atol=1e-19,
    )
    np.testing.assert_allclose(
        ledger["input_spectral_flux"], ledger["full_convolution_spectral_flux"],
        rtol=1e-9,
    )
    assert result["arrays"]["raw_frames"].shape == (1, 8, 8)
    insufficient = data.copy()
    insufficient["scene"] = {**scene, "sampling": {**scene["sampling"],
        "capture_crop": {"crop_origin": [35, 36], "shape": [8, 8]}}}
    assert "missing the PSF halo" in SpatialSceneRuntime().validate_request(insufficient)[0][
        "message"
    ]
