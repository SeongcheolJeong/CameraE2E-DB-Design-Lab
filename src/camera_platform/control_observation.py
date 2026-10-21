"""One RAW/control-log observation operator for simulated and acquired sequences.

The output retains missing samples. It never substitutes expected charge for RAW,
matches controls by their initial values, interpolates frames, or estimates event
times. Settling is a sampled, finite-window statement, not continuous stability.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

import numpy as np
from pydantic import Field, model_validator

from .contracts import RESPONSE_OBSERVABLE_UNITS, Contract, Provenance
from .control_contracts import CameraControlPolicy, InitialControlState
from .controls import raw_statistics
from .registry import digest
from .timing import MAX_TIME_NS, _clock, _geometry, _mode

METHOD = "control_response_observation_v1"
OBSERVABLES = RESPONSE_OBSERVABLE_UNITS
Observable = Literal[
    "green_mean_normalized",
    "wb_adjusted_rg_ratio",
    "wb_adjusted_bg_ratio",
    "exposure_ns",
    "gain",
    "wb_red_gain",
    "wb_blue_gain",
    "ae_command_latency_ns",
    "awb_command_latency_ns",
]
Reduction = Literal["trajectory", "peak_absolute_target_error", "sampled_settling_time"]


class ResponseRawFormat(Contract):
    cfa: Literal["RGGB"]
    bit_depth: int = Field(ge=2, le=16)
    black_level_dn: float = Field(ge=0)
    packing: Literal["unpacked"]
    layout: Literal["bayer"]
    response_type: Literal["linear"]
    hdr_streams: Literal[1]

    @model_validator(mode="after")
    def levels(self):
        if self.black_level_dn >= 2**self.bit_depth - 1:
            raise ValueError("Black level must be below ADC white")
        return self


class ResponseSpecification(Contract):
    schema_version: Literal["control_response_observation_v1"] = METHOD
    observable: Observable
    reduction: Reduction = "trajectory"
    raw_format: ResponseRawFormat
    geometry: dict
    frame_shape: list[int] = Field(min_length=2, max_length=2)
    mode: dict
    statistics_policy: CameraControlPolicy
    maximum_frame_alignment_error_ns: int = Field(ge=0, le=MAX_TIME_NS)
    target: float | None = None
    settling_band: float | None = Field(default=None, ge=0)
    minimum_dwell_frames: int = Field(default=2, ge=2, le=64)
    window_start_frame_id: str | None = None
    window_end_frame_id: str | None = None

    @model_validator(mode="after")
    def semantics(self):
        _geometry(self.geometry, self.frame_shape)
        _mode(self.mode)
        roi = self.statistics_policy.statistics_roi
        if any(roi.origin[i] + roi.shape[i] > self.frame_shape[i] for i in (0, 1)):
            raise ValueError("Response statistics ROI exceeds the native crop")
        if self.reduction == "trajectory":
            if any(
                x is not None
                for x in (
                    self.target,
                    self.settling_band,
                    self.window_start_frame_id,
                    self.window_end_frame_id,
                )
            ):
                raise ValueError("Trajectory has no target/window reduction parameters")
        else:
            if (
                self.target is None
                or not self.window_start_frame_id
                or not self.window_end_frame_id
            ):
                raise ValueError("Response reduction requires an explicit target and frame window")
            if self.reduction == "sampled_settling_time" and self.settling_band is None:
                raise ValueError("Sampled settling requires a nonnegative band in observable units")
            if self.reduction != "sampled_settling_time" and self.settling_band is not None:
                raise ValueError("A settling band applies only to sampled settling time")
        return self


class ObservedControlState(InitialControlState):
    @model_validator(mode="after")
    def wb_domain(self):
        if any(not 1 / 16 <= value <= 16 for value in self.wb_gains_rgb):
            raise ValueError("Observed WB gains must be in the supported [1/16,16] domain")
        return self


class ResponseFrame(Contract):
    frame_id: str = Field(min_length=1, max_length=200)
    raw_index: int | None = Field(ge=0, le=63)
    frame_start_ns: int | None = Field(ge=-MAX_TIME_NS, le=MAX_TIME_NS)
    latch_at_ns: int | None = Field(ge=-MAX_TIME_NS, le=MAX_TIME_NS)
    applied: ObservedControlState | None


class ResponseCommand(Contract):
    command_id: str = Field(min_length=1, max_length=200)
    kind: Literal["ae", "awb"]
    source_frame_id: str = Field(min_length=1, max_length=200)
    status: Literal["applied", "superseded", "stale", "pending_after_sequence", "unknown"]
    issued_at_ns: int | None = Field(ge=-MAX_TIME_NS, le=MAX_TIME_NS)
    applied_at_ns: int | None = Field(ge=-MAX_TIME_NS, le=MAX_TIME_NS)
    applied_frame_id: str | None
    requested: dict | None = None
    commanded: dict | None = None

    @model_validator(mode="after")
    def events(self):
        keys = {"exposure_ns", "gain"} if self.kind == "ae" else {"wb_gains_rgb"}
        for label, values in (("requested", self.requested), ("commanded", self.commanded)):
            if values is None:
                continue
            allowed = keys | (
                {"exposure_gain_product_ns"}
                if label == "requested" and self.kind == "ae"
                else set()
            )
            if (
                not values
                or set(values) - allowed
                or (label == "commanded" and set(values) != keys)
            ):
                raise ValueError("Control request/command fields differ from their AE/AWB meaning")
            numbers = values.get("wb_gains_rgb") if self.kind == "awb" else list(values.values())
            if (
                not isinstance(numbers, list)
                or (self.kind == "awb" and len(numbers) != 3)
                or any(type(v) not in (int, float) or v <= 0 for v in numbers)
            ):
                raise ValueError("Control requests and commands require explicit positive values")
            if (
                label == "commanded"
                and self.kind == "ae"
                and type(values["exposure_ns"]) is not int
            ):
                raise ValueError("Commanded exposure requires exact integer nanoseconds")
        if self.status == "applied":
            if not self.applied_frame_id:
                raise ValueError("Applied commands require an identified destination frame")
            if (
                self.issued_at_ns is not None
                and self.applied_at_ns is not None
                and self.applied_at_ns < self.issued_at_ns
            ):
                raise ValueError("Command application precedes issue")
        elif self.applied_at_ns is not None or self.applied_frame_id is not None:
            raise ValueError("Unapplied/unknown commands cannot declare application events")
        return self


class ResponseAcquisition(Contract):
    clock: dict
    # test-relative timestamp = logged timestamp + this one fixed offset.
    test_clock_offset_ns: int = Field(ge=-MAX_TIME_NS, le=MAX_TIME_NS)
    alignment_absolute_bound_ns: int | None = Field(ge=0, le=MAX_TIME_NS)
    alignment_source_uri: str = Field(min_length=1, max_length=2000)
    alignment_method_and_version: str = Field(min_length=1, max_length=500)
    program_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scene_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_source_uri: str = Field(min_length=1, max_length=2000)
    execution_method_and_version: str = Field(min_length=1, max_length=500)
    frames: list[ResponseFrame] = Field(min_length=1, max_length=64)
    commands: list[ResponseCommand] = Field(max_length=128)

    @model_validator(mode="after")
    def identities(self):
        _clock(self.clock, "acquisition.clock")
        ids = [f.frame_id for f in self.frames]
        if len(set(ids)) != len(ids):
            raise ValueError("Acquisition frame IDs must be unique")
        indices = [f.raw_index for f in self.frames if f.raw_index is not None]
        if indices != list(range(len(indices))):
            raise ValueError("RAW indices must be complete and ordered without reuse")
        times = [f.frame_start_ns for f in self.frames if f.frame_start_ns is not None]
        if any(a >= b for a, b in zip(times, times[1:], strict=False)):
            raise ValueError("Observed frame times must be strictly increasing")
        events = [
            t for f in self.frames for t in (f.frame_start_ns, f.latch_at_ns) if t is not None
        ]
        events += [
            t for c in self.commands for t in (c.issued_at_ns, c.applied_at_ns) if t is not None
        ]
        if events and max(events) - min(events) > MAX_TIME_NS:
            raise ValueError("Observed event span exceeds the supported exact relative-clock range")
        command_ids = [c.command_id for c in self.commands]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("Command IDs must be unique")
        applied, issued = set(), set()
        for command in self.commands:
            if command.source_frame_id not in ids:
                raise ValueError("Command refers to a foreign source frame")
            source_key = (command.kind, command.source_frame_id)
            if source_key in issued:
                raise ValueError("Only one command per control may be issued by each source frame")
            issued.add(source_key)
            if command.status == "applied":
                if command.applied_frame_id not in ids:
                    raise ValueError("Command refers to a foreign destination frame")
                if ids.index(command.source_frame_id) >= ids.index(command.applied_frame_id):
                    raise ValueError("Feedback must apply after its source frame")
                key = (command.kind, command.applied_frame_id)
                if key in applied:
                    raise ValueError("Only one command per control can apply at each frame latch")
                applied.add(key)
                frame = self.frames[ids.index(command.applied_frame_id)]
                if (
                    command.applied_at_ns is not None
                    and frame.latch_at_ns is not None
                    and command.applied_at_ns != frame.latch_at_ns
                ):
                    raise ValueError("Applied command time differs from the frame latch log")
        return self


class ResponseCaptureImport(Provenance):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    test_id: str
    assembly_id: str
    dataset_ids: list[str] = Field(min_length=1, max_length=100)
    instrument: dict
    conditions: dict
    acquisition: ResponseAcquisition
    # Bound is on the selected, reduced metric, not on unpropagated RAW samples.
    uncertainty: dict

    @model_validator(mode="after")
    def provenance(self):
        if len(set(self.dataset_ids)) != len(self.dataset_ids) or any(
            not s for s in self.dataset_ids
        ):
            raise ValueError("Capture dataset IDs must be nonempty and unique")
        if self.source_kind == "measured" and (
            not self.physical_instance
            or not self.instrument.get("id")
            or not self.instrument.get("calibration_revision")
            or not self.instrument.get("policy_implementation_evidence")
        ):
            raise ValueError(
                "Measured response needs serial, instrument calibration and policy evidence"
            )
        if self.uncertainty.get("kind") not in {"unknown", "absolute_bound"}:
            raise ValueError("Declare unknown or an absolute bound on the observed metric")
        if set(self.uncertainty) - {
            "kind",
            "unit",
            "value",
            "values",
            "source_uri",
            "method_and_version",
            "reason",
        }:
            raise ValueError("Unsupported response uncertainty fields")
        if self.uncertainty.get("kind") == "unknown" and (
            "value" in self.uncertainty
            or "values" in self.uncertainty
            or not self.uncertainty.get("reason")
        ):
            raise ValueError("Unknown metric uncertainty needs a reason, not numeric bounds")
        if self.uncertainty.get("kind") == "absolute_bound" and (
            ("value" in self.uncertainty) == ("values" in self.uncertainty)
        ):
            raise ValueError("Declare exactly one scalar or coordinate-wise metric bound")
        if self.uncertainty.get("kind") == "absolute_bound" and (
            not self.uncertainty.get("source_uri") or not self.uncertainty.get("method_and_version")
        ):
            raise ValueError("An observed metric bound requires its derivation/provenance")
        return self


class ResponseTestRequest(Contract):
    control_program_id: str
    observable: Observable = "green_mean_normalized"
    reduction: Reduction = "trajectory"
    target: float | None = None
    settling_band: float | None = None
    minimum_dwell_frames: int = Field(default=2, ge=2, le=64)
    window_start_frame_id: str | None = None
    window_end_frame_id: str | None = None
    maximum_frame_alignment_error_ns: int = Field(default=0, ge=0, le=MAX_TIME_NS)


def metric(spec):
    spec = ResponseSpecification.model_validate(spec)
    unit = "ns" if spec.reduction == "sampled_settling_time" else OBSERVABLES[spec.observable]
    return f"control.response.{spec.observable}.{spec.reduction}", unit


def method(spec):
    spec = ResponseSpecification.model_validate(spec).model_dump()
    return {"id": METHOD, "specification_sha256": digest(spec)}


def validate_test(test):
    spec = ResponseSpecification.model_validate(test["observation_specification"])
    if (
        test["observation_method"] != method(spec.model_dump())
        or test["observation_shape"] != spec.frame_shape
        or (test["metric_id"], test["unit"]) != metric(spec.model_dump())
        or test["reference_plane"] != "sensor_raw"
    ):
        raise ValueError("Response Test metric, shape and method must match its frozen operator")
    stimulus = test["stimulus"]
    if (
        stimulus.get("schema") != "closed_loop_scene_test_v1"
        or test.get("frame_conditions") is not None
    ):
        raise ValueError("Response Test requires a closed-loop program with generated controls")
    _clock(test["clock"], "test.clock")
    frames = stimulus["planned_frames"]
    ids = [f["frame_id"] for f in frames]
    if len(ids) != len(set(ids)) or not 1 <= len(ids) <= 64:
        raise ValueError("Response Test requires unique planned frame IDs")
    if spec.reduction != "trajectory":
        if spec.window_start_frame_id not in ids or spec.window_end_frame_id not in ids:
            raise ValueError("Response window must name frames in the pinned program")
        start, end = ids.index(spec.window_start_frame_id), ids.index(spec.window_end_frame_id)
        if start > end or (
            spec.reduction == "sampled_settling_time"
            and end - start + 1 < spec.minimum_dwell_frames
        ):
            raise ValueError("Response window is reversed or shorter than the required dwell")
    return spec


def _sample(observable, statistics, applied, command):
    if observable == "green_mean_normalized":
        return (statistics["ae"]["value"], "valid") if statistics else (None, "missing_raw")
    if observable in {"wb_adjusted_rg_ratio", "wb_adjusted_bg_ratio"}:
        if not statistics or statistics["awb"]["status"] != "valid":
            return None, "missing_raw" if not statistics else "insufficient_common_cells"
        if applied is None:
            return None, "unknown_applied_controls"
        means = statistics["awb"]["channel_mean_normalized"]
        c = 0 if observable == "wb_adjusted_rg_ratio" else 2
        return means[c] * applied["wb_gains_rgb"][c] / (
            means[1] * applied["wb_gains_rgb"][1]
        ), "valid"
    if observable.endswith("command_latency_ns"):
        if command is None:
            return None, "no_issued_command"
        if command["status"] != "applied":
            return None, "command_" + command["status"]
        if command["issued_at_ns"] is None or command["applied_at_ns"] is None:
            return None, "unknown_command_event"
        return command["applied_at_ns"] - command["issued_at_ns"], "valid"
    if applied is None:
        return None, "unknown_applied_controls"
    if observable in {"exposure_ns", "gain"}:
        return applied[observable], "valid"
    return applied["wb_gains_rgb"][0 if observable == "wb_red_gain" else 2], "valid"


def analyze(raw, test, acquisition):
    """Analyze actual ADC arrays and applied logs with the Test's frozen method."""
    spec = validate_test(test)
    acquisition = ResponseAcquisition.model_validate(acquisition).model_dump()
    planned = test["stimulus"]["planned_frames"]
    frames = acquisition["frames"]
    if [f["frame_id"] for f in frames] != [f["frame_id"] for f in planned]:
        raise ValueError(
            "Every planned frame needs an ordered record; retain missing frames explicitly"
        )
    for key, stimulus_key in (
        ("program_hash", "control_program_hash"),
        ("scene_hash", "scene_sequence_hash"),
        ("policy_hash", "control_policy_hash"),
    ):
        if acquisition[key] != test["stimulus"][stimulus_key]:
            raise ValueError("Acquisition program/scene/policy identity differs from the Test")
    raw = np.asarray(raw)
    expected_count = sum(f["raw_index"] is not None for f in frames)
    if (
        raw.dtype.kind not in "ui"
        or raw.dtype.itemsize > 4
        or raw.ndim != 3
        or list(raw.shape) != [expected_count, *spec.frame_shape]
        or raw.nbytes > 64 * 1024**2
    ):
        raise ValueError("Response RAW requires the exact ordered native integer frame stack")
    if expected_count and (raw.min() < 0 or raw.max() > 2**spec.raw_format.bit_depth - 1):
        raise ValueError("RAW ADC codes exceed the declared bit depth")
    row, col = spec.geometry["crop_origin"]
    yy, xx = np.indices(spec.frame_shape)
    cfa = np.array([[0, 1], [1, 2]])[(yy + row) % 2, (xx + col) % 2]
    policy = spec.statistics_policy.model_dump()
    raw_format = spec.raw_format.model_dump()
    offset = acquisition["test_clock_offset_ns"]
    alignment_bound = acquisition["alignment_absolute_bound_ns"]
    alignment, samples = [], []
    for frame, plan in zip(frames, planned, strict=True):
        timestamp = frame["frame_start_ns"]
        mapped = timestamp + offset if timestamp is not None else None
        if mapped is not None and abs(mapped) > MAX_TIME_NS:
            raise ValueError("Mapped relative frame timestamp exceeds the supported range")
        difference = None if mapped is None else abs(mapped - plan["frame_start_ns"])
        if difference is None or alignment_bound is None:
            alignment_status = "unknown"
        elif difference + alignment_bound <= spec.maximum_frame_alignment_error_ns:
            alignment_status = "aligned"
        elif difference - alignment_bound > spec.maximum_frame_alignment_error_ns:
            alignment_status = "mismatch"
        else:
            alignment_status = "uncertain"
        alignment.append(
            {
                "frame_id": frame["frame_id"],
                "status": alignment_status,
                "absolute_difference_ns": difference,
            }
        )
        if frame["latch_at_ns"] is not None and timestamp is not None:
            expected_latch = timestamp + spec.mode["hidden_top_rows"] * spec.mode["line_time_ns"]
            if frame["latch_at_ns"] != expected_latch:
                raise ValueError("Frame latch differs from the declared full-sensor readout mode")
        statistics = (
            None
            if frame["raw_index"] is None
            else raw_statistics(raw[frame["raw_index"]], cfa, raw_format, policy)
        )
        command = next(
            (
                c
                for c in acquisition["commands"]
                if c["source_frame_id"] == frame["frame_id"]
                and c["kind"] == spec.observable.split("_")[0]
            ),
            None,
        )
        value, status = _sample(spec.observable, statistics, frame["applied"], command)
        samples.append(
            {
                "frame_id": frame["frame_id"],
                "time_ns": mapped,
                "value": value,
                "status": status,
                "statistics": statistics,
            }
        )
    result = {
        "schema": METHOD,
        "method": method(spec.model_dump()),
        "metric_id": test["metric_id"],
        "unit": test["unit"],
        "observable_unit": OBSERVABLES[spec.observable],
        "samples": samples,
        "alignment": alignment,
        "acquisition": acquisition,
        "clock": deepcopy(test["clock"]),
        "limitations": [
            "WB ratios are gray-world diagnostics, not general color accuracy.",
            "ADC checks do not establish hardware linearity or detect hidden well clipping.",
            "No dark-current subtraction, interpolation or invented control event times.",
        ],
    }
    if spec.reduction == "trajectory":
        result["points"] = [{k: s[k] for k in ("frame_id", "value", "status")} for s in samples]
    else:
        ids = [s["frame_id"] for s in samples]
        window = samples[
            ids.index(spec.window_start_frame_id) : ids.index(spec.window_end_frame_id) + 1
        ]
        value, status = None, "incomplete_window"
        if all(s["value"] is not None for s in window):
            errors = [abs(s["value"] - spec.target) for s in window]
            if spec.reduction == "peak_absolute_target_error":
                value, status = max(errors), "valid"
            elif all(s["time_ns"] is not None for s in window):
                status = "not_settled_in_window"
                for index in range(len(window) - spec.minimum_dwell_frames + 1):
                    if all(e <= spec.settling_band for e in errors[index:]):
                        value = window[index]["time_ns"] - window[0]["time_ns"]
                        status = "valid"
                        break
        result["points"] = [{"frame_id": "window", "value": value, "status": status}]
        result["response_window"] = {
            "start_frame_id": spec.window_start_frame_id,
            "end_frame_id": spec.window_end_frame_id,
            "target": spec.target,
            "settling_band": spec.settling_band,
            "minimum_dwell_frames": spec.minimum_dwell_frames,
            "scope": "Nominal sampled response in the fixed window; "
            "no continuous-time stability claim.",
        }
    result["coverage"] = {
        "planned_frames": len(frames),
        "raw_frames": expected_count,
        "valid_points": sum(p["value"] is not None for p in result["points"]),
        "total_points": len(result["points"]),
    }
    return result


def metric_fields(observation):
    points = [p for p in observation["points"] if p["value"] is not None]
    return {
        "metric_id": observation["metric_id"],
        "unit": observation["unit"],
        "reference_plane": "sensor_raw",
        "observation_method": observation["method"],
        "response_observation": observation,
        "values": [float(p["value"]) for p in points],
        "axes": [
            {"name": "response_sample", "unit": "1", "values": [p["frame_id"] for p in points]}
        ],
        "clock": observation["clock"],
    }
