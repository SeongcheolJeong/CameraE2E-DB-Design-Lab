"""Strict imported nonlinear response declarations; supplied evidence is not certification."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from . import electrical, nonlinear_dcg, nonlinear_readout
from .contracts import Contract, Provenance

SCHEMA = "camera_nonlinear_response_calibration_v1"
EVIDENCE_SCHEMA = "camera_nonlinear_response_evidence_v1"
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Identity = Annotated[str, Field(min_length=1, max_length=200)]


class CalibrationInstrument(Contract):
    id: Identity
    calibration_revision: Identity


class ChargeReferenceUncertainty(Contract):
    kind: Literal[
        "absolute_bound",
        "relative_bound",
        "absolute_standard_uncertainty",
        "relative_standard_uncertainty",
    ]
    value: float = Field(ge=0, le=1e13)
    unit: Literal["electron", "1"]
    description: Text

    @model_validator(mode="after")
    def declared_units(self):
        if self.unit != ("1" if self.kind.startswith("relative_") else "electron"):
            raise ValueError("Charge-reference uncertainty kind and unit differ")
        return self


class AbsoluteChargeReference(Contract):
    kind: Literal[
        "external_charge",
        "external_radiometric",
        "vendor_calibration",
        "authored_simulation",
    ]
    reference_id: Identity
    source_uri: Text
    method_and_version: Text
    instrument: CalibrationInstrument
    uncertainty: ChargeReferenceUncertainty
    evidence_document_ids: list[Identity] = Field(min_length=1, max_length=100)


class CalibrationUncertainty(Contract):
    kind: Literal["unknown"]
    reason: Text


class CalibrationGeometry(Contract):
    full_sensor_shape: list[int] = Field(min_length=2, max_length=2)
    crop_origin: list[int] = Field(min_length=2, max_length=2)


class ReadoutGainDomain(Contract):
    readout_id: Literal["single", "HCG", "LCG"]
    analog_gain: list[float] = Field(min_length=2, max_length=2)
    digital_gain: float = Field(ge=1e-6, le=1e6)


class ReadoutCalibrationApplicability(Contract):
    exposure_s: list[float] = Field(min_length=2, max_length=2)
    temperature_c: list[float] = Field(min_length=2, max_length=2)
    readouts: list[ReadoutGainDomain] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def finite_domains(self):
        nonlinear_readout._range(self.exposure_s, "exposure_s", 1e-12, 3600)
        nonlinear_readout._range(self.temperature_c, "temperature_c", -273.15, 1000)
        for readout in self.readouts:
            nonlinear_readout._range(readout.analog_gain, "analog_gain", 1e-6, 1e6)
        return self


class CalibrationEvidenceColumn(Contract):
    name: Identity
    unit: Identity


class CalibrationEvidenceTable(Contract):
    name: Identity
    columns: list[CalibrationEvidenceColumn] = Field(min_length=1, max_length=16)
    rows: list[list[float]] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def rectangular(self):
        if len({c.name for c in self.columns}) != len(self.columns):
            raise ValueError("Evidence table columns must be unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Evidence table rows must match explicit columns")
        return self


class CalibrationEvidenceDocument(Contract):
    id: Identity
    role: Literal["charge_reference", "response_table", "dark_rate", "read_noise", "other"]
    source_uri: Text
    method_and_version: Text
    description: Text
    tables: list[CalibrationEvidenceTable] = Field(max_length=16)


class ReadoutCalibrationEvidence(Contract):
    schema: Literal["camera_nonlinear_response_evidence_v1"]
    documents: list[CalibrationEvidenceDocument] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_documents(self):
        if len({d.id for d in self.documents}) != len(self.documents):
            raise ValueError("Calibration evidence document IDs must be unique")
        cells = sum(len(t.rows) * len(t.columns) for d in self.documents for t in d.tables)
        if cells > 1_000_000:
            raise ValueError("Calibration evidence exceeds one million numeric cells")
        return self


class ReadoutCalibrationImport(Provenance):
    schema: Literal["camera_nonlinear_response_calibration_v1"]
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=80)
    sensor_model_id: Identity
    readout_mode: Literal["sequential_frames", "shared_charge_paths"]
    geometry: CalibrationGeometry
    pixel_pitch_um: float = Field(gt=0, le=1000)
    dark_rate_shape: list[int] = Field(min_length=2, max_length=2)
    dark_rate_unit: Literal["electron/pixel/s"]
    dark_reference_temperature_c: float = Field(ge=-273.15, le=1000)
    electrical_model: dict[str, Any]
    instrument: CalibrationInstrument
    dataset_role: Literal["development", "holdout"]
    dataset_ids: list[Identity] = Field(min_length=1, max_length=100)
    reference_capture_ids: list[Identity] = Field(max_length=100)
    charge_reference: AbsoluteChargeReference
    uncertainty: CalibrationUncertainty
    applicability: ReadoutCalibrationApplicability

    @model_validator(mode="after")
    def bound_response(self):
        for label, values in (
            ("parents", self.parents),
            ("dataset_ids", self.dataset_ids),
            ("reference_capture_ids", self.reference_capture_ids),
            ("evidence_document_ids", self.charge_reference.evidence_document_ids),
        ):
            if len(set(values)) != len(values) or any(not v.strip() for v in values):
                raise ValueError(f"{label} requires unique nonempty identities")
        if self.physical_instance is not None and not self.physical_instance.strip():
            raise ValueError("Physical-instance identity cannot be blank")
        if self.source_kind == "measured" and (
            self.physical_instance is None or self.charge_reference.kind == "authored_simulation"
        ):
            raise ValueError(
                "Measured response needs a physical instance and external charge anchor"
            )
        if (
            any(n < 2 or n > 4096 or n % 2 for n in self.dark_rate_shape)
            or self.dark_rate_shape[0] * self.dark_rate_shape[1] > electrical.MAX_NATIVE_PIXELS
        ):
            raise ValueError("Expected dark-rate map requires a bounded even native HxW shape")
        electrical._geometry(self.geometry.model_dump(), self.dark_rate_shape)
        shared = self.readout_mode == "shared_charge_paths"
        validate = nonlinear_dcg.validate_model if shared else nonlinear_readout.validate_model
        self.electrical_model = validate(self.electrical_model)
        model = self.electrical_model
        if self.dark_reference_temperature_c != model["reference_temperature_c"]:
            raise ValueError("Dark-rate reference temperature differs from the electrical model")
        # Imported expected maps are authoritative; no randomized PRNU/DSNU is ever generated.
        if model["fixed_pattern"]["prnu_sigma"] != 0 or model["fixed_pattern"]["dsnu_e_per_s"] != 0:
            raise ValueError("Imported calibration cannot declare synthesized random FPN")
        expected_ids = ["HCG", "LCG"] if shared else ["single"]
        if [r.readout_id for r in self.applicability.readouts] != expected_ids:
            raise ValueError("Applicability readouts must retain the declared physical path order")
        for index, domain in enumerate(self.applicability.readouts):
            path = model["paths"][index] if shared else model
            if domain.digital_gain != path["digital_gain"]:
                raise ValueError("Applicability digital gain differs from the electrical transfer")
            if shared and domain.analog_gain != [path["analog_gain"], path["analog_gain"]]:
                raise ValueError("Paired covariance is calibrated at each fixed path analog gain")
            for temperature in self.applicability.temperature_c:
                for gain in domain.analog_gain:
                    for exposure in self.applicability.exposure_s:
                        nonlinear_readout.validate_capture_conditions(
                            nonlinear_dcg._path_model(model, index) if shared else model,
                            exposure,
                            gain,
                            temperature,
                        )
        return self


FIELDS = set(ReadoutCalibrationImport.model_fields)


def validate_metadata(value):
    return ReadoutCalibrationImport.model_validate(value).model_dump()


def validate_evidence(value, metadata):
    evidence = ReadoutCalibrationEvidence.model_validate(value).model_dump()
    documents = {item["id"]: item for item in evidence["documents"]}
    for identity in metadata["charge_reference"]["evidence_document_ids"]:
        if identity not in documents or documents[identity]["role"] != "charge_reference":
            raise ValueError(
                "Absolute charge anchor requires its original charge-reference evidence"
            )
        if any(
            documents[identity][key] != metadata["charge_reference"][key]
            for key in ("source_uri", "method_and_version")
        ):
            raise ValueError("Charge-reference evidence source/method differs from its anchor")
    return evidence
