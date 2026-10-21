"""Response-chain evidence must identify the actual optical operator and source."""

import hashlib
from copy import deepcopy

import numpy as np
from test_source_position_ri import _native_case

from camera_platform import electrical, spatial
from camera_platform.response_chain import evaluate_response


def _rate_request(request):
    request = deepcopy(request)
    exposure = request.pop("exposure_s")
    request.pop("frames")
    return request, exposure


def test_source_position_ri_changes_charge_and_has_distinct_identity_and_order():
    request, _, _ = _native_case()
    request, exposure = _rate_request(request)
    model = electrical.default_electrical_model(request["readout"])
    selected = evaluate_response(
        request, model, exposure_s=exposure, analog_gain=1, noise_enabled=False
    )
    legacy_request = deepcopy(request)
    legacy_request.pop("field_radiometry_mode")
    legacy = evaluate_response(
        legacy_request, model, exposure_s=exposure, analog_gain=1, noise_enabled=False
    )
    assert selected["summary"]["response_identity"]["field_radiometry_mode"] == (
        spatial.SOURCE_RI_MODE
    )
    assert "field_radiometry_mode" not in legacy["summary"]["response_identity"]
    assert selected["summary"]["processing_order"][:3] == [
        "supplied_ideal_irradiance_with_throughput",
        "source_relative_illumination",
        "supplied_pixel_integrated_psf_once",
    ]
    assert legacy["summary"]["processing_order"][:3] == [
        "supplied_ideal_irradiance_with_throughput",
        "supplied_pixel_integrated_psf_once",
        "relative_illumination",
    ]
    assert not np.array_equal(
        selected["arrays"]["mean_photocharge_e"],
        legacy["arrays"]["mean_photocharge_e"],
    )
    assert not np.array_equal(selected["arrays"]["raw_dn"], legacy["arrays"]["raw_dn"])


def test_retained_subcell_source_bytes_are_bound_even_when_native_mean_is_same():
    request = spatial.request_template()
    request, exposure = _rate_request(request)
    source = np.full((8, 8, 2, 2, 5), 1e-5)
    source[4, 4, 0, 0] = 2e-5
    source[4, 4, 1, 1] = 0
    request["scene"]["spectral_irradiance"] = source.mean(axis=(2, 3))
    request["scene"]["source_subcell_irradiance"] = source
    request["scene"]["source_subcell_evidence"] = {
        "model": "bilinear_receiver_integrated_impulse_v1",
        "grid_side": 2,
        "radiance_sha256": "a" * 64,
        "source_render_id": "fixture-render",
        "projection_job_id": None,
    }
    model = electrical.default_electrical_model(request["readout"])
    first = evaluate_response(
        request, model, exposure_s=exposure, analog_gain=1, noise_enabled=False
    )
    altered = deepcopy(request)
    altered_source = altered["scene"]["source_subcell_irradiance"]
    altered_source[4, 4, 0, 0] = 0
    altered_source[4, 4, 1, 1] = 2e-5
    assert np.array_equal(
        request["scene"]["spectral_irradiance"],
        altered["scene"]["spectral_irradiance"],
    )
    second = evaluate_response(
        altered, model, exposure_s=exposure, analog_gain=1, noise_enabled=False
    )
    a = first["summary"]["response_identity"]
    b = second["summary"]["response_identity"]
    assert a["scene_array_sha256"] == b["scene_array_sha256"]
    assert a["source_subcell_array_sha256"] == hashlib.sha256(
        source.astype("<f8").tobytes(order="C")
    ).hexdigest()
    assert a["source_subcell_array_sha256"] != b["source_subcell_array_sha256"]
    assert a["source_subcell_shape"] == [8, 8, 2, 2, 5]
    assert not np.array_equal(
        first["arrays"]["blurred_spectral_irradiance"],
        second["arrays"]["blurred_spectral_irradiance"],
    )


def test_native_crop_location_changes_identity_with_identical_scene_bytes():
    request, _, _ = _native_case()
    request, exposure = _rate_request(request)
    model = electrical.default_electrical_model(request["readout"])
    center = evaluate_response(
        request, model, exposure_s=exposure, analog_gain=1, noise_enabled=False
    )
    shifted = deepcopy(request)
    shifted["scene"]["sampling"]["crop_origin"] = [1, 0]
    shifted_output = evaluate_response(
        shifted, model, exposure_s=exposure, analog_gain=1, noise_enabled=False
    )
    first_id = center["summary"]["response_identity"]
    second_id = shifted_output["summary"]["response_identity"]
    assert first_id["scene_array_sha256"] == second_id["scene_array_sha256"]
    assert first_id["scene_sampling"] != second_id["scene_sampling"]
    assert not np.array_equal(
        center["arrays"]["mean_photocharge_e"],
        shifted_output["arrays"]["mean_photocharge_e"],
    )
