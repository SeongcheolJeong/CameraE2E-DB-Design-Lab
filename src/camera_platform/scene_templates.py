"""Authoring templates; declared examples never claim measured material properties."""

from .scene_contracts import SceneAssetCreate, SceneRenderCreate

SOURCE = {
    "source_kind": "estimated",
    "source_uri": "fixture://authored-scene-design",
    "license": "MIT; authored synthetic design",
    "method_and_version": "scene-design-template:1",
    "physical_instance": None,
    "parents": [],
}


def templates(service):
    assets = service.registry.list("scene_assets")
    geometry = next((a for a in assets if a["payload"]["kind"] == "geometry"), None)
    material = next((a for a in assets if a["payload"]["kind"] == "material"), None)
    light = next(
        (
            a
            for a in assets
            if a["payload"]["kind"] == "light"
            and a["payload"]["distribution"] == "constant_environment"
        ),
        None,
    )
    spectrum = {"wavelength_nm": [450.0, 500.0, 550.0, 600.0, 650.0], "values": [0.5] * 5}
    payloads = {
        "geometry": {"kind": "geometry", "unit": "m", "primitive": "sphere"},
        "material": {
            "kind": "material",
            "bsdf": "diffuse",
            "spectrum": spectrum,
            "surface_state": "declared dry diffuse surface",
        },
        "light": {
            "kind": "light",
            "distribution": "constant_environment",
            "spectrum": {**spectrum, "values": [0.002] * 5},
            "unit": "W/m^2/sr/nm",
            "time_model": "static",
        },
        "scene": {
            "kind": "scene",
            "coordinate_system": "right_handed_y_up_metres",
            "instances": [
                {
                    "geometry_id": geometry["id"],
                    "material_id": material["id"],
                    "to_world": [
                        [0.015, 0.0, 0.0, 0.0],
                        [0.0, 0.015, 0.0, 0.0],
                        [0.0, 0.0, 0.015, 3.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ],
                }
            ]
            if geometry and material
            else [],
            "environment_light_id": light["id"] if light else None,
            "point_lights": [],
        },
    }
    labels = {
        "geometry": "새 형상",
        "material": "분광 재질 설계안",
        "light": "분광 환경광 설계안",
        "scene": "새 3D 장면",
    }
    return {
        "assets": {
            kind: SceneAssetCreate.model_validate(
                {**SOURCE, "name": labels[kind], "payload": payload}
            ).model_dump()
            for kind, payload in payloads.items()
        },
        "schemas": {
            "asset": SceneAssetCreate.model_json_schema(),
            "render": SceneRenderCreate.model_json_schema(),
        },
    }


def render_template(service, scene_id, assembly_id):
    from . import imaging, projection_radiometry
    from .scene_render import _asset

    scene = _asset(service, scene_id, "scene")
    assembly, sensor, pitch, full = imaging._native(service, assembly_id)
    lens = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "lens"
    )
    wave = sensor["payload"]["wavelength_nm"]
    projection = {
        **SOURCE,
        "projection_model": "paraxial_on_axis_projection_v1",
        "focal_length_mm": lens["properties"].get("focal_length", {}).get("value"),
        "f_number": lens["properties"].get("f_number", {}).get("value"),
        "on_axis_transmittance": {"wavelength_nm": wave, "values": [None] * len(wave)},
        "geometry_model": "pinhole_pixel_centre",
        "irradiance_model": "pi_L_T_over_4N2_before_relative_illumination",
        "approximation_uncertainty": "unknown",
    }
    absolute = projection_radiometry.requires_prescription_projection(lens)
    if absolute:
        projection, _ = projection_radiometry.model_projection(service, lens)
    issues = []
    if assembly["conditions"].get("light_id") != {"value": scene["illumination_id"], "unit": "1"}:
        issues.append("카메라의 light_id가 이 장면의 고정 조명과 다릅니다.")
    for key in ["focal_length_mm", "f_number"]:
        if projection[key] is None:
            issues.append(f"Lens 모델에 {key} 선언이 없습니다. 정확한 모델 버전을 등록하세요.")
    if assembly["conditions"].get("f_number") != {"value": projection["f_number"], "unit": "1"}:
        issues.append("카메라의 f_number 조건과 Lens 모델의 조리개가 일치해야 합니다.")
    for prop, unit in [("focal_length", "mm"), ("f_number", "1")]:
        if prop in lens["properties"] and lens["properties"][prop].get("unit") != unit:
            issues.append(f"Lens {prop}의 선언 단위는 {unit}여야 합니다.")
    if "psf" not in lens["payload"]:
        issues.append("렌더링 후 Camera 계산에는 이 Lens와 연결된 PSF가 필요합니다.")
    carrier_support = "charge_collection" in sensor["payload"]
    # The authored render request denotes the receiver ROI. A carrier Sensor
    # needs a separate optical-source halo, which may be much larger than a
    # 32-pixel receiver crop under the bounded directional integration budget.
    shape = [min(8 if carrier_support else 32, n // 2 * 2) for n in full]
    if (lens["payload"].get("psf", {}).get("spatial_model") == "field_grid"
            or carrier_support):
        from .scene_camera import field_response_preflight

        sampling = {
            "full_sensor_shape": full,
            "pixel_pitch_um": pitch,
            "crop_origin": [(n - side) // 2 for n, side in zip(full, shape, strict=True)],
        }
        issues.extend(
            p["message"] for p in field_response_preflight(service, assembly, sampling, shape)
        )
    return {
        "request": {
            "name": scene["name"] + " · 분광 렌더링",
            "scene_asset_id": scene_id,
            "assembly_id": assembly_id,
            "shape": shape,
            "crop_origin": None,
            "render_psf_halo": False,
            "render_charge_collection_halo": carrier_support,
            "samples_per_pixel": 16,
            "max_depth": 8,
            "seed": 42,
            "force": False,
            "pose": {
                "origin_m": [0.0, 0.0, 0.0],
                "target_m": [0.0, 0.0, 3.0],
                "up": [0.0, 1.0, 0.0],
            },
            "projection": projection,
        },
        "issues": issues,
        "required_input": (
            "Verified prescription E/L is fixed by the Lens; no additional transmittance input."
            if absolute else "on_axis_transmittance values and actual source/method"
        ),
        "scene_illumination_id": scene["illumination_id"],
        "physical_validation": "not_established",
    }
