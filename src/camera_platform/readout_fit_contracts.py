"""Original anchored total-charge observations and bounded fitting requests."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract, Provenance
from .readout_calibration_contracts import AbsoluteChargeReference

SCHEMA = "camera_readout_fit_observations_v1"
REQUEST_SCHEMA = "camera_readout_fit_request_v1"
Identity = Annotated[str, Field(min_length=1, max_length=200)]
Coordinate = Annotated[int, Field(ge=0, le=1_000_000)]
ChargeMean = Annotated[float, Field(ge=0)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _identities(values, label):
    if len(set(values)) != len(values) or any(not value.strip() for value in values):
        raise ValueError(f"{label} requires unique nonempty identities")


class ObsROI(Contract):
    row_range: list[Coordinate] = Field(min_length=2, max_length=2)
    column_range: list[Coordinate] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def even_native_ranges(self):
        for values in (self.row_range, self.column_range):
            if values[1] <= values[0] or any(value % 2 for value in values):
                raise ValueError(
                    "Observation ROI uses increasing even full-native end-exclusive bounds"
                )
        return self


class ReadoutFitObservationFrame(Contract):
    frame_id: Identity
    mean_total_charge_e: list[ChargeMean] = Field(min_length=4, max_length=4)


class ReadoutFitObservationImport(Provenance):
    schema: Literal["camera_readout_fit_observations_v1"]
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=80)
    capture_id: Identity
    dataset_role: Literal["development", "holdout"]
    dataset_ids: list[Identity] = Field(min_length=1, max_length=100)
    roi: ObsROI
    charge_statistics: Literal["poisson_total_pre_full_well"]
    charge_reference: AbsoluteChargeReference
    frames: list[ReadoutFitObservationFrame] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def declared_identities(self):
        _identities(self.parents, "Observation parents")
        _identities(self.dataset_ids, "Observation datasets")
        _identities([frame.frame_id for frame in self.frames], "Observation frames")
        _identities(self.charge_reference.evidence_document_ids, "Charge-reference documents")
        if self.physical_instance is not None and not self.physical_instance.strip():
            raise ValueError("Physical-instance identity cannot be blank")
        if self.source_kind == "measured" and (
            self.physical_instance is None or self.charge_reference.kind == "authored_simulation"
        ):
            raise ValueError(
                "Measured observations require a physical instance and external charge anchor"
            )
        return self


class ReadoutFitRequest(Contract):
    schema: Literal["camera_readout_fit_request_v1"]
    name: str = Field(min_length=1, max_length=200)
    base_calibration_id: Identity
    training_observation_ids: list[Identity] = Field(min_length=1, max_length=100)
    validation_observation_ids: list[Identity] = Field(min_length=1, max_length=100)
    slope_scale_bounds: list[float] = Field(min_length=2, max_length=2)
    sigma_v_bounds: list[float] = Field(min_length=2, max_length=2)
    max_iterations: int = Field(ge=10, le=200)

    @model_validator(mode="after")
    def fitting_bounds(self):
        _identities(self.training_observation_ids, "Training observations")
        _identities(self.validation_observation_ids, "Validation observations")
        if set(self.training_observation_ids) & set(self.validation_observation_ids):
            raise ValueError("Training and validation observation IDs must be distinct")
        if not 0.01 <= self.slope_scale_bounds[0] < self.slope_scale_bounds[1] <= 100:
            raise ValueError("Slope scale bounds require 0.01 <= low < high <= 100")
        if not 0 < self.sigma_v_bounds[0] < self.sigma_v_bounds[1] <= 1:
            raise ValueError("Read-noise bounds require 0 < low < high <= 1 V")
        return self


class ReadoutFitPublish(Contract):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=80)
    request_sha256: Sha256
    result_sha256: Sha256


FIELDS = set(ReadoutFitObservationImport.model_fields)


def validate_metadata(value):
    return ReadoutFitObservationImport.model_validate(value).model_dump()
