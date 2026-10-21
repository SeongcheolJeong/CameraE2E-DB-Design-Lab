"""RAW statistics and pure, bounded AE/AWB proposals, independent of scheduling.

Feedback uses observed ADC codes only. Expected charge, ideal scenes, and future
frames are deliberately absent from these interfaces.
"""

import math
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

from .control_contracts import CameraControlPolicy


def quantize(value, bounds):
    """Clamp then round to nearest grid point; exact decimal half steps round up."""
    low, high, step = [Decimal(str(bounds[k])) for k in ("minimum", "maximum", "step")]
    requested = Decimal(str(value))
    bounded = min(high, max(low, requested))
    ticks = ((bounded - low) / step).to_integral_value(rounding=ROUND_HALF_UP)
    output = min(high, max(low, low + ticks * step))
    return {
        "value": int(output) if type(bounds["step"]) is int else float(output),
        "clamped": bounded != requested,
        "quantized": output != bounded,
    }


def raw_statistics(raw, cfa, raw_format, policy):
    """Meter a native ROI and reject whole Bayer cells for gray-world statistics."""
    policy = CameraControlPolicy.model_validate(policy).model_dump()
    raw, cfa = np.asarray(raw), np.asarray(cfa)
    if (
        raw.ndim != 2
        or raw.shape != cfa.shape
        or raw.dtype.kind not in "ui"
        or cfa.dtype.kind not in "ui"
        or not np.isin(cfa, [0, 1, 2]).all()
    ):
        raise ValueError("Feedback requires one integer RAW frame and matching native CFA map")
    bits, black = raw_format["bit_depth"], raw_format["black_level_dn"]
    if (
        type(bits) is not int
        or not 1 <= bits <= 32
        or type(black) not in (int, float)
        or not math.isfinite(black)
        or not 0 <= black < 2**bits - 1
        or raw.min() < 0
        or raw.max() > 2**bits - 1
    ):
        raise ValueError("Invalid declared ADC range for control statistics")
    roi = policy["statistics_roi"]
    row, col = roi["origin"]
    height, width = roi["shape"]
    if row + height > raw.shape[0] or col + width > raw.shape[1]:
        raise ValueError("Statistics ROI exceeds native crop")
    codes = raw[row : row + height, col : col + width]
    phase = cfa[row : row + height, col : col + width]
    signal = (codes.astype(float) - black) / (2**bits - 1 - black)
    cells = signal.reshape(height // 2, 2, width // 2, 2).transpose(0, 2, 1, 3).reshape(-1, 4)
    cell_cfa = phase.reshape(height // 2, 2, width // 2, 2).transpose(0, 2, 1, 3).reshape(-1, 4)
    if any(
        not np.all((cell_cfa == channel).sum(axis=1) == count)
        for channel, count in enumerate([1, 2, 1])
    ):
        raise ValueError("Each native statistics cell must contain one R, two G and one B")
    awb = policy["awb"]
    valid = ((cells >= awb["valid_signal_min"]) & (cells <= awb["valid_signal_max"])).all(axis=1)
    count = int(valid.sum())
    sufficient = count >= awb["minimum_valid_cells"]
    means = (
        [float(cells[valid][cell_cfa[valid] == channel].mean()) for channel in range(3)]
        if sufficient
        else None
    )
    return {
        "schema": "raw_control_statistics_v1",
        "reference_plane": "sensor_raw",
        "normalization": "(observed_code - declared_black)/(ADC_white - declared_black)",
        "dark_current_correction": "none",
        "roi": roi,
        "ae": {
            "status": "valid",
            "meter": "green_mean",
            "unit": "1",
            "value": float(signal[phase == 1].mean()),
            "green_samples": int((phase == 1).sum()),
            "adc_saturated_fraction": float((codes == 2**bits - 1).mean()),
        },
        "awb": {
            "status": "valid" if sufficient else "insufficient_statistics",
            "valid_cells": count,
            "total_cells": int(cells.shape[0]),
            "channel_mean_normalized": means,
            "assumption": "gray_world_on_common_valid_bayer_cells",
            "fallback": "hold_previous_gains",
        },
    }


def propose_ae(statistics, state, policy):
    config = policy["ae"]
    if not config["enabled"]:
        return {"status": "disabled", "commanded": None}
    meter = statistics["ae"]["value"]
    if meter <= 0:
        requested_ev, status = None, "probe_dark"
        step_ev = config["max_step_ev"]
    else:
        requested_ev = math.log2(config["target_normalized"]) - math.log2(meter)
        if abs(requested_ev) <= config["deadband_ev"]:
            return {"status": "within_deadband", "requested_ev": requested_ev, "commanded": None}
        step_ev = max(-config["max_step_ev"], min(config["max_step_ev"], requested_ev))
        status = "update"
    product = state["exposure_ns"] * state["gain"] * 2**step_ev
    requested_exposure = product / config["readout_gain"]["minimum"]
    exposure = quantize(requested_exposure, config["exposure_ns"])
    requested_gain = product / exposure["value"]
    gain = quantize(requested_gain, config["readout_gain"])
    candidate = {"exposure_ns": exposure["value"], "gain": gain["value"]}
    unchanged = all(candidate[k] == state[k] for k in candidate)
    return {
        "status": "actuator_grid_hold" if unchanged else status,
        "requested_ev": requested_ev,
        "bounded_step_ev": step_ev,
        "requested": {
            "exposure_gain_product_ns": product,
            "exposure_ns": requested_exposure,
            "gain": requested_gain,
        },
        "commanded": None if unchanged else candidate,
        "quantized_candidate": candidate,
        "actuator_adjustments": {"exposure_ns": exposure, "gain": gain},
    }


def propose_awb(statistics, state, policy, *, green_anchor):
    config = policy["awb"]
    if not config["enabled"]:
        return {"status": "disabled", "commanded": None}
    if statistics["awb"]["status"] != "valid":
        return {"status": "insufficient_statistics", "commanded": None}
    red, green, blue = statistics["awb"]["channel_mean_normalized"]
    desired = [green / red * green_anchor, green_anchor, green / blue * green_anchor]
    ev = [
        math.log2(target / previous)
        for target, previous in zip(desired, state["wb_gains_rgb"], strict=True)
    ]
    if max(abs(v) for v in ev) <= config["deadband_ev"]:
        return {
            "status": "within_deadband",
            "requested": {"wb_gains_rgb": desired},
            "commanded": None,
        }
    bounded = [
        previous * 2 ** max(-config["max_step_ev"], min(config["max_step_ev"], step))
        for previous, step in zip(state["wb_gains_rgb"], ev, strict=True)
    ]
    # Preserve the declared initial green normalization, including non-unit anchors.
    bounded[1] = green_anchor
    adjusted = [quantize(v, config["gains"]) for v in bounded]
    candidate = [v["value"] for v in adjusted]
    unchanged = candidate == state["wb_gains_rgb"]
    return {
        "status": "actuator_grid_hold" if unchanged else "update",
        "requested": {"wb_gains_rgb": desired},
        "requested_ev": ev,
        "bounded_step_gains_rgb": bounded,
        "commanded": None if unchanged else {"wb_gains_rgb": candidate},
        "quantized_candidate": {"wb_gains_rgb": candidate},
        "actuator_adjustments": adjusted,
    }


def default_policy(frame_shape):
    """An explicit authored policy; users must supply/calibrate actual device bounds."""
    return CameraControlPolicy.model_validate(
        {
            "schema": "camera_control_policy_v1",
            "statistics_roi": {
                "reference": "native_crop",
                "origin": [0, 0],
                "shape": list(frame_shape),
            },
            "ae": {
                "algorithm": "green_mean_exposure_priority_v1",
                "enabled": True,
                "target_normalized": 0.18,
                "deadband_ev": 0.03,
                "max_step_ev": 1.0,
                "minimum_delay_frames": 1,
                "exposure_ns": {"minimum": 100_000, "maximum": 8_000_000, "step": 100_000},
                "readout_gain": {"minimum": 1.0, "maximum": 8.0, "step": 0.125},
            },
            "awb": {
                "algorithm": "bayer_cell_gray_world_v1",
                "enabled": True,
                "valid_signal_min": 0.005,
                "valid_signal_max": 0.95,
                "minimum_valid_cells": 1,
                "deadband_ev": 0.03,
                "max_step_ev": 0.5,
                "minimum_delay_frames": 1,
                "gains": {"minimum": 0.0625, "maximum": 16.0, "step": 0.00390625},
            },
            "latency": {
                "kind": "declared_simulation",
                "readout_tail_ns": 0,
                "frame_transport_ns": 0,
                "statistics_ns": 0,
                "controller_ns": 0,
                "command_transport_ns": 0,
                "actuator_ns": 0,
            },
        }
    ).model_dump()
