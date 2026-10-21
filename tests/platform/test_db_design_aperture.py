"""Aperture-demo temporal signal/noise and fail-closed conditional selection."""

import copy
import hashlib
import json

import numpy as np
import pytest

from camera_platform import db_design_aperture as aperture
from camera_platform.electrical import default_electrical_model


def _write(path, value):
    path.write_text(json.dumps(value))


def _array(path, value):
    np.save(path, value)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def captured(tmp_path):
    readout = {
        "gain": 1,
        "full_well_e": 4031.0,
        "read_noise_e": 2.0,
        "dark_current_e_per_s": 100.0,
        "bit_depth": 12,
        "black_level_dn": 64.0,
    }
    shape = (8, 8)
    rr, cc = np.indices(shape)
    green = (rr % 2) != (cc % 2)
    # Large fixed spatial variation must not enter the temporal-noise denominator.
    rate = np.full(shape, 25000.0)
    rate[~green] = 500000.0
    fixed = (rr * 40 + cc * 8).astype(int)
    captures = {}
    for chart in aperture.CHARTS:
        folder = tmp_path / chart
        folder.mkdir()
        records = []
        for repeat in range(8):
            raw = (64 + 100 + fixed + (2 * repeat - 7)).astype(np.int32)
            filename = f"raw-{repeat:02d}.npy"
            records.append(
                {
                    "file": filename,
                    "sha256": _array(folder / filename, raw),
                    "seed": repeat + 10,
                    "saturation_fractions": {
                        "well_saturated_mask": 0.0,
                        "adc_high_mask": 0.0,
                        "digital_high_mask": 0.0,
                    },
                }
            )
        captures[chart] = {
            "raw": records,
            "rate_sha256": _array(folder / "mean-electron-rate.npy", rate),
        }
    capture = {
        "configuration": {"frames": 8, "exposure_ms": 4},
        "geometry": {
            "native_shape_row_col": list(shape),
            "cfa_origin": {"pattern": "RGGB", "local_row": 0, "local_col": 0},
        },
        "readout": readout,
        "captures": captures,
    }
    _write(tmp_path / "capture.json", capture)
    _write(
        tmp_path / "uniform/capture.json",
        {
            "model": default_electrical_model(readout),
            "noise_enabled": True,
            "exposure_s": 0.004,
            "analog_gain": 1.0,
        },
    )
    rendered = {
        "id": "center",
        "status": "succeeded",
        "tests": {
            "resolution": {
                "status": "ok",
                "sites": {
                    "center": {
                        "roi": [1, 7, 1, 7],
                        "processed_system": {
                            "status": "ok",
                            "mtf50_cycles_per_pixel": 0.2,
                            "mtf50_reason": None,
                        },
                    }
                },
            }
        },
    }
    return tmp_path, capture, rendered


def test_green_signal_and_per_pixel_temporal_variance_share_roi(captured):
    root, _, rendered = captured
    result = aperture.measure_field(root, rendered)
    assert result["mean_signal_e"] == pytest.approx(100)
    expected_noise = np.std(np.arange(8) * 2, ddof=1)
    assert result["temporal_noise_e"] == pytest.approx(expected_noise)
    assert result["snr_linear"] == pytest.approx(100 / expected_noise)
    assert result["snr_db"] == pytest.approx(20 * np.log10(100 / expected_noise))
    assert result["measurement"]["sites"] == ["Gr", "Gb"]
    assert result["measurement"]["green_pixel_count"] == 32
    assert result["measurement_status"] == "ok"
    assert result["saturation_fraction"] == 0


def test_saturation_covers_nonuniform_charts_and_all_repeats(captured):
    root, capture, rendered = captured
    capture["captures"]["color"]["raw"][-1]["saturation_fractions"].update(
        well_saturated_mask=0.006, adc_high_mask=0.007, digital_high_mask=0.007
    )
    _write(root / "capture.json", capture)
    result = aperture.measure_field(root, rendered)
    assert result["saturation_fraction"] == pytest.approx(0.02)
    assert result["measurement_status"] == "ok"
    del capture["captures"]["texture"]["raw"][3]["saturation_fractions"]
    _write(root / "capture.json", capture)
    result = aperture.measure_field(root, rendered)
    assert result["saturation_fraction"] is None
    assert result["measurement_status"] == "inconclusive"


@pytest.mark.parametrize("failure", ["missing", "duplicate_seed", "changed_hash", "nonfinite"])
def test_invalid_or_nonindependent_preserved_capture_fails(captured, failure):
    root, capture, rendered = captured
    record = capture["captures"]["uniform"]
    if failure == "missing":
        record["raw"].pop()
    elif failure == "duplicate_seed":
        record["raw"][1]["seed"] = record["raw"][0]["seed"]
    elif failure == "changed_hash":
        record["raw"][0]["sha256"] = "bad"
    else:
        value = np.load(root / "uniform/mean-electron-rate.npy")
        value[0, 1] = np.nan
        record["rate_sha256"] = _array(root / "uniform/mean-electron-rate.npy", value)
    _write(root / "capture.json", capture)
    with pytest.raises(ValueError):
        aperture.measure_field(root, rendered)


def test_no_crossing_or_clipped_noise_never_becomes_an_infinite_score(captured):
    root, capture, rendered = captured
    sfr = rendered["tests"]["resolution"]["sites"]["center"]["processed_system"]
    sfr.update(mtf50_cycles_per_pixel=None, mtf50_reason="no_half_response_crossing_below_nyquist")
    assert aperture.measure_field(root, rendered)["processed_sfr50_cpp"] is None
    for record in capture["captures"]["uniform"]["raw"]:
        raw = np.zeros((8, 8), dtype=np.int32)
        record["sha256"] = _array(root / "uniform" / record["file"], raw)
    _write(root / "capture.json", capture)
    result = aperture.measure_field(root, rendered)
    assert result["snr_db"] is None and result["snr_linear"] is None
    assert result["snr_censored"] is True
    assert result["measurement_status"] == "inconclusive"


