"""Verified code-domain RAW views of completed retained-photodiode acquisitions.

The original run and its reservoir diagnostics remain evidence only. Processing
receives one explicitly selected path's integer RAW for every recorded frame,
with its original linear/nonlinear code format; no charge inverse is supplied.
"""

from __future__ import annotations

import io
import json
import math
from copy import deepcopy

import numpy as np

from .isp_charge_source import (
    ARRAY_ENCODING,
    _artifact,
    _source_geometry,
    array_sha256,
    artifact_bytes,
)
from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_retained_code_isp_source_v1"
SELECTION_SCHEMA = "camera_retained_raw_selection_v1"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_SELECTED_PIXELS = 1_000_000
PROCESSING_CONTRACT = {
    "quantity": "delivered_total_read_node_codes",
    "charge_inversion": False,
    "dark_subtraction": False,
    "normalization": "adc_black_to_white",
    "truth_arrays_used": False,
}


def validate_selector(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "readout_id"}
        or value["schema"] != SELECTION_SCHEMA
        or not isinstance(value["readout_id"], str)
        or value["readout_id"] not in {"single", "HCG", "LCG"}
    ):
        raise ValueError("Select an explicit case-sensitive retained RAW path: single, HCG or LCG")
    return deepcopy(value)


def _raw_array(service, artifact):
    """Inspect bounded original [frame,path,row,column] NPY before allocating it."""
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
        raise ValueError("Unsupported retained RAW NPY header") from exc
    if (
        len(shape) != 4
        or any(type(n) is not int or n <= 0 for n in shape)
        or fortran
        or dtype.hasobject
        or dtype.fields
        or dtype.kind not in "iu"
        or dtype.itemsize > 4
        or math.prod(shape) * dtype.itemsize + stream.tell() != len(content)
    ):
        raise ValueError("Retained RAW requires an exact bounded integer F,P,H,W NPY")
    frames, paths, height, width = shape
    if (
        not 1 <= frames <= 16
        or paths not in (1, 2)
        or height % 2
        or width % 2
        or min(height, width) < 2
        or max(height, width) > 4096
        or frames * height * width > MAX_SELECTED_PIXELS
    ):
        raise ValueError("Retained RAW exceeds the 16-frame/1000000-selected-pixel bounds")
    if (
        artifact.get("shape") != list(shape)
        or artifact.get("dtype") != str(dtype)
        or artifact.get("unit") != "DN"
        or artifact.get("spatial_sampling") != "native_sensor"
    ):
        raise IntegrityError("Retained RAW descriptor differs from original array bytes")
    return np.load(io.BytesIO(content), allow_pickle=False)


def _descriptor(model, frame, temperature, noise, shape, readouts):
    """Derive a forward receipt from original settings without executing a reader."""
    from .electrical import _delivered_descriptor

    family = model["schema"]
    orders = {
        "camera_electrical_readout_v1": [
            "input_read_noise",
            "conversion_and_analog_gain",
            "post_gain_voltage_noise",
            "ADC_nearest_even_clip",
            "black_relative_digital_gain_nearest_even_clip",
        ],
        "camera_electrical_readout_v2": [
            "charge_voltage_response",
            "conversion_output_voltage_read_noise",
            "analog_gain",
            "ADC_nearest_even_clip",
            "black_relative_digital_nearest_even_clip",
        ],
        "camera_shared_charge_readout_v1": [
            "per_path_input_linear_ceiling_clip",
            "joint_input_referred_read_noise",
            "conversion_and_analog_gain",
            "ADC_nearest_even_clip",
            "black_relative_digital_nearest_even_clip",
        ],
        "camera_shared_charge_readout_v2": [
            "per_path_input_linear_ceiling_clip",
            "charge_voltage_response",
            "joint_conversion_output_voltage_read_noise",
            "analog_gain",
            "ADC_nearest_even_clip",
            "black_relative_digital_nearest_even_clip",
        ],
    }
    extra = {"processing_order": orders[family]}
    if len(readouts) == 1:
        extra["analog_gain"] = frame["applied"]["gain"]
    else:
        unit = "v2" if family.endswith("_v2") else "e2"
        covariance = model["read_noise"]["covariance_" + unit]
        extra["read_noise_covariance_" + unit] = (
            deepcopy(covariance) if noise else [[0.0, 0.0], [0.0, 0.0]]
        )
    return {
        "frame_id": frame["frame_id"],
        **_delivered_descriptor(model, temperature, noise, shape, readouts, **extra),
    }


