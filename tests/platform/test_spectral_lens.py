"""Independent quadrature/basis evidence for spectral RI and angular Lens responses."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform.calibration import propose_angular_calibration
from camera_platform.runtime import RuntimeRequestError, get_plugin, run_plugin

EFFECTS = [
    "lens_relative_illumination",
    "lens_incident_angle_distribution",
    "sensor_effective_qe",
    "sensor_angular_response",
    "mean_dark_charge",
    "readout_clipping",
]
CONDITIONS = {
    "exposure_s": {"value": 0.004, "unit": "s"},
    "temperature_c": {"value": 25.0, "unit": "degC"},
    "gain": {"value": 1.0, "unit": "1"},
}


def spectral_request():
    return {
        "wavelength_nm": [450.0, 550.0, 650.0],
        "spectral_irradiance_w_m2_nm": [1e-5, 2.4e-5, 0.7e-5],
        "qe": [[0.08, 0.18, 0.55], [0.2, 0.6, 0.25], [0.65, 0.24, 0.05]],
        "pixel_pitch_um": 3.0,
        "exposure_s": 0.004,
        "temperature_c": 25.0,
        "irradiance_reference_plane": "sensor_surface_on_axis",
        "readout": {
            "full_well_e": 20000,
            "read_noise_e": 2,
            "dark_current_e_per_s": 100,
            "gain": 1,
            "bit_depth": 12,
            "black_level_dn": 64,
        },
        "sensor_angular_response": {
            "theta_deg": [0.0, 10.0, 30.0],
            "relative_response": [
                [[1, 0.95, 0.7], [1, 0.85, 0.58], [1, 0.8, 0.52]],
                [[1, 0.99, 0.9], [1, 0.96, 0.88], [1, 0.94, 0.83]],
                [[1, 0.9, 0.62], [1, 0.88, 0.64], [1, 0.92, 0.72]],
            ],
        },
        "lens": {
            "wavelength_nm": [450.0, 550.0, 650.0],
            "field_radius": [0.0, 0.25, 0.5, 0.75, 1.0],
            "theta_deg": [0.0, 5.0, 20.0, 30.0],
            "angular_weights": [
                [
                    [1, 0, 0, 0],
                    [0.7, 0.2, 0.1, 0],
                    [0.35, 0.3, 0.25, 0.1],
                    [0.15, 0.2, 0.3, 0.35],
                    [0.02, 0.08, 0.3, 0.6],
                ],
                [
                    [1, 0, 0, 0],
                    [0.6, 0.2, 0.15, 0.05],
                    [0.3, 0.15, 0.3, 0.25],
                    [0.1, 0.25, 0.4, 0.25],
                    [0.01, 0.04, 0.25, 0.7],
                ],
                [
                    [1, 0, 0, 0],
                    [0.8, 0.1, 0.1, 0],
                    [0.5, 0.2, 0.15, 0.15],
                    [0.05, 0.1, 0.3, 0.55],
                    [0, 0.05, 0.15, 0.8],
                ],
            ],
            "relative_illumination": [
                [1, 0.97, 0.88, 0.75, 0.62],
                [1, 0.98, 0.9, 0.8, 0.68],
                [1, 0.95, 0.82, 0.7, 0.5],
            ],
        },
        "noise_enabled": False,
        "frames": 1,
        "seed": 23,
        "output_size": [8, 8],
    }


def independent_electrons(request):
    """Explicit scalar sums: no Runtime interpolator, einsum, or trapezoid calls."""
    assert request["wavelength_nm"] == [450, 550, 650]
    assert request["sensor_angular_response"]["theta_deg"] == [0, 10, 30]
    assert request["lens"]["theta_deg"] == [0, 5, 20, 30]
    area_time = (request["pixel_pitch_um"] * 1e-6) ** 2 * request["exposure_s"]
    h, c = 6.62607015e-34, 299792458
    output = np.zeros((5, 3))
    for field in range(5):
        for channel in range(3):
            for wi, (wavelength, trapezoid_nm) in enumerate(
                zip([450, 550, 650], [50, 100, 50], strict=True)
            ):
                r0, r10, r30 = request["sensor_angular_response"]["relative_response"][channel][wi]
                angular_values = [r0, (r0 + r10) / 2, (r10 + r30) / 2, r30]
                angular = sum(
                    a * weight
                    for a, weight in zip(
                        angular_values, request["lens"]["angular_weights"][wi][field], strict=True
                    )
                )
                output[field, channel] += (
                    trapezoid_nm
                    * wavelength
                    * 1e-9
                    / (h * c)
                    * area_time
                    * request["spectral_irradiance_w_m2_nm"][wi]
                    * request["qe"][channel][wi]
                    * request["lens"]["relative_illumination"][wi][field]
                    * angular
                )
    return output


def test_spectral_lens_matches_independent_wavelength_and_angular_basis_sum():
    request = spectral_request()
    before = deepcopy(request)
    result = run_plugin("response_db", request)
    expected = independent_electrons(request)
    np.testing.assert_allclose(result["arrays"]["field_channel_electrons"], expected, rtol=2e-14)
    np.testing.assert_allclose(
        result["summary"]["channel_ratios"], expected[:, [0, 2]] / expected[:, 1:2], rtol=2e-14
    )
    assert request == before
    assert result["summary"]["uncertainty"] == {"kind": "unknown"}


def test_spectral_electrons_equal_sum_of_individual_wavelength_basis_runs():
    request = spectral_request()
    total = run_plugin("response_db", request)["arrays"]["field_channel_electrons"]
    decomposed = np.zeros_like(total)
    for wave in range(3):
        single = deepcopy(request)
        single["spectral_irradiance_w_m2_nm"] = [
            value if i == wave else 0
            for i, value in enumerate(request["spectral_irradiance_w_m2_nm"])
        ]
        contribution = run_plugin("response_db", single)["arrays"]["field_channel_electrons"]
        np.testing.assert_allclose(contribution, independent_electrons(single), rtol=2e-14)
        decomposed += contribution
    np.testing.assert_allclose(total, decomposed, rtol=2e-14)


def test_constant_spectral_lens_is_identical_to_legacy_lens_in_actual_raw_and_rgb():
    spectral = spectral_request()
    for key in ["angular_weights", "relative_illumination"]:
        spectral["lens"][key] = [deepcopy(spectral["lens"][key][1]) for _ in range(3)]
    legacy = deepcopy(spectral)
    legacy["lens"].pop("wavelength_nm")
    for key in ["angular_weights", "relative_illumination"]:
        legacy["lens"][key] = legacy["lens"][key][0]
    a, b = run_plugin("response_db", spectral), run_plugin("response_db", legacy)
    assert a["arrays"].keys() == b["arrays"].keys()
    for key in a["arrays"]:
        np.testing.assert_array_equal(a["arrays"][key], b["arrays"][key])
    assert a["summary"]["channel_ratios"] == b["summary"]["channel_ratios"]


def test_changing_one_wavelength_ri_changes_only_its_explicit_electron_contribution():
    request = spectral_request()
    expected = independent_electrons(request)
    modified = deepcopy(request)
    modified["lens"]["relative_illumination"][2][-1] *= 0.25
    changed = run_plugin("response_db", modified)["arrays"]["field_channel_electrons"]
    np.testing.assert_allclose(changed, independent_electrons(modified), rtol=2e-14)
    np.testing.assert_allclose(changed[:-1], expected[:-1], rtol=2e-14)
    assert np.all(changed[-1] < expected[-1])
    assert not np.allclose(
        changed[-1] / expected[-1], np.repeat(changed[-1, 1] / expected[-1, 1], 3)
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["lens"].update(wavelength_nm=[450, 550, 651]),
        lambda r: r["lens"].update(wavelength_nm=[650, 550, 450]),
        lambda r: r["lens"].update(relative_illumination=r["lens"]["relative_illumination"][0]),
        lambda r: r["lens"]["angular_weights"][1][2].__setitem__(0, 1.0),
        lambda r: r["lens"]["relative_illumination"][1].__setitem__(0, 0.99),
        lambda r: r["lens"]["relative_illumination"][1].__setitem__(2, float("nan")),
    ],
)
def test_spectral_coordinate_shape_normalization_and_finite_guards(mutate):
    request = spectral_request()
    mutate(request)
    assert get_plugin("response_db").validate_request(request)
    with pytest.raises(RuntimeRequestError):
        run_plugin("response_db", request)


def base_model(request):
    return {
        "id": "sensor:independent-spectral-fixture",
        "kind": "sensor",
        "reference_plane": "sensor_surface",
        "source_kind": "estimated",
        "source_uri": "fixture://independent-spectral-calibration",
        "method_and_version": "test:1",
        "calibration_dataset_ids": [],
        "included_effects": EFFECTS[2:],
        "valid_domain": {
            "exposure_s": {"min": 0.001, "max": 0.01, "unit": "s"},
            "temperature_c": {"min": 20, "max": 30, "unit": "degC"},
            "gain": {"min": 1, "max": 1, "unit": "1"},
        },
        "payload": {
            key: deepcopy(request[key])
            for key in [
                "wavelength_nm",
                "qe",
                "pixel_pitch_um",
                "sensor_angular_response",
                "readout",
            ]
        },
    }


def analytic_observation(request):
    electrons = independent_electrons(request)
    return {
        "measurement": {
            "id": "measurement:independent-spectral",
            "dataset_ids": ["synthetic:spectral-training"],
            "source_kind": "synthetic",
            "source_uri": "fixture://independent-scalar-quadrature",
            "method_and_version": "scalar-angle-and-trapezoid-reference:1",
            "metric_id": "raw.channel_ratio",
            "unit": "1",
            "reference_plane": "sensor_raw",
            "values": (electrons[:, [0, 2]] / electrons[:, 1:2]).reshape(-1).tolist(),
            "included_effects": EFFECTS,
            "conditions": deepcopy(CONDITIONS),
            "axes": [
                {"name": "field_radius", "unit": "1", "values": request["lens"]["field_radius"]},
                {"name": "channel_ratio", "unit": "1", "values": ["R/G", "B/G"]},
            ],
        },
        "conditions": deepcopy(CONDITIONS),
        "lens": deepcopy(request["lens"]),
        "stimulus": {
            "wavelength_nm": request["wavelength_nm"],
            "spectral_irradiance_w_m2_nm": request["spectral_irradiance_w_m2_nm"],
            "reference_plane": "sensor_surface",
        },
        "sampling": {
            "mode": "continuous_dark_corrected_expectation",
            "field_radius": request["lens"]["field_radius"],
        },
    }


def test_spectral_calibration_recovers_planted_scales_from_independent_quadrature():
    request = spectral_request()
    model = base_model(request)
    truth = deepcopy(request)
    response = np.asarray(truth["sensor_angular_response"]["relative_response"])
    response[0, :, 1:] *= [0.8, 1.15]
    response[2, :, 1:] *= [1.1, 0.75]
    truth["sensor_angular_response"]["relative_response"] = response.tolist()
    observation = analytic_observation(truth)
    result = propose_angular_calibration(
        model,
        [observation],
        {c: {"min": [0.5, 0.5], "max": [1.5, 1.5], "unit": "1"} for c in ["R", "B"]},
    )
    assert result["status"] == "PROPOSED", result["issues"]
    assert result["diagnostics"]["R"]["scale_factors"] == pytest.approx([0.8, 1.15], abs=1e-9)
    assert result["diagnostics"]["B"]["scale_factors"] == pytest.approx([1.1, 0.75], abs=1e-9)
    assert result["candidate_payload"]["qe"] == model["payload"]["qe"]
    assert result["training_dataset_ids"] == ["synthetic:spectral-training"]
    assert result["uncertainty"] == {"kind": "unknown"}
    assert not result["physical_evidence"] and not result["physical_validation_pass"]
    # Synthetic holdout changes both spectrum and RI after fitting; it is implementation evidence.
    holdout = deepcopy(request)
    holdout["spectral_irradiance_w_m2_nm"] = [2e-5, 0.3e-5, 1.4e-5]
    holdout["lens"]["relative_illumination"][0][-1] = 0.3
    truth_holdout = deepcopy(holdout)
    truth_holdout["sensor_angular_response"] = truth["sensor_angular_response"]
    holdout["sensor_angular_response"] = result["candidate_payload"]["sensor_angular_response"]
    np.testing.assert_allclose(
        run_plugin("response_db", holdout)["arrays"]["field_channel_electrons"],
        independent_electrons(truth_holdout),
        rtol=1e-10,
    )


def test_calibration_refuses_changed_lens_wavelengths_instead_of_silent_resampling():
    request = spectral_request()
    observation = analytic_observation(request)
    observation["lens"]["wavelength_nm"][-1] = 651
    result = propose_angular_calibration(
        base_model(request),
        [observation],
        {c: {"min": [0.5, 0.5], "max": [1.5, 1.5], "unit": "1"} for c in ["R", "B"]},
    )
    assert result["status"] != "PROPOSED"
    assert any("wavelength" in issue["message"].lower() for issue in result["issues"])
