"""Bounded RAW HDR reconstruction from one saved Lens x Sensor PWM study.

The input is the original two-exposure, four-phase integer RAW bracket and its
row timing.  The target is the photoelectron rate under the authored PWM ON
luminance.  This is a model-internal calculation for a static scene and ideal
rectangular illumination, not a product LFM or measured HDR mode.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import numpy as np

from pyisetcam.sensor_capture import _fixed_pattern_components, _readout_values

from . import hdr
from .lens_sensor_lfm import BOUNDED_SCHEMA as BOUNDED_LFM_SCHEMA
from .lens_sensor_lfm import LINE_TIME_S, PHASE_FRACTIONS
from .lens_sensor_lfm import SCHEMA as LFM_SCHEMA
from .lens_sensor_lfm import TIMED_BOUNDED_SCHEMA as TIMED_BOUNDED_LFM_SCHEMA
from .lens_sensor_timing_mode import timing_provenance as mode_provenance
from .lens_sensor_timing_mode import validate_timing_mode
from .registry import digest
from .saved_sensor_lfm import _on_fraction

SCHEMA = "camerae2e_lens_sensor_pwm_hdr_v1"
MAX_PIXELS = 2 * 4 * 32 * 32
FPN_FIELDS = {"prnu_fraction", "dsnu_e", "column_prnu_fraction", "column_dsnu_e"}
NOISE_SEED_POLICY = (
    "same seed for four phases and the constant-light reference "
    "within each exposure; paired sensitivity, not independent repeats"
)
TIMING_PROVENANCE = "authored 20 us/line, not saved sensor timing"


def _hash(array: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(array).tobytes(order="C")).hexdigest()


def _float(value: Any, label: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{label} must be a finite positive number.")
    return result


def _array(value: Any, shape: tuple[int, ...], label: str) -> np.ndarray:
    array = np.asarray(value)
    if array.shape != shape or array.dtype.kind not in "iuf":
        raise ValueError(f"{label} must have shape {shape} and numeric values.")
    result = array.astype(np.float64)
    if not np.isfinite(result).all():
        raise ValueError(f"{label} must be finite.")
    return result


def _model_fpn(
    report: Mapping[str, Any], shape: tuple[int, int], conversion_gain_v_per_e: float
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Regenerate the virtual-window maps using the capture's seed and algorithm.

    The LFM artifact does not store map bytes.  This is an exact deterministic
    model reconstruction only when its frozen capture version, four saved FPN
    parameters and seed match; it is not an independently measured calibration.
    """
    controls = report["controls"]
    assumptions = controls.get("saved_fpn_parameter_assumptions")
    if not isinstance(assumptions, Mapping) or set(assumptions) != FPN_FIELDS:
        raise ValueError("PWM HDR requires the four saved FPN parameters.")
    parameters = {key: _float(assumptions[key], key) for key in FPN_FIELDS}
    if any(value < 0 for value in parameters.values()):
        raise ValueError("Saved FPN sigmas must be nonnegative.")
    seed = controls.get("fixed_pattern_seed")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("PWM HDR requires a uint32 fixed-pattern seed.")
    conversion = _float(conversion_gain_v_per_e, "conversion_gain_v_per_e", positive=True)
    # _fixed_pattern_components is the same source routine used during the
    # original digital-RAW capture.  It creates the maps at virtual origin zero.
    sensor = SimpleNamespace(
        fields={
            "pixel": {
                "conversion_gain_v_per_electron": conversion,
                "dsnu_sigma_v": parameters["dsnu_e"] * conversion,
                "prnu_sigma": parameters["prnu_fraction"],
            },
            "column_fpn": [
                parameters["column_dsnu_e"] * conversion,
                parameters["column_prnu_fraction"],
            ],
            "fixed_pattern_seed": seed,
        }
    )
    gain, bias = _fixed_pattern_components(sensor, shape)
    gain = np.broadcast_to(np.asarray(gain, dtype=np.float64), shape).copy()
    bias = np.broadcast_to(np.asarray(bias, dtype=np.float64), shape).copy()
    if not np.isfinite(gain).all() or np.any(gain <= 0) or not np.isfinite(bias).all():
        raise ValueError("Saved FPN model generates noninvertible gain or bias.")
    return (
        gain,
        bias,
        {
            "mode": "saved_model_generated_virtual_window_fpn",
            "source": "pyisetcam.sensor_capture._fixed_pattern_components",
            "seed": seed,
            "saved_parameters": parameters,
            "conversion_gain_v_per_e": conversion,
            "response_gain_sha256": _hash(gain.astype("<f8")),
            "post_well_bias_e_sha256": _hash(bias.astype("<f8")),
            "calibration_independence": False,
            "physical_validation": "not_established",
        },
    )


