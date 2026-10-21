"""Independent PWM/row/history numerical checks, not physical-camera validation.

References enumerate integer ON intervals and use an analytical two-wavelength
photon rate. They never call the product waveform integrator, optical-rate
helper, state transition, or delivered-charge reader to calculate expectations.
"""

from copy import deepcopy

import numpy as np
import pytest
from test_native_temporal import native_request as native_request

from camera_platform.retained_charge_runtime import (
    RetainedChargeRuntime,
    default_capture_program,
)

SHAPE = (8, 10)
PERIOD_NS = 1_000_000
ON_NS = 320_000
FRAME_STARTS_NS = (0, 2_370_000, 5_610_000)
EXPOSURES_NS = (730_000, 610_000, PERIOD_NS)
LINE_NS = 137_000
HIDDEN_ROWS = 2
CAPACITY_E = 180
SITE_EQE = np.array([0.2, 0.35, 0.6, 0.8])
TRANSFER = np.array([0.25, 0.5, 0.75, 0.4])


@pytest.fixture
def pwm_request(native_request):
    data = deepcopy(native_request)
    data["noise_enabled"] = False
    data["readout"].update(full_well_e=CAPACITY_E, gain=1)
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(full_sensor_shape=list(SHAPE), crop_origin=[0, 0])
        knot["scene"]["spectral_irradiance"] = np.full((*SHAPE, 2), 1e-4)
    controls = data["control_sequence"]
    controls["mode"].update(shutter="rolling", line_time_ns=LINE_NS, hidden_top_rows=HIDDEN_ROWS)
    frame_template = deepcopy(controls["frames"][0])
    controls["frames"] = []
    for i, (start, exposure) in enumerate(zip(FRAME_STARTS_NS, EXPOSURES_NS, strict=True)):
        frame = deepcopy(frame_template)
        frame.update(frame_id=f"independent-pwm-{i}", frame_start_ns=start)
        frame["applied"].update(applied_at_ns=start, exposure_ns=exposure, gain=1)
        controls["frames"].append(frame)
    program = default_capture_program(
        data["readout"], [frame["frame_id"] for frame in controls["frames"]]
    )
    program["electrical_model"].update(dark_current_e_per_s=0, read_noise_e=0, post_gain_noise_v=0)
    program["retained_charge_model"]["transfer_efficiency"] = TRANSFER.tolist()
    program["waveform"] = {
        "schema": "camera_light_waveform_v1",
        "kind": "pwm",
        "clock_id": controls["clock"]["clock_id"],
        "period_ns": PERIOD_NS,
        "on_time_ns": ON_NS,
        "phase_origin_ns": -170_000,
        "low": 0,
        "high": 1,
    }
    data["capture_program"] = program
    return data


def on_overlap_ns(start, end, phase):
    """Brute explicit overlap of half-open integer ON intervals, for a few cycles."""
    result = 0
    first = (start - phase) // PERIOD_NS - 1
    last = (end - phase) // PERIOD_NS + 1
    for cycle in range(first, last + 1):
        left = phase + cycle * PERIOD_NS
        right = left + ON_NS
        result += max(0, min(end, right) - max(start, left))
    return result


