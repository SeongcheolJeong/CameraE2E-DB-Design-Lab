"""Two-component PWM/trap/HCG-LCG uses source RI at each optical knot.

Scalar PSF scatter and field RI evaluation are deliberately independent of the
product spatial/temporal optical helpers. Inputs are authored, not measured.
"""

import hashlib
from copy import deepcopy

import numpy as np
from test_native_temporal import EQE
from test_native_temporal import native_request as native_request
from test_retained_trap_two_component import _trapped_request

from camera_platform.registry import canonical
from camera_platform.retained_charge_runtime import (
    RetainedChargeRuntime,
    default_gain_merge_capture_program,
    default_trapped_capture_program,
)

MODE = "source_position_ri_v1"
PLANCK = 6.62607015e-34
LIGHT = 299792458.0


def _case(native_request):
    request = _trapped_request(native_request, paired=True)
    request["field_radiometry_mode"] = MODE
    lens = request["lens"]
    lens["field_reference_radius_mm"] = 0.03
    lens["relative_illumination"] = [[1., 0.5], [1., 0.7]]
    lens["directional_response"]["field_reference_radius_mm"] = 0.03
    lens["directional_response"]["image_height_mm"] = [0, 0.03]
    psf = np.array([
        [[0., .1, 0.], [.1, .6, .1], [0., .1, 0.]],
        [[.05, .05, .05], [.05, .6, .05], [.05, .05, .05]],
    ])
    request["psf"]["values"] = psf.tolist()
    for index, knot in enumerate(request["scene_sequence"]["knots"]):
        image = knot["scene"]["spectral_irradiance"]
        image[3, 4] = ([6e-4, 4e-4] if index == 0 else [2e-4, 7e-4])
        image[5, 7] = ([3e-4, 8e-4] if index == 0 else [7e-4, 2e-4])
    for index, knot in enumerate(request["unmodulated_scene_sequence"]["knots"]):
        image = knot["scene"]["spectral_irradiance"]
        image[2, 3] = ([7e-4, 3e-4] if index == 0 else [2e-4, 8e-4])
    return request, psf


def _direct_rates(image, request, kernels):
    """Native cell RI, explicit PSF scatter, photon integral and all four sites."""
    rows, cols, bands = image.shape
    sampling = request["scene_sequence"]["knots"][0]["scene"]["sampling"]
    full, origin = sampling["full_sensor_shape"], sampling["crop_origin"]
    reference = request["lens"]["field_reference_radius_mm"]
    ri = np.zeros_like(image)
    blurred = np.zeros_like(image)
    for sy in range(rows):
        for sx in range(cols):
            y = (origin[0] + sy - (full[0] - 1) / 2) * .004
            x = (origin[1] + sx - (full[1] - 1) / 2) * .004
            radius = np.hypot(y, x) / reference
            for band in range(bands):
                edge = request["lens"]["relative_illumination"][band][1]
                ri[sy, sx, band] = 1 + (edge - 1) * radius
                for ky in range(3):
                    for kx in range(3):
                        dy, dx = sy + ky - 1, sx + kx - 1
                        if 0 <= dy < rows and 0 <= dx < cols:
                            blurred[dy, dx, band] += (
                                image[sy, sx, band] * ri[sy, sx, band]
                                * kernels[band, ky, kx])
    waves = request["wavelength_nm"]
    factors = np.asarray(waves) * 1e-9 / (PLANCK * LIGHT)
    photon = (blurred[..., 0] * factors[0] + blurred[..., 1] * factors[1]) * (
        waves[1] - waves[0]) / 2
    rates = photon[..., None] * (.004e-3) ** 2 * EQE
    return rates, ri


