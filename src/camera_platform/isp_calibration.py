"""Immutable native RAW ISP calibration imports, not a calibration fitter.

Numeric hints are independently declared. Source labels never grant physical
validation or release authority; every application rechecks source and blobs.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy

from .registry import canonical, digest

SITES = ["R", "Gr", "Gb", "B"]
SCHEMA = "camera_isp_calibration_v1"
FIELDS = {
    "name",
    "revision",
    "source_kind",
    "source_uri",
    "license",
    "method_and_version",
    "physical_instance",
    "parents",
    "reference_source",
    "binding",
    "domain",
    "payload",
    "instrument",
    "dataset_ids",
    "dataset_role",
    "uncertainty",
    "evidence_refs",
}


def _exact(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{name} requires exactly {sorted(keys)}")
    return value


def _number(value, low, high, name):
    if type(value) not in (int, float):
        raise ValueError(f"{name} requires a finite number in [{low}, {high}]")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} requires a finite number in [{low}, {high}]") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} requires a finite number in [{low}, {high}]")
    return number


def _text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} requires nonempty bounded text")
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("An exact lowercase SHA256 is required")
    return value


def _pair(value, name, minimum=0):
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(v) is not int or not minimum <= v <= 1_000_000 for v in value)
    ):
        raise ValueError(f"{name} requires two bounded native integers")
    return value


def validate_numeric_payload(payload):
    """Return detached JSON for the ISP engine; no model/provenance inference."""
    _exact(payload, {"schema", "sites", "lsc", "noise"}, "numeric calibration")
    if payload["schema"] != SCHEMA or payload["sites"] != SITES:
        raise ValueError("Calibration requires camera_isp_calibration_v1 and R/Gr/Gb/B")
    result = {"schema": SCHEMA, "sites": SITES.copy(), "lsc": None, "noise": None}
    if payload["lsc"] is not None:
        lsc = _exact(
            payload["lsc"],
            {
                "schema",
                "row_nodes",
                "column_nodes",
                "gains",
                "valid_mask",
                "interpolation",
                "reference_roi",
                "normalization",
            },
            "LSC",
        )
        if (
            lsc["schema"] != "native_four_site_grid_v1"
            or lsc["interpolation"] != "bilinear"
            or lsc["normalization"] != "declared_per_site_reference"
        ):
            raise ValueError("Unsupported native LSC interpretation")
        axes = []
        for key in ("row_nodes", "column_nodes"):
            values = lsc[key]
            if not isinstance(values, list) or not 2 <= len(values) <= 65:
                raise ValueError("LSC axes require 2..65 explicit pixel-center nodes")
            axis = [_number(v, 0, 999_999, key) for v in values]
            if any(b <= a for a, b in zip(axis, axis[1:], strict=False)):
                raise ValueError("LSC nodes must be strictly increasing")
            axes.append(axis)
        normalized = deepcopy(lsc)
        for key in ("gains", "valid_mask"):
            matrix = lsc[key]
            if (
                not isinstance(matrix, list)
                or len(matrix) != len(axes[0])
                or any(not isinstance(row, list) or len(row) != len(axes[1]) for row in matrix)
                or any(
                    not isinstance(cell, list) or len(cell) != 4 for row in matrix for cell in row
                )
            ):
                raise ValueError("LSC arrays require [grid_row, grid_column, four_sites]")
            for row in matrix:
                for cell in row:
                    for value in cell:
                        if key == "gains":
                            _number(value, 0.25, 16, "LSC gain")
                        elif type(value) is not bool:
                            raise ValueError("LSC validity must be boolean")
        roi = _exact(
            lsc["reference_roi"],
            {"row_start", "row_end", "column_start", "column_end"},
            "reference ROI",
        )
        for dimension, axis in zip(("row", "column"), axes, strict=True):
            low = _number(roi[dimension + "_start"], axis[0], axis[-1], "reference ROI")
            high = _number(roi[dimension + "_end"], axis[0], axis[-1] + 1, "reference ROI")
            if high <= low:
                raise ValueError("Reference ROI must have positive extent (half-open)")
        normalized.update(row_nodes=axes[0], column_nodes=axes[1])
        result["lsc"] = normalized
    if payload["noise"] is not None:
        noise = _exact(
            payload["noise"],
            {
                "schema",
                "slope_dn",
                "intercept_dn2",
                "signal_interval_dn",
                "reference",
                "variance_model",
                "spatial_independence",
            },
            "noise",
        )
        if (
            noise["schema"] != "native_four_site_noise_v1"
            or noise["reference"] != "black_subtracted_dn_before_lsc"
            or noise["variance_model"] != "slope_times_max_signal_zero_plus_intercept"
            or noise["spatial_independence"] != "assumed"
        ):
            raise ValueError("Noise requires explicit pre-LSC black-subtracted DN variance hint")
        normalized = deepcopy(noise)
        for key in ("slope_dn", "intercept_dn2"):
            if not isinstance(noise[key], list) or len(noise[key]) != 4:
                raise ValueError("Noise coefficients require four separate native sites")
            normalized[key] = [_number(v, 0, 1e12, key) for v in noise[key]]
        interval = noise["signal_interval_dn"]
        if not isinstance(interval, list) or len(interval) != 2:
            raise ValueError("Noise signal interval requires two DN endpoints")
        normalized["signal_interval_dn"] = [
            _number(v, -(2**24), 2**24, "signal DN") for v in interval
        ]
        if interval[0] >= interval[1]:
            raise ValueError("Noise signal interval must increase")
        result["noise"] = normalized
    return json.loads(json.dumps(result, allow_nan=False))


def validate_payload(payload):
    """Validate the complete import document; applicability is resolved server-side."""
    _exact(payload, FIELDS, "ISP calibration import")
    data = deepcopy(payload)
    for key in ("name", "revision", "source_uri", "license", "method_and_version"):
        _text(data[key], key)
    if data["source_kind"] not in {"estimated", "simulated", "vendor", "measured"}:
        raise ValueError("Explicit calibration source kind required")
    for key in ("parents", "dataset_ids"):
        if (
            not isinstance(data[key], list)
            or len(data[key]) > 100
            or len(set(map(str, data[key]))) != len(data[key])
        ):
            raise ValueError(f"{key} requires unique bounded identities")
        for value in data[key]:
            _text(value, key)
    if data["physical_instance"] is not None:
        _text(data["physical_instance"], "physical instance")
    if data["instrument"] is not None:
        _exact(data["instrument"], {"id", "calibration_revision"}, "instrument")
        for value in data["instrument"].values():
            _text(value, "instrument")
    if data["source_kind"] == "measured" and (
        not data["physical_instance"]
        or not data["instrument"]
        or not data["dataset_ids"]
        or not data["evidence_refs"]
    ):
        raise ValueError(
            "Measured calibration needs physical identity, instrument, dataset and evidence"
        )
    if data["dataset_role"] not in {"development", "holdout"}:
        raise ValueError("Dataset role must distinguish development from holdout")
    _exact(data["uncertainty"], {"kind", "reason"}, "uncertainty")
    if data["uncertainty"]["kind"] != "unknown":
        raise ValueError("This slice does not infer or propagate calibration uncertainty bounds")
    _text(data["uncertainty"]["reason"], "unknown uncertainty reason")
    _exact(data["reference_source"], {"kind", "id"}, "reference source")
    if data["reference_source"]["kind"] not in {"run", "capture"}:
        raise ValueError("Calibration reference must be preserved camera RAW")
    _text(data["reference_source"]["id"], "reference identity")
    binding = _exact(
        data["binding"],
        {
            "sensor_model_id",
            "sensor_model_sha256",
            "lens_model_id",
            "lens_model_sha256",
            "full_sensor_shape",
            "pixel_pitch_um",
            "raw_format",
            "readout_mode",
        },
        "binding",
    )
    for role in ("sensor", "lens"):
        _text(binding[role + "_model_id"], role)
        _sha(binding[role + "_model_sha256"])
    _pair(binding["full_sensor_shape"], "full sensor", 2)
    _number(binding["pixel_pitch_um"], 0.001, 1000, "pixel pitch um")
    raw = _exact(
        binding["raw_format"],
        {"cfa", "bit_depth", "black_level_dn", "response_type", "packing", "layout", "hdr_streams"},
        "RAW format",
    )
    if (
        raw["cfa"] != "RGGB"
        or raw["response_type"] != "linear"
        or raw["packing"] != "unpacked"
        or raw["layout"] != "bayer"
        or type(raw["hdr_streams"]) is not int
        or raw["hdr_streams"] != 1
        or type(raw["bit_depth"]) is not int
        or not 1 <= raw["bit_depth"] <= 24
    ):
        raise ValueError("Calibration requires one linear unpacked native RGGB RAW stream")
    _number(raw["black_level_dn"], 0, 2 ** raw["bit_depth"] - 2, "RAW black level")
    _exact(
        binding["readout_mode"],
        {"plugin_id", "capture_program_id", "readout_sha256"},
        "readout mode",
    )
    _text(binding["readout_mode"]["plugin_id"], "readout mode")
    _sha(binding["readout_mode"]["readout_sha256"])
    if binding["readout_mode"]["capture_program_id"] is not None:
        _text(binding["readout_mode"]["capture_program_id"], "capture program")
    _exact(data["domain"], {"conditions"}, "domain")
    conditions = data["domain"]["conditions"]
    if (
        not isinstance(conditions, dict)
        or not {"exposure_s", "gain", "temperature_c"} <= conditions.keys()
    ):
        raise ValueError("Calibration domain requires exposure_s, gain and temperature_c")
    for key, spec in conditions.items():
        _text(key, "condition")
        if isinstance(spec, dict) and spec.get("kind") == "interval":
            _exact(spec, {"kind", "min", "max", "unit"}, "condition interval")
            _number(spec["min"], -1e15, 1e15, key)
            _number(spec["max"], spec["min"], 1e15, key)
            _text(spec["unit"], "condition unit")
        else:
            _exact(spec, {"kind", "value"}, "exact condition")
            if spec["kind"] != "exact":
                raise ValueError("Unknown condition domain rule")
    if not isinstance(data["evidence_refs"], list) or len(data["evidence_refs"]) > 100:
        raise ValueError("Evidence references must be a bounded list")
    for ref in data["evidence_refs"]:
        _exact(ref, {"sha256", "role"}, "evidence reference")
        _sha(ref["sha256"])
        _text(ref["role"], "evidence role")
    data["payload"] = validate_numeric_payload(data["payload"])
    return json.loads(json.dumps(data, allow_nan=False))


def _condition_rows(source):
    base = deepcopy(source["conditions"])
    rows = []
    for frame in source.get("frame_conditions", []):
        if "conditions" in frame:
            applied = frame["conditions"]
        else:
            applied = {}
            if "exposure_ns" in frame:
                applied["exposure_s"] = {"value": frame["exposure_ns"] / 1e9, "unit": "s"}
            if "gain" in frame:
                applied["gain"] = {"value": frame["gain"], "unit": "1"}
            if "temperature_c" in frame:
                applied["temperature_c"] = {"value": frame["temperature_c"], "unit": "degC"}
            if not applied:
                raise ValueError("RAW frame conditions cannot establish calibration applicability")
        rows.append(base | applied)
    return rows or [base]


def _source_context(service, source):
    from .imaging import _format, replay_source
    from .native_model_evidence import verify_models

    record = source["source_snapshot"]
    current = (
        service.registry.get_run(source["source_id"])
        if source["source_kind"] == "run"
        else service.registry.get("captures", source["source_id"])
    )
    if current != record:
        raise ValueError("RAW source differs from its exact frozen registry record")
    fresh = replay_source(service, source["source_kind"], source["source_id"])
    for key in (
        "raw_sha256",
        "raw_artifact",
        "raw_shape",
        "raw_format",
        "geometry",
        "conditions",
        "frame_conditions",
        "source_provenance",
    ):
        if source.get(key) != fresh.get(key):
            raise ValueError(f"RAW source {key} differs from its preserved acquisition")
    service.registry.blob_path(source["raw_sha256"])
    geometry = source["geometry"]
    if not isinstance(geometry, dict) or geometry.get("kind") != "native_sensor":
        raise ValueError("ISP calibration requires explicit native sensor geometry")
    full = _pair(geometry["full_sensor_shape"], "full sensor", 2)
    origin = _pair(geometry["crop_origin"], "crop origin")
    shape = source["raw_shape"]
    if (
        len(shape) != 3
        or any(type(v) is not int or v <= 0 for v in shape)
        or any(o + n > f for o, n, f in zip(origin, shape[-2:], full, strict=True))
    ):
        raise ValueError("RAW shape/crop lies outside the native sensor")
    assembly_id = record.get("assembly_id")
    if not assembly_id:
        raise ValueError("Capture lacks a fixed model/assembly identity")
    assembly = service.registry.get("assemblies", assembly_id)
    if "assembly_snapshot" in record and record["assembly_snapshot"] != assembly:
        raise ValueError("RAW assembly snapshot differs from its immutable model binding")
    models = [service.registry.get("models", i) for i in assembly["model_ids"]]
    if "model_snapshots" in record:
        frozen = {m["id"]: m for m in record["model_snapshots"]}
        if any(frozen.get(m["id"]) != m for m in models):
            raise ValueError("RAW model snapshots differ from their registered identities")
    verify_models(service, models)
    roles = {m["kind"]: m for m in models}
    sensor, lens = roles["sensor"], roles["lens"]
    if "directional_response" not in lens["payload"]:
        from .factory import verify_model_link, verify_psf_model_link

        if lens.get("psf_factory_job_id"):
            verify_psf_model_link(service, lens)
        elif lens.get("factory_job_id"):
            verify_model_link(service, lens)
    pitch = sensor["payload"]["pixel_pitch_um"]
    native = []
    for name in ("active_height", "active_width"):
        size = sensor["properties"][name]
        if size["unit"] != "mm":
            raise ValueError("Sensor active dimensions must be explicit mm")
        value = size["value"] * 1000 / pitch
        if abs(value - round(value)) > 0.1:
            raise ValueError("Sensor dimensions do not establish integer native sampling")
        native.append(round(value))
    if native != full or source["raw_format"] != _format(assembly["raw_format"]):
        raise ValueError("RAW geometry/format differs from fixed sensor/lens assembly")
    readout = sensor["payload"]["readout"]
    program_id = record.get("capture_program_id")
    if program_id:
        from .capture_programs import verify_program

        program = service.registry.get("capture_programs", program_id)
        verify_program(service, program, record["plugin_id"])
        if record.get("capture_program_snapshot") != program:
            raise ValueError("RAW capture program snapshot is not its exact registered revision")
        readout = program["payload"]["electrical_model"]
    binding = {
        "sensor_model_id": sensor["id"],
        "sensor_model_sha256": digest(sensor),
        "lens_model_id": lens["id"],
        "lens_model_sha256": digest(lens),
        "full_sensor_shape": full,
        "pixel_pitch_um": pitch,
        "raw_format": deepcopy(source["raw_format"]),
        "readout_mode": {
            "plugin_id": record.get("plugin_id", record.get("capture_kind", "capture")),
            "capture_program_id": program_id,
            "readout_sha256": digest(readout),
        },
    }
    return binding, models


def _applicable(data, source, binding):
    if data["binding"] != binding:
        raise ValueError("ISP calibration sensor/lens/RAW/native geometry/readout binding differs")
    rules = data["domain"]["conditions"]
    for row in _condition_rows(source):
        if set(row) != set(rules):
            raise ValueError("Calibration domain must explicitly cover every acquisition condition")
        for key, spec in rules.items():
            actual = row[key]
            if spec["kind"] == "exact":
                valid = actual == spec["value"]
            else:
                valid = (
                    isinstance(actual, dict)
                    and set(actual) == {"value", "unit"}
                    and actual["unit"] == spec["unit"]
                    and type(actual["value"]) in (int, float)
                    and spec["min"] <= actual["value"] <= spec["max"]
                )
            if not valid:
                raise ValueError(f"Calibration condition outside declared domain: {key}")
    lsc = data["payload"]["lsc"]
    if lsc is not None:
        for axis, full, start, size in zip(
            (lsc["row_nodes"], lsc["column_nodes"]),
            binding["full_sensor_shape"],
            source["geometry"]["crop_origin"],
            source["raw_shape"][-2:],
            strict=True,
        ):
            if axis[-1] > full - 1 or axis[0] > start or axis[-1] < start + size - 1:
                raise ValueError("LSC grid does not cover this native crop without extrapolation")


def template(service, source_kind, source_id):
    from .imaging import replay_source

    source = replay_source(service, source_kind, source_id)
    binding, _ = _source_context(service, source)
    height, width = binding["full_sensor_shape"]
    rows = _condition_rows(source)
    conditions = {}
    for key in rows[0]:
        values = [row[key] for row in rows]
        if all(value == values[0] for value in values):
            conditions[key] = {"kind": "exact", "value": values[0]}
        elif all(
            isinstance(v, dict)
            and set(v) == {"value", "unit"}
            and type(v["value"]) in (int, float)
            and v["unit"] == values[0]["unit"]
            for v in values
        ):
            conditions[key] = {
                "kind": "interval",
                "min": min(v["value"] for v in values),
                "max": max(v["value"] for v in values),
                "unit": values[0]["unit"],
            }
        else:
            raise ValueError("Reference RAW has incompatible nonnumeric frame conditions")
    black = binding["raw_format"]["black_level_dn"]
    white = 2 ** binding["raw_format"]["bit_depth"] - 1
    data = {
        "name": "Native RAW · declared ISP calibration example",
        "revision": "1",
        "source_kind": "estimated",
        "source_uri": "fixture://declared-isp-calibration-example",
        "license": "MIT; authored assumptions",
        "method_and_version": "declared_isp_example:1",
        "physical_instance": None,
        "parents": [],
        "reference_source": {"kind": source_kind, "id": source_id},
        "binding": binding,
        "domain": {"conditions": conditions},
        "instrument": None,
        "dataset_ids": [],
        "dataset_role": "development",
        "uncertainty": {
            "kind": "unknown",
            "reason": "Authored unity grid and variance hint; "
            "not fitted, measured, validated, or inferred from the reference RAW.",
        },
        "evidence_refs": [{"sha256": source["raw_sha256"], "role": "reference RAW identity only"}],
        "payload": {
            "schema": SCHEMA,
            "sites": SITES.copy(),
            "lsc": {
                "schema": "native_four_site_grid_v1",
                "row_nodes": [0, height - 1],
                "column_nodes": [0, width - 1],
                "gains": [[[1.0] * 4 for _ in range(2)] for _ in range(2)],
                "valid_mask": [[[True] * 4 for _ in range(2)] for _ in range(2)],
                "interpolation": "bilinear",
                "normalization": "declared_per_site_reference",
                "reference_roi": {
                    "row_start": 0,
                    "row_end": height,
                    "column_start": 0,
                    "column_end": width,
                },
            },
            "noise": {
                "schema": "native_four_site_noise_v1",
                "slope_dn": [1.0] * 4,
                "intercept_dn2": [4.0] * 4,
                "signal_interval_dn": [-black, white - black],
                "reference": "black_subtracted_dn_before_lsc",
                "variance_model": "slope_times_max_signal_zero_plus_intercept",
                "spatial_independence": "assumed",
            },
        },
    }
    return validate_payload(data)


def _references(service, data, seen):
    artifacts = []
    for index, ref in enumerate(data["evidence_refs"]):
        sha = ref["sha256"]
        content = service.registry.blob_path(sha).read_bytes()
        artifacts.append(
            {
                "id": sha,
                "sha256": sha,
                "bytes": len(content),
                "name": f"isp-calibration-evidence-{index:03d}.bin",
                "media_type": "application/octet-stream",
                "url": f"/api/v1/artifacts/{sha}",
                "role": ref["role"],
            }
        )
    for identity in data["parents"]:
        _get(service, identity, seen)
    return artifacts


def import_calibration(service, payload):
    from .imaging import replay_source

    data = validate_payload(payload)
    ref = data["reference_source"]
    source = replay_source(service, ref["kind"], ref["id"])
    binding, models = _source_context(service, source)
    _applicable(data, source, binding)
    evidence_artifacts = _references(service, data, set())
    artifacts = [
        service.registry.store_blob(canonical(value).encode(), name, "application/json")
        for name, value in (
            ("isp-calibration-source.json", payload),
            ("isp-calibration-normalized.json", data),
        )
    ]
    return service.registry.put(
        "isp_calibrations",
        {
            **data,
            "artifacts": artifacts,
            "evidence_artifacts": evidence_artifacts,
            "source_snapshot": source["source_snapshot"],
            "source_sha256": digest(source["source_snapshot"]),
            "model_snapshots": models,
            "physical_validation": "not_established",
            "approval_status": "draft",
            "authority": {"physical_validation": False, "release": False},
        },
    )


def _get(service, identity, seen):
    if identity in seen or len(seen) > 100:
        raise ValueError("Calibration parent graph must be bounded and acyclic")
    seen = seen | {identity}
    record = service.registry.get("isp_calibrations", identity)
    data = validate_payload({k: record[k] for k in FIELDS})
    artifacts = record["artifacts"]
    if len(artifacts) != 2 or {a["name"] for a in artifacts} != {
        "isp-calibration-source.json",
        "isp-calibration-normalized.json",
    }:
        raise ValueError("Calibration requires both original and normalized canonical artifacts")
    for artifact in artifacts:
        content = service.registry.blob_path(artifact["sha256"]).read_bytes()
        value = json.loads(content)
        expected = {
            "id": digest(value),
            "sha256": digest(value),
            "bytes": len(content),
            "name": artifact["name"],
            "media_type": "application/json",
            "url": f"/api/v1/artifacts/{digest(value)}",
        }
        if artifact != expected or content != canonical(value).encode():
            raise ValueError("Calibration artifact descriptor or canonical bytes differ")
        if (
            validate_payload(value) if artifact["name"] == "isp-calibration-source.json" else value
        ) != data:
            raise ValueError(
                "Calibration source/normalized bytes differ from its immutable payload"
            )
    if record.get("evidence_artifacts") != _references(service, data, seen):
        raise ValueError("Calibration evidence descriptors differ from preserved reference bytes")
    return record


def get_calibration(service, identity):
    """Resolve exact immutable numeric data and every referenced blob/parent."""
    from .imaging import replay_source

    record = _get(service, identity, set())
    ref = record["reference_source"]
    source = replay_source(service, ref["kind"], ref["id"])
    binding, models = _source_context(service, source)
    if (
        record["source_snapshot"] != source["source_snapshot"]
        or record["source_sha256"] != digest(source["source_snapshot"])
        or record["model_snapshots"] != models
    ):
        raise ValueError("Calibration reference RAW/model snapshot identity changed")
    _applicable(record, source, binding)
    return record


def resolve_for_source(service, identity, source):
    """Recheck the calibration and target native RAW for submit, cache and Worker."""
    record = get_calibration(service, identity)
    binding, models = _source_context(service, source)
    _applicable(record, source, binding)
    for document in [source["source_snapshot"], *models]:
        for key in ("applied_calibration_ids", "calibration_history"):
            for entry in document.get(key, []):
                if (
                    entry == identity
                    or isinstance(entry, dict)
                    and entry.get("calibration_id") == identity
                ):
                    raise ValueError(
                        "This calibration identity is already applied; refusing double correction"
                    )
    return record
