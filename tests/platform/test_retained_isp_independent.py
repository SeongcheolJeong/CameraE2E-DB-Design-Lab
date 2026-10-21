"""Scalar DSP references and actual source-to-processor checks independent of filters."""

import math
from copy import deepcopy

import numpy as np
import pytest
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow
from test_retained_isp_source import complete, selector

from camera_platform import isp, retained_isp, retained_isp_source
from camera_platform.registry import digest


def metadata(shape, *, starts=None, exposure=1_000, line=137, clear=None):
    count, height, width = shape
    # Stay inside the platform's binary64-safe JSON timestamp contract while
    # exercising large absolute times and exact one-nanosecond gaps.
    starts = starts if starts is not None else [2**52 + i * 10_000 for i in range(count)]
    clear = clear if clear is not None else [False] * count
    records, rows = [], []
    for index, start in enumerate(starts):
        identity = f"independent-{index}"
        opened = [start + (row + 2) * line for row in range(height)]
        ended = [value + exposure for value in opened]
        records.append(
            {
                "frame_id": identity,
                "frame_start_ns": start,
                "applied": {
                    "applied_at_ns": start,
                    "exposure_ns": exposure,
                    "gain": 1.0,
                    "isp_model_id": "declared-fixed-color",
                    "command_id": None,
                },
            }
        )
        rows.append(
            {
                "frame_id": identity,
                "row_open_ns": opened,
                "row_transfer_ns": ended,
                "row_clear_ns": opened if clear[index] else None,
                "row_gap_ns": [0] * height
                if not index
                else [a - b for a, b in zip(opened, rows[-1]["row_transfer_ns"], strict=True)],
                "clear_before_exposure": clear[index],
            }
        )
    events = {
        "schema": "camera_retained_row_events_v1",
        "clock": {"domain": "independent-integer-clock", "unit": "ns", "origin": "authored"},
        "event_contract": {
            "transfer_at": "row_exposure_end",
            "clear_at": "row_exposure_start",
            "simultaneous_order": ["transfer", "clear", "open"],
            "initial_state": "zero_at_each_row_first_exposure_start",
            "dark_current": "continuous_between_row_events",
            "photo_admission": "exposure_windows_only",
            "read_node_reset": "fresh_per_read",
        },
        "full_sensor_shape": [height, width],
        "initial_row_time_ns": rows[0]["row_open_ns"],
        "frames": rows,
    }
    events["schedule_sha256"] = digest(events)
    return {
        "geometry": {
            "kind": "native_sensor",
            "full_sensor_shape": [height, width],
            "crop_origin": [0, 0],
        },
        "frame_records": records,
        "events": events,
    }


def format_12bit():
    return {
        "cfa": "RGGB",
        "bit_depth": 12,
        "black_level_dn": 64,
        "response_type": "linear",
        "packing": "unpacked",
        "layout": "bayer",
        "hdr_streams": 1,
    }


def test_json_unsafe_absolute_nanosecond_clock_is_refused():
    raw = np.full((2, 6, 8), 128, dtype=np.int32)
    original = metadata(raw.shape, starts=[2**53, 2**53 + 10_000])
    with pytest.raises(isp.ISPError, match="integer nanoseconds"):
        retained_isp.process_raw(raw, format_12bit(), profile(), **original)


def profile():
    value = retained_isp.default_profile()
    value["temporal_filter"].update(
        mode="causal_difference_gate_v1", previous_weight=0.5, threshold_dn=4095
    )
    return value


def reflect_one(index, length):
    if length == 1:
        return 0
    return -index if index < 0 else 2 * length - 2 - index if index >= length else index