def test_source_ri_precedes_psf_for_both_component_knots_and_reaches_traps(native_request):
    request, kernels = _case(native_request)
    before = [[knot["scene"]["spectral_irradiance"].copy()
               for knot in request[key]["knots"]]
              for key in ("scene_sequence", "unmodulated_scene_sequence")]
    runtime = RetainedChargeRuntime()
    resources = runtime.estimate_resources(request)
    assert set(resources["source_position_ri"]["component_map_bytes"]) == (
        {"modulated", "unmodulated"})
    output = runtime.execute(request)
    arrays, summary = output["arrays"], output["summary"]
    for index, (key, rate_name, map_name, ledger_name) in enumerate((
        ("scene_sequence", "channel_photoelectron_rate_scenes",
         "source_relative_illumination_map", "flux_diagnostics_per_scene"),
        ("unmodulated_scene_sequence", "unmodulated_channel_photoelectron_rate_scenes",
         "unmodulated_source_relative_illumination_map",
         "unmodulated_flux_diagnostics_per_scene"),
    )):
        for knot_index, knot in enumerate(request[key]["knots"]):
            image = knot["scene"]["spectral_irradiance"]
            expected, ri = _direct_rates(image, request, kernels)
            np.testing.assert_allclose(arrays[rate_name][knot_index], expected,
                                       rtol=4e-14, atol=1e-8)
            np.testing.assert_allclose(arrays[map_name], ri, rtol=0, atol=1e-15)
            ledger = summary[ledger_name][knot_index]
            area = (4e-6)**2
            np.testing.assert_allclose(ledger["pre_ri_source_spectral_flux"],
                                       image.sum(axis=(0, 1)) * area)
            np.testing.assert_allclose(ledger["post_ri_source_spectral_flux"],
                                       (image * ri).sum(axis=(0, 1)) * area)
            np.testing.assert_allclose(ledger["input_spectral_flux"],
                                       ledger["post_ri_source_spectral_flux"])
            np.testing.assert_array_equal(image, before[index][knot_index])
    assert summary["field_radiometry_mode"] == MODE
    assert summary["source_position_ri_components"]["modulated"]["map_unit"] == "1"
    assert summary["source_position_ri_components"]["unmodulated"]["flux_unit"] == "W/nm"
    assert summary["prediction"]["method_and_version"].endswith("source_ri_v1")
    assert summary["physical_validation"] == "not_established"
    assert arrays["raw_groups"].shape == (2, 2, 8, 10)
    assert arrays["retained_trapped_e"].shape == (2, 8, 10)
    assert np.any(arrays["retained_trapped_e"] > 0)
    assert resources["output_array_bytes_estimate"] >= sum(a.nbytes for a in arrays.values())
    old_request = deepcopy(request)
    old_request.pop("field_radiometry_mode")
    old = runtime.execute(old_request)
    assert not np.allclose(old["arrays"]["channel_electrons"],
                           arrays["channel_electrons"], rtol=1e-3)


def test_source_ri_retained_subcell_evidence_and_unsupported_topologies(native_request):
    request, _ = _case(native_request)
    for component in ("scene_sequence", "unmodulated_scene_sequence"):
        for knot in request[component]["knots"]:
            scene = knot["scene"]
            image = scene["spectral_irradiance"]
            cells = np.repeat(np.repeat(image[:, :, None, None, :], 2, axis=2), 2, axis=3)
            cells[3, 4, 0, 0] *= 1.2
            cells[3, 4, 0, 1] *= .8
            assert np.allclose(cells.mean(axis=(2, 3)), image)
            scene["source_subcell_irradiance"] = cells
            scene["source_subcell_evidence"] = {
                "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
                "radiance_sha256": "a" * 64, "source_render_id": "authored-subcell-test",
                "projection_job_id": None,
            }
    output = RetainedChargeRuntime().execute(request)
    summary = output["summary"]
    for component, map_name in (("modulated", "source_relative_illumination_map"),
                                ("unmodulated", "unmodulated_source_relative_illumination_map")):
        assert output["arrays"][map_name].shape == (8, 10, 2, 2, 2)
        assert summary["source_position_ri_components"][component]["map_axes"] == [
            "source_row", "source_col", "subrow", "subcol", "wavelength"]
        assert len(summary["source_position_ri_components"][component][
            "source_subcell_evidence_per_scene"]) == 2
    for change in ("single_component", "constant_waveform", "untrapped", "single_readout"):
        invalid = deepcopy(request)
        if change == "single_component":
            invalid.pop("unmodulated_scene_sequence")
        elif change == "constant_waveform":
            invalid["capture_program"]["waveform"] = {
                "schema": "camera_light_waveform_v1", "kind": "constant",
                "clock_id": request["control_sequence"]["clock"]["clock_id"], "level": 1}
        elif change == "untrapped":
            frames = [frame["frame_id"] for frame in invalid["control_sequence"]["frames"]]
            program = default_gain_merge_capture_program(invalid["readout"], frames)
            program["waveform"] = deepcopy(request["capture_program"]["waveform"])
            invalid["capture_program"] = program
        else:
            frames = [frame["frame_id"] for frame in invalid["control_sequence"]["frames"]]
            program = default_trapped_capture_program(invalid["readout"], frames)
            program["waveform"] = deepcopy(request["capture_program"]["waveform"])
            invalid["capture_program"] = program
        issues = RetainedChargeRuntime().validate_request(invalid)
        assert issues and "Retained source-position RI requires" in issues[0]["message"]


def test_retained_absent_mode_trapped_paired_bytes_remain_exact(native_request):
    output = RetainedChargeRuntime().execute(_trapped_request(native_request, paired=True))
    for name, expected in {
        "channel_electrons": "38070f794deb2d8cdee5b3ec9d8a59c25ce66789781d72b2d4b6091cdf7e33a4",
        "retained_trapped_e": "b308a269658cf6a6cb338c23ce9f126f6def47d9b5088d2f22caa7397f055363",
        "raw_groups": "9e3f04ef1c31883b313dffc7b4f87c430e246d76783730f59879bbdff4b31751",
        "rgb_groups": "d478db7a8cd44cf9bb152a6ecb0f26401c22aa23bb665d0580cac9799d0d265c",
    }.items():
        assert hashlib.sha256(output["arrays"][name].tobytes()).hexdigest() == expected
    assert hashlib.sha256(canonical(output["summary"]).encode()).hexdigest() == (
        "f6e4690a99135243c1d0d79bd5d4b61064e6e9f12bec20ee0efca933b883d52e")
