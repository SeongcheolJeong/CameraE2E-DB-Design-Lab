"""Versioned nonlinear acquisition contracts and retained runtime evidence."""

import json
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_runtime import request_data as request_data
from test_nonlinear_readout import model

from camera_platform import (
    hdr_runtime,
    nonlinear_hdr,
    nonlinear_readout,
    reference_hdr,
    reference_hdr_runtime,
)
from camera_platform.runtime import RuntimeRequestError


@pytest.fixture
def nonlinear_request(request_data):
    selected = model()
    selected["adc"]["bit_depth"] = request_data["readout"]["bit_depth"]
    selected["adc"]["black_level_dn"] = request_data["readout"]["black_level_dn"]
    request_data["capture_program"] = {
        "schema": hdr_runtime.NONLINEAR_PROGRAM_SCHEMA,
        "electrical_model": selected,
        "waveform": None,
        "hdr_merge": nonlinear_hdr.default_profile(),
        "preview": deepcopy(hdr_runtime.NONLINEAR_PREVIEW),
    }
    return request_data


def test_nonlinear_program_keeps_versions_and_preview_contract_explicit(nonlinear_request):
    p = nonlinear_request["capture_program"]
    assert hdr_runtime.validate_capture_program(p) == p
    for key, replacement in (
        ("schema", hdr_runtime.PROGRAM_SCHEMA),
        ("electrical_model", hdr_runtime.default_capture_program(
            nonlinear_request["readout"])["electrical_model"]),
        ("hdr_merge", hdr_runtime.default_capture_program(
            nonlinear_request["readout"])["hdr_merge"]),
        ("preview", {**p["preview"], "isp_profile_role": "full_isp"}),
    ):
        bad = deepcopy(p)
        bad[key] = replacement
        with pytest.raises(ValueError):
            hdr_runtime.validate_capture_program(bad)
    legacy = hdr_runtime.default_capture_program(nonlinear_request["readout"])
    legacy["electrical_model"] = p["electrical_model"]
    with pytest.raises(ValueError):
        hdr_runtime.validate_capture_program(legacy)


def test_nonlinear_runtime_retains_integer_raw_calibration_and_finite_code_observation(
    nonlinear_request,
):
    output = hdr_runtime.HDRBracketRuntime().execute(nonlinear_request)
    arrays, summary = output["arrays"], output["summary"]
    assert arrays["raw_frames"].shape == (2, 8, 10)
    assert arrays["raw_frames"].dtype.kind in "iu"
    assert summary["raw_format"]["response_type"] == "nonlinear_charge_voltage"
    assert summary["isp_profile_role"] == "fixed_color_settings_only"
    assert "sampled_raw_dark_corrected_channel_mean_dn" not in summary
    assert summary["prediction"]["method_and_version"] == hdr_runtime.NONLINEAR_OBSERVATION
    assert summary["prediction"]["dark_correction"] == "not_applied_to_raw_code_metric"
    expected = []
    for raw in arrays["raw_frames"]:
        residual = raw.astype(float) - summary["raw_format"]["black_level_dn"]
        expected.extend(float(residual[arrays["cfa_channel_index"] == c].mean()) for c in range(3))
    assert summary["prediction"]["values"] == expected
    for index, calibration in enumerate(summary["calibrations"]):
        assert calibration["schema"] == nonlinear_readout.CALIBRATION_SCHEMA
        assert calibration["dark_e_array"] == "calibration_dark_e"
        assert calibration["frame_index"] == index
        assert "dark_e" not in calibration and "dn_per_e" not in calibration
        assert calibration["model"]["schema"] == nonlinear_readout.SCHEMA
    assert all(item["preview"] == hdr_runtime.NONLINEAR_PREVIEW
               for item in summary["isp_per_frame"])
    assert summary["hdr_merge"]["schema"] == "camera_hdr_merge_result_v2"
    assert set(arrays) == set(summary["array_units"])
    assert np.isfinite(arrays["rgb"]).all()
    assert summary["physical_validation"] == "not_established"
    json.dumps(summary, allow_nan=False)


def test_nonlinear_raw_observation_retains_dark_and_does_not_decode_clipped_codes(
    nonlinear_request,
):
    p = nonlinear_request["capture_program"]
    p["hdr_merge"] = None
    p["electrical_model"]["dark_current_e_per_s"] = 1e6
    output = hdr_runtime.HDRBracketRuntime().execute(nonlinear_request)
    arrays, summary = output["arrays"], output["summary"]
    assert arrays["calibration_dark_e"].min() > 0
    assert not arrays["raw_valid_mask_frames"].any()
    assert np.isfinite(summary["prediction"]["values"]).all()
    assert np.isfinite(arrays["rgb_frames"]).all()
    assert np.isnan(arrays["ccm_rgb_frames"]).all()
    assert summary["hdr_merge"] is None


