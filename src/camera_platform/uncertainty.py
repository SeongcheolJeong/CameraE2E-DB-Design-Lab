"""Input-bound propagation for continuous camera expectations.

Intervals enclose the declared discrete calculation, with outward rounded
arithmetic. They are conditional on all unspecified inputs remaining nominal.
Unknown physical discrepancy, interpolation error and observation effects stay
unknown. No standard deviation, coverage interval or sample extremum is promoted
to an absolute bound. Correlations are enclosed by a Cartesian superset, not
assumed independent.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import numpy as np

from .contracts import ParameterUncertainty

# Public paths address nominal model payloads or frozen test conditions/stimulus.
MODEL_PATHS = {
    "lens": {
        "angular_weights": ("lens.angular_weights", "1", 0, 1),
        "relative_illumination": ("lens.relative_illumination", "1", 0, None),
    },
    "sensor": {
        "qe": ("qe", "electron/photon", 0, 1),
        "pixel_pitch_um": ("pixel_pitch_um", "um", 0.1, 1000),
        "sensor_angular_response.relative_response": (
            "sensor_angular_response.relative_response",
            "1",
            0,
            None,
        ),
        "readout.full_well_e": ("readout.full_well_e", "electron", 1, 1e9),
        "readout.dark_current_e_per_s": ("readout.dark_current_e_per_s", "electron/s", 0, 1e9),
        "readout.gain": ("readout.gain", "1", 1e-6, 1e6),
        "readout.black_level_dn": ("readout.black_level_dn", "DN", 0, None),
    },
    "isp": {},
}
TEST_PATHS = {
    "stimulus.spectral_irradiance_w_m2_nm": ("spectral_irradiance_w_m2_nm", "W/m^2/nm", 0, None),
    "conditions.exposure_s.value": ("exposure_s", "s", 0, 3600),
}
REQUEST_PATHS = {
    v[0]: v[1:] for paths in [*MODEL_PATHS.values(), TEST_PATHS] for v in paths.values()
}
BASE_PATHS = set(REQUEST_PATHS) - {
    "lens.angular_weights",
    "lens.relative_illumination",
    "sensor_angular_response.relative_response",
}


def get_path(value, path):
    for key in path.split("."):
        # The runtime already accepts this historical relative-response name.
        if key == "relative_response" and key not in value and "response" in value:
            key = "response"
        value = value[key]
    return value


def numeric(value):
    def no_booleans(item):
        if isinstance(item, (bool, np.bool_)):
            raise ValueError("Uncertainty values cannot contain booleans")
        if isinstance(item, (list, tuple)):
            for child in item:
                no_booleans(child)

    no_booleans(value)
    array = np.asarray(value)
    if array.dtype.kind not in "fiu" or not np.isfinite(array).all():
        raise ValueError("Uncertainty values require finite numbers, not strings or booleans")
    return np.asarray(array, dtype=np.float64)


def checked_bound(nominal, definition, unit, minimum, maximum):
    definition = ParameterUncertainty.model_validate(definition).model_dump()
    if definition["unit"] != unit:
        raise ValueError(f"Uncertainty unit must be {unit}")
    nominal = numeric(nominal)
    if definition["kind"] == "unknown":
        return definition
    bound = numeric(definition["value"])
    if bound.ndim and bound.shape != nominal.shape:
        raise ValueError("Input bounds must be scalar or match the exact nominal array shape")
    if np.any(bound < 0) or bound.size > 2_000_000:
        raise ValueError("Input bounds must be nonnegative and within the supported array budget")
    low, high = nominal - bound, nominal + bound
    if not np.isfinite(low).all() or not np.isfinite(high).all():
        raise ValueError("Input bound arithmetic overflowed")
    if (minimum is not None and np.any(low < minimum)) or (
        maximum is not None and np.any(high > maximum)
    ):
        raise ValueError("Input uncertainty crosses the supported physical parameter domain")
    return definition


def validate_record(record, *, test=False):
    paths = TEST_PATHS if test else MODEL_PATHS[record["kind"]]
    bounds = record.get("parameter_uncertainty", {})
    if set(bounds) - set(paths):
        raise ValueError(
            "Unsupported parameter uncertainty path: " + ", ".join(sorted(set(bounds) - set(paths)))
        )
    source = record if test else record["payload"]
    for path, definition in bounds.items():
        _, unit, low, high = paths[path]
        checked_bound(get_path(source, path), definition, unit, low, high)
        if path in {"relative_illumination", "sensor_angular_response.relative_response"}:
            if definition["kind"] == "absolute_bound":
                value = np.broadcast_to(
                    numeric(definition["value"]), numeric(get_path(source, path)).shape
                )
                if np.any(value[..., 0] != 0):
                    raise ValueError(
                        "Normalized on-axis response requires a zero bound at its fixed reference"
                    )
    if not test and record["kind"] == "sensor" and "readout.black_level_dn" in bounds:
        definition = bounds["readout.black_level_dn"]
        if definition["kind"] == "absolute_bound":
            high = record["payload"]["readout"]["black_level_dn"] + numeric(definition["value"])
            if np.any(high >= 2 ** record["payload"]["readout"]["bit_depth"] - 1):
                raise ValueError("Black-level uncertainty reaches the ADC white level")


def collect_inputs(models, test, *, check_operating_domain=True):
    result = {}
    for record, is_test in [(m, False) for m in models] + [(test, True)]:
        validate_record(record, test=is_test)
        paths = TEST_PATHS if is_test else MODEL_PATHS[record["kind"]]
        for path, definition in record.get("parameter_uncertainty", {}).items():
            target = paths[path][0]
            result[target] = {
                "definition": definition,
                "source_record_id": record["id"],
                "source_content_hash": record.get("content_hash"),
                "source_path": path,
            }
    for target, condition, unit in [
        ("exposure_s", "exposure_s", "s"),
        ("readout.gain", "gain", "1"),
    ]:
        record = result.get(target)
        if check_operating_domain and record and record["definition"]["kind"] == "absolute_bound":
            value = test["conditions"][condition]["value"]
            delta = numeric(record["definition"]["value"])
            for model in models:
                domain = model["valid_domain"].get(condition)
                if domain and (
                    domain.get("unit") != unit
                    or value - delta < domain["min"]
                    or value + delta > domain["max"]
                ):
                    raise ValueError(
                        "OUT_OF_DOMAIN: input uncertainty crosses a model's operating domain"
                    )
    return result


def validate_request(request):
    records = request.get("parameter_uncertainty", {})
    if not isinstance(records, dict) or set(records) - set(REQUEST_PATHS):
        raise ValueError("Unsupported runtime uncertainty input path")
    for path, record in records.items():
        if not isinstance(record, dict) or set(record) != {
            "definition",
            "source_record_id",
            "source_content_hash",
            "source_path",
        }:
            raise ValueError("Uncertainty input needs its frozen source record and path")
        if any(
            not isinstance(record[key], str) or not record[key]
            for key in ["source_record_id", "source_path"]
        ):
            raise ValueError("Uncertainty source record and path must be nonempty strings")
        checked_bound(get_path(request, path), record["definition"], *REQUEST_PATHS[path])
    black = records.get("readout.black_level_dn", {}).get("definition")
    if black and black["kind"] == "absolute_bound":
        if (
            request["readout"]["black_level_dn"] + numeric(black["value"])
            >= 2 ** request["readout"]["bit_depth"] - 1
        ):
            raise ValueError("Black-level uncertainty reaches the ADC white level")


def down(value):
    return np.nextafter(value, -np.inf)


def up(value):
    return np.nextafter(value, np.inf)


@dataclass(frozen=True)
class Interval:
    lo: np.ndarray
    hi: np.ndarray

    @classmethod
    def point(cls, value):
        x = numeric(value)
        return cls(x, x)

    @classmethod
    def decimal_constant(cls, value):
        x = numeric(value)
        return cls(down(x), up(x))

    def __getitem__(self, item):
        return Interval(self.lo[item], self.hi[item])

    def __add__(self, other):
        return Interval(down(self.lo + other.lo), up(self.hi + other.hi))

    def __sub__(self, other):
        return Interval(down(self.lo - other.hi), up(self.hi - other.lo))

    def __mul__(self, other):
        products = np.broadcast_arrays(
            self.lo * other.lo, self.lo * other.hi, self.hi * other.lo, self.hi * other.hi
        )
        return Interval(down(np.minimum.reduce(products)), up(np.maximum.reduce(products)))

    def __truediv__(self, other):
        if np.any((other.lo <= 0) & (other.hi >= 0)):
            raise ValueError("Uncertainty denominator includes zero")
        return self * Interval(down(1 / other.hi), up(1 / other.lo))

    def minimum(self, other):
        return Interval(np.minimum(self.lo, other.lo), np.minimum(self.hi, other.hi))

    def nonnegative(self):
        return Interval(np.maximum(0, self.lo), np.maximum(0, self.hi))

    def transpose(self, axes):
        return Interval(self.lo.transpose(axes), self.hi.transpose(axes))

    def json(self):
        if not np.isfinite(self.lo).all() or not np.isfinite(self.hi).all():
            raise ValueError("Propagated bound exceeded the finite numerical domain")
        return {"lower": self.lo.tolist(), "upper": self.hi.tolist()}


def input_interval(request, path):
    nominal = numeric(get_path(request, path))
    definition = request.get("parameter_uncertainty", {}).get(path, {}).get("definition")
    if not definition or definition["kind"] == "unknown":
        return Interval.point(nominal)
    bound = numeric(definition["value"])
    low, high = down(nominal - bound), up(nominal + bound)
    minimum, maximum = REQUEST_PATHS[path][1:]
    if minimum is not None:
        low = np.maximum(low, minimum)
    if maximum is not None:
        high = np.minimum(high, maximum)
    return Interval(low, high)


def interpolate(values, axis, targets):
    """Linear interpolation on exact, fixed axes with outward rounded weights."""
    parts = []
    for x in targets:
        matches = np.flatnonzero(axis == x)
        if matches.size:
            parts.append(values[..., matches[0]])
            continue
        i = int(np.searchsorted(axis, x)) - 1
        if i < 0 or i >= len(axis) - 1:
            raise ValueError("Uncertainty interpolation would extrapolate")
        t = (Interval.point(x) - Interval.point(axis[i])) / (
            Interval.point(axis[i + 1]) - Interval.point(axis[i])
        )
        # Each term uses nonnegative weights; dependence is conservatively enclosed.
        parts.append(values[..., i] * (Interval.point(1) - t) + values[..., i + 1] * t)
    return Interval(
        np.stack([p.lo for p in parts], axis=-1), np.stack([p.hi for p in parts], axis=-1)
    )


def expectation_intervals(request, inputs, response_db):
    qe = input_interval(request, "qe")
    if response_db:
        response = interpolate(
            input_interval(request, "sensor_angular_response.relative_response"),
            inputs["sensor_theta"],
            inputs["theta"],
        )
        weights = input_interval(request, "lens.angular_weights")
        ri = input_interval(request, "lens.relative_illumination")
        if weights.lo.ndim == 2:
            weights = Interval(
                np.broadcast_to(weights.lo, inputs["weights"].shape),
                np.broadcast_to(weights.hi, inputs["weights"].shape),
            )
            ri = Interval(
                np.broadcast_to(ri.lo, inputs["ri"].shape),
                np.broadcast_to(ri.hi, inputs["ri"].shape),
            )
        effective = Interval.point(np.zeros((len(inputs["field"]), 3, len(inputs["wave"]))))
        for a in range(len(inputs["theta"])):
            effective = (
                effective
                + weights[:, :, a].transpose((1, 0))[:, None, :] * response[:, :, a][None, :, :]
            )
        effective = effective.nonnegative() * qe[None, :, :] * ri.transpose((1, 0))[:, None, :]
    else:
        effective = qe[None, :, :]
    density = input_interval(request, "spectral_irradiance_w_m2_nm") * Interval.point(
        inputs["wave"]
    )
    density = (
        density
        * Interval.decimal_constant(1e-9)
        / (Interval.decimal_constant(6.62607015e-34) * Interval.point(299792458))
    )
    spectrum = (effective * density).nonnegative()
    charge = Interval.point(np.zeros(spectrum.lo.shape[:-1]))
    for w in range(len(inputs["wave"]) - 1):
        width = Interval.point(inputs["wave"][w + 1]) - Interval.point(inputs["wave"][w])
        charge = charge + (spectrum[..., w] + spectrum[..., w + 1]) * width / Interval.point(2)
    pitch = input_interval(request, "pixel_pitch_um") * Interval.decimal_constant(1e-6)
    exposure = input_interval(request, "exposure_s")
    charge = (charge * pitch * pitch * exposure).nonnegative()
    dark = (input_interval(request, "readout.dark_current_e_per_s") * exposure).nonnegative()
    well, gain = (
        input_interval(request, "readout.full_well_e"),
        input_interval(request, "readout.gain"),
    )
    capacity = well.minimum(well / gain)
    white = Interval.point(2 ** inputs["readout"]["bit_depth"] - 1)
    scale = (white - input_interval(request, "readout.black_level_dn")) * gain / well
    corrected = ((charge + dark).minimum(capacity) - dark.minimum(capacity)).nonnegative() * scale
    corrected = corrected.nonnegative()
    uncorrected = ((charge + dark).minimum(capacity).nonnegative() * scale).nonnegative()
    return {
        "channel_electrons": charge,
        "channel_reference_signal_dn": corrected,
        "channel_black_corrected_signal_dn": uncorrected,
    }


def ratios_interval(signal):
    valid = signal.lo[:, 1] > 0
    lower, upper = np.full((len(valid), 2), np.nan), np.full((len(valid), 2), np.nan)
    if valid.any():
        ratios = signal[valid][:, [0, 2]] / signal[valid][:, 1:2]
        lower[valid], upper[valid] = ratios.lo, ratios.hi
    return {
        "lower": [[float(v) if np.isfinite(v) else None for v in row] for row in lower],
        "upper": [[float(v) if np.isfinite(v) else None for v in row] for row in upper],
        "defined": np.broadcast_to(valid[:, None], (len(valid), 2)).tolist(),
    }


def propagate(request, inputs, *, response_db):
    validate_request(request)
    required = set(REQUEST_PATHS) if response_db else BASE_PATHS
    supplied = request.get("parameter_uncertainty", {})
    known = {
        path for path, item in supplied.items() if item["definition"]["kind"] == "absolute_bound"
    }
    unknown = [
        {
            "path": path,
            "reason": supplied.get(path, {}).get("definition", {}).get("reason")
            or "No input bound supplied; fixed at nominal only for the conditional calculation",
        }
        for path in sorted(required - known)
    ]
    unknown.extend(
        [
            {
                "path": "model_discrepancy",
                "reason": "Independent physical model discrepancy is not bounded",
            },
            {
                "path": "spectral_quadrature",
                "reason": "Error between spectral samples is not bounded",
            },
            {
                "path": "raw_observation",
                "reason": "ADC rounding and finite observation statistics are not propagated",
            },
        ]
    )
    if response_db:
        unknown.append(
            {
                "path": "angular_field_interpolation",
                "reason": "Between-node physical response error is not bounded",
            }
        )
    result = {
        "schema": "camera_parameter_uncertainty_v1",
        "status": "partial" if known & required else "unknown",
        "method_and_version": "outward_interval_discrete_expectation_v1",
        "scope": "continuous_expectation_given_declared_input_bounds",
        "dependency_policy": "Cartesian superset; no statistical independence assumed",
        "sources": supplied,
        "propagated_paths": sorted(known & required),
        "not_used_by_this_quantity": sorted(set(supplied) - required),
        "unknown_contributions": unknown,
        "total_uncertainty": {"kind": "unknown"},
        "axes_policy": "Fixed nominal wavelength, field and angle coordinates",
    }
    if not known & required:
        return result
    intervals = expectation_intervals(request, inputs, response_db)
    result["conditional_intervals"] = {key: value.json() for key, value in intervals.items()}
    result["conditional_intervals"]["channel_ratios"] = ratios_interval(
        intervals["channel_reference_signal_dn"]
    )
    result["units"] = {
        "channel_electrons": "electron/pixel",
        "channel_reference_signal_dn": "DN",
        "channel_black_corrected_signal_dn": "DN",
        "channel_ratios": "1",
    }
    return result


def attach(summary, budget):
    budget["axes"] = deepcopy(summary["prediction"]["axes"])
    summary["uncertainty_budget"] = budget
    summary["prediction"]["uncertainty_budget"] = budget
    # Conditional input bounds do not replace the total prediction uncertainty.
    summary["uncertainty"] = {"kind": "unknown"}
    summary["prediction"]["uncertainty"] = {"kind": "unknown"}


def observe_budget(budget, sampling, dark_mode):
    result = deepcopy(budget)
    if "conditional_intervals" not in result:
        return result
    original = result.pop("conditional_intervals")
    key = (
        "channel_black_corrected_signal_dn"
        if dark_mode == "black_level_only"
        else "channel_reference_signal_dn"
    )
    signal = Interval(numeric(original[key]["lower"]), numeric(original[key]["upper"]))
    weights = numeric(sampling["channel_field_weights"])
    means = Interval.point(np.zeros(weights.shape[:2]))
    for field in range(weights.shape[-1]):
        means = means + Interval.point(weights[:, :, field]) * signal[field][None, :]
    means = means.nonnegative()
    result.update(
        upstream_conditional_intervals=original,
        conditional_intervals={
            "channel_means_dn": means.json(),
            "channel_ratios": ratios_interval(means),
        },
        units={"channel_means_dn": "DN", "channel_ratios": "1"},
        observation="frozen_CFA_ROI_operator_on_continuous_expectations",
    )
    return result


def unsupported_budget(models, test, plugin_id):
    inputs = collect_inputs(models, test, check_operating_domain=False)
    if not inputs:
        return None
    return {
        "schema": "camera_parameter_uncertainty_v1",
        "status": "not_propagated",
        "sources": inputs,
        "propagated_paths": [],
        "total_uncertainty": {"kind": "unknown"},
        "unknown_contributions": [
            {
                "path": plugin_id,
                "reason": "This runtime does not yet propagate the supplied input bounds",
            }
        ],
    }
