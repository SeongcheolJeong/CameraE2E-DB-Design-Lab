"""Condition-aware comparisons with explicit evidence and bounded uncertainty.

This module consumes JSON dictionaries so it can be used by the API, workers and
SDK without a database or numerical engine. It never converts units, aligns
coordinates, infers missing conditions or treats missing uncertainty as zero.
Those operations require an explicit, provenance-bearing upstream transform.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

VALIDATION_KINDS = {
    "implementation_verification": "Implementation Verification",
    "cross_tool_comparison": "Cross-tool Comparison",
    "physical_validation": "Physical Validation",
}


def _issue(code: str, message: str, path: str, severity: str = "unknown") -> dict:
    return {"code": code, "message": message, "path": path, "severity": severity}


def _finite_number(value: Any) -> bool:
    try:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    except OverflowError:
        return False


def _quantity(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("unit"), str)
        and bool(value["unit"])
        and (
            _finite_number(value.get("value"))
            or (isinstance(value.get("value"), str) and bool(value["value"]))
        )
    )


def _array(value: Any) -> tuple[list[float], tuple[int, ...]]:
    """Return finite rectangular JSON array data, rejecting ragged/empty arrays."""
    if not isinstance(value, list) or not value:
        raise ValueError("Expected a nonempty numerical array.")
    if all(_finite_number(item) for item in value):
        return [float(item) for item in value], (len(value),)
    if not all(isinstance(item, list) for item in value):
        raise ValueError("Values must be finite numbers in a rectangular array.")
    children = [_array(item) for item in value]
    if len({shape for _, shape in children}) != 1:
        raise ValueError("Ragged arrays cannot be compared.")
    return [number for data, _ in children for number in data], (len(value), *children[0][1])


def check_domain(
    conditions: dict,
    valid_domain: Any,
    *,
    axes: list | None = None,
    path: str = "valid_domain",
) -> list[dict]:
    """Gate declared numeric domains; absent domain/evaluation points stay unknown.

    A domain is ``{name: {min: number, max: number, unit: str}}``. A condition
    is ``{value: number, unit: str}``. An explicit coordinate axis can supply the
    evaluation points instead of a scalar condition. No extrapolation is done.
    """
    issues: list[dict] = []
    if not isinstance(valid_domain, dict) or not valid_domain:
        return [_issue("missing_valid_domain", "Model applicability is not declared.", path)]
    axis_map = {
        axis["name"]: axis
        for axis in (axes or [])
        if isinstance(axis, dict) and isinstance(axis.get("name"), str)
    }
    for name, limits in valid_domain.items():
        item_path = f"{path}.{name}"
        if not (
            isinstance(limits, dict)
            and _finite_number(limits.get("min"))
            and _finite_number(limits.get("max"))
            and limits["min"] <= limits["max"]
            and isinstance(limits.get("unit"), str)
            and bool(limits["unit"])
        ):
            issues.append(
                _issue(
                    "invalid_domain",
                    "Domain needs ordered finite bounds and a unit.",
                    item_path,
                    "error",
                )
            )
            continue
        coordinate, condition = axis_map.get(name), conditions.get(name)
        if condition is None and coordinate is None:
            issues.append(
                _issue("missing_domain_condition", f"No evaluation point for {name}.", item_path)
            )
            continue
        sources = []
        if condition is not None:
            if not _quantity(condition) or not _finite_number(condition.get("value")):
                issues.append(
                    _issue(
                        "invalid_domain_condition",
                        "Domain condition is not numeric.",
                        item_path,
                        "error",
                    )
                )
            else:
                sources.append((condition["unit"], [condition["value"]]))
        if coordinate is not None:
            points, unit = coordinate.get("values"), coordinate.get("unit")
            if not isinstance(points, list) or not points or not all(map(_finite_number, points)):
                issues.append(
                    _issue(
                        "invalid_domain_axis",
                        "Domain axis needs finite coordinates.",
                        item_path,
                        "error",
                    )
                )
            else:
                sources.append((unit, points))
        for unit, points in sources:
            if unit != limits["unit"]:
                issues.append(
                    _issue(
                        "domain_unit_mismatch",
                        "Domain and condition units differ.",
                        item_path,
                        "error",
                    )
                )
            elif any(point < limits["min"] or point > limits["max"] for point in points):
                issues.append(
                    _issue(
                        "outside_valid_domain",
                        f"{name} is outside model applicability.",
                        item_path,
                        "out_of_domain",
                    )
                )
    return issues


def _uncertainty(record: dict, count: int, shape: tuple, path: str) -> tuple[list | None, list]:
    budget = record.get("uncertainty_budget")
    if isinstance(budget, dict) and budget.get("total_uncertainty", {}).get("kind") == "unknown":
        return None, [_issue(
            "incomplete_uncertainty_budget",
            "Conditional input bounds leave total prediction uncertainty unknown.", path,
        )]
    uncertainty = record.get("uncertainty")
    if uncertainty is None or (
        isinstance(uncertainty, dict) and uncertainty.get("kind") == "unknown"
    ):
        return None, [_issue("missing_uncertainty", "Uncertainty has not been supplied.", path)]
    if not isinstance(uncertainty, dict) or uncertainty.get("kind") != "absolute_bound":
        return None, [
            _issue(
                "unsupported_uncertainty",
                "Declare absolute_bound uncertainty explicitly; "
                "standard deviations are not hard bounds.",
                path,
                "error",
            )
        ]
    if not uncertainty.get("unit"):
        return None, [
            _issue("missing_uncertainty_unit", "Uncertainty needs an explicit unit.", path)
        ]
    if uncertainty["unit"] != record.get("unit"):
        return None, [
            _issue(
                "uncertainty_unit_mismatch", "Uncertainty and metric units differ.", path, "error"
            )
        ]
    if "value" in uncertainty:
        value = uncertainty["value"]
        if _finite_number(value) and value >= 0:
            return [float(value)] * count, []
    elif "values" in uncertainty:
        try:
            values, uncertainty_shape = _array(uncertainty["values"])
            if uncertainty_shape == shape and all(value >= 0 for value in values):
                return values, []
        except ValueError:
            pass
    return None, [
        _issue(
            "invalid_uncertainty",
            "Bounds must be finite, nonnegative and have the "
            "same shape as the metric, or be an explicit scalar value.",
            path,
            "error",
        )
    ]


def _dataset_ids(record: dict, field: str, path: str, *, required: bool) -> tuple[set, list]:
    if field not in record:
        return set(), (
            [_issue("missing_dataset_lineage", f"Declare {field} explicitly.", path)]
            if required
            else []
        )
    values = record[field]
    if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
        return set(), [
            _issue(
                "invalid_dataset_lineage",
                "Dataset identities must be string arrays.",
                path,
                "error",
            )
        ]
    return set(values), []


def _temporal_frames(record: dict, name: str) -> tuple[list | None, list]:
    """Check the frozen frame-condition projection, not a hardware timing schedule."""
    from camera_platform.timing import MAX_EXPOSURE_NS, MAX_FRAMES, MAX_TIME_NS

    frames, issues = record.get("frame_conditions"), []
    path = f"{name}.frame_conditions"
    fields = {"frame_id", "frame_start_ns", "applied_at_ns", "exposure_ns", "gain", "isp_model_id"}
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        return None, [_issue("invalid_frame_conditions", "Declare 1..16 complete frame conditions.",
                             path, "error")]
    previous, seen, times = None, set(), []
    for index, frame in enumerate(frames):
        item_path = f"{path}[{index}]"
        if not isinstance(frame, dict) or set(frame) != fields:
            issues.append(_issue("invalid_frame_conditions", "Frame-condition fields differ from "
                                 "the recorded six-field contract.", item_path, "error"))
            continue
        valid = (
            all(isinstance(frame[key], str) and frame[key].strip() and len(frame[key]) <= 200
                for key in ("frame_id", "isp_model_id"))
            and all(type(frame[key]) is int and -MAX_TIME_NS <= frame[key] <= MAX_TIME_NS
                    for key in ("frame_start_ns", "applied_at_ns"))
            and type(frame["exposure_ns"]) is int
            and 1 <= frame["exposure_ns"] <= MAX_EXPOSURE_NS
            and _finite_number(frame["gain"]) and 1e-6 <= frame["gain"] <= 1e6
        )
        if not valid:
            issues.append(_issue("invalid_frame_conditions", "Frame identity, integer ns timing, "
                                 "exposure, gain and applied ISP must be explicit and bounded.",
                                 item_path, "error"))
            continue
        if frame["frame_id"] in seen:
            issues.append(_issue("duplicate_frame_id", "Frame IDs must be unique.",
                                 item_path + ".frame_id", "error"))
        seen.add(frame["frame_id"])
        if previous is not None and (frame["frame_start_ns"] <= previous["frame_start_ns"]
                                     or frame["applied_at_ns"] < previous["applied_at_ns"]):
            issues.append(_issue("invalid_frame_order", "Frame starts must increase and recorded "
                                 "application times cannot move backwards.", item_path, "error"))
        if previous is not None and frame["applied_at_ns"] == previous["applied_at_ns"]:
            if any(frame[key] != previous[key] for key in ("exposure_ns", "gain", "isp_model_id")):
                issues.append(_issue("conflicting_applied_event", "One applied event cannot "
                                     "declare different settings.", item_path, "error"))
        end = frame["frame_start_ns"] + frame["exposure_ns"]
        if end > MAX_TIME_NS:
            issues.append(_issue("frame_time_overflow", "Exposure end exceeds supported ns range.",
                                 item_path, "error"))
        times.extend([frame["frame_start_ns"], frame["applied_at_ns"], end])
        previous = frame
    if times and max(times) - min(times) > MAX_TIME_NS:
        issues.append(_issue("frame_time_overflow", "Recorded span exceeds exact ns range.",
                             path, "error"))
    return (None if issues else frames), issues


def _shared_charge_contract(specification: Any) -> dict:
    """Validate the complete frozen RAW sample identity, without inferring missing fields."""
    if not isinstance(specification, dict):
        raise ValueError("DCG Test requires a stimulus object")
    contract = specification.get("sample_contract")
    fields = {"schema", "topology", "capture_program_id", "capture_program_hash", "readout_ids",
              "raw_formats", "sampling", "raster_shape", "observation", "channels"}
    if not isinstance(contract, dict) or set(contract) != fields:
        raise ValueError("DCG sample contract requires every frozen field and no unknown fields")
    nonlinear = specification.get("schema") == "shared_charge_dcg_test_v2"
    expected_schema = ("camera_shared_charge_samples_v2" if nonlinear
                       else "camera_shared_charge_samples_v1")
    expected_observation = ("sampled_nonlinear_shared_charge_raw_black_relative_v1" if nonlinear
                            else "sampled_shared_charge_raw_dark_corrected_v1")
    supported = {"shared_charge_dcg_test_v1", "shared_charge_dcg_test_v2"}
    if (not isinstance(specification.get("schema"), str)
            or specification.get("schema") not in supported
            or contract["schema"] != expected_schema
            or contract["topology"] != "ideal_shared_charge_two_conversion_paths"
            or contract["readout_ids"] != ["HCG", "LCG"]
            or contract["observation"] != expected_observation
            or contract["channels"] not in (["R", "G", "B"], ["R", "Gr", "Gb", "B"])):
        raise ValueError("DCG topology, stream order, observation and sites must be explicit")
    identity, content_hash = contract["capture_program_id"], contract["capture_program_hash"]
    if (not isinstance(identity, str) or not identity.strip() or len(identity) > 200
            or not isinstance(content_hash, str) or len(content_hash) != 64
            or any(char not in "0123456789abcdef" for char in content_hash)
            or specification.get("capture_program_id") != identity
            or specification.get("capture_program_hash") != content_hash
            or specification.get("acquisition_topology") != contract["topology"]
            or specification.get("readout_ids") != contract["readout_ids"]):
        raise ValueError("DCG sample program identity and topology must match the frozen Test")
    formats = contract["raw_formats"]
    required = {"cfa", "bit_depth", "black_level_dn", "response_type", "packing", "layout",
                "hdr_streams"}
    if not isinstance(formats, list) or len(formats) != 2:
        raise ValueError("DCG requires two complete ordered RAW formats")
    for fmt in formats:
        if (not isinstance(fmt, dict) or set(fmt) != required
                or fmt["cfa"] != "RGGB"
                or fmt["response_type"] != ("nonlinear_charge_voltage" if nonlinear else "linear")
                or fmt["packing"] != "unpacked" or fmt["layout"] != "bayer"
                or type(fmt["hdr_streams"]) is not int or fmt["hdr_streams"] != 1
                or type(fmt["bit_depth"]) is not int or not 1 <= fmt["bit_depth"] <= 24
                or not _finite_number(fmt["black_level_dn"])
                or not 0 <= fmt["black_level_dn"] < 2**fmt["bit_depth"] - 1):
            raise ValueError("DCG RAW paths require version-matched single-stream "
                             "native RGGB formats")
    if formats[0]["bit_depth"] != formats[1]["bit_depth"]:
        raise ValueError("DCG RAW paths must have the same ADC bit depth")
    sampling = contract["sampling"]
    if (not isinstance(sampling, dict) or set(sampling) != {
            "kind", "full_sensor_shape", "crop_origin", "pixel_pitch_um"}
            or sampling["kind"] != "native_sensor"
            or not _finite_number(sampling["pixel_pitch_um"])
            or not .1 <= sampling["pixel_pitch_um"] <= 1000):
        raise ValueError("DCG sampling requires exact native geometry and finite pixel pitch")
    pairs = [(contract["raster_shape"], 8, 512), (sampling["full_sensor_shape"], 2, 1_000_000),
             (sampling["crop_origin"], 0, 1_000_000)]
    if any(not isinstance(pair, list) or len(pair) != 2
           or any(type(value) is not int or not low <= value <= high for value in pair)
           for pair, low, high in pairs):
        raise ValueError("DCG raster, full sensor and crop origin require bounded integer pairs")
    shape, full, origin = (pair for pair, _, _ in pairs)
    if (any(size % 2 for size in shape) or math.prod(full) > 16_777_216
            or any(start + size > maximum for start, size, maximum in zip(
                origin, shape, full, strict=True))):
        raise ValueError("DCG even native raster must fit its full sensor without resampling")
    return contract


def _nonlinear_sequence_contract(specification):
    contract = specification.get("sample_contract")
    fields = {"schema", "capture_program_id", "capture_program_hash", "raw_format",
              "sampling", "raster_shape", "observation", "channels"}
    if not isinstance(contract, dict) or set(contract) != fields:
        raise ValueError("Nonlinear RAW requires every frozen sample contract field")
    if (contract["schema"] != "camera_nonlinear_raw_samples_v1"
            or contract["observation"] != "sampled_nonlinear_raw_black_relative_v1"
            or contract["channels"] not in (["R", "G", "B"], ["R", "Gr", "Gb", "B"])):
        raise ValueError("Nonlinear sample observation and sites must be explicit")
    identity, content_hash = contract["capture_program_id"], contract["capture_program_hash"]
    if (not isinstance(identity, str) or not identity.strip() or len(identity) > 200
            or not isinstance(content_hash, str) or len(content_hash) != 64
            or any(char not in "0123456789abcdef" for char in content_hash)
            or specification.get("capture_program_id") != identity
            or specification.get("capture_program_hash") != content_hash):
        raise ValueError("Nonlinear sample identity must match the frozen Test program")
    fmt = contract["raw_format"]
    fields = {"cfa", "bit_depth", "black_level_dn", "response_type", "packing", "layout",
              "hdr_streams"}
    if (not isinstance(fmt, dict) or set(fmt) != fields or fmt["cfa"] != "RGGB"
            or fmt["response_type"] != "nonlinear_charge_voltage"
            or fmt["packing"] != "unpacked" or fmt["layout"] != "bayer"
            or type(fmt["hdr_streams"]) is not int or fmt["hdr_streams"] != 1
            or type(fmt["bit_depth"]) is not int or not 1 <= fmt["bit_depth"] <= 24
            or not _finite_number(fmt["black_level_dn"])
            or not 0 <= fmt["black_level_dn"] < 2**fmt["bit_depth"] - 1):
        raise ValueError("Nonlinear sample RAW format must remain explicit and version-matched")
    sampling = contract["sampling"]
    if (not isinstance(sampling, dict) or set(sampling) != {
            "kind", "full_sensor_shape", "crop_origin", "pixel_pitch_um"}
            or sampling["kind"] != "native_sensor"
            or not _finite_number(sampling["pixel_pitch_um"])
            or not .1 <= sampling["pixel_pitch_um"] <= 1000):
        raise ValueError("Nonlinear sample sampling requires exact native geometry")
    pairs = [(contract["raster_shape"], 8, 512), (sampling["full_sensor_shape"], 2, 1_000_000),
             (sampling["crop_origin"], 0, 1_000_000)]
    if any(not isinstance(pair, list) or len(pair) != 2
           or any(type(value) is not int or not low <= value <= high for value in pair)
           for pair, low, high in pairs):
        raise ValueError("Nonlinear native shape requires bounded integer pairs")
    shape, full, origin = (pair for pair, _, _ in pairs)
    if (any(size % 2 for size in shape) or math.prod(full) > 16_777_216
            or any(start + size > maximum for start, size, maximum in zip(
                origin, shape, full, strict=True))):
        raise ValueError("Nonlinear native raster must fit its sensor without resampling")
    return contract


def _retained_metadata(records, frame_records, axes_records, issues):
    """Delivered-history samples never inherit a current-exposure observation contract."""
    from .hdr_runtime import ILLUMINATION_COMPONENT_SCHEMA
    from .registry import digest
    from .retained_charge_runtime import EVENT_CONTRACT, METRIC_ID, OBSERVATION

    spec = records[2][1].get("stimulus", {})
    contract = spec.get("sample_contract") if isinstance(spec, dict) else None
    fields = {"schema", "capture_program_id", "capture_program_hash", "topology", "readout_ids",
              "channels", "raw_formats", "sampling", "raster_shape", "observation", "events"}
    try:
        if (not isinstance(spec, dict) or spec.get("schema") not in {
                "retained_charge_test_v1", "retained_charge_components_test_v1"}
                or spec.get("observation_method") != OBSERVATION
                or spec.get("dark_correction") != "not_applied_to_delivered_code_metric"
                or not isinstance(contract, dict) or set(contract) != fields
                or contract["schema"] != "camera_retained_delivered_samples_v1"
                or contract["topology"] != "retained_pd_transfer_to_fresh_read_node"
                or contract["readout_ids"] not in (["single"], ["HCG", "LCG"])
                or contract["channels"] not in (["R", "G", "B"], ["R", "Gr", "Gb", "B"])
                or contract["observation"] != OBSERVATION):
            raise ValueError("Retained samples require their complete delivered-history contract")
        component_fields = {
            "unmodulated_scene_sequence_id", "unmodulated_scene_sequence_hash",
            "illumination_component_schema",
        }
        if spec["schema"] == "retained_charge_components_test_v1":
            identity, content_hash = (spec.get("unmodulated_scene_sequence_id"),
                                      spec.get("unmodulated_scene_sequence_hash"))
            if (not isinstance(identity, str) or not identity
                    or not isinstance(content_hash, str) or len(content_hash) != 64
                    or any(c not in "0123456789abcdef" for c in content_hash)
                    or spec.get("illumination_component_schema")
                    != ILLUMINATION_COMPONENT_SCHEMA):
                raise ValueError("Retained two-component Test requires its second source hash")
        elif component_fields & set(spec):
            raise ValueError("Single-component retained Test cannot bind a second light source")
        # Reuse strict native geometry and ADC field checks without changing the
        # retained formats stored in Test, prediction or measurement.
        formats = contract["raw_formats"]
        if not isinstance(formats, list) or len(formats) != len(contract["readout_ids"]):
            raise ValueError("Delivered readout formats must match each declared path")
        for fmt in formats:
            if not isinstance(fmt, dict) or fmt.get("response_type") not in (
                "linear",
                "nonlinear_charge_voltage",
            ):
                raise ValueError("Delivered RAW preserves its explicit response type")
            surrogate = {
                key: contract[key]
                for key in (
                    "capture_program_id",
                    "capture_program_hash",
                    "sampling",
                    "raster_shape",
                    "channels",
                )
            }
            surrogate.update(schema="camera_nonlinear_raw_samples_v1",
                             observation="sampled_nonlinear_raw_black_relative_v1",
                             raw_format={**fmt, "response_type": "nonlinear_charge_voltage"})
            _nonlinear_sequence_contract({**spec, "sample_contract": surrogate})
        shape = contract["raster_shape"]
        if (contract["sampling"]["crop_origin"] != [0, 0]
                or contract["sampling"]["full_sensor_shape"] != shape):
            raise ValueError("Retained samples require the full native sensor")
        events = contract["events"]
        if (
            not isinstance(events, dict)
            or set(events)
            != {
                "schema",
                "clock",
                "event_contract",
                "full_sensor_shape",
                "initial_row_time_ns",
                "frames",
                "schedule_sha256",
            }
            or events["schema"] != "camera_retained_row_events_v1"
            or events["event_contract"] != EVENT_CONTRACT
            or events["full_sensor_shape"] != shape
            or events["clock"] != records[2][1].get("clock")
            or events["schedule_sha256"]
            != digest({k: v for k, v in events.items() if k != "schedule_sha256"})
        ):
            raise ValueError("Retained row event schedule must retain its exact hash and clock")
        frames = frame_records.get("test_spec", [])
        if not frames:
            frames = next(
                (value for key, value in frame_records.items() if key.startswith("test")), []
            )
        if not isinstance(events["frames"], list) or len(events["frames"]) != len(frames):
            raise ValueError("Retained row event schedule must cover every frame")
        previous = None
        for event, frame in zip(events["frames"], frames, strict=True):
            if (
                not isinstance(event, dict)
                or set(event)
                != {
                    "frame_id",
                    "row_open_ns",
                    "row_transfer_ns",
                    "row_clear_ns",
                    "row_gap_ns",
                    "clear_before_exposure",
                }
                or event["frame_id"] != frame["frame_id"]
                or type(event["clear_before_exposure"]) is not bool
            ):
                raise ValueError("Retained event frame identity and clear policy must be exact")
            for key in ("row_open_ns", "row_transfer_ns", "row_gap_ns"):
                values = event[key]
                if (not isinstance(values, list) or len(values) != shape[0]
                        or any(type(v) is not int or abs(v) > 2**53 - 1 for v in values)):
                    raise ValueError("Retained row times require bounded integer ns")
            starts, ends = event["row_open_ns"], event["row_transfer_ns"]
            gaps = (
                [0] * shape[0]
                if previous is None
                else [a - b for a, b in zip(starts, previous, strict=True)]
            )
            if (
                event["row_gap_ns"] != gaps
                or min(gaps) < 0
                or any(b - a != frame["exposure_ns"] for a, b in zip(starts, ends, strict=True))
                or min(starts) < frame["frame_start_ns"]
                or event["row_clear_ns"] != (starts if event["clear_before_exposure"] else None)
            ):
                raise ValueError(
                    "Retained row exposure/gap/clear events must match applied conditions"
                )
            previous = ends
        if (
            not events["frames"]
            or events["initial_row_time_ns"] != events["frames"][0]["row_open_ns"]
            or len(frames) * math.prod(shape) > 1_048_576
        ):
            raise ValueError("Retained initial clock and sequence pixel budget must be explicit")
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        issues.append(_issue("invalid_retained_sample_contract", str(exc),
                             "test_spec.stimulus.sample_contract", "error"))
        contract = None
    for name, record in records:
        if record.get("metric_id") != METRIC_ID:
            issues.append(
                _issue(
                    "retained_observation_mismatch",
                    "Every retained observation must use the dedicated delivered signal metric.",
                    name + ".metric_id",
                    "error",
                )
            )
    for name, record in records[:2]:
        if (record.get("method_and_version") != OBSERVATION or contract is None
                or record.get("sample_geometry") != contract
                or record.get("dark_correction", "not_applied_to_delivered_code_metric")
                   != "not_applied_to_delivered_code_metric"):
            issues.append(
                _issue(
                    "retained_sample_contract_mismatch",
                    "Delivered samples must preserve "
                    "the fixed program, full native geometry and complete row history.",
                    name,
                    "error",
                )
            )
        if contract is not None:
            expected = [
                {
                    "name": "frame",
                    "unit": "1",
                    "values": [f["frame_id"] for f in frame_records.get(name, [])],
                },
                {"name": "readout", "unit": "1", "values": contract["readout_ids"]},
                {"name": "channel", "unit": "1", "values": contract["channels"]},
            ]
            if axes_records.get(name) != expected:
                issues.append(_issue("temporal_axis_mismatch", "Retained samples require ordered "
                                     "frame/readout/channel coordinates.", name + ".axes", "error"))
            if "raw_formats" in record and record["raw_formats"] != contract["raw_formats"]:
                issues.append(
                    _issue(
                        "retained_sample_contract_mismatch",
                        "Delivered RAW formats must retain their fixed response type.",
                        name + ".raw_formats",
                        "error",
                    )
                )
    return frame_records, issues


def _linear_cropped_sequence_geometry(specification: dict) -> dict:
    """Validate the receiver/source coordinates frozen by a linear cropped Test.

    This is a comparison-coordinate declaration. Matching it does not prove a
    measured image had a simulated ideal-image halo or validate optical physics.
    """
    if not isinstance(specification, dict):
        raise ValueError("Cropped temporal Test requires a stimulus object")
    capture = specification.get("capture_crop")
    source = specification.get("source_support")
    if (not isinstance(capture, dict) or set(capture) != {"crop_origin", "shape"}
            or not isinstance(source, dict) or set(source) != {
                "kind", "full_sensor_shape", "crop_origin", "shape"
            } or source["kind"] != "native_sensor"):
        raise ValueError("Cropped temporal Test requires exact receiver and source geometry")

    def pair(value, minimum, maximum, *, even=False):
        if (not isinstance(value, list) or len(value) != 2
                or any(type(item) is not int or not minimum <= item <= maximum
                       or (even and item % 2) for item in value)):
            raise ValueError("Cropped temporal geometry requires bounded integer coordinate pairs")
        return value

    full = pair(source["full_sensor_shape"], 2, 1_000_000)
    source_origin = pair(source["crop_origin"], 0, 1_000_000)
    source_shape = pair(source["shape"], 8, 512, even=True)
    receiver_origin = pair(capture["crop_origin"], 0, 1_000_000)
    receiver_shape = pair(capture["shape"], 8, 512, even=True)
    if (any(start + size > native for start, size, native in zip(
            source_origin, source_shape, full, strict=True))
            or any(not start <= receiver_start
                   or receiver_start + receiver_size > start + size
                   for start, size, receiver_start, receiver_size in zip(
                       source_origin, source_shape, receiver_origin, receiver_shape, strict=True))):
        raise ValueError("Cropped temporal receiver must lie inside its native source support")
    return {"capture_crop": deepcopy(capture), "source_support": deepcopy(source)}


def _temporal_metadata(records: list, conditions: dict, axes_records: dict) -> tuple[dict, list]:
    """Require identical complete temporal coordinates in prediction/reference/test."""
    issues, clocks, frame_records = [], [], {}
    for name, record in records:
        if record.get("metric_id") not in {
            "raw.channel_mean_dn_sequence", "raw.channel_mean_dn_shared_charge",
            "raw.retained_delivered_channel_mean_dn_sequence"
        }:
            issues.append(_issue("unsupported_temporal_metric", "Timing metadata currently "
                                 "requires a registered ordered RAW metric.", f"{name}.metric_id",
                                 "error"))
        if record.get("unit") != "DN":
            issues.append(_issue("temporal_metric_unit", "Temporal RAW channel means require DN.",
                                 f"{name}.unit", "error"))
        clock = record.get("clock")
        if not (isinstance(clock, dict)
                and set(clock) == {"clock_id", "unit", "reference", "origin"}
                and clock.get("unit") == "ns" and clock.get("reference") == "relative"
                and all(isinstance(clock.get(key), str) and clock[key].strip()
                        and len(clock[key]) <= limit for key, limit in (("clock_id", 200),
                                                                      ("origin", 2000)))):
            issues.append(_issue("invalid_temporal_clock", "Declare the complete relative ns "
                                 "clock and origin.", f"{name}.clock", "error"))
        else:
            clocks.append(clock)
        frames, frame_issues = _temporal_frames(record, name)
        issues.extend(frame_issues)
        if frames is not None:
            frame_records[name] = frames
        if any(key in conditions[name] for key in ("exposure_s", "exposure_ns", "gain")):
            issues.append(_issue("global_temporal_setting", "Exposure and gain must be recorded "
                                 "per frame, not repeated as global conditions.",
                                 f"{name}.conditions", "error"))
    if clocks and any(clock != clocks[0] for clock in clocks):
        issues.append(_issue("temporal_clock_mismatch", "Clock identity, reference and origin "
                             "must match exactly; implicit synchronization is forbidden.",
                             "clock", "error"))
    values = list(frame_records.values())
    if values and any(frames != values[0] for frames in values):
        issues.append(_issue("frame_conditions_mismatch", "Ordered frame IDs, actual applied "
                             "settings and timing must match prediction, measurement and test.",
                             "frame_conditions", "error"))
    specification = records[2][1].get("stimulus", {})
    if (any(r.get("metric_id") == "raw.retained_delivered_channel_mean_dn_sequence"
            or r.get("method_and_version") == "sampled_retained_delivered_raw_black_relative_v1"
            for _, r in records) or (isinstance(specification, dict)
                                    and specification.get("schema") in {
                                        "retained_charge_test_v1",
                                        "retained_charge_components_test_v1",
                                    })):
        return _retained_metadata(records, frame_records, axes_records, issues)
    nonlinear = isinstance(specification, dict) and specification.get("schema") in (
        "shared_charge_dcg_test_v2", "electrical_hdr_test_v2", "reference_hdr_test_v2",
        "electrical_hdr_components_test_v2",
    )
    nonlinear_methods = ("sampled_nonlinear_raw_black_relative_v1",
                         "sampled_nonlinear_shared_charge_raw_black_relative_v1")
    if not nonlinear and any(r.get("method_and_version") in nonlinear_methods
                             for _, r in records[:2]):
        issues.append(_issue("invalid_nonlinear_sample_contract", "Nonlinear RAW observations "
                             "require their versioned fixed Test and sample contract.",
                             "test_spec.stimulus", "error"))
    sample_contract = None
    if nonlinear:
        paired = specification["schema"] == "shared_charge_dcg_test_v2"
        expected_method = ("sampled_nonlinear_shared_charge_raw_black_relative_v1" if paired
                           else "sampled_nonlinear_raw_black_relative_v1")
        if (specification.get("observation_method") != expected_method
                or specification.get("dark_correction") != "not_applied_to_raw_code_metric"):
            issues.append(_issue("nonlinear_observation_mismatch", "Nonlinear Test requires "
                                 "explicit black-relative RAW observation with dark retained.",
                                 "test_spec.stimulus", "error"))
        if not paired:
            try:
                sample_contract = _nonlinear_sequence_contract(specification)
            except ValueError as exc:
                issues.append(_issue("invalid_nonlinear_sample_contract", str(exc),
                                     "test_spec.stimulus.sample_contract", "error"))
        for name, record in records[:2]:
            if (record.get("method_and_version") != expected_method
                    or ("dark_correction" in record and record["dark_correction"]
                        != "not_applied_to_raw_code_metric")):
                issues.append(_issue("nonlinear_observation_mismatch", "Nonlinear RAW samples "
                                     "must preserve the declared black-relative observation.",
                                     name, "error"))
            if (not paired and (sample_contract is None
                                or record.get("sample_geometry") != sample_contract)):
                issues.append(_issue("nonlinear_sample_contract_mismatch", "Nonlinear RAW "
                                     "samples must match the fixed program, format and geometry.",
                                     name + ".sample_geometry", "error"))
            if (not paired and sample_contract is not None and "raw_format" in record
                    and record["raw_format"] != sample_contract["raw_format"]):
                issues.append(_issue("nonlinear_raw_format_mismatch", "Nonlinear RAW format "
                                     "must match the fixed Test without linear relabeling.",
                                     name + ".raw_format", "error"))
    linear_schemas = {
        "temporal_scene_test_v1", "electrical_hdr_test_v1",
        "electrical_hdr_components_test_v1",
    }
    if (isinstance(specification, dict) and specification.get("schema") in linear_schemas
            and ("capture_crop" in specification or "source_support" in specification)):
        try:
            crop_geometry = _linear_cropped_sequence_geometry(specification)
        except ValueError as exc:
            issues.append(_issue("invalid_linear_crop_contract", str(exc),
                                 "test_spec.stimulus", "error"))
        else:
            for name, record in records[:2]:
                if record.get("sample_geometry") != crop_geometry:
                    issues.append(_issue(
                        "linear_crop_geometry_mismatch",
                        "Linear temporal samples must declare the Test receiver ROI and "
                        "native source-support coordinates exactly.",
                        name + ".sample_geometry", "error",
                    ))
    dcg = any(r.get("metric_id") == "raw.channel_mean_dn_shared_charge" for _, r in records)
    contract = None
    if dcg:
        try:
            contract = _shared_charge_contract(records[2][1].get("stimulus"))
        except ValueError as exc:
            issues.append(_issue("invalid_shared_charge_contract", str(exc),
                                 "test_spec.stimulus.sample_contract", "error"))
        for name, record in records[:2]:
            if contract is None or record.get("sample_geometry") != contract:
                issues.append(_issue("shared_charge_contract_mismatch", "Prediction and "
                                     "reference must preserve the same program, two RAW "
                                     "formats and native sampling; no stream inference.",
                                     f"{name}.sample_geometry", "error"))
            if (contract is not None and "raw_formats" in record
                    and record["raw_formats"] != contract["raw_formats"]):
                issues.append(_issue("shared_charge_contract_mismatch", "Declared RAW formats "
                                     "conflict with the frozen paired sample contract.",
                                     f"{name}.raw_formats", "error"))
        for name, frames in frame_records.items():
            if any(frame["gain"] != 1 for frame in frames):
                issues.append(_issue("invalid_shared_charge_gain", "DCG path gains are fixed "
                                     "by the program; applied acquisition gain must equal one.",
                                     f"{name}.frame_conditions", "error"))
            if (contract is not None
                    and len(frames) * math.prod(contract["raster_shape"]) > 1_048_576):
                issues.append(_issue("invalid_shared_charge_contract", "DCG acquisition pixels "
                                     "exceed the declared temporal capture domain.",
                                     "test_spec.stimulus.sample_contract.raster_shape", "error"))
    for name, _ in records[:2]:
        if name not in frame_records:
            continue
        axes = axes_records.get(name)
        dimensions = 3 if dcg else 2
        channels = (axes[-1].get("values")
                    if isinstance(axes, list) and len(axes) == dimensions else None)
        supported = (contract is not None and channels == contract["channels"] if dcg
                     else sample_contract is not None and channels == sample_contract["channels"]
                     if nonlinear else channels in (["R", "G", "B"], ["R", "Gr", "Gb", "B"]))
        if (nonlinear and not dcg and sample_contract is not None
                and len(frame_records[name]) * math.prod(sample_contract["raster_shape"])
                > 1_048_576):
            issues.append(_issue("invalid_nonlinear_sample_contract", "Nonlinear frame pixels "
                                 "exceed the temporal acquisition budget.",
                                 "test_spec.stimulus.sample_contract.raster_shape", "error"))
        expected = [{"name": "acquisition" if dcg else "frame", "unit": "1",
                     "values": [frame["frame_id"] for frame in frame_records[name]]},
                    *([{"name": "readout", "unit": "1", "values": ["HCG", "LCG"]}]
                      if dcg else []),
                    {"name": "channel", "unit": "1", "values": channels}]
        if not supported or axes != expected:
            issues.append(_issue("temporal_axis_mismatch", "Temporal samples require ordered "
                                 "frame IDs and exact RGB or native R/Gr/Gb/B site channels "
                                 "with dimensionless axes; no implicit green-site merging.",
                                 f"{name}.axes", "error"))
    return frame_records, issues


def _temporal_domains(prediction: dict, frames: list, static: dict, axes: list) -> list:
    issues = []
    if "frame_valid_domains" in prediction:
        domains = prediction["frame_valid_domains"]
        if not (isinstance(domains, list) and len(domains) == len(frames)
                and all(isinstance(item, dict) and set(item) == {"frame_id", "valid_domain"}
                        and item["frame_id"] == frame["frame_id"]
                        for item, frame in zip(domains, frames, strict=True))):
            return [_issue("invalid_frame_valid_domains", "Declared per-frame domains must "
                           "cover every frame exactly in order; no global-domain fallback.",
                           "prediction.frame_valid_domains", "error")]
    else:
        domains = [{"frame_id": frame["frame_id"], "valid_domain": prediction.get("valid_domain")}
                   for frame in frames]
    for index, (frame, domain) in enumerate(zip(frames, domains, strict=True)):
        conditions = {**static,
                      "exposure_s": {"value": frame["exposure_ns"] / 1_000_000_000, "unit": "s"},
                      "gain": {"value": frame["gain"], "unit": "1"}}
        issues.extend(check_domain(conditions, domain["valid_domain"], axes=axes,
                                   path=f"prediction.frame_valid_domains[{index}].valid_domain"))
    return issues


def compare(prediction: dict, measurement: dict, test_spec: dict) -> dict:
    if (test_spec.get("observation_method") or {}).get("id") == "control_response_observation_v1":
        from .control_comparison import compare_response

        return compare_response(prediction, measurement, test_spec)
    return _compare_metric(prediction, measurement, test_spec)


def _compare_metric(prediction: dict, measurement: dict, test_spec: dict) -> dict:
    """Compare matched metric samples without promoting numerical agreement to evidence.

    Each record declares metric_id, unit, values, axes, reference_plane,
    included_effects, conditions, source_kind, method_and_version and uncertainty.
    Prediction additionally declares valid_domain and calibration_dataset_ids.
    A physical measurement requires physical_instance and dataset_ids.

    TestSpecification pins those semantics plus ``absolute_tolerance={value,unit}``
    and validation_kind (defaults conservatively to physical_validation). A point
    passes only when its entire bounded residual interval fits the tolerance;
    it fails only when that interval lies entirely outside. An overlap with the
    tolerance boundary is INCONCLUSIVE. Bounds add without assuming independence.
    """
    issues: list[dict] = []
    kind = test_spec.get("validation_kind", "physical_validation")
    if kind not in VALIDATION_KINDS:
        issues.append(
            _issue(
                "invalid_validation_kind",
                "Unknown evidence category.",
                "test_spec.validation_kind",
                "error",
            )
        )
    report = {
        "schema_version": "1.0",
        "prediction_id": prediction.get("id"),
        "measurement_id": measurement.get("id"),
        "uncertainty_budgets": {
            "prediction": deepcopy(prediction.get("uncertainty_budget")),
            "measurement": deepcopy(measurement.get("uncertainty_budget")),
        },
        "test_spec_id": test_spec.get("id"),
        "validation_kind": kind,
        "validation_label": VALIDATION_KINDS.get(kind, "Unknown"),
        "status": "INCONCLUSIVE",
        "issues": issues,
        "independent_validation": False,
        "residuals": None,
        "summary": None,
    }
    records = [("prediction", prediction), ("measurement", measurement), ("test_spec", test_spec)]
    stimulus = test_spec.get("stimulus")
    if prediction.get("acquisition_mode") == "closed_loop" or (
        isinstance(stimulus, dict) and stimulus.get("schema") in {
            "closed_loop_scene_test_v1", "electrical_control_test_v1"
        }
    ):
        issues.append(_issue(
            "closed_loop_comparison_operator_required",
            "Closed-loop RAW observations are conditional on generated applied controls. "
            "This comparator requires a recorded-settings test with matching acquisition, "
            "or a separately defined trajectory observation operator; initial settings cannot "
            "stand in for all frames.", "test_spec.stimulus", "unknown"))
        report.update(
            comparison_valid=False, physical_validation_pass=False,
            claim=("No physical validation pass; "
                   "a matching acquisition comparison operator is required."),
        )
        return report
    temporal = any(record.get("metric_id") in {
                       "raw.channel_mean_dn_sequence", "raw.channel_mean_dn_shared_charge",
            "raw.retained_delivered_channel_mean_dn_sequence"}
                   or record.get("clock") is not None or record.get("frame_conditions") is not None
                   for _, record in records) or "frame_valid_domains" in prediction
    for key in ("metric_id", "unit", "reference_plane"):
        known = []
        for name, record in records:
            value = record.get(key)
            if not isinstance(value, str) or not value:
                issues.append(
                    _issue(f"missing_{key}", f"Declare {key} explicitly.", f"{name}.{key}")
                )
            else:
                known.append(value)
        if len(set(known)) > 1:
            issues.append(
                _issue(
                    f"{key}_mismatch",
                    f"{key} differs; an explicit transformation "
                    "and corresponding test definition are required.",
                    key,
                    "error",
                )
            )

    effect_sets = []
    for name, record in records:
        effects = record.get("included_effects")
        if not isinstance(effects, list):
            issues.append(
                _issue(
                    "missing_effects",
                    "Declare included effects, including an explicit empty list when appropriate.",
                    f"{name}.included_effects",
                )
            )
        elif not all(isinstance(effect, str) and effect for effect in effects):
            issues.append(
                _issue(
                    "invalid_effects",
                    "Effect identities must be nonempty strings.",
                    f"{name}.included_effects",
                    "error",
                )
            )
        else:
            effect_sets.append(set(effects))
            if len(effects) != len(set(effects)):
                issues.append(
                    _issue(
                        "duplicate_effect",
                        "A physical effect is included more than once.",
                        f"{name}.included_effects",
                        "error",
                    )
                )
    if effect_sets and any(effects != effect_sets[0] for effects in effect_sets):
        issues.append(
            _issue(
                "included_effects_mismatch",
                "Compared observations include different physical or processing effects.",
                "included_effects",
                "error",
            )
        )

    conditions: dict[str, dict] = {}
    for name, record in records:
        if not isinstance(record.get("conditions"), dict):
            issues.append(
                _issue(
                    "missing_conditions",
                    "Conditions must be declared explicitly.",
                    f"{name}.conditions",
                )
            )
        conditions[name] = (
            record.get("conditions") if isinstance(record.get("conditions"), dict) else {}
        )
    required = test_spec.get("required_conditions", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        issues.append(
            _issue(
                "invalid_required_conditions",
                "Required conditions must be a name list.",
                "test_spec.required_conditions",
                "error",
            )
        )
        required = []
    names = set(required).union(*(set(value) for value in conditions.values()))
    if temporal:
        names -= {"exposure_s", "gain"}
    for condition_name in sorted(names):
        values = []
        for name, _ in records:
            value = conditions[name].get(condition_name)
            path = f"{name}.conditions.{condition_name}"
            if value is None:
                issues.append(_issue("missing_condition", f"Missing {condition_name}.", path))
            elif not _quantity(value):
                issues.append(
                    _issue(
                        "invalid_condition",
                        "Condition requires a finite number or "
                        "nonempty text value and an explicit unit.",
                        path,
                        "error",
                    )
                )
            else:
                values.append((value["value"], value["unit"]))
        if values and any(value != values[0] for value in values):
            issues.append(
                _issue(
                    "condition_mismatch",
                    f"{condition_name} differs between records.",
                    f"conditions.{condition_name}",
                    "error",
                )
            )

    axes_records = {}
    for name, record in records[:2]:
        axes = record.get("axes")
        if not isinstance(axes, list):
            issues.append(
                _issue("missing_axes", "Metric coordinate axes must be explicit.", f"{name}.axes")
            )
            continue
        valid_axes = True
        axis_names = []
        for axis in axes:
            if not (
                isinstance(axis, dict)
                and isinstance(axis.get("name"), str)
                and axis["name"]
                and isinstance(axis.get("unit"), str)
                and axis["unit"]
                and isinstance(axis.get("values"), list)
                and axis["values"]
                and all(
                    _finite_number(value) or (isinstance(value, str) and bool(value))
                    for value in axis["values"]
                )
            ):
                valid_axes = False
            else:
                axis_names.append(axis["name"])
        if not valid_axes or len(set(axis_names)) != len(axis_names):
            issues.append(
                _issue(
                    "invalid_axes",
                    "Axes need unique names, units and finite/text coordinates.",
                    f"{name}.axes",
                    "error",
                )
            )
        else:
            axes_records[name] = axes
    if len(axes_records) == 2 and axes_records["prediction"] != axes_records["measurement"]:
        issues.append(
            _issue(
                "axis_mismatch",
                "Coordinates differ; implicit reordering or interpolation is forbidden.",
                "axes",
                "error",
            )
        )

    if temporal:
        frame_records, temporal_issues = _temporal_metadata(records, conditions, axes_records)
        issues.extend(temporal_issues)
        if "prediction" in frame_records:
            issues.extend(_temporal_domains(prediction, frame_records["prediction"],
                                            conditions["prediction"],
                                            axes_records.get("prediction", [])))
        if not temporal_issues:
            report["temporal_comparison"] = {
                "clock": deepcopy(prediction["clock"]),
                "frame_conditions": deepcopy(frame_records["prediction"]),
                "domain_policy": ("per_frame" if "frame_valid_domains" in prediction
                                  else "global_evaluated_per_frame"),
            }
    else:
        issues.extend(
            check_domain(
                conditions["prediction"],
                prediction.get("valid_domain"),
                axes=prediction.get("axes") if isinstance(prediction.get("axes"), list) else [],
                path="prediction.valid_domain",
            )
        )
    for name, record in records[:2]:
        for field in ("source_kind", "method_and_version"):
            if not record.get(field):
                issues.append(_issue("missing_provenance", f"Declare {field}.", f"{name}.{field}"))
    observation_methods = [record.get("observation_method") for _, record in records]
    if any(method is not None for method in observation_methods):
        if any(method is None for method in observation_methods):
            issues.append(
                _issue(
                    "missing_observation_method",
                    "The observation/analysis procedure "
                    "must be declared on both results and the test.",
                    "observation_method",
                )
            )
        elif any(method != observation_methods[0] for method in observation_methods):
            issues.append(
                _issue(
                    "observation_method_mismatch",
                    "Observation/analysis procedures differ.",
                    "observation_method",
                    "error",
                )
            )

    if kind == "physical_validation":
        if measurement.get("source_kind") != "measured":
            issues.append(
                _issue(
                    "not_physical_measurement",
                    "This reference is not an actual measured "
                    "dataset; numerical agreement cannot establish physical validation.",
                    "measurement.source_kind",
                )
            )
        if not measurement.get("physical_instance"):
            issues.append(
                _issue(
                    "missing_physical_instance",
                    "Measured camera lot/serial identity is required for physical validation.",
                    "measurement.physical_instance",
                )
            )
        measurement_ids, lineage_issues = _dataset_ids(
            measurement, "dataset_ids", "measurement.dataset_ids", required=True
        )
        issues.extend(lineage_issues)
        lineage_complete = not bool(lineage_issues)
        if not measurement_ids:
            issues.append(
                _issue(
                    "missing_measurement_dataset",
                    "An identified independent measurement dataset is required.",
                    "measurement.dataset_ids",
                )
            )
        calibration_ids, lineage_issues = _dataset_ids(
            prediction,
            "calibration_dataset_ids",
            "prediction.calibration_dataset_ids",
            required=True,
        )
        issues.extend(lineage_issues)
        lineage_complete = lineage_complete and not bool(lineage_issues)
        additional_ids, lineage_issues = _dataset_ids(
            test_spec,
            "calibration_dataset_ids",
            "test_spec.calibration_dataset_ids",
            required=False,
        )
        issues.extend(lineage_issues)
        lineage_complete = lineage_complete and not bool(lineage_issues)
        calibration_ids |= additional_ids
        overlap = sorted(measurement_ids & calibration_ids)
        if overlap:
            issues.append(
                _issue(
                    "calibration_dataset_overlap",
                    "Calibration data cannot be used as "
                    f"independent validation: {', '.join(overlap)}.",
                    "dataset_ids",
                    "error",
                )
            )
        report["independent_validation"] = bool(
            measurement_ids
            and not overlap
            and "calibration_dataset_ids" in prediction
            and measurement.get("source_kind") == "measured"
            and lineage_complete
        )
    elif kind == "cross_tool_comparison":
        if prediction.get("method_and_version") and prediction.get(
            "method_and_version"
        ) == measurement.get("method_and_version"):
            issues.append(
                _issue(
                    "same_tool_reference",
                    "A cross-tool comparison requires distinct tool identities/versions.",
                    "method_and_version",
                    "error",
                )
            )

    tolerance = test_spec.get("absolute_tolerance")
    tolerance_value = None
    if tolerance is None:
        issues.append(
            _issue(
                "missing_tolerance",
                "An acceptance tolerance must be declared.",
                "test_spec.absolute_tolerance",
            )
        )
    elif not (
        _quantity(tolerance) and _finite_number(tolerance.get("value")) and tolerance["value"] >= 0
    ):
        issues.append(
            _issue(
                "invalid_tolerance",
                "Tolerance needs a finite nonnegative value and unit.",
                "test_spec.absolute_tolerance",
                "error",
            )
        )
    elif tolerance["unit"] != test_spec.get("unit"):
        issues.append(
            _issue(
                "tolerance_unit_mismatch",
                "Tolerance and metric units differ.",
                "test_spec.absolute_tolerance.unit",
                "error",
            )
        )
    else:
        tolerance_value = float(tolerance["value"])
    if test_spec.get("uncertainty_policy", "bounded_interval") != "bounded_interval":
        issues.append(
            _issue(
                "unsupported_uncertainty_policy",
                "Only explicit bounded intervals are supported by this comparator.",
                "test_spec.uncertainty_policy",
                "error",
            )
        )

    numerical_status = "INCONCLUSIVE"
    try:
        if prediction.get("values") is None or measurement.get("values") is None:
            raise LookupError(
                "Prediction and measurement values are required to compute residuals."
            )
        predicted, shape = _array(prediction.get("values"))
        measured, measured_shape = _array(measurement.get("values"))
        if shape != measured_shape:
            raise ValueError("Prediction and measurement arrays must have exactly equal shapes.")
        for name, axes in axes_records.items():
            if axes:
                coordinate_count = math.prod(len(axis["values"]) for axis in axes)
                coordinate_shape = tuple(len(axis["values"]) for axis in axes)
                if coordinate_count != len(predicted) or (
                    len(shape) > 1 and shape != coordinate_shape
                ):
                    issues.append(
                        _issue(
                            "axis_shape_mismatch",
                            "Coordinate cardinality does not match metric sample count.",
                            f"{name}.axes",
                            "error",
                        )
                    )
            elif len(predicted) > 1:
                issues.append(
                    _issue(
                        "missing_sample_coordinates",
                        "Multiple samples require named coordinates to establish correspondence.",
                        f"{name}.axes",
                    )
                )
        residuals = [p - m for p, m in zip(predicted, measured, strict=True)]
        if not all(map(math.isfinite, residuals)):
            raise ValueError("Residual arithmetic overflowed; rescale values explicitly upstream.")
        report["residuals"] = {
            "values": residuals,
            "shape": list(shape),
            "unit": prediction.get("unit"),
            "axes": prediction.get("axes", []),
        }
        # hypot avoids overflow from squaring otherwise finite large residuals.
        report["summary"] = {
            "sample_count": len(residuals),
            "mean_signed_error": math.fsum(x / len(residuals) for x in residuals),
            "mae": math.fsum(abs(x) / len(residuals) for x in residuals),
            "rmse": math.hypot(*(x / math.sqrt(len(residuals)) for x in residuals)),
            "max_abs_error": max(map(abs, residuals)),
        }
        prediction_bounds, bound_issues = _uncertainty(
            prediction, len(predicted), shape, "prediction.uncertainty"
        )
        issues.extend(bound_issues)
        measurement_bounds, bound_issues = _uncertainty(
            measurement, len(measured), shape, "measurement.uncertainty"
        )
        issues.extend(bound_issues)
        if (
            prediction_bounds is not None
            and measurement_bounds is not None
            and tolerance_value is not None
        ):
            combined = [p + m for p, m in zip(prediction_bounds, measurement_bounds, strict=True)]
            lower = [max(0.0, abs(r) - u) for r, u in zip(residuals, combined, strict=True)]
            upper = [abs(r) + u for r, u in zip(residuals, combined, strict=True)]
            if not all(map(math.isfinite, combined + upper)):
                raise ValueError("Uncertainty arithmetic overflowed; rescale explicitly upstream.")
            point_status = [
                "PASS"
                if high <= tolerance_value
                else "FAIL"
                if low > tolerance_value
                else "INCONCLUSIVE"
                for low, high in zip(lower, upper, strict=True)
            ]
            numerical_status = (
                "FAIL"
                if "FAIL" in point_status
                else "INCONCLUSIVE"
                if "INCONCLUSIVE" in point_status
                else "PASS"
            )
            report["residuals"].update(
                {
                    "absolute_lower_bound": lower,
                    "absolute_upper_bound": upper,
                    "point_status": point_status,
                }
            )
    except LookupError as error:
        issues.append(_issue("missing_values", str(error), "values"))
    except (ValueError, OverflowError) as error:
        issues.append(_issue("invalid_values", str(error), "values", "error"))

    severities = {issue["severity"] for issue in issues}
    report["status"] = (
        "INVALID_COMPARISON"
        if "error" in severities
        else "OUT_OF_DOMAIN"
        if "out_of_domain" in severities
        else "INCONCLUSIVE"
        if "unknown" in severities
        else numerical_status
    )
    report["comparison_valid"] = not bool(severities & {"error", "out_of_domain", "unknown"})
    report["physical_validation_pass"] = (
        kind == "physical_validation" and report["status"] == "PASS"
    )
    report["claim"] = (
        "Independent physical comparison passed for this test and declared domain only."
        if report["physical_validation_pass"]
        else "This report does not establish an independent physical validation pass."
    )
    return report
