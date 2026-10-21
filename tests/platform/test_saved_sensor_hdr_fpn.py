"""Model-known FPN calibration preserves saved RAW and corrects its rate inverse."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import hdr
from camera_platform.saved_sensor_hdr import (
    FPN_NATIVE,
    FPN_OVERRIDE,
    _merge_native_fpn,
    evaluate_saved_sensor_hdr_bracket,
)
from camera_platform.sensor_db_bridge import sensor_revision_catalog

WORKSPACE = Path(__file__).resolve().parents[2]
CODE = "DEF-1801-802"


def _selected() -> dict:
    rows = sensor_revision_catalog(root=WORKSPACE / "camerae2e_db", query=CODE)
    selected = next((row for row in rows if row["report_code"] == CODE), None)
    if selected is None:
        pytest.skip("Example saved physical sensor run is not installed")
    return selected


def test_native_model_fpn_keeps_original_raw_and_records_per_pixel_maps(tmp_path):
    selected = _selected()
    shared = {
        "run_id": selected["run_id"],
        "root": WORKSPACE / "camerae2e_db",
        "workspace": WORKSPACE,
        "exposure_s": [0.0005, 0.008],
        "seed": 11,
    }
    native = evaluate_saved_sensor_hdr_bracket(
        CODE,
        output_dir=tmp_path / "native",
        fixed_pattern_mode=FPN_NATIVE,
        **shared,
    )
    zero = evaluate_saved_sensor_hdr_bracket(
        CODE,
        output_dir=tmp_path / "zero",
        fixed_pattern_mode=FPN_OVERRIDE,
        **shared,
    )
    assert native["revision"] == zero["revision"]
    assert native["controls"]["fixed_pattern_mode"] == "model_known_native_fpn"
    assert native["controls"]["readout_overrides"] == {}
    assert native["controls"]["executed_fixed_pattern"] == native["controls"][
        "saved_fixed_pattern"
    ]
    calibration = native["controls"]["model_fpn_calibration"]
    assert calibration["independent_calibration"] is False
    assert calibration["source"] == "saved_simulation_model_known_maps_not_measured"
    assert native["fusion"]["method"] == "inverse_variance_raw_per_pixel_model_fpn"
    assert native["fusion"]["valid_pixels"] > 0
    assert native["fusion"]["calibration_independence"] is False
    assert native["captures"][0]["model_fpn_map_sha256"] == native["captures"][1][
        "model_fpn_map_sha256"
    ]
    assert len(native["levels"]) == 6
    assert native["levels"][0]["mean_merged_rate_e_per_pixel_s"] > native["levels"][
        -1
    ]["mean_merged_rate_e_per_pixel_s"]
    npz_path = Path(native["artifacts"]["npz"])
    report_path = Path(native["artifacts"]["json"])
    assert hashlib.sha256(npz_path.read_bytes()).hexdigest() == native["artifact_sha256"]["npz"]
    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == native["artifact_sha256"]["json"]
    on_disk = json.loads(report_path.read_text())
    assert on_disk["controls"]["model_fpn_calibration"]["map_sha256"] == calibration[
        "map_sha256"
    ]
    with np.load(npz_path, allow_pickle=False) as arrays:
        raw = arrays["original_raw_dn"]
        assert raw.dtype == np.int32
        assert raw.shape == (2, 36, 36)
        assert hashlib.sha256(raw.tobytes()).hexdigest() == native["fusion"][
            "input_array_bytes_sha256"
        ]
        assert np.ptp(arrays["model_fpn_response_gain"]) > 0
        assert np.ptp(arrays["model_fpn_post_well_bias_e"]) > 0
        assert arrays["hdr_valid_mask"].any()
        with np.load(zero["artifacts"]["npz"], allow_pickle=False) as baseline:
            assert not np.array_equal(raw, baseline["original_raw_dn"])
            assert "model_fpn_response_gain" not in baseline.files


def _calibrations(
    gain: np.ndarray, bias_e: np.ndarray, exposures: list[float], *, dark_rate: float = 0.0
) -> list[dict]:
    return [
        {
            "dn_per_e": 1.0,
            "black_level_dn": 64.0,
            "white_level_dn": 4095.0,
            "full_well_e": 1000.0,
            "dark_e": np.full(gain.shape, dark_rate * exposure),
            "read_noise_variance_e2": 0.0,
            "quantization_variance_e2": 1 / 12,
            "linear_ceiling_dn": 1064.0,
            "linear_floor_dn": 0.0,
            "response_gain": gain,
            "post_well_bias_e": bias_e,
        }
        for exposure in exposures
    ]


def test_per_pixel_inverse_recovers_rate_and_censors_biased_well_clip():
    gain = np.tile(np.array([[0.9, 1.1], [1.2, 0.8]]), (2, 2))
    bias = np.tile(np.array([[5.0, -2.0], [3.0, -4.0]]), (2, 2))
    exposures = [0.1, 2.0]
    photo_rate = 900.0
    dark_rate = 10.0
    raw = np.stack(
        [
            np.rint(64 + np.minimum(gain * (photo_rate + dark_rate) * t, 1000) + bias)
            for t in exposures
        ]
    ).astype(np.int32)
    merged = _merge_native_fpn(
        raw,
        exposures,
        _calibrations(gain, bias, exposures, dark_rate=dark_rate),
        geometry={"kind": "native_sensor", "full_sensor_shape": [4, 4], "crop_origin": [0, 0]},
    )
    arrays = merged["arrays"]
    assert np.all(arrays["exposure_valid_mask"][0])
    assert not np.any(arrays["exposure_valid_mask"][1])
    np.testing.assert_allclose(arrays["merged_rate"], photo_rate, atol=6.0)
    assert merged["summary"]["calibration_source"] == (
        "saved_simulation_model_known_maps_not_measured"
    )
    assert merged["summary"]["single_usable_exposure_pixels"] == 16
    assert merged["summary"]["input_array_bytes_sha256"] == hashlib.sha256(
        raw.tobytes()
    ).hexdigest()


def test_native_fpn_rejects_nonpositive_gain_without_publishing_rate():
    raw = np.full((2, 4, 4), 100, dtype=np.int32)
    gain = np.ones((4, 4))
    gain[0, 0] = 0
    calibrations = _calibrations(gain, np.zeros((4, 4)), [0.1, 0.2])
    with pytest.raises(ValueError, match="invalid gain"):
        _merge_native_fpn(
            raw,
            [0.1, 0.2],
            calibrations,
            geometry={
                "kind": "native_sensor",
                "full_sensor_shape": [4, 4],
                "crop_origin": [0, 0],
            },
        )


def test_unit_maps_match_existing_scalar_merger_on_uncensored_raw():
    raw = np.stack(
        [np.full((4, 4), 164, dtype=np.int32), np.full((4, 4), 464, dtype=np.int32)]
    )
    exposure_s = [0.1, 0.4]
    gain = np.ones((4, 4))
    bias = np.zeros((4, 4))
    native_calibrations = _calibrations(gain, bias, exposure_s)
    geometry = {"kind": "native_sensor", "full_sensor_shape": [4, 4], "crop_origin": [0, 0]}
    native = _merge_native_fpn(
        raw, exposure_s, native_calibrations, geometry=geometry
    )
    scalar = hdr.merge_raw_bracket(
        raw,
        exposure_s,
        [{key: value for key, value in item.items() if key in hdr.CALIBRATION_FIELDS}
         for item in native_calibrations],
        geometry=geometry,
        profile=hdr.default_profile(),
    )
    np.testing.assert_array_equal(native["arrays"]["valid_mask"], scalar["arrays"]["valid_mask"])
    for key in ("merged_rate", "estimated_variance", "weights"):
        np.testing.assert_allclose(native["arrays"][key], scalar["arrays"][key])
