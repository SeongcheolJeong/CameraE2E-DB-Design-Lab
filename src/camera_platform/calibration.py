"""Constrained angular-response calibration candidates, never model approval.

Only wavelength-independent R/B scale factors at existing nonzero sensor angle
nodes can change. Lens response, green response, normal-incidence QE, zero-angle
normalization, irradiation, timing and readout remain fixed. The linear system
uses the same spectral integration and angular/radial interpolation as the
response_db_v1 runtime, including the declared RAW ROI sampling operator.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

import numpy as np
from scipy.optimize import lsq_linear

from .measurement import observation_sampling_weights
from .runtime import LIGHT_M_S, PLANCK_J_S, get_plugin
from .validation import check_domain

SENSOR_EFFECTS = {
    "sensor_effective_qe",
    "sensor_angular_response",
    "mean_dark_charge",
    "readout_clipping",
}
OBSERVATION_EFFECTS = SENSOR_EFFECTS | {
    "lens_relative_illumination",
    "lens_incident_angle_distribution",
}
MAX_CONDITION_NUMBER = 1e8
ALGORITHM = "bounded_angular_scale_v1"


class _RefusalError(ValueError):
    def __init__(self, code: str, message: str, path: str = "observations"):
        self.issue = {"code": code, "message": message, "path": path, "severity": "error"}
        super().__init__(message)


def _require(condition: bool, code: str, message: str, path: str = "observations") -> None:
    if not condition:
        raise _RefusalError(code, message, path)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def _operator_hash(value: Any) -> str:
    """Match the RAW observation contract's canonical JSON encoding exactly."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _array(value: Any, shape: tuple | None, path: str) -> np.ndarray:
    raw = np.asarray(value)
    _require(
        raw.dtype.kind in {"i", "u", "f"},
        "NONNUMERIC_DATA",
        "Numbers must be explicit; strings and booleans are not converted.",
        path,
    )
    result = np.asarray(value, dtype=float)
    _require(np.isfinite(result).all(), "NONFINITE_DATA", "All fit inputs must be finite.", path)
    if shape is not None:
        _require(result.shape == shape, "SHAPE_MISMATCH", f"Expected shape {shape}.", path)
    return result


def _identities(value: Any, *, allow_empty: bool, path: str) -> set[str]:
    _require(
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and item for item in value),
        "MISSING_DATASET_LINEAGE",
        "Explicit dataset identity lists are required.",
        path,
    )
    _require(
        len(set(value)) == len(value), "DUPLICATE_DATASET", "Dataset IDs must be unique.", path
    )
    return set(value)


def _validate_saturation(measurement: dict, observation: dict, *, physical: bool) -> None:
    """Verify stored analysis evidence, without authenticating caller-supplied lab claims."""
    if not physical:
        return
    instrument = measurement.get("instrument", {})
    recorded = instrument.get("saturation_diagnostics")
    supplied = observation.get("saturation_diagnostics")
    _require(
        isinstance(recorded, dict) and recorded == supplied,
        "MISSING_SATURATION_EVIDENCE",
        "Physical fitting requires the registered measurement's matching saturation diagnostics.",
        "measurement.instrument.saturation_diagnostics",
    )
    _require(
        recorded.get("source") == "raw_roi_observation_v1"
        and recorded.get("linear_unsaturated") is True
        and recorded.get("saturated_samples") == 0
        and isinstance(recorded.get("total_samples"), int)
        and recorded["total_samples"] > 0,
        "SATURATED_OBSERVATION",
        "All examined RAW/dark ROI samples must pass saturation checks.",
    )
    linearity = instrument.get("linear_response_evidence")
    _require(
        isinstance(linearity, dict)
        and linearity.get("confirmed") is True
        and bool(linearity.get("method_and_version"))
        and bool(linearity.get("source_uri")),
        "MISSING_LINEARITY_EVIDENCE",
        "ADC endpoint checks cannot establish full-well linearity; "
        "physical fits require explicit external linear-response evidence.",
    )