def _verify_light_components(service, record, summary, program):
    """Bind both authored optical inputs before their delivered RAW is reused."""
    from .hdr_runtime import ILLUMINATION_COMPONENT_SCHEMA

    stimulus = record["test_snapshot"]["stimulus"]
    dual = stimulus.get("schema") == "retained_charge_components_test_v1"
    markers = (
        record.get("unmodulated_scene_sequence_id"),
        record.get("unmodulated_scene_sequence_snapshot"),
        record.get("parameters", {}).get("unmodulated_scene_sequence_id"),
        record.get("request", {}).get("unmodulated_scene_sequence"),
        summary.get("unmodulated_scene_sequence_id"),
        summary.get("unmodulated_scene_sequence_hash"),
        summary.get("illumination_components"),
        summary.get("unmodulated_scene_knots"),
        summary.get("unmodulated_scene_sources"),
        summary.get("unmodulated_scene_interpolation"),
        summary.get("unmodulated_flux_diagnostics_per_scene"),
        summary.get("integration"),
        stimulus.get("unmodulated_scene_sequence_id"),
        stimulus.get("unmodulated_scene_sequence_hash"),
        stimulus.get("illumination_component_schema"),
    )
    if not dual:
        if any(marker is not None for marker in markers):
            raise IntegrityError("Unbound second illumination component in retained acquisition")
        return
    identity = record.get("unmodulated_scene_sequence_id")
    scene = record.get("unmodulated_scene_sequence_snapshot")
    if (not isinstance(identity, str) or not isinstance(scene, dict)
            or program["payload"]["schema"] not in {
                "camera_retained_charge_capture_program_v1",
                "camera_retained_charge_capture_program_v2",
                "camera_retained_charge_capture_program_v3",
                "camera_retained_charge_capture_program_v4",
            }
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
        raise IntegrityError("Second retained illumination source differs from frozen evidence")
    expected_components = {
        "schema": ILLUMINATION_COMPONENT_SCHEMA,
        "reference_plane": "sensor_ideal_image_before_psf_ri",
        "modulated": {"scene_sequence": "scene_sequence",
                      "row_weights_array": "row_scene_integration_weights_s",
                      "channel_electrons_array": "modulated_channel_electrons"},
        "unmodulated": {"scene_sequence": "unmodulated_scene_sequence",
                        "row_weights_array": "row_unmodulated_integration_weights_s",
                        "channel_electrons_array": "unmodulated_channel_electrons"},
        "summation": ("channel_electrons = modulated_channel_electrons "
                      "+ unmodulated_channel_electrons"),
    }
    expected_waveform = {
        "specification": program["payload"]["waveform"],
        "on_time_correction": False,
        "scope": "modulated_component_only",
        "independent_ambient_component": True,
        "effective_seconds_scope": "modulated_component_only",
        "photo_hdr_row_integral_normalization": False,
    }
    if (summary.get("illumination_components") != expected_components
            or summary.get("waveform") != expected_waveform):
        raise IntegrityError("Retained illumination summary differs from its optical inputs")
    primary = record["scene_sequence_snapshot"]
    controls = record["control_sequence_snapshot"]["payload"]
    if (scene["payload"]["clock"] != primary["payload"]["clock"]
            or scene["payload"]["clock"] != controls["clock"]):
        raise IntegrityError("Second retained illumination clock differs from capture")
    knots, images = scene["payload"]["knots"], scene["image_snapshots"]
    if not knots or len(knots) != len(images):
        raise IntegrityError("Second retained illumination image count differs from knots")
    fixed = ("wavelength_nm", "unit", "reference_plane", "sampling",
             "included_effects", "shape")
    primary_image = primary["image_snapshots"][0]
    capture_crop = record.get("parameters", {}).get("capture_crop")
    if capture_crop is not None:
        sampling = primary_image["sampling"]
        if (stimulus.get("capture_crop") != capture_crop
                or stimulus.get("source_support") != {
                    "kind": sampling["kind"],
                    "full_sensor_shape": sampling["full_sensor_shape"],
                    "crop_origin": sampling["crop_origin"],
                    "shape": primary_image["shape"][:2],
                }):
            raise IntegrityError("Second retained receiver crop differs from frozen Test")
    elif stimulus.get("capture_crop") is not None:
        raise IntegrityError("Second retained receiver crop is missing from run parameters")
    for knot, image in zip(knots, images, strict=True):
        if (service.registry.get("spectral_images", knot["spectral_image_id"]) != image
                or any(image[key] != primary_image[key] for key in fixed)):
            raise IntegrityError("Second retained image differs from frozen optical source")
    from .sequences import _capture_scene
    from .temporal import _scene_hash

    lens_id = next(model["id"] for model in record["model_snapshots"]
                   if model["kind"] == "lens")
    known, expected_knots, expected_sources = {}, [], []
    for knot, image in zip(knots, images, strict=True):
        expanded = _capture_scene(service, image, lens_id, capture_crop)
        content_hash = _scene_hash(
            expanded, np.asarray(expanded["spectral_irradiance"], dtype=float))
        if content_hash not in known:
            known[content_hash] = len(expected_sources)
            expected_sources.append(expanded["source"])
        expected_knots.append({"time_ns": knot["time_ns"],
                               "scene_index": known[content_hash],
                               "scene_content_hash": content_hash})
    trapped = program["payload"]["schema"] in {
        "camera_retained_charge_capture_program_v2",
        "camera_retained_charge_capture_program_v4",
    }
    expected_integration = (
        "independent exact short-interval integration of waveform-modulated and "
        "unmodulated declared linear/ZOH irradiance bases before one trapped PD "
        "population evolution"
        if trapped else
        "independent exact row integration of waveform-modulated and unmodulated "
        "declared linear/ZOH irradiance bases before one retained PD transition"
    )
    if (summary.get("unmodulated_scene_knots") != expected_knots
            or summary.get("unmodulated_scene_sources") != expected_sources
            or summary.get("unmodulated_scene_interpolation")
            != scene["payload"]["interpolation"]
            or summary.get("integration") != expected_integration):
        raise IntegrityError("Second retained scene summary differs from frozen optical knots")
    inputs = {(item["sha256"], item["name"]): item
              for item in record["input_artifacts"]}
    for owner in (scene, *images):
        for artifact in owner["artifacts"]:
            if inputs.get((artifact["sha256"], artifact["name"])) != artifact:
                raise IntegrityError("Second retained illumination artifact missing from RAW")
            artifact_bytes(service, artifact)


def _resolve_checked(service, source_id, selector):
    from .capture_programs import verify_program
    from .factory import verify_model_link, verify_psf_model_link
    from .native_model_evidence import verify_models
    from .retained_charge_runtime import (
            GAIN_MERGE_PROGRAM_SCHEMA,
            METRIC_ID,
            OBSERVATION,
            PHOTO_HDR_PROGRAM_SCHEMA,
        PREVIEW,
        PROGRAM_SCHEMA,
        TRAPPED_GAIN_MERGE_PROGRAM_SCHEMA,
        TRAPPED_PROGRAM_SCHEMA,
        _paired,
        _raw_formats,
        event_schedule,
    )
    from .sequences import frame_conditions
    from .timing import summarize_timing

    record = service.registry.get_run(source_id)
    if record.get("plugin_id") == "electrical_control_v1":
        from .electrical_isp_source import _resolve_verified_record

        return _resolve_verified_record(service, source_id, selector, record)
    if record.get("status") != "succeeded" or record.get("plugin_id") != "retained_charge_v1":
        raise ValueError("Code-domain selection requires a completed retained_charge_v1 run")
    program = service.registry.get("capture_programs", record["capture_program_id"])
    verify_program(service, program, "retained_charge_v1")
    if (
        program != record["capture_program_snapshot"]
        or program["payload"]["schema"] not in {
            PROGRAM_SCHEMA, TRAPPED_PROGRAM_SCHEMA,
                GAIN_MERGE_PROGRAM_SCHEMA, TRAPPED_GAIN_MERGE_PROGRAM_SCHEMA,
                PHOTO_HDR_PROGRAM_SCHEMA,
        }
    ):
        raise IntegrityError("Retained source program differs from its registered snapshot")
    for kind, key in (
        ("assemblies", "assembly"),
        ("tests", "test"),
        ("control_sequences", "control_sequence"),
        ("scene_sequences", "scene_sequence"),
    ):
        if service.registry.get(kind, record[key + "_id"]) != record[key + "_snapshot"]:
            raise IntegrityError("Retained source differs from registered " + key + " snapshot")
    control_record, scene_record = (
        record["control_sequence_snapshot"],
        record["scene_sequence_snapshot"],
    )
    stimulus = record["test_snapshot"]["stimulus"]
    if (
        record["id"] != source_id
        or program["assembly_id"] != record["assembly_id"]
        or program["assembly_snapshot"] != record["assembly_snapshot"]
        or control_record["assembly_id"] != record["assembly_id"]
        or control_record["scene_sequence_id"] != record["scene_sequence_id"]
        or stimulus["control_sequence_id"] != control_record["id"]
        or stimulus["control_sequence_hash"] != control_record["content_hash"]
        or stimulus["scene_sequence_id"] != scene_record["id"]
        or stimulus["scene_sequence_hash"] != scene_record["content_hash"]
    ):
        raise IntegrityError("Retained source IDs/hashes differ from original registered bindings")
    for model in record["model_snapshots"]:
        if service.registry.get("models", model["id"]) != model:
            raise IntegrityError("Retained model differs from its registered snapshot")
    verify_models(service, record["model_snapshots"])
    for model in record["model_snapshots"]:
        if model.get("psf_factory_job_id"):
            verify_psf_model_link(service, model)
        elif model.get("factory_job_id"):
            verify_model_link(service, model)
    # Verify original acquisition input provenance, not any output reservoir array.
    for artifact in record["input_artifacts"]:
        artifact_bytes(service, artifact)
    summary = record["result"]["summary"]
    summary_artifact = _artifact(record, "summary.json")
    content = artifact_bytes(service, summary_artifact)
    if content != canonical(summary).encode() or json.loads(content) != summary:
        raise IntegrityError("Retained summary bytes differ from the completed result")
    _verify_light_components(service, record, summary, program)
    payload, request = program["payload"], record["request"]
    controls = record["control_sequence_snapshot"]["payload"]
    model = payload["electrical_model"]
    paired = _paired(model)
    readouts = ["HCG", "LCG"] if paired else ["single"]
    if selector["readout_id"] not in readouts:
        raise ValueError("Selected readout does not exist in the original retained acquisition")
    index = readouts.index(selector["readout_id"])
    if (
        request["capture_program"] != payload
        or request["control_sequence"] != controls
        or summary["capture_program"] != payload
        or summary["capture_program_hash"] != program["content_hash"]
        or summary["capture_program_id"] != program["id"]
        or summary["control_sequence"] != controls
        or summary.get("source_kind") != "simulated"
        or summary.get("plugin_id") != "retained_charge_v1"
        or summary.get("schema_version") != (
            "camera_retained_charge_result_v4"
            if payload["schema"] == "camera_retained_charge_capture_program_v5"
            else
            "camera_retained_charge_result_v3"
            if payload["schema"] in {
                "camera_retained_charge_capture_program_v3",
                "camera_retained_charge_capture_program_v4",
            }
            else "camera_retained_charge_result_v2"
            if payload["schema"] == "camera_retained_charge_capture_program_v2"
            else "camera_retained_charge_result_v1"
        )
        or summary.get("readout_ids") != readouts
        or summary.get("raw_formats") != _raw_formats(model)
        or summary.get("current_exposure_charge_estimation") is not (
            payload["schema"] == "camera_retained_charge_capture_program_v5"
        )
        or summary.get("hdr_merge_applied") is not (
            payload["schema"] == "camera_retained_charge_capture_program_v5"
        )
        or summary.get("preview_contract") != PREVIEW
        or "calibrations" in summary
    ):
        raise IntegrityError("Retained code format, program or delivered-history settings differ")
    raw_artifact = _artifact(record, "raw_groups.npy")
    container = _raw_array(service, raw_artifact)
    count, path_count, height, width = container.shape
    if path_count != len(readouts):
        raise IntegrityError("Original RAW path count differs from the retained program")
    geometry, pitch = _source_geometry(service, record, (height, width))
    if (payload["schema"] == "camera_retained_charge_capture_program_v5"
            and (geometry["crop_origin"] != [0, 0]
                 or geometry["full_sensor_shape"] != [height, width])):
        raise IntegrityError("Retained photo HDR inverse requires a full-native sensor")
    timing = summarize_timing(controls, geometry=geometry, frame_shape=(height, width))
    events = event_schedule(controls, payload, geometry, (height, width))
    state_geometry = (
        {"full_sensor_shape": geometry["full_sensor_shape"],
         "crop_origin": geometry["crop_origin"]}
        if geometry["full_sensor_shape"] != [height, width]
        or geometry["crop_origin"] != [0, 0]
        else None
    )
    state_scope = (
        "one_immutable_native_receiver_roi_sequence_job" if state_geometry is not None
        else "one_immutable_full_sequence_job"
    )
    retained_summary = summary["retained_charge"]
    if (
        summary["geometry"] != geometry
        or summary["pixel_pitch_um"] != pitch
        or summary["output_size"] != [height, width]
        or summary["clock"] != controls["clock"]
        or summary["timing"] != timing
        or request["retained_charge_events"] != events
        or retained_summary["events"] != events
        or retained_summary["model"] != payload["retained_charge_model"]
        or retained_summary.get("state_scope") != state_scope
        or ("receiver_state_geometry" in retained_summary) != (state_geometry is not None)
        or (state_geometry is not None
            and retained_summary["receiver_state_geometry"] != state_geometry)
    ):
        raise IntegrityError("Retained native geometry, timing or exact row events differ")
    if payload["schema"] in {TRAPPED_PROGRAM_SCHEMA, TRAPPED_GAIN_MERGE_PROGRAM_SCHEMA}:
        from . import trapped_charge_runtime

        trap_inputs = {
            "base": {"size": [height, width]},
            "program": payload,
            "timeline": scene_record["payload"],
            "control": controls,
            "events": events,
        }
        if record.get("unmodulated_scene_sequence_id") is not None:
            trap_inputs["unmodulated"] = {
                "timeline": record["unmodulated_scene_sequence_snapshot"]["payload"]
            }
        expected_trap = trapped_charge_runtime.summary(
            trapped_charge_runtime.plan(trap_inputs)
        )
        if summary["retained_charge"].get("trap_integration") != expected_trap:
            raise IntegrityError("Retained trap chronology differs from frozen scene knots")
    test = record["test_snapshot"]
    contract = test["stimulus"]["sample_contract"]
    channels = (
        ["R", "Gr", "Gb", "B"]
        if "native_pixel_response" in program["sensor_model_snapshot"]["payload"]
        else ["R", "G", "B"]
    )
    if (
        test["metric_id"] != METRIC_ID
        or test["unit"] != "DN"
        or test["reference_plane"] != "sensor_raw"
        or test["stimulus"]["schema"] not in {
            "retained_charge_test_v1", "retained_charge_components_test_v1"}
        or test["stimulus"]["observation_method"] != OBSERVATION
        or test["stimulus"]["capture_program_id"] != program["id"]
        or test["stimulus"]["capture_program_hash"] != program["content_hash"]
        or test["frame_conditions"] != frame_conditions(controls)
        or test["clock"] != controls["clock"]
        or contract["schema"] != "camera_retained_delivered_samples_v1"
        or contract["capture_program_id"] != program["id"]
        or contract["capture_program_hash"] != program["content_hash"]
        or contract["topology"] != "retained_pd_transfer_to_fresh_read_node"
        or contract["observation"] != OBSERVATION
        or contract["channels"] != channels
        or summary["channels"] != channels
        or contract["events"] != events
        or contract["raw_formats"] != _raw_formats(model)
        or contract["readout_ids"] != readouts
        or contract["raster_shape"] != [height, width]
        or contract["sampling"] != {**geometry, "pixel_pitch_um": pitch}
        or summary["prediction"]["sample_geometry"] != contract
        or summary["prediction"]["metric_id"] != METRIC_ID
        or summary["prediction"]["method_and_version"] != OBSERVATION
    ):
        raise IntegrityError("Fixed retained Test differs from original frame/path/event evidence")
    noise = request["noise_enabled"]
    temperature = request.get("temperature_c")
    temperature = model["reference_temperature_c"] if temperature is None else temperature
    if (
        type(noise) is not bool
        or summary["noise_enabled"] is not noise
        or noise != record["parameters"].get("noise_enabled", True)
        or request["seed"] != record["parameters"].get("seed", 42)
        or summary["rng"]["seed"] != request["seed"]
        or request.get("temperature_c") != test["conditions"]["temperature_c"]["value"]
    ):
        raise IntegrityError("Retained source recorded noise/seed settings differ")
    saved_frames, descriptors = summary["frames"], summary["readout_descriptors"]
    if not (
        count
        == len(controls["frames"])
        == len(saved_frames)
        == len(descriptors)
        == summary["frame_count"]
        and summary["readout_count"] == count * path_count
    ):
        raise IntegrityError("Retained RAW frame/descriptor counts differ")
    if summary["frame_ids"] != [f["frame_id"] for f in controls["frames"]]:
        raise IntegrityError("Retained RAW frame order differs from original controls")
    for path_index, fmt in enumerate(summary["raw_formats"]):
        if np.any(container[:, path_index] < 0) or np.any(
            container[:, path_index] > 2 ** fmt["bit_depth"] - 1
        ):
            raise ValueError("Retained RAW code exceeds its declared path ADC range")
    selected_path = model["paths"][index] if paired else model
    frames, conditions, identities = [], [], []
    for i, (frame, saved, descriptor) in enumerate(
        zip(controls["frames"], saved_frames, descriptors, strict=True)
    ):
        if (
            type(saved.get("frame_index")) is not int
            or saved["frame_index"] != i
            or any(saved.get(k) != v for k, v in frame.items())
            or saved.get("timing") != timing["frames"][i]
            or descriptor
            != _descriptor(model, frame, temperature, noise, (height, width), readouts)
        ):
            raise IntegrityError("Retained frame identity or delivered readout descriptor differs")
        identity = {
            "source_id": source_id,
            "acquisition_id": frame["frame_id"],
            "acquisition_index": i,
            "readout_id": selector["readout_id"],
            "readout_index": index,
            "shared_charge_group_id": frame["frame_id"] if paired else None,
        }
        frames.append({**deepcopy(frame), "frame_index": i, "source_acquisition": identity})
        identities.append(identity)
        conditions.append(
            {
                "frame_id": frame["frame_id"],
                "frame_start_ns": frame["frame_start_ns"],
                "applied_at_ns": frame["applied"]["applied_at_ns"],
                "exposure_ns": frame["applied"]["exposure_ns"],
                "gain": frame["applied"]["gain"],
                "isp_model_id": frame["applied"]["isp_model_id"],
                "temperature_c": temperature,
                "readout_analog_gain": selected_path["analog_gain"]
                if paired
                else frame["applied"]["gain"],
                "readout_digital_gain": selected_path["digital_gain"],
            }
        )
    raw = np.array(container[:, index], copy=True)
    source = {
        "schema": SCHEMA,
        "source_kind": "run",
        "source_id": source_id,
        "name": record["name"],
        "selector": selector,
        "readout_id": selector["readout_id"],
        "readout_index": index,
        "raw_sha256": raw_artifact["sha256"],
        "raw_container_shape": list(container.shape),
        "raw_shape": list(raw.shape),
        "selected_array_sha256": array_sha256(raw),
        "selected_array_hash_encoding": ARRAY_ENCODING,
        "raw_format": deepcopy(summary["raw_formats"][index]),
        "geometry": geometry,
        "pixel_pitch_um": pitch,
        "conditions": deepcopy(test["conditions"]),
        "frame_records": frames,
        "frame_conditions": conditions,
        "clock": deepcopy(controls["clock"]),
        "timing": timing,
        "retained_charge_events": events,
        "capture_program_id": program["id"],
        "capture_program_hash": program["content_hash"],
        "capture_program_sha256": digest(program),
        "capture_program_snapshot": deepcopy(program),
        "source_snapshot": deepcopy(record),
        "source_snapshot_role": "evidence_only_not_processing_inputs",
        "source_provenance": {
            "source_kind": "simulated",
            "source_uri": "run://" + source_id,
            "physical_instance": None,
            "method_and_version": record["plugin_id"],
        },
        "processing_contract": deepcopy(PROCESSING_CONTRACT),
        "raw_artifact": raw_artifact,
        "summary_artifact": summary_artifact,
        "input_artifacts": [raw_artifact, summary_artifact, *deepcopy(program["artifacts"])],
        "acquisition_identity": identities,
    }
    return source, raw


def _resolve(service, source_id, selector):
    selector = validate_selector(selector)
    if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 200:
        raise ValueError("Retained source requires an explicit completed run ID")
    try:
        return _resolve_checked(service, source_id, selector)
    except (KeyError, TypeError, IndexError) as exc:
        raise IntegrityError("Malformed retained source evidence") from exc


def resolve(service, source_id, selector):
    """Return detached JSON provenance and the original path's code interpretation."""
    return _resolve(service, source_id, selector)[0]


def load_inputs(service, source):
    """Revalidate frozen evidence and return only selected all-frame integer RAW."""
    if (
        not isinstance(source, dict)
        or source.get("schema") != SCHEMA
        or source.get("source_kind") != "run"
    ):
        raise ValueError("Expected an explicit retained code-domain ISP source")
    fresh, raw = _resolve(service, source.get("source_id"), source.get("selector"))
    if fresh != source:
        raise IntegrityError("Selected retained RAW source differs from its frozen evidence")
    return raw
