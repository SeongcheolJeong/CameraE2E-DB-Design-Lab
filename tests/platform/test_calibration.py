"""Constrained fit recovery and rejection tests; all generated data is synthetic."""

import hashlib
import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform.calibration import propose_angular_calibration
from camera_platform.measurement import analyze_raw_sequence, observation_sampling_weights
from camera_platform.runtime import get_plugin
from camera_platform.validation import compare

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
    "light_id": {"value": "synthetic-flat", "unit": "1"},
}


def sensor_fixture():
    return {
        "id": "sensor:synthetic-base",
        "kind": "sensor",
        "reference_plane": "sensor_surface",
        "source_kind": "estimated",
        "source_uri": "fixture://synthetic-sensor",
        "method_and_version": "synthetic-structure:1",
        "calibration_dataset_ids": [],
        "included_effects": EFFECTS[2:],
        "valid_domain": {
            "exposure_s": {"min": 0.001, "max": 0.01, "unit": "s"},
            "temperature_c": {"min": 20, "max": 30, "unit": "degC"},
            "gain": {"min": 1, "max": 1, "unit": "1"},
        },
        "payload": {
            "wavelength_nm": [450.0, 550.0, 650.0],
            "qe": [[0.1, 0.2, 0.5], [0.2, 0.7, 0.3], [0.6, 0.25, 0.05]],
            "pixel_pitch_um": 3.0,
            "sensor_angular_response": {
                "theta_deg": [0.0, 15.0, 30.0],
                "relative_response": [
                    [[1.0, 0.95, 0.8]] * 3,
                    [[1.0, 0.97, 0.92]] * 3,
                    [[1.0, 0.9, 0.7]] * 3,
                ],
            },
            "readout": {
                "full_well_e": 20000.0,
                "read_noise_e": 2.0,
                "dark_current_e_per_s": 100.0,
                "gain": 1.0,
                "bit_depth": 12,
                "black_level_dn": 64.0,
            },
        },
    }


def lens_fixture():
    return {
        "field_radius": [0.0, 0.25, 0.5, 0.75, 1.0],
        "theta_deg": [0.0, 15.0, 30.0],
        "angular_weights": [
            [1, 0, 0],
            [0.8, 0.2, 0],
            [0.35, 0.5, 0.15],
            [0.15, 0.45, 0.4],
            [0.05, 0.25, 0.7],
        ],
        "relative_illumination": [1.0, 0.98, 0.9, 0.8, 0.7],
    }


def stimulus_fixture():
    return {
        "wavelength_nm": [450.0, 550.0, 650.0],
        "spectral_irradiance_w_m2_nm": [0.0002] * 3,
        "reference_plane": "sensor_surface",
    }


def bounds_fixture():
    return {channel: {"min": [0.5, 0.5], "max": [1.5, 1.5], "unit": "1"} for channel in ("R", "B")}


def scaled_sensor(base, red=(0.8, 1.15), blue=(1.1, 0.75)):
    result = deepcopy(base)
    response = np.asarray(result["payload"]["sensor_angular_response"]["relative_response"])
    response[0, :, 1:] *= red
    response[2, :, 1:] *= blue
    result["payload"]["sensor_angular_response"]["relative_response"] = response.tolist()
    return result


def request(sensor, lens, stimulus, *, size=8):
    payload = sensor["payload"]
    return {
        **deepcopy(payload),
        "lens": deepcopy(lens),
        "exposure_s": 0.004,
        "temperature_c": 25.0,
        "irradiance_reference_plane": "sensor_surface_on_axis",
        "spectral_irradiance_w_m2_nm": stimulus["spectral_irradiance_w_m2_nm"],
        "noise_enabled": False,
        "frames": 2,
        "seed": 21,
        "output_size": [size, size],
    }


def continuous_observation(sensor, *, lens=None, stimulus=None, identity="synthetic:train"):
    lens = deepcopy(lens or lens_fixture())
    stimulus = deepcopy(stimulus or stimulus_fixture())
    prediction = get_plugin("response_db").execute(request(sensor, lens, stimulus))["summary"][
        "prediction"
    ]
    prediction.update(
        id=identity,
        dataset_ids=[identity],
        source_uri="fixture://" + identity,
        conditions=deepcopy(CONDITIONS),
        calibration_dataset_ids=[],
    )
    return {
        "measurement": prediction,
        "lens": lens,
        "stimulus": stimulus,
        "conditions": deepcopy(CONDITIONS),
        "sampling": {
            "mode": "continuous_dark_corrected_expectation",
            "field_radius": lens["field_radius"],
        },
    }