@pytest.mark.parametrize("setting", ["temperature", "gain"])
def test_operating_domain_is_rejected_before_optical_work(nonlinear_request, monkeypatch, setting):
    if setting == "temperature":
        nonlinear_request["temperature_c"] = 31
    else:
        nonlinear_request["control_sequence"]["frames"][0]["applied"]["gain"] = 4.1
    monkeypatch.setattr(hdr_runtime.TemporalSceneRuntime, "_optical_rates",
                        lambda *a: pytest.fail("Invalid nonlinear settings reached optics"))
    with pytest.raises(RuntimeRequestError):
        hdr_runtime.HDRBracketRuntime().execute(nonlinear_request)


def test_simulation_oracles_cannot_change_nonlinear_merge_or_observation(
    nonlinear_request, monkeypatch,
):
    baseline = hdr_runtime.HDRBracketRuntime().execute(nonlinear_request)
    actual_capture = nonlinear_readout.capture_electrons

    def poisoned(*args, **kwargs):
        result = actual_capture(*args, **kwargs)
        result["arrays"]["mean_total_charge_e"][:] = 1e12
        for key, value in result["arrays"].items():
            if key.endswith("_mask"):
                value[:] = ~value
        return result

    monkeypatch.setattr(nonlinear_readout, "capture_electrons", poisoned)
    result = hdr_runtime.HDRBracketRuntime().execute(nonlinear_request)
    np.testing.assert_array_equal(result["arrays"]["raw_frames"], baseline["arrays"]["raw_frames"])
    for key in ("hdr_merged_rate", "hdr_estimated_variance", "hdr_valid_mask", "rgb_frames"):
        np.testing.assert_array_equal(result["arrays"][key], baseline["arrays"][key])
    assert result["summary"]["prediction"] == baseline["summary"]["prediction"]


def test_reference_v2_uses_same_retained_calibration(nonlinear_request):
    p = nonlinear_request["capture_program"]
    p["schema"] = reference_hdr_runtime.NONLINEAR_PROGRAM_SCHEMA
    p["hdr_merge"] = reference_hdr.default_profile_v2()
    output = reference_hdr_runtime.ReferenceHDRRuntime().execute(nonlinear_request)
    assert output["summary"]["capture_program"]["schema"] == p["schema"]
    assert (output["summary"]["prediction"]["method_and_version"]
            == hdr_runtime.NONLINEAR_OBSERVATION)
    assert output["summary"]["hdr_merge"]["schema"] == "camera_reference_hdr_merge_result_v2"
    assert set(output["arrays"]) == set(output["summary"]["array_units"])
    json.dumps(output["summary"], allow_nan=False)
    p["hdr_merge"] = reference_hdr.default_profile()
    with pytest.raises(ValueError, match="match the capture program version"):
        reference_hdr_runtime.validate_capture_program(p)


@pytest.mark.parametrize("mode,schema", [
    ("independent_bracket_nonlinear", "camera_capture_program_v2"),
    ("shared_charge_nonlinear", "camera_dcg_capture_program_v2"),
    ("reference_guarded_nonlinear", "camera_reference_hdr_capture_program_v2"),
])
def test_authored_v2_templates_do_not_modify_legacy_defaults(request_data, mode, schema):
    from types import SimpleNamespace

    from camera_platform import capture_programs

    records = {
        ("assemblies", "assembly"): {"model_ids": ["sensor"]},
        ("models", "sensor"): {"kind": "sensor", "payload": {"readout": request_data["readout"]}},
    }
    service = SimpleNamespace(registry=SimpleNamespace(get=lambda k, i: deepcopy(records[k, i])))
    legacy = capture_programs.template(service, "assembly")
    draft = capture_programs.template(service, "assembly", mode)
    assert draft["payload"]["schema"] == schema
    assert draft["source_kind"] == "estimated" and "authored" in draft["source_uri"]
    assert draft["method_and_version"] == "authored_nonlinear_charge_voltage_example:1"
    assert draft["payload"]["preview"] == hdr_runtime.NONLINEAR_PREVIEW
    assert capture_programs.template(service, "assembly") == legacy
    assert legacy["payload"]["schema"] == hdr_runtime.PROGRAM_SCHEMA
    assert "preview" not in legacy["payload"]
