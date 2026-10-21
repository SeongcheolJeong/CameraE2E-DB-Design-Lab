"""Joint selection and common spatial support cannot reward censored borders."""

import copy

import numpy as np
import pytest

from camera_platform import db_design_aperture as aperture
from camera_platform import db_design_motion_metrics as motion
from test_db_design_aperture import _array, _row, _write, captured as captured


def _support(captured, bounds=(1, 7, 1, 7)):
    root, capture, rendered = captured
    for chart in aperture.CHARTS:
        for record in capture["captures"][chart]["raw"]:
            record["supported_roi_row_col"] = list(bounds)
            record["supported_saturation_fractions"] = dict(record["saturation_fractions"])
            if chart == "resolution":
                record["sfr_support_roi_row_col"] = rendered["tests"]["resolution"]["sites"]["center"]["roi"]
                record["sfr_support_saturation_fractions"] = dict(record["saturation_fractions"])
    _write(root / "capture.json", capture)
    return root, capture, rendered, list(bounds)


def test_optional_roi_keeps_legacy_full_crop_semantics(captured):
    root, _, rendered = captured
    before = aperture.measure_field(root, rendered)
    assert before == aperture.measure_field(root, rendered, roi_row_col=None)
    _support(captured, (0, 8, 0, 8))
    after = aperture.measure_field(root, rendered, roi_row_col=[0, 8, 0, 8])
    # The capture metadata changed only to add supported fractions.
    before["measurement"]["capture_sha256"] = after["measurement"]["capture_sha256"]
    assert before == after


def test_unsupported_border_noise_clipping_and_signal_cannot_affect_score(captured):
    root, capture, rendered, bounds = _support(captured)
    before = motion.measure_field(root, rendered, valid_rect=bounds, isp_guard_px=0)
    rr, cc = np.indices((8, 8))
    border = (rr < 1) | (rr >= 7) | (cc < 1) | (cc >= 7)
    for chart in aperture.CHARTS:
        for repeat, record in enumerate(capture["captures"][chart]["raw"]):
            path = root / chart / record["file"]
            raw = np.load(path)
            raw[border] = 4095 if repeat % 2 else 0
            record["sha256"] = _array(path, raw)
            record["saturation_fractions"]["well_saturated_mask"] = .9
            record["saturation_fractions"]["adc_high_mask"] = .9
        path = root / chart / "mean-electron-rate.npy"
        rate = np.load(path)
        rate[border] = 1e10
        capture["captures"][chart]["rate_sha256"] = _array(path, rate)
    _write(root / "capture.json", capture)
    after = motion.measure_field(root, rendered, valid_rect=bounds, isp_guard_px=0)
    for key in ("mean_signal_e", "temporal_noise_e", "snr_db", "saturation_fraction",
                "processed_sfr50_cpp", "snr_censored", "measurement_status"):
        assert after[key] == before[key]
    assert after["mean_signal_e"] == 100
    assert after["measurement"]["green_pixel_count"] == 18
    assert after["measurement"]["roi_row_col"] == bounds
    assert after["measurement_status"] == "ok"


@pytest.mark.parametrize("failure", ["missing", "different"])
def test_supported_fraction_coordinates_are_mandatory(captured, failure):
    root, capture, rendered, bounds = _support(captured)
    record = capture["captures"]["texture"]["raw"][3]
    if failure == "missing":
        del record["supported_roi_row_col"]
    else:
        record["supported_roi_row_col"] = [0, 8, 0, 8]
    _write(root / "capture.json", capture)
    with pytest.raises(ValueError, match="saturation support differs"):
        motion.measure_field(root, rendered, valid_rect=bounds, isp_guard_px=0)


@pytest.mark.parametrize("bounds", [[0, 9, 0, 8], [1, 1, 0, 8], [0, 8, 0, True], [1.0, 7, 1, 7]])
def test_invalid_common_rectangle_rejected(captured, bounds):
    root, _, rendered = captured
    with pytest.raises(ValueError, match="supported rectangle"):
        motion.measure_field(root, rendered, valid_rect=bounds, isp_guard_px=0)


def test_central_edge_must_fit_shared_motion_and_isp_support(captured):
    root, _, rendered, bounds = _support(captured, (2, 7, 1, 7))
    result = motion.measure_field(root, rendered, valid_rect=bounds, isp_guard_px=0)
    assert result["processed_sfr50_cpp"] is None
    assert result["processed_sfr_reason"] == "central_edge_roi_outside_motion_and_ISP_support"
    assert result["measurement_status"] == "inconclusive"


@pytest.mark.parametrize("where,reason", [
    ("uniform", "uniform_noise_is_censored_by_well_or_readout_saturation"),
    ("resolution", "resolution_sfr_censored_by_well_or_readout_saturation"),
])
def test_well_clipping_cannot_be_hidden_below_saturation_policy(captured, where, reason):
    root, capture, rendered, bounds = _support(captured)
    capture["captures"][where]["raw"][0]["supported_saturation_fractions"]["well_saturated_mask"] = .001
    if where == "resolution":
        capture["captures"][where]["raw"][0]["sfr_support_saturation_fractions"]["well_saturated_mask"] = .001
    _write(root / "capture.json", capture)
    result = motion.measure_field(root, rendered, valid_rect=bounds, isp_guard_px=0)
    assert reason in result["measurement_issues"]
    assert result["measurement_status"] == "inconclusive"
    assert result["saturation_fraction"] == pytest.approx(.001)
    row = _joint("bad", 4.12, 1)
    result["relative_signal"] = 1
    row["fields"][0] = result
    assert motion.assess([row], {"min_snr_db": -20, "max_saturation_fraction": 1})["recommended_candidate_id"] is None


