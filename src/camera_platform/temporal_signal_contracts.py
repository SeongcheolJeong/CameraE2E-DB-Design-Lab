"""Frozen, explicitly authored temporal signal reference contracts.

The independent-reference claim is authored provenance, not authenticated
physical truth. The array is never a processor input.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import Contract


class TemporalStep(Contract):
    onset_frame: int = Field(ge=1, le=15)
    steady_start_frame: int = Field(ge=1, le=15)
    onset_fraction: float = Field(ge=1e-3, lt=1)
    settling_fraction: float = Field(ge=1e-3, lt=1)

    @model_validator(mode="after")
    def order(self):
        if self.steady_start_frame < self.onset_frame:
            raise ValueError("Step steady support precedes onset")
        return self


class TemporalFlickerWindow(Contract):
    kind: Literal["known_period_two"]
    start_frame: int = Field(ge=0, le=15)
    end_frame: int = Field(ge=2, le=16)
    maximum_reference_variation: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def span(self):
        if self.end_frame - self.start_frame < 2:
            raise ValueError("Period-two support needs at least two samples")
        return self


class TemporalReferenceImport(Contract):
    schema: Literal["camera_temporal_signal_reference_v1"]
    name: str = Field(min_length=1, max_length=160)
    revision: str = Field(min_length=1, max_length=80)
    derivation: Literal["authored_independent"]
    source_kind: Literal["simulated"]
    source_uri: str = Field(min_length=1, max_length=2000)
    method_and_version: str = Field(min_length=1, max_length=500)
    license: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=4000)
    uncertainty: dict[str, Any]
    source_id: str = Field(min_length=1, max_length=200)
    selector: dict[str, Any]
    isp_model_id: str = Field(min_length=1, max_length=200)
    source_binding: dict[str, Any]
    fixed_color_profile: dict[str, Any]
    roi: dict[str, int]
    row_transfer_ns: list[list[int]]
    step: TemporalStep | None = None
    flicker_window: TemporalFlickerWindow | None = None
    minimum_reference_change: float = Field(ge=1e-6, le=1)
    reference_stage: Literal["ccm_code_rgb_frames"]
    unit: Literal["normalized_code_rgb"]

    @model_validator(mode="after")
    def provenance(self):
        if (
            self.source_uri.startswith(("run://", "replay://", "isp-study://", "comparison://"))
            or self.source_uri == "synthetic://replace-with-independent-generator-id"
            or self.method_and_version == "replace-with-independent-generator-version"
            or self.license == "replace-with-source-license"
            or self.description == (
                "State how truth was generated independently of ISP candidate output."
            )
            or any(not value.strip() or value != value.strip() for value in (
                self.source_uri, self.method_and_version, self.license, self.description
            ))
            or set(self.uncertainty) != {"kind", "reason"}
            or self.uncertainty["kind"] != "unknown"
            or not isinstance(self.uncertainty["reason"], str)
            or not self.uncertainty["reason"]
        ):
            raise ValueError(
                "Temporal truth needs independent authored provenance and unknown uncertainty"
            )
        return self
