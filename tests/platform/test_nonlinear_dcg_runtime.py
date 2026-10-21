"""Two nonlinear paths remain one acquisition with original paired RAW evidence."""

import json
from copy import deepcopy

import numpy as np
import pytest
from test_dcg_runtime import request_data as request_data
from test_nonlinear_dcg import model

from camera_platform import dcg_runtime, hdr_runtime, nonlinear_dcg
from camera_platform.runtime import RuntimeRequestError


@pytest.fixture
def nonlinear_request(request_data):
    selected = model()
    for path in selected["paths"]:
        path["adc"].update(bit_depth=request_data["readout"]["bit_depth"], black_level_dn=64)
    request_data["capture_program"] = {
        "schema": dcg_runtime.NONLINEAR_PROGRAM_SCHEMA, "electrical_model": selected,
        "merge_profile": nonlinear_dcg.default_profile(),
        "preview": deepcopy(hdr_runtime.NONLINEAR_PREVIEW),
    }
    return request_data


def test_nonlinear_dcg_keeps_paths_calibration_masks_and_raw_observation(nonlinear_request):
    output = dcg_runtime.SharedChargeDCGRuntime().execute(nonlinear_request)
    a, s = output["arrays"], output["summary"]
    assert a["raw_groups"].shape == (2, 2, 8, 10)
    assert a["rgb_groups"].shape == (2, 2, 8, 10, 3)
    assert a["path_raw_valid_mask_groups"].shape == (2, 2, 8, 10)
    assert a["electrical_read_noise_v"].shape == (2, 2, 8, 10)
    assert s["array_units"]["electrical_read_noise_v"] == "V"
    assert all(fmt["response_type"] == "nonlinear_charge_voltage" for fmt in s["raw_formats"])
    assert s["prediction"]["method_and_version"] == dcg_runtime.NONLINEAR_OBSERVATION
    assert s["prediction"]["dark_correction"] == "not_applied_to_raw_code_metric"
    expected = []
    for acquisition in a["raw_groups"]:
        for index, path in enumerate(acquisition):
            residual = path.astype(float) - s["raw_formats"][index]["black_level_dn"]
            expected.extend(float(residual[a["cfa_channel_index"] == c].mean()) for c in range(3))
    assert s["prediction"]["values"] == expected
    for cal in s["calibrations"]:
        assert "dark_e" not in cal and cal["dark_e_array"] == "calibration_dark_e"
        assert all("dark_e" not in path for path in cal["paths"])
    assert s["isp_profile_role"] == "fixed_color_settings_only"
    assert set(a) == set(s["array_units"])
    assert np.isfinite(a["rgb"]).all()
    assert "raw_frames" not in a
    json.dumps(s, allow_nan=False)


def test_nonlinear_dcg_without_merge_retains_calibrated_path_validity(nonlinear_request):
    nonlinear_request["capture_program"]["merge_profile"] = None
    nonlinear_request["capture_program"]["electrical_model"]["dark_current_e_per_s"] = 1e6
    output = dcg_runtime.SharedChargeDCGRuntime().execute(nonlinear_request)
    a, s = output["arrays"], output["summary"]
    assert not s["merge_applied"] and "merged_rate" not in a
    assert not a["path_raw_valid_mask_groups"].any()
    assert np.isnan(a["path_ccm_rgb_groups"]).all()
    assert np.isfinite(a["rgb_groups"]).all()
    assert np.isfinite(s["prediction"]["values"]).all()


def test_nonlinear_dcg_does_not_relax_acquisition_gain_or_schema(nonlinear_request):
    p = nonlinear_request["capture_program"]
    legacy = dcg_runtime.default_capture_program(nonlinear_request["readout"])
    for replacement in ({**p, "schema": dcg_runtime.PROGRAM_SCHEMA},
                        {**p, "electrical_model": legacy["electrical_model"]},
                        {**p, "merge_profile": legacy["merge_profile"]}):
        with pytest.raises(ValueError):
            dcg_runtime.validate_capture_program(replacement)
    nonlinear_request["control_sequence"]["frames"][0]["applied"]["gain"] = 2
    with pytest.raises(RuntimeRequestError, match="gain"):
        dcg_runtime.SharedChargeDCGRuntime().execute(nonlinear_request)


def test_paired_oracles_cannot_change_merge(nonlinear_request, monkeypatch):
    first = dcg_runtime.SharedChargeDCGRuntime().execute(nonlinear_request)
    actual = nonlinear_dcg.capture_shared_electrons

    def poisoned(*args, **kwargs):
        result = actual(*args, **kwargs)
        for key, value in result["arrays"].items():
            if key.endswith("_mask"):
                value[:] = ~value
            elif key in {"mean_total_charge_e", "shared_charge_e", "read_noise_v"}:
                value[:] = 1e10
        return result

    monkeypatch.setattr(nonlinear_dcg, "capture_shared_electrons", poisoned)
    second = dcg_runtime.SharedChargeDCGRuntime().execute(nonlinear_request)
    for key in ("raw_groups", "merged_rate", "shared_shot_read_rate_variance_estimate",
                "valid_mask", "rgb_groups"):
        np.testing.assert_array_equal(second["arrays"][key], first["arrays"][key])
    assert second["summary"]["prediction"] == first["summary"]["prediction"]
