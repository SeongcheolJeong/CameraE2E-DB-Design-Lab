"""Independent source-to-well arithmetic through native RAW and real ISP paths.

Every generation/collection probability below is authored synthetic data, not
an optical/device characterization or a physical validation claim.
"""

import hashlib
import json
from copy import deepcopy

import numpy as np
import pytest
import scipy
from test_dcg_runtime import expected_integer_raw
from test_native_pixel_workflow import _pure_request
from test_native_temporal import native_request as native_request

from camera_platform import spatial
from camera_platform.charge_collection import transport
from camera_platform.dcg_runtime import (
    SharedChargeDCGRuntime,
)
from camera_platform.dcg_runtime import (
    default_capture_program as dcg_program,
)
from camera_platform.hdr_runtime import HDRBracketRuntime
from camera_platform.hdr_runtime import default_capture_program as hdr_program
from camera_platform.runtime import RuntimeRequestError
from camera_platform.temporal import TemporalSceneRuntime

WAVE = [500, 600]
GENERATION = np.array([[0.2, 0.35, 0.6, 0.8], [0.7, 0.5, 0.3, 0.1]])
SHAPE = (8, 10)
SITES = np.array([[0, 1], [2, 3]])[
    np.arange(SHAPE[0])[:, None] % 2, np.arange(SHAPE[1])[None, :] % 2
]


def _collection(*, offsets=None, probabilities=None):
    if offsets is None:
        offsets = [[0, 0], [0, 1], [1, 0]]
    if probabilities is None:
        probabilities = [
            [[0.5, 0.1, 0.2], [0.4, 0.2, 0.3], [0.3, 0.3, 0.1], [0.2, 0.4, 0.2]],
            [[0.1, 0.6, 0.2], [0.2, 0.5, 0.1], [0.4, 0.1, 0.3], [0.5, 0.2, 0.2]],
        ]
    return {
        "schema": "camera_charge_collection_v1",
        "site_names": ["R", "Gr", "Gb", "B"],
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": 4.0,
        "wavelength_nm": WAVE.copy(),
        "destination_offsets_rc": deepcopy(offsets),
        "values_axis_order": ["wavelength", "source_site", "destination_offset"],
        "probabilities": deepcopy(probabilities),
        "interpolation": "linear_wavelength",
        "input_plane": "silicon_generated_charge",
        "output_plane": "collection_well_before_readout",
        "temporal_model": "instantaneous_memoryless",
        "boundary": "absorb_outside_full_sensor",
        "angular_assumption": "direction_independent_depth_marginal",
        "statistics": "single_destination_poisson_thinning",
        "regime": "linear_low_injection",
        "source": {"kind": "estimated", "description": "Authored test transfer, not measured"},
    }


def _image():
    rows, cols = np.indices(SHAPE)
    return 1e-4 * np.stack([0.25 + cols / 10, 1.25 - rows / 8], axis=-1)


def _request(native_request, *, mode="spatial", model=None):
    data = deepcopy(native_request)
    response = data["native_pixel_response"]
    response["schema"] = "camera_native_generation_response_v1"
    response["response_basis"] = "independent_site_generated_electrons_per_incident_pixel_photon"
    response["values"] = GENERATION[:, None, None, :].tolist()
    response["source"]["description"] = "Authored generation, explicitly excludes collection"
    data["charge_collection"] = model or _collection()
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] = _image()
        knot["scene"]["sampling"].update(full_sensor_shape=list(SHAPE), crop_origin=[0, 0])
    if mode == "spatial":
        data["scene"] = deepcopy(data.pop("scene_sequence")["knots"][0]["scene"])
        data.pop("control_sequence")
        data.pop("isp_profiles")
        data.update(exposure_s=0.002, frames=1)
    elif mode == "hdr":
        data["capture_program"] = hdr_program(data["readout"])
    elif mode == "dcg":
        data["capture_program"] = dcg_program(data["readout"])
    return data


def _reference(image, generation, collection):
    """Scalar scatter and two-node trapezoid, independent of model/runtime helpers.

    The source pixel's CFA has already affected generation. A destination CFA
    must never filter those generated electrons a second time.
    """
    result = {
        key: np.zeros(SHAPE)
        for key in ("generated", "collected", "intrinsic_loss", "off_sensor_loss")
    }
    for row in range(SHAPE[0]):
        for col in range(SHAPE[1]):
            site = SITES[row, col]
            for band, wavelength in enumerate(WAVE):
                rate = (
                    float(image[row, col, band])
                    * wavelength
                    * 1e-9
                    / (6.62607015e-34 * 299792458)
                    * (4e-6) ** 2
                    * generation[band, site]
                    * 50
                )
                probabilities = collection["probabilities"][band][site]
                result["generated"][row, col] += rate
                result["intrinsic_loss"][row, col] += rate * (1 - sum(probabilities))
                for (dr, dc), probability in zip(
                    collection["destination_offsets_rc"], probabilities, strict=True
                ):
                    dest_row, dest_col = row + dr, col + dc
                    if 0 <= dest_row < SHAPE[0] and 0 <= dest_col < SHAPE[1]:
                        result["collected"][dest_row, dest_col] += rate * probability
                    else:
                        result["off_sensor_loss"][row, col] += rate * probability
    return result


