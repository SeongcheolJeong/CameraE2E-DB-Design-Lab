"""Analytic capture fixtures verify the RAW observer; no fixture is hardware data."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform.measurement import (
    ObservationError,
    analyze_raw_sequence,
    observation_method,
    observation_sampling_weights,
    observe_field_response,
)


@pytest.fixture
def spec():
    return {
        "raw_format": {
            "cfa": "RGGB",
            "bit_depth": 12,
            "black_level_dn": 64,
            "response_type": "linear",
        },
        "geometry": {"kind": "native_sensor", "full_sensor_shape": [48, 64], "crop_origin": [5, 9]},
        "rois": [
            {"id": "inner", "field_radius": 0.25, "box": [8, 8, 8, 8]},
            {"id": "outer", "field_radius": 0.75, "box": [0, 0, 8, 8]},
        ],
        "conditions": {
            "exposure_s": {"value": 0.004, "unit": "s"},
            "temperature_c": {"value": 25.0, "unit": "degC"},
            "gain": {"value": 1.0, "unit": "1"},
            "light_id": {"value": "analytic-fixture", "unit": "1"},
        },
        "included_effects": ["sensor_effective_qe", "mean_dark_charge", "readout_clipping"],
        "source_kind": "simulated",
        "source_uri": "fixture://analytic-raw-observer-test",
    }


def _cfa(shape, origin):
    rows, cols = np.indices(shape)
    return np.array([[0, 1], [1, 2]])[(rows + origin[0]) % 2, (cols + origin[1]) % 2]


def _flat(spec, values=(100, 200, 80), *, frames=4, dark=0):
    cfa = _cfa((16, 24), spec["geometry"].get("crop_origin", [0, 0]))
    image = np.asarray(values)[cfa] + spec["raw_format"]["black_level_dn"] + dark
    return np.repeat(image[None, ...], frames, axis=0).astype(np.uint16)


def test_dark_and_black_are_each_subtracted_once_with_odd_crop_cfa_phase(spec):
    raw = _flat(spec, dark=20)
    dark = _flat(spec, values=(0, 0, 0), dark=20, frames=3)
    spec["dark_conditions"] = deepcopy(spec["conditions"])
    with_dark = analyze_raw_sequence(raw, dark, spec)
    black_only = analyze_raw_sequence(raw, None, spec)
    np.testing.assert_allclose(with_dark["measurement"]["channel_means_dn"], [[100, 200, 80]] * 2)
    np.testing.assert_allclose(with_dark["measurement"]["values"], [0.5, 0.4] * 2)
    np.testing.assert_allclose(black_only["measurement"]["channel_means_dn"], [[120, 220, 100]] * 2)
    assert with_dark["measurement"]["observation_method"]["dark_correction"] == "mean_dark_frame"
    assert black_only["measurement"]["observation_method"]["dark_correction"] == "black_level_only"
    assert (
        with_dark["diagnostics"]["saturation_diagnostics"]["physical_well_clipping_verified"]
        is False
    )
    json.dumps(with_dark, allow_nan=False)


def test_sampling_basis_matches_exact_native_crop_samples_not_roi_point_labels(spec):
    basis = np.array([0, 0.4, 0.8, 1])
    sampling = observation_sampling_weights(spec, [16, 24], basis)
    response = np.array([80, 120, 60]) + basis[:, None] * np.array([40, 20, 10])
    observed = observe_field_response(response, sampling)
    full_rows, full_cols = spec["geometry"]["full_sensor_shape"]
    yy, xx = np.indices((16, 24))
    radius = np.hypot(yy + 5 - (full_rows - 1) / 2, xx + 9 - (full_cols - 1) / 2)
    radius /= np.hypot((full_rows - 1) / 2, (full_cols - 1) / 2)
    channel_map = np.array([80, 120, 60]) + radius[..., None] * np.array([40, 20, 10])
    cfa = _cfa((16, 24), [5, 9])
    expected = []
    for roi in spec["rois"]:
        row, col, height, width = roi["box"]
        mask = cfa[row : row + height, col : col + width]
        values = channel_map[row : row + height, col : col + width]
        expected.append([values[..., c][mask == c].mean() for c in range(3)])
    np.testing.assert_allclose(observed["channel_means"], expected, rtol=1e-14)
    np.testing.assert_allclose(np.asarray(sampling["channel_field_weights"]).sum(axis=-1), 1)
    assert sampling["roi_field_radius"] == [0.25, 0.75]
    assert sampling == observation_sampling_weights(spec, [4, 16, 24], basis)
    # Nominal labels are not substituted for averaging the actual CFA coordinates.
    point_prediction = np.array([80, 120, 60]) + np.array([0.25, 0.75])[:, None] * [40, 20, 10]
    assert not np.allclose(observed["channel_means"], point_prediction)


def test_continuous_model_and_quantized_raw_share_same_operator(spec):
    basis = [0, 0.5, 1]
    sampling = observation_sampling_weights(spec, [16, 24], basis)
    response = np.array([[100, 200, 80], [140, 190, 70], [170, 170, 40]])
    model_observation = observe_field_response(response, sampling)
    yy, xx = np.indices((16, 24))
    radius = np.hypot(yy + 5 - 23.5, xx + 9 - 31.5) / np.hypot(23.5, 31.5)
    cfa = _cfa((16, 24), [5, 9])
    image = np.zeros((16, 24))
    for channel in range(3):
        image[cfa == channel] = np.interp(radius[cfa == channel], basis, response[:, channel])
    raw = np.repeat(np.rint(image + 64)[None], 4, axis=0).astype(np.uint16)
    measured = analyze_raw_sequence(raw, None, spec)["measurement"]
    np.testing.assert_allclose(
        measured["channel_means_dn"], model_observation["channel_means"], atol=0.5
    )
    np.testing.assert_allclose(measured["values"], model_observation["values"], atol=0.005)
    assert measured["uncertainty"] == {"kind": "unknown"}


def test_empirical_standard_error_is_not_an_absolute_bound(spec):
    raw = _flat(spec, frames=5)
    cfa = _cfa((16, 24), [5, 9])
    red_delta = np.array([-20, -10, 0, 10, 20])
    for index in range(5):
        raw[index, cfa == 0] = raw[index, cfa == 0].astype(int) + red_delta[index]
    result = analyze_raw_sequence(raw, None, spec)
    expected = red_delta.std(ddof=1) / np.sqrt(5) / 200
    errors = result["diagnostics"]["statistical_uncertainty"]
    np.testing.assert_allclose(errors["values"], [expected, 0] * 2)
    assert errors["kind"] == "empirical_delta_method_standard_error"
    assert errors["is_absolute_bound"] is False
    assert result["measurement"]["uncertainty"] == {"kind": "unknown"}


def test_dark_temporal_uncertainty_contributes_and_short_stacks_remain_unknown(spec):
    raw = _flat(spec, frames=3, dark=20)
    dark = _flat(spec, frames=3, values=(0, 0, 0), dark=20)
    spec["dark_conditions"] = deepcopy(spec["conditions"])
    for index, offset in enumerate([-3, 0, 3]):
        dark[index] = dark[index].astype(int) + offset
    result = analyze_raw_sequence(raw, dark, spec)
    assert all(value > 0 for value in result["diagnostics"]["statistical_uncertainty"]["values"])
    short = analyze_raw_sequence(raw, dark[:2], spec)
    assert short["diagnostics"]["statistical_uncertainty"]["values"] == [None] * 4
    short_light = analyze_raw_sequence(raw[:2], None, spec)
    assert short_light["diagnostics"]["statistical_uncertainty"]["values"] == [None] * 4


def test_observation_method_is_same_for_measured_and_simulated_provenance(spec):
    raw = _flat(spec)
    first = analyze_raw_sequence(raw, None, spec)["measurement"]
    spec.update(
        source_kind="measured",
        physical_instance="declared-test-serial",
        instrument={"id": "declared-fixture-instrument", "calibration_revision": "test-only"},
    )
    second = analyze_raw_sequence(raw, None, spec)["measurement"]
    assert first["observation_method"] == second["observation_method"]
    assert second["physical_instance"] == "declared-test-serial"
    assert second["instrument"] == spec["instrument"]
    assert second["source_kind"] == "measured"


@pytest.mark.parametrize("value", [0, 4095])
def test_roi_adc_clipping_is_refused_with_observed_counts(spec, value):
    raw = _flat(spec)
    raw[0, 8, 8] = value
    with pytest.raises(ObservationError) as error:
        analyze_raw_sequence(raw, None, spec)
    assert error.value.issues[0]["code"] == "ADC_CLIPPING"
    assert error.value.issues[0]["total_samples"] == 4 * 8 * 8


def test_outside_roi_clipping_is_disclosed_without_claiming_whole_frame_unsaturated(spec):
    raw = _flat(spec)
    raw[0, 0, 23] = 4095
    result = analyze_raw_sequence(raw, None, spec)
    assert result["diagnostics"]["full_input_raw_code_statistics"]["white_adc_samples"] == 1
    assert result["diagnostics"]["saturation_diagnostics"]["saturated_samples"] == 0
    assert "selected_ROI" in result["diagnostics"]["saturation_diagnostics"]["scope"]


@pytest.mark.parametrize("change", ["exposure_s", "gain", "temperature_c"])
def test_dark_conditions_cannot_be_scaled_or_assumed(spec, change):
    raw = _flat(spec)
    dark = _flat(spec, values=(0, 0, 0))
    spec["dark_conditions"] = deepcopy(spec["conditions"])
    spec["dark_conditions"][change]["value"] *= 2
    with pytest.raises(ObservationError, match="must exactly match"):
        analyze_raw_sequence(raw, dark, spec)


def test_shape_pixel_counts_and_bit_depth_are_enforced(spec):
    raw = _flat(spec)
    invalid = deepcopy(spec)
    invalid["rois"][0]["box"] = [8, 8, 4, 4]
    with pytest.raises(ObservationError, match="at least 16"):
        analyze_raw_sequence(raw, None, invalid)
    with pytest.raises(ObservationError, match="integer dtype"):
        analyze_raw_sequence(raw.astype(float), None, spec)
    bad_codes = raw.astype(np.int32)
    bad_codes[0, 0, 23] = 5000
    with pytest.raises(ObservationError, match="declared bit depth"):
        analyze_raw_sequence(bad_codes, None, spec)
    with pytest.raises(ObservationError, match="even sides"):
        analyze_raw_sequence(raw[:, :-1], None, spec)
    with pytest.raises(ObservationError, match="same crop shape"):
        analyze_raw_sequence(raw, raw[:, :8], spec)


def test_explicit_representative_geometry_cannot_resize_or_extrapolate(spec):
    spec["geometry"] = {
        "kind": "representative_normalized_field_raster",
        "raster_shape": [16, 24],
        "mapping": "center_to_corner_radius_v1",
    }
    analyze_raw_sequence(_flat(spec), None, spec)
    with pytest.raises(ObservationError, match="exceed"):
        observation_sampling_weights(spec, [16, 24], [0.1, 0.2])
    spec["geometry"]["raster_shape"] = [32, 48]
    with pytest.raises(ObservationError, match="exactly match"):
        observation_sampling_weights(spec, [16, 24], [0, 1])


def test_declared_bounds_require_external_method_provenance_and_matching_unit(spec):
    raw = _flat(spec)
    spec["uncertainty"] = {"kind": "standard_error", "values": [0.01] * 4, "unit": "1"}
    with pytest.raises(ObservationError, match="absolute_bound"):
        analyze_raw_sequence(raw, None, spec)
    spec["uncertainty"] = {"kind": "absolute_bound", "values": [0.02] * 4, "unit": "%"}
    with pytest.raises(ObservationError, match="unit 1"):
        analyze_raw_sequence(raw, None, spec)
    spec["uncertainty"]["unit"] = "1"
    with pytest.raises(ObservationError, match="external method"):
        analyze_raw_sequence(raw, None, spec)
    spec["uncertainty"].update(
        method_and_version="externally-declared-budget/v1",
        source_uri="fixture://bound-contract-test-only",
    )
    result = analyze_raw_sequence(raw, None, spec)
    assert result["measurement"]["uncertainty"] == spec["uncertainty"]


def test_nonpositive_corrected_signal_and_large_logical_inputs_are_refused(spec):
    raw = _flat(spec)
    dark = _flat(spec, values=(300, 300, 300))
    spec["dark_conditions"] = deepcopy(spec["conditions"])
    with pytest.raises(ObservationError, match="must be positive"):
        analyze_raw_sequence(raw, dark, spec)
    # Strided views avoid allocating a huge fixture while exercising byte limits.
    large = np.broadcast_to(np.array(100, dtype=np.uint8), (256, 512, 512))
    with pytest.raises(ObservationError, match="64 MiB"):
        analyze_raw_sequence(large, large, spec)


def test_operator_only_check_reproduces_fingerprint_and_refuses_policy_conflicts(spec):
    raw = _flat(spec)
    result = analyze_raw_sequence(raw, None, spec)["measurement"]
    method = observation_method(spec, [16, 24], dark_present=False)
    assert method == result["observation_method"]
    assert method == observation_method(method["specification"], raw.shape, dark_present=False)
    conflicting = deepcopy(spec)
    conflicting["minimum_cfa_pixels"] = 1
    with pytest.raises(ObservationError, match="policy disagrees"):
        analyze_raw_sequence(raw, None, conflicting)
    conflicting = deepcopy(spec)
    conflicting["dark_correction"] = "mean_dark_frame"
    with pytest.raises(ObservationError, match="policy disagrees"):
        analyze_raw_sequence(raw, None, conflicting)
    changed = deepcopy(spec)
    changed["geometry"]["crop_origin"] = [6, 9]
    assert (
        observation_method(changed, raw.shape, dark_present=False)["sampling_hash"]
        != method["sampling_hash"]
    )


def test_nonfinite_raw_and_undeclared_transform_are_not_silently_accepted(spec):
    raw = _flat(spec).astype(float)
    raw[0, 8, 8] = np.nan
    with pytest.raises(ObservationError, match="integer dtype"):
        analyze_raw_sequence(raw, None, spec)
    spec["raw_format"]["tone_curve"] = "already-applied"
    with pytest.raises(ObservationError, match="Unsupported RAW preprocessing"):
        analyze_raw_sequence(_flat(spec), None, spec)