def raw_observation(sensor, *, physical=False):
    lens, stimulus = lens_fixture(), stimulus_fixture()
    captured = get_plugin("response_db").execute(request(sensor, lens, stimulus, size=64))
    specification = {
        "raw_format": {
            "cfa": "RGGB",
            "bit_depth": 12,
            "black_level_dn": 64,
            "response_type": "linear",
            "packing": "unpacked",
            "layout": "bayer",
            "hdr_streams": 1,
        },
        "geometry": {
            "kind": "representative_normalized_field_raster",
            "raster_shape": [64, 64],
            "mapping": "center_to_corner_radius_v1",
        },
        "rois": [
            {"id": "중심", "field_radius": 0.1, "box": [24, 24, 16, 16]},
            {"id": "middle", "field_radius": 0.5, "box": [16, 40, 16, 16]},
            {"id": "corner", "field_radius": 0.9, "box": [0, 0, 16, 16]},
        ],
        "conditions": deepcopy(CONDITIONS),
        "included_effects": EFFECTS,
        "source_kind": "measured" if physical else "simulated",
        "source_uri": "fixture://synthetic-raw-test",
        "dataset_ids": ["synthetic:raw-train"],
        "physical_instance": "synthetic-test-serial",
        "instrument": {"id": "synthetic-test-instrument", "calibration_revision": "test-only"},
    }
    if physical:
        specification["geometry"] = {
            "kind": "native_sensor",
            "full_sensor_shape": [64, 64],
            "crop_origin": [0, 0],
        }
    analyzed = analyze_raw_sequence(captured["arrays"]["raw_frames"], None, specification)
    measurement = analyzed["measurement"]
    measurement["id"] = "measurement:synthetic-raw"
    measurement["instrument"]["saturation_diagnostics"] = analyzed["diagnostics"][
        "saturation_diagnostics"
    ]
    measurement["instrument"]["linear_response_evidence"] = {
        "confirmed": True,
        "method_and_version": "synthetic-linearity-test:1",
        "source_uri": "fixture://synthetic-linearity-evidence",
    }
    return {
        "measurement": measurement,
        "lens": lens,
        "stimulus": stimulus,
        "conditions": deepcopy(CONDITIONS),
        "sampling": observation_sampling_weights(specification, [64, 64], lens["field_radius"]),
        "saturation_diagnostics": deepcopy(analyzed["diagnostics"]["saturation_diagnostics"]),
    }


def codes(report):
    return {issue["code"] for issue in report["issues"]}


def test_recovers_known_synthetic_angular_scales_and_preserves_frozen_physics():
    base = sensor_fixture()
    observed = continuous_observation(scaled_sensor(base))
    before = deepcopy((base, observed))
    result = propose_angular_calibration(base, [observed], bounds_fixture())
    assert result["status"] == "PROPOSED", result["issues"]
    assert result["diagnostics"]["R"]["scale_factors"] == pytest.approx([0.8, 1.15], abs=1e-9)
    assert result["diagnostics"]["B"]["scale_factors"] == pytest.approx([1.1, 0.75], abs=1e-9)
    candidate = result["candidate_payload"]
    assert candidate["qe"] == base["payload"]["qe"]
    assert candidate["readout"] == base["payload"]["readout"]
    candidate_response = np.asarray(candidate["sensor_angular_response"]["relative_response"])
    original_response = np.asarray(base["payload"]["sensor_angular_response"]["relative_response"])
    np.testing.assert_array_equal(candidate_response[1], original_response[1])
    np.testing.assert_array_equal(candidate_response[..., 0], original_response[..., 0])
    assert (base, observed) == before
    assert result["uncertainty"] == {"kind": "unknown"}
    assert result["training_dataset_ids"] == ["synthetic:train"]
    assert not result["physical_evidence"]
    assert not result["physical_validation_pass"]
    assert result["approval_status"] == "unapproved"
    json.dumps(result, allow_nan=False)