def _integer_raw(charge, readout):
    white, black = 2 ** readout["bit_depth"] - 1, readout["black_level_dn"]
    return np.rint(black + charge * (white - black) / readout["full_well_e"])


def test_wavelength_source_site_transfer_conserves_charge_and_changes_actual_raw_rgb(
    native_request,
):
    request = _request(native_request)
    expected = _reference(_image(), GENERATION, request["charge_collection"])
    output = spatial.SpatialSceneRuntime().execute(request)
    arrays, summary = output["arrays"], output["summary"]
    charge = expected["collected"] * request["exposure_s"]
    np.testing.assert_allclose(arrays["mean_electrons"], charge, rtol=4e-13, atol=1e-12)
    for key in ("generated", "intrinsic_loss", "off_sensor_loss"):
        np.testing.assert_allclose(
            arrays["collection_" + key],
            expected[key] * request["exposure_s"],
            rtol=4e-13,
            atol=1e-12,
        )
    np.testing.assert_allclose(
        expected["generated"].sum(),
        sum(expected[key].sum() for key in ("collected", "intrinsic_loss", "off_sensor_loss")),
        rtol=4e-15,
    )
    np.testing.assert_array_equal(arrays["raw_frames"][0], _integer_raw(charge, request["readout"]))
    assert arrays["raw_frames"].dtype.kind in "iu"
    # Four-site storage is only a native-site address container: hypothetical
    # responses at the three other CFA sites must not be sent to readout.
    for site in range(4):
        np.testing.assert_allclose(
            arrays["channel_electrons"][..., site],
            np.where(SITES == site, charge, 0),
            rtol=4e-13,
            atol=1e-12,
        )
    assert "sensor_charge_generation" in summary["included_effects"]
    assert "sensor_charge_collection" in summary["included_effects"]
    assert "sensor_effective_qe" not in summary["included_effects"]
    expected_site_means = [float(charge[SITES == site].mean()) for site in range(4)]
    np.testing.assert_allclose(summary["channel_electron_mean"], expected_site_means, rtol=4e-13)
    scale = (4095 - 64) / request["readout"]["full_well_e"]
    np.testing.assert_allclose(
        summary["prediction"]["values"], np.asarray(expected_site_means) * scale, rtol=4e-13
    )
    assert summary["charge_collection"]
    assert set(arrays) == set(summary["array_units"])
    json.dumps(summary, allow_nan=False)
    identity = deepcopy(request)
    identity["charge_collection"] = _collection(offsets=[[0, 0]], probabilities=[[[1]] * 4] * 2)
    unchanged = spatial.SpatialSceneRuntime().execute(identity)
    assert np.count_nonzero(arrays["raw_frames"] != unchanged["arrays"]["raw_frames"]) > 50
    assert np.count_nonzero(arrays["rgb_frames"] != unchanged["arrays"]["rgb_frames"]) > 50
    assert spatial.SpatialSceneRuntime().estimate_resources(request)[
        "working_array_bytes_estimate"
    ] >= sum(array.nbytes for array in arrays.values())


def test_opposite_spectral_routing_is_applied_before_wavelength_integration(native_request):
    model = _collection(offsets=[[0, -1], [0, 1]], probabilities=[[[1, 0]] * 4, [[0, 1]] * 4])
    request = _request(native_request, model=model)
    image = np.zeros((*SHAPE, 2))
    # Different source CFA sites and opposite wavelength routes separate all
    # three operations: spectral generation, charge transport and Bayer readout.
    image[2, 4, 0] = 1e-3
    image[3, 4, 1] = 2e-3
    request["scene"]["spectral_irradiance"] = image
    expected = _reference(image, GENERATION, model)["collected"] * request["exposure_s"]
    arrays = spatial.SpatialSceneRuntime().execute(request)["arrays"]
    np.testing.assert_allclose(arrays["mean_electrons"], expected, rtol=4e-13, atol=1e-12)
    assert expected[2, 3] > 0 and expected[3, 5] > 0
    assert np.count_nonzero(expected) == 2
    np.testing.assert_array_equal(
        arrays["raw_frames"][0], _integer_raw(expected, request["readout"])
    )


