"""Direct HDR/LFM source RI arithmetic with synthetic, unmeasured optics."""

from copy import deepcopy

import numpy as np
import pytest
from test_source_position_ri import _source_cell_oracle
from test_temporal_source_position_ri import _moving_request

from camera_platform import hdr_runtime
from camera_platform.hdr_runtime import HDRBracketRuntime, default_capture_program
from camera_platform.isp import process_raw


def _capture_request(*, dynamic: bool, two_components: bool):
    request, first, second, kernel = _moving_request()
    if not dynamic:
        request["scene_sequence"]["knots"][1]["scene"] = deepcopy(
            request["scene_sequence"]["knots"][0]["scene"])
    request["capture_program"] = default_capture_program(request["readout"])
    request["capture_program"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm" if not dynamic else "constant",
        "clock_id": request["control_sequence"]["clock"]["clock_id"],
        **({"period_ns": 1_000_000, "on_time_ns": 500_000,
            "phase_origin_ns": 0, "low": 0, "high": 1}
           if not dynamic else {"level": 0.4}),
    }
    if dynamic:
        request["capture_program"]["hdr_merge"] = None
    ambient = None
    if two_components:
        request["unmodulated_scene_sequence"] = deepcopy(request["scene_sequence"])
        ambient = first.copy() * 0.3
        ambient[3, 5] += [5e-5, 7e-5]
        for knot in request["unmodulated_scene_sequence"]["knots"]:
            knot["scene"]["spectral_irradiance"] = ambient.copy()
    return request, first, second, ambient, kernel


def _oracle_rate(request, scene, image, kernel):
    spatial = {
        "scene": scene,
        **{key: request[key] for key in ("lens", "wavelength_nm", "readout")},
        "exposure_s": 1,
    }
    return _source_cell_oracle(spatial, image, kernel)[2]


def test_direct_hdr_source_ri_dual_pwm_matches_independent_psf_charge_raw_and_isp():
    request, first, _, ambient, kernel = _capture_request(
        dynamic=False, two_components=True)
    runtime = HDRBracketRuntime()
    estimate = runtime.estimate_resources(request)
    assert estimate["source_position_ri"]["component_map_bytes"] == {
        "modulated": first.nbytes,
        "unmodulated": first.nbytes,
    }
    result = runtime.execute(request)
    arrays, summary = result["arrays"], result["summary"]
    modulated_rate = _oracle_rate(
        request, request["scene_sequence"]["knots"][0]["scene"], first, kernel)
    ambient_rate = _oracle_rate(
        request, request["unmodulated_scene_sequence"]["knots"][0]["scene"],
        ambient, kernel)
    white, black = 2**request["readout"]["bit_depth"] - 1, request["readout"]["black_level_dn"]
    for index, frame in enumerate(request["control_sequence"]["frames"]):
        seconds = frame["applied"]["exposure_ns"] * 1e-9
        np.testing.assert_allclose(arrays["modulated_channel_electrons"][index],
                                   arrays["channel_photoelectron_rate_scenes"][0]
                                   * seconds * 0.5, rtol=4e-14)
        expected = (modulated_rate * seconds * 0.5 + ambient_rate * seconds)
        np.testing.assert_allclose(arrays["mean_electrons"][index], expected,
                                   rtol=4e-14, atol=1e-10)
        expected_raw = np.rint(black + expected * (white - black)
                               / request["readout"]["full_well_e"]).astype(np.int32)
        np.testing.assert_array_equal(arrays["raw_frames"][index], expected_raw)
        processed = process_raw(
            expected_raw,
            {"cfa": "RGGB", "bit_depth": request["readout"]["bit_depth"],
             "black_level_dn": black, "response_type": "linear", "packing": "unpacked",
             "layout": "bayer", "hdr_streams": 1},
            request["isp_profiles"][frame["applied"]["isp_model_id"]],
            geometry=summary["geometry"],
        )
        np.testing.assert_array_equal(arrays["rgb_frames"][index], processed["arrays"]["rgb"])
    np.testing.assert_array_equal(arrays["source_relative_illumination_map"],
                                  arrays["unmodulated_source_relative_illumination_map"])
    assert summary["field_radiometry_mode"] == "source_position_ri_v1"
    assert summary["relative_illumination_application"][
        "receiver_relative_illumination_map"] == "diagnostic_only_not_multiplied"
    assert summary["hdr_merge_applied"]
    assert summary["physical_validation"] == "not_established"
    assert set(arrays) == set(summary["array_units"])
    legacy = deepcopy(request)
    legacy.pop("field_radiometry_mode")
    old = runtime.execute(legacy)
    assert not np.allclose(old["arrays"]["mean_electrons"], arrays["mean_electrons"], rtol=1e-3)
    assert "source_relative_illumination_map" not in old["arrays"]


