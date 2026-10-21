"""Verified nonlinear RAW views; original paired acquisition bytes remain authoritative."""

from __future__ import annotations

import hashlib
import io
import json
import math
from copy import deepcopy

import numpy as np

from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_charge_isp_source_v1"
SELECTION_SCHEMA = "camera_nonlinear_raw_selection_v1"
CAPTURE_SELECTION_SCHEMA = "camera_nonlinear_raw_selection_v2"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_SELECTED_PIXELS = 1_048_576
ARRAY_ENCODING = "shape_dtype_json_then_C_little_endian_int32_v1"


def validate_selector(value):
    if not isinstance(value, dict) or value.get("schema") not in (
        SELECTION_SCHEMA, CAPTURE_SELECTION_SCHEMA,
    ):
        raise ValueError("Explicit nonlinear RAW selection schema required")
    extra = set()
    if value["schema"] == CAPTURE_SELECTION_SCHEMA:
        identity = value.get("readout_calibration_id")
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 200:
            raise ValueError("Imported RAW requires an explicit response calibration identity")
        extra.add("readout_calibration_id")
    mode = value.get("mode")
    if mode == "sequential_frames" and set(value) == {"schema", "mode"} | extra:
        return deepcopy(value)
    if (
        mode == "shared_charge_path"
        and set(value) == {"schema", "mode", "readout_id"} | extra
        and isinstance(value["readout_id"], str)
        and value["readout_id"] in {"hcg", "lcg"}
    ):
        return deepcopy(value)
    raise ValueError("Select all sequential frames or one explicit hcg/lcg shared-charge path")


def artifact_bytes(service, artifact):
    """Hash the same bounded bytes consumed below, not just a previously checked path."""
    if (
        type(artifact.get("bytes")) is not int
        or not 0 <= artifact["bytes"] <= MAX_INPUT_BYTES
        or artifact.get("id") != artifact.get("sha256")
        or artifact.get("url") != "/api/v1/artifacts/" + artifact.get("sha256", "")
    ):
        raise IntegrityError("Invalid bounded acquisition artifact descriptor")
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    if (
        len(content) != artifact["bytes"]
        or hashlib.sha256(content).hexdigest() != artifact["sha256"]
    ):
        raise IntegrityError("Acquisition artifact bytes differ from their descriptor")
    return content


def _artifact(record, name):
    entries = [a for a in record["artifacts"] if a.get("name") == name]
    if len(entries) != 1:
        raise IntegrityError("Exactly one original acquisition artifact required: " + name)
    return deepcopy(entries[0])


def _array(service, artifact, *, dimensions, kind, unit):
    content = artifact_bytes(service, artifact)
    stream = io.BytesIO(content)
    try:
        version = np.lib.format.read_magic(stream)
        reader = {
            (1, 0): np.lib.format.read_array_header_1_0,
            (2, 0): np.lib.format.read_array_header_2_0,
        }[version]
        shape, fortran, dtype = reader(stream)
    except (KeyError, ValueError, EOFError, TypeError) as exc:
        raise ValueError("Unsupported bounded acquisition NPY header") from exc
    if (
        len(shape) != dimensions
        or any(type(n) is not int or n <= 0 for n in shape)
        or fortran
        or dtype.hasobject
        or dtype.fields
        or dtype.kind not in kind
        or dtype.itemsize > (4 if kind == "iu" else 8)
        or (kind == "f" and dtype.itemsize != 8)
        or math.prod(shape) * dtype.itemsize + stream.tell() != len(content)
    ):
        raise ValueError("Acquisition NPY shape, dtype or byte count is invalid")
    if (
        artifact.get("shape") != list(shape)
        or artifact.get("dtype") != str(dtype)
        or artifact.get("unit") != unit
        or artifact.get("spatial_sampling") != "native_sensor"
    ):
        raise IntegrityError("Acquisition array metadata differs from its retained bytes")
    count, height, width = shape[0], *shape[-2:]
    if (
        not 1 <= count <= 64
        or height % 2
        or width % 2
        or min(height, width) < 2
        or max(height, width) > 4096
        or count * height * width > MAX_SELECTED_PIXELS
        or (dimensions == 4 and shape[1] != 2)
    ):
        raise ValueError("Nonlinear source exceeds native frame/pixel bounds")
    return np.load(io.BytesIO(content), allow_pickle=False)


