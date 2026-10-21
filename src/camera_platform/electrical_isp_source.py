"""Bind electrical AE ISP inputs to original RAW and replayed causal controls.

This is an acquisition verifier, not a camera simulator or a charge estimator.
Only original integer RAW is replayed through the pure controller. Optical and
reservoir output arrays are not read. The parent source API owns registry lookup;
this module consumes that operation-local, registry-verified completed record.
"""

import io
import json
import math
from copy import deepcopy

import numpy as np

from . import electrical_control, sequences
from .capture_programs import verify_program
from .electrical_control_runtime import (
    METRIC_ID,
    OBSERVATION,
    PROGRAM_SCHEMAS,
    TRAPPED_PROGRAM_SCHEMA,
    _target_outcome,
    observation_path,
    retained_program,
    validate_capture_program,
)
from .factory import verify_model_link, verify_psf_model_link
from .imaging import resolve_isp
from .isp_charge_source import _artifact, _source_geometry, array_sha256, artifact_bytes
from .native_model_evidence import verify_models
from .registry import IntegrityError, canonical, digest
from .retained_charge_runtime import PREVIEW, _raw_formats, event_schedule
from .retained_isp_source import (
    ARRAY_ENCODING,
    PROCESSING_CONTRACT,
    SCHEMA,
    _descriptor,
    _raw_array,
)
from .runtime import sensor_response_fields
from .timing import summarize_timing, validate_control_sequence, validate_timing_coverage


def _same(actual, expected, label):
    if canonical(actual) != canonical(expected):
        raise IntegrityError("Electrical ISP source " + label + " differs from original evidence")


def _sources(service, record):
    """Check registered source closure without requiring the current capture environment."""
    for kind, key in (
        ("assemblies", "assembly"),
        ("tests", "test"),
        ("control_sequences", "control_sequence"),
        ("scene_sequences", "scene_sequence"),
        ("capture_programs", "capture_program"),
    ):
        _same(service.registry.get(kind, record[key + "_id"]), record[key + "_snapshot"], key)
    assembly, test, control, scene, program = (
        record[key + "_snapshot"]
        for key in ("assembly", "test", "control_sequence", "scene_sequence", "capture_program")
    )
    verify_program(service, program, "electrical_control_v1")
    if program["payload"]["schema"] not in PROGRAM_SCHEMAS:
        raise IntegrityError("Electrical ISP requires the original electrical control program")
    _same(validate_capture_program(program["payload"]), program["payload"], "program contract")
    _same(program["assembly_snapshot"], assembly, "program assembly")
    if program["assembly_id"] != assembly["id"]:
        raise IntegrityError("Electrical ISP program is attached to another assembly")
    expected_test = sequences.test_template(
        service, assembly["id"], scene["id"], control["id"], program["id"]
    )
    for key in (
        "metric_id",
        "unit",
        "reference_plane",
        "stimulus",
        "clock",
        "frame_conditions",
        "conditions",
    ):
        _same(test.get(key), expected_test[key], "planned Test " + key)
    if set(test["included_effects"]) != set(expected_test["included_effects"]) or test.get(
        "observation_method"
    ):
        raise IntegrityError("Electrical ISP planned Test observation effects differ")
    images = sequences._images(service, scene["payload"])
    _same(images, scene["image_snapshots"], "scene image snapshots")
    models = [service.registry.get("models", ident) for ident in assembly["model_ids"]]
    expected_models = {model["id"]: model for model in models}
    for frame in control["payload"]["frames"]:
        ident = frame["applied"]["isp_model_id"]
        expected_models[ident] = service.registry.get("models", ident)
    _same(record["model_snapshots"], list(expected_models.values()), "models")
    response_artifacts = verify_models(service, models)
    for model in record["model_snapshots"]:
        if model.get("psf_factory_job_id"):
            verify_psf_model_link(service, model)
        elif model.get("factory_job_id"):
            verify_model_link(service, model)
    artifacts = {
        (artifact["sha256"], artifact["name"]): artifact
        for source in (scene, control, *images, program)
        for artifact in source["artifacts"]
    }
    artifacts.update({(a["sha256"], a["name"]): a for a in response_artifacts})
    _same(record["input_artifacts"], list(artifacts.values()), "input artifact closure")
    for artifact in artifacts.values():
        artifact_bytes(service, artifact)
    roles = {model["kind"]: model for model in models}
    sensor, lens = roles["sensor"]["payload"], deepcopy(roles["lens"]["payload"])
    psf = lens.pop("psf", None)
    if psf is None:
        raise IntegrityError("Electrical source requires the original explicit native PSF")
    parameters = record["parameters"]
    if not isinstance(parameters, dict) or set(parameters) - {"seed", "noise_enabled"}:
        raise IntegrityError(
            "Electrical source parameters are not the original seed/noise contract"
        )
    # The capture contract admits integral JSON floats and preserves their
    # original request representation; only the runtime's RNG receipt is int.
    seed = parameters.get("seed", 42)
    if (
        type(seed) not in (int, float)
        or not 0 <= seed <= 2**32 - 17
        or not math.isfinite(seed)
        or seed != int(seed)
    ):
        raise IntegrityError(
            "Electrical source seed must be finite integral numeric data, never bool"
        )
    noise = parameters.get("noise_enabled", True)
    if type(noise) is not bool:
        raise IntegrityError("Electrical source requires explicit boolean noise mode")
    profiles = {
        frame["applied"]["isp_model_id"]: resolve_isp(
            expected_models[frame["applied"]["isp_model_id"]]
        )
        for frame in control["payload"]["frames"]
    }
    expected_request = {
        "wavelength_nm": sensor["wavelength_nm"],
        **sensor_response_fields(sensor),
        "pixel_pitch_um": sensor["pixel_pitch_um"],
        "readout": sensor["readout"],
        "lens": lens,
        "psf": psf,
        "temperature_c": test["conditions"]["temperature_c"]["value"],
        "seed": seed,
        "noise_enabled": noise,
        "isp_profiles": profiles,
        "control_sequence": control["payload"],
        "scene_sequence": scene["payload"],
        "capture_program": program["payload"],
    }
    _same(record["request"], expected_request, "planned request")
    return program, test, control["payload"], sensor