def _row(identity, f_number, sfrs=(0.2, 0.15), *, snr_db=30, signal=0.5, saturation=0):
    linear_snr = 10 ** (snr_db / 20)
    return {
        "candidate_id": identity,
        "f_number": f_number,
        "fields": [
            {
                "field_id": name,
                "mean_signal_e": 100,
                "temporal_noise_e": 100 / linear_snr,
                "snr_linear": linear_snr,
                "snr_db": snr_db,
                "relative_signal": signal,
                "processed_sfr50_cpp": sfr,
                "saturation_fraction": saturation,
                "measurement_status": "ok",
                "processed_sfr_status": "ok",
            }
            for name, sfr in zip(aperture.REQUIRED_FIELDS, sfrs, strict=True)
        ],
    }


def test_selection_uses_worst_field_and_declared_tie_breakers():
    good = _row("good", 5.6, (0.2, 0.19))
    center_only = _row("worse", 4.12, (0.4, 0.18))
    tied_higher_snr = _row("higher_snr", 8, (0.2, 0.19), snr_db=31)
    tied_lower_f = _row("lower_f", 4.12, (0.2, 0.19), snr_db=31)
    result = aperture.assess([good, center_only, tied_higher_snr, tied_lower_f])
    assert result["recommended_candidate_id"] == "lower_f"
    assert result["eligible_candidate_ids"] == ["lower_f", "higher_snr", "good", "worse"]


@pytest.mark.parametrize(
    "failure",
    [
        "missing_field",
        "missing_metric",
        "censored",
        "saturated",
        "dim",
        "low_snr",
        "inconsistent_snr",
        "invalid_sfr",
        "warn_sfr",
    ],
)
def test_selection_missing_censored_or_invalid_metrics_are_not_eligible(failure):
    candidate = _row("candidate", 5.6)
    field = candidate["fields"][1]
    if failure == "missing_field":
        candidate["fields"].pop()
    elif failure == "missing_metric":
        field["snr_db"] = None
    elif failure == "censored":
        field["snr_censored"] = True
    elif failure == "saturated":
        field["saturation_fraction"] = 0.011
    elif failure == "dim":
        field["relative_signal"] = 0.099
    elif failure == "low_snr":
        candidate = _row("candidate", 5.6, snr_db=24.9)
    elif failure == "inconsistent_snr":
        field["snr_db"] = 99
    elif failure == "invalid_sfr":
        field["processed_sfr50_cpp"] = float("nan")
    else:
        field["processed_sfr_status"] = "warning"
    result = aperture.assess([candidate])
    assert result["recommended_candidate_id"] is None
    assert result["eligible_candidate_ids"] == []
    assert result["decisions"][0]["reasons"]


def test_constraints_include_boundaries_and_policy_is_frozen():
    policy = copy.deepcopy(aperture.DEFAULT_POLICY)
    candidate = _row("boundary", 4.12, snr_db=25, signal=0.1, saturation=0.01)
    assert aperture.assess([candidate], policy)["recommended_candidate_id"] == "boundary"
    assert policy == aperture.DEFAULT_POLICY
    with pytest.raises(ValueError):
        aperture.assess([candidate], {"min_snr_db": True})


def test_uniform_well_clipping_censors_snr_even_without_adc_endpoint(captured):
    root, capture, rendered = captured
    # A saturated charge node can still receive read noise and encode below white.
    capture["captures"]["uniform"]["raw"][4]["saturation_fractions"].update(
        well_saturated_mask=0.005, adc_high_mask=0.0, digital_high_mask=0.0
    )
    _write(root / "capture.json", capture)
    result = aperture.measure_field(root, rendered)
    assert result["measurement"]["uniform_green_adc_endpoint_fraction"] == 0
    assert result["measurement"]["uniform_high_saturation_upper_bound"] == .005
    assert result["snr_censored"] is True
    assert result["measurement_status"] == "inconclusive"
    assert "uniform_noise_is_censored_by_well_or_readout_saturation" in result["measurement_issues"]
    assert result["snr_db"] is not None  # Stored diagnostic remains available, never a valid winner.
    fields = [{**result, "field_id": name, "relative_signal": 1.0}
              for name in aperture.REQUIRED_FIELDS]
    selected = aperture.assess([{"candidate_id": "clipped", "f_number": 4.12, "fields": fields}],
                               {"min_snr_db": -20, "max_saturation_fraction": 1.0})
    assert selected["recommended_candidate_id"] is None
    assert not selected["decisions"][0]["eligible"]


def test_processed_sfr_refuses_clipped_central_roi_but_not_unrelated_roi(captured):
    root, _, rendered = captured
    support = {"eligible": False, "invalid_roi_ids": ["center"], "valid_pixels": 32,
               "reason": "measurement_roi_outside_common_support"}
    rendered["tests"]["isp_lab"] = {"metric_support": {"resolution": support}}
    invalid = aperture.measure_field(root, rendered)
    assert invalid["processed_sfr50_cpp"] is None
    assert invalid["processed_sfr_status"] == "unavailable"
    assert invalid["measurement_status"] == "inconclusive"
    # Selection evaluates the declared central ROI of each physical field probe,
    # not an unrelated local corner ROI in the same probe.
    support["invalid_roi_ids"] = ["upper_left"]
    valid = aperture.measure_field(root, rendered)
    assert valid["processed_sfr50_cpp"] == .2
    assert valid["measurement_status"] == "ok"
