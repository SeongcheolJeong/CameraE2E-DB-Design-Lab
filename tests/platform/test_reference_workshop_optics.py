"""Reference source identity and numerical camera adapter invariants."""

import copy
from pathlib import Path

import numpy as np
import pytest

from camera_platform import reference_workshop_optics as optics

ROOT = Path(__file__).resolve().parents[2]


def test_fixed_reference_fields_preserve_the_real_image_height_and_pitch():
    spec = optics.normalize(optics.template())
    assert spec["pixel_pitch_um"] == 2.9
    fields = optics.field_layout()
    assert len(fields) == 9
    assert [f["normalized_radius"] for f in fields] == [0] + [0.5] * 4 + [0.85] * 4
    np.testing.assert_allclose(np.linalg.norm(fields[-1]["baseline_reference_xy_mm"]), 13.66 * 0.85)
    assert optics.geometry(spec)["input_shape_row_col"] == [384, 384]
    assert optics.geometry(spec)["crop_bounds_row_col"] == [64, 320, 64, 320]


def test_curvature_edits_actual_radius_and_sensor_gap_without_source_mutation():
    baseline = optics._request(ROOT, optics.template(), [450.0, 550.0, 650.0])
    spec = optics.template()
    spec["controls"].update(curvature_scale=1.005, sensor_shift_um=20)
    changed = optics._request(ROOT, spec, [450.0, 550.0, 650.0])
    a, b = baseline["prescription"]["surfaces"], changed["prescription"]["surfaces"]
    assert a[0]["radius_mm"] == 46.29
    assert b[0]["radius_mm"] == pytest.approx(46.29 / 1.005)
    assert b[-1]["thickness_mm"] == pytest.approx(a[-1]["thickness_mm"] + 0.02)
    assert a[1:-1] == b[1:-1]
    assert baseline["reference_focal_length_mm"] == changed["reference_focal_length_mm"]


def test_explicit_profiles_never_select_an_automatic_fallback():
    spec = optics.template()
    spec["optical_profile"] = "auto"
    with pytest.raises(ValueError, match="explicit"):
        optics.normalize(spec)
    spec["optical_profile"] = "wavefront"
    assert optics.normalize(spec)["optical_profile"] == "wavefront"


def test_geometric_receiver_bins_retain_one_pixel_aperture_and_orientation():
    kernel, retained = optics._histogram(np.array([[0.0, 0.0], [0.0029, 0.0029]]), 17, 2.9)
    assert retained == 1
    assert kernel[8, 8] == 0.5
    assert kernel[7, 9] == 0.5  # +y image height is upward; array rows increase downwards.
    with pytest.raises(ValueError, match="retains"):
        optics._histogram(np.array([[1.0, 1.0]]), 17, 2.9)


def _solution():
    spec = optics.template()
    field = optics.field_layout()[0]
    field.update(status="succeeded", issues=[], geometry=optics.geometry(spec))
    psf = np.zeros((21, 1, 3, 3))
    psf[..., 1, 1] = 1
    return {
        "result": {"revision": "unit", "fields": [field]},
        "arrays": {
            "psf": psf,
            "irradiance_per_radiance_sr": np.ones((21, 3)) * 0.02,
            "conditional_weights": np.ones((21, 3, 1)),
            "directions_xyz": np.tile([[0.0, 0.0, 1.0]], (3, 1, 1)),
        },
    }


def test_absolute_qe_area_spectrum_once_and_rggb_origin():
    solution = _solution()
    photons = np.ones((384, 384, 21)) * 1e12
    qe = np.tile([0.1, 0.2, 0.3, 0.4], (21, 1))
    output = optics.apply_field(solution, "center", photons, pixel_qe=qe)
    expected = 1e12 * 0.02 * 210 * (2.9e-6) ** 2
    np.testing.assert_allclose(
        output["mean_electron_rate"][:2, :2], expected * np.array([[0.1, 0.2], [0.3, 0.4]])
    )
    np.testing.assert_allclose(
        output["center_electron_rate_per_band"].sum(axis=0), expected * qe[0]
    )
    assert output["metadata"]["pixel_aperture_applications"] == 1


def test_angular_qe_replaces_normal_qe():
    solution = _solution()
    photons = np.ones((384, 384, 21)) * 1e12

    def response(theta, phi, wavelength):
        return np.full((len(theta), 4), 0.25)

    actual = optics.apply_field(
        solution, "center", photons, angular_response=response, pixel_qe=np.full((21, 4), 0.9)
    )
    direct = optics.apply_field(solution, "center", photons, pixel_qe=np.full((21, 4), 0.25))
    np.testing.assert_allclose(actual["mean_electron_rate"], direct["mean_electron_rate"])


def test_refused_fields_are_never_converted_into_raw():
    solution = _solution()
    solution["result"]["fields"][0].update(status="failed", issues=["numerical refusal"])
    with pytest.raises(ValueError, match="Refused field"):
        optics.apply_field(solution, "center", np.zeros((384, 384, 21)), pixel_qe=np.ones((21, 4)))


def test_normalization_preserves_input_and_rejects_product_pitch_changes():
    spec = optics.template()
    before = copy.deepcopy(spec)
    optics.normalize(spec)
    assert before == spec
    spec["pixel_pitch_um"] = 3.45
    with pytest.raises(ValueError, match="preserves"):
        optics.normalize(spec)