def _source_geometry(service, record, frame_shape):
    """Bind receiver RAW geometry to registered source support without expanding images."""
    from .imaging import native_sensor_grid

    scene = record["scene_sequence_snapshot"]
    if record["request"]["scene_sequence"] != scene["payload"]:
        raise IntegrityError("Frozen scene sequence differs from its registered source")
    knots, snapshots = scene["payload"]["knots"], scene["image_snapshots"]
    if not knots or len(knots) != len(snapshots):
        raise IntegrityError("Scene image snapshot count differs from its knots")
    sampling = snapshots[0]["sampling"]
    source_shape = snapshots[0]["shape"][:2]
    for knot, snapshot in zip(knots, snapshots, strict=True):
        image = service.registry.get("spectral_images", knot["spectral_image_id"])
        if (
            image != snapshot
            or image["sampling"] != sampling
            or image["shape"][:2] != source_shape
        ):
            raise IntegrityError("Native scene sampling differs from retained acquisition")
    sensors = [
        model
        for model in record["model_snapshots"]
        if model["kind"] == "sensor" and model["id"] in record["assembly_snapshot"]["model_ids"]
    ]
    if len(sensors) != 1:
        raise IntegrityError("Exactly one original native sensor is required")
    pitch, full = native_sensor_grid(sensors[0])
    if (
        sampling["kind"] != "native_sensor"
        or sampling["full_sensor_shape"] != full
        or sampling["pixel_pitch_um"] != pitch
        or record["request"]["pixel_pitch_um"] != pitch
    ):
        raise IntegrityError("Native scene pitch/geometry differs from original sensor request")
    source_origin = sampling["crop_origin"]
    if any(source_origin[axis] + source_shape[axis] > full[axis] for axis in (0, 1)):
        raise IntegrityError("Registered scene source support exceeds the original sensor")
    capture = sampling.get("capture_crop")
    frozen_crop = record.get("parameters", {}).get("capture_crop")
    if frozen_crop is not None:
        stimulus = record["test_snapshot"].get("stimulus", {})
        if (stimulus.get("capture_crop") != frozen_crop
                or stimulus.get("source_support") != {
                    "kind": sampling["kind"],
                    "full_sensor_shape": full,
                    "crop_origin": source_origin,
                    "shape": source_shape,
                }
                or (capture is not None and capture != frozen_crop)):
            raise IntegrityError("Receiver crop differs from frozen Test and source support")
        capture = frozen_crop
    elif record["test_snapshot"].get("stimulus", {}).get("capture_crop") is not None:
        raise IntegrityError("Frozen receiver crop is missing from run parameters")
    if capture is None:
        if source_shape != list(frame_shape):
            raise IntegrityError("Native scene raster differs from retained receiver RAW")
        receiver_origin = source_origin
    else:
        if (capture.get("shape") != list(frame_shape)
                or any(capture["crop_origin"][axis] < source_origin[axis]
                       or capture["crop_origin"][axis] + frame_shape[axis]
                       > source_origin[axis] + source_shape[axis]
                       for axis in (0, 1))):
            raise IntegrityError("Frozen receiver crop differs from retained RAW/source support")
        receiver_origin = capture["crop_origin"]
    geometry = {
        "kind": "native_sensor", "full_sensor_shape": deepcopy(full),
        "crop_origin": deepcopy(receiver_origin),
    }
    return geometry, pitch