def reference(data):
    """Scalar/row reference from authored irradiance, not any runtime output array."""
    # Delta PSF, normal rays, RI=1, and wavelength-independent four-site EQE.
    # Irradiance already refers to the ideal image plane; no throughput multiplier.
    wavelength_m = np.array([500e-9, 600e-9])
    spectral_photons = 1e-4 * wavelength_m / (6.62607015e-34 * 299792458)
    area_m2 = (data["pixel_pitch_um"] * 1e-6) ** 2
    photons_per_second = 100 * (spectral_photons[0] + spectral_photons[1]) / 2 * area_m2
    rates = photons_per_second * SITE_EQE
    site = 2 * (np.arange(SHAPE[0])[:, None] % 2) + np.arange(SHAPE[1])[None, :] % 2
    eta = TRANSFER[site]
    program = data["capture_program"]
    phase = program["waveform"]["phase_origin_ns"]
    dark = program["electrical_model"]["dark_current_e_per_s"]
    residual = np.zeros(SHAPE)
    previous_ends = None
    result = {
        key: []
        for key in (
            "opens",
            "ends",
            "gaps",
            "on_ns",
            "photo",
            "initial_residual",
            "gap_generated",
            "gap_overflow",
            "exposure_generated",
            "exposure_overflow",
            "pretransfer",
            "delivered",
            "residual",
            "raw",
        )
    }
    for frame in data["control_sequence"]["frames"]:
        exposure = frame["applied"]["exposure_ns"]
        opens = [frame["frame_start_ns"] + (HIDDEN_ROWS + row) * LINE_NS for row in range(SHAPE[0])]
        ends = [start + exposure for start in opens]
        gaps = (
            [0] * SHAPE[0]
            if previous_ends is None
            else [start - prior for start, prior in zip(opens, previous_ends, strict=True)]
        )
        on = [on_overlap_ns(start, end, phase) for start, end in zip(opens, ends, strict=True)]
        photo = rates[site] * (np.asarray(on)[:, None] * 1e-9)
        gap_generated = np.broadcast_to(np.asarray(gaps)[:, None] * 1e-9 * dark, SHAPE)
        # Continuous dark is independent of PWM: it occupies the entire exposure
        # and every gap, while photocharge is admitted only inside row windows.
        exposure_generated = photo + dark * (exposure * 1e-9)
        after_gap = np.minimum(residual + gap_generated, CAPACITY_E)
        gap_overflow = np.maximum(residual + gap_generated - CAPACITY_E, 0)
        exposure_overflow = np.maximum(after_gap + exposure_generated - CAPACITY_E, 0)
        pretransfer = np.minimum(after_gap + exposure_generated, CAPACITY_E)
        delivered = eta * pretransfer
        next_residual = pretransfer - delivered
        # Independent default linear RAW mapping, with Python nearest-even round.
        # All fixture codes lie safely below the ADC/digital clipping limits.
        raw = np.array(
            [round(64 + (4095 - 64) * charge / CAPACITY_E) for charge in delivered.flat],
            dtype=np.int32,
        ).reshape(SHAPE)
        for key, value in {
            "opens": opens,
            "ends": ends,
            "gaps": gaps,
            "on_ns": on,
            "photo": photo,
            "initial_residual": residual,
            "gap_generated": gap_generated,
            "gap_overflow": gap_overflow,
            "exposure_generated": exposure_generated,
            "exposure_overflow": exposure_overflow,
            "pretransfer": pretransfer,
            "delivered": delivered,
            "residual": next_residual,
            "raw": raw,
        }.items():
            result[key].append(value)
        residual, previous_ends = next_residual, ends
    return {key: np.asarray(value) for key, value in result.items()}, rates, site


def checked_run(data):
    expected, rates, site = reference(data)
    output = RetainedChargeRuntime().execute(data)
    arrays, summary = output["arrays"], output["summary"]
    np.testing.assert_allclose(
        arrays["channel_photoelectron_rate_scenes"],
        np.broadcast_to(rates, (1, *SHAPE, 4)),
        rtol=4e-14,
    )
    np.testing.assert_array_equal(arrays["cfa_channel_index"], site)
    np.testing.assert_array_equal(
        arrays["retained_native_site_index"], np.broadcast_to(site, (3, *SHAPE))
    )
    for name, key in (
        ("row_start_ns", "opens"),
        ("row_end_ns", "ends"),
        ("row_gap_ns", "gaps"),
    ):
        np.testing.assert_array_equal(arrays[name], expected[key])
    np.testing.assert_allclose(
        arrays["waveform_row_effective_s"], expected["on_ns"] * 1e-9, rtol=0, atol=1e-18
    )
    np.testing.assert_allclose(arrays["mean_electrons"], expected["photo"], rtol=5e-14, atol=1e-12)
    for name in (
        "initial_residual",
        "gap_generated",
        "gap_overflow",
        "exposure_generated",
        "exposure_overflow",
        "pretransfer",
        "delivered",
        "residual",
    ):
        np.testing.assert_allclose(
            arrays[f"retained_{name}_e"], expected[name], rtol=5e-14, atol=1e-12
        )
    np.testing.assert_array_equal(arrays["raw_groups"][:, 0], expected["raw"])
    for index, event in enumerate(summary["retained_charge"]["events"]["frames"]):
        assert event["row_transfer_ns"] == expected["ends"][index].tolist()
        assert event["row_open_ns"] == expected["opens"][index].tolist()
        assert event["row_clear_ns"] is None
    return output, expected, site


def offset_clock(data, offset):
    for frame in data["control_sequence"]["frames"]:
        frame["frame_start_ns"] += offset
        frame["applied"]["applied_at_ns"] += offset
    for knot in data["scene_sequence"]["knots"]:
        knot["time_ns"] += offset
    data["capture_program"]["waveform"]["phase_origin_ns"] += offset


