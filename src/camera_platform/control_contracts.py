"""Versioned, explicit control policy and acquisition-program contracts.

Latencies are declared simulation inputs, never measured hardware events.
The readout gain scales collected charge into ADC codes; it is not a general
analogue gain / conversion-gain path model.
"""

from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .contracts import Contract, Provenance
from .timing import MAX_EXPOSURE_NS, MAX_TIME_NS, _clock, _mode

MAX_CONTROL_FRAMES = 64


class IntegerControlRange(Contract):
    minimum: int = Field(ge=1, le=MAX_EXPOSURE_NS)
    maximum: int = Field(ge=1, le=MAX_EXPOSURE_NS)
    step: int = Field(ge=1, le=MAX_EXPOSURE_NS)

    @model_validator(mode="after")
    def grid(self):
        if self.maximum < self.minimum or (self.maximum - self.minimum) % self.step:
            raise ValueError("Control maximum must lie on the grid anchored at minimum")
        return self


class GainControlRange(Contract):
    minimum: float = Field(ge=1e-6, le=1e6)
    maximum: float = Field(ge=1e-6, le=1e6)
    step: float = Field(ge=1e-6, le=1e6)

    @model_validator(mode="after")
    def grid(self):
        if not on_grid(self.maximum, self.minimum, self.maximum, self.step):
            raise ValueError("Control maximum must lie on the grid anchored at minimum")
        return self


def on_grid(value, minimum, maximum, step):
    value, minimum, maximum, step = map(lambda x: Decimal(str(x)), (value, minimum, maximum, step))
    return minimum <= value <= maximum and (value - minimum) % step == 0


class AutoExposurePolicy(Contract):
    algorithm: Literal["green_mean_exposure_priority_v1"]
    enabled: bool
    target_normalized: float = Field(ge=1e-6, lt=1)
    deadband_ev: float = Field(ge=0, le=2)
    max_step_ev: float = Field(gt=0, le=4)
    minimum_delay_frames: int = Field(ge=1, le=64)
    exposure_ns: IntegerControlRange
    readout_gain: GainControlRange


class AutoWhiteBalancePolicy(Contract):
    algorithm: Literal["bayer_cell_gray_world_v1"]
    enabled: bool
    valid_signal_min: float = Field(ge=1e-6, lt=1)
    valid_signal_max: float = Field(gt=0, lt=1)
    minimum_valid_cells: int = Field(ge=1, le=262144)
    deadband_ev: float = Field(ge=0, le=2)
    max_step_ev: float = Field(gt=0, le=4)
    minimum_delay_frames: int = Field(ge=1, le=64)
    gains: GainControlRange

    @model_validator(mode="after")
    def bounds(self):
        if self.valid_signal_min >= self.valid_signal_max:
            raise ValueError("AWB minimum valid signal must be below maximum")
        if self.gains.minimum < 1 / 16 or self.gains.maximum > 16:
            raise ValueError("WB gains must remain inside the fixed ISP domain [1/16,16]")
        return self


class ControlStatisticsROI(Contract):
    reference: Literal["native_crop"]
    origin: list[int] = Field(min_length=2, max_length=2)
    shape: list[int] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def shape_bounds(self):
        if any(v < 0 for v in self.origin) or any(v < 2 or v > 512 or v % 2 for v in self.shape):
            raise ValueError("Statistics ROI requires nonnegative origin and even shape in [2,512]")
        return self


class DeclaredControlLatency(Contract):
    kind: Literal["declared_simulation"]
    readout_tail_ns: int = Field(ge=0, le=MAX_TIME_NS)
    frame_transport_ns: int = Field(ge=0, le=MAX_TIME_NS)
    statistics_ns: int = Field(ge=0, le=MAX_TIME_NS)
    controller_ns: int = Field(ge=0, le=MAX_TIME_NS)
    command_transport_ns: int = Field(ge=0, le=MAX_TIME_NS)
    actuator_ns: int = Field(ge=0, le=MAX_TIME_NS)


class CameraControlPolicy(Contract):
    model_config = ConfigDict(serialize_by_alias=True)
    schema_version: Literal["camera_control_policy_v1"] = Field(alias="schema")
    statistics_roi: ControlStatisticsROI
    ae: AutoExposurePolicy
    awb: AutoWhiteBalancePolicy
    latency: DeclaredControlLatency


class InitialControlState(Contract):
    exposure_ns: int = Field(ge=1, le=MAX_EXPOSURE_NS)
    gain: float = Field(ge=1e-6, le=1e6)
    wb_gains_rgb: list[float] = Field(min_length=3, max_length=3)
    isp_model_id: str = Field(min_length=1, max_length=200)


class ControlFrame(Contract):
    frame_id: str = Field(min_length=1, max_length=200)
    frame_start_ns: int = Field(ge=-MAX_TIME_NS, le=MAX_TIME_NS)


class ClosedLoopProgram(Contract):
    model_config = ConfigDict(serialize_by_alias=True)
    schema_version: Literal["closed_loop_program_v1"] = Field(alias="schema")
    clock: dict
    mode: dict
    frames: list[ControlFrame] = Field(min_length=1, max_length=MAX_CONTROL_FRAMES)
    initial: InitialControlState
    policy: CameraControlPolicy

    @model_validator(mode="after")
    def coherence(self):
        _clock(self.clock, "control_program.clock")
        _mode(self.mode)
        ids = [f.frame_id for f in self.frames]
        starts = [f.frame_start_ns for f in self.frames]
        if len(set(ids)) != len(ids) or any(
            b <= a for a, b in zip(starts, starts[1:], strict=False)
        ):
            raise ValueError("Control frames require unique IDs and strictly increasing starts")
        ranges = [
            (self.initial.exposure_ns, self.policy.ae.exposure_ns),
            (self.initial.gain, self.policy.ae.readout_gain),
        ]
        ranges += [(v, self.policy.awb.gains) for v in self.initial.wb_gains_rgb]
        if any(not on_grid(v, r.minimum, r.maximum, r.step) for v, r in ranges):
            raise ValueError("Initial controls must lie on their declared actuator grids")
        return self


class ControlPolicyCreate(Provenance):
    model_config = ConfigDict(serialize_by_alias=True)
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=80)
    payload: CameraControlPolicy


class ControlSchedule(Contract):
    model_config = ConfigDict(serialize_by_alias=True)
    schema_version: Literal["camera_control_schedule_v1"] = Field(alias="schema")
    clock: dict
    mode: dict
    frames: list[ControlFrame] = Field(min_length=1, max_length=MAX_CONTROL_FRAMES)
    initial: InitialControlState


class ControlProgramCreate(Provenance):
    model_config = ConfigDict(serialize_by_alias=True)
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=80)
    assembly_id: str = Field(min_length=1)
    scene_sequence_id: str = Field(min_length=1)
    control_policy_id: str = Field(min_length=1)
    payload: ControlSchedule