def test_full_sensor_generation_and_collection_feed_cropped_receiver(native_request):
    request = _request(
        native_request,
        model=_collection(offsets=[[1, 0]], probabilities=[[[1]] * 4] * 2),
    )
    image = np.zeros((16, 20, 2))
    image[3, 5] = [1e-4, 2e-4]
    request["scene"]["spectral_irradiance"] = image
    request["scene"]["sampling"].update(full_sensor_shape=[16, 20], crop_origin=[0, 0])
    # PSF moves an outside-ROI optical source to native site (3,6), then
    # carrier routing moves it into receiver (4,6), inside the declared ROI.
    kernel = np.zeros((2, 3, 3))
    kernel[:, 1, 2] = 1
    request["psf"]["values"] = kernel.tolist()
    complete = spatial.SpatialSceneRuntime().execute(request)
    cropped_request = deepcopy(request)
    cropped_request["scene"]["sampling"]["capture_crop"] = {
        "crop_origin": [4, 6], "shape": [8, 10],
    }
    cropped = spatial.SpatialSceneRuntime().execute(cropped_request)
    full_arrays, arrays = complete["arrays"], cropped["arrays"]
    roi = (slice(4, 12), slice(6, 16))
    for name in ("blurred_spectral_irradiance", "sensor_spectral_irradiance",
                 "channel_electrons", "mean_electrons", "cfa_channel_index"):
        np.testing.assert_allclose(arrays[name], full_arrays[name][roi], rtol=1e-13, atol=1e-11)
    np.testing.assert_array_equal(
        arrays["raw_frames"], full_arrays["raw_frames"][:, roi[0], roi[1]]
    )
    np.testing.assert_allclose(arrays["source_support_collected_electrons"],
                               full_arrays["mean_electrons"], rtol=1e-13, atol=1e-11)
    for name in ("generated", "intrinsic_loss", "off_sensor_loss"):
        np.testing.assert_allclose(arrays["collection_" + name],
                                   full_arrays["collection_" + name], rtol=1e-13, atol=1e-11)
    photon_density = (np.array([1e-4, 2e-4]) * np.array(WAVE) * 1e-9
                      / (6.62607015e-34 * 299792458))
    expected = np.trapezoid(photon_density * GENERATION[:, 2], x=WAVE) * (4e-6)**2 * .002
    assert arrays["mean_electrons"][0, 0] == pytest.approx(expected, rel=1e-13)
    assert np.count_nonzero(arrays["mean_electrons"] > expected * 1e-9) == 1
    assert arrays["raw_frames"][0, 0, 0] > request["readout"]["black_level_dn"]
    assert np.isfinite(arrays["rgb_frames"]).all()
    summary = cropped["summary"]
    diagnostics = summary["charge_collection"]
    assert diagnostics["crop_support"] == "full_sensor_generation_then_receiver_crop"
    assert any("requires explicit combined optical" in item
               for item in diagnostics["limitations"])
    assert not any("support edge collection may omit" in item
                   for item in diagnostics["limitations"])
    assert diagnostics["capture_collected"] == pytest.approx(float(arrays["mean_electrons"].sum()))
    assert diagnostics["outside_capture_collected"] == pytest.approx(
        float(full_arrays["mean_electrons"].sum() - arrays["mean_electrons"].sum()))
    assert summary["array_geometry"]["collection_generated"] == "source_support"
    assert summary["array_geometry"]["source_support_collected_electrons"] == "source_support"
    assert summary["array_geometry"]["mean_electrons"] == "capture_crop"
    assert set(summary["array_units"]) == set(arrays)
    json.dumps(summary, allow_nan=False)

    rate_request = deepcopy(cropped_request)
    rate_request.pop("exposure_s")
    rate_request.pop("frames")
    rate = spatial.photoelectron_rate(rate_request)
    np.testing.assert_allclose(rate["arrays"]["photoelectron_rate"] * .002,
                               arrays["mean_electrons"], rtol=1e-13, atol=1e-11)
    np.testing.assert_allclose(rate["arrays"]["source_support_collected_photoelectron_rate"]
                               * .002, full_arrays["mean_electrons"], rtol=1e-13, atol=1e-11)
    assert set(rate["summary"]["array_units"]) == set(rate["arrays"])


