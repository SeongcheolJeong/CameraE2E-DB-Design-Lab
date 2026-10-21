"""Explicit engineering fixtures. No vendor claims and no invented measurements."""

from __future__ import annotations

from copy import deepcopy

RAW_FORMAT = {
    "cfa": "RGGB",
    "bit_depth": 12,
    "layout": "bayer",
    "packing": "unpacked",
    "black_level": 64,
    "hdr_streams": 1,
}
CONDITIONS = {
    "exposure_s": {"value": 0.004, "unit": "s"},
    "temperature_c": {"value": 25.0, "unit": "degC"},
    "gain": {"value": 1.0, "unit": "1"},
    "light_id": {"value": "fixture-flat-spectrum-v1", "unit": "1"},
}
DOMAIN = {
    "exposure_s": {"min": 0.0001, "max": 0.02, "unit": "s"},
    "temperature_c": {"min": 20.0, "max": 30.0, "unit": "degC"},
    "gain": {"min": 1.0, "max": 1.0, "unit": "1"},
}
SPECTRAL_RANGE = {"min": 450.0, "max": 650.0, "unit": "nm"}
EFFECTS = [
    "lens_relative_illumination",
    "lens_incident_angle_distribution",
    "sensor_effective_qe",
    "sensor_angular_response",
    "mean_dark_charge",
    "readout_clipping",
]
PROVENANCE = {
    "source_kind": "estimated",
    "source_uri": "fixture://camera-platform/angular-shading-v1",
    "license": "MIT; authored synthetic engineering fixture",
    "method_and_version": "analytic-fixture/1",
    "physical_instance": None,
    "parents": [],
}


