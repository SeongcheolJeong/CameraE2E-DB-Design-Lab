"""Analytic synthetic fixtures: implementation evidence, never measured optics."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform.isp import default_profile
from camera_platform.runtime import LIGHT_M_S, PLANCK_J_S, RuntimeRequestError
from camera_platform.spatial import (
    SpatialSceneRuntime,
    photoelectron_rate,
    request_template,
    validate_psf,
)


@pytest.fixture
def request_data():
    data = request_template()
    data["scene"]["spectral_irradiance"] = np.full((16, 20, 5), 1e-5)
    data["scene"]["sampling"]["full_sensor_shape"] = [16, 20]
    data.update(frames=1, noise_enabled=False)
    data["psf"]["values"] = np.ones((5, 1, 1))
    return data


def run(data):
    return SpatialSceneRuntime().execute(data)


def test_spatial_raw_capture_does_not_fetch_unused_color_filters(request_data, monkeypatch):
    from pyisetcam.assets import AssetStore

    def no_implicit_asset_store(*_args, **_kwargs):
        raise AssertionError("Spatial RAW capture must not load upstream assets.")

    monkeypatch.setattr(AssetStore, "default", no_implicit_asset_store)
    result = run(request_data)
    assert result["arrays"]["raw_frames"].shape[-2:] == (16, 20)


def uniform_optics(data):
    data["lens"]["relative_illumination"] = [[1, 1]] * 5
    data["sensor_angular_response"]["relative_response"] = [[[1, 1]] * 5] * 3


def test_delta_psf_identity_and_absolute_photoelectron_units(request_data):
    uniform_optics(request_data)
    output = run(request_data)
    arrays, summary = output["arrays"], output["summary"]
    np.testing.assert_allclose(arrays["blurred_spectral_irradiance"],
                               request_data["scene"]["spectral_irradiance"], rtol=1e-14)
    photons = 1e-5 * 1e-9 * (650**2 - 450**2) / 2 / (PLANCK_J_S * LIGHT_M_S)
    electrons = photons * (4e-6)**2 * 0.004 * np.array([0.3, 0.5, 0.2])
    np.testing.assert_allclose(arrays["channel_electrons"],
                               np.broadcast_to(electrons, (16, 20, 3)), rtol=2e-14)
    flux = summary["flux_diagnostics"]
    np.testing.assert_allclose(flux["input_spectral_flux"], [1e-5 * 16 * 20 * (4e-6)**2] * 5)
    np.testing.assert_allclose(flux["full_convolution_spectral_flux"], flux["input_spectral_flux"])
    assert summary["geometry"] == {"kind": "native_sensor", "full_sensor_shape": [16, 20],
                                    "crop_origin": [0, 0]}
    assert set(summary["array_units"]) == set(arrays)
    assert summary["prediction"]["metric_id"] == "raw.channel_mean_dn"
    assert summary["prediction"]["uncertainty"] == {"kind": "unknown"}
    json.dumps(summary, allow_nan=False)


def test_psf_convolution_orientation_not_correlation(request_data):
    image = request_data["scene"]["spectral_irradiance"]
    image[:] = 0
    image[7, 8] = 1e-4
    kernels = np.zeros((5, 3, 3))
    kernels[:, 1, 2] = 1
    request_data["psf"]["values"] = kernels
    blurred = run(request_data)["arrays"]["blurred_spectral_irradiance"]
    expected = np.zeros_like(image)
    expected[7, 9] = 1e-4
    np.testing.assert_allclose(blurred, expected, atol=1e-19)


def test_zero_exterior_energy_leaves_crop_instead_of_wrapping(request_data):
    image = request_data["scene"]["spectral_irradiance"]
    image[:] = 0
    image[0, 0] = 1e-4
    kernel = np.outer([1, 2, 1], [1, 2, 1]) / 16
    request_data["psf"]["values"] = np.repeat(kernel[None], 5, axis=0)
    result = run(request_data)
    flux = result["summary"]["flux_diagnostics"]
    np.testing.assert_allclose(flux["retained_crop_spectral_flux"],
                               np.asarray(flux["input_spectral_flux"]) * 9 / 16)
    np.testing.assert_allclose(flux["outside_crop_spectral_flux"],
                               np.asarray(flux["input_spectral_flux"]) * 7 / 16)
    assert result["arrays"]["blurred_spectral_irradiance"][-1, -1].max() < 1e-19


def test_declared_psf_halo_matches_full_sensor_capture_roi(request_data):
    uniform_optics(request_data)
    rows, cols = np.indices((16, 20))
    scene = np.broadcast_to(
        (1 + rows[..., None] / 16 + cols[..., None] / 20) * 1e-5,
        (16, 20, 5),
    ).copy()
    scene[4, 8, :] += 3e-5  # A source outside the capture ROI contributes inside it.
    request_data["scene"]["spectral_irradiance"] = scene
    kernel = np.outer([1, 2, 1], [1, 2, 1]) / 16
    request_data["psf"]["values"] = np.repeat(kernel[None], 5, axis=0)
    full = run(request_data)

    halo = deepcopy(request_data)
    halo["scene"]["spectral_irradiance"] = scene[4:14, 6:16].copy()
    halo["scene"]["sampling"]["crop_origin"] = [4, 6]
    halo["scene"]["sampling"]["capture_crop"] = {
        "crop_origin": [5, 7], "shape": [8, 8],
    }
    cropped = run(halo)
    for name in ("blurred_spectral_irradiance", "sensor_spectral_irradiance",
                 "channel_electrons", "mean_electrons", "radius_map",
                 "relative_illumination_map", "cfa_channel_index", "reference_dn"):
        np.testing.assert_allclose(cropped["arrays"][name],
                                   full["arrays"][name][5:13, 7:15], rtol=1e-13, atol=1e-12)
    np.testing.assert_array_equal(cropped["arrays"]["raw_frames"],
                                  full["arrays"]["raw_frames"][:, 5:13, 7:15])
    support_blurred = cropped["arrays"]["source_support_blurred_spectral_irradiance"]
    assert support_blurred.shape == (10, 10, 5)
    np.testing.assert_array_equal(
        support_blurred[1:9, 1:9], cropped["arrays"]["blurred_spectral_irradiance"]
    )
    summary = cropped["summary"]
    assert summary["output_size"] == [8, 8]
    assert summary["geometry"]["crop_origin"] == [5, 7]
    assert cropped["arrays"]["cfa_channel_index"][0, 0] == 2
    assert summary["optical_support"]["source_support"]["crop_origin"] == [4, 6]
    assert summary["optical_support"]["capture_crop"]["shape"] == [8, 8]
    assert summary["optical_support"]["psf_halo_pixels"] == [1, 1]
    assert summary["array_geometry"]["ideal_spectral_irradiance"] == "source_support"
    assert summary["array_geometry"]["blurred_spectral_irradiance"] == "capture_crop"
    assert summary["array_geometry"]["rgb_frames"] == "capture_crop"
    flux = summary["flux_diagnostics"]
    assert flux["retained_crop_geometry"] == "source_support"
    np.testing.assert_allclose(flux["capture_spectral_flux"],
                               cropped["arrays"]["blurred_spectral_irradiance"].sum((0, 1))
                               * (4e-6) ** 2)

    def rate_request(value):
        result = deepcopy(value)
        result.pop("exposure_s")
        result.pop("frames")
        return result

    full_rate = photoelectron_rate(rate_request(request_data))
    halo_rate = photoelectron_rate(rate_request(halo))
    np.testing.assert_allclose(halo_rate["arrays"]["photoelectron_rate"],
                               full_rate["arrays"]["photoelectron_rate"][5:13, 7:15],
                               rtol=1e-13)
    assert halo_rate["summary"]["array_geometry"]["photoelectron_rate"] == "capture_crop"


def test_capture_crop_rejects_missing_optical_halo(request_data):
    request_data["scene"]["sampling"]["full_sensor_shape"] = [24, 28]
    request_data["scene"]["sampling"]["crop_origin"] = [4, 6]
    request_data["scene"]["sampling"]["capture_crop"] = {
        "crop_origin": [4, 6], "shape": [16, 20],
    }
    request_data["psf"]["values"] = np.ones((5, 3, 3)) / 9
    with pytest.raises(RuntimeRequestError, match="missing the PSF halo"):
        run(request_data)
    request_data["charge_collection"] = {}
    with pytest.raises(RuntimeRequestError, match="camera_charge_collection_v1 fields"):
        run(request_data)


def test_each_wavelength_uses_its_declared_psf(request_data):
    image = request_data["scene"]["spectral_irradiance"]
    image[:] = 0
    image[7, 8] = np.arange(1, 6) * 1e-5
    kernels = np.zeros((5, 3, 3))
    for band in range(5):
        kernels[band, 1, band % 3] = 1
    request_data["psf"]["values"] = kernels
    output = run(request_data)["arrays"]["blurred_spectral_irradiance"]
    for band in range(5):
        assert np.unravel_index(output[..., band].argmax(), output.shape[:2]) == (7, 7 + band % 3)
        assert output[..., band].max() == pytest.approx((band + 1) * 1e-5)


def test_spatial_psf_changes_real_raw_and_rgb(request_data):
    request_data["scene"]["spectral_irradiance"][:, 10:] *= 4
    sharp = run(request_data)["arrays"]
    request_data["psf"]["values"] = np.ones((5, 3, 3)) / 9
    blurred = run(request_data)["arrays"]
    assert not np.array_equal(sharp["raw_frames"], blurred["raw_frames"])
    assert not np.array_equal(sharp["rgb"], blurred["rgb"])
    np.testing.assert_allclose(sharp["ideal_spectral_irradiance"],
                               blurred["ideal_spectral_irradiance"])


def test_native_crop_preserves_sensor_position_cfa_phase_and_charge(request_data):
    image = np.full((32, 40, 5), 1e-5)
    image *= 1 + np.arange(40)[None, :, None] / 40
    request_data["scene"]["spectral_irradiance"] = image
    request_data["scene"]["sampling"]["full_sensor_shape"] = [32, 40]
    full = run(request_data)
    request_data["scene"]["spectral_irradiance"] = image[3:19, 5:25].copy()
    request_data["scene"]["sampling"]["crop_origin"] = [3, 5]
    cropped = run(request_data)
    for name in ("radius_map", "cfa_channel_index", "mean_electrons", "channel_electrons"):
        np.testing.assert_allclose(cropped["arrays"][name], full["arrays"][name][3:19, 5:25])
    np.testing.assert_array_equal(cropped["arrays"]["raw_frames"],
                                  full["arrays"]["raw_frames"][:, 3:19, 5:25])
    assert cropped["arrays"]["cfa_channel_index"][0, 0] == 2
    assert cropped["arrays"]["radius_map"].max() < 1


def test_independent_spectral_ri_and_angular_qe_integral(request_data):
    # Five intentionally different wavelength terms, including spectral RI.
    request_data["lens"]["relative_illumination"] = [[1, x] for x in [.9, .8, .7, .6, .5]]
    output = run(request_data)["arrays"]
    r = output["radius_map"][0, 0]
    expected = []
    wave = np.array(request_data["wavelength_nm"])
    for channel in range(3):
        spectral_terms = []
        for band in range(5):
            angular = request_data["sensor_angular_response"]["relative_response"][channel][band][1]
            ri_edge = request_data["lens"]["relative_illumination"][band][1]
            spectral_terms.append(
                1e-5 * (1 + (ri_edge - 1) * r)
                * request_data["qe"][channel][band] * (1 + (angular - 1) * r)
                * wave[band] * 1e-9 / (PLANCK_J_S * LIGHT_M_S)
            )
        expected.append(np.trapezoid(spectral_terms, x=wave) * (4e-6)**2 * .004)
    np.testing.assert_allclose(output["channel_electrons"][0, 0], expected, rtol=1e-14)


def test_area_exposure_scaling_and_readout_gain_are_separate(request_data):
    initial = run(request_data)["arrays"]
    request_data["exposure_s"] *= 2
    request_data["pixel_pitch_um"] *= 2
    request_data["scene"]["sampling"]["pixel_pitch_um"] *= 2
    request_data["psf"]["sample_pitch_um"] *= 2
    scaled = run(request_data)["arrays"]
    np.testing.assert_allclose(scaled["mean_electrons"], initial["mean_electrons"] * 8)
    request_data["readout"]["gain"] = 2
    amplified = run(request_data)["arrays"]
    np.testing.assert_array_equal(amplified["mean_electrons"], scaled["mean_electrons"])
    assert amplified["raw_frames"].mean() > scaled["raw_frames"].mean()


def test_isp_tuning_keeps_new_capture_fixed(request_data):
    initial = run(request_data)
    request_data["isp_profile"] = default_profile()
    request_data["isp_profile"]["white_balance"]["gains_rgb"] = [2, 1, 1]
    tuned = run(request_data)
    np.testing.assert_array_equal(initial["arrays"]["raw_frames"], tuned["arrays"]["raw_frames"])
    assert tuned["arrays"]["rgb"][..., 0].mean() > initial["arrays"]["rgb"][..., 0].mean()
    assert tuned["summary"]["prediction"] == initial["summary"]["prediction"]


def test_actual_noise_is_reproducible_and_variance_is_temporal(request_data):
    request_data.update(frames=16, noise_enabled=True)
    image_before = request_data["scene"]["spectral_irradiance"].copy()
    one, two = run(request_data), run(request_data)
    for key in one["arrays"]:
        np.testing.assert_array_equal(one["arrays"][key], two["arrays"][key])
    np.testing.assert_array_equal(image_before, request_data["scene"]["spectral_irradiance"])
    raw = one["arrays"]["raw_frames"]
    assert np.issubdtype(raw.dtype, np.integer)
    assert not np.array_equal(raw[0], raw[1])
    scale = one["summary"]["capture_metadata"][0]["dn_per_electron"]
    expected = (one["arrays"]["mean_electrons"].mean() + 4) * scale**2
    assert one["arrays"]["raw_temporal_variance_dn2"].mean() == pytest.approx(expected, rel=.12)


def test_prediction_uses_cfa_samples_and_clipped_dark_corrected_expectation(request_data):
    request_data["readout"].update(dark_current_e_per_s=100, full_well_e=100, gain=2)
    request_data["scene"]["spectral_irradiance"] *= 2
    result = run(request_data)
    arrays, summary = result["arrays"], result["summary"]
    for c, value in enumerate(summary["prediction"]["values"]):
        assert value == pytest.approx(arrays["channel_reference_signal_dn"][..., c][
            arrays["cfa_channel_index"] == c].mean())
    dark_dn = .4 * (4095 - 64) * 2 / 100
    np.testing.assert_allclose(summary["prediction"]["values"], [4095 - 64 - dark_dn] * 3)
    assert summary["linear_unsaturated"] is False
    assert summary["raw_temporal_snr_db"] is None
    request_data["scene"]["spectral_irradiance"][:] = 0
    dark = run(request_data)
    assert dark["summary"]["prediction"]["values"] == [0, 0, 0]
    assert np.all(dark["arrays"]["raw_frames"] > 64)


@pytest.mark.parametrize("change", [
    lambda d: d.update(output_size=[8, 8]),
    lambda d: d["scene"].update(unit="W/m^2/sr/nm"),
    lambda d: d["scene"].update(reference_plane="scene_radiance"),
    lambda d: d["scene"].update(included_effects=["on_axis_lens_throughput", "lens_psf"]),
    lambda d: d["scene"]["sampling"].update(kind="representative_normalized_field_raster"),
    lambda d: d["scene"]["sampling"].update(crop_origin=[1, 0]),
    lambda d: d["scene"]["sampling"].update(pixel_pitch_um=2),
    lambda d: d["psf"].update(sample_pitch_um=2),
    lambda d: d["psf"].update(boundary="wrap"),
    lambda d: d["psf"].update(sampling_model="point_sampled_optical_intensity"),
    lambda d: d["psf"].update(values=np.ones((5, 2, 2)) / 4),
    lambda d: d["psf"].update(values=np.ones((5, 1, 1)) * 2),
    lambda d: d["psf"].update(values=np.ones((5, 1, 1)) * -1),
    lambda d: d["psf"].update(wavelength_nm=[451, 500, 550, 600, 650]),
    lambda d: d["scene"].update(wavelength_nm=[450, 500, 550, 600]),
    lambda d: d["scene"].update(spectral_irradiance=np.zeros((16, 20, 3))),
    lambda d: d["scene"].update(spectral_irradiance=np.zeros((15, 20, 5))),
    lambda d: d["scene"].update(spectral_irradiance=np.full((16, 20, 5), np.nan)),
    lambda d: d["scene"].update(spectral_irradiance=np.full((16, 20, 5), -1)),
    lambda d: d["scene"].update(spectral_irradiance=np.full((16, 20, 5), "1")),
    lambda d: d["lens"].update(relative_illumination=[[1, 1e300]] * 5),
    lambda d: d.update(frames=17),
    lambda d: d["isp_profile"].update(precision="float16"),
])
def test_unsupported_or_ambiguous_inputs_refused_before_engine(request_data, change):
    data = deepcopy(request_data)
    data["isp_profile"] = default_profile()
    change(data)
    runtime = SpatialSceneRuntime()
    assert runtime.validate_request(data)
    with pytest.raises(RuntimeRequestError):
        runtime.execute(data)


def test_frame_pixel_budget_and_resource_estimate(request_data):
    runtime = SpatialSceneRuntime()
    estimate = runtime.estimate_resources(request_data)
    assert estimate["output_size"] == [16, 20]
    assert estimate["working_array_bytes_estimate"] > 16 * 20 * 5 * 8
    request_data["scene"]["spectral_irradiance"] = np.ones((512, 512, 5)) * 1e-5
    request_data["scene"]["sampling"]["full_sensor_shape"] = [512, 512]
    request_data["frames"] = 5
    with pytest.raises(RuntimeRequestError, match="frame-pixel"):
        runtime.estimate_resources(request_data)


def test_standalone_psf_returns_detached_json_without_renormalization(request_data):
    source = request_data["psf"]
    source["wavelength_nm"] = np.asarray(source["wavelength_nm"], dtype=np.float64)
    source["values"][:, 0, 0] = 1 + 5e-11  # permitted roundoff is retained, not renormalized
    normalized = validate_psf(source)
    assert normalized["values"][0][0][0] == 1 + 5e-11
    json.dumps(normalized, allow_nan=False)
    source["values"][0, 0, 0] = 0
    source["wavelength_nm"][0] = 100
    source["source"]["description"] = "changed original"
    assert normalized["values"][0][0][0] == 1 + 5e-11
    assert normalized["wavelength_nm"][0] == 450
    assert normalized["source"]["description"] != "changed original"
    normalized["source"]["description"] = "changed result"
    assert source["source"]["description"] == "changed original"


@pytest.mark.parametrize("patch", [
    {"wavelength_nm": [450, 450, 550, 600, 650]},
    {"wavelength_nm": [450, 550, 500, 600, 650]},
    {"wavelength_nm": [190, 500, 550, 600, 650]},
    {"wavelength_nm": [450, 500, 550, 600, 2501]},
    {"wavelength_nm": [450]},
    {"wavelength_nm": [450, 500, 550, 600, float("nan")]},
    {"wavelength_nm": ["450", "500", "550", "600", "650"]},
    {"sample_pitch_um": -1},
    {"sample_pitch_um": float("inf")},
    {"sample_pitch_um": True},
    {"sample_pitch_um": "4"},
    {"values": np.ones((5, 1, 1), dtype=complex)},
    {"values": np.ones((5, 67, 1)) / 67},
    {"values": np.ones((4, 1, 1))},
    {"values": np.zeros((5, 1, 1))},
    {"sampling_model": "point_sampled_optical_intensity"},
    {"normalization": "automatic"},
    {"source": {"kind": [], "description": "invalid"}},
    {"mtf": [1, .5, 0]},
])
def test_standalone_and_runtime_share_psf_refusals(request_data, patch):
    request_data["psf"].update(patch)
    with pytest.raises(ValueError):
        validate_psf(request_data["psf"])
    assert SpatialSceneRuntime().validate_request(request_data)


def test_standalone_psf_does_not_claim_sensor_compatibility(request_data):
    request_data["psf"]["sample_pitch_um"] = 8
    assert validate_psf(request_data["psf"])["sample_pitch_um"] == 8
    with pytest.raises(RuntimeRequestError, match="pixel pitch must match"):
        run(request_data)