def _verify_unmodulated_source(service, record, summary, program, controls):
    """Close the second authored component before nonlinear RAW is reused by ISP."""
    from .hdr_runtime import ILLUMINATION_COMPONENT_SCHEMA

    stimulus = record["test_snapshot"].get("stimulus", {})
    dual = stimulus.get("schema") in {
        "electrical_hdr_components_test_v1", "electrical_hdr_components_test_v2",
    }
    markers = (
        record.get("unmodulated_scene_sequence_id"),
        record.get("unmodulated_scene_sequence_snapshot"),
        record.get("parameters", {}).get("unmodulated_scene_sequence_id"),
        record.get("request", {}).get("unmodulated_scene_sequence"),
        summary.get("unmodulated_scene_sequence_id"),
        summary.get("unmodulated_scene_sequence_hash"),
        summary.get("illumination_components"),
        stimulus.get("unmodulated_scene_sequence_id"),
    )
    if not dual:
        if any(marker is not None for marker in markers):
            raise IntegrityError("Unbound second illumination component in retained acquisition")
        return
    if stimulus["schema"] != "electrical_hdr_components_test_v2":
        raise ValueError("Nonlinear ISP source requires the v2 component RAW Test")
    identity = record.get("unmodulated_scene_sequence_id")
    scene = record.get("unmodulated_scene_sequence_snapshot")
    if (not isinstance(identity, str) or not isinstance(scene, dict)
            or record.get("plugin_id") != "hdr_bracket_v1"
            or program["payload"].get("waveform") is None
            or record.get("parameters", {}).get("unmodulated_scene_sequence_id") != identity
            or scene.get("id") != identity
            or service.registry.get("scene_sequences", identity) != scene
            or record["request"].get("unmodulated_scene_sequence") != scene["payload"]
            or stimulus.get("unmodulated_scene_sequence_id") != identity
            or stimulus.get("unmodulated_scene_sequence_hash") != scene["content_hash"]
            or stimulus.get("illumination_component_schema") != ILLUMINATION_COMPONENT_SCHEMA
            or summary.get("unmodulated_scene_sequence_id") != identity
            or summary.get("unmodulated_scene_sequence_hash") != scene["content_hash"]):
        raise IntegrityError("Second illumination sequence differs from frozen Test/source/summary")
    component = summary.get("illumination_components")
    if (not isinstance(component, dict)
            or component.get("schema") != ILLUMINATION_COMPONENT_SCHEMA
            or component.get("reference_plane") != "sensor_ideal_image_before_psf_ri"
            or not isinstance(component.get("modulated"), dict)
            or not isinstance(component.get("unmodulated"), dict)
            or component.get("modulated", {}).get("scene_sequence") != "scene_sequence"
            or component.get("unmodulated", {}).get("scene_sequence")
            != "unmodulated_scene_sequence"):
        raise IntegrityError("Second illumination component summary differs from its RAW stage")
    primary = record["scene_sequence_snapshot"]
    if (scene["payload"]["clock"] != primary["payload"]["clock"]
            or scene["payload"]["clock"] != controls["clock"]):
        raise IntegrityError("Second illumination clock differs from the frozen acquisition")
    knots, images = scene["payload"]["knots"], scene["image_snapshots"]
    if not knots or len(knots) != len(images):
        raise IntegrityError("Second illumination source image count differs from its knots")
    fixed = ("wavelength_nm", "unit", "reference_plane", "sampling",
             "included_effects", "shape")
    primary_image = primary["image_snapshots"][0]
    for knot, image in zip(knots, images, strict=True):
        if (service.registry.get("spectral_images", knot["spectral_image_id"]) != image
                or any(image[key] != primary_image[key] for key in fixed)):
            raise IntegrityError("Second illumination image differs from frozen optical source")
    inputs = {(item["sha256"], item["name"]): item
              for item in record["input_artifacts"]}
    for owner in (scene, *images):
        for artifact in owner["artifacts"]:
            if inputs.get((artifact["sha256"], artifact["name"])) != artifact:
                raise IntegrityError("Second illumination source artifact missing from RAW inputs")
            service.registry.blob_path(artifact["sha256"])


def array_sha256(array):
    normalized = np.ascontiguousarray(array, dtype="<i4")
    value = hashlib.sha256(canonical({"shape": list(array.shape), "dtype": "<i4"}).encode())
    value.update(normalized.tobytes())
    return value.hexdigest()