def test_resolution_missing_supported_saturation_is_not_a_pass(captured):
    root, capture, rendered, bounds = _support(captured)
    del capture["captures"]["resolution"]["raw"][0]["sfr_support_saturation_fractions"]
    _write(root / "capture.json", capture)
    result = motion.measure_field(root, rendered, valid_rect=bounds, isp_guard_px=0)
    assert result["processed_sfr50_cpp"] is None
    assert result["measurement_status"] == "inconclusive"


def test_clipping_outside_edge_and_isp_support_does_not_invalidate_sfr(captured):
    captured[2]["tests"]["resolution"]["sites"]["center"]["roi"] = [3, 5, 3, 5]
    root, capture, rendered, bounds = _support(captured, (0, 8, 0, 8))
    for record in capture["captures"]["resolution"]["raw"]:
        record["sfr_support_roi_row_col"] = [2, 6, 2, 6]
        path = root / "resolution" / record["file"]
        raw = np.load(path)
        raw[0, 0] = 4095
        record["sha256"] = _array(path, raw)
        record["supported_saturation_fractions"]["adc_high_mask"] = 1 / 64
    _write(root / "capture.json", capture)
    result = motion.measure_field(root, rendered, valid_rect=bounds, isp_guard_px=1)
    assert result["saturation_fraction"] == 1 / 64
    assert result["processed_sfr50_cpp"] == .2
    assert result["processed_sfr_status"] == "ok"
    assert result["measurement"]["sfr_support_roi_row_col"] == [2, 6, 2, 6]


def test_isp_influence_support_coordinates_cannot_silently_change(captured):
    root, capture, rendered, bounds = _support(captured)
    capture["captures"]["resolution"]["raw"][2]["sfr_support_roi_row_col"] = [0, 8, 0, 8]
    _write(root / "capture.json", capture)
    with pytest.raises(ValueError, match="SFR saturation support differs"):
        motion.measure_field(root, rendered, valid_rect=bounds, isp_guard_px=0)


def test_edge_support_uses_guard_once_and_rejects_missing_native_input():
    assert motion.edge_support_roi([108, 148, 108, 148], [256, 256]) == [100, 156, 100, 156]
    with pytest.raises(ValueError, match="outside the native crop"):
        motion.edge_support_roi([4, 44, 4, 44], [256, 256])


def _joint(identity, f_number, exposure_ms, *, blur=1, snr_db=30, sfrs=(.2, .15)):
    return {**_row(identity, f_number, sfrs=sfrs, snr_db=snr_db),
            "exposure_ms": exposure_ms, "blur_length_px": blur, "source_candidate_id": "source"}


def test_joint_selection_prioritizes_worst_sfr_then_snr_then_short_exposure_then_f():
    rows = [
        _joint("low_f_long", 2.75, 16),
        _joint("high_f_short", 8, 1),
        _joint("lower_f_short", 5.6, 1),
        _joint("better_snr", 11, 16, snr_db=31),
        _joint("better_worst_sfr", 11, 16, sfrs=(.16, .16)),
    ]
    before = copy.deepcopy(rows)
    result = motion.assess(rows)
    assert result["eligible_candidate_ids"] == [
        "better_worst_sfr", "better_snr", "lower_f_short", "high_f_short", "low_f_long"
    ]
    assert result["recommended_candidate_id"] == "better_worst_sfr"
    assert result["tie_breakers"][1] == "shorter exposure"
    assert rows == before


@pytest.mark.parametrize("key,value,reason", [
    ("exposure_ms", 0, "invalid_exposure_ms"),
    ("exposure_ms", float("nan"), "invalid_exposure_ms"),
    ("exposure_ms", True, "invalid_exposure_ms"),
    ("blur_length_px", -1, "invalid_blur_length_px"),
    ("blur_length_px", float("inf"), "invalid_blur_length_px"),
    ("blur_length_px", None, "invalid_blur_length_px"),
])
def test_invalid_motion_coordinates_are_fail_closed(key, value, reason):
    row = _joint("bad", 4.12, 1)
    row[key] = value
    result = motion.assess([row])
    assert reason in result["decisions"][0]["reasons"]
    assert result["recommended_candidate_id"] is None


def test_default_relative_signal_does_not_arbitrarily_exclude_closed_iris():
    row = _joint("closed", 11, 16, blur=0)
    for field in row["fields"]:
        field["relative_signal"] = .063
    assert motion.assess([row])["recommended_candidate_id"] == "closed"
    assert motion.assess([row], {"min_relative_signal": .1})["recommended_candidate_id"] is None
