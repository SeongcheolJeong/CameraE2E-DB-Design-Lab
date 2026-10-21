"""Independent DSP behavior checks; photodiode truth is never an input."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import isp
from camera_platform import retained_isp as code
from camera_platform.registry import digest
from camera_platform.retained_charge_runtime import EVENT_CONTRACT


def inputs(raw, *, starts=None, exposure=None, gains=None, clear=None, offsets=None):
    count, rows, cols = raw.shape
    starts = starts or [i * 8_000_000 for i in range(count)]
    exposure = exposure or [4_000_000] * count
    gains = gains or [1.0] * count
    clear = clear or [False] * count
    offsets = offsets or [[0] * rows for _ in range(count)]
    records, frames = [], []
    for i in range(count):
        opened = [starts[i] + offset for offset in offsets[i]]
        ended = [v + exposure[i] for v in opened]
        records.append(
            {
                "frame_id": f"f{i}",
                "frame_start_ns": starts[i],
                "applied": {"exposure_ns": exposure[i], "gain": gains[i]},
            }
        )
        frames.append(
            {
                "frame_id": f"f{i}",
                "row_open_ns": opened,
                "row_transfer_ns": ended,
                "clear_before_exposure": clear[i],
            }
        )
    events = {
        "schema": "camera_retained_row_events_v1",
        "event_contract": EVENT_CONTRACT,
        "full_sensor_shape": [rows, cols],
        "frames": frames,
    }
    events["schedule_sha256"] = digest(events)
    return {
        "raw_format": {
            "cfa": "RGGB",
            "bit_depth": 12,
            "black_level_dn": 64,
            "response_type": "linear",
            "packing": "unpacked",
            "layout": "bayer",
            "hdr_streams": 1,
        },
        "geometry": {
            "kind": "native_sensor",
            "full_sensor_shape": [rows, cols],
            "crop_origin": [0, 0],
        },
        "frame_records": records,
        "events": events,
    }


def temporal(weight=0.5, threshold=4095):
    p = code.default_profile()
    p["temporal_filter"].update(
        mode="causal_difference_gate_v1", previous_weight=weight, threshold_dn=threshold
    )
    return p


def run(raw, profile=None, **kwargs):
    metadata = inputs(raw, **kwargs)
    return code.process_raw(raw, profile=profile or code.default_profile(), **metadata)


def test_bypass_matches_existing_code_display_without_modifying_acquired_bytes():
    raw = np.random.default_rng(2).integers(0, 4096, (3, 8, 8), dtype=np.int32)
    before = raw.tobytes()
    data = inputs(raw)
    result = code.process_raw(raw, profile=code.default_profile(), **data)
    baseline = isp.process_raw(
        raw, data["raw_format"], isp.default_profile(), geometry=data["geometry"]
    )
    assert np.array_equal(result["arrays"]["rgb_frames"], baseline["arrays"]["rgb_frames"])
    assert raw.tobytes() == before
    assert result["summary"]["charge_inversion"] is False
    assert result["summary"]["source_truth_arrays_used"] is False


def test_nonlinear_response_is_preserved_and_processed_only_as_final_codes():
    raw = np.full((2, 8, 8), 200, dtype=np.int32)
    data = inputs(raw)
    data["raw_format"]["response_type"] = "nonlinear_charge_voltage"
    result = code.process_raw(raw, profile=code.default_profile(), **data)
    assert result["summary"]["raw_format"]["response_type"] == "nonlinear_charge_voltage"
    np.testing.assert_array_equal(result["arrays"]["black_relative_dn_frames"], 136)
    np.testing.assert_allclose(result["arrays"]["pre_tone_code_rgb_frames"], 136 / 4031)


def test_wide_temporal_gate_adds_persistence_instead_of_claiming_lag_recovery():
    raw = np.broadcast_to(
        np.array([464, 264, 164], dtype=np.int32)[:, None, None], (3, 8, 8)
    ).copy()
    result = run(raw, temporal())["arrays"]
    np.testing.assert_array_equal(result["temporal_dn_frames"][:, 0, 0], [400, 300, 200])
    np.testing.assert_array_equal(
        result["temporal_previous_state_dn_frames"][:, 0, 0], [0, 400, 300]
    )
    assert not result["temporal_gate_mask_frames"][0].any()
    assert result["temporal_gate_mask_frames"][1:].all()
    # Input decay 400->200->100; filter produces 400->300->200, more persistence.
    assert result["temporal_dn_frames"][2].mean() > (raw[2] - 64).mean()


def test_gate_equality_and_rejection_are_code_difference_rules():
    raw = np.broadcast_to(
        np.array([164, 144, 164], dtype=np.int32)[:, None, None], (3, 8, 8)
    ).copy()
    result = run(raw, temporal(threshold=20))["arrays"]
    np.testing.assert_array_equal(result["temporal_dn_frames"][:, 0, 0], [100, 90, 95])
    rejected = run(raw, temporal(threshold=19))["arrays"]
    np.testing.assert_array_equal(rejected["temporal_dn_frames"][:, 0, 0], [100, 80, 100])


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"gains": [1, 2, 2]}, "settings"),
        ({"exposure": [4_000_000, 3_000_000, 3_000_000]}, "settings"),
        ({"clear": [False, True, False]}, "pd_clear"),
        ({"starts": [0, 200_000_000, 208_000_000]}, "interval"),
        (
            {"offsets": [[0] * 8, [i * 100 for i in range(8)], [i * 100 for i in range(8)]]},
            "settings",
        ),
    ],
)
def test_control_or_time_discontinuity_seeds_new_algorithm_state(kwargs, reason):
    raw = np.broadcast_to(
        np.array([464, 264, 164], dtype=np.int32)[:, None, None], (3, 8, 8)
    ).copy()
    result = run(raw, temporal(), **kwargs)["arrays"]
    np.testing.assert_array_equal(result["temporal_dn_frames"][:, 0, 0], [400, 200, 150])
    assert np.all(result["temporal_reset_reason_frames"][1] & code.RESET_REASONS[reason])


def test_large_clock_origin_and_unchanged_rolling_offsets_do_not_reset_every_frame():
    raw = np.broadcast_to(
        np.array([464, 264, 164], dtype=np.int32)[:, None, None], (3, 8, 8)
    ).copy()
    offsets = [[r * 100_000 for r in range(8)]] * 3
    result = run(
        raw, temporal(), starts=[2**52 + i * 8_000_000 for i in range(3)], offsets=offsets
    )["arrays"]
    np.testing.assert_array_equal(result["temporal_dn_frames"][:, 0, 0], [400, 300, 200])
    np.testing.assert_array_equal(result["temporal_inter_transfer_interval_ns"][1:], 8_000_000)


def test_final_code_rail_seeds_current_and_next_sample_without_full_well_claim():
    raw = np.full((4, 8, 8), 264, dtype=np.int32)
    raw[1, 0, 0] = 4095
    result = run(raw, temporal())["arrays"]
    assert result["temporal_dn_frames"][1, 0, 0] == 4031
    assert result["temporal_dn_frames"][2, 0, 0] == 200
    assert result["temporal_reset_reason_frames"][2, 0, 0] & 32
    assert not result["temporal_reset_mask_frames"][3].any()


def test_spatial_filter_never_blends_distinct_native_sites():
    tile = np.array([[100, 200], [300, 400]], dtype=np.int32)
    raw = np.tile(tile, (2, 4, 4))
    p = code.default_profile()
    p["spatial_filter"].update(mode="same_site_bilateral_code_v1", strength=1, range_sigma_dn=4000)
    result = run(raw, p)["arrays"]
    np.testing.assert_array_equal(result["spatial_dn_frames"], raw - 64)
    raw[:, 2, 2] = 500
    result = run(raw, p)["arrays"]
    for row, column in ((0, 1), (1, 0), (1, 1)):
        np.testing.assert_array_equal(
            result["spatial_dn_frames"][:, row::2, column::2], raw[:, row::2, column::2] - 64
        )
    assert result["spatial_dn_frames"][0, 2, 2] < 436


def test_zero_strength_graph_preserves_signed_codes_and_same_output():
    raw = np.random.default_rng(3).integers(0, 4096, (3, 8, 8), dtype=np.int32)
    raw[:, 0, 0] = 32
    p = temporal(weight=0)
    p["spatial_filter"].update(mode="same_site_bilateral_code_v1", strength=0)
    p["detail"].update(mode="bounded_code_detail_v1", amount=0)
    result = run(raw, p)
    np.testing.assert_array_equal(result["arrays"]["rgb_frames"], run(raw)["arrays"]["rgb_frames"])
    assert result["arrays"]["black_relative_dn_frames"][0, 0, 0] == -32
    assert result["arrays"]["temporal_dn_frames"][0, 0, 0] == -32


@pytest.mark.parametrize(
    "path,value",
    [
        (("temporal_filter", "previous_weight"), True),
        (("temporal_filter", "previous_weight"), 0.951),
        (("temporal_filter", "max_inter_transfer_interval_ns"), 1.0),
        (("temporal_filter", "threshold_dn"), float("nan")),
        (("temporal_filter", "reset_on_pd_clear"), 1),
        (("spatial_filter", "range_sigma_dn"), 0),
        (("detail", "max_delta"), 1.01),
    ],
)
def test_invalid_profile_is_refused_before_array_execution(path, value):
    p = code.default_profile()
    p[path[0]][path[1]] = value
    with pytest.raises(ValueError):
        code.validate_profile(p)


def test_schedule_and_format_tampering_are_refused():
    raw = np.full((3, 8, 8), 100, dtype=np.int32)
    data = inputs(raw)
    for mutate in (
        lambda d: d["events"]["frames"][1]["row_transfer_ns"].__setitem__(0, 1),
        lambda d: d["frame_records"][1]["applied"].__setitem__("exposure_ns", 5),
        lambda d: d["raw_format"].__setitem__("hdr_streams", True),
        lambda d: d["geometry"].__setitem__("crop_origin", [False, 0]),
    ):
        changed = deepcopy(data)
        mutate(changed)
        with pytest.raises(ValueError):
            code.process_raw(raw, profile=code.default_profile(), **changed)


def test_causality_future_frames_cannot_change_previous_outputs():
    raw = np.random.default_rng(4).integers(65, 2000, (4, 8, 8), dtype=np.int32)
    before = run(raw, temporal())
    raw[-1] = 4000
    after = run(raw, temporal())
    for name in ("temporal_dn_frames", "rgb_frames", "temporal_gate_mask_frames"):
        np.testing.assert_array_equal(before["arrays"][name][:-1], after["arrays"][name][:-1])


def test_resources_bound_actual_serialized_array_payloads():
    raw = np.full((3, 8, 8), 100, dtype=np.int32)
    result = run(raw, temporal())
    assert (
        sum(a.nbytes for a in result["arrays"].values())
        < result["summary"]["resources"]["output_array_bytes_bound"]
    )
