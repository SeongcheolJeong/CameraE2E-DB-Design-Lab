"""Authored single-plane radiance mapped once into a finite-conjugate ideal image.

This is a nominal plane plus conditional PSF approximation, not a depth-resolved
renderer. Reciprocal object footprints are evidence, never a second blur pass.
"""

from __future__ import annotations

import io
import json
from copy import deepcopy
from typing import Literal

import numpy as np
from pydantic import Field

from .contracts import Contract
from .registry import canonical, digest

SCHEMA = "camera_finite_plane_fixture_v1"
METHOD = "authored_finite_plane_projection_v1"


class PlaneFixtureCreate(Contract):
    schema: Literal["camera_finite_plane_fixture_v1"] = SCHEMA
    assembly_id: str
    name: str = Field(default="유한 물체면 · 작성한 분광 차트", min_length=1, max_length=200)
    revision: str = Field(default="1", min_length=1, max_length=80)
    object_distance_mm: float = Field(ge=0.001, le=1e6)
    reference_magnification: float = Field(ge=-10, le=-1e-6)
    shape: list[int] = Field(default=[16, 16], min_length=2, max_length=2)
    pattern: Literal["flat", "slanted_edge", "disk"] = "slanted_edge"
    feature_center_xy_mm: list[float] = Field(default=[0, 0], min_length=2, max_length=2)
    disk_diameter_mm: float = Field(default=0.15, gt=0, le=1e6)
    radiance_w_m2_sr_nm: float = Field(default=0.1, gt=0, le=1e6)


def _lens(service, assembly):
    return next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "lens"
    )


def _evidence(service, lens):
    from . import factory
    from .finite_conjugate import validate_conjugate

    if not lens.get("psf_factory_job_id"):
        raise ValueError("Finite plane input requires a matched finite PSF Lens")
    factory.verify_psf_model_link(service, lens)
    psf_job = factory.get_job(service, lens["psf_factory_job_id"])
    if psf_job["request"].get("schema") != "prescription_field_psf_v5":
        raise ValueError("Finite plane input requires the explicit v5 finite optical model")
    conjugate = validate_conjugate(psf_job["request"]["prescription"]["object_conjugate"])
    radiometry = lens["payload"]["radiometry"]
    if (
        radiometry.get("object_conjugate") != conjugate
        or lens["properties"].get("object_conjugate") != conjugate
    ):
        raise ValueError("Finite plane source and optical response conjugates differ")
    return conjugate, np.asarray(radiometry["irradiance_per_radiance_sr"])[:, 0]


