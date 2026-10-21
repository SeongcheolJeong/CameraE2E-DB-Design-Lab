"""Registered RAW acquisition and the same frozen ROI operator on model predictions."""

from __future__ import annotations

import copy
import hashlib
import io
import math

import numpy as np

from .contracts import CaptureImport, MeasurementCreate, TestCreate
from .registry import ConflictError, canonical, digest

MAX_CAPTURE_BYTES = 64 * 1024 * 1024


def load_raw_npy(content: bytes) -> np.ndarray:
    """Inspect the NPY header before allocation; never deserialize objects or ZIP members."""
    if not content or len(content) > MAX_CAPTURE_BYTES:
        raise ValueError("RAW NPY input must be nonempty and no larger than 64 MiB")
    stream = io.BytesIO(content)
    try:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, fortran_order, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2, 0):
            shape, fortran_order, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError("Only NPY format versions 1 and 2 are supported")
    except (EOFError, TypeError) as exc:
        raise ValueError("Invalid NPY header") from exc
    if dtype.hasobject or dtype.fields or dtype.kind not in {"u", "i"} or dtype.itemsize > 4:
        raise ValueError("RAW codes must be an integer NPY array, never Python objects")
    if len(shape) != 3 or not all(isinstance(n, int) and n > 0 for n in shape):
        raise ValueError("RAW input must have [frames, rows, columns] shape")
    expected = math.prod(shape) * dtype.itemsize
    if expected > MAX_CAPTURE_BYTES or stream.tell() + expected != len(content):
        raise ValueError("NPY payload size does not match its bounded declared shape")
    if fortran_order:
        raise ValueError("RAW NPY must use C row-major order")
    return np.load(io.BytesIO(content), allow_pickle=False)


def _validate_sensor_geometry(assembly, models, specification, source_kind):
    sensor = next(m for m in models if m["kind"] == "sensor")
    raw = specification.get("raw_format", {})
    expected = assembly["raw_format"]
    for key, expected_key in (
        ("cfa", "cfa"),
        ("bit_depth", "bit_depth"),
        ("black_level_dn", "black_level"),
    ):
        if raw.get(key) != expected.get(expected_key):
            raise ValueError(f"Acquired RAW {key} disagrees with the pinned camera contract")
    geometry = specification.get("geometry", {})
    if source_kind == "measured" and geometry.get("kind") != "native_sensor":
        raise ValueError(
            "Physical RAW measurements require an explicit native sensor/crop geometry"
        )
    if geometry.get("kind") == "native_sensor":
        properties = sensor["properties"]
        width, height = properties.get("active_width", {}), properties.get("active_height", {})
        if width.get("unit") != "mm" or height.get("unit") != "mm":
            raise ValueError("Native capture geometry requires sensor active dimensions in mm")
        pitch = sensor["payload"]["pixel_pitch_um"]
        native = [height["value"] * 1000 / pitch, width["value"] * 1000 / pitch]
        if not all(abs(value - round(value)) <= 0.1 for value in native):
            raise ValueError("Sensor dimensions and pixel pitch do not determine an integer raster")
        if geometry.get("full_sensor_shape") != [round(value) for value in native]:
            raise ValueError(
                "Capture native sensor shape disagrees with dimensions and pixel pitch"
            )