def spatial_scalar(raw, black, white, strength, sigma):
    result = raw.astype(float) - black
    frames, height, width = raw.shape
    for frame in range(frames):
        for row in range(height):
            for col in range(width):
                center_code = int(raw[frame, row, col])
                if center_code in (0, white):
                    continue
                numerator, denominator = 0.0, 0.0
                for dy in (-1, 0, 1):
                    y = row % 2 + 2 * reflect_one(row // 2 + dy, height // 2)
                    for dx in (-1, 0, 1):
                        x = col % 2 + 2 * reflect_one(col // 2 + dx, width // 2)
                        neighbor = int(raw[frame, y, x])
                        if neighbor in (0, white):
                            continue
                        kernel = (2 if dy == 0 else 1) * (2 if dx == 0 else 1)
                        weight = kernel * math.exp(-0.5 * ((neighbor - center_code) / sigma) ** 2)
                        numerator += weight * (neighbor - black)
                        denominator += weight
                center = center_code - black
                result[frame, row, col] = center + strength * (numerator / denominator - center)
    return result


def temporal_scalar(signal, raw, white, config, records, events):
    result = np.empty_like(signal)
    reasons = np.zeros(raw.shape, dtype=np.uint8)
    for index, frame in enumerate(records):
        entry = events["frames"][index]
        for row in range(raw.shape[1]):
            for col in range(raw.shape[2]):
                bits = 1 if index == 0 else 0
                if index:
                    old, old_event = records[index - 1], events["frames"][index - 1]
                    settings = ("exposure_ns", "gain")
                    if any(frame["applied"][key] != old["applied"][key] for key in settings):
                        bits |= 2
                    if [v - frame["frame_start_ns"] for v in entry["row_open_ns"]] != [
                        v - old["frame_start_ns"] for v in old_event["row_open_ns"]
                    ]:
                        bits |= 2
                    interval = entry["row_transfer_ns"][row] - old_event["row_transfer_ns"][row]
                    if interval > config["max_inter_transfer_interval_ns"]:
                        bits |= 8
                    if int(raw[index - 1, row, col]) in (0, white):
                        bits |= 32
                if entry["clear_before_exposure"]:
                    bits |= 4
                if int(raw[index, row, col]) in (0, white):
                    bits |= 16
                value = signal[index, row, col]
                if not bits and abs(value - result[index - 1, row, col]) <= config["threshold_dn"]:
                    value = (1 - config["previous_weight"]) * value + config[
                        "previous_weight"
                    ] * result[index - 1, row, col]
                result[index, row, col] = value
                reasons[index, row, col] = bits
    return result, reasons


@pytest.mark.parametrize(
    "values,expected",
    [
        ([50, 100, 150, 200], [50, 75, 112.5, 156.25]),
        ([0, 0, 1000, 0, 0], [0, 0, 500, 250, 125]),
    ],
)
def test_independent_ramp_and_pulse_show_algorithm_delay_not_retention_correction(values, expected):
    raw = np.broadcast_to(
        np.array(values, dtype=np.int32)[:, None, None] + 64, (len(values), 6, 8)
    ).copy()
    p = profile()
    result = retained_isp.process_raw(raw, format_12bit(), p, **metadata(raw.shape))
    np.testing.assert_array_equal(result["arrays"]["temporal_dn_frames"][:, 0, 0], expected)
    assert not np.array_equal(values, expected)
    assert (
        result["summary"]["temporal_state_role"] == "causal_algorithm_state_not_photodiode_charge"
    )
    assert result["summary"]["charge_inversion"] is False


def test_scalar_site_reflection_and_code_rail_exclusion_at_every_boundary():
    raw = np.random.default_rng(41).integers(1, 4095, (2, 6, 8), dtype=np.int32)
    raw[0, 0, 0], raw[0, 1, 1], raw[1, -1, -1] = 0, 4095, 4095
    p = retained_isp.default_profile()
    p["spatial_filter"].update(
        mode="same_site_bilateral_code_v1", strength=0.7, range_sigma_dn=1100
    )
    expected = spatial_scalar(raw, 64, 4095, 0.7, 1100)
    result = retained_isp.process_raw(raw, format_12bit(), p, **metadata(raw.shape))
    np.testing.assert_allclose(result["arrays"]["spatial_dn_frames"], expected, atol=2e-12, rtol=0)
    assert result["arrays"]["spatial_dn_frames"][0, 0, 0] == -64
    # A perturbation of Gr cannot contaminate any R/Gb/B spatial sample.
    changed = raw.copy()
    changed[:, 0::2, 1::2] = 3333
    again = retained_isp.process_raw(changed, format_12bit(), p, **metadata(raw.shape))
    for row, col in ((0, 0), (1, 0), (1, 1)):
        np.testing.assert_array_equal(
            result["arrays"]["spatial_dn_frames"][:, row::2, col::2],
            again["arrays"]["spatial_dn_frames"][:, row::2, col::2],
        )


def test_scalar_exact_ns_gap_rails_and_explicit_pd_clear_reset_only_algorithm_state():
    raw = np.broadcast_to(
        np.array([100, 200, 300, 400, 500], np.int32)[:, None, None] + 64, (5, 6, 8)
    ).copy()
    raw[1, 0, 0], raw[1, 1, 1] = 0, 4095
    origin = 2**52
    data = metadata(
        raw.shape,
        starts=[origin + v for v in (0, 10_000, 20_001, 30_001, 40_001)],
        clear=[False, False, False, True, False],
    )
    p = profile()
    p["temporal_filter"]["max_inter_transfer_interval_ns"] = 10_000
    expected, reasons = temporal_scalar(
        raw - 64.0, raw, 4095, p["temporal_filter"], data["frame_records"], data["events"]
    )
    result = retained_isp.process_raw(raw, format_12bit(), p, **data)["arrays"]
    np.testing.assert_array_equal(result["temporal_dn_frames"], expected)
    np.testing.assert_array_equal(result["temporal_reset_reason_frames"], reasons)
    np.testing.assert_array_equal(
        result["temporal_inter_transfer_interval_ns"][:, 0], [0, 10_000, 10_001, 10_000, 10_000]
    )
    assert reasons[2, 0, 0] == 8 | 32
    assert reasons[2, 2, 2] == 8
    assert reasons[3, 2, 2] == 4
    assert not reasons[4].any()


@pytest.mark.parametrize(
    "family,path",
    [("linear", "single"), ("nonlinear", "single"), ("paired", "HCG"), ("paired_nonlinear", "LCG")],
)
def test_actual_registered_source_history_flows_to_scalar_referenced_code_dsp(
    workflow, family, path
):
    service, run = complete(workflow, family)
    source = retained_isp_source.resolve(service, run["id"], selector(path))
    raw = retained_isp_source.load_inputs(service, source)
    original = raw.copy()
    p = profile()
    p["spatial_filter"].update(mode="same_site_bilateral_code_v1", strength=0.4, range_sigma_dn=31)
    fmt = source["raw_format"]
    white, black = 2 ** fmt["bit_depth"] - 1, fmt["black_level_dn"]
    spatial = spatial_scalar(raw, black, white, 0.4, 31)
    expected, reasons = temporal_scalar(
        spatial,
        raw,
        white,
        p["temporal_filter"],
        source["frame_records"],
        source["retained_charge_events"],
    )
    result = retained_isp.process_raw(
        raw,
        fmt,
        p,
        geometry=source["geometry"],
        frame_records=source["frame_records"],
        events=source["retained_charge_events"],
    )
    np.testing.assert_allclose(result["arrays"]["spatial_dn_frames"], spatial, atol=2e-12, rtol=0)
    np.testing.assert_allclose(result["arrays"]["temporal_dn_frames"], expected, atol=2e-12, rtol=0)
    np.testing.assert_array_equal(result["arrays"]["temporal_reset_reason_frames"], reasons)
    np.testing.assert_array_equal(raw, original)
    assert result["summary"]["raw_format"] == fmt
    assert result["summary"]["source_truth_arrays_used"] is False
    assert result["summary"]["acquisition_response_type_preserved"] == fmt["response_type"]
    assert result["arrays"]["temporal_gate_mask_frames"][1:].any()
    assert not np.array_equal(result["arrays"]["temporal_dn_frames"], spatial)
    assert (
        sum(a.nbytes for a in result["arrays"].values())
        <= result["summary"]["resources"]["output_array_bytes_bound"]
    )


@pytest.mark.parametrize("tone", ["linear", "srgb"])
def test_bypass_is_bitwise_original_code_display_with_nontrivial_color_and_tone(tone):
    raw = np.random.default_rng(431).integers(0, 4096, (4, 6, 8), dtype=np.int32)
    common = isp.default_profile()
    common["white_balance"]["gains_rgb"] = [1.4, 0.9, 1.15]
    common["ccm"]["matrix_rgb"] = [[1.1, -0.1, 0], [0.03, 0.94, 0.03], [-0.02, -0.08, 1.1]]
    common["tone"]["curve"] = tone
    data = metadata(raw.shape)
    expected = isp.process_raw(raw, format_12bit(), common, geometry=data["geometry"])
    actual = retained_isp.process_raw(
        raw, format_12bit(), retained_isp.default_profile(common), **data
    )
    np.testing.assert_array_equal(actual["arrays"]["rgb_frames"], expected["arrays"]["rgb_frames"])
    nonlinear = deepcopy(format_12bit())
    nonlinear["response_type"] = "nonlinear_charge_voltage"
    codes = retained_isp.process_raw(raw, nonlinear, retained_isp.default_profile(common), **data)
    np.testing.assert_array_equal(codes["arrays"]["rgb_frames"], actual["arrays"]["rgb_frames"])
    assert codes["summary"]["raw_format"]["response_type"] == "nonlinear_charge_voltage"
