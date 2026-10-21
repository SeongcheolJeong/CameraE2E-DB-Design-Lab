"""Source-position RI at actual temporal knots, then analytic rolling integration.

All values are synthetic. The oracle scatters PSF taps explicitly and integrates
the declared irradiance basis without calling either product temporal integrator.
"""

import hashlib
from copy import deepcopy

import numpy as np
import pytest
from test_source_position_ri import _native_case, _source_cell_oracle

from camera_platform.isp import default_profile
from camera_platform.registry import canonical
from camera_platform.temporal import TemporalSceneRuntime, request_template

MODE = "source_position_ri_v1"


def _moving_request():
    spatial, first, kernel = _native_case()
    second = np.full_like(first, 1e-5)
    second[4, 4] = [2e-5, 3e-5]
    second[2, 6] = [9e-5, 8e-5]
    clock = {"clock_id": "source-ri-rolling-clock", "unit": "ns", "reference": "relative",
             "origin": "Authored simulation zero, not a measured camera time"}
    request = {key: deepcopy(value) for key, value in spatial.items()
               if key not in {"scene", "exposure_s", "frames", "isp_profile"}}
    request.update(
        noise_enabled=False,
        scene_sequence={
            "schema": "timed_spectral_scene_v1", "clock": clock,
            "interpolation": "linear_irradiance",
            "knots": [
                {"time_ns": 0, "scene": spatial["scene"]},
                {"time_ns": 20_000_000, "scene": {
                    **deepcopy(spatial["scene"]), "spectral_irradiance": second}},
            ],
        },
        control_sequence={
            "schema": "recorded_control_sequence_v1", "clock": clock,
            "mode": {"shutter": "rolling", "line_time_ns": 1_000_000,
                     "hidden_top_rows": 0, "readout_basis": "full_sensor_rows"},
            "frames": [
                {"frame_id": "moving-0", "frame_start_ns": 0,
                 "applied": {"applied_at_ns": 0, "exposure_ns": 3_000_000,
                             "gain": 1, "isp_model_id": "authored-isp", "command_id": None}},
                {"frame_id": "moving-1", "frame_start_ns": 5_000_000,
                 "applied": {"applied_at_ns": 0, "exposure_ns": 3_000_000,
                             "gain": 1, "isp_model_id": "authored-isp", "command_id": None}},
            ],
        },
        isp_profiles={"authored-isp": default_profile()},
    )
    return request, first, second, kernel


def test_temporal_source_ri_matches_direct_psf_and_independent_row_integral():
    request, first, second, kernel = _moving_request()
    originals = first.copy(), second.copy()
    spatial = {"scene": request["scene_sequence"]["knots"][0]["scene"],
               **{key: request[key] for key in ("lens", "wavelength_nm", "readout")},
               "exposure_s": 1}
    blur0, ri0, e0 = _source_cell_oracle(spatial, first, kernel)
    spatial["scene"] = request["scene_sequence"]["knots"][1]["scene"]
    blur1, ri1, e1 = _source_cell_oracle(spatial, second, kernel)
    np.testing.assert_array_equal(ri0, ri1)
    output = TemporalSceneRuntime().execute(request)
    arrays, summary = output["arrays"], output["summary"]
    np.testing.assert_array_equal(first, originals[0])
    np.testing.assert_array_equal(second, originals[1])
    np.testing.assert_allclose(arrays["source_relative_illumination_map"], ri0,
                               rtol=0, atol=1e-15)
    for i, source in enumerate((first, second)):
        ledger = summary["flux_diagnostics_per_scene"][i]
        area = (4e-6) ** 2
        np.testing.assert_allclose(ledger["pre_ri_source_spectral_flux"],
                                   source.sum(axis=(0, 1)) * area, rtol=1e-15)
        np.testing.assert_allclose(ledger["post_ri_source_spectral_flux"],
                                   (source * ri0).sum(axis=(0, 1)) * area, rtol=1e-15)
        np.testing.assert_allclose(ledger["input_spectral_flux"],
                                   ledger["post_ri_source_spectral_flux"], rtol=1e-15)
    assert not np.array_equal(blur0, blur1)
    for frame, frame_start_ns in enumerate((0, 5_000_000)):
        for row in range(8):
            left_ns = frame_start_ns + row * 1_000_000
            right_ns = left_ns + 3_000_000
            # Integral of two linear basis functions over the actual row window.
            fraction = (left_ns + right_ns) / (2 * 20_000_000)
            independent = (1 - fraction) * e0[row] * 0.003 + fraction * e1[row] * 0.003
            np.testing.assert_allclose(arrays["mean_electrons"][frame, row], independent,
                                       rtol=4e-14, atol=1e-10)
            assert arrays["row_start_ns"][frame, row] == left_ns
            assert arrays["row_end_ns"][frame, row] == right_ns
    readout = request["readout"]
    white = 2**readout["bit_depth"] - 1
    black = readout["black_level_dn"]
    expected_raw = np.rint(black + arrays["mean_electrons"]
                           * (white - black) / readout["full_well_e"])
    np.testing.assert_array_equal(arrays["raw_frames"], expected_raw.astype(np.int32))
    assert summary["field_radiometry_mode"] == MODE
    assert summary["relative_illumination_application"][
        "receiver_relative_illumination_map"] == "diagnostic_only_not_multiplied"
    assert summary["prediction"]["method_and_version"].endswith("source_ri_v1")
    assert arrays["rgb_frames"].shape == (2, 8, 8, 3)
    legacy = deepcopy(request)
    legacy.pop("field_radiometry_mode")
    old = TemporalSceneRuntime().execute(legacy)
    assert not np.allclose(old["arrays"]["mean_electrons"],
                           arrays["mean_electrons"], rtol=1e-3)


