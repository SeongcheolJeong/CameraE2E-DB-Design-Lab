"""Reusable physical scene assets and an explicit ideal-projection boundary."""

from __future__ import annotations

from typing import Annotated, Literal

import numpy as np
from pydantic import Field, model_validator

from .contracts import Contract, Provenance


class Spectrum(Contract):
    wavelength_nm: list[float] = Field(min_length=2, max_length=16)
    values: list[float] = Field(min_length=2, max_length=16)

    @model_validator(mode="after")
    def axis(self):
        w = np.asarray(self.wavelength_nm)
        if (
            len(w) != len(self.values)
            or not np.all(np.diff(w) > 0)
            or w[0] < 360
            or w[-1] > 830
            or min(self.values) < 0
        ):
            raise ValueError("Declare increasing [360,830] nm samples with nonnegative values")
        return self


class Geometry(Contract):
    kind: Literal["geometry"]
    unit: Literal["m"]
    primitive: Literal["sphere", "rectangle", "cube", "triangle_mesh"]
    vertices: list[list[float]] = Field(default_factory=list, max_length=20000)
    faces: list[list[int]] = Field(default_factory=list, max_length=40000)

    @model_validator(mode="after")
    def mesh(self):
        if self.primitive != "triangle_mesh":
            if self.vertices or self.faces:
                raise ValueError("Primitive geometry cannot silently ignore mesh arrays")
            return self
        v, f = np.asarray(self.vertices), np.asarray(self.faces)
        if (
            v.ndim != 2
            or v.shape[1] != 3
            or len(v) < 3
            or np.max(np.abs(v)) > 1e6
            or f.ndim != 2
            or f.shape[1] != 3
            or len(f) < 1
            or f.min() < 0
            or f.max() >= len(v)
        ):
            raise ValueError("Mesh requires bounded metre vertices and valid triangle indices")
        area = np.linalg.norm(np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]), axis=1)
        if np.any(area < 1e-16):
            raise ValueError("Degenerate mesh triangles are unsupported")
        return self


class Material(Contract):
    kind: Literal["material"]
    bsdf: Literal["diffuse", "roughconductor"]
    spectrum: Spectrum
    extinction_k: Spectrum | None = None
    roughness: float | None = Field(default=None, ge=0.01, le=1)
    surface_state: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def semantics(self):
        if self.bsdf == "diffuse":
            if (
                max(self.spectrum.values) > 1
                or self.extinction_k is not None
                or self.roughness is not None
            ):
                raise ValueError(
                    "Diffuse material needs reflectance in [0,1], without conductor fields"
                )
        elif (
            min(self.spectrum.values) <= 0
            or self.extinction_k is None
            or self.roughness is None
            or self.extinction_k.wavelength_nm != self.spectrum.wavelength_nm
        ):
            raise ValueError("Conductor needs positive eta, matching k samples and GGX roughness")
        return self


class Light(Contract):
    kind: Literal["light"]
    distribution: Literal["constant_environment", "point", "area"]
    spectrum: Spectrum
    unit: Literal["W/m^2/sr/nm", "W/sr/nm"]
    time_model: Literal["static"]

    @model_validator(mode="after")
    def units(self):
        expected = "W/sr/nm" if self.distribution == "point" else "W/m^2/sr/nm"
        if self.unit != expected:
            raise ValueError("Point intensity and environment/area radiance have different units")
        return self


IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


class SceneClock(Contract):
    """The same relative nanosecond coordinate used by registered exposure timing."""

    clock_id: str = Field(min_length=1, max_length=200)
    unit: Literal["ns"]
    reference: Literal["relative"]
    origin: str = Field(min_length=1, max_length=2000)


class LinearTranslation(Contract):
    """World-frame metres per second from the declared zero-time pose."""

    model: Literal["linear_translation_v1"]
    velocity_m_s: list[float] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def bounded_velocity(self):
        velocity = np.asarray(self.velocity_m_s)
        if (not np.isfinite(velocity).all() or not np.any(velocity)
                or np.max(np.abs(velocity)) > 1000):
            raise ValueError("Linear translation needs nonzero, bounded m/s velocity")
        return self


class Instance(Contract):
    geometry_id: str
    material_id: str
    area_light_id: str | None = None
    to_world: list[list[float]] = Field(default_factory=lambda: [r[:] for r in IDENTITY])
    motion: LinearTranslation | None = None

    @model_validator(mode="after")
    def transform(self):
        t = np.asarray(self.to_world)
        if (t.shape != (4, 4) or not np.isfinite(t).all()
                or not np.array_equal(t[3], [0, 0, 0, 1])
                or np.max(np.abs(t)) > 1e6):
            raise ValueError("to_world requires a bounded affine 4x4 transform")
        sv = np.linalg.svd(t[:3, :3], compute_uv=False)
        if sv.min() < 1e-6 or sv.max() > 1e6 or np.linalg.det(t[:3, :3]) <= 0:
            raise ValueError("Transforms must be nonsingular and preserve orientation")
        return self


class PointLight(Contract):
    light_id: str
    position_m: list[float] = Field(min_length=3, max_length=3)


