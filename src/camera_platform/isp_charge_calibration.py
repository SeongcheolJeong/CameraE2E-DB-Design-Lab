"""Declared charge-domain LSC evidence, never an inferred DN-noise calibration."""

import json
from copy import deepcopy

from . import isp_charge_source as sources
from .isp_calibration import FIELDS as LEGACY_FIELDS
from .isp_calibration import _exact, _number, _sha, _text
from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_charge_isp_calibration_v1"
NUMERIC_SCHEMA = "camera_charge_isp_calibration_numeric_v1"
SIGNAL_BASIS = "calibrated_photocharge_e_before_lsc"
FIELDS = LEGACY_FIELDS | {"schema"}


def validate_numeric_payload(payload):
    from .isp_calibration import validate_numeric_payload as validate_grid

    _exact(payload, {"schema", "sites", "lsc"}, "charge ISP numeric calibration")
    if payload["schema"] != NUMERIC_SCHEMA or payload["sites"] != ["R", "Gr", "Gb", "B"]:
        raise ValueError(
            "Explicit charge LSC calibration schema/sites required; DN noise is invalid"
        )
    checked = validate_grid(
        {
            "schema": "camera_isp_calibration_v1",
            "sites": payload["sites"],
            "lsc": payload["lsc"],
            "noise": None,
        }
    )
    return {"schema": NUMERIC_SCHEMA, "sites": checked["sites"], "lsc": checked["lsc"]}


def condition_rows(source):
    result = []
    for frame in source["frame_conditions"]:
        result.append(
            {
                **deepcopy(source["conditions"]),
                "exposure_s": {"value": frame["exposure_ns"] * 1e-9, "unit": "s"},
                "gain": {"value": frame["gain"], "unit": "1"},
                "temperature_c": {"value": frame["temperature_c"], "unit": "degC"},
                "readout_analog_gain": {"value": frame["readout_analog_gain"], "unit": "1"},
            }
        )
    return result


def source_binding(source):
    models = {
        m["kind"]: m
        for m in source["source_snapshot"]["model_snapshots"]
        if m["kind"] in {"sensor", "lens"}
    }
    readout_mode = (
        deepcopy(source["decoder_binding"])
        if source["source_kind"] == "capture"
        else {
            "plugin_id": source["source_snapshot"]["plugin_id"],
            "capture_program_id": source["capture_program_id"],
            "capture_program_sha256": source["capture_program_sha256"],
            "model_sha256": source["calibration_summaries"][0]["model_sha256"],
        }
    )
    return {
        "signal_basis": SIGNAL_BASIS,
        "normalization_e": source["normalization_e"],
        "sensor_model_id": models["sensor"]["id"],
        "sensor_model_sha256": digest(models["sensor"]),
        "lens_model_id": models["lens"]["id"],
        "lens_model_sha256": digest(models["lens"]),
        "full_sensor_shape": deepcopy(source["geometry"]["full_sensor_shape"]),
        "pixel_pitch_um": source["pixel_pitch_um"],
        "raw_format": deepcopy(source["raw_format"]),
        "selector": deepcopy(source["selector"]),
        "readout_mode": readout_mode,
    }