def test_partial_transport_separates_physical_loss_from_outside_support_collection():
    model = _collection(offsets=[[-1, 0], [1, 1]], probabilities=[
        [[0.4, 0.6], [0.2, 0.8], [0.3, 0.7], [0.25, 0.75]],
        [[0.4, 0.6], [0.2, 0.8], [0.3, 0.7], [0.25, 0.75]],
    ])
    generated = np.zeros((4, 4))
    generated[0, 0] = 100   # native (0,3), Gr; local (0,0) would be R
    generated[3, 3] = 200   # native (3,6), Gb; local (3,3) would be B
    result = transport(generated, 500, model, origin=(0, 3), full_sensor_shape=(8, 9))
    arrays = result["arrays"]
    assert arrays["collected"][1, 1] == pytest.approx(80)
    assert arrays["collected"][2, 3] == pytest.approx(60)
    assert arrays["off_sensor_loss"][0, 0] == pytest.approx(20)
    assert arrays["outside_support_collected"][3, 3] == pytest.approx(140)
    assert arrays["off_sensor_loss"].sum() == pytest.approx(20)
    assert arrays["outside_support_collected"].sum() == pytest.approx(140)
    assert result["diagnostics"]["conservation_residual"] == pytest.approx(0)
    assert result["diagnostics"]["native_origin"] == [0, 3]
    assert result["diagnostics"]["full_sensor_shape"] == [8, 9]


def test_partial_source_combined_halo_matches_full_sensor_capture(native_request):
    request = _request(
        native_request,
        model=_collection(offsets=[[1, 0]], probabilities=[[[1]] * 4] * 2),
    )
    image = np.zeros((16, 20, 2))
    image[2, 6] = [1e-4, 2e-4]    # optical shift then carrier shift into ROI (4,6)
    image[0, 5] = [2e-4, 1e-4]    # omitted optical source cannot reach the ROI
    image[13, 5] = [1e-4, 2e-4]   # carrier exits supplied support but stays on sensor
    request["scene"]["spectral_irradiance"] = image
    request["scene"]["sampling"].update(full_sensor_shape=[16, 20], crop_origin=[0, 0])
    kernel = np.zeros((2, 3, 3))
    kernel[:, 2, 1] = 1
    request["psf"]["values"] = kernel.tolist()
    full = spatial.SpatialSceneRuntime().execute(request)

    partial = deepcopy(request)
    partial["scene"]["sampling"]["crop_origin"] = [1, 3]
    partial["scene"]["spectral_irradiance"] = image[1:15, 3:19]
    partial["scene"]["sampling"]["capture_crop"] = {
        "crop_origin": [4, 6], "shape": [8, 10],
    }
    output = spatial.SpatialSceneRuntime().execute(partial)
    arrays, summary = output["arrays"], output["summary"]
    roi = (slice(4, 12), slice(6, 16))
    for name in ("blurred_spectral_irradiance", "sensor_spectral_irradiance",
                 "channel_electrons", "mean_electrons", "cfa_channel_index"):
        np.testing.assert_allclose(arrays[name], full["arrays"][name][roi],
                                   rtol=1e-13, atol=1e-11)
    np.testing.assert_array_equal(arrays["raw_frames"],
                                  full["arrays"]["raw_frames"][:, roi[0], roi[1]])
    photons = np.array([1e-4, 2e-4]) * np.array(WAVE) * 1e-9 / (6.62607015e-34 * 299792458)
    edge_charge = np.trapezoid(photons * GENERATION[:, 2], x=WAVE) * (4e-6)**2 * .002
    assert arrays["mean_electrons"][0, 0] == pytest.approx(edge_charge, rel=1e-13)
    assert arrays["raw_frames"][0, 0, 0] > request["readout"]["black_level_dn"]
    assert np.isfinite(arrays["rgb_frames"]).all()
    assert summary["charge_collection"]["crop_support"] == (
        "partial_source_combined_halo_then_receiver_crop")
    assert any("support edge collection may omit" in item
               for item in summary["charge_collection"]["limitations"])
    assert summary["charge_collection"]["totals"]["outside_support_collected"] > 0
    assert summary["charge_collection"]["totals"]["off_sensor_loss"] == pytest.approx(0)
    assert summary["charge_collection"]["conservation_residual"] == pytest.approx(0, abs=1e-9)
    assert summary["charge_collection"]["capture_collected"] == pytest.approx(
        float(arrays["mean_electrons"].sum()))
    assert summary["optical_support"]["required_source_support"]["crop_origin"] == [2, 5]
    assert summary["optical_support"]["carrier_destination_offset_bounds_rc"] == {
        "minimum": [1, 0], "maximum": [1, 0],
    }
    assert summary["optical_support"]["source_support_collected_scope"].startswith(
        "from_supplied_sources_only")
    assert summary["array_geometry"]["collection_outside_support_collected"] == "source_support"
    assert set(arrays) == set(summary["array_units"])
    json.dumps(summary, allow_nan=False)

    rate_request = deepcopy(partial)
    rate_request.pop("exposure_s")
    rate_request.pop("frames")
    rate = spatial.photoelectron_rate(rate_request)
    np.testing.assert_allclose(rate["arrays"]["photoelectron_rate"] * .002,
                               arrays["mean_electrons"], rtol=1e-13, atol=1e-11)
    assert set(rate["arrays"]) == set(rate["summary"]["array_units"])


