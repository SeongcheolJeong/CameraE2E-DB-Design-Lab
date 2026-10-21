"""OCL trend projection is a sensitivity experiment, never qualified 2D QE."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import electrical
from camera_platform import sensor_structure_camera as camera


def electrical_model():
    return electrical.default_electrical_model(
        dict(full_well_e=10000.0, read_noise_e=3.0,
             dark_current_e_per_s=10.0, gain=1.0,
             bit_depth=16, black_level_dn=64.0)
    )


def site_rates():
    values = np.empty((16, 20), dtype=float)
    for index, (row, col) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        values[row::2, col::2] = (index + 1) * 10000.0
    return values


def evidence_summary():
    summary = dict(specification={"dimension": 2}, camera_response_eligible=False,
                   trend_assessment={"decision_summary": [],
                                     "camera_response_eligible": False}, samples=[])
    for angle, delta in ((0.0, -.02), (20.0, .06)):
        for metric, value, status in (("central_absorption", delta, "magnitude_stable"),
                                      ("total_absorption", .003, "indistinguishable"),
                                      ("neighbor_fraction", -.04, "magnitude_stable")):
            summary["trend_assessment"]["decision_summary"].append(dict(
                angle_deg=angle, wavelength_nm=550.0, variant="ocl_shift", metric=metric,
                delta=value, delta_unit="relative_fraction" if metric != "neighbor_fraction"
                else "fraction", status=status, sensitivity=.001))
        for variant in ("reviewed", "ocl_shift"):
            for pol in ("s", "p"):
                center = .6 * (1 + delta if variant == "ocl_shift" else 1)
                summary["samples"].append(dict(
                    task_id=f"{angle}-{variant}-{pol}", variant=variant, polarization=pol,
                    angle_deg=angle, wavelength_nm=550.0, resolution=80, control="mesh",
                    passive=True, energy_residual=.001, support_tail_fraction=.002,
                    pixel_absorption=[.001, .01, center, .01, .001]))
    return summary


def test_evidence_uses_both_computed_anchors_without_promoting_camera_qualification():
    source = evidence_summary()
    before = deepcopy(source)
    evidence = camera.response_evidence(source)
    assert [item["angle_deg"] for item in evidence] == [0, 20]
    assert [item["central_gain"] for item in evidence] == pytest.approx([.98, 1.06])
    assert all(len(item["anchor_task_ids"]) == 4 for item in evidence)
    assert source == before
    assert source["camera_response_eligible"] is False
    assert source["trend_assessment"]["camera_response_eligible"] is False


@pytest.mark.parametrize("status", ["unresolved", "direction_stable", "indistinguishable"])
def test_unstable_central_effect_cannot_be_used_for_projection(status):
    source = evidence_summary()
    source["trend_assessment"]["decision_summary"][0]["status"] = status
    with pytest.raises(ValueError):
        camera.response_evidence(source)


def test_missing_polarization_or_wrong_angle_is_not_interpolated():
    for altered in ("missing_p", "wrong_angle", "wrong_wavelength"):
        source = evidence_summary()
        if altered == "missing_p":
            source["samples"].pop()
        elif altered == "wrong_angle":
            source["samples"][-1]["angle_deg"] = 19.0
        else:
            source["samples"][-1]["wavelength_nm"] = 540.0
        with pytest.raises(ValueError):
            camera.response_evidence(source)


def test_absorption_arrays_must_agree_with_recorded_effect():
    source = evidence_summary()
    source["samples"][2]["pixel_absorption"][2] *= 1.01
    with pytest.raises(ValueError, match="disagree"):
        camera.response_evidence(source)


@pytest.mark.parametrize("key,value", [("energy_residual", .011),
                                       ("support_tail_fraction", .016),
                                       ("passive", False)])
def test_anchor_validity_cannot_be_relaxed(key, value):
    source = evidence_summary()
    source["samples"][0][key] = value
    with pytest.raises(ValueError):
        camera.response_evidence(source)


@pytest.mark.parametrize("key", ["energy_residual", "support_tail_fraction"])
def test_nonfinite_anchor_validity_is_rejected(key):
    source = evidence_summary()
    source["samples"][0][key] = float("nan")
    with pytest.raises(ValueError):
        camera.response_evidence(source)


def test_negative_absorption_cannot_produce_plausible_ratio():
    source = evidence_summary()
    for sample in source["samples"]:
        sample["pixel_absorption"][2] *= -1
    with pytest.raises(ValueError):
        camera.response_evidence(source)


def test_hypothesis_adapter_does_not_unlock_qualified_2d_qe(monkeypatch):
    from camera_platform import sensor_structure_response as qualified
    # Even a mistakenly permissive legacy assessment cannot promote a 2D source.
    monkeypatch.setattr(qualified, "qualify",
                        lambda *_: ([], {"camera_response_eligible": True}))
    with pytest.raises(ValueError, match="2D"):
        qualified.photoelectron_rate([], {"dimension": 2}, {}, np.ones((4, 4, 1)),
                                     wavelength_nm=[550], spectral_bin_width_nm=[10],
                                     angle_deg=0, variant="ocl_shift", collection_efficiency=.75)


@pytest.mark.parametrize("profile", ["green_only", "achromatic_shared"])
def test_identity_gain_preserves_input_and_native_cfa(profile):
    original = site_rates()
    before = original.copy()
    result = camera.project_rate(original, 1.0, profile)
    np.testing.assert_array_equal(result, original)
    np.testing.assert_array_equal(original, before)


def test_green_projection_does_not_mix_or_change_red_blue():
    original = site_rates()
    result = camera.project_rate(original, 1.065, "green_only")
    np.testing.assert_array_equal(result[0::2, 0::2], original[0::2, 0::2])
    np.testing.assert_array_equal(result[1::2, 1::2], original[1::2, 1::2])
    np.testing.assert_allclose(result[0::2, 1::2], original[0::2, 1::2] * 1.065)
    np.testing.assert_allclose(result[1::2, 0::2], original[1::2, 0::2] * 1.065)


def test_achromatic_projection_keeps_four_site_ratios():
    original = site_rates()
    result = camera.project_rate(original, 0.987, "achromatic_shared")
    np.testing.assert_allclose(result, original * 0.987)


@pytest.mark.parametrize("bad_gain", [-1.0, float("nan"), float("inf")])
def test_projection_rejects_invalid_gain(bad_gain):
    with pytest.raises(ValueError):
        camera.project_rate(site_rates(), bad_gain, "green_only")


@pytest.mark.parametrize("bad_rate", [-1.0, float("nan"), float("inf")])
def test_projection_rejects_invalid_rate(bad_rate):
    rate = site_rates()
    rate[0, 0] = bad_rate
    with pytest.raises(ValueError):
        camera.project_rate(rate, 1.0, "green_only")


def test_projection_requires_declared_spectral_assumption():
    with pytest.raises(ValueError):
        camera.project_rate(site_rates(), 1.1, "measured_rgb_qe")


def test_acquisition_is_seeded_integer_raw_with_independent_repeats():
    rate = site_rates()
    model = electrical_model()
    before = deepcopy(model)
    first = camera.acquire(rate, model, seed=81, frames=8, exposure_s=.004)
    second = camera.acquire(rate, model, seed=81, frames=8, exposure_s=.004)
    assert first["raw_frames"].shape == (8, *rate.shape)
    assert first["raw_frames"].dtype.kind in "iu"
    np.testing.assert_array_equal(first["raw_frames"], second["raw_frames"])
    assert np.any(first["raw_frames"][0] != first["raw_frames"][1])
    assert model == before
    for name in ("electron_frames", "valid_frames", "well_frames", "adc_frames"):
        assert first[name].shape == (8, *rate.shape)
    assert np.all(np.isfinite(first["variance_e2"]))
    assert np.all(first["variance_e2"] >= 0)


def test_projected_rate_is_acquired_again_not_scaled_noisy_raw():
    rate = np.full((32, 32), 100000.0)
    original = camera.acquire(rate, electrical_model(), seed=19, frames=8)
    changed = camera.acquire(camera.project_rate(rate, 1.1, "achromatic_shared"),
                             electrical_model(), seed=19, frames=8)
    # Fresh shot noise must not be the original noisy electron sample multiplied.
    assert not np.allclose(changed["electron_frames"], original["electron_frames"] * 1.1)
    # The linear unsaturated mean increases while the shot variance increases
    # by the photoelectron delta, without multiplying read-noise variance.
    assert changed["electron_frames"].mean() > original["electron_frames"].mean()
    np.testing.assert_allclose(changed["variance_e2"] - original["variance_e2"], 40.0)


def test_fixed_pattern_and_dark_calibration_are_shared_across_variants():
    model = electrical_model()
    model["fixed_pattern"] = dict(prnu_sigma=.05, dsnu_e_per_s=3.0, seed=117)
    first = camera.acquire(site_rates(), model, seed=4, frames=3)
    second = camera.acquire(site_rates() * 1.1, model, seed=4, frames=3)
    np.testing.assert_array_equal(first["calibration"]["dark_e"],
                                  second["calibration"]["dark_e"])
    assert np.ptp(first["calibration"]["dark_e"]) > 0


def test_well_and_adc_clipping_remain_visible():
    result = camera.acquire(np.full((16, 16), 2e8), electrical_model(), seed=9, frames=3)
    assert result["well_frames"].all()
    assert not result["valid_frames"].all()
    assert result["raw_frames"].max() <= 65535


def test_common_isp_applies_lsc_once_and_scales_variance_by_gain_squared():
    capture = camera.acquire(site_rates(), electrical_model(), seed=44, frames=4)
    matrix = np.eye(3) / 1000
    unity = camera.process(capture, matrix, np.ones((16, 20)))
    doubled = camera.process(capture, matrix, np.full((16, 20), 2.0))
    np.testing.assert_allclose(doubled["linear"], unity["linear"] * 2)
    np.testing.assert_allclose(doubled["expected_linear"], unity["expected_linear"] * 2)
    np.testing.assert_allclose(doubled["corrected_variance_e2"],
                               unity["corrected_variance_e2"] * 4)


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_acquisition_rejects_invalid_input_before_readout(bad):
    rate = site_rates()
    rate[0, 0] = bad
    with pytest.raises(ValueError):
        camera.acquire(rate, electrical_model(), seed=1, frames=2)