def validate_payload(payload):
    _exact(payload, FIELDS, "charge ISP calibration")
    data = deepcopy(payload)
    if (
        data["schema"] != SCHEMA
        or not isinstance(data["source_kind"], str)
        or data["source_kind"]
        not in {
            "estimated",
            "simulated",
            "measured",
        }
    ):
        raise ValueError("Explicit charge calibration schema and source kind required")
    for key in ["name", "revision", "source_uri", "license", "method_and_version"]:
        _text(data[key], key)
    for key in ["parents", "dataset_ids"]:
        values = data[key]
        if (
            not isinstance(values, list)
            or len(values) > 100
            or any(type(x) is not str for x in values)
        ):
            raise ValueError("Bounded calibration identity list required")
        if len(set(values)) != len(values):
            raise ValueError("Calibration identities must be unique")
        for value in values:
            _text(value, key)
    if data["physical_instance"] is not None:
        _text(data["physical_instance"], "physical instance")
    if data["instrument"] is not None:
        _exact(data["instrument"], {"id", "calibration_revision"}, "instrument")
        for value in data["instrument"].values():
            _text(value, "instrument")
    if not isinstance(data["dataset_role"], str) or data["dataset_role"] not in {
        "development",
        "holdout",
    }:
        raise ValueError("Calibration dataset role must be explicit")
    _exact(data["uncertainty"], {"kind", "reason"}, "uncertainty")
    if data["uncertainty"]["kind"] != "unknown":
        raise ValueError("Total calibration uncertainty remains unknown")
    _text(data["uncertainty"]["reason"], "uncertainty reason")
    ref = _exact(data["reference_source"], {"kind", "id", "selector"}, "reference source")
    if ref["kind"] not in ("run", "capture"):
        raise ValueError("Charge calibration references retained nonlinear RAW")
    _text(ref["id"], "source identity")
    ref["selector"] = sources.validate_selector(ref["selector"])
    if not isinstance(data["evidence_refs"], list) or len(data["evidence_refs"]) > 100:
        raise ValueError("Bounded explicit evidence references required")
    for ref in data["evidence_refs"]:
        _exact(ref, {"sha256", "role"}, "evidence reference")
        _sha(ref["sha256"])
        _text(ref["role"], "evidence role")
    if data["source_kind"] == "measured" and not all(
        data[k] for k in ["physical_instance", "instrument", "dataset_ids", "evidence_refs"]
    ):
        raise ValueError(
            "Measured calibration requires physical identity, instrument, datasets/evidence"
        )
    if not isinstance(data["binding"], dict) or data["binding"].get("signal_basis") != SIGNAL_BASIS:
        raise ValueError("Charge calibration must declare its pre-LSC electron signal basis")
    _exact(data["domain"], {"conditions"}, "domain")
    if not isinstance(data["domain"]["conditions"], dict) or not data["domain"]["conditions"]:
        raise ValueError("Explicit acquisition-condition domain required")
    for spec in data["domain"]["conditions"].values():
        if not isinstance(spec, dict):
            raise ValueError("Condition domain entries must be objects")
        if spec.get("kind") == "exact":
            _exact(spec, {"kind", "value"}, "exact condition")
        elif spec.get("kind") == "range":
            _exact(spec, {"kind", "min", "max", "unit"}, "condition range")
            low = _number(spec["min"], -1e20, 1e20, "range min")
            high = _number(spec["max"], low, 1e20, "range max")
            spec.update(min=low, max=high)
            _text(spec["unit"], "condition unit")
        else:
            raise ValueError("Condition domain requires exact or range entries")
    data["payload"] = validate_numeric_payload(data["payload"])
    return json.loads(canonical(data))


def _applicable(data, source):
    if data["binding"] != source_binding(source):
        raise ValueError("Charge calibration model/path/native/normalization binding differs")
    rules = data["domain"]["conditions"]
    for row in condition_rows(source):
        if set(row) != set(rules):
            raise ValueError("Calibration domain must cover every original acquisition condition")
        for key, spec in rules.items():
            value = row[key]
            if spec["kind"] == "exact":
                valid = value == spec["value"]
            else:
                valid = (
                    isinstance(value, dict)
                    and set(value) == {"value", "unit"}
                    and value["unit"] == spec["unit"]
                    and type(value["value"]) in (int, float)
                    and spec["min"] <= value["value"] <= spec["max"]
                )
            if not valid:
                raise ValueError("Charge calibration outside declared condition: " + key)
    lsc = data["payload"]["lsc"]
    if lsc is not None:
        for nodes, start, size, full in zip(
            [lsc["row_nodes"], lsc["column_nodes"]],
            source["geometry"]["crop_origin"],
            source["raw_shape"][-2:],
            source["geometry"]["full_sensor_shape"],
            strict=True,
        ):
            if nodes[0] > start or nodes[-1] < start + size - 1 or nodes[-1] > full - 1:
                raise ValueError("Charge LSC grid does not cover native crop without extrapolation")


def template(service, source_kind, source_id, selector):
    source = sources.resolve(service, source_kind, source_id, selector)
    height, width = source["geometry"]["full_sensor_shape"]
    rows = condition_rows(source)
    conditions = {}
    for key in rows[0]:
        values = [r[key] for r in rows]
        if all(v == values[0] for v in values):
            conditions[key] = {"kind": "exact", "value": values[0]}
        elif all(
            isinstance(v, dict) and set(v) == {"value", "unit"} and v["unit"] == values[0]["unit"]
            for v in values
        ):
            conditions[key] = {
                "kind": "range",
                "min": min(v["value"] for v in values),
                "max": max(v["value"] for v in values),
                "unit": values[0]["unit"],
            }
        else:
            raise ValueError("Source conditions require an explicit authored domain")
    return validate_payload(
        {
            "schema": SCHEMA,
            "name": "보정 전하 · 선언된 Native LSC",
            "revision": "charge-1",
            "source_kind": "estimated",
            "source_uri": "fixture://declared-charge-lsc-example",
            "license": "MIT; authored assumptions",
            "method_and_version": "declared_charge_lsc:1",
            "physical_instance": None,
            "instrument": None,
            "parents": [],
            "dataset_ids": [],
            "dataset_role": "development",
            "uncertainty": {
                "kind": "unknown",
                "reason": "Authored unity grid, not fitted or physically validated; "
                "decoder noise is separate.",
            },
            "reference_source": {"kind": source_kind, "id": source_id, "selector": selector},
            "binding": source_binding(source),
            "domain": {"conditions": conditions},
            "evidence_refs": [
                {"sha256": source["raw_sha256"], "role": "original RAW identity only"}
            ],
            "payload": {
                "schema": NUMERIC_SCHEMA,
                "sites": ["R", "Gr", "Gb", "B"],
                "lsc": {
                    "schema": "native_four_site_grid_v1",
                    "row_nodes": [0, height - 1],
                    "column_nodes": [0, width - 1],
                    "gains": [[[1.0] * 4] * 2] * 2,
                    "valid_mask": [[[True] * 4] * 2] * 2,
                    "interpolation": "bilinear",
                    "normalization": "declared_per_site_reference",
                    "reference_roi": {
                        "row_start": 0,
                        "row_end": height,
                        "column_start": 0,
                        "column_end": width,
                    },
                },
            },
        }
    )


