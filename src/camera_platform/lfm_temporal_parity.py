"""Read-only PWM row-integration cross-check against pyisetcam's time engine.

The existing joint LFM study analytically integrates an authored rectangular
waveform and then uses the saved sensor readout.  This diagnostic independently
integrates the same optical response with ``sensor_compute_temporal`` at two
explicit sample densities.  The time engine interpolates sampled responses,
so rectangular edges are approximated; a comparison is meaningful only at the
reported numerical resolution and tolerance.  No vendor mode is inferred.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.sensor import sensor_compute_noise_free
from pyisetcam.sensor_capture import _require_single_linear_capture
from pyisetcam.temporal import TemporalCaptureConfig, sensor_compute_temporal
from pyisetcam.types import OpticalImage, Sensor

from .periodic_light import MAX_TIME_NS, exact_on_duration_ns

SCHEMA = "camerae2e_lfm_temporal_parity_v1"
MAX_ROWS = 32
MAX_COLUMNS = 32
MAX_SCENE_SAMPLES = 4096
LINEARITY_TOLERANCE = 1e-6
PHASE_SET_SCHEMA = "camerae2e_lfm_four_phase_temporal_parity_v1"
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_NPZ_BYTES = 64 * 1024 * 1024
MAX_NPZ_UNCOMPRESSED_BYTES = 16 * 1024 * 1024


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_TIME_NS:
        raise ValueError(
            f"{name} must be an integer nanosecond value in [{minimum}, {MAX_TIME_NS}]."
        )
    return value


def _electron_mean(sensor: Sensor, optical: OpticalImage) -> np.ndarray:
    # Match sensor_compute_temporal's optical-only path.  Keeping digital_raw
    # enabled here would mix the readout pedestal into the optical comparison.
    optical_sensor = sensor.clone()
    optical_sensor.fields.pop("sensor_output_mode", None)
    optical_sensor.fields.pop("sensor_readout", None)
    optical_sensor.metadata.pop("sensor_capture", None)
    sampled = sensor_compute_noise_free(optical_sensor, optical)
    conversion = float(sampled.fields["pixel"]["conversion_gain_v_per_electron"])
    mean = np.asarray(sampled.data["volts"], dtype=np.float64) / conversion
    if not math.isfinite(conversion) or conversion <= 0 or not np.isfinite(mean).all():
        raise ValueError("Static optical response must produce finite sensor electrons.")
    if np.any(mean < 0):
        raise ValueError("Static optical response produced negative sensor electrons.")
    return mean


def crosscheck_pwm_temporal_capture(
    sensor: Sensor,
    on_optical_image: OpticalImage,
    *,
    period_ns: int,
    on_time_ns: int,
    phase_offset_ns: int,
    line_time_ns: int,
    off_to_on_ratio: float,
    expected_lfm_mean_electrons: np.ndarray | None = None,
    seed: int = 7,
    samples_per_exposure: Sequence[int] = (128, 512),
    maximum_relative_error: float = 0.003,
    allow_numerical_negative_photons: bool = False,
) -> dict[str, Any]:
    """Compare one authored PWM phase and one exposure with the temporal engine.

    ``expected_lfm_mean_electrons`` can be the corresponding array from the
    joint LFM study.  The comparison then checks that study's analytic output
    against an independent integer-time integral before inspecting numerical
    time-engine agreement.  The caller must supply the *same pinned* Sensor
    and ON-state OpticalImage used by that study; this function does not claim
    or reconstruct their identity from a saved report.

    Bounded PSF convolution can leave tiny negative floating-point photon
    samples.  Explicitly opting in to their in-memory clipping is allowed only
    when their magnitude is negligible and the sensor-plane photocharge is
    unchanged within the stated numerical bound.  The original OI is untouched.
    """
    if not isinstance(sensor, Sensor) or not isinstance(on_optical_image, OpticalImage):
        raise TypeError("Cross-check requires a pyisetcam Sensor and OpticalImage.")
    if sensor.fields.get("sensor_output_mode") != "digital_raw":
        raise ValueError("Cross-check requires the selected digital_raw sensor readout.")
    exposure_s = _require_single_linear_capture(sensor)
    if exposure_s <= 0:
        raise ValueError("Cross-check requires a positive scalar exposure.")
    exposure_ns = round(exposure_s * 1e9)
    if abs(exposure_s * 1e9 - exposure_ns) > 1e-6:
        raise ValueError("Sensor exposure must resolve to an integer nanosecond.")
    exposure_ns = _integer(exposure_ns, "sensor exposure_ns", minimum=1)
    period_ns = _integer(period_ns, "period_ns", minimum=1)
    on_time_ns = _integer(on_time_ns, "on_time_ns")
    phase_offset_ns = _integer(phase_offset_ns, "phase_offset_ns")
    line_time_ns = _integer(line_time_ns, "line_time_ns")
    if not 0 < on_time_ns < period_ns:
        raise ValueError("PWM on_time_ns must be strictly inside the period.")
    if type(off_to_on_ratio) not in (int, float) or not math.isfinite(off_to_on_ratio):
        raise ValueError("off_to_on_ratio must be a finite number in [0, 1].")
    ratio = float(off_to_on_ratio)
    if not 0 <= ratio < 1:
        raise ValueError("off_to_on_ratio must be in [0, 1).")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    if (
        not isinstance(samples_per_exposure, Sequence)
        or isinstance(samples_per_exposure, (str, bytes))
        or len(samples_per_exposure) != 2
        or any(type(value) is not int or value < 2 for value in samples_per_exposure)
        or samples_per_exposure[0] >= samples_per_exposure[1]
    ):
        raise ValueError("samples_per_exposure requires two ascending integers >= 2.")
    if type(maximum_relative_error) not in (int, float) or (
        not math.isfinite(maximum_relative_error) or not 0 < maximum_relative_error < 1
    ):
        raise ValueError("maximum_relative_error must be finite and in (0, 1).")
    rows, columns = tuple(sensor.fields["size"])
    if not (1 <= rows <= MAX_ROWS and 1 <= columns <= MAX_COLUMNS):
        raise ValueError("Cross-check is bounded to at most 32 x 32 sensor pixels.")
    if (rows - 1) * line_time_ns + exposure_ns > MAX_TIME_NS:
        raise ValueError("Row exposure exceeds the exact integer-time domain.")
    # The time engine samples the entire rolling span, not just one exposure.
    span_ratio = ((rows - 1) * line_time_ns + exposure_ns) / exposure_ns
    if math.ceil(span_ratio * samples_per_exposure[1]) + 1 > MAX_SCENE_SAMPLES:
        raise ValueError("Fine temporal sampling exceeds the explicit 4096-sample budget.")

    waveform = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": "authored_pwm_crosscheck", "period_ns": period_ns,
        "on_time_ns": on_time_ns, "phase_origin_ns": -phase_offset_ns,
        "low": ratio, "high": 1.0,
    }
    starts = [row * line_time_ns for row in range(rows)]
    fractions = np.array([
        exact_on_duration_ns(waveform, start, start + exposure_ns) / exposure_ns
        for start in starts
    ], dtype=np.float64)
    photons = np.asarray(on_optical_image.data.get("photons"), dtype=np.float64)
    if not np.isfinite(photons).all() or not photons.size:
        raise ValueError("ON optical photon rates must be finite and nonempty.")
    if type(allow_numerical_negative_photons) is not bool:
        raise ValueError("allow_numerical_negative_photons must be a boolean.")
    negative_count = int(np.count_nonzero(photons < 0))
    correction = {
        "applied": False, "negative_sample_count": negative_count,
        "minimum_original_photons": float(np.min(photons)),
        "max_relative_negative_magnitude": 0.0,
        "maximum_relative_charge_change": 0.0,
    }
    if negative_count:
        if not allow_numerical_negative_photons:
            raise ValueError(
                "ON optical photons contain negative values; explicit bounded "
                "numerical-clipping opt-in is required."
            )
        positive_max = float(np.max(photons))
        original_min = float(np.min(photons))
        negative_ratio = abs(original_min) / max(positive_max, 1e-30)
        if positive_max <= 0 or negative_ratio > 1e-12:
            raise ValueError("Negative optical photons exceed the numerical-roundoff bound.")
        clipped_optical = on_optical_image.clone()
        photons = np.maximum(photons, 0)
        clipped_optical.data["photons"] = photons
        before = _electron_mean(sensor, on_optical_image)
        after = _electron_mean(sensor, clipped_optical)
        charge_change = float(
            np.max(np.abs(before - after)) / max(float(np.max(np.abs(before))), 1e-30)
        )
        if charge_change > 1e-10:
            raise ValueError("Clipping negative optical photons changes sensor photocharge.")
        on_optical_image = clipped_optical
        correction = {
            "applied": True, "negative_sample_count": negative_count,
            "minimum_original_photons": original_min,
            "max_relative_negative_magnitude": negative_ratio,
            "maximum_relative_charge_change": charge_change,
        }
    off_optical = on_optical_image.clone()
    off_optical.data["photons"] = photons * ratio
    on_mean = _electron_mean(sensor, on_optical_image)
    off_mean = _electron_mean(sensor, off_optical)
    if on_mean.shape != (rows, columns) or off_mean.shape != (rows, columns):
        raise ValueError("Static sensor response differs from the selected sensor size.")
    scale = float(np.max(on_mean))
    if scale <= 0:
        raise ValueError("ON optical response has no photoelectron signal to compare.")
    linearity_error = float(np.max(np.abs(off_mean - on_mean * ratio)) / scale)
    exact_mean = off_mean + (on_mean - off_mean) * fractions[:, None]
    saved_error = None
    if expected_lfm_mean_electrons is not None:
        saved = np.asarray(expected_lfm_mean_electrons)
        if saved.shape != exact_mean.shape or saved.dtype.kind not in "iuf":
            raise ValueError("LFM mean array must match the selected sensor shape and be numeric.")
        if not np.isfinite(saved).all() or np.any(saved < 0):
            raise ValueError("LFM mean array must contain finite nonnegative electrons.")
        saved_error = float(np.max(np.abs(saved - exact_mean)) / scale)

    def optical_at(time_s: float) -> OpticalImage:
        phase_ns = (time_s * 1e9 + phase_offset_ns) % period_ns
        level = 1.0 if phase_ns < on_time_ns else ratio
        optical = on_optical_image.clone()
        optical.data["photons"] = photons * level
        return optical

    comparisons = []
    for samples in samples_per_exposure:
        captured = sensor_compute_temporal(
            sensor, optical_at, seed=seed,
            timing=TemporalCaptureConfig(
                frame_start_s=0.0, line_time_s=line_time_ns / 1e9,
                rolling_shutter=True, hidden_lines_top=0,
                samples_per_exposure=samples,
                max_scene_samples=MAX_SCENE_SAMPLES,
            ),
        )
        actual = np.asarray(captured.data["mean_electrons"], dtype=np.float64)
        if actual.shape != exact_mean.shape or not np.isfinite(actual).all():
            raise ValueError("Temporal engine returned an invalid electron array.")
        metadata = captured.metadata["temporal_capture"]
        comparisons.append({
            "samples_per_exposure": samples,
            "scene_sample_count": metadata["sample_count"],
            "max_sample_spacing_s": metadata["max_sample_spacing_s"],
            "maximum_absolute_error_e": float(np.max(np.abs(actual - exact_mean))),
            "maximum_relative_error_to_on_signal": float(
                np.max(np.abs(actual - exact_mean)) / scale
            ),
            "numeric_raw_sha256": hashlib.sha256(
                np.ascontiguousarray(captured.data["raw_dn"]).tobytes(order="C")
            ).hexdigest(),
            "readout_matches_selected_sensor": (
                captured.fields["sensor_readout"] == sensor.fields["sensor_readout"]
            ),
        })

    fine_error = comparisons[-1]["maximum_relative_error_to_on_signal"]
    passed = (
        linearity_error <= LINEARITY_TOLERANCE
        and (saved_error is None or saved_error <= LINEARITY_TOLERANCE)
        and fine_error <= maximum_relative_error
        and all(item["readout_matches_selected_sensor"] for item in comparisons)
    )
    return {
        "schema": SCHEMA,
        "status": "passed_at_declared_numerical_resolution" if passed else "inconclusive",
        "scope": "one authored PWM phase, one static optical response, one rolling exposure",
        "timing": {
            "exposure_ns": exposure_ns, "period_ns": period_ns,
            "on_time_ns": on_time_ns, "phase_offset_ns": phase_offset_ns,
            "line_time_ns": line_time_ns,
            "row_on_fraction_exact_integer_time": fractions.tolist(),
        },
        "off_to_on_ratio": ratio,
        "numerical_optical_correction": correction,
        "on_mean_electrons_max": scale,
        "off_state_linearity_relative_error": linearity_error,
        "saved_lfm_mean_relative_error": saved_error,
        "maximum_accepted_relative_error": float(maximum_relative_error),
        "comparisons": comparisons,
        "numeric_method": "pyisetcam.sensor_compute_temporal; piecewise_linear_sampled_response",
        "exact_reference_method": "camera_platform.periodic_light.exact_on_duration_ns",
        "physical_validation": "not_established",
        "vendor_hdr_lfm_mode_validation": "not_established",
        "limitations": [
            "The selected optical response and PWM timing are caller-authored or "
            "separately pinned; this function does not establish their provenance.",
            "Finite temporal samples approximate rectangular edges; the reported "
            "tolerance applies only to this phase and resolution.",
            "The comparison does not validate motion, native crop/row origin, "
            "vendor capture schedule, illumination waveform, or product HDR/LFM performance.",
        ],
    }


def load_verified_lfm_artifact(
    report_path: str | Path,
    npz_path: str | Path,
    *,
    expected_report_sha256: str,
) -> dict[str, Any]:
    """Read a sealed four-phase LFM source and recheck its original RAW binding.

    The caller supplies the trusted JSON digest.  The report binds the NPZ,
    while the existing HDR source validator binds each original int32 RAW
    phase, row window, readout, seed, and static-scene scope to that report.
    """
    if (
        not isinstance(expected_report_sha256, str)
        or len(expected_report_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_report_sha256)
    ):
        raise ValueError("Expected LFM report SHA-256 must be lowercase hexadecimal.")
    report_file = Path(report_path).expanduser().resolve()
    npz_file = Path(npz_path).expanduser().resolve()
    if report_file.stat().st_size > MAX_REPORT_BYTES or npz_file.stat().st_size > MAX_NPZ_BYTES:
        raise ValueError("Saved LFM artifact exceeds the bounded diagnostic size.")
    report_bytes = report_file.read_bytes()
    report_sha = hashlib.sha256(report_bytes).hexdigest()
    if report_sha != expected_report_sha256:
        raise ValueError("Saved LFM JSON differs from the selected SHA-256.")
    report = json.loads(report_bytes)
    if not isinstance(report, Mapping):
        raise ValueError("Saved LFM report must be a JSON object.")
    artifacts = report.get("artifacts")
    if (
        not isinstance(artifacts, Mapping)
        or artifacts.get("npz_name") != npz_file.name
    ):
        raise ValueError("Saved LFM NPZ path differs from the report binding.")
    npz_bytes = npz_file.read_bytes()
    npz_sha = hashlib.sha256(npz_bytes).hexdigest()
    if artifacts.get("npz_sha256") != npz_sha:
        raise ValueError("Saved LFM NPZ differs from the report SHA-256 binding.")
    required = {
        "original_pwm_raw_dn.npy", "original_constant_raw_dn.npy",
        "integrated_mean_electrons.npy",
    }
    with zipfile.ZipFile(io.BytesIO(npz_bytes)) as archive:
        members = archive.infolist()
        if (
            len(members) != len(required)
            or {item.filename for item in members} != required
            or sum(item.file_size for item in members) > MAX_NPZ_UNCOMPRESSED_BYTES
        ):
            raise ValueError("Saved LFM NPZ has unexpected or oversized members.")
    with np.load(io.BytesIO(npz_bytes), allow_pickle=False) as arrays:
        raw = arrays["original_pwm_raw_dn"]
        constant = arrays["original_constant_raw_dn"]
        means = arrays["integrated_mean_electrons"]
    if (
        raw.dtype != np.dtype("int32")
        or raw.ndim != 4 or raw.shape[:2] != (2, 4)
        or means.dtype != np.dtype("float64") or means.shape != raw.shape
        or constant.dtype != np.dtype("int32") or constant.shape != (2, *raw.shape[2:])
        or not np.isfinite(means).all() or np.any(means < 0)
    ):
        raise ValueError("Saved LFM original RAW or electron arrays have invalid semantics.")
    from .lens_sensor_pwm_hdr import _validate_source

    _validate_source(raw, report)
    for index, case in enumerate(report["cases"]):
        if case.get("constant_raw_sha256") != hashlib.sha256(
            np.ascontiguousarray(constant[index]).tobytes(order="C")
        ).hexdigest():
            raise ValueError("Saved LFM constant RAW differs from its report hash.")
    return {
        "report": report,
        "original_pwm_raw_dn": raw,
        "original_constant_raw_dn": constant,
        "integrated_mean_electrons": means,
        "report_sha256": report_sha,
        "npz_sha256": npz_sha,
        "original_raw_sha256": hashlib.sha256(
            np.ascontiguousarray(raw).tobytes(order="C")
        ).hexdigest(),
        "integrated_mean_sha256": hashlib.sha256(
            np.ascontiguousarray(means).tobytes(order="C")
        ).hexdigest(),
    }


def crosscheck_pwm_four_phase_set(
    sensor: Sensor,
    on_optical_image: OpticalImage,
    *,
    period_ns: int,
    on_time_ns: int,
    line_time_ns: int,
    off_to_on_ratio: float,
    saved_mean_electrons: np.ndarray,
    phase_seeds: Sequence[int],
    original_raw_dn: np.ndarray | None = None,
    samples_per_exposure: Sequence[int] = (128, 512),
    maximum_relative_error: float = 0.003,
    allow_numerical_negative_photons: bool = False,
) -> dict[str, Any]:
    """Check all four fixture phases at one pinned exposure, preserving failures."""
    period_ns = _integer(period_ns, "period_ns", minimum=1)
    if period_ns % 4:
        raise ValueError("Four quarter-period phases require an integer ns offset each.")
    means = np.asarray(saved_mean_electrons)
    if means.shape != (4, *tuple(sensor.fields["size"])):
        raise ValueError("Saved LFM four-phase means must match the selected sensor size.")
    if not isinstance(phase_seeds, Sequence) or len(phase_seeds) != 4:
        raise ValueError("Four original phase seeds are required.")
    if original_raw_dn is not None:
        raw = np.asarray(original_raw_dn)
        if raw.dtype != np.dtype("int32") or raw.shape != means.shape:
            raise ValueError("Four original int32 RAW phases must match saved means.")
    else:
        raw = None
    results = []
    for phase_index in range(4):
        result = crosscheck_pwm_temporal_capture(
            sensor,
            on_optical_image,
            period_ns=period_ns,
            on_time_ns=on_time_ns,
            phase_offset_ns=phase_index * (period_ns // 4),
            line_time_ns=line_time_ns,
            off_to_on_ratio=off_to_on_ratio,
            expected_lfm_mean_electrons=means[phase_index],
            seed=phase_seeds[phase_index],
            samples_per_exposure=samples_per_exposure,
            maximum_relative_error=maximum_relative_error,
            allow_numerical_negative_photons=allow_numerical_negative_photons,
        )
        result["phase_index"] = phase_index
        result["phase_fraction_of_period"] = phase_index / 4
        if raw is not None:
            saved_raw_sha = hashlib.sha256(
                np.ascontiguousarray(raw[phase_index]).tobytes(order="C")
            ).hexdigest()
            result["saved_original_raw_sha256"] = saved_raw_sha
            result["numeric_raw_matches_saved_original"] = [
                sample["numeric_raw_sha256"] == saved_raw_sha
                for sample in result["comparisons"]
            ]
        results.append(result)
    passed = all(item["status"] == "passed_at_declared_numerical_resolution"
                 for item in results)
    return {
        "schema": PHASE_SET_SCHEMA,
        "status": "passed_at_declared_numerical_resolution" if passed else "inconclusive",
        "scope": "four authored quarter-period phases at one saved LFM exposure",
        "phase_count": 4,
        "phases": results,
        "maximum_saved_mean_relative_error": max(
            item["saved_lfm_mean_relative_error"] for item in results
        ),
        "maximum_fine_temporal_relative_error": max(
            item["comparisons"][-1]["maximum_relative_error_to_on_signal"]
            for item in results
        ),
        "physical_validation": "not_established",
        "product_hdr_lfm_decision": "not_established",
        "limitations": [
            "Four authored phases are finite fixture samples, not all possible absolute phases.",
            "The saved RAW is reverified but numerical RAW hash equality is diagnostic only; "
            "sampled rectangular edges and stochastic readout can differ.",
            "No native crop, vendor capture program, moving scene, or physical "
            "validation is inferred.",
        ],
    }
