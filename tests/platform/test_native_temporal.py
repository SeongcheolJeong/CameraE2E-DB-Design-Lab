"""Independent authored four-site charge/readout checks; no physical validation claim."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import electrical, hdr
from camera_platform.hdr_runtime import HDRBracketRuntime, default_capture_program
from camera_platform.runtime import RuntimeRequestError
from camera_platform.temporal import TemporalSceneRuntime, request_template

SITES = ["R", "Gr", "Gb", "B"]
EQE = np.array([0.2, 0.35, 0.6, 0.8])


@pytest.fixture
def native_request():
    """Explicit synthetic LUT and normal rays, independent of any Factory evidence."""
    data = request_template()
    wave = [500, 600]
    data["wavelength_nm"] = wave
    data.pop("qe")
    data.pop("sensor_angular_response")
    data["noise_enabled"] = False
    data["readout"].update(read_noise_e=0, dark_current_e_per_s=0)
    data["native_pixel_response"] = {
        "schema": "camera_native_pixel_response_v1",
        "site_names": SITES.copy(),
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": 4.0,
        "reference_plane": "sensor_surface_before_pixel_optics",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "polarization": "unpolarized",
        "response_basis": "independent_site_collected_electrons_per_incident_pixel_photon",
        "wavelength_nm": wave,
        "theta_deg": [0],
        "phi_deg": [0],
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
        "values": np.broadcast_to(EQE, (2, 1, 1, 4)).tolist(),
        "source": {"kind": "estimated", "description": "Authored temporal acceptance LUT"},
    }
    data["lens"] = {
        "wavelength_nm": wave,
        "field_radius": [0, 1],
        "theta_deg": [0, 20],
        "angular_weights": [[[1, 0], [1, 0]]] * 2,
        "relative_illumination": [[1, 1]] * 2,
        "field_reference_radius_mm": 1,
        "directional_response": {
            "schema": "camera_lens_directional_response_v1",
            "wavelength_nm": wave,
            "field_radius": [0, 1],
            "image_height_mm": [0, 1],
            "field_reference_radius_mm": 1,
            "direction_convention": "propagation_into_sensor_x_column_y_row",
            "reference_plane": "sensor_surface_before_pixel_optics",
            "field_reference": "positive_x_column",
            "symmetry": "centered_spherical_or_plane_prescription_radial_stop",
            "normalization": "conditional_transmitted_irradiance_fraction",
            "prescription_sha256": "a" * 64,
            "stop_transmission_sha256": "b" * 64,
            "directions_xyz": [[[0, 0, 1]], [[0, 0, 1]]],
            "conditional_weights": [[[1], [1]], [[1], [1]]],
            "on_axis_rotation_phi_deg": list(range(0, 360, 45)),
        },
    }
    data["psf"].update(wavelength_nm=wave, values=[[[1]], [[1]]])
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["wavelength_nm"] = wave
        knot["scene"]["spectral_irradiance"] = np.full((8, 10, 2), 1e-4)
        knot["scene"]["sampling"].update(full_sensor_shape=[12, 14], crop_origin=[1, 1])
    for index, frame in enumerate(data["control_sequence"]["frames"]):
        frame["applied"].update(exposure_ns=2_000_000 * (index + 1),
                                applied_at_ns=frame["frame_start_ns"])
    return data


def expected_rate(data):
    """Direct area and two-node wavelength trapezoid, without runtime helpers."""
    photon_spectrum = 1e-4 * np.array([500, 600]) * 1e-9 / (6.62607015e-34 * 299792458)
    photon_integral = (600 - 500) * (photon_spectrum[0] + photon_spectrum[1]) / 2
    return (data["pixel_pitch_um"] * 1e-6) ** 2 * photon_integral * EQE


@pytest.mark.parametrize("runtime_kind", ["temporal", "hdr"])
def test_unequal_sites_integrate_twice_and_reach_actual_integer_raw(native_request, runtime_kind):
    data = native_request
    runtime = TemporalSceneRuntime()
    if runtime_kind == "hdr":
        data["capture_program"] = default_capture_program(data["readout"])
        runtime = HDRBracketRuntime()
    output = runtime.execute(data)
    arrays, summary = output["arrays"], output["summary"]
    site_rates = expected_rate(data)
    expected = np.broadcast_to(site_rates, (1, 8, 10, 4))
    np.testing.assert_allclose(arrays["channel_photoelectron_rate_scenes"], expected, rtol=3e-14)
    assert arrays["channel_electrons"].shape == (2, 8, 10, 4)
    np.testing.assert_allclose(arrays["channel_electrons"][0], expected[0] * .002, rtol=3e-14)
    np.testing.assert_array_equal(arrays["mean_electrons"][1], 2 * arrays["mean_electrons"][0])
    cfa = np.array([[0, 1], [2, 3]])[(np.arange(8)[:, None] + 1) % 2,
                                     (np.arange(10)[None, :] + 1) % 2]
    np.testing.assert_array_equal(arrays["cfa_channel_index"], cfa)
    np.testing.assert_array_equal(arrays["native_site_index"], cfa)
    np.testing.assert_array_equal(arrays["native_eqe"], data["native_pixel_response"]["values"])
    assert summary["native_pixel_response"]["site_names"] == SITES
    assert summary["native_pixel_response"]["response_basis"] == (
        "independent_site_collected_electrons_per_incident_pixel_photon")
    assert cfa[0, 0] == 3  # Native B is site 3, not RGB channel 2 / Gb.
    scale = (2**data["readout"]["bit_depth"] - 1 - data["readout"]["black_level_dn"]
             ) / data["readout"]["full_well_e"]
    expected_raw = np.rint(data["readout"]["black_level_dn"]
                          + np.array([.002, .004])[:, None, None] * site_rates[cfa] * scale)
    np.testing.assert_array_equal(arrays["raw_frames"], expected_raw)
    assert arrays["raw_frames"].dtype.kind in "iu"
    assert summary["channels"] == SITES
    assert summary["prediction"]["axes"][-1]["values"] == SITES
    assert len(summary["prediction"]["values"]) == 8
    assert arrays["raw_frames"][0][cfa == 1].mean() < arrays["raw_frames"][0][cfa == 2].mean()
    assert arrays["rgb_frames"].shape == (2, 8, 10, 3)
    assert set(arrays) == set(summary["array_units"])
    if runtime_kind == "hdr":
        assert arrays["hdr_valid_mask"].all()
        assert (arrays["hdr_merged_rate"][cfa == 1].mean()
                < arrays["hdr_merged_rate"][cfa == 2].mean())
        # Quantization is propagated through each actual exposure, never hidden.
        np.testing.assert_allclose(arrays["hdr_merged_rate"], site_rates[cfa],
                                   rtol=0, atol=.5 / (scale * .002))
    estimate = runtime.estimate_resources(data)
    assert estimate["working_array_bytes_estimate"] >= sum(a.nbytes for a in arrays.values())


@pytest.mark.parametrize("origin", [[0, 0], [0, 1], [1, 0], [1, 1]])
def test_every_crop_phase_retains_physical_green_sites_and_bayer_isp(native_request, origin):
    data = native_request
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"]["crop_origin"] = origin
    data["capture_program"] = default_capture_program(data["readout"])
    output = HDRBracketRuntime().execute(data)
    arrays = output["arrays"]
    site = np.array([[0, 1], [2, 3]])[(np.arange(8)[:, None] + origin[0]) % 2,
                                      (np.arange(10)[None, :] + origin[1]) % 2]
    np.testing.assert_array_equal(arrays["cfa_channel_index"], site)
    raw = arrays["raw_frames"][0]
    assert (raw[site == 0].mean() < raw[site == 1].mean()
            < raw[site == 2].mean() < raw[site == 3].mean())
    # ISP consumes the ordinary native Bayer RAW; the native site axis must not
    # cause Gb to become blue or drop the true blue sites.
    rgb = arrays["rgb_frames"][0]
    assert np.isfinite(rgb).all() and (rgb[..., 2] > 0).all()
    assert output["summary"]["raw_format"]["cfa"] == "RGGB"


def test_changed_green_response_changes_raw_and_hdr_from_raw_only(native_request, monkeypatch):
    data = native_request
    data["capture_program"] = default_capture_program(data["readout"])
    original = HDRBracketRuntime().execute(data)
    modified = deepcopy(data)
    values = np.array(modified["native_pixel_response"]["values"])
    values[..., [1, 2]] = values[..., [2, 1]]
    modified["native_pixel_response"]["values"] = values.tolist()
    switched = HDRBracketRuntime().execute(modified)
    cfa = original["arrays"]["cfa_channel_index"]
    green = (cfa == 1) | (cfa == 2)
    assert np.all(original["arrays"]["raw_frames"][:, green]
                  != switched["arrays"]["raw_frames"][:, green])
    np.testing.assert_array_equal(original["arrays"]["raw_frames"][:, ~green],
                                  switched["arrays"]["raw_frames"][:, ~green])
    assert np.all(original["arrays"]["hdr_merged_rate"][green]
                  != switched["arrays"]["hdr_merged_rate"][green])
    real_capture, real_merge = electrical.capture_electrons, hdr.merge_raw_bracket
    observed = []

    def altered_oracle(*args, **kwargs):
        result = real_capture(*args, **kwargs)
        result["arrays"]["mean_total_charge_e"][:] = 1e10
        result["arrays"]["well_saturated_mask"][:] = True
        return result

    def checked_merge(raw, exposures, calibrations, **kwargs):
        assert raw.dtype.kind in "iu"
        assert all(set(cal) == hdr.CALIBRATION_FIELDS for cal in calibrations)
        observed.append(raw.copy())
        return real_merge(raw, exposures, calibrations, **kwargs)

    monkeypatch.setattr(electrical, "capture_electrons", altered_oracle)
    monkeypatch.setattr(hdr, "merge_raw_bracket", checked_merge)
    no_oracle = HDRBracketRuntime().execute(modified)
    assert len(observed) == 1
    np.testing.assert_array_equal(observed[0], no_oracle["arrays"]["raw_frames"])
    np.testing.assert_array_equal(switched["arrays"]["hdr_merged_rate"],
                                  no_oracle["arrays"]["hdr_merged_rate"])


def test_prepared_capture_retains_four_site_rates_and_native_phase(native_request):
    runtime = TemporalSceneRuntime()
    baseline = runtime.execute(native_request)
    prepared = runtime.prepare_capture(native_request)
    frame = native_request["control_sequence"]["frames"][1]
    output = prepared.capture_frame(frame, seed_index=1)
    np.testing.assert_array_equal(output["arrays"]["raw_frames"][0],
                                  baseline["arrays"]["raw_frames"][1])
    assert output["arrays"]["channel_photoelectron_rate_scenes"].shape[-1] == 4
    assert not output["arrays"]["channel_photoelectron_rate_scenes"].flags.writeable


def test_equal_green_phi_invariant_native_model_matches_legacy_actual_raw(native_request):
    data = native_request
    values = np.asarray(data["native_pixel_response"]["values"])
    values[..., 2] = values[..., 1]
    data["native_pixel_response"]["values"] = values.tolist()
    old = deepcopy(data)
    old.pop("native_pixel_response")
    old["qe"] = [[.2, .2], [.35, .35], [.8, .8]]
    old["sensor_angular_response"] = {"theta_deg": [0, 20],
                                      "relative_response": np.ones((3, 2, 2)).tolist()}
    old["lens"].pop("directional_response")
    new_output = TemporalSceneRuntime().execute(data)
    old_output = TemporalSceneRuntime().execute(old)
    np.testing.assert_array_equal(new_output["arrays"]["raw_frames"],
                                  old_output["arrays"]["raw_frames"])
    np.testing.assert_array_equal(new_output["arrays"]["rgb_frames"],
                                  old_output["arrays"]["rgb_frames"])
    np.testing.assert_allclose(new_output["arrays"]["channel_electrons"][..., [0, 1, 3]],
                               old_output["arrays"]["channel_electrons"], rtol=3e-14)


@pytest.mark.parametrize("extra", ["qe", "sensor_angular_response"])
def test_native_temporal_refuses_double_applied_sensor_response(native_request, extra):
    native_request[extra] = request_template()[extra]
    with pytest.raises(RuntimeRequestError, match="Unknown or missing"):
        TemporalSceneRuntime().execute(native_request)


def test_temporal_and_hdr_rate_bounds_count_all_four_sites(native_request, monkeypatch):
    import camera_platform.hdr_runtime as bracket
    import camera_platform.temporal as temporal

    # 80 pixels: legacy RGB rate needs 1920 bytes; native four-site rate needs
    # 2560. A 2300-byte limit must refuse before allocating/capturing either path.
    native_request["control_sequence"]["frames"] = native_request["control_sequence"]["frames"][:1]
    monkeypatch.setattr(temporal, "MAX_OUTPUT_ARRAY_BYTES", 2300)
    monkeypatch.setattr(temporal.TemporalSceneRuntime, "_optical_rates",
                        lambda *args: pytest.fail("Optics allocated before resource refusal"))
    with pytest.raises(RuntimeRequestError, match="array memory"):
        TemporalSceneRuntime().execute(native_request)
    monkeypatch.setattr(temporal, "MAX_OUTPUT_ARRAY_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(bracket, "MAX_OUTPUT_ARRAY_BYTES", 2300)
    native_request["capture_program"] = default_capture_program(native_request["readout"])
    native_request["capture_program"]["hdr_merge"] = None
    with pytest.raises(RuntimeRequestError, match="array memory"):
        HDRBracketRuntime().execute(native_request)


def test_direction_budget_covers_all_unique_scenes_before_rate_allocation(
    native_request, monkeypatch
):
    import camera_platform.temporal as temporal

    runtime = TemporalSceneRuntime()
    per_scene = runtime.estimate_resources(native_request)["native_direction_weighted_samples"]
    monkeypatch.setattr(temporal, "MAX_NATIVE_DIRECTION_WORK", per_scene)
    assert not runtime.validate_request(native_request)
    native_request["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] *= 2
    monkeypatch.setattr(temporal.TemporalSceneRuntime, "_optical_rates",
                        lambda *args: pytest.fail("Optics allocated before work refusal"))
    with pytest.raises(RuntimeRequestError, match="across unique scenes"):
        runtime.execute(native_request)
