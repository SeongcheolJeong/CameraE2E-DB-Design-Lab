"""Public JSON contracts. Array payloads carry explicit axes and physical meaning."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceKind = Literal["measured", "simulated", "vendor", "estimated"]

RESPONSE_OBSERVABLE_UNITS = {
    "green_mean_normalized": "1", "wb_adjusted_rg_ratio": "1", "wb_adjusted_bg_ratio": "1",
    "exposure_ns": "ns", "gain": "1", "wb_red_gain": "1", "wb_blue_gain": "1",
    "ae_command_latency_ns": "ns", "awb_command_latency_ns": "ns",
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def finite_json(self):
        # Also reject NaN nested inside plugin-specific payload dictionaries.
        json.dumps(self.model_dump(), allow_nan=False)
        return self


class Provenance(Contract):
    source_kind: SourceKind
    source_uri: str = Field(min_length=1, max_length=2000)
    license: str = Field(min_length=1, max_length=500)
    method_and_version: str = Field(min_length=1, max_length=500)
    physical_instance: str | None = None
    parents: list[str] = Field(default_factory=list, max_length=100)


class AssetCreate(Provenance):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["lens", "sensor", "isp", "scene", "material", "light", "measurement"]
    revision: str = Field(default="1", min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    properties: dict[str, Any] = Field(default_factory=dict)


class ParameterUncertainty(Contract):
    """A supplied deterministic input bound; never interpret sigma as a bound."""

    kind: Literal["absolute_bound", "unknown"]
    unit: str = Field(min_length=1, max_length=80)
    value: Any = None
    source_kind: SourceKind
    source_uri: str = Field(min_length=1, max_length=2000)
    method_and_version: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def bound_or_unknown(self):
        if self.kind == "absolute_bound" and self.value is None:
            raise ValueError("An absolute input bound requires value")
        if self.kind == "unknown" and (self.value is not None or not self.reason):
            raise ValueError("Unknown input uncertainty needs a reason and no numeric value")
        return self


class ModelCreate(Provenance):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    asset_id: str
    kind: Literal["lens", "sensor", "isp"]
    reference_plane: str = Field(min_length=1)
    included_effects: list[str]
    valid_domain: dict[str, Any]
    spectral_range: dict[str, Any]
    properties: dict[str, Any] = Field(default_factory=dict)
    raw_format: dict[str, Any] = Field(default_factory=dict)
    input_raw_format: dict[str, Any] = Field(default_factory=dict)
    uncertainty: dict[str, Any]
    parameter_uncertainty: dict[str, ParameterUncertainty] = Field(default_factory=dict)
    calibration_dataset_ids: list[str]
    payload: dict[str, Any]
    factory_job_id: str | None = None
    factory_application_conditions: dict[str, Any] | None = None
    psf_factory_job_id: str | None = None
    factory_condition_scope: Literal["all_declared", "optical_response"] = "all_declared"


class AssemblyCreate(Contract):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    model_ids: list[str] = Field(min_length=3, max_length=3)
    conditions: dict[str, Any]
    spectral_range: dict[str, Any]
    raw_format: dict[str, Any]
    description: str = Field(default="", max_length=4000)


class TestCreate(Contract):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    validation_kind: Literal[
        "implementation_verification", "cross_tool_comparison", "physical_validation"
    ] = "physical_validation"
    metric_id: str
    unit: str
    reference_plane: str
    included_effects: list[str]
    conditions: dict[str, Any]
    required_conditions: list[str]
    absolute_tolerance: dict[str, Any]
    uncertainty_policy: Literal["bounded_interval"] = "bounded_interval"
    calibration_dataset_ids: list[str] = Field(default_factory=list)
    stimulus: dict[str, Any]
    parameter_uncertainty: dict[str, ParameterUncertainty] = Field(default_factory=dict)
    parent_test_id: str | None = None
    observation_method: dict[str, Any] | None = None
    observation_specification: dict[str, Any] | None = None
    observation_shape: list[int] | None = None
    clock: dict[str, Any] | None = None
    frame_conditions: list[dict[str, Any]] | None = None


class MeasurementCreate(Provenance):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    test_id: str
    assembly_id: str
    metric_id: str
    unit: str
    reference_plane: str
    included_effects: list[str]
    values: list[float] = Field(max_length=100_000)
    axes: list[dict[str, Any]]
    conditions: dict[str, Any]
    valid_domain: dict[str, Any] = Field(default_factory=dict)
    uncertainty: dict[str, Any]
    dataset_ids: list[str] = Field(min_length=1, max_length=100)
    calibration_dataset_ids: list[str] = Field(default_factory=list)
    instrument: dict[str, Any] = Field(default_factory=dict)
    observation_method: dict[str, Any] | None = None
    sample_geometry: dict[str, Any] | None = None
    channel_means_dn: list[list[float]] | None = None
    raw_capture_id: str | None = None
    capture_analysis_id: str | None = None
    clock: dict[str, Any] | None = None
    frame_conditions: list[dict[str, Any]] | None = None
    response_observation: dict[str, Any] | None = None

    @model_validator(mode="after")
    def measured_provenance(self):
        if self.capture_analysis_id and not self.raw_capture_id:
            raise ValueError("Capture analysis requires its original RAW capture")
        if not self.values and self.response_observation is None:
            raise ValueError(
                "An empty measurement requires an explicit missing response observation"
            )
        if self.source_kind == "measured":
            if not self.physical_instance:
                raise ValueError("Measured data requires a physical sample/serial identity")
            if not self.instrument.get("id") or not self.instrument.get("calibration_revision"):
                raise ValueError("Measured data requires instrument ID and calibration revision")
        return self


class RunCreate(Contract):
    assembly_id: str
    test_id: str
    plugin_id: Literal[
        "response_db",
        "quick_estimate",
        "response_db_v1",
        "quick_estimate_v1",
        "spatial_scene_v1",
        "temporal_scene_v1",
        "closed_loop_scene_v1",
        "hdr_bracket_v1",
        "reference_hdr_v1",
        "shared_charge_dcg_v1",
        "retained_charge_v1",
        "electrical_control_v1",
    ] = "response_db"
    scene_id: str | None = None
    scene_sequence_id: str | None = None
    control_sequence_id: str | None = None
    control_program_id: str | None = None
    capture_program_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    force: bool = False


class CaptureProgramCreate(Provenance):
    """Explicit electrical/illumination/HDR assumptions for one fixed assembly."""

    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    assembly_id: str
    payload: dict[str, Any]


class FactoryJobCreate(Provenance):
    name: str = Field(min_length=1, max_length=200)
    asset_id: str
    plugin_id: Literal[
        "rayoptics_lens_v1", "meep_planar_stack_v1", "prysm_psf_v1", "prescription_psf_v1"
    ]
    request: dict[str, Any]
    force: bool = False


class FactoryModelCreate(Contract):
    job_id: str
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=80)
    conditions: dict[str, Any]
    base_lens_model_id: str | None = None
    condition_scope: Literal["all_declared", "optical_response"] = "all_declared"


class SpectralImageImport(Provenance):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    asset_id: str | None = None
    wavelength_nm: list[float]
    unit: Literal["W/m^2/nm"]
    reference_plane: Literal["sensor_ideal_image_before_psf_ri"]
    sampling: dict[str, Any]
    included_effects: list[str]
    conditions: dict[str, Any]


class SpectralFixtureCreate(Contract):
    assembly_id: str
    name: str = Field(default="합성 분광 차트 · 명시적 이상 영상", min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    pattern: Literal["spectral_chart", "slanted_edge", "point", "flat"] = "spectral_chart"
    shape: list[int] = Field(default=[64, 64], min_length=2, max_length=2)
    irradiance_scale: float = Field(default=0.0002, gt=0, le=10)


class ReplayCreate(Contract):
    name: str = Field(min_length=1, max_length=200)
    source_kind: Literal["capture", "run"]
    source_id: str
    isp_model_id: str
    force: bool = False


class SceneSequenceCreate(Provenance):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    payload: dict[str, Any]
    fixture_specification: dict[str, Any] | None = None


class ControlSequenceCreate(SceneSequenceCreate):
    assembly_id: str
    scene_sequence_id: str


class TemporalFixtureCreate(Contract):
    assembly_id: str
    name: str = Field(default="합성 이동 광점 · 시간 분광 영상", min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    pattern: Literal["moving_spot", "flicker", "static"] = "moving_spot"
    shape: list[int] = Field(default=[64, 64], min_length=2, max_length=2)
    knot_count: int = Field(default=65, ge=2, le=128)
    duration_ns: int = Field(default=80_000_000, ge=1_000_000, le=1_000_000_000)
    irradiance_scale: float = Field(default=0.0003, gt=0, le=10)
    frequency_hz: float = Field(default=100, gt=0, le=10000)
    phase_rad: float = Field(default=0, ge=-6.2831853072, le=6.2831853072)


class ValidationCreate(Contract):
    run_id: str
    measurement_id: str
    test_id: str
    allow_calibrated_model: bool = False


class MeasurementImport(Contract):
    document_text: str = Field(min_length=1, max_length=2_000_000)


class CaptureImport(Provenance):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    test_id: str
    assembly_id: str
    dataset_ids: list[str] = Field(min_length=1, max_length=100)
    instrument: dict[str, Any] = Field(default_factory=dict)
    observation: dict[str, Any]

    @model_validator(mode="after")
    def acquisition_identity(self):
        if len(self.dataset_ids) != len(set(self.dataset_ids)):
            raise ValueError("Capture dataset identities must be unique")
        if self.source_kind == "measured" and (
            not self.physical_instance
            or not self.instrument.get("id")
            or not self.instrument.get("calibration_revision")
        ):
            raise ValueError(
                "Measured RAW requires a sample/serial, instrument and calibration revision"
            )
        return self


class CalibrationCreate(Contract):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="calibrated-1", min_length=1, max_length=80)
    base_sensor_model_id: str
    measurement_ids: list[str] = Field(min_length=1, max_length=32)
    parameter_bounds: dict[str, Any]


class ReleaseCreate(Contract):
    name: str = Field(min_length=1, max_length=200)
    run_ids: list[str] = Field(min_length=1, max_length=30)
    validation_ids: list[str] = Field(default_factory=list, max_length=100)


class ExperimentCreate(Contract):
    name: str = Field(min_length=1, max_length=200)
    assembly_ids: list[str] = Field(min_length=1, max_length=12)
    test_id: str
    exposure_ms: list[float] = Field(min_length=1, max_length=8)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)


PROPERTY_DICTIONARY = [
    {
        "id": "isp.temporal_signal_change_error", "unit": "1",
        "plane": "isp_fixed_color_code_rgb",
        "axes": ["frame", "native_row", "native_column", "RGB"],
        "description": "Reference-backed sampled time-change error of retained-code ISP "
        "fixed-color pre-tone RGB on one frozen native ROI. Authored independent truth "
        "is not authenticated physical camera validation.",
    },
    {
        "id": "raw.electrical_control_delivered_channel_mean_dn_sequence", "unit": "DN",
        "plane": "sensor_raw", "axes": ["frame", "readout", "channel"],
        "description": "Signed black-relative delivered RAW channel means conditional on "
        "causally generated exposures, retained photodiode state and declared command timing; "
        "no current-exposure inversion or generic physical comparison operator.",
    },
    {
        "id": "raw.retained_delivered_channel_mean_dn_sequence", "unit": "DN",
        "plane": "sensor_raw", "axes": ["frame", "readout", "channel"],
        "description": "Black-relative delivered total-charge RAW means with explicit residual "
        "history, row transfer/clear events and fresh read nodes; no current-exposure inversion.",
    },
    {
        "id": "raw.channel_mean_dn_shared_charge", "unit": "DN", "plane": "sensor_raw",
        "axes": ["acquisition", "readout", "channel"],
        "description": "Black and mean-dark corrected native CFA means for paired HCG/LCG "
        "readouts of one shared charge per acquisition; no independent-exposure flattening.",
    },
    *[
        {"id": f"control.response.{observable}.{reduction}",
         "unit": "ns" if reduction == "sampled_settling_time" else unit,
         "plane": "sensor_raw", "axes": ["response_sample"],
         "description": "Observed RAW/control-log response with frozen native sampling, "
                        "target/window and explicit missing data; not general color accuracy."}
        for observable, unit in RESPONSE_OBSERVABLE_UNITS.items()
        for reduction in ("trajectory", "peak_absolute_target_error", "sampled_settling_time")
    ],
    {
        "id": "raw.channel_mean_dn_sequence",
        "unit": "DN",
        "plane": "sensor_raw",
        "axes": ["frame", "channel"],
        "description": "Ordered native CFA channel means with explicit relative clock "
        "and applied frame settings; scene variation is not temporal noise.",
    },
    {
        "id": "lens.psf",
        "unit": "1",
        "plane": "sensor_surface",
        "axes": ["x_um", "y_um", "wavelength_nm"],
        "description": "2D intensity PSF; MTF magnitude alone is not a unique PSF.",
    },
    {
        "id": "lens.field_psf",
        "unit": "1",
        "plane": "sensor_surface",
        "axes": ["wavelength_nm", "field_y_mm", "field_x_mm", "offset_y_um", "offset_x_um"],
        "description": "Unit-sum pixel-integrated intensity PSFs at ideal source positions. "
        "Field coordinates use native sensor center; bilinear source-position interpolation "
        "preserves full spectral flux. No field extrapolation or physical accuracy claim.",
    },
    {
        "id": "lens.mtf",
        "unit": "1",
        "plane": "sensor_surface",
        "axes": ["frequency_lp_mm", "wavelength_nm", "field_radius"],
        "description": "Optical MTF, distinct from sampled RAW or processed SFR.",
    },
    {
        "id": "lens.relative_illumination",
        "unit": "1",
        "plane": "sensor_surface",
        "axes": ["field_radius"],
        "description": "Relative illumination, not refractive index.",
    },
    {
        "id": "material.refractive_index",
        "unit": "1",
        "plane": "material",
        "axes": ["wavelength_nm"],
        "description": "Complex index n and extinction k stored separately.",
    },
    {
        "id": "sensor.eqe",
        "unit": "electron/photon",
        "plane": "sensor_surface",
        "axes": ["channel", "wavelength_nm", "theta_deg"],
        "description": "Collected-electron QE; not optical absorption alone.",
    },
    {
        "id": "sensor.read_noise",
        "unit": "electron",
        "plane": "sensor_raw",
        "axes": ["temperature_c", "gain"],
        "description": "Temporal RMS read noise, not spatial image standard deviation.",
    },
    {
        "id": "raw.channel_ratio",
        "unit": "1",
        "plane": "sensor_raw",
        "axes": ["field_radius", "channel_ratio"],
        "description": "Expected pre-ISP R/G and B/G; test conditions and axis order fixed.",
    },
    {
        "id": "raw.channel_mean_dn",
        "unit": "DN",
        "plane": "sensor_raw",
        "axes": ["channel"],
        "description": "Native crop CFA-channel mean of continuous clipped RAW "
        "after matched dark and black subtraction.",
    },
    {
        "id": "image.sfr",
        "unit": "1",
        "plane": "isp_output",
        "axes": ["frequency_cy_pixel"],
        "description": "Processed SFR includes sampling and nonlinear ISP effects.",
    },
]
