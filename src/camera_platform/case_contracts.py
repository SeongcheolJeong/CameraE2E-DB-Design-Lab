"""Append-only engineering review requests; snapshots are resolved by the server."""

from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from .contracts import Contract

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]
RequirementKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")]


class CaseRequirement(Contract):
    key: RequirementKey
    description: Text
    test_id: Identifier | None = None


class CaseEvidence(Contract):
    kind: Literal["runs", "measurements", "validations", "studies"]
    id: Identifier
    requirement_keys: list[RequirementKey] = Field(min_length=1, max_length=40)


class ISPStudyCaseEvidence(Contract):
    """Dataset-scoped ISP evidence; legacy evidence serialization stays unchanged."""

    kind: Literal["isp_studies"]
    id: Identifier
    requirement_keys: list[RequirementKey] = Field(min_length=1, max_length=40)
    dataset_requirements: dict[Identifier, list[RequirementKey]] = Field(
        min_length=1, max_length=24
    )
    selection_id: Identifier | None = None

    @model_validator(mode="after")
    def explicit_dataset_scope(self):
        linked = set()
        for keys in self.dataset_requirements.values():
            if not keys or len(keys) > 40 or len(set(keys)) != len(keys):
                raise ValueError("Each ISP dataset needs distinct requirement keys")
            linked.update(keys)
        if linked != set(self.requirement_keys):
            raise ValueError("ISP dataset scope must cover exactly the linked requirements")
        return self


ScopedCaseEvidence = Annotated[
    CaseEvidence | ISPStudyCaseEvidence, Field(discriminator="kind")
]


class CaseDecision(Contract):
    status: Literal["open", "investigate", "candidate", "rejected", "deferred"] = "open"
    rationale: Text


class CaseNextExperiment(Contract):
    question: Text
    reason: Text
    mode: Literal[
        "quick_estimate",
        "response_db",
        "surrogate_prediction",
        "detailed_characterization",
        "measurement",
    ]
    conditions: dict[str, Any] = Field(default_factory=dict)
    assembly_id: Identifier | None = None
    test_id: Identifier | None = None


class CaseReviewCreate(Contract):
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
    previous_revision_id: Identifier | None = None
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    question: Text
    requirements: list[CaseRequirement] = Field(default_factory=list, max_length=40)
    assembly_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    evidence: list[ScopedCaseEvidence] = Field(default_factory=list, max_length=80)
    decision: CaseDecision
    unresolved: list[Text] = Field(default_factory=list, max_length=80)
    next_experiments: list[CaseNextExperiment] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def unique_and_scoped(self):
        for values in (
            self.assembly_ids,
            [item.key for item in self.requirements],
            [(item.kind, item.id) for item in self.evidence],
            *(item.requirement_keys for item in self.evidence),
        ):
            if len(set(values)) != len(values):
                raise ValueError("Duplicate case identifiers or evidence links")
        requirements = {item.key: item for item in self.requirements}
        for item in self.evidence:
            if any(key not in requirements for key in item.requirement_keys):
                raise ValueError("Evidence references an unknown case requirement")
            if any(not requirements[key].test_id for key in item.requirement_keys):
                raise ValueError("Evidence requires an explicitly fixed requirement test")
        tests = {item.test_id for item in self.requirements if item.test_id}
        for item in self.next_experiments:
            if item.assembly_id and item.assembly_id not in self.assembly_ids:
                raise ValueError("Next experiment assembly is outside this case")
            if item.test_id and item.test_id not in tests:
                raise ValueError("Next experiment test is outside this case requirements")
        return self
