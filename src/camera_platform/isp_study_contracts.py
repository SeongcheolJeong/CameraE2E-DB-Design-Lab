"""Strict retained-RAW study, explicit selection and independent reference contracts."""

from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import Contract, Provenance


class ISPAxis(Contract):
    parameter: Literal["raw_denoise.strength", "linear_sharpen.amount"]
    values: list[float] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def bounded(self):
        if len(set(self.values)) != len(self.values) or any(not 0 <= v <= 1 for v in self.values):
            raise ValueError("ISP coordinates must be unique finite values in [0,1]")
        return self


class ISPStationarity(Contract):
    kind: Literal["declared_static"] = "declared_static"
    source_kind: Literal["estimated", "simulated", "measured", "vendor"]
    description: str = Field(min_length=1, max_length=2000)


class ISPStudyDataset(Contract):
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    source_kind: Literal["run", "capture"]
    source_id: str = Field(min_length=1)
    purpose: Literal["tuning", "independent_validation"]
    acquisition_group_id: str = Field(min_length=1, max_length=160)
    stationarity: ISPStationarity
    metric_spec: dict[str, Any]
    reference_id: str | None = None


class ISPReduction(Contract):
    metric: str = Field(min_length=1)
    dataset_keys: list[str] = Field(min_length=1, max_length=12)
    reducer: Literal["max", "min", "max_abs"] = "max"


class ISPObjective(ISPReduction):
    direction: Literal["minimize", "maximize"]


class ISPConstraint(ISPReduction):
    operator: Literal["<=", ">="]
    value: float
    unit: str = Field(min_length=1)


class ISPStudyPlan(Contract):
    schema: Literal["camera_isp_study_v1"] = "camera_isp_study_v1"
    name: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=2000)
    phase: Literal["tuning", "independent_validation"] = "tuning"
    calibration_id: str
    baseline_isp_model_id: str
    selection_id: str | None = None
    parent_study_id: str | None = None
    attempt: int = Field(default=1, ge=1, le=100)
    axes: list[ISPAxis] = Field(default_factory=list, max_length=2)
    datasets: list[ISPStudyDataset] = Field(min_length=1, max_length=12)
    objectives: list[ISPObjective] = Field(min_length=1, max_length=4)
    constraints: list[ISPConstraint] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def identity(self):
        for values in (
            [a.parameter for a in self.axes],
            [d.key for d in self.datasets],
            [(d.source_kind, d.source_id) for d in self.datasets],
        ):
            if len(set(values)) != len(values):
                raise ValueError(
                    "Study axes, dataset keys and RAW source identities must be unique"
                )
        if any(d.purpose != self.phase for d in self.datasets):
            raise ValueError("Every dataset purpose must equal the study phase")
        if self.phase == "tuning" and (not self.axes or self.selection_id):
            raise ValueError("Tuning requires explicit axes and cannot consume a selection")
        if self.phase == "independent_validation" and (self.axes or not self.selection_id):
            raise ValueError("Independent validation fixes a selection and accepts no tuning axes")
        keys = {d.key for d in self.datasets}
        for target in [*self.objectives, *self.constraints]:
            if (
                len(set(target.dataset_keys)) != len(target.dataset_keys)
                or not set(target.dataset_keys) <= keys
            ):
                raise ValueError("Reducers require unique explicit dataset keys from this plan")
        if self.attempt > 1 and not self.parent_study_id:
            raise ValueError("A new attempt must name the preserved previous Study")
        return self


class ISPStudySubmit(Contract):
    specification: ISPStudyPlan
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ISPSelectionCreate(Contract):
    candidate_id: str
    rationale: str = Field(min_length=1, max_length=4000)
    analysis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ISPReferenceImport(Provenance):
    schema: Literal["camera_isp_reference_v1"] = "camera_isp_reference_v1"
    name: str = Field(min_length=1, max_length=160)
    revision: str = Field(default="1", min_length=1, max_length=80)
    acquisition_group_id: str = Field(min_length=1, max_length=160)
    derivation: Literal["authored_independent", "independent_measurement"]
    geometry: dict[str, Any]
    pixel_pitch_um: float = Field(gt=0, le=1000)
    common_linear_profile: dict[str, Any]
    normalization_dn: float = Field(gt=0, le=2**24)
    unit: Literal["normalized_linear_sensor_rgb"] = "normalized_linear_sensor_rgb"
    reference_plane: Literal["sensor_rgb_after_fixed_wb_ccm_before_tone"] = (
        "sensor_rgb_after_fixed_wb_ccm_before_tone"
    )
    instrument: dict[str, Any] | None = None
    uncertainty: dict[str, Any]
    description: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def provenance(self):
        from .isp import default_profile, validate_profile

        keys = {"black_level", "demosaic", "white_balance", "ccm"}
        if set(self.common_linear_profile) != keys:
            raise ValueError("Reference common linear profile requires exact BLC/demosaic/WB/CCM")
        common = default_profile()
        common.update(self.common_linear_profile)
        normalized = validate_profile(common)
        self.common_linear_profile = {k: normalized[k] for k in sorted(keys)}
        if self.source_uri.startswith(("replay://", "isp-study://")):
            raise ValueError("Candidate output is not an independent reference")
        if self.derivation == "independent_measurement" and (
            self.source_kind != "measured"
            or not self.physical_instance
            or not self.instrument
            or not self.instrument.get("id")
            or not self.instrument.get("calibration_revision")
        ):
            raise ValueError("Measured references need physical identity and calibrated instrument")
        if self.derivation == "authored_independent" and self.source_kind not in {
            "estimated",
            "simulated",
        }:
            raise ValueError("Authored truth must remain explicit estimated/simulated evidence")
        if (
            set(self.uncertainty) != {"kind", "reason"}
            or self.uncertainty["kind"] != "unknown"
            or not self.uncertainty["reason"]
        ):
            raise ValueError("Reference uncertainty remains explicitly unknown in this slice")
        return self