def import_capture(service, metadata, raw_bytes, dark_bytes=None):
    from .measurement import analyze_raw_sequence

    data = CaptureImport.model_validate(metadata).model_dump()
    if len(raw_bytes) + len(dark_bytes or b"") > MAX_CAPTURE_BYTES:
        raise ValueError("Combined RAW and dark NPY inputs exceed 64 MiB")
    raw = load_raw_npy(raw_bytes)
    dark = load_raw_npy(dark_bytes) if dark_bytes is not None else None
    raw_hash = hashlib.sha256(canonical(list(raw.shape)).encode())
    raw_hash.update(raw.astype("<u4", copy=False).tobytes(order="C"))
    raw_content_hash = raw_hash.hexdigest()
    if any(
        c.get("raw_content_hash") == raw_content_hash for c in service.registry.list("captures")
    ):
        raise ConflictError(
            "Identical RAW pixels are already registered; "
            "dataset renaming is not independent evidence"
        )
    assembly = service.registry.get("assemblies", data["assembly_id"])
    base_test = service.registry.get("tests", data["test_id"])
    models = [service.registry.get("models", i) for i in assembly["model_ids"]]
    for measurement in service.registry.list("measurements"):
        if set(measurement["dataset_ids"]) & set(data["dataset_ids"]):
            raise ConflictError("This capture dataset identity is already registered")
    specification = {**copy.deepcopy(data["observation"]), "source_kind": data["source_kind"]}
    if specification.get("conditions") != base_test["conditions"]:
        raise ValueError("Actual acquisition conditions must match the selected frozen test")
    _validate_sensor_geometry(assembly, models, specification, data["source_kind"])
    analyzed = analyze_raw_sequence(raw, dark, specification)
    frame_hashes = []
    for frame in raw:
        frame_hash = hashlib.sha256(canonical(list(frame.shape)).encode())
        frame_hash.update(frame.astype("<u4", copy=False).tobytes(order="C"))
        frame_hashes.append(frame_hash.hexdigest())
    if any(
        set(c.get("raw_frame_hashes", [])) & set(frame_hashes)
        for c in service.registry.list("captures")
    ):
        raise ConflictError(
            "RAW frames overlap an existing capture; preserve its original identity"
        )
    measured = analyzed["measurement"]
    if measured["included_effects"] != base_test["included_effects"]:
        if set(measured["included_effects"]) != set(base_test["included_effects"]):
            raise ValueError("Observed RAW effects do not match this test's metric definition")
    # Freeze acquisition analysis as a new test revision without changing acceptance criteria.
    observation_specification = copy.deepcopy(specification)
    observation_specification.pop("source_kind", None)
    observation_shape = list(raw.shape[-2:])
    operator = measured["observation_method"]
    if base_test.get("observation_method") is not None:
        if (
            base_test["observation_method"] != operator
            or base_test.get("observation_specification") != observation_specification
            or base_test.get("observation_shape") != observation_shape
        ):
            raise ValueError("Capture operator differs from the selected observation test revision")
        test = base_test
    else:
        payload = {
            k: copy.deepcopy(v) for k, v in base_test.items() if k in TestCreate.model_fields
        }
        payload.update(
            name=base_test["name"] + " · RAW 관측",
            parent_test_id=base_test["id"],
            revision=base_test["revision"][:40] + "+roi-" + digest(operator)[:12],
            observation_method=operator,
            observation_specification=observation_specification,
            observation_shape=observation_shape,
        )
        matching = [
            t
            for t in service.registry.list("tests")
            if all(t.get(k) == payload.get(k) for k in TestCreate.model_fields)
        ]
        test = matching[0] if matching else service.create_test(payload)
    instrument = copy.deepcopy(data["instrument"])
    instrument["raw_analysis"] = analyzed["diagnostics"]
    instrument["saturation_diagnostics"] = analyzed["diagnostics"]["saturation_diagnostics"]
    # The analysis helper reports ADC linearity; it cannot prove hidden full-well behavior.
    measurement_payload = {
        k: copy.deepcopy(v) for k, v in measured.items() if k in MeasurementCreate.model_fields
    }
    measurement_payload.update(
        {
            k: copy.deepcopy(data[k])
            for k in (
                "name",
                "revision",
                "source_kind",
                "source_uri",
                "license",
                "physical_instance",
                "parents",
                "dataset_ids",
            )
        }
    )
    measurement_payload.update(
        test_id=test["id"],
        assembly_id=assembly["id"],
        instrument=instrument,
        valid_domain=service._domain_intersection(models),
    )
    measurement_payload = MeasurementCreate.model_validate(measurement_payload).model_dump()
    artifacts = [
        service.registry.store_blob(raw_bytes, "raw-capture.npy", "application/octet-stream")
    ]
    if dark_bytes is not None:
        artifacts.append(
            service.registry.store_blob(dark_bytes, "dark-capture.npy", "application/octet-stream")
        )
    artifacts.append(
        service.registry.store_blob(
            canonical(data).encode(), "acquisition-metadata.json", "application/json"
        )
    )
    capture = service.registry.put(
        "captures",
        {
            **data,
            "test_id": test["id"],
            "base_test_id": base_test["id"],
            "name": data["name"],
            "raw_shape": list(raw.shape),
            "raw_content_hash": raw_content_hash,
            "raw_frame_hashes": frame_hashes,
            "dark_shape": list(dark.shape) if dark is not None else None,
            "artifacts": artifacts,
            "observation_method": operator,
            "analysis_measurement": measured,
            "measurement_contract": copy.deepcopy(measurement_payload),
            "diagnostics": analyzed["diagnostics"],
            "physical_validation": "not_established",
        },
    )
    measurement_payload["raw_capture_id"] = capture["id"]
    measurement = service.create_measurement(measurement_payload)
    return {
        "capture": capture,
        "measurement": measurement,
        "test": test,
        "next_action": "Run this pinned assembly with the returned observation test, then compare",
    }


def verify_capture_measurement(service, measurement):
    """Bind a measurement to the preserved acquisition and deterministically replay its analysis."""
    from .measurement import analyze_raw_sequence

    identifier = measurement.get("raw_capture_id")
    if not identifier:
        return None
    capture = service.registry.get("captures", identifier)
    if capture.get("capture_kind") == "control_response_observation_v1":
        from .control_response_service import verify_capture

        return verify_capture(service, measurement, capture)
    expected = copy.deepcopy(capture["measurement_contract"])
    expected["raw_capture_id"] = identifier
    actual = {
        k: copy.deepcopy(v) for k, v in measurement.items() if k in MeasurementCreate.model_fields
    }
    if (
        MeasurementCreate.model_validate(actual).model_dump()
        != MeasurementCreate.model_validate(expected).model_dump()
    ):
        raise ValueError("Measurement contract differs from its preserved RAW capture analysis")
    artifacts = {a["name"]: service.registry.blob_path(a["sha256"]) for a in capture["artifacts"]}
    raw = load_raw_npy(artifacts["raw-capture.npy"].read_bytes())
    dark = (
        load_raw_npy(artifacts["dark-capture.npy"].read_bytes())
        if "dark-capture.npy" in artifacts
        else None
    )
    specification = {**copy.deepcopy(capture["observation"]), "source_kind": capture["source_kind"]}
    analysis = analyze_raw_sequence(raw, dark, specification)
    if (
        analysis["measurement"] != capture["analysis_measurement"]
        or analysis["diagnostics"] != capture["diagnostics"]
    ):
        raise ValueError("Replayed RAW analysis differs from the frozen capture evidence")
    return capture