def _sampling(observation: dict, measurement: dict, field: np.ndarray) -> tuple[np.ndarray, list]:
    sampling = observation.get("sampling")
    _require(
        isinstance(sampling, dict), "MISSING_SAMPLING", "A frozen observation operator is required."
    )
    _require(
        sampling.get("field_radius") == field.tolist(),
        "SAMPLING_FIELD_MISMATCH",
        "Sampling basis coordinates must exactly match the supplied lens field axis.",
    )
    mode = sampling.get("mode")
    if mode == "continuous_dark_corrected_expectation":
        _require(
            measurement.get("source_kind") != "measured",
            "PHYSICAL_POINT_APPROXIMATION",
            "Physical RAW samples need the RAW ROI operator; point-field expectations are "
            "implementation fixtures only.",
        )
        weights = np.repeat(np.eye(len(field))[:, None, :], 3, axis=1)
        roi_fields = field.tolist()
    elif mode == "raw_roi_channel_mean_ratio":
        roi_fields = sampling.get("roi_field_radius")
        _require(
            isinstance(roi_fields, list) and 0 < len(roi_fields) <= 4096,
            "MISSING_ROI_COORDINATES",
            "RAW sampling requires ordered ROI field labels.",
        )
        labels = _array(roi_fields, (len(roi_fields),), "sampling.roi_field_radius")
        _require(
            np.all(np.diff(labels) > 0) and np.all((labels >= 0) & (labels <= 1)),
            "INVALID_ROI_COORDINATES",
            "ROI field labels must be unique, increasing and in [0,1].",
        )
        weights = _array(
            sampling.get("channel_field_weights"),
            (len(labels), 3, len(field)),
            "sampling.channel_field_weights",
        )
        _require(
            np.all(weights >= 0) and np.allclose(weights.sum(axis=-1), 1, rtol=0, atol=1e-10),
            "INVALID_SAMPLING_WEIGHTS",
            "Per-ROI, per-CFA radial weights must be nonnegative "
            "and sum to one; relative illumination is applied separately.",
        )
        method = measurement.get("observation_method")
        _require(
            isinstance(method, dict)
            and method.get("id") == "raw_roi_observation_v1"
            and isinstance(method.get("specification"), dict),
            "OBSERVATION_METHOD_MISMATCH",
            "RAW ratios require the explicit raw_roi_observation_v1 "
            "method and its frozen specification.",
        )
        specification = method["specification"]
        if measurement.get("source_kind") == "measured":
            _require(
                specification.get("geometry", {}).get("kind") == "native_sensor",
                "PHYSICAL_GEOMETRY_MISMATCH",
                "Physical RAW fitting needs native full-sensor/crop "
                "geometry; representative synthetic rasters cannot establish physical sampling.",
            )
        _require(
            method.get("sampling_hash") == _operator_hash(specification),
            "OBSERVATION_HASH_MISMATCH",
            "Measurement observation specification failed its content hash.",
        )
        if "observation_method" in observation:
            _require(
                observation["observation_method"] == method,
                "OBSERVATION_METHOD_MISMATCH",
                "Measurement and forward observation methods must match exactly.",
            )
        if "specification_hash" in sampling:
            _require(
                sampling["specification_hash"] == method["sampling_hash"],
                "OBSERVATION_HASH_MISMATCH",
                "Sampling and measurement specifications differ.",
            )
        _require(
            specification.get("dark_correction") in {"mean_dark_frame", "black_level_only"},
            "DARK_CORRECTION_MISMATCH",
            "Only recorded dark-frame or black-level correction is supported.",
        )
        shape = measurement.get("sample_geometry", {}).get("shape")
        _require(
            isinstance(shape, list),
            "MISSING_SAMPLE_GEOMETRY",
            "RAW calibration requires the registered native/cropped sample shape.",
        )
        _require(
            measurement["sample_geometry"].get("geometry") == specification.get("geometry")
            and measurement["sample_geometry"].get("dark_correction")
            == specification.get("dark_correction"),
            "SAMPLE_GEOMETRY_MISMATCH",
            "Captured sample geometry/correction differs from the method.",
        )
        recomputed = observation_sampling_weights(specification, shape, field.tolist())
        _require(
            all(sampling.get(key) == value for key, value in recomputed.items()),
            "SAMPLING_GEOMETRY_MISMATCH",
            "Sampling weights do not match the registered ROI/CFA geometry.",
        )
    else:
        raise _RefusalError("UNSUPPORTED_SAMPLING", "Unsupported observation sampling mode.")
    if "content_hash" in sampling:
        _require(
            sampling["content_hash"]
            == _operator_hash({k: v for k, v in sampling.items() if k != "content_hash"}),
            "SAMPLING_HASH_MISMATCH",
            "Frozen sampling weights failed their content hash.",
        )
    expected_axes = [
        {"name": "field_radius", "unit": "1", "values": roi_fields},
        {"name": "channel_ratio", "unit": "1", "values": ["R/G", "B/G"]},
    ]
    _require(
        measurement.get("axes") == expected_axes,
        "AXIS_MISMATCH",
        "Metric coordinates must match the observation operator, including CFA ratio order.",
    )
    return weights, roi_fields


