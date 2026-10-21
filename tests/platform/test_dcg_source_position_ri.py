"""Static source-position RI reaches shared charge and both original DCG RAW paths.

The scalar source/PSF oracle is independent of production optical helpers. All
scene and response coefficients are authored; no physical accuracy is claimed.
"""

import hashlib
from copy import deepcopy

import numpy as np
from test_dcg_runtime import expected_integer_raw
from test_source_position_ri import _source_cell_oracle
from test_source_ri_collection_chain import _case as _collection_case
from test_source_ri_collection_chain import _scalar_ledger
from test_temporal_source_position_ri import _moving_request

from camera_platform import nonlinear_examples
from camera_platform.dcg_runtime import SharedChargeDCGRuntime, default_capture_program
from camera_platform.registry import canonical

MODE = "source_position_ri_v1"


def _case():
    request, first, _, kernel = _moving_request()
    # DCG integrates a static optical image. A second declared knot records
    # the same scene so the rolling windows remain fully specified.
    request["scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] = first.copy()
    request["control_sequence"]["frames"][1]["applied"]["exposure_ns"] = 5_000_000
    request["control_sequence"]["frames"][1]["applied"]["applied_at_ns"] = 5_000_000
    request["capture_program"] = default_capture_program(request["readout"])
    request["capture_program"]["merge_profile"] = None
    return request, first, kernel


def test_static_source_ri_reaches_one_shared_charge_and_two_integer_raw_paths():
    request, image, kernel = _case()
    before = image.copy()
    spatial = {"scene": request["scene_sequence"]["knots"][0]["scene"],
               **{key: request[key] for key in ("lens", "wavelength_nm", "readout")},
               "exposure_s": 1}
    _, source_ri, electron_rate = _source_cell_oracle(spatial, image, kernel)
    model = request["capture_program"]["electrical_model"]
    runtime = SharedChargeDCGRuntime()
    resources = runtime.estimate_resources(request)
    result = runtime.execute(request)
    arrays, summary = result["arrays"], result["summary"]
    np.testing.assert_array_equal(image, before)
    np.testing.assert_allclose(arrays["source_relative_illumination_map"],
                               source_ri, rtol=0, atol=1e-15)
    assert resources["source_position_ri_map_bytes"] == source_ri.nbytes
    assert resources["source_position_ri_unique_scenes"] == 1
    assert resources["working_array_bytes_estimate"] >= sum(a.nbytes for a in arrays.values())
    for acquisition, exposure in enumerate((.003, .005)):
        expected_charge = electron_rate * exposure
        np.testing.assert_allclose(arrays["mean_electrons"][acquisition], expected_charge,
                                   rtol=5e-14, atol=1e-10)
        np.testing.assert_allclose(arrays["electrical_shared_charge_e"][acquisition],
                                   expected_charge, rtol=5e-14, atol=1e-10)
        for path_index, path in enumerate(model["paths"]):
            expected_raw = expected_integer_raw(expected_charge, exposure, model, path)
            np.testing.assert_array_equal(
                arrays["raw_groups"][acquisition, path_index], expected_raw)
        assert arrays["row_start_ns"][acquisition, 1] - arrays[
            "row_start_ns"][acquisition, 0] == 1_000_000
    assert arrays["raw_groups"].shape == (2, 2, 8, 8)
    assert summary["field_radiometry_mode"] == MODE
    assert summary["relative_illumination_application"][
        "receiver_relative_illumination_map"] == "diagnostic_only_not_multiplied"
    assert summary["prediction"]["method_and_version"].endswith("source_ri_v1")
    assert summary["physical_validation"] == "not_established"
    assert set(arrays) == set(summary["array_units"])
    legacy = deepcopy(request)
    del legacy["field_radiometry_mode"]
    old = runtime.execute(legacy)
    assert "source_relative_illumination_map" not in old["arrays"]
    assert "field_radiometry_mode" not in old["summary"]
    assert not np.allclose(old["arrays"]["mean_electrons"],
                           arrays["mean_electrons"], rtol=1e-3)
    assert hashlib.sha256(old["arrays"]["raw_groups"].tobytes()).hexdigest() == (
        "55a6f6d3fb413b9ef8487bccc9ca74abe79be0812f26c2bbe63cc4392c8a1da0"
    )
    assert hashlib.sha256(canonical(old["summary"]).encode()).hexdigest() == (
        "913aaebc644d90224ad29bc512ab5d0817d591d77cce60d88cf266ee595429ea"
    )


def test_dcg_rejects_a_changed_subcell_distribution_with_same_parent_image():
    request, image, _ = _case()
    initial = np.repeat(np.repeat(image[:, :, None, None, :], 2, axis=2), 2, axis=3)
    changed = initial.copy()
    changed[4, 4, 0, 0] += 1e-5
    changed[4, 4, 0, 1] -= 1e-5
    np.testing.assert_allclose(changed.mean(axis=(2, 3)), image, rtol=0, atol=1e-20)
    for knot, subcells in zip(request["scene_sequence"]["knots"],
                              (initial, changed), strict=True):
        knot["scene"]["source_subcell_irradiance"] = subcells
        knot["scene"]["source_subcell_evidence"] = {
            "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
            "radiance_sha256": "a" * 64, "source_render_id": "authored-dcg-subcells",
            "projection_job_id": None,
        }
    same = deepcopy(request)
    same["scene_sequence"]["knots"][1]["scene"]["source_subcell_irradiance"] = (
        initial.copy()
    )
    output = SharedChargeDCGRuntime().execute(same)
    assert output["arrays"]["source_relative_illumination_map"].shape == initial.shape
    issues = SharedChargeDCGRuntime().validate_request(request)
    assert len(issues) == 1
    assert "one static unique optical image" in issues[0]["message"]


def test_source_ri_reaches_nonlinear_shared_path_without_relabeling_linear_metric():
    request, _, _ = _case()
    request["capture_program"] = nonlinear_examples.capture_program(
        request["readout"], "shared_charge_nonlinear"
    )
    request["capture_program"]["merge_profile"] = None
    result = SharedChargeDCGRuntime().execute(request)
    assert result["arrays"]["raw_groups"].shape == (2, 2, 8, 8)
    assert result["summary"]["prediction"]["method_and_version"] == (
        "sampled_nonlinear_shared_charge_raw_black_relative_v1_source_ri_v1"
    )
    assert "sampled_raw_dark_corrected_channel_mean_dn" not in result["summary"]
    assert result["summary"]["physical_validation"] == "not_established"


def test_generation_collection_source_ri_reaches_dcg_and_keeps_prnu_gate():
    spatial, image, kernel = _collection_case()
    request, _, _ = _case()
    for key in ("wavelength_nm", "pixel_pitch_um", "readout", "lens", "psf",
                "native_pixel_response", "charge_collection"):
        request[key] = deepcopy(spatial[key])
    for knot in request["scene_sequence"]["knots"]:
        knot["scene"] = deepcopy(spatial["scene"])
    request["scene_sequence"]["knots"][-1]["time_ns"] = 40_000_000
    request["capture_program"] = default_capture_program(request["readout"])
    request["capture_program"]["merge_profile"] = None
    _, generated_rate, routed_rate, loss_rate = _scalar_ledger(spatial, image, kernel)
    result = SharedChargeDCGRuntime().execute(request)
    arrays = result["arrays"]
    np.testing.assert_allclose(arrays["collection_generated_rate_scenes"][0],
                               generated_rate, rtol=5e-14, atol=1e-8)
    np.testing.assert_allclose(arrays["collection_intrinsic_loss_rate_scenes"][0],
                               loss_rate, rtol=5e-14, atol=1e-8)
    model = request["capture_program"]["electrical_model"]
    for acquisition, exposure in enumerate((.003, .005)):
        expected_charge = routed_rate * exposure
        np.testing.assert_allclose(arrays["mean_electrons"][acquisition], expected_charge,
                                   rtol=5e-14, atol=1e-8)
        for path_index, path in enumerate(model["paths"]):
            np.testing.assert_array_equal(
                arrays["raw_groups"][acquisition, path_index],
                expected_integer_raw(expected_charge, exposure, model, path),
            )
    request["capture_program"]["electrical_model"]["fixed_pattern"]["prnu_sigma"] = .02
    issues = SharedChargeDCGRuntime().validate_request(request)
    assert len(issues) == 1
    assert "Charge collection requires PRNU=0" in issues[0]["message"]