@pytest.mark.parametrize("clock_offset", [0, 2**50])
def test_pwm_row_phase_changes_actual_raw_and_survives_in_later_whole_period_history(
    pwm_request, clock_offset
):
    offset_clock(pwm_request, clock_offset)
    original, expected, site = checked_run(pwm_request)
    shifted = deepcopy(pwm_request)
    shifted["capture_program"]["waveform"]["phase_origin_ns"] += 390_000
    changed, other, _ = checked_run(shifted)
    a, b = original["arrays"], changed["arrays"]
    assert np.unique(expected["on_ns"][0]).size > 3  # Actual rolling-row PWM banding.
    assert np.any(expected["exposure_overflow"] > 0)  # Clipping happens before transfer.
    assert not np.allclose(expected["photo"][:2], other["photo"][:2])
    assert not np.array_equal(a["raw_groups"][:2], b["raw_groups"][:2])
    # Last exposure is one complete period at every rolling row. Current light
    # is identical; different earlier phase still changes retained delivered RAW.
    np.testing.assert_array_equal(expected["on_ns"][2], ON_NS)
    np.testing.assert_array_equal(other["on_ns"][2], ON_NS)
    np.testing.assert_array_equal(expected["photo"][2], other["photo"][2])
    np.testing.assert_allclose(a["mean_electrons"][2], b["mean_electrons"][2], rtol=5e-14)
    red = site == 0
    assert np.any(np.abs(a["retained_delivered_e"][2, red] - b["retained_delivered_e"][2, red]) > 1)
    assert np.any(a["raw_groups"][2, 0, red] != b["raw_groups"][2, 0, red])
    # A current-exposure-only interpretation would erase the retained contribution.
    assert np.all(expected["delivered"][2, red] > expected["photo"][2, red] * TRANSFER[0])


def test_only_whole_period_exposures_remove_phase_dependence_even_with_retention(pwm_request):
    for frame, periods in zip(pwm_request["control_sequence"]["frames"], [1, 2, 1], strict=True):
        frame["applied"]["exposure_ns"] = periods * PERIOD_NS
    baseline, expected, _ = checked_run(pwm_request)
    shifted = deepcopy(pwm_request)
    shifted["capture_program"]["waveform"]["phase_origin_ns"] += 390_000
    changed, other, _ = checked_run(shifted)
    np.testing.assert_array_equal(
        expected["on_ns"], np.broadcast_to(np.array([1, 2, 1])[:, None] * ON_NS, (3, SHAPE[0]))
    )
    np.testing.assert_array_equal(expected["delivered"], other["delivered"])
    np.testing.assert_allclose(
        baseline["arrays"]["retained_delivered_e"],
        changed["arrays"]["retained_delivered_e"],
        rtol=5e-14,
    )
    np.testing.assert_array_equal(baseline["arrays"]["raw_groups"], changed["arrays"]["raw_groups"])


@pytest.mark.parametrize("dark_e_s", [0, 1200])
def test_period_aligned_extra_gap_adds_only_continuous_dark_not_unadmitted_pwm_light(
    pwm_request, dark_e_s
):
    pwm_request["capture_program"]["electrical_model"]["dark_current_e_per_s"] = dark_e_s
    baseline, expected, site = checked_run(pwm_request)
    longer = deepcopy(pwm_request)
    extra_gap_ns = 3 * PERIOD_NS
    for frame in longer["control_sequence"]["frames"][1:]:
        frame["frame_start_ns"] += extra_gap_ns
        frame["applied"]["applied_at_ns"] += extra_gap_ns
    changed, other, _ = checked_run(longer)
    a, b = baseline["arrays"], changed["arrays"]
    np.testing.assert_array_equal(expected["photo"], other["photo"])
    np.testing.assert_allclose(a["mean_electrons"], b["mean_electrons"], rtol=5e-14)
    np.testing.assert_array_equal(a["raw_groups"][0], b["raw_groups"][0])
    if dark_e_s == 0:
        np.testing.assert_array_equal(expected["delivered"], other["delivered"])
        np.testing.assert_allclose(a["retained_delivered_e"], b["retained_delivered_e"], rtol=5e-14)
        np.testing.assert_array_equal(a["raw_groups"], b["raw_groups"])
    else:
        # The added gap is only before frame 1; its extra dark persists into
        # frame 2 with one intervening transfer. R sites remain uncensored.
        red = site == 0
        extra_dark = extra_gap_ns * 1e-9 * dark_e_s
        np.testing.assert_allclose(
            b["retained_delivered_e"][1, red] - a["retained_delivered_e"][1, red],
            extra_dark * TRANSFER[0],
            rtol=0,
            atol=1e-12,
        )
        np.testing.assert_allclose(
            b["retained_delivered_e"][2, red] - a["retained_delivered_e"][2, red],
            extra_dark * (1 - TRANSFER[0]) * TRANSFER[0],
            rtol=0,
            atol=1e-12,
        )
        assert np.all(b["raw_groups"][1:, 0, red] > a["raw_groups"][1:, 0, red])