def propose_angular_calibration(
    base_sensor_model: dict, observations: list[dict], parameter_bounds: dict
) -> dict:
    """Return an unapproved candidate or a structured refusal, without mutating inputs.

    Bounds are ``{'R': {'min': [...], 'max': [...], 'unit': '1'}, 'B': ...}``,
    one scale per existing nonzero theta node. Observations provide registered
    measurement metadata, lens payload, stimulus, identical conditions and an
    explicit point-field or RAW ROI sampling operator. The service is responsible
    for resolving these from registered immutable records; this pure function
    cannot authenticate lab provenance. Training fit errors are never converted
    into prediction-uncertainty bounds or independent physical validation.
    """
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "algorithm": ALGORITHM,
        "status": "REFUSED",
        "candidate_payload": None,
        "uncertainty": {"kind": "unknown"},
        "training_dataset_ids": [],
        "calibration_dataset_ids": [],
        "physical_evidence": False,
        "evidence_kind": "implementation_verification",
        "approval_status": "unapproved",
        "physical_validation_pass": False,
        "independent_validation": False,
        "parameters": [],
        "diagnostics": {},
        "issues": [],
        "limitations": [
            "Calibration is a fit to training data, not independent validation or model approval.",
            "Prediction uncertainty stays unknown; training residuals are not uncertainty bounds.",
            "Only wavelength-independent R/B angular-node scales change; other inputs stay fixed.",
            "The registry supplies physical provenance; this function cannot authenticate a lab.",
        ],
    }
    try:
        report["input_hash"] = _hash(
            {
                "base_sensor_model": base_sensor_model,
                "observations": observations,
                "parameter_bounds": parameter_bounds,
            }
        )
        _require(
            isinstance(base_sensor_model, dict)
            and base_sensor_model.get("kind") == "sensor"
            and isinstance(base_sensor_model.get("id"), str),
            "INVALID_BASE_MODEL",
            "Calibration requires a pinned sensor model revision.",
            "base_sensor_model",
        )
        _require(
            base_sensor_model.get("reference_plane") == "sensor_surface",
            "REFERENCE_PLANE_MISMATCH",
            "Sensor QE must refer to the sensor entrance surface.",
            "base_sensor_model.reference_plane",
        )
        effects = base_sensor_model.get("included_effects", [])
        _require(
            set(effects) == SENSOR_EFFECTS and len(effects) == len(SENSOR_EFFECTS),
            "INCLUDED_EFFECTS_MISMATCH",
            "Only the declared response_db_v1 sensor effects can be fitted.",
        )
        inherited_ids = _identities(
            base_sensor_model.get("calibration_dataset_ids"),
            allow_empty=True,
            path="base_sensor_model.calibration_dataset_ids",
        )
        _require(
            isinstance(observations, list) and 1 <= len(observations) <= 64,
            "INVALID_OBSERVATIONS",
            "Supply 1..64 independent training records.",
        )
        payload = base_sensor_model["payload"]
        sensor_angular = payload["sensor_angular_response"]
        theta = _array(sensor_angular["theta_deg"], None, "sensor.theta_deg")
        wave = _array(payload["wavelength_nm"], None, "sensor.wavelength_nm")
        _require(
            theta.ndim == 1
            and 2 <= len(theta) <= 181
            and theta[0] == 0
            and np.all(np.diff(theta) > 0),
            "INVALID_SENSOR_ANGLES",
            "Sensor angles must start at zero and strictly increase.",
        )
        _require(
            wave.ndim == 1 and 2 <= len(wave) <= 2048 and np.all(np.diff(wave) > 0),
            "INVALID_SPECTRAL_AXIS",
            "A finite, increasing spectral axis is required.",
        )
        qe = _array(payload["qe"], (3, len(wave)), "sensor.qe")
        response = _array(
            sensor_angular["relative_response"],
            (3, len(wave), len(theta)),
            "sensor.relative_response",
        )
        _require(
            np.all((qe >= 0) & (qe <= 1))
            and np.all(response >= 0)
            and np.allclose(response[:, :, 0], 1, rtol=0, atol=1e-12)
            and np.all(response * qe[..., None] <= 1 + 1e-12),
            "INVALID_SENSOR_RESPONSE",
            "QE must be physical and relative response must normalize to 1 at normal incidence.",
        )
        count = len(theta) - 1
        _require(
            isinstance(parameter_bounds, dict) and set(parameter_bounds) == {"R", "B"},
            "INVALID_PARAMETER_BOUNDS",
            "Explicit bounds for R and B are required.",
            "parameter_bounds",
        )
        bounds = {}
        for channel, index in (("R", 0), ("B", 2)):
            spec = parameter_bounds[channel]
            _require(
                isinstance(spec, dict) and spec.get("unit") == "1",
                "BOUND_UNIT_MISMATCH",
                "Angular scale bounds are dimensionless and require unit '1'.",
                "parameter_bounds",
            )
            lower = _array(spec.get("min"), (count,), f"bounds.{channel}.min")
            upper = _array(spec.get("max"), (count,), f"bounds.{channel}.max")
            _require(
                np.all(lower >= 0) and np.all(upper > lower),
                "INVALID_PARAMETER_BOUNDS",
                "Each scale requires finite 0 <= min < max.",
                "parameter_bounds",
            )
            max_qe = (response[index, :, 1:] * qe[index, :, None]).max(axis=0)
            cap = np.divide(1.0, max_qe, out=np.full(count, np.inf), where=max_qe > 0)
            effective_upper = np.minimum(upper, cap)
            _require(
                np.all(effective_upper > lower),
                "INFEASIBLE_PHYSICAL_BOUNDS",
                "The requested lower scale exceeds the QE<=1 upper cap.",
                "parameter_bounds",
            )
            bounds[channel] = (lower, effective_upper)
            report["parameters"].extend(
                {
                    "channel": channel,
                    "theta_deg": float(theta[k + 1]),
                    "requested_min": float(lower[k]),
                    "requested_max": float(upper[k]),
                    "effective_max": float(effective_upper[k]),
                }
                for k in range(count)
            )

        systems = {"R": [], "B": []}
        targets = {"R": [], "B": []}
        partitions = []
        training_ids: set[str] = set()
        physical_flags = []
        physical_instances = set()
        frozen_observations = []
        reference_temperature = None
        for obs_index, observation in enumerate(observations):
            measurement = observation["measurement"]
            _require(
                isinstance(measurement.get("id"), str),
                "MISSING_MEASUREMENT_ID",
                "Training measurements must have pinned record identities.",
            )
            dataset_ids = _identities(
                measurement.get("dataset_ids"),
                allow_empty=False,
                path=f"observations.{obs_index}.measurement.dataset_ids",
            )
            _require(
                not (training_ids & dataset_ids),
                "DUPLICATE_TRAINING_RECORD",
                "A training dataset may not be counted repeatedly in one fit.",
            )
            training_ids |= dataset_ids
            source = measurement.get("source_kind")
            _require(
                source in {"measured", "simulated", "estimated", "synthetic"},
                "UNSUPPORTED_TRAINING_SOURCE",
                "Training records must explicitly identify measured "
                "or synthetic origins; vendor declarations are not observations.",
            )
            physical = source == "measured"
            physical_flags.append(physical)
            _require(
                bool(measurement.get("source_uri")) and bool(measurement.get("method_and_version")),
                "MISSING_PROVENANCE",
                "Every observation requires source and method provenance.",
            )
            if physical:
                instrument = measurement.get("instrument", {})
                _require(
                    bool(measurement.get("physical_instance"))
                    and bool(instrument.get("id"))
                    and bool(instrument.get("calibration_revision")),
                    "MISSING_PHYSICAL_PROVENANCE",
                    "Physical fitting requires sample, instrument and calibration identities.",
                )
                physical_instances.add(measurement["physical_instance"])
            _validate_saturation(measurement, observation, physical=physical)
            _require(
                measurement.get("metric_id") == "raw.channel_ratio"
                and measurement.get("unit") == "1"
                and measurement.get("reference_plane") == "sensor_raw",
                "METRIC_CONTRACT_MISMATCH",
                "Only dimensionless pre-ISP RAW R/G and B/G ratios at sensor_raw can be fitted.",
            )
            observed_effects = measurement.get("included_effects", [])
            _require(
                set(observed_effects) == OBSERVATION_EFFECTS
                and len(observed_effects) == len(OBSERVATION_EFFECTS),
                "INCLUDED_EFFECTS_MISMATCH",
                "Observation physical effects differ from the fixed runtime.",
            )
            conditions = observation.get("conditions")
            _require(
                isinstance(conditions, dict) and conditions == measurement.get("conditions"),
                "CONDITION_MISMATCH",
                "Observation and registered measurement conditions must match exactly.",
            )
            for key, unit in {"exposure_s": "s", "temperature_c": "degC", "gain": "1"}.items():
                value = conditions.get(key, {})
                _require(
                    isinstance(value, dict)
                    and value.get("unit") == unit
                    and isinstance(value.get("value"), (float, int))
                    and not isinstance(value["value"], bool),
                    "CONDITION_UNIT_MISMATCH",
                    f"{key} must be a numerical condition in {unit}.",
                )
            domain_issues = check_domain(conditions, base_sensor_model.get("valid_domain"))
            _require(
                not domain_issues,
                "OUT_OF_DOMAIN",
                "Model validity is missing, unknown or exceeded: "
                + "; ".join(issue["message"] for issue in domain_issues),
            )
            temperature = conditions["temperature_c"]
            if reference_temperature is None:
                reference_temperature = temperature
            _require(
                temperature == reference_temperature,
                "TEMPERATURE_MISMATCH",
                "This static model has no temperature response; fit one temperature at a time.",
            )
            stimulus, lens = observation["stimulus"], observation["lens"]
            _require(
                stimulus.get("wavelength_nm") == wave.tolist(),
                "SPECTRAL_COORDINATE_MISMATCH",
                "Stimulus and sensor spectral coordinates must be exactly equal; no resampling.",
            )
            _require(
                stimulus.get("reference_plane") == "sensor_surface",
                "REFERENCE_PLANE_MISMATCH",
                "Stimulus must be on-axis sensor-surface irradiance, including lens throughput.",
            )
            request = {
                "wavelength_nm": payload["wavelength_nm"],
                "qe": payload["qe"],
                "pixel_pitch_um": payload["pixel_pitch_um"],
                "readout": payload["readout"],
                "sensor_angular_response": sensor_angular,
                "lens": lens,
                "spectral_irradiance_w_m2_nm": stimulus["spectral_irradiance_w_m2_nm"],
                "irradiance_reference_plane": "sensor_surface_on_axis",
                "exposure_s": conditions["exposure_s"]["value"],
                "temperature_c": conditions["temperature_c"]["value"],
                "output_size": [8, 8],
                "frames": 1,
            }
            runtime_issues = get_plugin("response_db").validate_request(request)
            _require(
                not runtime_issues,
                "RUNTIME_CONTRACT_REFUSAL",
                "; ".join(issue["message"] for issue in runtime_issues),
            )
            _require(
                conditions["gain"]["value"] == payload["readout"]["gain"],
                "GAIN_MISMATCH",
                "Readout gain must match the recorded fixed condition.",
            )
            field = np.asarray(lens["field_radius"], dtype=float)
            sampling_weights, roi_fields = _sampling(observation, measurement, field)
            if observation["sampling"]["mode"] == "raw_roi_channel_mean_ratio":
                raw_format = measurement["observation_method"]["specification"]["raw_format"]
                _require(
                    raw_format.get("bit_depth") == payload["readout"]["bit_depth"]
                    and raw_format.get("black_level_dn") == payload["readout"]["black_level_dn"],
                    "RAW_READOUT_MISMATCH",
                    "Registered RAW encoding differs from the fixed sensor readout.",
                )
            observed = _array(
                measurement.get("values"), (len(roi_fields) * 2,), "measurement.values"
            ).reshape(-1, 2)
            _require(
                np.all(observed >= 0),
                "INVALID_RATIO",
                "Linear dark-corrected channel ratios cannot be negative.",
            )

            # Basis k is the original response at sensor angle node k. All operations
            # are linear in the R/B node scale while the green denominator is fixed.
            lens_theta = np.asarray(lens["theta_deg"], dtype=float)
            interpolation = np.stack(
                [np.interp(lens_theta, theta, row) for row in np.eye(len(theta))], axis=-1
            )
            angular_basis = np.asarray(lens["angular_weights"]) @ interpolation
            irradiance = np.asarray(stimulus["spectral_irradiance_w_m2_nm"])
            photons = irradiance * wave * 1e-9 / (PLANCK_J_S * LIGHT_M_S)
            exposure_area = (
                conditions["exposure_s"]["value"] * (payload["pixel_pitch_um"] * 1e-6) ** 2
            )
            spectral_terms = response * qe[..., None] * photons[None, :, None]
            if "wavelength_nm" in lens:
                field_terms = (
                    np.trapezoid(
                        spectral_terms[:, :, None, :]
                        * angular_basis[None, :, :, :]
                        * np.asarray(lens["relative_illumination"])[None, :, :, None],
                        x=wave,
                        axis=1,
                    )
                    * exposure_area
                )
            else:
                spectral = np.trapezoid(spectral_terms, x=wave, axis=1) * exposure_area
                field_terms = (
                    spectral[:, None, :]
                    * angular_basis[None, :, :]
                    * np.asarray(lens["relative_illumination"])[None, :, None]
                )
            readout = payload["readout"]
            dark = readout["dark_current_e_per_s"] * conditions["exposure_s"]["value"]
            correction = (
                measurement.get("observation_method", {})
                .get("specification", {})
                .get("dark_correction")
            )
            retained_dark = dark if correction == "black_level_only" else 0.0
            green_field = field_terms[1].sum(axis=-1)
            green = sampling_weights[:, 1, :] @ green_field + retained_dark
            _require(
                np.all(green > 0) and np.isfinite(green).all(),
                "ZERO_GREEN_REFERENCE",
                "Every observed ROI needs a finite positive known green denominator.",
            )
            capacity = min(readout["full_well_e"], readout["full_well_e"] / readout["gain"])
            maxima = [green_field]
            for channel, index, ratio_index in (("R", 0, 0), ("B", 2, 1)):
                coefficients = sampling_weights[:, index, :] @ field_terms[index, :, 1:]
                fixed = sampling_weights[:, index, :] @ field_terms[index, :, 0] + retained_dark
                # Equal weight per observed ratio; no unprovided noise covariance is inferred.
                systems[channel].append(coefficients / green[:, None])
                targets[channel].append(observed[:, ratio_index] - fixed / green)
                maxima.append(
                    field_terms[index, :, 0] + field_terms[index, :, 1:] @ bounds[channel][1]
                )
            _require(
                all(np.all(values + dark < capacity) for values in maxima),
                "SATURATION_IN_PARAMETER_DOMAIN",
                "The parameter box reaches sensor/ADC clipping; linear fitting is invalid.",
            )
            partitions.append(
                {
                    "measurement_id": measurement["id"],
                    "sample_count": len(roi_fields),
                    "dataset_ids": sorted(dataset_ids),
                    "source_kind": source,
                }
            )
            frozen_observations.append(
                {
                    "measurement_id": measurement["id"],
                    "measurement_hash": _hash(measurement),
                    "lens_hash": _hash(lens),
                    "stimulus_hash": _hash(stimulus),
                    "sampling_hash": _hash(observation["sampling"]),
                    "conditions": deepcopy(conditions),
                }
            )

        _require(
            len(physical_instances) <= 1,
            "PHYSICAL_INSTANCE_MISMATCH",
            "This fit targets one camera instance; population fitting needs another model.",
        )
        _require(
            all(physical_flags) or not any(physical_flags),
            "MIXED_EVIDENCE_SOURCES",
            "Physical measurements and synthetic fixtures cannot be pooled into one fit.",
        )
        candidate = deepcopy(payload)
        fitted_response = deepcopy(sensor_angular["relative_response"])
        diagnostics = {}
        for channel, index in (("R", 0), ("B", 2)):
            matrix, target = np.concatenate(systems[channel]), np.concatenate(targets[channel])
            singular = np.linalg.svd(matrix, compute_uv=False)
            rank = int(np.linalg.matrix_rank(matrix))
            with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
                condition = (
                    float(singular[0] / singular[-1])
                    if len(singular) == count and singular[-1] > 0
                    else None
                )
            if condition is not None and not np.isfinite(condition):
                condition = None
            diagnostics[channel] = {
                "rows": len(target),
                "parameters": count,
                "rank": rank,
                "singular_values": singular.tolist(),
                "condition_number": condition,
            }
            report["diagnostics"] = diagnostics
            _require(
                rank == count and condition is not None and condition <= MAX_CONDITION_NUMBER,
                "UNIDENTIFIABLE_FIT",
                f"{channel} angular scales are rank deficient or ill-conditioned; "
                "additional independent incident-angle distributions are required.",
            )
            fit = lsq_linear(
                matrix,
                target,
                bounds=bounds[channel],
                method="trf",
                tol=1e-12,
                max_iter=1000,
                lsmr_tol="auto",
            )
            _require(
                fit.success and np.isfinite(fit.x).all(),
                "SOLVER_REFUSAL",
                "Bounded least-squares did not converge.",
            )
            before, after = matrix @ np.ones(count) - target, matrix @ fit.x - target
            diagnostics[channel].update(
                {
                    "scale_factors": fit.x.tolist(),
                    "active_bounds": fit.active_mask.tolist(),
                    "before_ratio_rmse": float(np.sqrt(np.mean(before**2))),
                    "training_ratio_rmse": float(np.sqrt(np.mean(after**2))),
                    "training_ratio_mae": float(np.mean(np.abs(after))),
                    "training_ratio_residuals": after.tolist(),
                }
            )
            for wave_index in range(len(wave)):
                for node in range(count):
                    fitted_response[index][wave_index][node + 1] = float(
                        response[index, wave_index, node + 1] * fit.x[node]
                    )
            for parameter, scale in zip(
                (p for p in report["parameters"] if p["channel"] == channel), fit.x, strict=True
            ):
                parameter["scale"] = float(scale)
        candidate["sensor_angular_response"]["relative_response"] = fitted_response
        _require(
            np.all(np.asarray(fitted_response) * qe[..., None] <= 1 + 1e-12),
            "PHYSICAL_BOUND_VIOLATION",
            "Candidate angular QE exceeded one electron per incident photon.",
        )
        report.update(
            status="PROPOSED",
            candidate_payload=candidate,
            training_dataset_ids=sorted(training_ids),
            calibration_dataset_ids=sorted(training_ids | inherited_ids),
            physical_evidence=all(physical_flags),
            evidence_kind="physical_calibration_fit"
            if all(physical_flags)
            else "implementation_verification",
            input_manifest={
                "base_model_id": base_sensor_model["id"],
                "base_model_hash": _hash(base_sensor_model),
                "observations": frozen_observations,
                "parameter_bounds": deepcopy(parameter_bounds),
            },
            training_observations=partitions,
        )
        report["diagnostics"]["weighting"] = (
            "equal weight per observed channel ratio; no covariance inferred"
        )
        report["diagnostics"]["condition_number_limit"] = MAX_CONDITION_NUMBER
        _hash(report)  # Refuse any non-JSON/nonfinite diagnostic before exposing a candidate.
    except _RefusalError as error:
        report["issues"].append(error.issue)
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        np.linalg.LinAlgError,
    ) as error:
        report["issues"].append(
            {
                "code": "INVALID_CALIBRATION_INPUT",
                "message": str(error),
                "path": "request",
                "severity": "error",
            }
        )
    if report["issues"]:
        report.update(status="REFUSED", candidate_payload=None, physical_evidence=False)
    return report