def _replay_controller(raw, raw_format, scaffold, program, geometry):
    """Rebuild every applied setting and command from completed original RAW only."""
    observation_index, observation_id, observation_format = observation_path(program)
    _same(raw_format, observation_format, "fixed feedback readout format")
    shape = list(raw.shape[-2:])
    scaffold = validate_control_sequence(scaffold, geometry=geometry, frame_shape=shape)
    policy = electrical_control.validate_policy(
        program["electrical_control_policy"], frame_shape=shape, raw_format=raw_format
    )
    initial = {
        key: deepcopy(value)
        for key, value in scaffold["frames"][0]["applied"].items()
        if key not in {"applied_at_ns", "command_id"}
    }
    for frame in scaffold["frames"]:
        settings = {
            k: v for k, v in frame["applied"].items() if k not in {"applied_at_ns", "command_id"}
        }
        if (
            set(frame) != {"frame_id", "frame_start_ns", "applied"}
            or canonical(settings) != canonical(initial)
            or frame["applied"]["command_id"] is not None
            or frame["applied"]["applied_at_ns"]
            != electrical_control.latch(frame, scaffold["mode"])
        ):
            raise IntegrityError("Electrical ISP scaffold is not uniform initial settings")
    count = len(scaffold["frames"])
    if len(raw) != count:
        raise IntegrityError("Electrical original RAW and scaffold frame counts differ")
    actual = {**deepcopy(scaffold), "frames": []}
    exposure, prior, current_command = initial["exposure_ns"], -1, None
    queue, trace = [], []
    for index, frame in enumerate(scaffold["frames"]):
        pending = electrical_control.apply_pending(
            queue,
            frame_index=index,
            frame=frame,
            mode=scaffold["mode"],
            exposure_ns=exposure,
            last_applied_source_index=prior,
        )
        queue, exposure = pending["commands"], pending["exposure_ns"]
        prior = pending["last_applied_source_index"]
        current_command = pending["applied_command_id"] or current_command
        acquired = {
            "frame_id": frame["frame_id"],
            "frame_start_ns": frame["frame_start_ns"],
            "applied": {
                **deepcopy(initial),
                "exposure_ns": exposure,
                "applied_at_ns": pending["latch_at_ns"],
                "command_id": current_command,
            },
        }
        events = electrical_control.events(
            frame, exposure, scaffold["mode"], shape[0], policy["latency"]
        )
        statistics = electrical_control.raw_statistics(raw[index], raw_format, policy)
        proposal = electrical_control.propose_ae(statistics, exposure, policy)
        command = electrical_control.make_command(proposal, frame, index, events, policy)
        issued = []
        if command is not None:
            queue.append(command)
            issued.append(command["command_id"])
            acquired["commanded"] = {
                **deepcopy(initial),
                **proposal["commanded"],
                "issued_at_ns": command["issued_at_ns"],
                "command_id": command["command_id"],
            }
        actual["frames"].append(acquired)
        trace.append(
            {
                "frame_id": frame["frame_id"],
                "index": index,
                "latch_at_ns": pending["latch_at_ns"],
                "applied": deepcopy(acquired["applied"]),
                "applied_command_id": current_command,
                "events": events,
                "statistics": statistics,
                "proposal": proposal,
                "issued_command_ids": issued,
            }
        )
    queue = electrical_control.finalize_pending(
        queue,
        last_frame_index=count - 1,
        last_latch_ns=electrical_control.latch(actual["frames"][-1], actual["mode"]),
    )
    actual = validate_control_sequence(actual, geometry=geometry, frame_shape=shape)
    receipt = {
        "schema": "camera_electrical_control_trace_v1",
        "policy": policy,
        "policy_sha256": digest(policy),
        "initial_settings": deepcopy(initial),
        "observation_source": "integer_raw_dn",
        "readout_id": observation_id,
        "raw_format": raw_format,
        "observation_quantity": "signed_black_relative_delivered_total_code_dn",
        "frames": trace,
        "commands": queue,
        "target_outcome": _target_outcome(trace, policy),
        "latency_evidence": "declared_simulation_not_hardware_observation",
        "source_truth_used": False,
        "clock": deepcopy(scaffold["clock"]),
        "mode": deepcopy(scaffold["mode"]),
        "fixed_controls": ["gain", "isp_model_id", "white_balance", "pd_clear_policy"],
    }
    if observation_id != "single" or program["schema"] == TRAPPED_PROGRAM_SCHEMA:
        receipt["observation_readout_index"] = observation_index
        receipt["fixed_controls"].append("observation_readout_id")
    return actual, receipt


