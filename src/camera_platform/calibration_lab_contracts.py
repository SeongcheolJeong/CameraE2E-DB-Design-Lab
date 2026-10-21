"""File-import contracts for the bounded Camera Calibration & Validation Lab.

Imported declarations are provenance, never certification of physical accuracy.
Numerical columns are explicit about units; unknown metadata is rejected.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA = "camera_calibration_lab_v1"
CHARTS = ("dark", "ptc", "shading", "mtf", "geometry", "color", "tone", "texture")
ROLES = ("training", "development", "final_validation")
FIELD_COORDINATE_DOMAINS = ("sensor_normalized", "reference_lens_object_direction")
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_POINTS = 10000
# Whole-chart studies are admitted by their retained input size, not by an
# arbitrary number of positions. These budgets are shared by API and engine.
MAX_STUDY_IDS_BYTES = 1024 * 1024
MAX_MEASUREMENT_BYTES = 64 * 1024 * 1024
MAX_OBSERVATION_SCALARS = 2_000_000
DEFAULT_CONDITIONS = {
    "mode_id": "linear-rggb",
    "gain": 1.0,
    "temperature_c": 25.0,
    "light_id": "D65",
    "aperture_f_number": 4.0,
    "focus_distance_m": 2.0,
}
FIELDS = {
    "dark": {"exposure_s": "s", "mean_dn": "DN", "variance_dn2": "DN^2"},
    "ptc": {"mean_dn": "DN", "variance_dn2": "DN^2", "exposure_s": "s", "clipped_fraction": "1"},
    "shading": {"field_radius": "1", "relative_response": "1"},
    "mtf": {"frequency_cy_px": "cy/px", "mtf": "1"},
    "geometry": {"object_x": "1", "object_y": "1", "image_x": "1", "image_y": "1"},
    "color": {**{f"reference_{c}": "1" for c in "rgb"}, **{f"measured_{c}": "1" for c in "rgb"}},
    "tone": {"input_level": "1", "output_level": "1"},
    "texture": {"frequency_cy_px": "cy/px", "response": "1"},
}
OBSERVATION_FIELDS = {
    **{k: set(v) for k, v in FIELDS.items()},
    "mtf": {"frequency_cy_px", "mtf", "field_xy", "orientation_deg", "field_coordinate_domain"},
    "texture": {
        "frequency_cy_px",
        "response",
        "field_xy",
        "orientation_deg",
        "field_coordinate_domain",
    },
    "geometry": {"mode", "object_xy", "image_xy", "view_ids", "native_shape", "view_extrinsics"},
    "color": {"reference_rgb", "measured_rgb"},
}
REQUIRED_OBSERVATIONS = {
    "dark": {"exposure_s", "mean_dn", "variance_dn2"},
    "ptc": {"mean_dn", "variance_dn2"},
    "shading": {"field_radius", "relative_response"},
    "mtf": {"frequency_cy_px", "mtf"},
    "geometry": {"object_xy", "image_xy"},
    "color": {"reference_rgb", "measured_rgb"},
    "tone": {"input_level", "output_level"},
    "texture": {"frequency_cy_px", "response"},
}


def _object(value: Any, allowed: set[str], name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")
    return copy.deepcopy(value)


def _text(value: Any, name: str, *, optional: bool = False) -> str:
    if optional and value is None:
        return ""
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError(f"{name} must be nonempty text of at most 1000 characters")
    return value.strip()


def _number(value: Any, name: str, low: float = -1e12, high: float = 1e12) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not np.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite in [{low}, {high}]")
    return float(value)


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def _finite_tree(value: Any, name: str = "payload") -> None:
    if isinstance(value, float) and not np.isfinite(value):
        raise ValueError(f"{name} contains a nonfinite number")
    if isinstance(value, dict):
        for key, child in value.items():
            _finite_tree(child, f"{name}.{key}")
    elif isinstance(value, list):
        for child in value:
            _finite_tree(child, name)


def conditions_spec(value: Any) -> dict:
    fields = set(DEFAULT_CONDITIONS) | {"exposure_s"}
    result = _object(value, fields, "conditions")
    for key in ("mode_id", "light_id"):
        result[key] = _text(result.get(key), f"conditions.{key}")
    bounds = {
        "gain": (1e-6, 1e6),
        "temperature_c": (-273.15, 1000),
        "aperture_f_number": (0.1, 1024),
        "focus_distance_m": (1e-6, 1e9),
    }
    for key, (low, high) in bounds.items():
        result[key] = _number(result.get(key), f"conditions.{key}", low, high)
    if "exposure_s" in result:
        result["exposure_s"] = _number(result["exposure_s"], "exposure_s", 1e-12, 3600)
    return result


def _shape(value: Any, name: str = "native_shape") -> list[int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} requires [height, width]")
    result = [_integer(x, name, 2, 65536) for x in value]
    if any(x % 2 for x in result):
        raise ValueError(f"{name} requires even native Bayer dimensions")
    return result


def campaign_spec(payload: dict) -> dict:
    result = _object(
        payload,
        {
            "name",
            "camera_id",
            "native_shape",
            "pixel_pitch_um",
            "conditions",
            "baseline_profile",
            "thresholds",
            "source",
            "db_bridge_id",
            "db_optical_profile_id",
        },
        "campaign",
    )
    result["name"] = _text(result.get("name"), "name")
    result["camera_id"] = _text(result.get("camera_id"), "camera_id")
    result["native_shape"] = _shape(result.get("native_shape"))
    result["pixel_pitch_um"] = _number(result.get("pixel_pitch_um"), "pixel_pitch_um", 0.01, 1000)
    result["conditions"] = conditions_spec(result.get("conditions"))
    profile = result.get("baseline_profile")
    if profile is None:
        from .calibration_lab_engine import default_profile

        profile = default_profile()
    if not isinstance(profile, dict):
        raise ValueError("baseline_profile requires an explicit profile object")
    _finite_tree(profile, "baseline_profile")
    result["baseline_profile"] = copy.deepcopy(profile)
    thresholds = result.get("thresholds", {})
    if not isinstance(thresholds, dict) or len(thresholds) > 100:
        raise ValueError("thresholds requires at most 100 named nonnegative limits")
    result["thresholds"] = {
        _text(k, "threshold name"): _number(v, k, 0) for k, v in thresholds.items()
    }
    if "source" in result and result["source"] is not None:
        source = _object(result["source"], {"run_id", "candidate_id", "condition_id"}, "source")
        result["source"] = {k: _text(v, f"source.{k}") for k, v in source.items()}
    for name in ("db_bridge_id", "db_optical_profile_id"):
        if result.get(name) is not None:
            result[name] = _text(result[name], name)
    return result


def _observations(chart: str, value: Any) -> dict:
    result = _object(value, OBSERVATION_FIELDS[chart], "observations")
    multiview = chart == "geometry" and result.get("mode") == "multiview_planar"
    if chart == "geometry" and result.get("mode", "known_ideal_warp") not in {
        "known_ideal_warp",
        "multiview_planar",
    }:
        raise ValueError("Unsupported geometry observation mode")
    length = None
    for key, data in result.items():
        if key == "field_coordinate_domain":
            if data not in FIELD_COORDINATE_DOMAINS:
                raise ValueError("Unsupported field_coordinate_domain")
            continue
        if key == "view_extrinsics":
            if not isinstance(data, dict) or len(data) > 256:
                raise ValueError("view_extrinsics requires at most 256 declared poses")
            poses = {}
            for identity, pose in data.items():
                _text(identity, "pose view id")
                pose = _object(pose, {"rvec", "tvec"}, "view pose")
                for vector in ("rvec", "tvec"):
                    values = pose.get(vector)
                    if not isinstance(values, list) or len(values) != 3:
                        raise ValueError("Pose rvec (radians) and tvec (mm) require 3 values")
                    pose[vector] = [_number(x, vector) for x in values]
                poses[identity] = pose
            result[key] = poses
            continue
        if key == "mode":
            continue
        if key == "native_shape":
            result[key] = _shape(data)
            continue
        if key == "view_ids":
            if not isinstance(data, list) or not 1 <= len(data) <= MAX_POINTS:
                raise ValueError("view_ids requires one view identifier per point")
            result[key] = [
                _text(x, "view id") if isinstance(x, str) else str(_integer(x, "view id", 0, 10000))
                for x in data
            ]
            if length is not None and len(data) != length:
                raise ValueError("view_ids must align with every point")
            length = len(data)
            continue
        if key == "orientation_deg":
            result[key] = _number(data, key, -360, 360)
            continue
        array = np.asarray(data)
        if array.dtype.kind not in "ifu" or array.dtype.kind == "b" or not np.isfinite(array).all():
            raise ValueError(f"observations.{key} must contain finite numeric values")
        columns = 3 if key.endswith("rgb") else 2 if key.endswith("xy") else None
        expected_dim = 1 if columns is None or key == "field_xy" else 2
        if array.ndim != expected_dim or not 1 <= len(array) <= MAX_POINTS:
            raise ValueError(f"observations.{key} has unsupported shape")
        if columns and (array.shape[-1] != columns):
            raise ValueError(f"observations.{key} requires {columns} columns")
        if key != "field_xy":
            if length is not None and len(array) != length:
                raise ValueError("Observation columns must have equal lengths")
            length = len(array)
        low, high = (0, 1e12)
        if key in {"object_xy", "image_xy", "field_xy"}:
            low, high = -1, 1
            if multiview:
                low, high = (-1e6, 1e6) if key == "object_xy" else (0, 65536)
        elif key in {"frequency_cy_px"}:
            low, high = 0, 0.5
        elif key in {
            "field_radius",
            "reference_rgb",
            "measured_rgb",
            "input_level",
            "output_level",
            "clipped_fraction",
        }:
            low, high = 0, 1
        if np.any(array < low) or np.any(array > high):
            raise ValueError(f"observations.{key} must lie in [{low}, {high}]")
        if key in {"frequency_cy_px", "field_radius"} and np.any(np.diff(array) <= 0):
            raise ValueError(f"observations.{key} must be strictly increasing")
        if key == "exposure_s" and np.any(array <= 0):
            raise ValueError("exposure_s must be positive")
        result[key] = array.astype(float).tolist()
    if multiview:
        if not {"view_ids", "native_shape"} <= set(result):
            raise ValueError("Multiview geometry requires view_ids and native_shape")
        if "image_xy" in result:
            points = np.asarray(result["image_xy"])
            if np.any(points[:, 0] >= result["native_shape"][1]) or np.any(
                points[:, 1] >= result["native_shape"][0]
            ):
                raise ValueError("Geometry image points must lie inside native_shape")
    if chart in {"mtf", "texture"} and result:
        result.setdefault("field_coordinate_domain", "sensor_normalized")
    return result


def measurement_spec(payload: dict) -> dict:
    allowed = {
        "campaign_id",
        "chart",
        "source_kind",
        "physical_instance",
        "session_id",
        "acquisition_id",
        "parent_acquisition_ids",
        "acquisition_group_id",
        "role",
        "conditions",
        "observations",
        "uncertainty",
        "method",
        "instrument",
        "reference",
        "raw_format",
        "geometry",
        "analysis",
        "units",
        "measurement_domain",
    }
    result = _object(payload, allowed, "measurement")
    _finite_tree(result)
    for key in ("campaign_id", "session_id", "acquisition_id"):
        result[key] = _text(result.get(key), key)
    for key, choices in (
        ("chart", CHARTS),
        ("role", ROLES),
        ("source_kind", ("measured", "synthetic")),
    ):
        if result.get(key) not in choices:
            raise ValueError(f"{key} must be one of {choices}")
    result["physical_instance"] = _text(result.get("physical_instance"), "physical_instance")
    domain = result.get("measurement_domain")
    if result["chart"] == "mtf" and domain is None and result["source_kind"] == "measured":
        raise ValueError(
            "Measured MTF requires measurement_domain camera_raw, camera_isp, or optical"
        )
    if domain is not None and domain not in {"camera_raw", "camera_isp", "optical"}:
        raise ValueError("Unsupported measurement_domain")
    if result["chart"] == "mtf":
        result["measurement_domain"] = domain or "camera_raw"
    result["conditions"] = conditions_spec(result.get("conditions"))
    result["observations"] = _observations(result["chart"], result.get("observations", {}))
    method = _object(result.get("method"), {"id", "version"}, "method")
    result["method"] = {k: _text(method.get(k), f"method.{k}") for k in ("id", "version")}
    uncertainty = _object(
        result.get("uncertainty", {"kind": "unknown"}),
        {"kind", "value", "unit", "method", "reason"},
        "uncertainty",
    )
    if uncertainty.get("kind") not in {"unknown", "absolute_bound", "standard_uncertainty"}:
        raise ValueError("Unsupported uncertainty kind")
    if uncertainty["kind"] != "unknown":
        uncertainty["value"] = _number(uncertainty.get("value"), "uncertainty.value", 0)
        for key in ("unit", "method"):
            uncertainty[key] = _text(uncertainty.get(key), f"uncertainty.{key}")
    result["uncertainty"] = uncertainty
    for field, keys in (
        ("instrument", {"id", "calibration_revision"}),
        ("reference", {"id", "source_uri", "method", "encoding", "white_point"}),
    ):
        value = result.get(field)
        if value is None:
            if result["source_kind"] == "measured":
                raise ValueError(f"Measured observations require {field} evidence")
            result[field] = None
        else:
            value = _object(value, keys, field)
            required = (
                ("id", "calibration_revision") if field == "instrument" else ("id", "source_uri")
            )
            for key in required:
                value[key] = _text(value.get(key), f"{field}.{key}")
            for key in set(value) - set(required):
                value[key] = _text(value[key], f"{field}.{key}")
            result[field] = value
    if result["chart"] == "texture" and not (result.get("reference") or {}).get("method"):
        raise ValueError("Texture transfer requires an explicit reference method")
    if result["chart"] == "color" and result["source_kind"] == "measured":
        ref = result["reference"]
        if ref.get("encoding") != "linear_rgb" or not ref.get("white_point"):
            raise ValueError(
                "Color measurements require linear_rgb reference encoding and white_point"
            )
    parents = result.get("parent_acquisition_ids", [])
    if not isinstance(parents, list) or len(parents) > 256:
        raise ValueError("parent_acquisition_ids requires at most 256 identities")
    result["parent_acquisition_ids"] = [_text(x, "parent_acquisition_ids") for x in parents]
    if len(set(result["parent_acquisition_ids"])) != len(parents):
        raise ValueError("parent_acquisition_ids must be unique")
    if "acquisition_group_id" in result:
        result["acquisition_group_id"] = _text(
            result["acquisition_group_id"], "acquisition_group_id"
        )
    if "units" in result:
        units = dict(FIELDS[result["chart"]])
        if (
            result["chart"] == "geometry"
            and result.get("analysis", {}).get("geometry_mode") == "multiview_planar"
        ):
            units = {
                "object_x": "mm",
                "object_y": "mm",
                "image_x": "px",
                "image_y": "px",
                "view_id": "1",
            }
        result["units"] = _object(result["units"], set(units), "units")
        for key, unit in result["units"].items():
            if unit != units[key]:
                raise ValueError(f"Unit mismatch for {key}: expected {units[key]}")
    # RAW-specific declarations are checked against the actual NPY shape before allocation.
    if "raw_format" in result:
        result["raw_format"] = _object(
            result["raw_format"],
            {"cfa", "bit_depth", "black_level_dn", "response_type"},
            "raw_format",
        )
    if "geometry" in result:
        result["geometry"] = _object(
            result["geometry"], {"full_sensor_shape", "crop_origin"}, "geometry"
        )
    if "analysis" in result:
        result["analysis"] = _object(
            result["analysis"],
            {
                "channel",
                "exposure_s",
                "rois",
                "reference_rgb",
                "input_level",
                "object_xy",
                "reference_amplitude",
                "frequency_cy_px",
                "field_xy",
                "field_coordinate_domain",
                "orientation_deg",
                "geometry_mode",
                "view_id",
                "native_shape",
            },
            "analysis",
        )
        if "field_coordinate_domain" in result["analysis"]:
            if result["chart"] not in {"mtf", "texture"}:
                raise ValueError("Field coordinate domains apply only to MTF and texture")
            if result["analysis"]["field_coordinate_domain"] not in FIELD_COORDINATE_DOMAINS:
                raise ValueError("Unsupported analysis.field_coordinate_domain")
            if result["observations"] and result["analysis"]["field_coordinate_domain"] != result[
                "observations"
            ].get("field_coordinate_domain", "sensor_normalized"):
                raise ValueError("Analysis and observation field coordinate domains disagree")
    return result


def _bounded_json_bytes(value, limit: int, name: str) -> int:
    """Count serialized bytes incrementally, without allocating a large copy."""
    size = 0
    try:
        for chunk in json.JSONEncoder(
            sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).iterencode(value):
            size += len(chunk.encode("utf-8"))
            if size > limit:
                raise ValueError(f"{name} exceeds the {limit}-byte resource budget")
    except (TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must contain finite JSON data") from exc
    return size


def study_measurement_ids(value) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("measurement_ids requires a nonempty list of identifiers")
    _bounded_json_bytes(value, MAX_STUDY_IDS_BYTES, "measurement_ids")
    result = [_text(x, "measurement id") for x in value]
    if len(set(result)) != len(result):
        raise ValueError("measurement_ids must be unique")
    return result


def measurement_resource_budget(measurements) -> dict:
    """Reject oversized studies before deepcopy, fitting or image allocation.

    Frequency, response, field and direction scalars all count; no curve or
    direction is silently discarded to fit this budget.
    """
    if not isinstance(measurements, (list, tuple)) or not measurements:
        raise ValueError("Require a nonempty collection of measurement records")
    total_scalars = 0
    for record in measurements:
        if not isinstance(record, dict) or not isinstance(record.get("observations"), dict):
            raise ValueError("Measurement records require observations")
        pending = [record["observations"]]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, (list, tuple)):
                pending.extend(value)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                total_scalars += 1
                if total_scalars > MAX_OBSERVATION_SCALARS:
                    raise ValueError(
                        f"Study exceeds {MAX_OBSERVATION_SCALARS} observation scalars; "
                        "reduce retained data or split the study"
                    )
    size = _bounded_json_bytes(measurements, MAX_MEASUREMENT_BYTES, "Measurement records")
    return {
        "admission": "resource_budget_v1",
        "measurement_count": len(measurements),
        "canonical_measurement_bytes": size,
        "observation_scalars": total_scalars,
        "limits": {
            "canonical_measurement_bytes": MAX_MEASUREMENT_BYTES,
            "observation_scalars": MAX_OBSERVATION_SCALARS,
            "measurement_ids_bytes": MAX_STUDY_IDS_BYTES,
        },
        "position_count_limit": None,
        "measurement_count_limit": None,
    }


def study_spec(payload: dict) -> dict:
    result = _object(
        payload,
        {"name", "campaign_id", "measurement_ids", "base_profile_id", "seed", "max_iterations", "fit_method"},
        "study",
    )
    result["name"] = _text(result.get("name", "Calibration study"), "name")
    result["campaign_id"] = _text(result.get("campaign_id"), "campaign_id")
    result["measurement_ids"] = study_measurement_ids(result.get("measurement_ids"))
    result["base_profile_id"] = (
        None
        if result.get("base_profile_id") is None
        else _text(result["base_profile_id"], "base_profile_id")
    )
    result["seed"] = _integer(result.get("seed", 17), "seed", 0, 2**31 - 65536)
    result["max_iterations"] = _integer(result.get("max_iterations", 100), "max_iterations", 1, 500)
    if "fit_method" in result and result["fit_method"] not in {"analytic_curve", "esfr_forward_observer_v1"}:
        raise ValueError("Unsupported calibration fit_method")
    return result


def _read_csv(
    content: bytes, chart: str, declared_units: dict, analysis: dict | None = None
) -> dict:
    try:
        stream = io.StringIO(content.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ValueError("CSV requires UTF-8 text") from exc
    reader = csv.DictReader(stream)
    names = reader.fieldnames or []
    if not names or len(names) != len(set(names)):
        raise ValueError("CSV requires unique explicit column headers")
    mapping = {}
    analysis = analysis or {}
    multiview = chart == "geometry" and analysis.get("geometry_mode") == "multiview_planar"
    units = dict(FIELDS[chart])
    if multiview:
        units = {
            "object_x": "mm",
            "object_y": "mm",
            "image_x": "px",
            "image_y": "px",
            "view_id": "1",
        }
    for header in names:
        if "[" in header and header.endswith("]"):
            name, unit = header[:-1].rsplit("[", 1)
        else:
            name, unit = header, declared_units.get(header)
        if name not in units or unit != units[name]:
            raise ValueError(f"CSV column {header!r} requires a supported name and explicit unit")
        if name in mapping.values():
            raise ValueError("Duplicate normalized CSV columns")
        mapping[header] = name
    result = {name: [] for name in mapping.values()}
    for index, row in enumerate(reader):
        if index >= MAX_POINTS:
            raise ValueError("CSV exceeds 10000 observations")
        if None in row or any(value is None or not value.strip() for value in row.values()):
            raise ValueError("CSV rows must be rectangular without missing cells")
        for header, name in mapping.items():
            try:
                result[name].append(float(row[header]))
            except ValueError as exc:
                raise ValueError(f"CSV {name} requires numerical values") from exc
    if not result or not len(next(iter(result.values()))):
        raise ValueError("CSV contains no observations")
    groups = (
        ("object", "image")
        if chart == "geometry"
        else ("reference", "measured")
        if chart == "color"
        else ()
    )
    axes = "xy" if chart == "geometry" else "rgb"
    for group in groups:
        keys = [f"{group}_{axis}" for axis in axes]
        if any(key in result for key in keys) and not all(key in result for key in keys):
            raise ValueError(f"CSV requires every {group} coordinate column")
        if all(key in result for key in keys):
            result[f"{group}_{axes}"] = np.array([result.pop(key) for key in keys]).T.tolist()
    if multiview:
        result["mode"] = "multiview_planar"
        result["native_shape"] = analysis.get("native_shape")
        if "view_id" not in result or any(x != int(x) for x in result["view_id"]):
            raise ValueError("Multiview CSV requires integer view_id[1]")
        result["view_ids"] = [str(int(x)) for x in result.pop("view_id")]
    if chart in {"mtf", "texture"}:
        for key in ("field_xy", "orientation_deg", "field_coordinate_domain"):
            if key in analysis:
                result[key] = analysis[key]
    return _observations(chart, result)


def import_measurement(
    metadata: dict, original_bytes: bytes, filename: str, *, raw_bytes: bytes | None = None
) -> dict:
    """Normalize a CSV, JSON table, or integer NPY without writing original files."""
    if not isinstance(original_bytes, bytes) or not 0 < len(original_bytes) <= MAX_INPUT_BYTES:
        raise ValueError("Input file must contain 1 byte..64 MiB")
    result = measurement_spec(metadata)
    suffix = Path(filename).suffix.lower()
    if suffix == ".npy" and result["chart"] in {"color", "tone"}:
        if result.get("measurement_domain") not in {None, "camera_raw"}:
            raise ValueError("NPY color/tone is camera_raw; ISP/optical declarations conflict")
        # This records the actual observation stage without changing original metadata bytes.
        result["measurement_domain"] = "camera_raw"
    quality = {}
    frame_hashes = []
    if raw_bytes is not None and suffix == ".npy":
        raise ValueError("Supply one NPY source, not duplicate RAW bytes")
    if suffix == ".csv":
        if result["observations"]:
            raise ValueError("CSV import must not also declare observations")
        result["observations"] = _read_csv(
            original_bytes, result["chart"], result.get("units", {}), result.get("analysis")
        )
    elif suffix == ".json":
        # Metadata and original JSON must represent the same declared measurement.
        try:
            document = json.loads(original_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON import must contain a measurement object") from exc
        if measurement_spec(document) != result:
            raise ValueError("JSON source and measurement metadata disagree")
    elif suffix != ".npy":
        raise ValueError("Supported imports are .csv, .json and .npy only")
    if suffix == ".npy" or raw_bytes is not None:
        from .calibration_lab_raw import analyze_npy

        observations, quality, frame_hashes = analyze_npy(
            result, original_bytes if suffix == ".npy" else raw_bytes
        )
        if suffix == ".npy":
            if result["observations"]:
                raise ValueError("NPY import must not also declare observations")
            result["observations"] = _observations(result["chart"], observations)
        else:
            quality["supplementary_raw_only"] = True
            quality["raw_observations"] = observations
    missing = sorted(REQUIRED_OBSERVATIONS[result["chart"]] - set(result["observations"]))
    if result["chart"] == "mtf" and result["source_kind"] == "measured":
        for key in ("field_xy", "orientation_deg"):
            if key not in result["observations"]:
                missing.append(key)
    if result["chart"] == "shading" and result["observations"]:
        obs = result["observations"]
        if "field_radius" in obs and "relative_response" in obs:
            if obs["field_radius"][0] != 0 or not np.isclose(obs["relative_response"][0], 1):
                quality.setdefault("not_fit_reasons", []).append(
                    "Relative shading requires declared on-axis radius 0 normalized to response 1"
                )
    result["import_status"] = "partial" if missing or quality.get("not_fit_reasons") else "ready"
    result["missing_observations"] = missing
    result["quality"] = quality
    result["provenance"] = {
        "filename": Path(filename).name,
        "source_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "source_bytes": len(original_bytes),
        "source_frame_hashes": frame_hashes,
        "metadata_sha256": hashlib.sha256(
            json.dumps(metadata, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
        "importer": SCHEMA,
        "physical_validation": "not_established",
    }
    if raw_bytes is not None:
        result["provenance"]["raw_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    result["source_frame_hashes"] = frame_hashes
    return result


def template() -> dict:
    from .calibration_lab_engine import default_profile

    profile = default_profile()
    campaign = {
        "name": "Camera calibration campaign",
        "camera_id": "camera-serial-001",
        "native_shape": profile.get("native_shape", [128, 128]),
        "pixel_pitch_um": profile.get("pixel_pitch_um", 3.0),
        "conditions": copy.deepcopy(DEFAULT_CONDITIONS),
        "baseline_profile": profile,
        "thresholds": {},
    }
    measurement = {
        "campaign_id": "REPLACE_WITH_CAMPAIGN_ID",
        "chart": "mtf",
        "measurement_domain": "camera_raw",
        "analysis": {
            "field_xy": [0, 0],
            "orientation_deg": 0,
            "field_coordinate_domain": "sensor_normalized",
        },
        "source_kind": "synthetic",
        "physical_instance": "camera-serial-001",
        "session_id": "session-001",
        "acquisition_id": "capture-001",
        "role": "training",
        "conditions": copy.deepcopy(DEFAULT_CONDITIONS),
        "observations": {},
        "uncertainty": {"kind": "unknown"},
        "method": {"id": "external_table", "version": "1"},
        "instrument": None,
        "reference": {
            "id": "synthetic-chart",
            "source_uri": "synthetic://example",
            "method": "authored synthetic fixture",
        },
    }
    examples = {
        "dark": (
            "exposure_s[s],mean_dn[DN],variance_dn2[DN^2]\n0.001,64.1,4\n0.01,65,4.5\n0.02,66,5\n"
        ),
        "ptc": (
            "mean_dn[DN],variance_dn2[DN^2],exposure_s[s],clipped_fraction[1]\n"
            "164,54,0.001,0\n264,104,0.002,0\n464,204,0.004,0\n864,404,0.008,0\n"
        ),
        "shading": "field_radius[1],relative_response[1]\n0,1\n0.5,0.95\n1,0.8\n",
        "mtf": (
            "frequency_cy_px[cy/px],mtf[1]\n0,1\n0.1,0.9\n0.2,0.65\n0.3,0.4\n0.4,0.2\n0.5,0.08\n"
        ),
        "geometry": (
            "object_x[1],object_y[1],image_x[1],image_y[1]\n"
            "-0.5,-0.5,-0.51,-0.51\n0.5,-0.5,0.51,-0.51\n"
            "-0.5,0.5,-0.51,0.51\n0.5,0.5,0.51,0.51\n"
            "-0.5,0,-0.505,0\n0.5,0,0.505,0\n0,-0.5,0,-0.505\n0,0.5,0,0.505\n"
        ),
        "color": (
            "reference_r[1],reference_g[1],reference_b[1],"
            "measured_r[1],measured_g[1],measured_b[1]\n"
            "0.5,0.2,0.2,0.45,0.2,0.22\n0.2,0.5,0.2,0.18,0.5,0.22\n"
            "0.2,0.2,0.5,0.18,0.2,0.55\n0.5,0.5,0.5,0.45,0.5,0.55\n"
        ),
        "tone": "input_level[1],output_level[1]\n0,0\n0.25,0.3\n0.5,0.55\n0.75,0.8\n1,1\n",
        "texture": "frequency_cy_px[cy/px],response[1]\n0,1\n0.1,0.85\n0.2,0.6\n0.3,0.35\n",
    }
    study = {
        "name": "Calibration study",
        "campaign_id": "REPLACE_WITH_CAMPAIGN_ID",
        "measurement_ids": [],
        "base_profile_id": None,
        "seed": 17,
        "max_iterations": 100,
    }
    help_text = [
        "DB optical initialization retains a modeled effective PSF for source optics "
        "and pixel aperture, with transfer residuals and source spectral assumptions. "
        "It does not identify a measured physical PSF or charge diffusion. "
        "Only a qualified conversion may initialize a new campaign baseline.",
        "DB bridge imports electronics, pitch, CFA, gain and temperature only. "
        "Its 256x256 probe is an independent virtual ROI, not the physical camera raster. "
        "Supply the camera identity and full sensor native_shape explicitly.",
        "DB replay transfers black level, DN/e, read noise and dark current only; "
        "source optics, pre-noise photoelectron rates, reference CCM and ISP remain frozen. "
        "An electrical replay is not full camera or physical validation.",
        "CSV headers carry [unit]; bare headers require matching metadata.units. "
        "Values are never silently rescaled.",
        "Measured inputs require physical_instance, instrument id/calibration_revision "
        "and reference id/source_uri.",
        "NPY is integer linear RGGB only: [H,W], [repeat,H,W], or [level,repeat,H,W]. "
        "No pickle, resizing, or implicit black correction.",
        "Dark/PTC use per-pixel temporal sample variance; "
        "one repeat is partial, not a noise measurement.",
        "NPY analysis.channel is R/G1/G2/B, default G1. "
        "analysis.exposure_s supplies each level's exposure.",
        "Shading/color/tone require analysis.rois [row,column,height,width]; "
        "shading uses {box,field_radius} objects.",
        "Geometry RAW uses fiducial centroids and known ideal image coordinates. "
        "Multiview CSV uses object_xy in mm and image_xy in pixels with view_id[1].",
        "MTF uses one CFA sublattice: native frequencies are limited to 0.25 cy/native-pixel. "
        "Effective camera blur is not uniquely identified optical PSF.",
        "MTF/texture field_coordinate_domain is sensor_normalized by default. "
        "Reference-lens model coordinates require explicit reference_lens_object_direction "
        "on CSV analysis or JSON observations and a matching profile domain. "
        "Native RAW imports derive sensor-normalized crop coordinates and cannot establish "
        "a reference-lens coordinate mapping.",
        "Color requires linear RGB [0,1] with declared reference white point; "
        "texture is comparison-only with an explicit reference transfer method.",
        "Synthetic fixtures and successful imports do not establish physical validation. "
        "Final validation requires independent measured acquisitions and declared criteria.",
    ]
    return {
        "schema": SCHEMA,
        "campaign": campaign,
        "campaign_template": campaign,
        "measurement": measurement,
        "measurement_template": measurement,
        "measurement_templates": {
            chart: {
                **copy.deepcopy(measurement),
                "chart": chart,
                "measurement_domain": "camera_isp" if chart in {"color", "tone"} else "camera_raw",
                "analysis": copy.deepcopy(measurement["analysis"])
                if chart in {"mtf", "texture"}
                else {},
            }
            for chart in CHARTS
        },
        "study_template": study,
        "charts": list(CHARTS),
        "roles": list(ROLES),
        "csv_examples": examples,
        "supported_input_formats": ["csv", "json", "npy"],
        "parameter_catalog": list(CHARTS),
        "help": help_text,
        "limits": {
            "max_bytes": MAX_INPUT_BYTES,
            "max_points": MAX_POINTS,
            "max_spatial_pixels": 4194304,
            "max_frames": 256,
            "max_measurements_per_study": 64,
            "max_iterations": 500,
            "max_seed": 2**31 - 65536,
        },
    }
