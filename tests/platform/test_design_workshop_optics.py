"""Bounded physical and unit contracts for editable workshop optics."""

import copy
import time

import numpy as np
import pytest

from camera_platform import design_workshop_optics as optics
from camera_platform import prescription_psf


def test_spectral_batches_cover_all_bands_with_one_shared_reference():
    spec = optics.normalize(optics.default_specification())
    batches = optics.wavelength_batches(spec)
    assert sorted(set(sum(batches, []))) == optics.WAVE.tolist()
    for batch in batches:
        assert batch[len(batch) // 2] == 550
        assert len(batch) <= 16
        request = optics._request(spec, batch, validate=True)
        assert request["prescription"]["wavelength_nm"] == batch


def test_supported_native_crop_preserves_pitch_and_halo():
    spec = optics.normalize(optics.default_specification())
    geometry = optics.geometry(spec)
    assert geometry["native_shape_row_col"] == [138, 248]
    assert geometry["input_shape_row_col"] == [178, 288]
    assert geometry["crop_bounds_row_col"] == [20, 158, 20, 268]
    assert geometry["pixel_pitch_um"] == 3.45
    spec["native_shape_row_col"] = [360, 640]
    with pytest.raises(ValueError, match="field support"):
        optics.normalize(spec)


def test_actual_curvature_and_sensor_position_change_the_traced_wavefront():
    spec = optics.normalize(optics.default_specification())
    reference = optics._request(spec, [500, 550, 600], validate=True)
    changed = copy.deepcopy(spec)
    changed["controls"]["sensor_shift_um"] = 10
    changed["controls"]["a4_mm3"] = 0
    request = optics._request(changed, [500, 550, 600], validate=True)
    assert request["prescription"]["surfaces"][-1]["thickness_mm"] == pytest.approx(
        reference["prescription"]["surfaces"][-1]["thickness_mm"] + 0.01
    )
    arrays = []
    for r in (reference, request):
        model = prescription_psf._model(r, 1)
        arrays.append(prescription_psf._wavefront(r, model, 550, 0, 0, 16, time.monotonic() + 20))
    assert np.max(np.abs(arrays[0]["opd_nm"] - arrays[1]["opd_nm"])) > 1
    assert not np.allclose(arrays[0]["ray_sensor_xy_mm"], arrays[1]["ray_sensor_xy_mm"])


def _solution():
    spec = optics.default_specification()
    spec.update(native_shape_row_col=[8, 10], kernel_size=3)
    geometry = optics.geometry(spec)
    psf = np.zeros((21, 3, 3, 3, 3))
    psf[..., 1, 1] = 1
    return {
        "result": {"status": "PROPOSED", "revision": "test", "geometry": geometry},
        "arrays": {
            "psf": psf,
            "field_y_mm": np.array([-0.5, 0, 0.5]),
            "field_x_mm": np.array([-0.5, 0, 0.5]),
            "image_height_mm": np.array([0, 0.375, 0.75]),
            "irradiance_per_radiance_sr": np.ones((21, 3)) * 0.02,
            "directions_xyz": np.tile([[0, 0, 1]], (3, 1, 1)),
            "conditional_weights": np.ones((21, 3, 1)),
        },
    }


def test_native_adapter_applies_qe_area_spectrum_once_and_preserves_rggb():
    solution = _solution()
    photons = np.ones((*solution["result"]["geometry"]["input_shape_row_col"], 21)) * 1e12
    qe = np.tile([0.1, 0.2, 0.3, 0.4], (21, 1))
    output = optics.apply_spectral_radiance(solution, photons, pixel_qe=qe)
    expected = 1e12 * 0.02 * 210 * (3.45e-6) ** 2
    np.testing.assert_allclose(
        output["planes_electron_rate"], np.broadcast_to(expected * qe[0], (8, 10, 4)), rtol=1e-12
    )
    np.testing.assert_allclose(
        output["mean_electron_rate"][:2, :2], expected * np.array([[0.1, 0.2], [0.3, 0.4]])
    )
    assert output["metadata"]["pixel_aperture_applications"] == 1


def test_absolute_angular_response_replaces_normal_qe_without_double_counting():
    solution = _solution()
    photons = np.ones((*solution["result"]["geometry"]["input_shape_row_col"], 21)) * 1e12
    normal = np.full((21, 4), 0.8)

    def response(theta, phi, wave):
        return np.full((*np.asarray(theta).shape, 4), 0.25)

    angular = optics.apply_spectral_radiance(
        solution, photons, pixel_qe=normal, angular_response=response
    )
    direct = optics.apply_spectral_radiance(solution, photons, pixel_qe=np.full((21, 4), 0.25))
    np.testing.assert_allclose(angular["mean_electron_rate"], direct["mean_electron_rate"])


def test_refused_optics_are_not_used_as_camera_inputs():
    solution = _solution()
    solution["result"]["status"] = "REFUSED"
    with pytest.raises(ValueError, match="Refused"):
        optics.apply_spectral_radiance(solution, np.zeros((10, 12, 21)), pixel_qe=np.ones((21, 4)))


def test_through_focus_changes_real_sensor_gap_and_preserves_base(monkeypatch, tmp_path):
    spec = optics.default_specification()
    observed = []

    def capture(child, directory, **kwargs):
        observed.append(child["controls"]["sensor_shift_um"])
        return {"revision": "test", "status": "PROPOSED", "diagnostics": {"mtf": []}}

    monkeypatch.setattr(optics, "solve", capture)
    output = optics.through_focus(spec, [-10, 0, 10], tmp_path)
    assert observed == [-10, 0, 10]
    assert spec["controls"]["sensor_shift_um"] == 0
    assert output["sensor_plane_actually_retraced"]