def template(service, assembly_id):
    from .imaging import _native

    assembly, _, _, full = _native(service, assembly_id)
    conjugate, _ = _evidence(service, _lens(service, assembly))
    return PlaneFixtureCreate(
        assembly_id=assembly_id,
        object_distance_mm=conjugate["distance_mm"],
        reference_magnification=conjugate["reference_magnification"],
        shape=[min(16, n // 2 * 2) for n in full],
    ).model_dump()


def _compute(request, sampling, wave, coefficient):
    """Pixel-center samples on a finite source window, zero outside that window."""
    from .finite_conjugate import object_xy_mm

    rows, cols = request["shape"]
    if any(type(n) is not int or n < 8 or n > 64 or n % 2 for n in (rows, cols)):
        raise ValueError("Finite plane fixture requires even sides in [8,64]")
    full, origin = sampling["full_sensor_shape"], sampling["crop_origin"]
    if any(o < 0 or o + n > f for o, n, f in zip(origin, (rows, cols), full, strict=True)):
        raise ValueError("Finite plane crop lies outside native sensor")
    center = np.asarray(request["feature_center_xy_mm"])
    if not np.isfinite(center).all() or np.max(np.abs(center)) > 1e6:
        raise ValueError("Finite plane feature center requires bounded finite coordinates")
    y, x = np.indices((rows, cols))
    sensor_xy = np.stack(
        (x + origin[1] - (full[1] - 1) / 2, y + origin[0] - (full[0] - 1) / 2), axis=-1
    )
    sensor_xy = sensor_xy * sampling["pixel_pitch_um"] / 1000
    # Same signed map at every wavelength; no per-wavelength recentering.
    conjugate = {
        "schema": "camera_finite_planar_object_v1",
        "distance_mm": request["object_distance_mm"],
        "reference": "first_surface_vertex",
        "orientation": "normal_toward_sensor",
        "support": "unbounded_uniform_lambertian_plane",
        "reference_magnification": request["reference_magnification"],
    }
    xy = object_xy_mm(sensor_xy, conjugate)
    delta = xy - center
    mask = np.ones((rows, cols))
    if request["pattern"] == "slanted_edge":
        mask = np.where(delta[..., 0] > 0.1 * delta[..., 1], 0.9, 0.03)
    elif request["pattern"] == "disk":
        mask = (np.linalg.norm(delta, axis=-1) <= request["disk_diameter_mm"] / 2).astype(float)
    radiance = np.broadcast_to(mask[..., None], (rows, cols, len(wave))).copy()
    radiance *= request["radiance_w_m2_sr_nm"]
    return xy, radiance, radiance * coefficient


def create(service, payload):
    from . import imaging
    from .finite_conjugate import application_conditions
    from .scene_camera import field_response_preflight

    imaging._current(service)
    request = PlaneFixtureCreate.model_validate(payload).model_dump()
    assembly, sensor, pitch, full = imaging._native(service, request["assembly_id"])
    lens = _lens(service, assembly)
    conjugate, coefficient = _evidence(service, lens)
    if (
        request["object_distance_mm"] != conjugate["distance_mm"]
        or request["reference_magnification"] != conjugate["reference_magnification"]
    ):
        raise ValueError("Finite plane distance/magnification must match its pinned optical model")
    if any(
        assembly["conditions"].get(k) != v for k, v in application_conditions(conjugate).items()
    ):
        raise ValueError("Assembly conditions differ from the finite plane conjugate")
    wave = sensor["payload"]["wavelength_nm"]
    if wave != lens["payload"]["wavelength_nm"]:
        raise ValueError("Finite plane projection requires exact Lens and sensor wavelengths")
    sampling = {
        "kind": "native_sensor",
        "full_sensor_shape": full,
        "crop_origin": [(f - n) // 2 for f, n in zip(full, request["shape"], strict=True)],
        "pixel_pitch_um": pitch,
    }
    xy, radiance, irradiance = _compute(request, sampling, wave, coefficient)
    issues = field_response_preflight(service, assembly, sampling, request["shape"])
    if issues:
        raise ValueError(
            "Finite plane response coverage: " + "; ".join(i["message"] for i in issues)
        )
    metadata = imaging.spectral_template(service, assembly["id"])
    metadata.update(
        name=request["name"],
        revision=request["revision"],
        sampling=sampling,
        source_uri="finite-plane://" + digest(request),
        method_and_version=METHOD,
        license="MIT; authored synthetic plane, optical model rights retained",
        parents=[lens["id"]],
    )
    asset = service.create_asset(
        {
            k: deepcopy(metadata[k])
            for k in (
                "name",
                "revision",
                "source_kind",
                "source_uri",
                "license",
                "method_and_version",
                "physical_instance",
                "parents",
            )
        }
        | {
            "kind": "scene",
            "description": "Authored single finite plane; nominal map plus PSF approximation",
        }
    )
    metadata["asset_id"] = asset["id"]
    evidence = {
        "schema": "camera_finite_plane_projection_evidence_v1",
        "request": request,
        "lens_model_id": lens["id"],
        "lens_model_sha256": digest(lens),
        "object_conjugate": conjugate,
        "radiance_unit": "W/m^2/sr/nm",
        "on_axis_irradiance_per_radiance_sr": coefficient.tolist(),
        "mapping": "object_xy_mm=ideal_sensor_xy_mm/reference_magnification",
        "source_sampling": "point_samples_of_authored_plane_at_native_pixel_centres",
        "source_support": "zero_radiance_outside_supplied_nominal_image_rectangle",
        "source_halo": "none; kernel tails outside the supplied window are lost",
        "uniform_plane_scope": "radiometry calibration only; flat fixture is windowed",
        "approximation": "nominal_plane_then_conditional_PSF_then_receiver_RI_and_angular_QE",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
    }
    artifacts = []
    for name, array in (
        ("object-plane-xy-mm", xy),
        ("spectral-radiance", radiance),
        ("spectral-image", irradiance),
    ):
        stream = io.BytesIO()
        np.save(stream, np.asarray(array, dtype="<f8"), allow_pickle=False)
        artifacts.append(
            service.registry.store_blob(
                stream.getvalue(), name + ".npy", "application/octet-stream"
            )
        )
    for name, value in (
        ("finite-plane-projection", evidence),
        ("spectral-image-metadata", metadata),
    ):
        artifacts.append(
            service.registry.store_blob(
                canonical(value).encode(), name + ".json", "application/json"
            )
        )
    return service.registry.put(
        "spectral_images",
        {
            **metadata,
            "shape": list(irradiance.shape),
            "artifacts": artifacts,
            "asset_snapshot": asset,
            "projection_lens_model_id": lens["id"],
            "finite_plane_projection": evidence,
            "approval_status": "draft",
            "physical_validation": "not_established",
        },
    )


def verify_scene(service, record, lens):
    """Reject relabeled depths and verify original plane samples before any runtime."""
    conjugate, coefficient = _evidence(service, lens)
    evidence = record.get("finite_plane_projection")
    if not evidence or evidence.get("object_conjugate") != conjugate:
        raise ValueError("Finite Lens requires a verified conjugate-bound finite plane input")
    if (
        evidence.get("lens_model_id") != lens["id"]
        or evidence.get("lens_model_sha256") != digest(lens)
        or record.get("projection_lens_model_id") != lens["id"]
    ):
        raise ValueError("Finite plane must preserve its exact optical model")

    def content(name):
        found = [a for a in record["artifacts"] if a["name"] == name]
        if len(found) != 1:
            raise ValueError("Finite plane requires unique original evidence: " + name)
        return service.registry.blob_path(found[0]["sha256"]).read_bytes()

    if json.loads(content("finite-plane-projection.json")) != evidence:
        raise ValueError("Finite plane declaration differs from its original bytes")
    metadata = json.loads(content("spectral-image-metadata.json"))
    if any(record.get(k) != v for k, v in metadata.items()):
        raise ValueError("Finite plane metadata differs from its original declaration")
    request = PlaneFixtureCreate.model_validate(evidence["request"]).model_dump()
    if (
        request["object_distance_mm"] != conjugate["distance_mm"]
        or request["reference_magnification"] != conjugate["reference_magnification"]
    ):
        raise ValueError("Finite plane input depth or magnification differs from optical model")
    expected = _compute(request, record["sampling"], record["wavelength_nm"], coefficient)
    for name, array in zip(
        ("object-plane-xy-mm", "spectral-radiance", "spectral-image"), expected, strict=True
    ):
        if not np.array_equal(
            np.load(io.BytesIO(content(name + ".npy")), allow_pickle=False), array
        ):
            raise ValueError("Finite plane numerical evidence differs from its original projection")