def test_synthetic_holdout_improves_with_another_lens_and_spectrum_without_physical_claim():
    base = sensor_fixture()
    truth = scaled_sensor(base)
    fitted = propose_angular_calibration(base, [continuous_observation(truth)], bounds_fixture())
    candidate = {**base, "payload": fitted["candidate_payload"]}
    lens = lens_fixture()
    lens["angular_weights"] = [
        [1, 0, 0],
        [0.9, 0.1, 0],
        [0.7, 0.2, 0.1],
        [0.4, 0.3, 0.3],
        [0.2, 0.2, 0.6],
    ]
    stimulus = stimulus_fixture()
    stimulus["spectral_irradiance_w_m2_nm"] = [0.00012, 0.0004, 0.0001]
    observed = continuous_observation(
        truth, lens=lens, stimulus=stimulus, identity="synthetic:holdout"
    )
    true_values = np.asarray(observed["measurement"]["values"])
    base_values = continuous_observation(base, lens=lens, stimulus=stimulus)["measurement"][
        "values"
    ]
    candidate_values = continuous_observation(candidate, lens=lens, stimulus=stimulus)[
        "measurement"
    ]["values"]
    before = np.sqrt(np.mean((np.asarray(base_values) - true_values) ** 2))
    after = np.sqrt(np.mean((np.asarray(candidate_values) - true_values) ** 2))
    assert before > 0.01
    assert after < 1e-10
    assert "synthetic:holdout" not in fitted["calibration_dataset_ids"]
    assert fitted["evidence_kind"] == "implementation_verification"


def test_actual_synthetic_raw_roi_fit_accounts_for_cfa_sampling_and_adc_rounding():
    base = sensor_fixture()
    observation = raw_observation(scaled_sensor(base))
    result = propose_angular_calibration(base, [observation], bounds_fixture())
    assert result["status"] == "PROPOSED", result["issues"]
    assert result["diagnostics"]["R"]["scale_factors"] == pytest.approx([0.8, 1.15], abs=0.03)
    assert result["diagnostics"]["B"]["scale_factors"] == pytest.approx([1.1, 0.75], abs=0.03)
    assert result["uncertainty"] == {"kind": "unknown"}
    assert not result["physical_evidence"]


def test_declared_physical_evidence_still_only_creates_an_unapproved_training_fit():
    base = sensor_fixture()
    result = propose_angular_calibration(
        base, [raw_observation(scaled_sensor(base), physical=True)], bounds_fixture()
    )
    assert result["status"] == "PROPOSED", result["issues"]
    assert result["physical_evidence"]  # Declared test metadata, not a claim about real lab data.
    assert result["evidence_kind"] == "physical_calibration_fit"
    assert not result["physical_validation_pass"]
    assert not result["independent_validation"]
    assert result["approval_status"] == "unapproved"


def test_training_dataset_lineage_blocks_independent_validation_even_after_perfect_fit():
    base = sensor_fixture()
    observed = continuous_observation(scaled_sensor(base))
    fitted = propose_angular_calibration(base, [observed], bounds_fixture())
    prediction = deepcopy(observed["measurement"])
    prediction.update(
        calibration_dataset_ids=fitted["calibration_dataset_ids"], valid_domain=base["valid_domain"]
    )
    measurement = deepcopy(observed["measurement"])
    measurement.update(
        source_kind="measured",
        physical_instance="test-only",
        uncertainty={"kind": "absolute_bound", "value": 0.0, "unit": "1"},
    )
    test = {
        key: deepcopy(prediction[key])
        for key in ("metric_id", "unit", "reference_plane", "included_effects", "conditions")
    }
    test["absolute_tolerance"] = {"value": 0.1, "unit": "1"}
    report = compare(prediction, measurement, test)
    assert report["status"] == "INVALID_COMPARISON"
    assert "calibration_dataset_overlap" in {issue["code"] for issue in report["issues"]}


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda o: o["measurement"].update(unit="%"), "METRIC_CONTRACT_MISMATCH"),
        (lambda o: o["measurement"]["axes"][0]["values"].reverse(), "AXIS_MISMATCH"),
        (
            lambda o: o["stimulus"]["wavelength_nm"].__setitem__(1, 551.0),
            "SPECTRAL_COORDINATE_MISMATCH",
        ),
        (lambda o: o["conditions"]["temperature_c"].update(unit="K"), "CONDITION_MISMATCH"),
        (
            lambda o: o["measurement"]["included_effects"].append("software_isp"),
            "INCLUDED_EFFECTS_MISMATCH",
        ),
        (
            lambda o: o["measurement"]["values"].__setitem__(1, float("nan")),
            "INVALID_CALIBRATION_INPUT",
        ),
    ],
)
def test_semantics_units_coordinates_and_nonfinite_poison_are_refused(mutation, expected):
    base = sensor_fixture()
    observation = continuous_observation(scaled_sensor(base))
    mutation(observation)
    report = propose_angular_calibration(base, [observation], bounds_fixture())
    assert report["status"] == "REFUSED"
    assert expected in codes(report)
    assert report["candidate_payload"] is None


