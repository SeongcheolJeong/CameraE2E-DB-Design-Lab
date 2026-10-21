"""Preserved nonlinear acquisition metadata, independent of response calibration."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from . import timing
from .contracts import Contract, Provenance

SCHEMA = "camera_nonlinear_capture_v1"
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_SELECTED_PIXELS = 1_048_576


class RawFormat(Contract):
    cfa: Literal["RGGB", "GRBG", "GBRG", "BGGR"]
    bit_depth: int = Field(ge=4, le=24)
    black_level_dn: float = Field(ge=0)
    response_type: Literal["nonlinear_charge_voltage"]
    packing: Literal["unpacked"]
    layout: Literal["bayer"]
    hdr_streams: Literal[1]

    @model_validator(mode="after")
    def code_domain(self):
        if self.black_level_dn >= 2**self.bit_depth - 1:
            raise ValueError("Black level must be below the ADC upper code")
        return self


class NativeGeometry(Contract):
    kind: Literal["native_sensor"]
    full_sensor_shape: list[int] = Field(min_length=2, max_length=2)
    crop_origin: list[int] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def coordinates(self):
        timing._pair(self.full_sensor_shape, "full_sensor_shape", 2, 1_000_000)
        timing._pair(self.crop_origin, "crop_origin", 0, 1_000_000)
        return self


class ReadoutSettings(Contract):
    analog_gain: float | None = Field(ge=1e-6, le=1e6)
    digital_gain: float | None = Field(gt=0, le=1e6)


class AppliedSettings(Contract):
    exposure_ns: int | None = Field(ge=1, le=timing.MAX_EXPOSURE_NS)
    gain: float | None = Field(ge=1e-6, le=1e6)
    temperature_c: float | None = Field(ge=-273.15, le=1000)
    applied_at_ns: int | None = Field(ge=-timing.MAX_TIME_NS, le=timing.MAX_TIME_NS)
    readout_settings: list[ReadoutSettings] = Field(min_length=1, max_length=2)


class SettingsEvidence(Contract):
    status: Literal["recorded_applied", "command_only", "unknown"]
    source_uri: str = Field(min_length=1, max_length=2000)
    method_and_version: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=2000)


class CaptureFrame(Contract):
    frame_id: str = Field(min_length=1, max_length=200)
    raw_index: int = Field(ge=0, le=63)
    frame_start_ns: int | None = Field(ge=-timing.MAX_TIME_NS, le=timing.MAX_TIME_NS)
    applied: AppliedSettings
    settings_evidence: SettingsEvidence

    @model_validator(mode="after")
    def evidence(self):
        if self.settings_evidence.status == "recorded_applied":
            values = self.applied.model_dump()
            if any(values[k] is None for k in ("exposure_ns", "gain", "temperature_c")):
                raise ValueError("Recorded applied exposure, gain and temperature must be known")
            if any(v is None for path in values["readout_settings"] for v in path.values()):
                raise ValueError("Recorded applied readout gains must be known")
        return self


class NonlinearCaptureImport(Provenance):
    schema: Literal["camera_nonlinear_capture_v1"]
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    assembly_id: str = Field(min_length=1)
    test_id: str = Field(min_length=1)
    dataset_ids: list[str] = Field(min_length=1, max_length=100)
    acquisition_group_id: str = Field(min_length=1, max_length=160)
    instrument: dict
    geometry: NativeGeometry
    pixel_pitch_um: float = Field(gt=0, le=1000)
    readout_mode: Literal["sequential_frames", "shared_charge_paths"]
    raw_formats: list[RawFormat] = Field(min_length=1, max_length=2)
    clock: dict
    timing_mode: dict
    frames: list[CaptureFrame] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def acquisition(self):
        for name in ("parents", "dataset_ids"):
            values = getattr(self, name)
            if len(values) != len(set(values)) or any(not v.strip() for v in values):
                raise ValueError("Capture identities must be nonempty and unique")
        if self.source_kind == "measured" and (
            not self.physical_instance
            or not self.instrument.get("id")
            or not self.instrument.get("calibration_revision")
        ):
            raise ValueError("Measured RAW requires physical sample and instrument calibration")
        paths = 2 if self.readout_mode == "shared_charge_paths" else 1
        if len(self.raw_formats) != paths:
            raise ValueError("RAW formats require one path or ordered HCG/LCG paths")
        if len({f.cfa for f in self.raw_formats}) != 1:
            raise ValueError("Shared-charge readouts must retain the same native CFA")
        ids = [frame.frame_id for frame in self.frames]
        if len(ids) != len(set(ids)):
            raise ValueError("Frame identities must be unique")
        for index, frame in enumerate(self.frames):
            if frame.raw_index != index or len(frame.applied.readout_settings) != paths:
                raise ValueError("Ordered frames must map exactly once to every RAW acquisition")
            if (
                paths == 1
                and frame.settings_evidence.status == "recorded_applied"
                and frame.applied.gain != frame.applied.readout_settings[0].analog_gain
            ):
                raise ValueError("Sequential recorded gain must equal its analog readout gain")
        if self.clock.get("status") == "unknown":
            timing._keys(self.clock, {"status", "reason"}, "clock")
            timing._text(self.clock["reason"], "clock.reason", 2000)
        else:
            self.clock = timing._clock(self.clock, "clock")
        if self.timing_mode.get("shutter") == "unknown":
            timing._keys(self.timing_mode, {"shutter", "reason"}, "timing_mode")
            timing._text(self.timing_mode["reason"], "timing_mode.reason", 2000)
        else:
            self.timing_mode = timing._mode(self.timing_mode)
        return self


def validate_timing(data, shape):
    """Reuse exact timing primitives; never fabricate command IDs or a control sequence."""
    geometry, mode = data["geometry"], data["timing_mode"]
    if any(
        geometry["crop_origin"][i] + shape[i] > geometry["full_sensor_shape"][i] for i in (0, 1)
    ):
        raise ValueError("Native RAW crop lies outside the declared sensor")
    if data["clock"].get("status") == "unknown" or mode["shutter"] == "unknown":
        return
    times, previous, last_known_start = [], None, None
    for frame in data["frames"]:
        start, applied = frame["frame_start_ns"], frame["applied"]
        if start is None:
            previous = None
            continue
        times.append(start)
        if last_known_start is not None and start <= last_known_start:
            raise ValueError("Known acquisition starts must strictly increase")
        last_known_start = start
        if frame["settings_evidence"]["status"] != "recorded_applied":
            previous = frame
            continue
        first = timing._endpoint(
            start + mode["hidden_top_rows"] * mode["line_time_ns"], "first_row"
        )
        last = timing._endpoint(
            first
            + (geometry["full_sensor_shape"][0] - 1) * mode["line_time_ns"]
            + applied["exposure_ns"],
            "last_row_end",
        )
        times.extend([first, last])
        if applied["applied_at_ns"] is not None:
            times.append(applied["applied_at_ns"])
            if applied["applied_at_ns"] > first:
                raise ValueError("Recorded settings were applied after first active row exposure")
        if previous is not None and previous["settings_evidence"]["status"] == "recorded_applied":
            old = previous["applied"]
            if previous["frame_start_ns"] + old["exposure_ns"] > start:
                raise ValueError("Same native pixel exposures overlap")
            if old["applied_at_ns"] is not None and applied["applied_at_ns"] is not None:
                if applied["applied_at_ns"] < old["applied_at_ns"]:
                    raise ValueError("Recorded applied settings cannot move backwards")
                if applied["applied_at_ns"] == old["applied_at_ns"] and applied != old:
                    raise ValueError("One applied event cannot contain conflicting settings")
        previous = frame
    timing._span(times, "acquisition")
