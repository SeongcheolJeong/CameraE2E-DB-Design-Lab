"""Diffraction kernels feed the same absolute electron-rate operator as geometry."""

import copy

import numpy as np

from camera_platform import reference_workshop_optics as optics


def _solution():
    spec = optics.template()
    field = optics.field_layout()[0]
    field.update(status="succeeded", issues=[], geometry=optics.geometry(spec))
    kernels = np.zeros((21, 1, 3, 3))
    kernels[:, :, 1, 1] = 1
    return {
        "result": {"revision": "test", "fields": [field]},
        "arrays": {
            "psf": kernels,
            "irradiance_per_radiance_sr": np.ones((21, 3)) * 0.02,
            "conditional_weights": np.ones((21, 3, 1)),
            "directions_xyz": np.tile([[0.0, 0.0, 1.0]], (3, 1, 1)),
        },
    }


def test_flat_field_flux_is_not_changed_or_attenuated_twice_by_diffraction():
    geometric = _solution()
    diffracted = copy.deepcopy(geometric)
    diffracted["result"]["fields"][0]["diffraction_included"] = True
    diffracted["arrays"]["psf"][...] = (
        np.array([[1.0, 2.0, 1.0], [2.0, 4.0, 2.0], [1.0, 2.0, 1.0]]) / 16
    )
    photons = np.full((384, 384, 21), 1e12)
    qe = np.full((21, 4), 0.25)
    a = optics.apply_field(geometric, "center", photons, pixel_qe=qe)
    b = optics.apply_field(diffracted, "center", photons, pixel_qe=qe)
    np.testing.assert_allclose(a["mean_electron_rate"], b["mean_electron_rate"], rtol=1e-12)
    assert a["metadata"]["diffraction_included"] is False
    assert b["metadata"]["diffraction_included"] is True
    assert b["metadata"]["pixel_aperture_applications"] == 1


def test_spatial_input_uses_the_new_kernel_without_extra_pixel_filter():
    solution = _solution()
    solution["result"]["fields"][0]["diffraction_included"] = True
    solution["arrays"]["psf"][...] = 0
    solution["arrays"]["psf"][:, :, 1, 1:] = [0.25, 0.75]
    photons = np.zeros((384, 384, 21))
    photons[192, 192, :] = 1e12
    output = optics.apply_field(solution, "center", photons, pixel_qe=np.full((21, 4), 0.25))
    row = output["planes_electron_rate"][128, :, 0]
    assert np.count_nonzero(row > 1e-10) == 2
    np.testing.assert_allclose(row[128:130] / row.sum(), [0.25, 0.75], atol=1e-12)