def test_conditions_outside_model_domain_refuse_fit():
    base = sensor_fixture()
    observation = continuous_observation(scaled_sensor(base))
    for conditions in (observation["conditions"], observation["measurement"]["conditions"]):
        conditions["temperature_c"]["value"] = 60
    assert "OUT_OF_DOMAIN" in codes(
        propose_angular_calibration(base, [observation], bounds_fixture())
    )


def test_rank_deficient_incident_angle_data_cannot_identify_node_scales():
    base = sensor_fixture()
    lens = lens_fixture()
    lens["angular_weights"] = [[1, 0, 0]] * 5
    observation = continuous_observation(scaled_sensor(base), lens=lens)
    result = propose_angular_calibration(base, [observation], bounds_fixture())
    assert result["status"] == "REFUSED"
    assert "UNIDENTIFIABLE_FIT" in codes(result)
    assert result["diagnostics"]["R"]["rank"] == 0


def test_numerically_full_rank_but_ill_conditioned_angle_basis_is_refused():
    base = sensor_fixture()
    lens = lens_fixture()
    lens["angular_weights"] = [
        [1, 0, 0],
        [0.8, 0.2, 0],
        [0.5, 0.5, 0],
        [0.2, 0.8 - 1e-10, 1e-10],
        [0.1, 0.9 - 1e-10, 1e-10],
    ]
    observed = continuous_observation(scaled_sensor(base), lens=lens)
    result = propose_angular_calibration(base, [observed], bounds_fixture())
    assert result["status"] == "REFUSED"
    assert "UNIDENTIFIABLE_FIT" in codes(result)
    assert result["diagnostics"]["R"]["condition_number"] > 1e8


@pytest.mark.parametrize(
    "value,code", [("0.5", "NONNUMERIC_DATA"), (float("nan"), "INVALID_CALIBRATION_INPUT")]
)
def test_poisoned_parameter_bounds_cannot_enter_numeric_solver(value, code):
    base = sensor_fixture()
    bounds = bounds_fixture()
    bounds["R"]["min"][0] = value
    observed = continuous_observation(scaled_sensor(base))
    result = propose_angular_calibration(base, [observed], bounds)
    assert result["status"] == "REFUSED"
    assert code in codes(result)


def test_scale_bounds_need_dimensionless_units():
    base = sensor_fixture()
    bounds = bounds_fixture()
    bounds["R"]["unit"] = "deg"
    result = propose_angular_calibration(
        base, [continuous_observation(scaled_sensor(base))], bounds
    )
    assert "BOUND_UNIT_MISMATCH" in codes(result)


def test_active_bounds_are_reported_without_claiming_fit_success_as_validation():
    base = sensor_fixture()
    observed = continuous_observation(scaled_sensor(base, red=(0.6, 1.4)))
    bounds = bounds_fixture()
    bounds["R"].update(min=[0.9, 0.9], max=[1.1, 1.1])
    result = propose_angular_calibration(base, [observed], bounds)
    assert result["status"] == "PROPOSED", result["issues"]
    scales = np.asarray(result["diagnostics"]["R"]["scale_factors"])
    assert np.all(scales >= 0.9) and np.all(scales <= 1.1)
    assert any(result["diagnostics"]["R"]["active_bounds"])
    assert result["diagnostics"]["R"]["training_ratio_rmse"] > 0
    assert not result["physical_validation_pass"]


