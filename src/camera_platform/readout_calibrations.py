"""Immutable imported charge-response evidence and expected-dark interpretation.

No fitting, random pattern generation, or physical provenance authentication is
performed. A selected calibration interprets an acquisition; it never changes it.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from copy import deepcopy

import numpy as np

from . import electrical, nonlinear_dcg, nonlinear_readout
from .imaging import native_sensor_grid
from .readout_calibration_contracts import (
    EVIDENCE_SCHEMA,
    FIELDS,
    SCHEMA,
    ReadoutCalibrationImport,
    validate_evidence,
    validate_metadata,
)
from .registry import IntegrityError, canonical, digest

MAX_IMPORT_BYTES = 64 * 1024**2
MAX_CLOSURE_BYTES = 256 * 1024**2
MAX_PREPARED_PIXELS = 1_000_000
NAMES = {"readout-calibration-metadata.json", "expected-dark-rate.npy", "readout-evidence.json"}


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _json(content):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate calibration JSON key")
            value[key] = item
        return value

    def nonfinite(value):
        raise ValueError("Nonfinite calibration JSON number: " + value)

    try:
        return json.loads(content, object_pairs_hook=unique, parse_constant=nonfinite)
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("Invalid bounded calibration JSON") from exc


def _dark_rate(content, data):
    if type(content) is not bytes or not 0 < len(content) <= MAX_IMPORT_BYTES:
        raise ValueError("Expected-dark-rate upload requires bounded original NPY bytes")
    stream = io.BytesIO(content)
    try:
        version = np.lib.format.read_magic(stream)
        reader = {
            (1, 0): np.lib.format.read_array_header_1_0,
            (2, 0): np.lib.format.read_array_header_2_0,
        }[version]
        shape, fortran, dtype = reader(stream)
    except (KeyError, ValueError, EOFError, TypeError) as exc:
        raise ValueError("Unsupported expected-dark-rate NPY header") from exc
    if (
        list(shape) != data["dark_rate_shape"]
        or fortran
        or dtype.hasobject
        or dtype.fields
        or dtype.kind not in "iuf"
        or dtype.itemsize > 8
        or math.prod(shape) * dtype.itemsize + stream.tell() != len(content)
    ):
        raise ValueError("Expected-dark-rate NPY shape, dtype, layout or byte count differs")
    # Header, product and exact bytes are checked before NumPy allocates the array.
    array = np.load(io.BytesIO(content), allow_pickle=False)
    if not np.isfinite(array).all() or np.any(array < 0) or np.any(array > electrical.MAX_CHARGE_E):
        raise ValueError("Expected dark rates require finite nonnegative electron/pixel/s")
    largest_exposure = data["applicability"]["exposure_s"][1]
    for temperature in data["applicability"]["temperature_c"]:
        factor = _temperature_factor(data["electrical_model"], temperature)
        with np.errstate(over="ignore", invalid="ignore"):
            maximum = float(np.max(array)) * factor * largest_exposure
        if not math.isfinite(maximum) or maximum > electrical.MAX_CHARGE_E:
            raise ValueError("Declared dark-rate domain exceeds the supported expected charge")
    return array


def _temperature_factor(model, temperature):
    doubling = model["dark_current_doubling_c"]
    if doubling is None:
        return 1.0
    exponent = (temperature - model["reference_temperature_c"]) / doubling
    if not -1074 <= exponent <= 1023:
        raise ValueError("Dark-rate temperature law is outside the finite numeric domain")
    value = math.exp2(exponent)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Dark-rate temperature law is outside the finite numeric domain")
    return value


def _sensor(service, data):
    sensor = service.registry.get("models", data["sensor_model_id"])
    if sensor["kind"] != "sensor":
        raise ValueError("Readout calibration requires a sensor model revision")
    pitch, shape = native_sensor_grid(sensor)
    if data["pixel_pitch_um"] != pitch or data["geometry"]["full_sensor_shape"] != shape:
        raise ValueError("Calibration native sensor geometry or pixel pitch differs")
    if sensor.get("raw_format", {}).get("cfa", "RGGB") != "RGGB":
        raise ValueError("Imported response currently supports native RGGB sensors")
    return sensor


def _get_capture(service, identity):
    from .nonlinear_captures import get

    return get(service, identity)


def _capture_closure(service, capture):
    from .nonlinear_captures import evidence_closure

    return evidence_closure(service, capture)


def _capture_references(captures):
    references = []
    for capture in captures:
        references.append(("captures", capture))
        references.extend(("models", model) for model in capture.get("model_snapshots", []))
        for field, kind in (("assembly_snapshot", "assemblies"), ("test_snapshot", "tests")):
            if field in capture:
                references.append((kind, capture[field]))
    return references


def _capture_sensor(capture):
    sensors = [m for m in capture["model_snapshots"] if m["kind"] == "sensor"]
    if len(sensors) != 1:
        raise ValueError("Capture requires exactly one original sensor snapshot")
    return sensors[0]


def _references(service, data):
    captures = [_get_capture(service, identity) for identity in data["reference_capture_ids"]]
    for capture in captures:
        if (
            _capture_sensor(capture)["id"] != data["sensor_model_id"]
            or capture["geometry"]["full_sensor_shape"] != data["geometry"]["full_sensor_shape"]
            or capture["pixel_pitch_um"] != data["pixel_pitch_um"]
            or (
                data["physical_instance"] is not None
                and capture["physical_instance"] != data["physical_instance"]
            )
        ):
            raise ValueError("Response-fitting capture differs from sensor/physical native binding")
    return captures


def _raw_formats(model, mode):
    paths = model["paths"] if mode == "shared_charge_paths" else [model]
    return [
        {
            "cfa": "RGGB",
            "bit_depth": path["adc"]["bit_depth"],
            "black_level_dn": path["adc"]["black_level_dn"],
            "response_type": "nonlinear_charge_voltage",
            "packing": "unpacked",
            "layout": "bayer",
            "hdr_streams": 1,
        }
        for path in paths
    ]


def _normalization(data):
    key = (
        "shared_charge_capacity_e"
        if data["readout_mode"] == "shared_charge_paths"
        else "full_well_e"
    )
    return data["electrical_model"][key]


def _dark_description(array, data):
    return {
        "shape": list(array.shape),
        "dtype": str(array.dtype),
        "unit": "electron/pixel/s",
        "reference_temperature_c": data["dark_reference_temperature_c"],
        "value_sha256": _sha(
            canonical(list(array.shape)).encode()
            + np.ascontiguousarray(array, dtype="<f8").tobytes()
        ),
        "value_hash_encoding": "canonical shape JSON then C-order little-endian float64",
    }


def import_calibration(service, metadata, dark_rate_bytes, evidence_bytes, *, metadata_bytes=None):
    """Publish after validating every original byte and immutable reference."""
    data = validate_metadata(metadata)
    original = canonical(metadata).encode() if metadata_bytes is None else metadata_bytes
    blobs = [original, dark_rate_bytes, evidence_bytes]
    if (
        any(type(b) is not bytes or not b for b in blobs)
        or sum(len(b) for b in blobs) > MAX_IMPORT_BYTES
    ):
        raise ValueError(
            "Original calibration metadata, dark map and evidence total at most 64 MiB"
        )
    if validate_metadata(_json(original)) != data:
        raise ValueError("Original metadata bytes differ from the supplied calibration")
    evidence = validate_evidence(_json(evidence_bytes), data)
    rate = _dark_rate(dark_rate_bytes, data)
    sensor = _sensor(service, data)
    captures = _references(service, data)
    state = _state()
    parents = [_tree(service, identity, state, set()) for identity in data["parents"]]
    for capture in captures:
        state["captures"].update({r["id"]: r for r in _capture_closure(service, capture)})
    if (
        len(state["records"]) >= 100
        or state["bytes"] + sum(len(b) for b in blobs) > MAX_CLOSURE_BYTES
        or len(set(state["captures"]) | {r["id"] for r in captures}) > 100
    ):
        raise ValueError("Response calibration ancestry exceeds revision/capture/byte budgets")
    for parent in parents:
        if (
            parent["sensor_model_id"] != data["sensor_model_id"]
            or parent["physical_instance"] != data["physical_instance"]
        ):
            raise ValueError("Response calibration ancestry changes sensor or physical instance")
    artifacts = [
        service.registry.store_blob(content, name, media)
        for content, name, media in [
            (original, "readout-calibration-metadata.json", "application/json"),
            (dark_rate_bytes, "expected-dark-rate.npy", "application/x-npy"),
            (evidence_bytes, "readout-evidence.json", "application/json"),
        ]
    ]
    description = _dark_description(rate, data)
    artifacts[1].update({key: description[key] for key in ("shape", "dtype", "unit")})
    references = [("models", sensor)]
    references.extend(("readout_calibrations", p) for p in state["records"].values())
    references.extend(_capture_references(state["captures"].values()))
    return service.registry.put_verified_record(
        "readout_calibrations",
        {
            **data,
            "artifacts": artifacts,
            "sensor_model_snapshot": sensor,
            "sensor_model_sha256": digest(sensor),
            "reference_capture_sha256": {r["id"]: digest(r) for r in captures},
            "parent_calibration_sha256": {r["id"]: digest(r) for r in parents},
            "electrical_model_sha256": digest(data["electrical_model"]),
            "evidence_sha256": _sha(evidence_bytes),
            "evidence_normalized_sha256": digest(evidence),
            "dark_rate": description,
            "raw_formats": _raw_formats(data["electrical_model"], data["readout_mode"]),
            "normalization_e": _normalization(data),
            "physical_validation": "not_established",
            "approval_status": "draft",
            "authority": {"physical_validation": False, "release": False},
            "interpretation": "supplied_absolute_charge_calibration_no_fitting_or_authentication",
        },
        references,
    )


def _artifact_bytes(service, artifact):
    name = artifact.get("name")
    expected_keys = {"id", "name", "sha256", "bytes", "media_type", "url"}
    if name == "expected-dark-rate.npy":
        expected_keys |= {"shape", "dtype", "unit"}
    if (
        name not in NAMES
        or set(artifact) != expected_keys
        or artifact.get("media_type")
        != ("application/x-npy" if name == "expected-dark-rate.npy" else "application/json")
        or type(artifact.get("bytes")) is not int
        or not 0 < artifact["bytes"] <= MAX_IMPORT_BYTES
        or artifact.get("id") != artifact.get("sha256")
        or artifact.get("url") != "/api/v1/artifacts/" + artifact.get("sha256", "")
    ):
        raise IntegrityError("Invalid original calibration artifact descriptor")
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    if len(content) != artifact["bytes"] or _sha(content) != artifact["sha256"]:
        raise IntegrityError("Original calibration artifact bytes differ")
    return content


def _tree(service, identity, state, active):
    if identity in active:
        raise IntegrityError("Cyclic response calibration ancestry")
    if identity in state["records"]:
        return state["records"][identity]
    if len(state["visited"]) >= 100:
        raise IntegrityError("Response calibration ancestry exceeds 100 revisions")
    state["visited"].add(identity)
    record = service.registry.get("readout_calibrations", identity)
    if record.get("schema") == "camera_fitted_response_calibration_v1":
        from .readout_fitted_calibrations import verify

        return verify(service, record, state, active)
    data = validate_metadata({key: record[key] for key in FIELDS})
    artifacts = record["artifacts"]
    if len(artifacts) != 3 or {a["name"] for a in artifacts} != NAMES:
        raise IntegrityError("Exactly three original response calibration artifacts required")
    if any(type(a.get("bytes")) is not int or a["bytes"] <= 0 for a in artifacts):
        raise IntegrityError("Invalid calibration artifact sizes")
    size = sum(a["bytes"] for a in artifacts)
    state["bytes"] += size
    if size > MAX_IMPORT_BYTES or state["bytes"] > MAX_CLOSURE_BYTES:
        raise IntegrityError("Calibration import or ancestry exceeds its byte budget")
    by_name = {a["name"]: a for a in artifacts}
    contents = {name: _artifact_bytes(service, artifact) for name, artifact in by_name.items()}
    if validate_metadata(_json(contents["readout-calibration-metadata.json"])) != data:
        raise IntegrityError("Original metadata no longer matches normalized response calibration")
    evidence = validate_evidence(_json(contents["readout-evidence.json"]), data)
    rate = _dark_rate(contents["expected-dark-rate.npy"], data)
    sensor, captures = _sensor(service, data), _references(service, data)
    description = _dark_description(rate, data)
    expected = {
        "sensor_model_snapshot": sensor,
        "sensor_model_sha256": digest(sensor),
        "reference_capture_sha256": {r["id"]: digest(r) for r in captures},
        "electrical_model_sha256": digest(data["electrical_model"]),
        "evidence_sha256": _sha(contents["readout-evidence.json"]),
        "evidence_normalized_sha256": digest(evidence),
        "dark_rate": description,
        "raw_formats": _raw_formats(data["electrical_model"], data["readout_mode"]),
        "normalization_e": _normalization(data),
        "physical_validation": "not_established",
        "approval_status": "draft",
        "authority": {"physical_validation": False, "release": False},
        "interpretation": "supplied_absolute_charge_calibration_no_fitting_or_authentication",
    }
    if any(record[key] != value for key, value in expected.items()):
        raise IntegrityError("Response calibration immutable metadata or evidence binding differs")
    dark_artifact = by_name["expected-dark-rate.npy"]
    if any(dark_artifact.get(k) != description[k] for k in ("shape", "dtype", "unit")):
        raise IntegrityError("Expected-dark-rate artifact semantics differ")
    parents = [_tree(service, p, state, active | {identity}) for p in data["parents"]]
    if record["parent_calibration_sha256"] != {r["id"]: digest(r) for r in parents}:
        raise IntegrityError("Response calibration parent identities differ")
    if any(
        p["sensor_model_id"] != data["sensor_model_id"]
        or p["physical_instance"] != data["physical_instance"]
        for p in parents
    ):
        raise IntegrityError("Response calibration parent native binding differs")
    state["records"][identity] = record
    for capture in captures:
        state["captures"].update({r["id"]: r for r in _capture_closure(service, capture)})
    if len(state["captures"]) > 100:
        raise IntegrityError("Response calibration ancestry exceeds 100 fitting captures")
    return record


def _state():
    return {"records": {}, "captures": {}, "bytes": 0, "visited": set()}


def get(service, identity):
    """Every call revalidates original bytes and references; no process-global cache."""
    return _tree(service, identity, _state(), set())


def lineage(service, calibration):
    """Keep decoder fitting exposure identity separate from shared calibration blobs."""
    state = _state()
    record = _tree(service, calibration["id"], state, set())
    if record != calibration:
        raise IntegrityError("Selected response calibration differs from its original revision")
    tokens = set(state.get("extra_fitting_tokens", ()))
    for value in state["records"].values():
        tokens.update("dataset-id:" + identity for identity in value["dataset_ids"])
    for capture in state["captures"].values():
        if "exposure_tokens" in capture:
            tokens.update(capture["exposure_tokens"])
        else:
            # Legacy parents retain the same explicit exposure identity domains.
            tokens.add("capture-id:" + capture["id"])
            tokens.update("dataset-id:" + identity for identity in capture.get("dataset_ids", []))
            tokens.update("raw-frame-sha256:" + sha for sha in capture.get("raw_frame_hashes", []))
    records = [state["records"][key] for key in sorted(state["records"])]
    captures = [state["captures"][key] for key in sorted(state["captures"])]
    extras = {}
    if "fit_jobs" in state:
        extras = {
            "readout_observations": [state["observations"][k]
                                     for k in sorted(state["observations"])],
            "readout_fit_jobs": [state["fit_jobs"][k] for k in sorted(state["fit_jobs"])],
            "evaluation_captures": [state["evaluation_captures"][k]
                                    for k in sorted(state["evaluation_captures"])],
            "decoder_evaluation_tokens": sorted(state["decoder_evaluation_tokens"]),
        }
    evidence_records = records + captures + [
        record for key, values in extras.items() if key != "decoder_evaluation_tokens"
        for record in values
    ]
    evidence = {(a["sha256"], a["name"]): a
                for record in evidence_records for a in record["artifacts"]}
    return {
        "calibrations": records,
        "reference_captures": captures,
        "decoder_fitting_tokens": sorted(tokens),
        "evidence_artifacts": [evidence[key] for key in sorted(evidence)],
        **extras,
    }


def prepare_for_capture(service, calibration_id, capture):
    """Build expected dark and exact kernel calibrations from recorded applied settings."""
    fresh = _get_capture(service, capture["id"])
    if fresh != capture:
        raise IntegrityError("Selected capture differs from its immutable original")
    record = get(service, calibration_id)
    sensor = _capture_sensor(capture)
    if (
        sensor != record["sensor_model_snapshot"]
        or capture["readout_mode"] != record["readout_mode"]
        or capture["geometry"]["full_sensor_shape"] != record["geometry"]["full_sensor_shape"]
        or capture["pixel_pitch_um"] != record["pixel_pitch_um"]
        or capture["raw_formats"] != record["raw_formats"]
    ):
        raise ValueError("Capture sensor, native sampling, path mode or RAW format differs")
    if (
        record["physical_instance"] is not None
        and capture["physical_instance"] != record["physical_instance"]
    ):
        raise ValueError("Capture physical instance differs from response calibration")
    if capture["source_kind"] == "measured" and (
        record["charge_reference"]["kind"] == "authored_simulation"
        or record["physical_instance"] is None
    ):
        raise ValueError("Measured capture requires an external anchor and exact physical instance")
    frames, shape = capture["frames"], capture["raw_shape"][-2:]
    if (
        not 1 <= len(frames) <= 64
        or len(frames) * math.prod(shape) > MAX_PREPARED_PIXELS
        or list(capture["raw_shape"])[0] != len(frames)
    ):
        raise ValueError("Prepared nonlinear calibration exceeds frame/pixel limits")
    origin = [
        a - b
        for a, b in zip(
            capture["geometry"]["crop_origin"], record["geometry"]["crop_origin"], strict=True
        )
    ]
    if any(
        start < 0 or start + count > bound
        for start, count, bound in zip(origin, shape, record["dark_rate_shape"], strict=True)
    ):
        raise ValueError("Capture crop lies outside the expected dark-rate calibration map")
    model, domain = record["electrical_model"], record["applicability"]
    conditions = []
    for frame in frames:
        if frame["settings_evidence"]["status"] != "recorded_applied":
            raise ValueError(
                "Charge replay requires recorded applied settings, not commands or unknowns"
            )
        values = frame["applied"]
        exposure_ns = values["exposure_ns"]
        if type(exposure_ns) not in (int, float) or isinstance(exposure_ns, bool):
            raise ValueError("Recorded exposure is required")
        exposure, temperature = exposure_ns * 1e-9, values["temperature_c"]
        for value, key in ((exposure, "exposure_s"), (temperature, "temperature_c")):
            if type(value) not in (int, float) or not domain[key][0] <= value <= domain[key][1]:
                raise ValueError("Capture outside response applicability: " + key)
        readouts = values["readout_settings"]
        if len(readouts) != len(domain["readouts"]):
            raise ValueError("Capture path count differs from response calibration")
        for observed, expected in zip(readouts, domain["readouts"], strict=True):
            analog = observed["analog_gain"]
            if (
                type(analog) not in (int, float)
                or not expected["analog_gain"][0] <= analog <= expected["analog_gain"][1]
                or observed["digital_gain"] != expected["digital_gain"]
            ):
                raise ValueError("Applied readout gain differs from response calibration")
        shared = record["readout_mode"] == "shared_charge_paths"
        if values["gain"] != (1.0 if shared else readouts[0]["analog_gain"]):
            raise ValueError("Common capture gain cannot silently change calibrated readout gains")
        if shared:
            nonlinear_dcg.validate_capture_conditions(model, exposure, temperature)
        else:
            nonlinear_readout.validate_capture_conditions(
                model, exposure, readouts[0]["analog_gain"], temperature
            )
        conditions.append((exposure, temperature, readouts[0]["analog_gain"]))
    # All frame/settings/domain bounds precede allocation of F x H x W dark maps.
    artifact = next(a for a in record["artifacts"] if a["name"] == "expected-dark-rate.npy")
    rate = _dark_rate(_artifact_bytes(service, artifact), record)
    rate = rate[origin[0] : origin[0] + shape[0], origin[1] : origin[1] + shape[1]].astype(float)
    dark, calibrations = [], []
    native = {key: capture["geometry"][key] for key in ("full_sensor_shape", "crop_origin")}
    for exposure, temperature, analog in conditions:
        with np.errstate(over="ignore", invalid="ignore"):
            mean = rate * _temperature_factor(model, temperature) * exposure
        if not np.isfinite(mean).all() or np.any(mean > electrical.MAX_CHARGE_E):
            raise ValueError("Expected dark charge exceeds the supported finite charge domain")
        if record["readout_mode"] == "shared_charge_paths":
            cal = nonlinear_dcg._make_calibration(
                model,
                exposure_s=exposure,
                temperature_c=temperature,
                geometry=native,
                dark_e=mean,
                noise_enabled=True,
            )
        else:
            cal = nonlinear_readout.make_calibration(
                model,
                exposure_s=exposure,
                temperature_c=temperature,
                analog_gain=analog,
                geometry=native,
                dark_e=mean,
                noise_enabled=True,
            )
        dark.append(mean)
        calibrations.append(cal)
    return {
        "record": record,
        "raw_formats": deepcopy(record["raw_formats"]),
        "normalization_e": record["normalization_e"],
        "calibrations": calibrations,
        "dark_e": np.stack(dark),
    }


def template(service, sensor_model_id, mode="sequential_frames"):
    """Authored metadata starting point; callers must supply dark and evidence files."""
    from .nonlinear_examples import shared_model, single_model

    if mode not in {"sequential_frames", "shared_charge_paths"}:
        raise ValueError("Unknown imported readout mode")
    sensor = service.registry.get("models", sensor_model_id)
    pitch, full = native_sensor_grid(sensor)
    shared = mode == "shared_charge_paths"
    model = (shared_model if shared else single_model)(sensor["payload"]["readout"])
    paths = model["paths"] if shared else [model]
    return ReadoutCalibrationImport.model_validate(
        {
            "schema": SCHEMA,
            "name": "Imported nonlinear response · authored starting example",
            "revision": "1",
            "source_kind": "simulated",
            "source_uri": "fixture://readout-calibration",
            "license": "Authored numerical example",
            "method_and_version": "declared-response:1",
            "physical_instance": None,
            "parents": [],
            "sensor_model_id": sensor_model_id,
            "readout_mode": mode,
            "geometry": {"full_sensor_shape": full, "crop_origin": [0, 0]},
            "pixel_pitch_um": pitch,
            "dark_rate_shape": [min(full[0], 32), min(full[1], 32)],
            "dark_rate_unit": "electron/pixel/s",
            "dark_reference_temperature_c": 25.0,
            "electrical_model": model,
            "instrument": {"id": "authored-example", "calibration_revision": "1"},
            "dataset_role": "development",
            "dataset_ids": ["authored-readout-response-1"],
            "reference_capture_ids": [],
            "charge_reference": {
                "kind": "authored_simulation",
                "reference_id": "authored-electron-scale-1",
                "source_uri": "fixture://absolute-electron-scale",
                "method_and_version": "declared-electrons:1",
                "instrument": {"id": "authored-example", "calibration_revision": "1"},
                "uncertainty": {
                    "kind": "absolute_bound",
                    "value": 0.0,
                    "unit": "electron",
                    "description": "Authored mathematical scale only; no physical bound.",
                },
                "evidence_document_ids": ["charge-anchor-1"],
            },
            "uncertainty": {
                "kind": "unknown",
                "reason": (
                    "Supplied response; no fitting, uncertainty propagation or physical validation."
                ),
            },
            "applicability": {
                "exposure_s": [1e-9, 3600.0],
                "temperature_c": [25.0, 25.0],
                "readouts": [
                    {
                        "readout_id": ["HCG", "LCG"][i] if shared else "single",
                        "analog_gain": [path["analog_gain"], path["analog_gain"]]
                        if shared
                        else path["charge_voltage_response"]["valid_domain"]["analog_gain"],
                        "digital_gain": path["digital_gain"],
                    }
                    for i, path in enumerate(paths)
                ],
            },
        }
    ).model_dump()


def evidence_template(metadata):
    """A visibly authored declaration, never a generated measured certificate."""
    data = validate_metadata(metadata)
    if data["charge_reference"]["kind"] != "authored_simulation":
        raise ValueError(
            "External charge anchors require the original externally supplied evidence"
        )
    return {
        "schema": EVIDENCE_SCHEMA,
        "documents": [
            {
                "id": identity,
                "role": "charge_reference",
                "source_uri": data["charge_reference"]["source_uri"],
                "method_and_version": data["charge_reference"]["method_and_version"],
                "description": (
                    "Authored numeric electron scale for implementation verification only."
                ),
                "tables": [],
            }
            for identity in data["charge_reference"]["evidence_document_ids"]
        ],
    }