def test_mixed_psf_and_opposite_carrier_offsets_preserve_partial_receiver(native_request):
    model = _collection(offsets=[[-1, 2], [2, -1]], probabilities=[
        [[0.3, 0.6], [0.5, 0.4], [0.2, 0.7], [0.6, 0.3]],
        [[0.7, 0.2], [0.4, 0.5], [0.6, 0.3], [0.3, 0.6]],
    ])
    request = _request(native_request, model=model)
    rng = np.random.default_rng(20260927)
    image = rng.uniform(1e-6, 5e-5, size=(20, 24, 2))
    kernel = rng.uniform(0.01, 1, size=(2, 3, 5))
    kernel /= kernel.sum(axis=(1, 2), keepdims=True)
    request["scene"]["spectral_irradiance"] = image
    request["scene"]["sampling"].update(full_sensor_shape=[20, 24], crop_origin=[0, 0])
    request["psf"]["values"] = kernel.tolist()
    complete = spatial.SpatialSceneRuntime().execute(request)["arrays"]

    partial = deepcopy(request)
    partial["scene"]["sampling"]["crop_origin"] = [1, 1]
    partial["scene"]["spectral_irradiance"] = image[1:17, 1:21]
    partial["scene"]["sampling"]["capture_crop"] = {
        "crop_origin": [5, 7], "shape": [8, 10],
    }
    output = spatial.SpatialSceneRuntime().execute(partial)
    roi = (slice(5, 13), slice(7, 17))
    for name in ("blurred_spectral_irradiance", "sensor_spectral_irradiance",
                 "mean_electrons", "channel_electrons"):
        np.testing.assert_allclose(output["arrays"][name], complete[name][roi],
                                   rtol=1e-12, atol=1e-10)
    np.testing.assert_array_equal(output["arrays"]["raw_frames"],
                                  complete["raw_frames"][:, roi[0], roi[1]])
    assert output["summary"]["optical_support"]["required_source_support"] == {
        "kind": "native_sensor", "full_sensor_shape": [20, 24],
        "crop_origin": [2, 3], "shape": [13, 17],
    }


def test_collection_capture_crop_refuses_missing_combined_halo(native_request):
    request = _request(
        native_request,
        model=_collection(offsets=[[1, 0]], probabilities=[[[1]] * 4] * 2),
    )
    request["scene"]["sampling"].update(full_sensor_shape=[16, 20], crop_origin=[3, 5])
    request["scene"]["sampling"]["capture_crop"] = {
        "crop_origin": [4, 6], "shape": [8, 10],
    }
    request["scene"]["spectral_irradiance"] = np.zeros((10, 12, 2))
    request["psf"]["values"] = (np.ones((2, 3, 3)) / 9).tolist()
    # This rectangle satisfies the old optical-only PSF halo, but misses the
    # extra source row that can move charge into the first receiver row.
    with pytest.raises(RuntimeRequestError, match="combined PSF and carrier halo"):
        spatial.SpatialSceneRuntime().execute(request)

    request["scene"]["sampling"].pop("capture_crop")
    with pytest.raises(RuntimeRequestError, match="full-sensor native image"):
        spatial.SpatialSceneRuntime().execute(request)