def test_bounds_are_capped_so_angular_qe_cannot_exceed_one():
    base = sensor_fixture()
    observed = continuous_observation(scaled_sensor(base))
    bounds = bounds_fixture()
    for channel in bounds:
        bounds[channel]["max"] = [10.0, 10.0]
    result = propose_angular_calibration(base, [observed], bounds)
    assert result["status"] == "PROPOSED", result["issues"]
    assert all(p["effective_max"] < p["requested_max"] for p in result["parameters"])
    candidate = result["candidate_payload"]
    effective = (
        np.asarray(candidate["sensor_angular_response"]["relative_response"])
        * np.asarray(candidate["qe"])[..., None]
    )
    assert np.all(effective <= 1 + 1e-12)


def test_infeasible_qe_bounds_are_refused_before_optimization():
    base = sensor_fixture()
    observed = continuous_observation(scaled_sensor(base))
    bounds = bounds_fixture()
    bounds["R"].update(min=[5.0, 5.0], max=[6.0, 6.0])
    assert "INFEASIBLE_PHYSICAL_BOUNDS" in codes(
        propose_angular_calibration(base, [observed], bounds)
    )


def test_parameter_box_that_can_saturate_is_refused():
    base = sensor_fixture()
    observed = continuous_observation(scaled_sensor(base))
    observed["stimulus"]["spectral_irradiance_w_m2_nm"] = [0.02] * 3
    assert "SATURATION_IN_PARAMETER_DOMAIN" in codes(
        propose_angular_calibration(base, [observed], bounds_fixture())
    )


def test_normalized_but_wrong_raw_roi_weights_are_refused():
    base = sensor_fixture()
    observation = raw_observation(scaled_sensor(base))
    observation["sampling"]["channel_field_weights"][0][0] = [1.0, 0, 0, 0, 0]
    payload = {
        key: value for key, value in observation["sampling"].items() if key != "content_hash"
    }
    observation["sampling"]["content_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = propose_angular_calibration(base, [observation], bounds_fixture())
    assert "SAMPLING_GEOMETRY_MISMATCH" in codes(result)


@pytest.mark.parametrize("missing", ["saturation_diagnostics", "linear_response_evidence"])
def test_physical_fit_requires_registered_saturation_and_external_linearity_evidence(missing):
    base = sensor_fixture()
    observation = raw_observation(scaled_sensor(base), physical=True)
    del observation["measurement"]["instrument"][missing]
    result = propose_angular_calibration(base, [observation], bounds_fixture())
    assert result["status"] == "REFUSED"
    assert not result["physical_evidence"]


def test_changed_raw_encoding_cannot_be_fitted_to_another_sensor_readout():
    base = sensor_fixture()
    observation = raw_observation(scaled_sensor(base))
    base["payload"]["readout"]["black_level_dn"] = 32.0
    result = propose_angular_calibration(base, [observation], bounds_fixture())
    assert "RAW_READOUT_MISMATCH" in codes(result)


def test_static_model_cannot_pool_different_temperatures_even_inside_declared_domain():
    base = sensor_fixture()
    first = continuous_observation(scaled_sensor(base), identity="synthetic:t25")
    second = continuous_observation(scaled_sensor(base), identity="synthetic:t26")
    for conditions in (second["conditions"], second["measurement"]["conditions"]):
        conditions["temperature_c"]["value"] = 26.0
    result = propose_angular_calibration(base, [first, second], bounds_fixture())
    assert "TEMPERATURE_MISMATCH" in codes(result)


def test_inherited_training_lineage_is_preserved():
    base = sensor_fixture()
    base["calibration_dataset_ids"] = ["ancestor:training"]
    result = propose_angular_calibration(
        base, [continuous_observation(scaled_sensor(base))], bounds_fixture()
    )
    assert result["calibration_dataset_ids"] == ["ancestor:training", "synthetic:train"]


def test_duplicate_training_dataset_is_not_counted_twice():
    base = sensor_fixture()
    observed = continuous_observation(scaled_sensor(base))
    assert "DUPLICATE_TRAINING_RECORD" in codes(
        propose_angular_calibration(base, [observed, deepcopy(observed)], bounds_fixture())
    )