def _validate_source(
    raw: np.ndarray, report: Mapping[str, Any]
) -> tuple[np.ndarray, np.ndarray, list[list[dict]]]:
    if not isinstance(report, Mapping) or report.get("schema") not in {
        LFM_SCHEMA, BOUNDED_LFM_SCHEMA, TIMED_BOUNDED_LFM_SCHEMA
    }:
        raise ValueError("PWM HDR requires a saved Lens x Sensor LFM report.")
    if report["schema"] in {BOUNDED_LFM_SCHEMA, TIMED_BOUNDED_LFM_SCHEMA}:
        preflight = report.get("resource_preflight")
        execution = report.get("psf_execution")
        if (
            report.get("psf_compute_mode") != "bounded_streaming"
            or not isinstance(preflight, Mapping)
            or preflight.get("scope") != "central_bounded_strict_raytrace"
            or preflight.get("status") != "within_development_budget"
            or preflight.get("execution_guaranteed") is not False
            or not isinstance(execution, Mapping)
            or execution.get("mode") != "strict_bounded_streaming"
            or execution.get("complete_precomputed_stack") is not False
            or type(execution.get("peak_cached_kernels")) is not int
            or not 0 <= execution["peak_cached_kernels"] <= 4
        ):
            raise ValueError("PWM HDR requires intact bounded PSF execution evidence.")
    if (
        report.get("status") != "completed"
        or report.get("linearity_check", {}).get("status") != "passed"
    ):
        raise ValueError("PWM HDR requires a completed exposure-linear LFM capture.")
    boundary = report.get("source_boundary") or {}
    if (
        boundary.get("saved_sensor_simulation_run_used") is not True
        or boundary.get("saved_fpn_parameters_applied_to_generated_virtual_window_map") is not True
        or boundary.get("physical_validation") != "not_established"
    ):
        raise ValueError("PWM HDR requires the pinned saved model and generated FPN contract.")
    scene = report.get("scene_contract") or {}
    if scene.get("spatial_motion") is not False or scene.get("scene_kind") != "uniform_d65":
        raise ValueError("PWM HDR supports one static authored D65 scene only.")
    source_geometry = report.get("geometry_provenance") or {}
    cfa = source_geometry.get("saved_native_cfa") or {}
    if (
        cfa.get("pattern") != [[1, 2], [2, 3]]
        or cfa.get("filter_names") != ["red", "green", "blue"]
        or cfa.get("pattern_index_base") != 1
    ):
        raise ValueError("PWM HDR requires exact native 2x2 RGGB CFA.")
    if (
        not isinstance(raw, np.ndarray)
        or raw.dtype != np.dtype("int32")
        or raw.ndim != 4
        or raw.shape[:2] != (2, 4)
        or any(size < 8 or size > 32 or size % 2 for size in raw.shape[2:])
        or raw.size > MAX_PIXELS
    ):
        raise ValueError("PWM HDR requires original int32 RAW [2,4,even H,even W].")
    rows, cols = raw.shape[2:]
    if (
        source_geometry.get("configured_sensor_size") != [rows, cols]
        or source_geometry.get("configured_sensor_scope") != "virtual_simulation_window_only"
        or source_geometry.get("saved_native_array_crop_origin") is not None
        or source_geometry.get("saved_native_array_crop_mapping") != "not_established"
        or source_geometry.get("cfa_phase_basis")
        != "saved_native_cfa_tile_anchored_at_virtual_origin_zero"
        or source_geometry.get("saved_native_array_size") != cfa.get("native_size")
    ):
        raise ValueError(
            "PWM HDR requires the saved virtual-window geometry and unknown native crop."
        )
    controls = report.get("controls") or {}
    if controls.get("sensor_size") != [rows, cols]:
        raise ValueError("PWM HDR RAW differs from the virtual sensor window.")
    exposures = _array(controls.get("exposure_s"), (2,), "exposure_s")
    if exposures[0] <= 0 or exposures[0] >= exposures[1]:
        raise ValueError("PWM HDR requires two ascending positive exposures.")
    period = _float(controls.get("pwm_period_s"), "pwm_period_s", positive=True)
    duty = _float(controls.get("duty_cycle"), "duty_cycle", positive=True)
    low = _float(controls.get("off_to_on_ratio"), "off_to_on_ratio")
    line_time = _float(controls.get("line_time_s"), "line_time_s", positive=True)
    if not 0 < duty < 1 or not 0 <= low <= 0.5:
        raise ValueError("PWM HDR source waveform is outside its authored domain.")
    timed_mode = report["schema"] == TIMED_BOUNDED_LFM_SCHEMA
    if timed_mode:
        mode = validate_timing_mode(controls.get("timing_mode"))
        selected_sha = controls.get("timing_mode_sha256")
        selected_canonical = controls.get("timing_mode_canonical_sha256")
        source_kind = mode["provenance"]["source_kind"]
        binding = mode["binding"]
        if (
            not isinstance(selected_sha, str) or len(selected_sha) != 64
            or any(character not in "0123456789abcdef" for character in selected_sha)
            or selected_canonical != digest(mode)
            or binding["sensor_revision"] != report.get("sensor_revision")
            or binding["saved_readout_sha256"] != digest(controls.get("saved_readout"))
            or binding["readout_profile"] != boundary.get("sensor_readout_profile")
            or binding["readout_classification"]
            != boundary.get("sensor_readout_classification")
            or binding["virtual_sensor_size"] != [rows, cols]
            or binding["exposure_s"] != controls.get("exposure_s")
            or line_time != mode["timing"]["line_time_s"]
            or controls.get("timing_provenance") != mode_provenance(mode)
            or boundary.get("timing_mode_source_kind") != source_kind
            or boundary.get("timing_mode_source_document_sha256")
            != mode["provenance"]["source_document_sha256"]
            or boundary.get("timing_value_verification")
            != "declared_not_independently_verified"
        ):
            raise ValueError("PWM HDR timing mode differs from its frozen Sensor source.")
    elif line_time != LINE_TIME_S or controls.get("timing_provenance") != TIMING_PROVENANCE:
        raise ValueError("PWM HDR requires the frozen authored row timing and seed policy.")
    if controls.get("noise_seed_policy") != NOISE_SEED_POLICY:
        raise ValueError("PWM HDR requires the frozen authored row timing and seed policy.")
    source_seed = controls.get("seed")
    if type(source_seed) is not int or not 0 <= source_seed < 2**32:
        raise ValueError("PWM HDR requires the original uint32 source seed.")
    if controls.get("phase_fractions_of_period") != list(PHASE_FRACTIONS):
        raise ValueError("PWM HDR requires the four frozen LFM phase offsets.")
    readout = controls.get("saved_readout")
    if not isinstance(readout, Mapping):
        raise ValueError("PWM HDR requires a saved digital readout.")
    try:
        normalized_readout = _readout_values(readout)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("PWM HDR saved digital readout is invalid.") from exc
    if dict(readout) != normalized_readout:
        raise ValueError("PWM HDR saved digital readout differs from the capture contract.")
    white_expected = 2 ** normalized_readout["bit_depth"] - 1
    dn_per_e_expected = (
        (white_expected - normalized_readout["black_level_dn"])
        * normalized_readout["gain"]
        / normalized_readout["full_well_e"]
    )
    cases = report.get("cases")
    if not isinstance(cases, list) or len(cases) != 2:
        raise ValueError("PWM HDR requires two complete exposure cases.")
    on_time = np.empty((2, 4, rows), dtype=np.float64)
    phases: list[list[dict]] = []
    starts_expected = np.arange(rows, dtype=np.float64) * line_time
    for i, case in enumerate(cases):
        frame_seed = int(np.random.SeedSequence([source_seed, i]).generate_state(1)[0])
        if (
            case.get("exposure_s") != float(exposures[i])
            or not isinstance(case.get("phases"), list)
            or len(case["phases"]) != 4
        ):
            raise ValueError("PWM HDR source exposure case differs from saved controls.")
        phase_rows = []
        for j, phase in enumerate(case["phases"]):
            timing = phase.get("row_timing") or {}
            fraction = PHASE_FRACTIONS[j]
            if (
                phase.get("phase_fraction_of_period") != fraction
                or phase.get("phase_s") != fraction * period
                or phase.get("raw_sha256") != _hash(raw[i, j])
                or timing.get("schema") != (
                    "camerae2e_analytic_pwm_row_timing_v2"
                    if timed_mode else "camerae2e_analytic_pwm_row_timing_v1"
                )
                or timing.get("rolling_shutter") is not True
                or timing.get("method") != "exact_piecewise_constant_row_overlap"
                or timing.get("frame_start_s") != 0.0
                or timing.get("line_time_s") != line_time
                or (
                    timed_mode
                    and timing.get("timing_mode_sha256") != selected_sha
                )
            ):
                raise ValueError("PWM HDR original RAW hash or phase timing differs.")
            starts = _array(timing.get("row_exposure_start_s"), (rows,), "row starts")
            ends = _array(timing.get("row_exposure_end_s"), (rows,), "row ends")
            on_fraction = _array(
                timing.get("ideal_pwm_on_fraction_per_row"), (rows,), "PWM on fraction"
            )
            computed = _on_fraction(
                starts_expected, float(exposures[i]), period, duty, fraction * period
            )
            if (
                not np.array_equal(starts, starts_expected)
                or not np.allclose(ends, starts + exposures[i], rtol=0, atol=1e-15)
                or not np.allclose(on_fraction, computed, rtol=0, atol=1e-12)
                or np.any(on_fraction < 0)
                or np.any(on_fraction > 1)
            ):
                raise ValueError(
                    "PWM HDR row windows or ON time do not match the declared waveform."
                )
            capture = phase.get("sensor_capture") or {}
            if (
                capture.get("schema") != "camerae2e_sensor_capture_v1"
                or capture.get("mode") != "digital_raw"
                or capture.get("noise_flag") != 2
                or capture.get("exposure_time_s") != float(exposures[i])
                or capture.get("readout") != readout
                or capture.get("white_level_dn") != white_expected
                or capture.get("black_level_dn") != normalized_readout["black_level_dn"]
                or capture.get("dn_per_electron") != dn_per_e_expected
                or capture.get("fixed_pattern_seed") != controls.get("fixed_pattern_seed")
                or capture.get("fixed_pattern_order")
                != "response_gain_then_well_clip_then_signed_bias_then_read_noise"
                or capture.get("dsnu_definition")
                != "fixed_readout_bias_per_capture_not_dark_current_variation"
                or capture.get("noise_seed") != phase.get("noise_seed")
                or capture.get("noise_seed") != frame_seed
            ):
                raise ValueError("PWM HDR capture calibration or FPN semantics changed.")
            on_time[i, j] = on_fraction * exposures[i]
            phase_rows.append(capture)
        phases.append(phase_rows)
    if (
        any(
            type(item["noise_seed"]) is not int or not 0 <= item["noise_seed"] < 2**32
            for row in phases
            for item in row
        )
        or any(len({item["noise_seed"] for item in row}) != 1 for row in phases)
        or phases[0][0]["noise_seed"] == phases[1][0]["noise_seed"]
    ):
        raise ValueError(
            "PWM HDR requires one phase-paired seed per exposure and distinct bracket seeds."
        )
    return exposures, on_time, phases