def test_direct_lfm_source_ri_integrates_two_independent_light_timelines():
    request, first, second, ambient, kernel = _capture_request(
        dynamic=True, two_components=True)
    result = HDRBracketRuntime().execute(request)
    arrays, summary = result["arrays"], result["summary"]
    scene = request["scene_sequence"]["knots"][0]["scene"]
    rate0 = _oracle_rate(request, scene, first, kernel)
    rate1 = _oracle_rate(request, request["scene_sequence"]["knots"][1]["scene"],
                         second, kernel)
    ambient_rate = _oracle_rate(
        request, request["unmodulated_scene_sequence"]["knots"][0]["scene"],
        ambient, kernel)
    for frame_index, frame_start in enumerate((0, 5_000_000)):
        for row in range(8):
            start, end = frame_start + row * 1_000_000, frame_start + row * 1_000_000 + 3_000_000
            fraction = (start + end) / (2 * 20_000_000)
            expected = (0.003 * 0.4 * ((1 - fraction) * rate0[row] + fraction * rate1[row])
                        + 0.003 * ambient_rate[row])
            np.testing.assert_allclose(arrays["mean_electrons"][frame_index, row],
                                       expected, rtol=4e-14, atol=1e-10)
    assert summary["hdr_merge_applied"] is False
    assert summary["source_position_ri_components"]["maps_equal"] is True


def test_source_ri_hdr_static_gate_catches_subcell_motion_with_same_parent_image():
    request, first, _, _, _ = _capture_request(dynamic=False, two_components=False)
    subcells = np.repeat(np.repeat(first[:, :, None, None, :], 2, axis=2), 2, axis=3)
    shifted = subcells.copy()
    shifted[4, 4, 0, 0] += 2e-5
    shifted[4, 4, 0, 1] -= 2e-5
    for index, knot in enumerate(request["scene_sequence"]["knots"]):
        knot["scene"]["source_subcell_irradiance"] = subcells.copy()
        knot["scene"]["source_subcell_evidence"] = {
            "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
            "radiance_sha256": "a" * 64, "source_render_id": f"authored-static-gate-{index}",
            "projection_job_id": None,
        }
    # Source descriptors can differ when the numeric optical scene is identical.
    assert HDRBracketRuntime().validate_request(request) == []
    request["scene_sequence"]["knots"][1]["scene"]["source_subcell_irradiance"] = shifted
    assert np.array_equal(subcells.mean(axis=(2, 3)), shifted.mean(axis=(2, 3)))
    issues = HDRBracketRuntime().validate_request(request)
    assert any("static" in issue["message"] for issue in issues)


def test_source_ri_hdr_dual_grid_alignment_and_combined_memory_fail_closed(monkeypatch):
    request, first, _, _, _ = _capture_request(dynamic=False, two_components=True)
    runtime = HDRBracketRuntime()
    single = deepcopy(request)
    single.pop("unmodulated_scene_sequence")
    single_bytes = runtime.estimate_resources(single)["working_array_bytes_estimate"]
    assert runtime.estimate_resources(request)["working_array_bytes_estimate"] > single_bytes
    monkeypatch.setattr(hdr_runtime, "MAX_WORKING_ARRAY_BYTES", single_bytes + 1)
    assert any("Combined illumination components exceed working-array budget" in issue["message"]
               for issue in runtime.validate_request(request))
    monkeypatch.undo()
    subcells = np.repeat(np.repeat(first[:, :, None, None, :], 2, axis=2), 2, axis=3)
    for knot in request["scene_sequence"]["knots"]:
        knot["scene"]["source_subcell_irradiance"] = subcells.copy()
        knot["scene"]["source_subcell_evidence"] = {
            "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
            "radiance_sha256": "a" * 64, "source_render_id": "authored-grid-mismatch",
            "projection_job_id": None,
        }
    assert any("one exact source cell/subcell grid" in issue["message"]
               for issue in runtime.validate_request(request))


def test_source_ri_hdr_refuses_different_computed_component_maps_before_readout(monkeypatch):
    request, _, _, _, _ = _capture_request(dynamic=False, two_components=True)
    original = hdr_runtime.TemporalSceneRuntime._optical_rates
    calls = 0

    def different_map(parsed):
        nonlocal calls
        result = original(parsed)
        calls += 1
        if calls == 2:
            parsed["source_ri_map"] = parsed["source_ri_map"].copy()
            parsed["source_ri_map"][0, 0, 0] += 0.01
        return result

    monkeypatch.setattr(hdr_runtime.TemporalSceneRuntime, "_optical_rates", different_map)
    monkeypatch.setattr(
        hdr_runtime.electrical, "capture_electrons",
        lambda *args, **kwargs: pytest.fail("readout must not run after optical map mismatch"),
    )
    with pytest.raises(hdr_runtime.RuntimeRequestError, match="fixed source map"):
        HDRBracketRuntime().execute(request)