def test_temporal_legacy_exact_hash_and_other_topologies_refuse_mode():
    legacy = TemporalSceneRuntime().execute(request_template())
    for name, expected in {
        "channel_electrons": "3ed599f26f0f79c8390c17827be156da43bb55908471a713ab51b668e414e984",
        "raw_frames": "83980ab24ca0c7faea502d3bdc887368bb671ca6c0a74d1774fe1ec3785c2b9f",
        "rgb_frames": "eb9ff3385944d5e056cc358e18b4752c2c4e3fbf99bdb3f266305974cba31777",
    }.items():
        assert hashlib.sha256(legacy["arrays"][name].tobytes()).hexdigest() == expected
    assert hashlib.sha256(canonical(legacy["summary"]).encode()).hexdigest() == (
        "11f30b86bfd3a55ebdee5f48c536c00621bd5197ef59acfb84a134de6b8ccc73")
    from camera_platform.temporal import _parse

    request, _, _, _ = _moving_request()
    with pytest.raises(ValueError, match="Unknown or missing temporal fields"):
        _parse(request)  # HDR/DCG/retained runtimes call this restricted parser.


def test_temporal_subcell_hashes_numeric_source_not_only_parent_cell_mean():
    request, first, _, _ = _moving_request()
    source_a = np.repeat(np.repeat(first[:, :, None, None, :], 2, axis=2), 2, axis=3)
    source_b = source_a.copy()
    source_b[4, 4, 0, 0, :] += 2e-5
    source_b[4, 4, 0, 1, :] -= 2e-5
    assert np.array_equal(source_a.mean(axis=(2, 3)), source_b.mean(axis=(2, 3)))
    for knot, source in zip(request["scene_sequence"]["knots"],
                            (source_a, source_b), strict=True):
        knot["scene"]["spectral_irradiance"] = first.copy()
        knot["scene"]["source_subcell_irradiance"] = source
        knot["scene"]["source_subcell_evidence"] = {
            "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
            "radiance_sha256": "a" * 64, "source_render_id": "authored-subcell-test",
            "projection_job_id": None,
        }
    runtime = TemporalSceneRuntime()
    estimate = runtime.estimate_resources(request)
    assert estimate["unique_scenes"] == 2
    assert estimate["source_position_ri_map_bytes"] == source_a.nbytes
    output = runtime.execute(request)
    assert output["arrays"]["source_relative_illumination_map"].shape == source_a.shape
    assert not np.array_equal(output["arrays"]["channel_photoelectron_rate_scenes"][0],
                              output["arrays"]["channel_photoelectron_rate_scenes"][1])
    mixed = deepcopy(request)
    del mixed["scene_sequence"]["knots"][1]["scene"]["source_subcell_irradiance"]
    del mixed["scene_sequence"]["knots"][1]["scene"]["source_subcell_evidence"]
    assert "one source cell/subcell grid" in runtime.validate_request(mixed)[0]["message"]


def test_legacy_json_list_subcell_timeline_keeps_original_summary_and_raw_hashes():
    request, _, _, _ = _moving_request()
    request.pop("field_radiometry_mode")
    for knot in request["scene_sequence"]["knots"]:
        scene = knot["scene"]
        image = np.asarray(scene["spectral_irradiance"])
        source = np.repeat(np.repeat(image[:, :, None, None, :], 2, axis=2), 2, axis=3)
        scene["source_subcell_irradiance"] = source.tolist()
        scene["source_subcell_evidence"] = {
            "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
            "radiance_sha256": "a" * 64, "source_render_id": "legacy-json-list-test",
            "projection_job_id": None,
        }
    output = TemporalSceneRuntime().execute(request)
    assert [item["scene_content_hash"] for item in output["summary"]["scene_knots"]] == [
        "a4a2fd3d57a4b0e4a94255e58c30a9c2604ecbd6bedbff55cdceefad6c52b547",
        "1b2b32a8c62721f9c77e97593710e2d48aa2cbb57ece8c1f3861c1df83b37e90",
    ]
    assert hashlib.sha256(canonical(output["summary"]).encode()).hexdigest() == (
        "33e318df424e4612e77badf6fda92cf2507496926664f3c09862f006658e41cf")
    assert hashlib.sha256(output["arrays"]["raw_frames"].tobytes()).hexdigest() == (
        "1dd008f020c2a87f5916df198e57fe8415e8969bf493fefc1f87a98396094921")