def _timing_artifact(service, record, name, expected, *, unit="ns"):
    """Admit exact small int64 observed-timing arrays, never arbitrary output arrays."""
    artifact = _artifact(record, name + ".npy")
    content = artifact_bytes(service, artifact)
    expected = np.asarray(expected, dtype=np.int64)
    stream = io.BytesIO(content)
    try:
        version = np.lib.format.read_magic(stream)
        reader = {
            (1, 0): np.lib.format.read_array_header_1_0,
            (2, 0): np.lib.format.read_array_header_2_0,
        }[version]
        shape, fortran, dtype = reader(stream)
    except (KeyError, ValueError, EOFError, TypeError) as exc:
        raise IntegrityError("Malformed electrical observed-timing NPY") from exc
    if (
        tuple(shape) != expected.shape
        or fortran
        or dtype != np.dtype("int64")
        or len(content) != stream.tell() + expected.nbytes
        or artifact.get("shape") != list(expected.shape)
        or artifact.get("dtype") != "int64"
        or artifact.get("unit") != unit
        or artifact.get("spatial_sampling") != "native_sensor"
    ):
        raise IntegrityError("Electrical observed-timing descriptor differs from its actual shape")
    actual = np.load(io.BytesIO(content), allow_pickle=False)
    if not np.array_equal(actual, expected):
        raise IntegrityError("Electrical observed-timing array differs from RAW-replayed controls")
    return artifact