def _references(service, data, seen):
    artifacts = []
    for i, ref in enumerate(data["evidence_refs"]):
        service.registry.artifact_owner(ref["sha256"])
        content = service.registry.blob_path(ref["sha256"]).read_bytes()
        artifact = {
            "id": ref["sha256"],
            "sha256": ref["sha256"],
            "bytes": len(content),
            "name": f"charge-isp-calibration-evidence-{i:03d}.bin",
            "media_type": "application/octet-stream",
            "url": "/api/v1/artifacts/" + ref["sha256"],
            "role": ref["role"],
        }
        sources.artifact_bytes(service, artifact)
        artifacts.append(artifact)
    for identity in data["parents"]:
        _get(service, identity, seen)
    return artifacts


def create(service, payload):
    data = validate_payload(payload)
    ref = data["reference_source"]
    source = sources.resolve(service, ref["kind"], ref["id"], ref["selector"])
    _applicable(data, source)
    evidence = _references(service, data, set())
    artifacts = [
        service.registry.store_blob(canonical(value).encode(), name, "application/json")
        for name, value in [
            ("charge-isp-calibration-source.json", payload),
            ("charge-isp-calibration-normalized.json", data),
        ]
    ]
    return service.registry.put(
        "isp_calibrations",
        {
            **data,
            "artifacts": artifacts,
            "evidence_artifacts": evidence,
            "source_snapshot": source["source_snapshot"],
            "source_sha256": digest(source["source_snapshot"]),
            "source_selection_sha256": digest(source),
            "model_snapshots": source["source_snapshot"]["model_snapshots"],
            "physical_validation": "not_established",
            "approval_status": "draft",
            "authority": {"physical_validation": False, "release": False},
        },
    )


def _get(service, identity, seen):
    if identity in seen or len(seen) >= 100:
        raise ValueError("Charge calibration parent graph must be bounded and acyclic")
    seen = seen | {identity}
    record = service.registry.get("isp_calibrations", identity)
    data = validate_payload({k: record[k] for k in FIELDS})
    entries = record["artifacts"]
    names = {"charge-isp-calibration-source.json", "charge-isp-calibration-normalized.json"}
    if len(entries) != 2 or {a["name"] for a in entries} != names:
        raise IntegrityError("Charge calibration source and normalized artifacts required")
    for a in entries:
        content = sources.artifact_bytes(service, a)
        value = json.loads(content)
        if content != canonical(value).encode() or (validate_payload(value) != data):
            raise IntegrityError("Charge calibration artifact differs from its immutable payload")
    if record["evidence_artifacts"] != _references(service, data, seen):
        raise IntegrityError("Charge calibration evidence changed")
    ref = data["reference_source"]
    source = sources.resolve(service, ref["kind"], ref["id"], ref["selector"])
    if (
        source["source_snapshot"] != record["source_snapshot"]
        or digest(source["source_snapshot"]) != record["source_sha256"]
        or digest(source) != record["source_selection_sha256"]
        or source["source_snapshot"]["model_snapshots"] != record["model_snapshots"]
    ):
        raise IntegrityError("Charge calibration original source/selection identity changed")
    _applicable(data, source)
    return record


def get(service, identity):
    return _get(service, identity, set())


get_calibration = get
import_calibration = create


def resolve_for_source(service, identity, source):
    record = get(service, identity)
    fresh = sources.resolve(service, source["source_kind"], source["source_id"], source["selector"])
    if source != fresh:
        raise IntegrityError("Charge calibration target source changed")
    _applicable(record, source)
    for document in [source["source_snapshot"], *source["source_snapshot"]["model_snapshots"]]:
        for key in ("applied_calibration_ids", "calibration_history"):
            if any(
                e == identity or isinstance(e, dict) and e.get("calibration_id") == identity
                for e in document.get(key, [])
            ):
                raise ValueError("Charge LSC calibration was already applied")
    return record
