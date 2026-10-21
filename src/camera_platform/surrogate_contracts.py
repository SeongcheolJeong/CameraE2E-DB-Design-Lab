"""Explicit reference splits and numerical acceptance for metric surrogates."""

from pydantic import Field, model_validator

from .contracts import Contract


class SurrogateSources(Contract):
    study_id: str
    run_ids: list[str] = Field(min_length=1, max_length=24)


class SurrogateMetric(Contract):
    metric: str
    unit: str
    max_absolute_error: float = Field(gt=0)


class SurrogatePlan(Contract):
    name: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=2000)
    training: list[SurrogateSources] = Field(min_length=1, max_length=16)
    validation: list[SurrogateSources] = Field(min_length=1, max_length=16)
    metrics: list[SurrogateMetric] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique(self):
        for split in (self.training, self.validation):
            ids = [r for s in split for r in s.run_ids]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate source run in surrogate split")
        if set(r for s in self.training for r in s.run_ids) & set(
            r for s in self.validation for r in s.run_ids
        ):
            raise ValueError("Training and validation run IDs must be disjoint")
        if len({m.metric for m in self.metrics}) != len(self.metrics):
            raise ValueError("Duplicate surrogate output metric")
        return self


class SurrogateSubmit(Contract):
    specification: SurrogatePlan
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SurrogatePredict(Contract):
    # Keys and units must exactly match the trained axes; no positional guessing.
    points: list[dict[str, float]] = Field(min_length=1, max_length=4096)
    units: dict[str, str]