def _resolve_verified_record(service, source_id, selector, record):
    if (
        record.get("status") != "succeeded"
        or record.get("plugin_id") != "electrical_control_v1"
        or record.get("job_kind") != "camera_simulation"
        or record.get("id") != source_id
    ):
        raise ValueError("Code-domain selection requires a completed electrical_control_v1 run")
    program, test, scaffold, sensor = _sources(service, record)
    payload, request = program["payload"], record["request"]
    trapped = payload["schema"] == TRAPPED_PROGRAM_SCHEMA
    model, summary = payload["electrical_model"], record["result"]["summary"]
    observation_index, observation_id, observation_format = observation_path(payload)
    paired = observation_id != "single"
    readouts = ["HCG", "LCG"] if paired else ["single"]
    if selector["readout_id"] not in readouts:
        if not paired:
            raise ValueError("Electrical control ISP requires the original single readout")
        raise ValueError("Electrical control ISP requires an original acquired readout path")
    selected_index = readouts.index(selector["readout_id"])
    summary_artifact = _artifact(record, "summary.json")
    content = artifact_bytes(service, summary_artifact)
    if content != canonical(summary).encode() or json.loads(content) != summary:
        raise IntegrityError("Electrical original summary bytes differ from the completed result")
    raw_artifact = _artifact(record, "raw_groups.npy")
    container = _raw_array(service, raw_artifact)
    if container.shape[1] != len(readouts):
        raise IntegrityError("Electrical control acquisition RAW path count differs")
    count, _, height, width = container.shape
    formats = _raw_formats(model)
    if len(formats) != len(readouts) or any(
        np.any(container[:, path] < 0) or np.any(container[:, path] > 2 ** fmt["bit_depth"] - 1)
        for path, fmt in enumerate(formats)
    ):
        raise IntegrityError("Electrical RAW code/path range differs from the original readout")
    geometry, pitch = _source_geometry(service, record, (height, width))
    if geometry["crop_origin"] != [0, 0] or geometry["full_sensor_shape"] != [height, width]:
        raise IntegrityError("Electrical ISP requires the complete original native sensor")
    # Metering is fixed by the authored capture program, independently of the
    # path selected for code-domain display. Both still bind the same RAW blob.
    actual, trace = _replay_controller(
        container[:, observation_index], observation_format, scaffold, payload, geometry
    )
    timing = summarize_timing(actual, geometry=geometry, frame_shape=(height, width))
    events = event_schedule(actual, retained_program(payload), geometry, (height, width))
    coverage = validate_timing_coverage(
        record["scene_sequence_snapshot"]["payload"],
        actual,
        geometry=geometry,
        frame_shape=(height, width),
    )
    ids = [frame["frame_id"] for frame in actual["frames"]]
    plan = test["stimulus"]["sample_contract"]
    realized = {
        "schema": "camera_electrical_control_realized_samples_v1",
        "plan": plan,
        "control_sequence": actual,
        "events": events,
        "observation": OBSERVATION,
    }
    fields = {
        "schema_version": (
            "camera_electrical_control_result_v2" if trapped
            else "camera_electrical_control_result_v1"
        ),
        "plugin_id": "electrical_control_v1",
        "source_kind": "simulated",
        "acquisition_mode": "closed_loop",
        "capture_program": payload,
        "capture_program_hash": program["content_hash"],
        "capture_program_id": program["id"],
        "control_sequence": actual,
        "control_sequence_id": record["control_sequence_id"],
        "scene_sequence_id": record["scene_sequence_id"],
        "geometry": geometry,
        "pixel_pitch_um": pitch,
        "output_size": [height, width],
        "clock": scaffold["clock"],
        "timing": timing,
        "coverage": coverage,
        "frame_conditions": sequences.frame_conditions(actual),
        "conditions": test["conditions"],
        "frame_ids": ids,
        "frame_count": count,
        "readout_count": count * len(readouts),
        "readout_ids": readouts,
        "raw_formats": formats,
        "preview_contract": PREVIEW,
        "current_exposure_charge_estimation": False,
        "hdr_merge_applied": False,
        "planning_contract": plan,
        "electrical_control": trace,
        "noise_enabled": request["noise_enabled"],
    }
    for key, value in fields.items():
        _same(summary.get(key), value, key)
    if "calibrations" in summary:
        raise IntegrityError("Electrical delivered-code source cannot supply inverse calibration")
    _same(summary["retained_charge"]["events"], events, "actual retained row events")
    _same(summary["retained_charge"]["model"], payload["retained_charge_model"], "retained model")
    _same(
        summary["rng"],
        {
            "seed": int(request["seed"]),
            "method": "numpy.SeedSequence.spawn:2",
            "charge_spawn_key": [0],
            "readout_spawn_key": [1],
            "external_state": False,
        },
        "whole-sequence RNG receipt",
    )
    prediction = summary["prediction"]
    for key, value in {
        "acquisition_mode": "closed_loop",
        "sample_geometry": realized,
        "metric_id": METRIC_ID,
        "method_and_version": OBSERVATION,
        "unit": "DN",
        "reference_plane": "sensor_raw",
        "frame_conditions": sequences.frame_conditions(actual),
        "clock": scaffold["clock"],
        "assembly_id": record["assembly_id"],
        "test_id": record["test_id"],
        "id": source_id,
        "dataset_ids": [source_id],
        "conditions": test["conditions"],
    }.items():
        _same(prediction.get(key), value, "realized prediction " + key)
    channels = ["R", "Gr", "Gb", "B"] if "native_pixel_response" in sensor else ["R", "G", "B"]
    _same(summary["channels"], channels, "native channels")
    temperature = request["temperature_c"]
    if temperature is None:
        temperature = model["reference_temperature_c"]
    saved = summary["frames"]
    if len(saved) != count or len(summary["readout_descriptors"]) != count:
        raise IntegrityError("Electrical original frame/descriptor counts differ")
    frame_records, conditions, identities = [], [], []
    selected_path = model["paths"][selected_index] if paired else model
    for index, frame in enumerate(actual["frames"]):
        for key, value in {
            **frame,
            "frame_index": index,
            "timing": timing["frames"][index],
        }.items():
            _same(saved[index].get(key), value, "actual frame " + key)
        _same(
            summary["readout_descriptors"][index],
            _descriptor(
                model, frame, temperature, request["noise_enabled"], (height, width), readouts
            ),
            "delivered readout descriptor",
        )
        identity = {
            "source_id": source_id,
            "acquisition_id": frame["frame_id"],
            "acquisition_index": index,
            "readout_id": selector["readout_id"],
            "readout_index": selected_index,
            "shared_charge_group_id": frame["frame_id"] if paired else None,
        }
        identities.append(identity)
        frame_records.append(
            {**deepcopy(frame), "frame_index": index, "source_acquisition": identity}
        )
        conditions.append(
            {
                **sequences.frame_conditions({"frames": [frame]})[0],
                "temperature_c": temperature,
                "readout_analog_gain": selected_path["analog_gain"]
                if paired
                else frame["applied"]["gain"],
                "readout_digital_gain": selected_path["digital_gain"],
                **(
                    {"wb_gains_rgb": deepcopy(frame["applied"]["wb_gains_rgb"])}
                    if "wb_gains_rgb" in frame["applied"]
                    else {}
                ),
            }
        )
    observed_artifacts = [
        _timing_artifact(service, record, name, values)
        for name, values in {
            "applied_exposure_ns": [f["applied"]["exposure_ns"] for f in actual["frames"]],
            "row_start_ns": [f["row_open_ns"] for f in events["frames"]],
            "row_end_ns": [f["row_transfer_ns"] for f in events["frames"]],
            "row_gap_ns": [f["row_gap_ns"] for f in events["frames"]],
        }.items()
    ]
    if trapped:
        # Rebuild only the bounded integer-clock schedule. No photon generation,
        # trap occupancy, delivered-charge ledger or inverse model is consumed.
        from . import trapped_charge_runtime, trapped_control_runtime

        schedule_input = {
            "base": {"size": (height, width)},
            "program": retained_program(payload),
            "control": actual,
            "timeline": record["scene_sequence_snapshot"]["payload"],
            "events": events,
        }
        schedule = trapped_charge_runtime.plan(schedule_input)
        _same(
            summary.get("trap_control_work_envelope"),
            trapped_control_runtime.admission({
                **schedule_input, "control": scaffold,
                "policy": payload["electrical_control_policy"],
                "initial_settings": trace["initial_settings"],
            }),
            "complete-grid trap work envelope",
        )
        _same(
            summary["retained_charge"].get("trap_integration"),
            trapped_charge_runtime.summary(schedule),
            "actual trap integration schedule",
        )
        observed_artifacts.extend(
            _timing_artifact(
                service, record, name, values,
                unit="ns" if name.endswith("_ns") else "index",
            )
            for name, values in trapped_charge_runtime.evidence(schedule, height).items()
        )
    raw = np.array(container[:, selected_index], copy=True)
    return {
        "schema": SCHEMA,
        "source_kind": "run",
        "source_id": source_id,
        "name": record["name"],
        "selector": deepcopy(selector),
        "readout_id": selector["readout_id"],
        "readout_index": selected_index,
        "raw_sha256": raw_artifact["sha256"],
        "raw_container_shape": list(container.shape),
        "raw_shape": list(raw.shape),
        "selected_array_sha256": array_sha256(raw),
        "selected_array_hash_encoding": ARRAY_ENCODING,
        "raw_format": deepcopy(formats[selected_index]),
        "geometry": geometry,
        "pixel_pitch_um": pitch,
        "conditions": deepcopy(test["conditions"]),
        "frame_records": frame_records,
        "frame_conditions": conditions,
        "clock": deepcopy(scaffold["clock"]),
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
            "method_and_version": "electrical_control_v1",
        },
        "source_plugin_id": "electrical_control_v1",
        "acquisition_mode": "closed_loop",
        "planning_contract": deepcopy(plan),
        "realized_acquisition": deepcopy(realized),
        "control_trace": trace,
        "control_trace_role": "evidence_only_not_processing_inputs",
        "processing_contract": deepcopy(PROCESSING_CONTRACT),
        "raw_artifact": raw_artifact,
        "summary_artifact": summary_artifact,
        "input_artifacts": [
            raw_artifact,
            summary_artifact,
            *deepcopy(program["artifacts"]),
            *observed_artifacts,
        ],
        "acquisition_identity": identities,
    }, raw
