"""Prepare a scene camera from immutable models without inventing optical data."""

import math
from copy import deepcopy

import numpy as np
from pydantic import Field

from . import imaging
from .composer import check_assembly
from .contracts import AssemblyCreate, Contract
from .registry import ConflictError, digest
from .scene_render import _asset, dependencies
from .service import PreflightError
from .spatial import SpatialSceneRuntime


class SceneCameraDraft(Contract):
    scene_asset_id: str
    source_assembly_id: str
    assembly: AssemblyCreate


class SceneCameraSave(Contract):
    draft: SceneCameraDraft
    preview_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _response_preflight(assembly, models, sampling, *, shape=None, crop_origin=None,
                        capture_crop=None):
    """Check the real RAW path without rendering, storing or allocating a full frame.

    Zero irradiance tests model interfaces only. Actual scene charge and render
    workload still require the downstream preflight after radiance is available.
    """
    sensor = models["sensor"]["payload"]
    conditions, raw = assembly["conditions"], assembly["raw_format"]
    for key, unit in {"exposure_s": "s", "temperature_c": "degC", "gain": "1"}.items():
        quantity = conditions.get(key)
        if not isinstance(quantity, dict) or quantity.get("unit") != unit:
            raise ValueError(f"{key} must use {unit}")
    if conditions["gain"].get("value") != sensor["readout"]["gain"]:
        raise ValueError("Fixed sensor gain differs from camera conditions")
    # These are the readout bindings enforced by prepare_spatial and the ISP.
    expected_raw = {
        "bit_depth": sensor["readout"]["bit_depth"],
        "black_level": sensor["readout"]["black_level_dn"],
        "cfa": "RGGB",
        "layout": "bayer",
        "packing": "unpacked",
        "hdr_streams": 1,
    }
    if any(raw.get(key) != value for key, value in expected_raw.items()):
        raise ValueError("Sensor readout differs from declared or supported RAW format")
    lens = deepcopy(models["lens"]["payload"])
    psf = lens.pop("psf", {})
    wave = sensor["wavelength_nm"]
    shape = list(shape or [8, 8])
    if crop_origin is None:
        crop_origin = [0, 0]
        if psf.get("spatial_model") == "field_grid":
            # Preparation has no requested crop yet. Select an explicit small
            # covered patch, not a corner outside the declared field domain.
            # Render/projection preflight checks the actual crop separately.
            from .spatial import validate_psf

            psf = validate_psf(psf)
            pitch_mm = sampling["pixel_pitch_um"] / 1000
            crop_origin = []
            for axis, full, side in zip(
                (psf["field_y_mm"], psf["field_x_mm"]),
                sampling["full_sensor_shape"],
                shape,
                strict=True,
            ):
                first = max(0, math.ceil(axis[0] / pitch_mm + (full - 1) / 2))
                last = min(full - 1, math.floor(axis[-1] / pitch_mm + (full - 1) / 2))

                # Use the runtime's forward expression to settle floating-point
                # inverse rounding at exact native pixel centers. Never widen
                # the physical domain with an extrapolation tolerance.
                def position(index, native=full):
                    return (index - (native - 1) / 2) * sampling["pixel_pitch_um"] / 1000

                if first > 0 and position(first - 1) >= axis[0]:
                    first -= 1
                if first < full and position(first) < axis[0]:
                    first += 1
                if last < full - 1 and position(last + 1) <= axis[-1]:
                    last += 1
                if last >= 0 and position(last) > axis[-1]:
                    last -= 1
                if last - first + 1 < side:
                    raise ValueError("Field PSF has no covered 8 by 8 native camera crop")
                crop_origin.append((first + last + 1 - side) // 2)
    from .runtime import sensor_response_fields

    response = sensor_response_fields(sensor)
    if "charge_collection" in response:
        from .scene_render import _carrier_generation_crop, _halo_source_crop

        # A bounded zero-input probe still needs the real PSF and carrier
        # support. It may not silently replace the collection model or allocate
        # an entire large sensor just to inspect camera readiness.
        if capture_crop is None:
            capture_crop = {"crop_origin": list(crop_origin), "shape": list(shape)}
            generation_crop = _carrier_generation_crop(
                sampling["full_sensor_shape"], capture_crop["crop_origin"],
                capture_crop["shape"], response["charge_collection"]["destination_offsets_rc"],
            )
            psf_shape = list(np.shape(psf["values"])[-2:])
            crop_origin, shape = _halo_source_crop(
                sampling["full_sensor_shape"], capture_crop["crop_origin"],
                capture_crop["shape"], psf_shape, generation_crop,
            )
    request = {
        "wavelength_nm": sensor["wavelength_nm"],
        "pixel_pitch_um": sensor["pixel_pitch_um"],
        "readout": sensor["readout"],
        **response,
        "exposure_s": conditions["exposure_s"].get("value"),
        "temperature_c": conditions["temperature_c"].get("value"),
        "lens": lens,
        "psf": psf,
        "isp_profile": imaging.resolve_isp(models["isp"]),
        "frames": 1,
        "seed": 0,
        "noise_enabled": False,
        "scene": {
            "schema": "spectral_ideal_image_v1",
            "wavelength_nm": wave,
            "unit": "W/m^2/nm",
            "reference_plane": "sensor_ideal_image_before_psf_ri",
            "included_effects": ["on_axis_lens_throughput"],
            "spectral_irradiance": np.zeros((*shape, len(wave))),
            "sampling": {
                "kind": "native_sensor",
                "full_sensor_shape": sampling["full_sensor_shape"],
                "crop_origin": crop_origin,
                "pixel_pitch_um": sampling["pixel_pitch_um"],
                **({"capture_crop": capture_crop} if capture_crop is not None else {}),
            },
            "source": {
                "kind": "estimated",
                "description": "Zero-irradiance interface preflight only; not scene evidence",
            },
        },
    }
    return SpatialSceneRuntime().validate_request(request)


def field_response_preflight(service, assembly, sampling, shape, *, capture_crop=None):
    """Check an actual render/reuse crop before spending compute or storing inputs."""
    models = {
        model["kind"]: model
        for model in (service.registry.get("models", i) for i in assembly["model_ids"])
    }
    if (models["lens"]["payload"].get("psf", {}).get("spatial_model") != "field_grid"
            and "charge_collection" not in models["sensor"]["payload"]):
        return []
    return _response_preflight(
        assembly, models, sampling, shape=shape, crop_origin=sampling["crop_origin"],
        capture_crop=capture_crop,
    )


def template(service, scene_id, assembly_id):
    scene = _asset(service, scene_id, "scene")
    source = service.registry.get("assemblies", assembly_id)
    assembly = {k: deepcopy(source[k]) for k in AssemblyCreate.model_fields}
    assembly.update(name=(source["name"][:160] + " · 장면 구성"), revision="scene-1")
    assembly["conditions"]["light_id"] = {"value": scene["illumination_id"], "unit": "1"}
    lens = next(
        service.registry.get("models", i)
        for i in source["model_ids"]
        if service.registry.get("models", i)["kind"] == "lens"
    )
    if "f_number" in lens["properties"]:
        assembly["conditions"]["f_number"] = deepcopy(lens["properties"]["f_number"])
    return SceneCameraDraft(
        scene_asset_id=scene_id, source_assembly_id=assembly_id, assembly=assembly
    ).model_dump()


def preview(service, payload):
    environment = imaging._current(service)
    data = SceneCameraDraft.model_validate(payload).model_dump()
    scene = _asset(service, data["scene_asset_id"], "scene")
    related = dependencies(service, scene["payload"])
    if related != scene["dependency_snapshots"]:
        raise ValueError("Scene dependencies differ from their frozen snapshots")
    source = service.registry.get("assemblies", data["source_assembly_id"])
    assembly = data["assembly"]
    models = [service.registry.get("models", i) for i in assembly["model_ids"]]
    if sorted(m["kind"] for m in models) != ["isp", "lens", "sensor"]:
        raise ValueError("Select exactly one Lens, Sensor and ISP revision")
    by_kind = {m["kind"]: m for m in models}
    lens, sensor = by_kind["lens"], by_kind["sensor"]
    compatibility = check_assembly(assembly, models)
    issues = deepcopy(compatibility["issues"])

    def issue(code, message, path):
        issues.append({"code": code, "message": message, "path": path, "severity": "error"})

    if assembly["conditions"].get("light_id") != {"value": scene["illumination_id"], "unit": "1"}:
        issue(
            "scene_light_mismatch",
            "촬영 light_id는 선택 장면의 조명 버전과 같아야 합니다.",
            "conditions.light_id",
        )
    optics = {}
    for prop, unit, low, high in [("focal_length", "mm", 0, 10000), ("f_number", "1", 0.5, 128)]:
        q = lens["properties"].get(prop, {})
        v = q.get("value") if isinstance(q, dict) else None
        valid = (
            type(v) in (int, float)
            and math.isfinite(v)
            and v > 0
            and low <= v <= high
            and q.get("unit") == unit
        )
        optics[prop] = deepcopy(q) if valid else None
        if not valid:
            issue(
                "missing_lens_" + prop,
                f"Lens의 {prop}를 {unit} 단위로 선언한 모델 버전이 필요합니다.",
                "models." + lens["id"],
            )
    if optics["f_number"] and assembly["conditions"].get("f_number") != optics["f_number"]:
        issue(
            "lens_aperture_mismatch",
            "촬영 조리개는 고정 Lens의 F-number와 같아야 합니다.",
            "conditions.f_number",
        )
    sampling = None
    wave = sensor["payload"]["wavelength_nm"]
    try:
        if not 2 <= len(wave) <= 16 or wave[0] < 360 or wave[-1] > 830:
            raise ValueError("Scene rendering supports 2..16 wavelength samples in 360..830 nm")
        pitch, shape = imaging.native_sensor_grid(sensor)
        if any(n < 8 for n in shape):
            raise ValueError("Native sensor must support at least an 8 by 8 crop")
        sampling = {"pixel_pitch_um": pitch, "full_sensor_shape": shape, "wavelength_nm": wave}
        for problem in _response_preflight(assembly, by_kind, sampling):
            issue("camera_response_not_ready", problem["message"], "models")
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        issue("camera_response_not_ready", str(exc), "models")
    for record in related:
        spec = record["payload"].get("spectrum")
        if spec and (spec["wavelength_nm"][0] > wave[0] or spec["wavelength_nm"][-1] < wave[-1]):
            issue(
                "scene_spectral_coverage",
                "재질·광원의 파장 범위가 Sensor를 포함해야 합니다.",
                "scene_assets." + record["id"],
            )
    keys = sorted(set(source["conditions"]) | set(assembly["conditions"]))
    result = {
        "schema": "camera_scene_preparation_v1",
        "draft": data,
        "status": "ready" if compatibility["status"] == "PASS" and not issues else "needs_input",
        "issues": issues,
        "compatibility": compatibility,
        "optics": optics,
        "sampling": sampling,
        "condition_changes": [
            {
                "condition": k,
                "before": source["conditions"].get(k),
                "after": assembly["conditions"].get(k),
            }
            for k in keys
            if source["conditions"].get(k) != assembly["conditions"].get(k)
        ],
        "model_changes": {"before": source["model_ids"], "after": assembly["model_ids"]},
        "source_assembly_sha256": source["content_hash"],
        "scene_sha256": scene["content_hash"],
        "model_hashes": {m["id"]: m["content_hash"] for m in models},
        "environment_sha256": digest(environment),
        "physical_validation": "not_established",
        "required_render_input": "Declare spectral transmittance and provenance before rendering.",
    }
    psf = lens["payload"].get("psf", {})
    if psf.get("spatial_model") == "field_grid":
        result["psf_coverage"] = {
            "field_y_mm": deepcopy(psf["field_y_mm"]),
            "field_x_mm": deepcopy(psf["field_x_mm"]),
            "coordinates": "sensor_center_yx",
            "preflight_scope": (
                "covered_patch_interfaces_only; actual render/reuse crop checked separately"
            ),
        }
    return {**result, "preview_sha256": digest(result)}


def save(service, payload):
    data = SceneCameraSave.model_validate(payload).model_dump()
    result = preview(service, data["draft"])
    if result["preview_sha256"] != data["preview_sha256"]:
        raise ConflictError("Camera preparation changed; preview the exact draft again")
    if result["status"] != "ready":
        raise PreflightError({"status": "FAIL", "issues": result["issues"]})
    identity = "assemblie-scene-" + result["preview_sha256"]
    document = {
        **result["draft"]["assembly"],
        "model_hashes": result["model_hashes"],
        "compatibility": result["compatibility"],
        "scene_preparation": result,
    }
    try:
        assembly = service.registry.put("assemblies", document, identity=identity)
    except ConflictError:
        assembly = service.registry.get("assemblies", identity)
        if {k: assembly[k] for k in document} != document:
            raise ConflictError(
                "Camera preparation identity differs from the stored record"
            ) from None
    from .scene_templates import render_template

    return {
        "assembly": assembly,
        "render": render_template(service, data["draft"]["scene_asset_id"], identity),
    }