@pytest.mark.parametrize("mode", ["temporal", "hdr"])
def test_vertical_transfer_integrates_receiver_rolling_window_not_source_window(
    native_request, mode
):
    model = _collection(offsets=[[1, 0]], probabilities=[[[1]] * 4] * 2)
    request = _request(native_request, mode=mode, model=model)
    first, second = request["scene_sequence"]["knots"]
    second["scene"]["spectral_irradiance"] = _image() * 4
    request["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=1_000_000)
    request["control_sequence"]["frames"] = request["control_sequence"]["frames"][:1]
    if mode == "hdr":
        request["capture_program"]["hdr_merge"] = None
    runtime = TemporalSceneRuntime() if mode == "temporal" else HDRBracketRuntime()
    result = runtime.execute(request)
    arrays = result["arrays"]
    base = _reference(_image(), GENERATION, model)
    # The scene grows linearly by 3 over 20 ms. Destination row r integrates
    # [r,r+2] ms, so its average multiplier is 1 + 3*(r+1)/20.
    expected = base["collected"] * (0.002 * (1 + 3 * (np.arange(8) + 1) / 20))[:, None]
    np.testing.assert_allclose(arrays["mean_electrons"][0], expected, rtol=4e-13, atol=1e-12)
    np.testing.assert_array_equal(
        arrays["raw_frames"][0], _integer_raw(expected, request["readout"])
    )
    wrong_source_window = base["collected"] * (0.002 * (1 + 3 * np.arange(8) / 20))[:, None]
    assert np.max(np.abs(expected - wrong_source_window)) > 50
    assert (
        np.count_nonzero(
            arrays["raw_frames"][0] != _integer_raw(wrong_source_window, request["readout"])
        )
        > 50
    )
    for key in ("generated", "intrinsic_loss", "off_sensor_loss"):
        observed = arrays["collection_" + key + "_rate_scenes"]
        np.testing.assert_allclose(observed[0], base[key], rtol=4e-13, atol=1e-10)
        np.testing.assert_allclose(observed[1], 4 * base[key], rtol=4e-13, atol=1e-10)
    assert set(arrays) == set(result["summary"]["array_units"])
    if mode == "temporal":
        # Continuous expected DN remains separate from rounded sampled RAW DN.
        scale = (4095 - 64) / request["readout"]["full_well_e"]
        np.testing.assert_allclose(
            result["summary"]["prediction"]["values"],
            [expected[SITES == site].mean() * scale for site in range(4)],
            rtol=4e-13,
        )


@pytest.mark.parametrize("mode", ["hdr", "dcg"])
def test_collection_reaches_real_electrical_capture_and_shared_dcg_paths(native_request, mode):
    request = _request(native_request, mode=mode)
    runtime = HDRBracketRuntime() if mode == "hdr" else SharedChargeDCGRuntime()
    result = runtime.execute(request)
    arrays, summary = result["arrays"], result["summary"]
    rate = _reference(_image(), GENERATION, request["charge_collection"])["collected"]
    expected = np.array([0.002, 0.004])[:, None, None] * rate
    np.testing.assert_allclose(arrays["mean_electrons"], expected, rtol=4e-13, atol=1e-12)
    np.testing.assert_allclose(
        arrays["electrical_mean_total_charge_e"], expected, rtol=4e-13, atol=1e-12
    )
    if mode == "hdr":
        np.testing.assert_array_equal(
            arrays["raw_frames"], _integer_raw(expected, request["readout"])
        )
    else:
        model = request["capture_program"]["electrical_model"]
        np.testing.assert_allclose(
            arrays["electrical_shared_charge_e"], expected, rtol=4e-13, atol=1e-12
        )
        for frame, exposure in enumerate((0.002, 0.004)):
            for path_index, path in enumerate(model["paths"]):
                np.testing.assert_array_equal(
                    arrays["raw_groups"][frame, path_index],
                    expected_integer_raw(expected[frame], exposure, model, path),
                )
    assert "sensor_charge_collection" in summary["prediction"]["included_effects"]
    raw = arrays["raw_frames"][:, None] if mode == "hdr" else arrays["raw_groups"]
    black_levels = (
        [request["readout"]["black_level_dn"]]
        if mode == "hdr"
        else [path["adc"]["black_level_dn"] for path in model["paths"]]
    )
    sampled_means = [
        float((frame[path_index].astype(float) - black)[SITES == site].mean())
        for frame in raw
        for path_index, black in enumerate(black_levels)
        for site in range(4)
    ]
    np.testing.assert_array_equal(summary["prediction"]["values"], sampled_means)
    assert set(arrays) == set(summary["array_units"])
    assert runtime.estimate_resources(request)["working_array_bytes_estimate"] >= sum(
        array.nbytes for array in arrays.values()
    )


def test_noisy_dcg_samples_one_collected_charge_for_both_adc_paths(native_request):
    request = _request(native_request, mode="dcg")
    request["noise_enabled"] = True
    result = SharedChargeDCGRuntime().execute(request)
    arrays = result["arrays"]
    shared = arrays["electrical_shared_charge_e"]
    assert np.any(shared != arrays["mean_electrons"])
    model = request["capture_program"]["electrical_model"]
    for frame, exposure in enumerate((0.002, 0.004)):
        for path_index, path in enumerate(model["paths"]):
            np.testing.assert_array_equal(
                arrays["raw_groups"][frame, path_index],
                expected_integer_raw(shared[frame], exposure, model, path),
            )


@pytest.mark.parametrize("mode", ["spatial", "temporal", "hdr", "dcg"])
def test_total_collection_loss_preserves_dark_readout_and_finite_evidence(native_request, mode):
    request = _request(
        native_request,
        mode=mode,
        model=_collection(offsets=[[0, 0]], probabilities=[[[0]] * 4] * 2),
    )
    request["readout"]["dark_current_e_per_s"] = 1000
    if mode in {"hdr", "dcg"}:
        request["capture_program"]["electrical_model"]["dark_current_e_per_s"] = 1000
    runtime = {
        "spatial": spatial.SpatialSceneRuntime,
        "temporal": TemporalSceneRuntime,
        "hdr": HDRBracketRuntime,
        "dcg": SharedChargeDCGRuntime,
    }[mode]()
    result = runtime.execute(request)
    arrays = result["arrays"]
    assert not arrays["mean_electrons"].any()
    assert not arrays["channel_electrons"].any()
    generated = "collection_generated" + ("" if mode == "spatial" else "_rate_scenes")
    loss = generated.replace("generated", "intrinsic_loss")
    np.testing.assert_array_equal(arrays[generated], arrays[loss])
    if mode == "dcg":
        model = request["capture_program"]["electrical_model"]
        for frame, exposure in enumerate((0.002, 0.004)):
            for path_index, path in enumerate(model["paths"]):
                np.testing.assert_array_equal(
                    arrays["raw_groups"][frame, path_index],
                    expected_integer_raw(np.zeros(SHAPE), exposure, model, path),
                )
    else:
        exposures = [0.002] if mode == "spatial" else [0.002, 0.004]
        for frame, exposure in enumerate(exposures):
            np.testing.assert_array_equal(
                arrays["raw_frames"][frame],
                _integer_raw(np.full(SHAPE, exposure * 1000), request["readout"]),
            )
    assert set(arrays) == set(result["summary"]["array_units"])
    json.dumps(result["summary"], allow_nan=False)


def test_prepared_capture_collection_evidence_cannot_mutate_later_frames(native_request):
    request = _request(native_request, mode="temporal")
    session = TemporalSceneRuntime().prepare_capture(request)
    frame = request["control_sequence"]["frames"][0]
    output = session.capture_frame(frame, seed_index=0)
    keys = [
        key
        for key in output["arrays"]
        if key.startswith("collection_") and key.endswith("_rate_scenes")
    ]
    assert len(keys) == 3
    for key in keys:
        assert not output["arrays"][key].flags.writeable
        with pytest.raises(ValueError):
            output["arrays"][key].flags.writeable = True
        with pytest.raises(ValueError):
            output["arrays"][key][...] = -1
    again = session.capture_frame(frame, seed_index=0)
    for key in keys + ["raw_frames"]:
        np.testing.assert_array_equal(output["arrays"][key], again["arrays"][key])


@pytest.mark.parametrize("mode", ["temporal", "hdr", "dcg"])
def test_collection_working_budget_refuses_before_allocating_optical_rates(
    native_request, monkeypatch, mode
):
    import camera_platform.dcg_runtime as dcg_module
    import camera_platform.hdr_runtime as hdr_module
    import camera_platform.temporal as temporal_module

    request = _request(native_request, mode=mode)
    runtime = {
        "temporal": TemporalSceneRuntime,
        "hdr": HDRBracketRuntime,
        "dcg": SharedChargeDCGRuntime,
    }[mode]()
    module = {"temporal": temporal_module, "hdr": hdr_module, "dcg": dcg_module}[mode]
    estimate = runtime.estimate_resources(request)["working_array_bytes_estimate"]
    monkeypatch.setattr(module, "MAX_WORKING_ARRAY_BYTES", estimate - 1)
    monkeypatch.setattr(
        TemporalSceneRuntime,
        "_optical_rates",
        lambda *args: pytest.fail("Optical rates allocated before budget refusal"),
    )
    with pytest.raises(RuntimeRequestError, match="array memory|memory budget|working"):
        runtime.execute(request)


@pytest.mark.parametrize("mode", ["spatial", "temporal", "hdr", "dcg"])
@pytest.mark.parametrize(
    "violation", ["partial_grid", "offset_grid", "already_collected", "unpaired_generation"]
)
def test_unsupported_geometry_and_double_collection_fail_before_capture(
    native_request, mode, violation
):
    request = _request(native_request, mode=mode)
    scenes = (
        [request["scene"]]
        if mode == "spatial"
        else [knot["scene"] for knot in request["scene_sequence"]["knots"]]
    )
    if violation in {"partial_grid", "offset_grid"}:
        for scene in scenes:
            scene["sampling"]["full_sensor_shape"] = [10, 12]
            if violation == "offset_grid":
                scene["sampling"]["crop_origin"] = [1, 1]
    elif violation == "already_collected":
        request["native_pixel_response"] = deepcopy(native_request["native_pixel_response"])
    else:
        request.pop("charge_collection")
    runtime = {
        "spatial": spatial.SpatialSceneRuntime,
        "temporal": TemporalSceneRuntime,
        "hdr": HDRBracketRuntime,
        "dcg": SharedChargeDCGRuntime,
    }[mode]()
    assert runtime.validate_request(request)
    with pytest.raises(RuntimeRequestError):
        runtime.execute(request)


@pytest.mark.parametrize("mode", ["hdr", "dcg"])
def test_receiver_prnu_cannot_silently_multiply_transported_charge(native_request, mode):
    request = _request(native_request, mode=mode)
    request["capture_program"]["electrical_model"]["fixed_pattern"]["prnu_sigma"] = 0.02
    runtime = HDRBracketRuntime() if mode == "hdr" else SharedChargeDCGRuntime()
    with pytest.raises(RuntimeRequestError, match="PRNU|prnu"):
        runtime.execute(request)


# Captured from the unchanged v0.16.0.dev7 implementation before this feature's
# runtime edits. Include every array's key, dtype, shape and bytes, plus the whole
# summary, so an absent model preserves existing evidence as well as visible RAW.
# Historical/current two-environment proof and exact metadata differences:
# reports/camera_platform/install-v016/regression-diagnosis/. Only live dependency
# version labels are checked separately and restored to the original golden labels.
LEGACY_SHA256 = {
    "spatial": (
        "2ae8ee2d9985f0ac7504a474f082a8ccdd68c87715c58ed7310495508e49cd62",
        "1964c1418e9191bb2b618baddda657ed7f4ca088ab97cbada8f95f40a4324f87",
    ),
    "temporal": (
        "6a6ba08781883c901d1df549709dd4453479f0e9b7d8c21fdebd3360f9803e31",
        "1c35dbb353c5ea7f5bda0c1f9af85ce33a1aa8d0b1ab5ee444a77fccfaeabe57",
    ),
    "hdr": (
        "ec7a4f73abc802cf75cc5b12e4cda29498a029c3176aa05d5809c63d193d29c4",
        "f985c48cc138691310a4369e1a33f2dbb1ea2efa9f3c052955b9eae95d2978bb",
    ),
    "dcg": (
        "07f6383b8d4a90256b69994dc3ace138cf0056fc5051f6fccaf2b1e334b304d9",
        "eb4ae0e172aaf98de31d338bdb6f5932959161e132aa529775699c86ae7e5964",
    ),
}


@pytest.mark.parametrize("mode", list(LEGACY_SHA256))
def test_absent_collection_preserves_all_legacy_array_bytes_and_summary(native_request, mode):
    request = _pure_request() if mode == "spatial" else deepcopy(native_request)
    if mode in {"hdr", "dcg"}:
        request["capture_program"] = {"hdr": hdr_program, "dcg": dcg_program}[mode](
            request["readout"]
        )
    runtime = {
        "spatial": spatial.SpatialSceneRuntime,
        "temporal": TemporalSceneRuntime,
        "hdr": HDRBracketRuntime,
        "dcg": SharedChargeDCGRuntime,
    }[mode]()
    output = runtime.execute(request)
    arrays_hash = hashlib.sha256()
    for name, array in sorted(output["arrays"].items()):
        arrays_hash.update(name.encode() + b"\0")
        arrays_hash.update(array.dtype.str.encode() + b"\0")
        arrays_hash.update(json.dumps(array.shape).encode() + b"\0")
        arrays_hash.update(array.tobytes())
    summary = deepcopy(output["summary"])
    if mode == "spatial":
        implementations = [summary["isp"]["implementation"]]
    elif mode in {"temporal", "hdr"}:
        assert len(summary["isp_per_frame"]) == 2
        implementations = [frame["implementation"] for frame in summary["isp_per_frame"]]
    else:
        assert len(summary["isp_per_acquisition"]) == 2
        assert all(len(acquisition["paths"]) == 2
                   for acquisition in summary["isp_per_acquisition"])
        implementations = [path["implementation"]
                           for acquisition in summary["isp_per_acquisition"]
                           for path in acquisition["paths"]]
    for implementation in implementations:
        assert implementation["numpy_version"] == np.__version__
        assert implementation["scipy_version"] == scipy.__version__
        implementation.update(numpy_version="2.4.2", scipy_version="1.17.1")
    summary_hash = hashlib.sha256(
        json.dumps(
            summary, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    )
    assert (arrays_hash.hexdigest(), summary_hash.hexdigest()) == LEGACY_SHA256[mode]
