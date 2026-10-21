"""ISP chart support and counterfactual co-located reference contracts."""

import numpy as np
import pytest

from camera_platform.reference_isp_scene import (
    chirp_pattern,
    chirp_scene,
    colocated_reference,
    mosaic_from_planes,
)


def geometry(halo):
    return {
        "native_shape_row_col": [32, 48],
        "input_shape_row_col": [32 + 2 * halo, 48 + 2 * halo],
        "crop_bounds_row_col": [halo, 32 + halo, halo, 48 + halo],
    }


def test_chirp_phase_and_support_do_not_depend_on_halo_width():
    white = np.linspace(2, 4, 21)
    photons, native = chirp_scene(geometry(6), {"white": white})
    bigger, other = chirp_scene(geometry(10), {"white": white})
    np.testing.assert_array_equal(native, other)
    np.testing.assert_array_equal(photons, bigger[4:-4, 4:-4])
    np.testing.assert_allclose(photons[6:-6, 6:-6], native[..., None] * white)
    assert photons.min() >= 0 and 0.099999 <= native.min() <= native.max() <= 0.600001
    full = chirp_pattern(geometry(6), full_support=True)
    assert not np.array_equal(full[0], full[6])  # actual continuation, not copied edge


@pytest.mark.parametrize("origin", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_plane_mosaic_retains_native_site_identity(origin):
    planes = np.broadcast_to(np.array([1, 2, 3, 4]), (8, 10, 4))
    raw = mosaic_from_planes(planes, origin=origin)
    y, x = np.indices(raw.shape)
    np.testing.assert_array_equal(raw, ((y + origin[0]) % 2) * 2 + ((x + origin[1]) % 2) + 1)


def test_colocated_electron_units_green_mean_and_linear_transform_order():
    planes = np.broadcast_to(np.array([100, 200, 300, 400]), (8, 10, 4))
    matrix = np.array([[2, -1, 0], [0, 1, 0], [0, 0, 0.5]])
    gains, wb = np.array([1, 2, 3, 4]), np.array([1, 2, 1])
    ccm = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 2]])
    original = planes.copy()
    out = colocated_reference(planes, 0.004, matrix, gains=gains, wb=wb, ccm=ccm)
    expected = (np.array([0.4, 2.6, 6.4]) @ matrix) * wb @ ccm.T
    np.testing.assert_allclose(out, np.broadcast_to(expected, out.shape))
    np.testing.assert_array_equal(planes, original)
    negative = colocated_reference(planes, 0.004, -np.eye(3))
    assert negative.max() < 0  # no premature display clipping


def test_constant_native_lsc_map_is_equivalent_but_varying_map_is_refused():
    planes = np.ones((8, 10, 4))
    gains = np.array([1, 1.2, 1.5, 2])
    native = mosaic_from_planes(np.broadcast_to(gains, planes.shape))
    np.testing.assert_array_equal(
        colocated_reference(planes, 0.004, np.eye(3), gains=native),
        colocated_reference(planes, 0.004, np.eye(3), gains=gains),
    )
    native[4, 4] += 0.1
    with pytest.raises(ValueError, match="Spatially varying"):
        colocated_reference(planes, 0.004, np.eye(3), gains=native)


def test_bad_scene_and_reference_inputs_are_rejected():
    with pytest.raises(ValueError):
        chirp_scene(geometry(4), {"white": np.ones(20)})
    with pytest.raises(ValueError):
        colocated_reference(np.ones((8, 8, 4)), 0, np.eye(3))
    with pytest.raises(ValueError):
        mosaic_from_planes(np.ones((8, 8, 4)), origin=(0.5, 0))