def fuse_lens_sensor_pwm_hdr(
    raw: np.ndarray, report: Mapping[str, Any], *, conversion_gain_v_per_e: float
) -> dict[str, Any]:
    """Fuse only paired phase brackets with positive observed PWM ON time.

    A phase/row with zero ON time is not a valid bracket observation, even when
    the authored OFF ratio is nonzero. Two uncensored estimates yield fused HDR,
    whereas one usable bracket yields a separately labeled fallback.
    Original RAW is neither modified nor synthesized from truth maps.
    """
    before = _hash(raw) if isinstance(raw, np.ndarray) else None
    exposures, on_time, captures = _validate_source(raw, report)
    rows, cols = raw.shape[2:]
    gain, bias, fpn = _model_fpn(report, (rows, cols), conversion_gain_v_per_e)
    profile = hdr.validate_profile(hdr.default_profile())
    pattern = np.array([[1, 2], [2, 3]], dtype=np.int8)
    geometry = {
        "kind": "virtual_window",
        "configured_sensor_size": [rows, cols],
        "cfa_tile_origin_in_virtual_window": [0, 0],
        "saved_native_array_crop_origin": None,
        "saved_native_array_crop_mapping": "not_established",
    }
    low = float(report["controls"]["off_to_on_ratio"])
    effective = on_time + low * (exposures[:, None, None] - on_time)
    if not np.isfinite(effective).all() or np.any(effective < 0):
        raise ValueError("PWM HDR effective illumination time is invalid.")
    rates = np.full(raw.shape, np.nan, dtype=np.float64)
    variances = np.full(raw.shape, np.nan, dtype=np.float64)
    valid = np.zeros(raw.shape, dtype=bool)
    frame_diagnostics: list[dict] = []
    calibrations: list[dict] = []
    for i in range(2):
        phase_calibrations = []
        for j in range(4):
            capture = captures[i][j]
            readout = capture["readout"]
            scalar = {
                "dn_per_e": capture["dn_per_electron"],
                "black_level_dn": capture["black_level_dn"],
                "white_level_dn": capture["white_level_dn"],
                "full_well_e": readout["full_well_e"],
                "dark_e": np.full(
                    (rows, cols), float(readout["dark_current_e_per_s"]) * exposures[i]
                ),
                "read_noise_variance_e2": float(readout["read_noise_e"]) ** 2,
                "quantization_variance_e2": 1.0 / (12.0 * float(capture["dn_per_electron"]) ** 2),
                "linear_floor_dn": 0.0,
                "linear_ceiling_dn": min(
                    float(capture["white_level_dn"]),
                    float(capture["black_level_dn"])
                    + float(readout["full_well_e"]) * float(capture["dn_per_electron"]),
                ),
            }
            cal, dark = hdr._calibration(scalar, (rows, cols))
            dn_per_e = cal["dn_per_e"]
            black = cal["black_level_dn"]
            white = cal["white_level_dn"]
            if np.any(raw[i, j] < 0) or np.any(raw[i, j] > white):
                raise ValueError("PWM HDR RAW is outside its saved ADC range.")
            read_var = cal["read_noise_variance_e2"]
            quant_var = cal["quantization_variance_e2"]
            guard = max(
                profile["saturation_margin_dn"],
                profile["read_noise_guard_sigma"] * math.sqrt(read_var) * dn_per_e,
            )
            floor = np.clip(black + dn_per_e * bias, 0.0, white)
            ceiling = np.clip(black + dn_per_e * (cal["full_well_e"] + bias), 0.0, white)
            observed_e = (raw[i, j].astype(np.float64) - black) / dn_per_e
            input_e = (observed_e - bias) / gain
            row_effective = effective[i, j, :, None]
            positive_on = on_time[i, j, :, None] > 0
            positive_effective = row_effective > 0
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                rate = np.divide(
                    input_e - dark,
                    row_effective,
                    out=np.full((rows, cols), np.nan),
                    where=positive_effective,
                )
                variance = np.divide(
                    np.maximum(input_e, 0) + (read_var + quant_var) / gain**2,
                    row_effective**2,
                    out=np.full((rows, cols), np.nan),
                    where=positive_effective,
                )
            censored = (
                (raw[i, j] <= floor + guard)
                | (raw[i, j] >= ceiling - guard)
                | (ceiling - floor <= 2 * guard)
            )
            usable = (
                positive_on
                & positive_effective
                & ~censored
                & np.isfinite(rate)
                & np.isfinite(variance)
                & (variance > 0)
            )
            rates[i, j] = rate
            variances[i, j] = variance
            valid[i, j] = usable
            phase_calibrations.append(
                {
                    **cal,
                    "dark_e_sha256": _hash(np.ascontiguousarray(dark, dtype="<f8")),
                    "response_gain_sha256": fpn["response_gain_sha256"],
                    "post_well_bias_e_sha256": fpn["post_well_bias_e_sha256"],
                }
            )
            frame_diagnostics.append(
                {
                    "exposure_index": i,
                    "phase_index": j,
                    "zero_on_time_rows": int(np.count_nonzero(~positive_on[:, 0])),
                    "censored_pixels": int(np.count_nonzero(censored)),
                    "usable_pixels": int(np.count_nonzero(usable)),
                    "guard_dn": float(guard),
                }
            )
        calibrations.append(phase_calibrations)
    dual_valid = valid[0] & valid[1]
    fallback = valid[0] ^ valid[1]
    reconstructed_valid = dual_valid | fallback
    fallback_source = np.full((4, rows, cols), -1, dtype=np.int8)
    fallback_source[fallback & valid[0]] = 0
    fallback_source[fallback & valid[1]] = 1
    weights = np.zeros(raw.shape, dtype=np.float64)
    minimum = np.minimum(variances[0], variances[1])
    with np.errstate(divide="ignore", invalid="ignore"):
        first = np.divide(minimum, variances[0], out=np.zeros_like(minimum), where=dual_valid)
        second = np.divide(minimum, variances[1], out=np.zeros_like(minimum), where=dual_valid)
        denominator = first + second
        np.divide(first, denominator, out=weights[0], where=dual_valid)
        np.divide(second, denominator, out=weights[1], where=dual_valid)
    safe_rates = np.where(valid, rates, 0.0)
    safe_variances = np.where(valid, variances, 0.0)
    fused = np.sum(weights * safe_rates, axis=0)
    fused_variance = np.sum(weights**2 * safe_variances, axis=0)
    fused[~dual_valid] = np.nan
    fused_variance[~dual_valid] = np.nan
    reconstruction_weights = weights.copy()
    reconstruction_weights[0][fallback & valid[0]] = 1.0
    reconstruction_weights[1][fallback & valid[1]] = 1.0
    reconstructed = np.sum(reconstruction_weights * safe_rates, axis=0)
    reconstructed_variance = np.sum(reconstruction_weights**2 * safe_variances, axis=0)
    reconstructed[~reconstructed_valid] = np.nan
    reconstructed_variance[~reconstructed_valid] = np.nan
    with np.errstate(invalid="ignore"):
        threshold = profile["consistency_sigma"] * np.hypot(
            np.sqrt(variances[0]), np.sqrt(variances[1])
        )
        consistency = dual_valid & (np.abs(rates[0] - rates[1]) <= threshold)
    consistency_unknown = ~dual_valid
    consistency_inconsistent = dual_valid & ~consistency
    linear_rgb, rgb_valid, preview = [], [], []
    for j in range(4):
        linear, rgb_mask, rgb = hdr._rgb(reconstructed[j], reconstructed_valid[j], pattern, profile)
        linear_rgb.append(linear)
        rgb_valid.append(rgb_mask)
        preview.append(rgb)
    if _hash(raw) != before:
        raise RuntimeError("Original PWM RAW changed during HDR fusion.")
    arrays = {
        "original_pwm_raw_dn": raw.copy(),
        "pwm_on_time_s": on_time,
        "effective_illumination_time_s": effective,
        "response_gain": gain,
        "post_well_bias_e": bias,
        "bracket_rate": rates,
        "bracket_estimated_variance": variances,
        "bracket_valid_mask": valid,
        "weights": weights,
        "fused_on_luminance_rate": fused,
        "fused_estimated_variance": fused_variance,
        "fused_valid_mask": dual_valid,
        "single_bracket_fallback_mask": fallback,
        "single_bracket_fallback_source_exposure_index": fallback_source,
        "reconstruction_weights": reconstruction_weights,
        "reconstructed_on_luminance_rate": reconstructed,
        "reconstructed_estimated_variance": reconstructed_variance,
        "reconstructed_valid_mask": reconstructed_valid,
        "temporal_consistency_mask": consistency,
        "temporal_consistency_unknown_mask": consistency_unknown,
        "temporal_inconsistent_mask": consistency_inconsistent,
        "linear_rgb": np.stack(linear_rgb),
        "linear_rgb_valid_mask": np.stack(rgb_valid),
        "rgb": np.stack(preview),
    }
    fused_pixels = int(np.count_nonzero(dual_valid))
    fallback_pixels = int(np.count_nonzero(fallback))
    reconstructed_pixels = fused_pixels + fallback_pixels
    total_pixels = 4 * rows * cols
    report_out = {
        "schema": SCHEMA,
        "status": "completed"
        if fused_pixels > 0 and reconstructed_pixels == total_pixels
        else "inconclusive",
        "completion_scope": (
            "all_pixels_estimable_with_fallback"
            if reconstructed_pixels == total_pixels and fallback_pixels > 0 and fused_pixels > 0
            else "all_pixels_two_bracket_fused"
            if fused_pixels == total_pixels
            else "partial_estimability_or_no_two_bracket_fusion"
        ),
        "scope": (
            "declared_timing_static_rectangular_pwm_model_internal_hdr_reconstruction"
            if report["schema"] == TIMED_BOUNDED_LFM_SCHEMA else
            "authored_static_rectangular_pwm_model_internal_hdr_reconstruction"
        ),
        "source_lfm_schema": report["schema"],
        "source_psf_compute_mode": report.get("psf_compute_mode", "precomputed"),
        "lens_identity": report["lens_identity"],
        "sensor_revision": report["sensor_revision"],
        "upstream_assets": report["upstream_assets"],
        "geometry_provenance": report["geometry_provenance"],
        "virtual_geometry": geometry,
        "original_pwm_raw_sha256": before,
        "original_pwm_raw_shape": list(raw.shape),
        "source_bracket_raw_sha256": [
            [phase["raw_sha256"] for phase in case["phases"]] for case in report["cases"]
        ],
        "source_row_timing": [
            [phase["row_timing"] for phase in case["phases"]] for case in report["cases"]
        ],
        "calibration": calibrations,
        "fpn": fpn,
        "frame_diagnostics": frame_diagnostics,
        "fused_hdr_pixels": fused_pixels,
        "single_bracket_fallback_pixels": fallback_pixels,
        "reconstructed_pixels": reconstructed_pixels,
        "invalid_pixels": total_pixels - reconstructed_pixels,
        "coverage_fraction": reconstructed_pixels / total_pixels,
        "two_bracket_fused_fraction": fused_pixels / total_pixels,
        "single_bracket_fallback_fraction": fallback_pixels / total_pixels,
        "temporal_consistency_eligible_pixels": fused_pixels,
        "temporal_consistent_pixels": int(np.count_nonzero(consistency)),
        "temporal_inconsistent_pixels": int(np.count_nonzero(consistency_inconsistent)),
        "temporal_consistency_unknown_pixels": int(np.count_nonzero(consistency_unknown)),
        "zero_on_time_bracket_rows": int(np.count_nonzero(on_time == 0)),
        "method": "per_row_phase_effective_illumination_inverse_variance_raw",
        "rate_estimator": (
            "((((raw-black)/dn_per_e-bias_e)/fpn_gain)-dark_e)"
            "/effective_illumination_time_s"
        ),
        "variance_estimator": (
            "(max(input_e,0)+(read_var+quant_var)/fpn_gain**2)"
            "/effective_time_s**2"
        ),
        "fusion_policy": (
            "each bracket requires positive PWM ON time; two usable brackets fuse as HDR; "
            "one unusable bracket allows explicitly labeled single-bracket fallback; "
            "no usable bracket remains invalid"
        ),
        "array_sha256": {
            "pwm_on_time_s": _hash(on_time.astype("<f8")),
            "effective_illumination_time_s": _hash(effective.astype("<f8")),
            "bracket_valid_mask": _hash(valid),
            "weights": _hash(weights.astype("<f8")),
            "single_bracket_fallback_mask": _hash(fallback),
            "reconstruction_weights": _hash(reconstruction_weights.astype("<f8")),
        },
        "temporal_consistency_scope": (
            "diagnostic on two-bracket fused pixels only; unknown for fallback/invalid; "
            "no motion registration or deghosting"
        ),
        "waveform": {
            key: report["controls"][key]
            for key in (
                "exposure_s",
                "pwm_period_s",
                "duty_cycle",
                "off_to_on_ratio",
                "phase_fractions_of_period",
                "line_time_s",
                "timing_provenance",
            )
        },
        "array_units": {
            "original_pwm_raw_dn": "DN",
            "pwm_on_time_s": "s per exposure phase row",
            "effective_illumination_time_s": "s of ON-luminance equivalent per exposure phase row",
            "response_gain": "1",
            "post_well_bias_e": "electron",
            "bracket_rate": "electron/pixel/s at authored ON luminance",
            "bracket_estimated_variance": "(electron/pixel/s)^2",
            "fused_on_luminance_rate": "electron/pixel/s at authored ON luminance",
            "fused_estimated_variance": "(electron/pixel/s)^2",
            "reconstructed_on_luminance_rate": "electron/pixel/s at authored ON luminance",
            "reconstructed_estimated_variance": "(electron/pixel/s)^2",
            "weights": "1",
            "reconstruction_weights": "1",
            "linear_rgb": "electron/pixel/s",
            "rgb": "1",
        },
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
        },
        "limitations": [
            "Static D65 scene and ideal rectangular PWM; no motion or spectral shift.",
            "Phase RAW shares one seed per exposure; phases are not independent repeats.",
            "FPN maps are deterministic model reconstructions, not measured calibration.",
            "Saved native-array crop location is unknown; RGGB is anchored at virtual origin zero.",
            "Plug-in variance omits correlated noise and total model uncertainty.",
            "A DN guard cannot prove a sample avoided stochastic well clipping.",
            "Lens throughput, relative illumination, CRA and diffraction remain unvalidated.",
            "This is not vendor LFM, general moving HDR, physical accuracy or product approval.",
        ],
    }
    if report["schema"] == TIMED_BOUNDED_LFM_SCHEMA:
        report_out["waveform"].update(
            timing_mode=report["controls"]["timing_mode"],
            timing_mode_sha256=report["controls"]["timing_mode_sha256"],
            timing_mode_canonical_sha256=report["controls"]["timing_mode_canonical_sha256"],
        )
        report_out["limitations"].append(
            "Timing source bytes are pinned, but their line-time assertion and "
            "unknown native-row phase are not independently verified."
        )
    return {"report": report_out, "arrays": arrays}