def capture_content_ids(capture):
    return sorted({"raw-frame-sha256:" + value for value in capture["raw_frame_hashes"]})


def observe_prediction(summary, test):
    """Apply the acquisition ROI operator to continuous expected RAW."""
    from .measurement import observation_method, observation_sampling_weights

    specification = {
        **copy.deepcopy(test["observation_specification"]),
        "source_kind": "simulated",
        "conditions": test["conditions"],
    }
    expected_method = observation_method(
        specification,
        test["observation_shape"],
        dark_present=test["observation_method"].get("dark_correction") == "mean_dark_frame",
    )
    if expected_method != test["observation_method"]:
        raise ValueError("Frozen observation method does not match its specification")
    sampling = observation_sampling_weights(
        specification, test["observation_shape"], summary["field_radius"]
    )
    signal = np.asarray(summary["channel_reference_signal_dn"], dtype=float)
    dark_mode = test["observation_method"].get("dark_correction")
    if dark_mode == "black_level_only":
        readout = summary["readout"]
        dark_e = readout["dark_current_e_per_s"] * summary["conditions"]["exposure_s"]["value"]
        capacity = min(readout["full_well_e"], readout["full_well_e"] / readout["gain"])
        dn_per_e = (
            (2 ** readout["bit_depth"] - 1 - readout["black_level_dn"])
            * readout["gain"]
            / readout["full_well_e"]
        )
        signal = signal + min(dark_e, capacity) * dn_per_e
    means = np.einsum("rcf,fc->rc", np.asarray(sampling["channel_field_weights"]), signal)
    if not np.isfinite(means).all() or (means[:, 1] <= 0).any():
        raise ValueError("ROI expectation has no finite positive green denominator")
    ratios = means[:, [0, 2]] / means[:, 1:2]
    prediction = copy.deepcopy(summary["prediction"])
    prediction.update(
        values=ratios.reshape(-1).tolist(),
        axes=[
            {"name": "field_radius", "unit": "1", "values": sampling["roi_field_radius"]},
            {"name": "channel_ratio", "unit": "1", "values": ["R/G", "B/G"]},
        ],
        observation_method=copy.deepcopy(test["observation_method"]),
        sample_geometry={"specification": test["observation_specification"], "sampling": sampling},
        channel_means_dn=means.tolist(),
        method_and_version="response_db_v1+raw_roi_observation_v1",
    )
    if "uncertainty_budget" in prediction:
        from .uncertainty import observe_budget

        prediction["uncertainty_budget"] = observe_budget(
            prediction["uncertainty_budget"], sampling, dark_mode
        )
        prediction["uncertainty_budget"]["axes"] = copy.deepcopy(prediction["axes"])
    return prediction, {
        "sampling": sampling,
        "channel_means_dn": means.tolist(),
        "channel_ratios": ratios.tolist(),
        "field_radius": sampling["roi_field_radius"],
        "uncertainty": {"kind": "unknown"},
        "interpretation": (
            "Continuous expected RAW averaged over the frozen CFA/ROI geometry; "
            "ADC rounding, finite frame statistics and model error remain unquantified"
        ),
    }


def capture_template(service):
    from .demo import CONDITIONS, PROVENANCE

    info = service.bootstrap()
    test = service.registry.get("tests", info["demo"]["test_id"])
    return {
        **copy.deepcopy(PROVENANCE),
        "name": "RAW 입력 형식 · 실제 값으로 교체",
        "revision": "1",
        "test_id": info["demo"]["test_id"],
        "assembly_id": info["demo"]["assembly_ids"][0],
        "dataset_ids": ["replace-with-unique-acquisition-id"],
        "instrument": {},
        "observation": {
            "source_kind": "estimated",
            "conditions": copy.deepcopy(CONDITIONS),
            "included_effects": copy.deepcopy(test["included_effects"]),
            "raw_format": {
                "cfa": "RGGB",
                "bit_depth": 12,
                "black_level_dn": 64,
                "response_type": "linear",
            },
            "geometry": {
                "kind": "representative_normalized_field_raster",
                "raster_shape": [64, 64],
                "mapping": "center_to_corner_radius_v1",
            },
            "rois": [
                {"id": "center", "field_radius": 0.0, "box": [24, 24, 16, 16]},
                {"id": "middle", "field_radius": 0.5, "box": [8, 8, 16, 16]},
                {"id": "edge", "field_radius": 1.0, "box": [0, 0, 8, 8]},
            ],
        },
    }
