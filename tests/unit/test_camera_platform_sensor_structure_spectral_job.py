"""Numerical evidence must stay separate from the camera spectral projection."""
import copy

import numpy as np
import pytest

from camera_platform import sensor_structure_spectral_camera as camera
from camera_platform import sensor_structure_spectral_job as job


def _evidence():
    return [dict(field_id="right-edge", wavelength_nm=wave, cfa_channel=channel,
                 numerical_validity=True, baseline_absorption=.2,
                 response_gain={"red": [1., 1.2, 1.4], "green": [.9, 1., 1.1],
                                "blue": [1.3, 1.2, 1.1]}[channel][i])
            for channel in camera.CHANNELS for i, wave in enumerate([450., 550., 650.])]


def test_all_cfa_responses_use_all_wavelengths_and_both_green_sites_match():
    gains = camera.spectral_gains(_evidence(), "right-edge", [450., 500., 550., 600., 650.])
    np.testing.assert_allclose(gains[:, 0], [1., 1.1, 1.2, 1.3, 1.4])
    np.testing.assert_allclose(gains[:, 1], [.9, .95, 1., 1.05, 1.1])
    np.testing.assert_array_equal(gains[:, 1], gains[:, 2])
    np.testing.assert_allclose(gains[:, 3], [1.3, 1.25, 1.2, 1.15, 1.1])


@pytest.mark.parametrize("waves", [[440., 550.], [550., 660.], [550., 450.], [550., 550.]])
def test_no_uncomputed_wavelength_extrapolation(waves):
    with pytest.raises(ValueError):
        camera.spectral_gains(_evidence(), "right-edge", waves)


def test_missing_off_band_blue_node_cannot_be_replaced_with_unity():
    evidence = _evidence()
    evidence = [r for r in evidence if not (r["cfa_channel"] == "blue" and r["wavelength_nm"] == 650.)]
    with pytest.raises(ValueError, match="three computed"):
        camera.spectral_gains(evidence, "right-edge", [450., 550., 650.])


def test_invalid_numerical_node_is_never_projected():
    evidence = _evidence()
    evidence[0]["numerical_validity"] = False
    with pytest.raises(ValueError, match="invalid"):
        camera.spectral_gains(evidence, "right-edge", [450., 550., 650.])


def test_spectral_multiplication_precedes_integration_and_preserves_cfa_phase():
    bands = np.zeros((3, 2, 2, 4))
    bands[0] = [1., 2., 3., 4.]
    bands[1] = [5., 6., 7., 8.]
    bands[2] = [9., 10., 11., 12.]
    gains = camera.spectral_gains(_evidence(), "right-edge", [450., 550., 650.])
    raw, planes = camera.integrate(bands, gains)
    wanted = np.sum(bands * gains[:, None, None, :], axis=0)
    np.testing.assert_allclose(planes, wanted)
    np.testing.assert_allclose(raw, [[wanted[0, 0, 0], wanted[0, 1, 1]],
                                     [wanted[1, 0, 2], wanted[1, 1, 3]]])
    # Red at450nm and650nm must not receive a single shared integrated gain.
    assert not np.allclose(planes, bands.sum(axis=0) * gains[1])
    flipped, _ = camera.integrate(bands, gains, cfa_origin=(1, 1))
    np.testing.assert_allclose(flipped, [[wanted[0, 0, 3], wanted[0, 1, 2]],
                                        [wanted[1, 0, 1], wanted[1, 1, 0]]])


def test_unity_projection_preserves_native_pre_noise_rate():
    bands = np.random.default_rng(12).uniform(0, 500, (21, 8, 8, 4))
    raw, planes = camera.integrate(bands, np.ones((21, 4)))
    np.testing.assert_array_equal(planes, bands.sum(axis=0))
    assert np.isfinite(raw).all() and (raw >= 0).all()


def _screen(monkeypatch, low_delta, high_delta, *, valid=True):
    rows = [dict(field="right-edge", wavelength_nm=550., angle_deg=4.524756324933152,
                 cfa_channel="green", resolution=res, control="mesh", numerical_valid=valid,
                 absolute_delta=delta, baseline_absorption=.5, changed_absorption=.5 + delta,
                 gain=(.5 + delta) / .5, weak_response=False, polarized=[])
            for res, delta in [(60, low_delta), (80, high_delta)]]
    monkeypatch.setattr(job.spectral, "ratios", lambda samples: copy.deepcopy(rows))
    samples = [dict(field="right-edge", wavelength_nm=550., cfa_channel="green", energy_residual=.001)]
    return job.evidence_rows(samples)[0]


def test_grid_screen_keeps_numerical_validity_separate_from_small_effect(monkeypatch):
    result = _screen(monkeypatch, .001, .0011)
    assert result["numerical_validity"] is True
    assert result["trend_status"] == "indistinguishable"
    assert "not full convergence or 3D QE" in result["scope"]
    assert "time_refinement" in result["pending_checks"]


def test_two_grid_agreement_does_not_claim_completed_time_or_pml_checks(monkeypatch):
    result = _screen(monkeypatch, .02, .021)
    assert result["trend_status"] == "two_grid_stable"
    assert result["observed_grid_sensitivity"] == pytest.approx(.001)
    assert not any(item in result["completed_checks"] for item in ("time_refinement", "pml_refinement"))


def test_direction_reversal_is_unresolved_not_a_small_stable_difference(monkeypatch):
    result = _screen(monkeypatch, -.02, .021)
    assert result["numerical_validity"] is True
    assert result["trend_status"] in ("unresolved", "direction_reversed")


def test_invalid_grid_cannot_receive_stability_grade(monkeypatch):
    result = _screen(monkeypatch, .02, .021, valid=False)
    assert result["numerical_validity"] is False
    assert result["trend_status"] == "unresolved"