class Scene(Contract):
    kind: Literal["scene"]
    coordinate_system: Literal["right_handed_y_up_metres"]
    instances: list[Instance] = Field(default_factory=list, max_length=64)
    environment_light_id: str | None = None
    point_lights: list[PointLight] = Field(default_factory=list, max_length=16)
    clock: SceneClock | None = None

    @model_validator(mode="after")
    def motion_clock(self):
        if any(instance.motion is not None for instance in self.instances) and self.clock is None:
            raise ValueError("Moving 3D instances require an explicit scene clock")
        return self


class SceneAssetCreate(Provenance):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    payload: Annotated[Geometry | Material | Light | Scene, Field(discriminator="kind")]


class Pose(Contract):
    origin_m: list[float] = Field(min_length=3, max_length=3)
    target_m: list[float] = Field(min_length=3, max_length=3)
    up: list[float] = Field(default=[0.0, 1.0, 0.0], min_length=3, max_length=3)
    motion: LinearTranslation | None = None

    @model_validator(mode="after")
    def basis(self):
        direction = np.asarray(self.target_m) - self.origin_m
        if (
            not np.isfinite(np.asarray([self.origin_m, self.target_m, self.up])).all()
            or np.linalg.norm(direction) < 1e-9
            or np.linalg.norm(np.cross(direction, self.up)) < 1e-9
            or np.max(np.abs([self.origin_m, self.target_m])) > 1e6
        ):
            raise ValueError("Camera pose needs distinct positions and a nonparallel up vector")
        return self


class Projection(Provenance):
    projection_model: Literal["paraxial_on_axis_projection_v1"]
    focal_length_mm: float = Field(gt=0, le=10000)
    f_number: float = Field(ge=0.5, le=128)
    on_axis_transmittance: Spectrum
    geometry_model: Literal["pinhole_pixel_centre"]
    irradiance_model: Literal["pi_L_T_over_4N2_before_relative_illumination"]
    approximation_uncertainty: Literal["unknown"]

    @model_validator(mode="after")
    def throughput(self):
        if max(self.on_axis_transmittance.values) > 1:
            raise ValueError("Lens transmittance must lie in [0,1]")
        return self


class PrescriptionProjection(Provenance):
    """Frozen absolute geometric E/L; normalized RI and PSF remain downstream."""

    projection_model: Literal["prescription_radiometry_projection_v1"]
    focal_length_mm: float = Field(gt=0, le=10000)
    f_number: float = Field(ge=0.5, le=128)
    on_axis_irradiance_per_radiance_sr: Spectrum
    lens_model_id: str = Field(min_length=1)
    lens_model_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    factory_job_id: str = Field(min_length=1)
    factory_job_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    psf_factory_job_id: str = Field(min_length=1)
    psf_factory_job_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stop_transmission_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    geometry_model: Literal["pinhole_pixel_centre"]
    irradiance_model: Literal[
        "L_times_verified_on_axis_irradiance_per_radiance_before_relative_illumination"
    ]
    approximation_uncertainty: Literal["unknown"]

    @model_validator(mode="after")
    def positive_radiometry(self):
        if min(self.on_axis_irradiance_per_radiance_sr.values) <= 0:
            raise ValueError("Verified absolute E/L requires positive values at every wavelength")
        return self


# Keep Projection import compatibility and its exact historical serialization.
ProjectionSpec = Annotated[
    Projection | PrescriptionProjection, Field(discriminator="projection_model")
]


class SourcePixelQuadrature(Contract):
    """Optional source-area sampling, with explicit retained-subcell opt-in."""

    model: Literal[
        "native_sensor_area_midpoint_grid_v1",
        "native_sensor_area_midpoint_retained_subcells_v1",
    ]
    grid_side: Literal[2, 4, 8, 16]


class SceneRenderCreate(Contract):
    name: str = Field(min_length=1, max_length=200)
    scene_asset_id: str
    assembly_id: str
    light_component_id: str | None = Field(default=None, min_length=1)
    pose: Pose
    projection: ProjectionSpec
    shape: list[int] = Field(default=[32, 32], min_length=2, max_length=2)
    crop_origin: list[int] | None = Field(default=None, min_length=2, max_length=2)
    render_psf_halo: bool = False
    render_charge_collection_halo: bool = False
    samples_per_pixel: int = Field(default=16, ge=2, le=256)
    source_pixel_quadrature: SourcePixelQuadrature | None = None
    max_depth: int = Field(default=8, ge=2, le=32)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    render_clock: SceneClock | None = None
    render_time_ns: int | None = Field(default=None, ge=-(2**53 - 1), le=2**53 - 1)
    force: bool = False

    @model_validator(mode="after")
    def resources(self):
        if (self.render_clock is None) != (self.render_time_ns is None):
            raise ValueError("Timed rendering requires both render_clock and render_time_ns")
        if self.pose.motion is not None and self.render_time_ns is None:
            raise ValueError("Camera translation requires an explicit render time")
        if any(n < 8 or n > 256 or n % 2 for n in self.shape):
            raise ValueError("Native crops require even 8..256 sides")
        # Multiplied by the actual wavelength count again at submission.
        grid_side = self.source_pixel_quadrature.grid_side if self.source_pixel_quadrature else 1
        if np.prod(self.shape) * self.samples_per_pixel * grid_side**2 > 262144:
            raise ValueError("Scalar rendering exceeds the bounded primary-path budget")
        return self


class RenderPublish(Contract):
    job_id: str
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