def _resolve(service, source_kind, source_id, selector):
    from . import nonlinear_dcg, nonlinear_readout
    from .capture_programs import verify_program
    from .native_model_evidence import verify_models

    selector = validate_selector(selector)
    if source_kind == "capture":
        from .isp_capture_source import resolve_inputs

        if selector["schema"] != CAPTURE_SELECTION_SCHEMA:
            raise ValueError("Imported nonlinear RAW requires a v2 response calibration selection")
        return resolve_inputs(service, source_id, selector)
    if source_kind != "run" or selector["schema"] != SELECTION_SCHEMA:
        raise ValueError("Retained nonlinear simulation requires its original v1 RAW selection")
    record = service.registry.get_run(source_id)
    if record.get("plugin_id") in {"retained_charge_v1", "electrical_control_v1"}:
        raise ValueError(
            "Retained photodiode RAW is delivered total charge with frame history; "
            "the current-exposure photocharge decoder is unsupported"
        )
    paired = selector["mode"] == "shared_charge_path"
    expected = (
        {"shared_charge_dcg_v1": "camera_dcg_capture_program_v2"}
        if paired
        else {
            "hdr_bracket_v1": "camera_capture_program_v2",
            "reference_hdr_v1": "camera_reference_hdr_capture_program_v2",
        }
    )
    if record.get("status") != "succeeded" or record.get("plugin_id") not in expected:
        raise ValueError("Selection requires a completed matching nonlinear acquisition")
    program = service.registry.get("capture_programs", record["capture_program_id"])
    verify_program(service, program, record["plugin_id"])
    if (
        program != record.get("capture_program_snapshot")
        or program["payload"]["schema"] != expected[record["plugin_id"]]
    ):
        raise IntegrityError("Nonlinear acquisition program binding differs")
    for kind, key in [
        ("assemblies", "assembly"),
        ("tests", "test"),
        ("control_sequences", "control_sequence"),
        ("scene_sequences", "scene_sequence"),
    ]:
        if service.registry.get(kind, record[key + "_id"]) != record[key + "_snapshot"]:
            raise IntegrityError("Acquisition snapshot differs from registered " + key)
    for model in record["model_snapshots"]:
        if service.registry.get("models", model["id"]) != model:
            raise IntegrityError("Acquisition model snapshot differs")
    verify_models(service, record["model_snapshots"])
    from .factory import verify_model_link, verify_psf_model_link

    for model in record["model_snapshots"]:
        if model.get("psf_factory_job_id"):
            verify_psf_model_link(service, model)
        elif model.get("factory_job_id"):
            verify_model_link(service, model)
    summary = record["result"]["summary"]
    summary_artifact = _artifact(record, "summary.json")
    content = artifact_bytes(service, summary_artifact)
    if content != canonical(summary).encode() or json.loads(content) != summary:
        raise IntegrityError("Saved acquisition summary differs from completed result")
    controls = record["control_sequence_snapshot"]["payload"]
    if (
        summary["capture_program"] != program["payload"]
        or summary["capture_program_hash"] != program["content_hash"]
        or summary["control_sequence"] != controls
        or record["request"]["control_sequence"] != controls
    ):
        raise IntegrityError("Saved program/control acquisition settings differ")
    if (
        summary.get("source_kind") != "simulated"
        or summary.get("capture_program_id") != program["id"]
        or record["request"].get("capture_program") != program["payload"]
    ):
        raise IntegrityError("Acquisition provenance or frozen electrical request differs")
    _verify_unmodulated_source(service, record, summary, program, controls)
    raw_artifact = _artifact(record, "raw_groups.npy" if paired else "raw_frames.npy")
    dark_artifact = _artifact(record, "calibration_dark_e.npy")
    container = _array(service, raw_artifact, dimensions=4 if paired else 3, kind="iu", unit="DN")
    dark = _array(service, dark_artifact, dimensions=3, kind="f", unit="electron/pixel")
    count, height, width = container.shape[0], *container.shape[-2:]
    if dark.shape != (count, height, width):
        raise IntegrityError("Dark calibration shape differs from retained RAW")
    index = {"hcg": 0, "lcg": 1}.get(selector.get("readout_id"))
    raw = container[:, index] if paired else container
    model = program["payload"]["electrical_model"]
    normalization = model["shared_charge_capacity_e"] if paired else model["full_well_e"]
    geometry, pitch = _source_geometry(service, record, (height, width))
    from .timing import summarize_timing

    timing = summarize_timing(controls, geometry=geometry, frame_shape=(height, width))
    if (
        summary["geometry"] != geometry
        or summary["pixel_pitch_um"] != pitch
        or summary["clock"] != controls["clock"]
        or summary["timing"] != timing
    ):
        raise IntegrityError(
            "Acquisition geometry, pitch, clock or timing differs from original inputs"
        )
    records = summary["acquisitions"] if paired else summary["frames"]
    cals = summary["calibrations"]
    if not (len(records) == len(cals) == len(controls["frames"]) == count):
        raise IntegrityError("RAW, calibration and acquisition record counts differ")
    if paired and (
        summary.get("raw_axes") != ["acquisition", "readout", "row", "column"]
        or summary.get("readout_ids") != ["HCG", "LCG"]
    ):
        raise IntegrityError("Shared-charge readout order differs")
    calibrations, frames, conditions, identities = [], [], [], []
    temperature = record["request"]["temperature_c"]
    noise = record["request"].get("noise_enabled", True)
    for i, (entry, saved, control) in enumerate(
        zip(cals, records, controls["frames"], strict=True)
    ):
        index_key = "acquisition_index" if paired else "frame_index"
        if (
            entry.get(index_key) != i
            or type(entry.get(index_key)) is not int
            or entry.get("dark_e_array") != "calibration_dark_e"
        ):
            raise IntegrityError("Calibration acquisition index differs")
        if saved.get("timing") != timing["frames"][i]:
            raise IntegrityError("Acquisition record timing differs from original control")
        cal = {k: deepcopy(v) for k, v in entry.items() if k not in {index_key, "dark_e_array"}}
        cal["dark_e"] = dark[i]
        if cal["model"] != model:
            raise IntegrityError("Calibration model differs from registered program")
        checked = (
            nonlinear_dcg.validate_calibration(cal, (height, width))
            if paired
            else nonlinear_readout.validate_calibration(cal, (height, width))
        )
        acq = checked["acquisition"]
        expected_acq = {
            "exposure_s": control["applied"]["exposure_ns"] * 1e-9,
            "temperature_c": temperature,
            "noise_enabled": noise,
            "geometry": {k: geometry[k] for k in ("full_sensor_shape", "crop_origin")},
        }
        if not paired:
            expected_acq["analog_gain"] = control["applied"]["gain"]
        if acq != expected_acq:
            raise IntegrityError("Calibration settings differ from original acquisition")
        frame_id = control["frame_id"]
        if paired:
            if (
                saved.get("acquisition_id") != frame_id
                or saved.get("acquisition_index") != i
                or saved.get("shared_charge_group_id") != frame_id
                or saved.get("recorded_frame") != control
                or saved.get("readout_ids") != ["HCG", "LCG"]
            ):
                raise IntegrityError("Shared-charge acquisition identity differs")
            selected = nonlinear_dcg.calibration_for_path(checked, index)
        else:
            if (
                saved.get("frame_id") != frame_id
                or saved.get("frame_index") != i
                or any(saved.get(k) != v for k, v in control.items())
            ):
                raise IntegrityError("Sequential frame identity differs")
            selected = checked
        nonlinear_readout.validate_calibration(selected, (height, width))
        white = 2 ** selected["model"]["adc"]["bit_depth"] - 1
        if np.any(raw[i] < 0) or np.any(raw[i] > white):
            raise ValueError("RAW code exceeds selected path ADC domain")
        identity = {
            "source_id": source_id,
            "acquisition_id": frame_id,
            "acquisition_index": i,
            "shared_charge_group_id": frame_id if paired else None,
            "readout_id": selector.get("readout_id"),
            "readout_index": index,
        }
        frame = deepcopy(control)
        frame.update(frame_index=i, source_acquisition=identity)
        frames.append(frame)
        identities.append(identity)
        conditions.append(
            {
                "frame_id": frame_id,
                "frame_start_ns": control["frame_start_ns"],
                "applied_at_ns": control["applied"]["applied_at_ns"],
                "exposure_ns": control["applied"]["exposure_ns"],
                "gain": control["applied"]["gain"],
                "isp_model_id": control["applied"]["isp_model_id"],
                "temperature_c": temperature,
                "readout_analog_gain": selected["acquisition"]["analog_gain"],
            }
        )
        calibrations.append(selected)
    raw_format = deepcopy(summary["raw_formats"][index] if paired else summary["raw_format"])
    from .hdr_runtime import _raw_format

    if raw_format != _raw_format(calibrations[0]["model"]):
        raise IntegrityError("Stored RAW format differs from selected calibration model")
    for artifact in program["artifacts"]:
        artifact_bytes(service, artifact)
    source = {
        "schema": SCHEMA,
        "source_kind": source_kind,
        "source_id": source_id,
        "name": record["name"],
        "selector": selector,
        "raw_sha256": raw_artifact["sha256"],
        "raw_container_shape": list(container.shape),
        "raw_shape": list(raw.shape),
        "selected_array_sha256": array_sha256(raw),
        "selected_array_hash_encoding": ARRAY_ENCODING,
        "raw_format": raw_format,
        "geometry": geometry,
        "normalization_e": normalization,
        "pixel_pitch_um": summary["pixel_pitch_um"],
        "source_snapshot": record,
        "source_provenance": {
            "source_kind": "simulated",
            "source_uri": "run://" + source_id,
            "physical_instance": None,
            "method_and_version": record["plugin_id"],
        },
        "conditions": deepcopy(record["test_snapshot"]["conditions"]),
        "frame_conditions": conditions,
        "frame_records": frames,
        "clock": deepcopy(summary["clock"]),
        "timing": deepcopy(summary["timing"]),
        "capture_program_id": program["id"],
        "capture_program_sha256": digest(program),
        "calibration_summaries": [
            {k: v for k, v in c.items() if k != "dark_e"} for c in calibrations
        ],
        "raw_artifact": raw_artifact,
        "dark_artifact": dark_artifact,
        "input_artifacts": [raw_artifact, dark_artifact, *deepcopy(program["artifacts"])],
        "original_acquisition_records": deepcopy(records),
        "acquisition_identity": identities,
    }
    return source, raw, calibrations


def resolve(service, source_kind, source_id, selector):
    return _resolve(service, source_kind, source_id, selector)[0]


def load_inputs(service, source):
    fresh, raw, calibrations = _resolve(
        service, source["source_kind"], source["source_id"], source["selector"]
    )
    if fresh != source:
        raise IntegrityError("Selected nonlinear RAW source differs from its frozen evidence")
    return raw, calibrations
