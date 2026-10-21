"""Bounded, paired experiments with explicitly idealized frozen lens specs.

The first runner compares one sensor revision at a common physical crop. It
refuses cross-sensor rankings and geometric raytrace rather than substituting
another model. Historical matched comparisons remain available in the UI.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
from pathlib import Path

import numpy as np
from PIL import Image

from .assets import AssetStore
from .camera import camera_create, camera_get, camera_set
from .scene import scene_adjust_luminance, scene_set
from .sensor_capture import resample_sensor_capture
from .sensor_comparison import _make_scene
from .sensor_simulation_db import sensor_simulation_db_config
from .system_faca import camerae2e_faca_report, camerae2e_run_scenario


def optics_specs(variant):
    lens = variant.get("lens")
    if not lens or lens["model_kind"] != "patent_prescription":
        raise ValueError(
            "첫 실행 버전은 사양이 있는 특허 렌즈의 회절 한계 모델을 지원합니다. "
            "다른 렌즈는 근거 비교를 이용하세요."
        )
    specs = lens["snapshot"].get("specifications", {})
    focal, fnumber = specs.get("focal_length_mm"), specs.get("f_number")
    if not all(
        isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in (focal, fnumber)
    ):
        raise ValueError("선택한 렌즈 버전에 유효한 초점거리·F-number가 없습니다.")
    return {"focal_length_mm": focal, "f_number": fnumber}


def validate_variants(variants):
    for variant in variants:
        if variant["optics_mode"] != "diffraction_proxy":
            raise ValueError(
                "실행하려면 설계안에서 ‘회절 한계 근사’를 명시적으로 선택하세요. "
                "처방 수차·기하 PSF 연결은 이 실행 경로에 포함되지 않습니다."
            )
        optics_specs(variant)
        sensor = variant.get("sensor")
        if (
            not sensor
            or sensor["model_kind"] not in ("physical_estimate_v1", "db_estimate_v1")
            or sensor["snapshot"].get("snapshot_status") != "verified_result_hash"
            or not sensor.get("runtime_binding")
        ):
            raise ValueError(
                "저장된 성공 실행에 연결된 physical_estimate_v1 또는 "
                "db_estimate_v1 센서 버전이 필요합니다."
            )
        pitch = sensor.get("geometry", {}).get("pixel_pitch_um")
        if not isinstance(pitch, (int, float)) or not math.isfinite(pitch) or pitch <= 0:
            raise ValueError("비교할 센서 버전의 픽셀 피치가 없습니다.")
    if variants[0]["sensor"]["revision_id"] != variants[1]["sensor"]["revision_id"]:
        raise ValueError(
            "첫 실행 버전은 동일 센서 버전에서 노출·렌즈 사양 변경을 비교합니다. "
            "서로 다른 센서는 ‘기존 근거’의 공통 크롭 비교를 이용하세요."
        )
    if optics_specs(variants[0])["focal_length_mm"] != optics_specs(variants[1])["focal_length_mm"]:
        raise ValueError(
            "초점거리가 달라 장면 배율이 일치하지 않습니다. "
            "첫 버전에서는 같은 초점거리의 설계안을 비교하세요."
        )


def run_comparison(variants, conditions, directory: Path, catalog_root: Path, requirements=()):
    validate_variants(variants)
    directory.mkdir(parents=True, exist_ok=False)
    n = conditions["output_size"]
    seeds = [conditions["seed"] + i for i in range(conditions["frames"])]
    store = AssetStore.default()
    specs = optics_specs(variants[0])
    pitch = variants[0]["sensor"]["geometry"]["pixel_pitch_um"]
    fov = math.degrees(2 * math.atan(1.2 * pitch * 1e-3 * n / (2 * specs["focal_length_mm"])))
    scene_kind = "macbeth_d65" if conditions["scene"] == "macbeth" else conditions["scene"]
    scene, _ = _make_scene(scene_kind, (n, n), fov, store)
    scene_set(scene, "distance", np.inf)
    scene_adjust_luminance(scene, conditions["luminance"], asset_store=store)
    records, arrays, images = [], {}, []
    for index, variant in enumerate(variants):
        frozen = variant["sensor"]
        code = frozen["snapshot"]["config"]["code"]
        binding = frozen["runtime_binding"]["legacy_run_id"]
        config = sensor_simulation_db_config(code, root=catalog_root, run_id=binding)
        spec = optics_specs(variant)
        optics = {
            "model": "diffractionlimited",
            "compute_method": "opticsotf",
            "offaxis_method": "cos4th",
            "focal_length_m": spec["focal_length_mm"] * 1e-3,
            "f_number": spec["f_number"],
            "fidelity": "diffraction_limited_proxy",
            "name": variant["lens"]["model"],
            "prescription_applied": False,
        }
        camera = camera_set(camera_create(), "optics", optics)
        exposure = (
            variant["exposure_ms"]
            if variant["exposure_ms"] is not None
            else conditions["exposure_ms"]
        )
        scenario = {
            **config["scenario"],
            "sensor": {"size": [n, n], "integration_time": exposure / 1000, "noise_flag": 2},
            "sensor_resize": False,
        }
        result = camerae2e_run_scenario(scenario, camera=camera, scene=scene.clone(), seed=seeds[0])
        sensor = camera_get(result["camera"], "sensor")
        sampled = [resample_sensor_capture(sensor, seed=seed) for seed in seeds]
        frames = np.stack([s.data["raw_dn"] for s in sampled])
        variance = frames.var(axis=0, ddof=1)
        noise = float(np.sqrt(variance.mean()))
        dark = sensor.clone()
        dark.data["mean_electrons"] = np.zeros_like(sensor.data["mean_electrons"])
        dark = resample_sensor_capture(dark, seed=seeds[0])
        signal = float(np.mean(sensor.data["reference_dn"] - dark.data["reference_dn"]))
        sat = {
            key: max(s.metadata["sensor_capture"]["saturation"][key] for s in sampled)
            for key in ("well_fraction", "adc_white_fraction", "adc_zero_fraction")
        }
        clipped = max(sat.values())
        snr = (
            20 * math.log10(signal / noise)
            if signal > 0 and noise > 0 and clipped <= 0.001
            else None
        )
        metrics = {
            "mean_photoelectrons": float(np.mean(sensor.data["mean_electrons"])),
            "raw_temporal_noise_dn": noise,
            "raw_snr_db": snr,
            "saturation_fraction": clipped,
        }
        prefix = "a" if index == 0 else "b"
        for stage, value in result["stages"].items():
            if "array" in value and value["array"] is not None:
                arrays[f"{prefix}_{stage}"] = np.asarray(value["array"])
        arrays[f"{prefix}_raw_frames"] = frames
        arrays[f"{prefix}_raw_temporal_variance"] = variance
        rgb = np.asarray(result["stages"]["ip_srgb"]["array"], dtype=float)
        if not np.all(np.isfinite(rgb)):
            raise ValueError("ISP 결과에 비유한 수치가 있습니다.")
        white = sensor.metadata["sensor_capture"]["white_level_dn"]
        images.append(
            {
                "rgb": np.clip(rgb, 0, 1),
                "raw": frames[0] / white,
                "electrons": sensor.data["mean_electrons"],
                "optical": np.asarray(result["stages"]["oi_photons"]["array"]).sum(axis=-1),
            }
        )
        report = camerae2e_faca_report(result)
        records.append(
            {
                "variant_id": variant["id"],
                "label": variant["label"],
                "prefix": prefix,
                "metrics": metrics,
                "saturation": sat,
                "exposure_ms": exposure,
                "optics": optics,
                "sensor_revision_id": frozen["revision_id"],
                "lens_revision_id": variant["lens"]["revision_id"],
                "sensor_model_sha256": frozen["content_sha256"],
                "lens_model_sha256": variant["lens"]["content_sha256"],
                "sensor_simulation_run_id": binding,
                "runtime_report": report,
                "snr_eligible": snr is not None,
            }
        )
    for stage in ("rgb", "raw", "electrons", "optical"):
        scale = (
            max(float(np.max(img[stage])) for img in images)
            if stage in ("electrons", "optical")
            else 1.0
        )
        for index, img in enumerate(images):
            pixels = np.rint(np.clip(img[stage] / max(scale, 1e-30), 0, 1) * 255).astype(np.uint8)
            Image.fromarray(pixels).save(directory / f"{'a' if index == 0 else 'b'}_{stage}.png")
    np.savez_compressed(directory / "stages.npz", **arrays)
    source_hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(__file__).parent.glob("*.py")
    }
    changed = []
    if records[0]["exposure_ms"] != records[1]["exposure_ms"]:
        changed.append("exposure_ms")
    if records[0]["optics"]["f_number"] != records[1]["optics"]["f_number"]:
        changed.append("f_number")
    output = {
        "schema": "camerae2e_design_comparison_v1",
        "variants": records,
        "conditions": conditions,
        "seed_sequence": seeds,
        "changed_axes": changed,
        "comparison": {
            "status": "matched",
            "sensor_revision_fixed": True,
            "physical_crop_mm": pitch * 1e-3 * n,
            "scene_fov_deg": fov,
            "illuminant": "D65",
            "static_scene": True,
            "note": "동일 센서·크롭·장면·시드 집합. "
            "여러 변수를 바꾼 경우 단일 원인으로 해석하지 않습니다.",
        },
        "fidelity": "diffraction_limited_proxy_with_frozen_sensor_estimate",
        "measured_accuracy_verified": False,
        "limitations": [
            "렌즈 처방 수차·flare·기하 PSF는 적용하지 않았습니다.",
            "센서와 읽기 모델은 저장된 추정·가정에 의존합니다.",
            "정적 장면이므로 노출 변화의 움직임 잔상은 이 실험에서 계산하지 않습니다.",
            "SNR는 RAW 시간 분산 기반이며 포화가 0.1%를 넘으면 판정하지 않습니다.",
            "소수 반복의 추정치이며 실측 제품 순위나 통계적 유의성을 보장하지 않습니다.",
        ],
        "display": {
            "rgb": "fixed sRGB [0,1]",
            "raw": "fixed ADC [0,white]",
            "electrons": "shared pair maximum",
            "optical": "shared pair maximum",
        },
        "implementation_sha256": source_hashes,
        "python": platform.python_version(),
        "numpy": np.__version__,
    }
    from .design_workbench import evaluate_requirements

    output["requirements"] = evaluate_requirements(requirements, records)
    (directory / "result.json").write_text(
        json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2)
    )
    output["artifacts"] = [
        {"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted(directory.iterdir())
    ]
    return output
