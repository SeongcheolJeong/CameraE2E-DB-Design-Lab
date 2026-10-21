"""Versioned design-study contracts; all coordinates and decisions are explicit."""

from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract


class StudyAxis(Contract):
    parameter: str = Field(min_length=1, max_length=100)
    unit: str = Field(min_length=1, max_length=40)
    values: list[float] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.values)) != len(self.values):
            raise ValueError("Study axis values must be unique")
        return self


class StudyObjective(Contract):
    metric: str
    direction: Literal["minimize", "maximize"]


class StudyConstraint(Contract):
    metric: str
    operator: Literal["<=", ">="]
    value: float
    unit: str


class StudyPlan(Contract):
    name: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=2000)
    assembly_ids: list[str] = Field(min_length=1, max_length=8)
    test_id: str
    plugin_id: Literal["quick_estimate_v1", "response_db_v1"] = "response_db_v1"
    design: Literal["one_at_a_time", "cartesian"] = "one_at_a_time"
    axes: list[StudyAxis] = Field(min_length=1, max_length=6)
    objectives: list[StudyObjective] = Field(min_length=1, max_length=4)
    constraints: list[StudyConstraint] = Field(default_factory=list, max_length=8)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    frames: int = Field(default=4, ge=2, le=16)
    output_size: list[int] = Field(default_factory=lambda: [32, 32], min_length=2, max_length=2)

    @model_validator(mode="after")
    def unique_identifiers(self):
        for values in [
            self.assembly_ids,
            [a.parameter for a in self.axes],
            [o.metric for o in self.objectives],
        ]:
            if len(set(values)) != len(values):
                raise ValueError("Duplicate study assemblies, axes or objectives")
        return self


class StudySubmit(Contract):
    specification: StudyPlan
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