def seed(service):
    if service.registry.list("assemblies"):
        return
    # API initialization holds a process lock while registering this deterministic seed.
    lens_asset = service.create_asset(
        {
            **PROVENANCE,
            "name": "입사각 분포 비교 렌즈",
            "kind": "lens",
            "description": "가정한 렌즈 A/B. 실제 처방 또는 상용 렌즈와의 일치 미검증.",
        }
    )
    sensor_asset = service.create_asset(
        {
            **PROVENANCE,
            "name": "분광·각도 응답 센서",
            "kind": "sensor",
            "description": "가정한 3-channel angular response; 실물 센서가 아님.",
        }
    )
    isp_asset = service.create_asset(
        {
            **PROVENANCE,
            "name": "CameraE2E Software ISP",
            "kind": "isp",
            "description": "기존 pyisetcam RAW sampler 및 ISP를 재사용하는 고정 조건 경로.",
        }
    )
    base = {
        **PROVENANCE,
        "revision": "1",
        "reference_plane": "sensor_surface",
        "valid_domain": DOMAIN,
        "spectral_range": SPECTRAL_RANGE,
        "uncertainty": {"kind": "unknown"},
        "calibration_dataset_ids": [],
    }
    models = []
    for suffix, weights, illumination in (
        (
            "A",
            [[1.0, 0.0, 0.0], [0.8, 0.2, 0.0], [0.4, 0.5, 0.1], [0.1, 0.5, 0.4], [0.0, 0.3, 0.7]],
            [1.0, 0.98, 0.9, 0.78, 0.64],
        ),
        (
            "B",
            [
                [1.0, 0.0, 0.0],
                [0.9, 0.1, 0.0],
                [0.75, 0.25, 0.0],
                [0.5, 0.45, 0.05],
                [0.2, 0.7, 0.1],
            ],
            [1.0, 0.99, 0.95, 0.88, 0.8],
        ),
    ):
        models.append(
            service.create_model(
                {
                    **base,
                    "name": f"Lens {suffix} · 가정한 입사각 분포",
                    "asset_id": lens_asset["id"],
                    "kind": "lens",
                    "included_effects": [
                        "lens_relative_illumination",
                        "lens_incident_angle_distribution",
                    ],
                    "properties": {
                        "image_circle_diameter": {"value": 8.0, "unit": "mm"},
                        "incident_angle_range": {"min": 0.0, "max": 30.0, "unit": "deg"},
                    },
                    "payload": {
                        "field_radius": [0.0, 0.25, 0.5, 0.75, 1.0],
                        "theta_deg": [0.0, 15.0, 30.0],
                        "angular_weights": weights,
                        "relative_illumination": illumination,
                    },
                }
            )
        )
    sensor = service.create_model(
        {
            **base,
            "name": "Sensor · 가정한 각도별 색 응답",
            "asset_id": sensor_asset["id"],
            "kind": "sensor",
            "included_effects": [
                "sensor_effective_qe",
                "sensor_angular_response",
                "mean_dark_charge",
                "readout_clipping",
            ],
            "properties": {
                "active_width": {"value": 5.76, "unit": "mm"},
                "active_height": {"value": 4.32, "unit": "mm"},
                "accepted_angle_range": {"min": 0.0, "max": 30.0, "unit": "deg"},
            },
            "raw_format": RAW_FORMAT,
            "payload": {
                "wavelength_nm": [450.0, 500.0, 550.0, 600.0, 650.0],
                "qe": [
                    [0.03, 0.08, 0.2, 0.65, 0.55],
                    [0.1, 0.5, 0.75, 0.4, 0.05],
                    [0.7, 0.55, 0.1, 0.02, 0.01],
                ],
                "pixel_pitch_um": 3.0,
                "sensor_angular_response": {
                    "theta_deg": [0.0, 15.0, 30.0],
                    "relative_response": [
                        [[1.0, 0.92, 0.65]] * 5,
                        [[1.0, 0.97, 0.88]] * 5,
                        [[1.0, 0.86, 0.48]] * 5,
                    ],
                },
                "readout": {
                    "full_well_e": 10000.0,
                    "read_noise_e": 2.0,
                    "dark_current_e_per_s": 1.0,
                    "gain": 1.0,
                    "bit_depth": 12,
                    "black_level_dn": 64.0,
                },
            },
        }
    )
    isp = service.create_model(
        {
            **base,
            "name": "고정 Software ISP · pyisetcam",
            "asset_id": isp_asset["id"],
            "kind": "isp",
            "reference_plane": "sensor_raw",
            "included_effects": ["software_isp"],
            "input_raw_format": RAW_FORMAT,
            "payload": {
                "implementation": "pyisetcam",
                "mode": "fixed",
                "awb": "fixed",
                "lens_shading_correction": "bypass",
            },
        }
    )
    for lens in models:
        service.create_assembly(
            {
                "name": "주변부 색 변화 · " + lens["name"].split(" · ")[0],
                "model_ids": [lens["id"], sensor["id"], isp["id"]],
                "conditions": CONDITIONS,
                "spectral_range": SPECTRAL_RANGE,
                "raw_format": RAW_FORMAT,
                "description": (
                    "동일 센서·광원·노출에서 입사각 분포가 RAW R/G, B/G에 미치는 영향을 비교합니다."
                ),
            }
        )
    service.create_test(
        {
            "name": "주변부 RAW 채널 비율 · 독립 실측 검증",
            "metric_id": "raw.channel_ratio",
            "unit": "1",
            "reference_plane": "sensor_raw",
            "included_effects": EFFECTS,
            "conditions": CONDITIONS,
            "required_conditions": list(CONDITIONS),
            "absolute_tolerance": {"value": 0.03, "unit": "1"},
            "stimulus": {
                "wavelength_nm": [450.0, 500.0, 550.0, 600.0, 650.0],
                "spectral_irradiance_w_m2_nm": [0.0002] * 5,
                "wavelength_unit": "nm",
                "spectral_irradiance_unit": "W/m^2/nm",
                "reference_plane": "sensor_surface",
                "description": (
                    "Explicit flat spectral irradiance at on-axis sensor surface; "
                    "not lux-to-spectrum conversion"
                ),
            },
        }
    )


def measurement_template(service) -> dict:
    tests, assemblies = service.registry.list("tests"), service.registry.list("assemblies")
    test, assembly = tests[-1], assemblies[-1]
    return {
        **deepcopy(PROVENANCE),
        "name": "형식 예제 · 실제 측정값으로 교체 필요",
        "revision": "1",
        "source_uri": "fixture://format-only-not-a-measurement",
        "test_id": test["id"],
        "assembly_id": assembly["id"],
        "metric_id": test["metric_id"],
        "unit": "1",
        "reference_plane": "sensor_raw",
        "included_effects": EFFECTS,
        "values": [1.0] * 10,
        "axes": [
            {"name": "field_radius", "unit": "1", "values": [0.0, 0.25, 0.5, 0.75, 1.0]},
            {"name": "channel_ratio", "unit": "1", "values": ["R/G", "B/G"]},
        ],
        "conditions": deepcopy(CONDITIONS),
        "valid_domain": DOMAIN,
        "uncertainty": {"kind": "unknown"},
        "dataset_ids": ["format-example-not-physical"],
        "calibration_dataset_ids": [],
        "instrument": {},
    }


def model_template(service) -> dict:
    model = next(m for m in reversed(service.registry.list("models")) if m["kind"] == "lens")
    allowed = set(service.model_contract.model_fields)
    return {
        **{k: deepcopy(v) for k, v in model.items() if k in allowed},
        "name": "새 Lens 응답 모델 후보",
        "revision": "2",
        "parents": [model["id"]],
    }
