"""One reviewable decision package and an interactive, source-coordinate viewer."""

# Generated Markdown prose and replay commands keep their natural line lengths.
# ruff: noqa: E501

from __future__ import annotations

import json
import math
import shutil
import sys
from importlib import metadata
from pathlib import Path

import numpy as np
from PIL import Image

from .road_design_inputs import write_json
from .road_rgb_source import file_sha256

_VIEWER = (Path(__file__).parent / "static" / "road-design-report.html").read_text()


def _compact(records):
    return [
        {
            key: value
            for key, value in row.items()
            if key
            not in {
                "bootstrap_cnr",
                "well_fractions",
                "adc_fractions",
                "seeds",
                "profile",
                "fixed_pattern",
            }
        }
        for row in records
    ]


def geometry_diagnostics(spec):
    """Expose the area/pixel-count cost of keeping FOV and native pitch fixed."""
    result = {}
    for candidate in spec["candidates"]:
        focal = candidate["lens"]["model"]["effective_focal_length_mm"]
        pitch = candidate["sensor"]["native_response"]["pixel_pitch_um"]
        ideal_width = 2 * focal * math.tan(math.radians(spec["geometry"]["horizontal_fov_deg"]) / 2)
        cols = 2 * math.ceil(ideal_width * 1000 / pitch / 2)
        rows = 2 * math.ceil(cols * spec["geometry"]["aspect_row_col"] / 2)
        width, height = cols * pitch / 1000, rows * pitch / 1000
        result[candidate["id"]] = {
            "declared_virtual_shape_row_col": [rows, cols],
            "declared_virtual_width_height_mm": [width, height],
            "declared_virtual_area_mm2": width * height,
            "declared_virtual_megapixels": rows * cols / 1e6,
            "product_active_array_fit": "not_established",
        }
    baseline = result[spec["baseline_id"]]
    for value in result.values():
        value["area_ratio_to_baseline"] = (
            value["declared_virtual_area_mm2"] / baseline["declared_virtual_area_mm2"]
        )
        value["pixel_count_ratio_to_baseline"] = (
            value["declared_virtual_megapixels"] / baseline["declared_virtual_megapixels"]
        )
    return result


def _spatial_diagnostics(package, spec, records):
    """Fixed-grid masks and line profiles, for inspection; not selection metrics."""
    roi_views = {}
    for image in spec["images"]:
        for roi in image["rois"]:
            path = package / roi["mask_path"]
            if file_sha256(path) != roi["mask_sha256"]:
                raise ValueError("Frozen mask differs from the spatial diagnostic input.")
            with np.load(path) as masks:
                obj, bg = masks["object"], masks["background"]
            pixels = np.zeros((*obj.shape, 3), np.uint8) + 22
            pixels[obj] = [235, 180, 60]
            pixels[bg] = [75, 185, 220]
            mask_file = path.with_suffix(".png")
            Image.fromarray(pixels).save(mask_file)
            row = int(np.median(np.nonzero(obj)[0]))
            key = image["id"] + "/" + roi["id"]
            roi_views[key] = {"mask_file": str(mask_file.relative_to(package)), "line_row": row}
    for row in records:
        key = row["image_id"] + "/" + row["roi_id"]
        image_file = (package / row["preview_file"]).with_name("noise_free_linear_rgb.npy")
        linear = np.load(image_file, mmap_mode="r")
        values = linear[roi_views[key]["line_row"]] @ [0.2126, 0.7152, 0.0722]
        row["spatial_profile"] = values.tolist()
        row["mean_photoelectrons_scope"] = "native ROI including numerical halo"
    return roi_views


def design_assessment(spec, decision, records, geometry):
    """Make the actual failed conditions and engineering costs explicit."""
    if "evaluation" not in decision:
        return None
    baseline, selected = spec["baseline_id"], decision["selection"]["selected_id"]
    lookup = {(r["candidate_id"], r["image_id"], r["roi_id"], r["brightness"]): r for r in records}
    rois = {(image["id"], roi["id"]): roi for image in spec["images"] for roi in image["rois"]}
    contrast_failures = []
    for record in records:
        if record["candidate_id"] != selected or record["roi_kind"] != "object":
            continue
        base = lookup[(baseline, record["image_id"], record["roi_id"], record["brightness"])]
        ratio = record["local_contrast"] / base["local_contrast"]
        if ratio < spec["acceptance"]["contrast_ratio_min"]:
            roi = rois[(record["image_id"], record["roi_id"])]
            contrast_failures.append(
                {
                    "image_id": record["image_id"],
                    "roi_id": record["roi_id"],
                    "category": roi["category"],
                    "source_bbox_xyxy": roi["source_bbox_xyxy"],
                    "brightness": record["brightness"],
                    "contrast_ratio": ratio,
                    "baseline_local_contrast": base["local_contrast"],
                    "selected_local_contrast": record["local_contrast"],
                }
            )
    evaluation = decision["evaluation"]
    return {
        "status": decision["status"],
        "model_scope_recommended_id": decision["recommended_id"],
        "strict_contrast_requirement": (
            "Retain the baseline when >=95% contrast retention is mandatory. "
            "The selected design is a conditional lowlight tradeoff."
            if contrast_failures
            else "No observed violation of the declared contrast requirement."
        ),
        "heldout_lowlight_cnr_gain_median": evaluation["median_lowlight_cnr_gain"],
        "contrast_failures": contrast_failures,
        "geometry_cost": geometry[selected],
        "product_active_array_fit": "not_established",
        "scope": "saved pixel/readout response in a virtual common field; no product ranking",
    }


def build_report(package: Path, spec, decision, selection_records, evaluation_records):
    for image in spec["images"]:
        destination = package / "inputs" / image["id"] / "original.jpg"
        if not destination.exists():
            shutil.copyfile(image["source_path"], destination)
        if file_sha256(destination) != image["source_sha256"]:
            raise ValueError("Original-photo snapshot differs from the frozen source.")
    records = _compact(selection_records + evaluation_records)
    roi_views = _spatial_diagnostics(package, spec, records)
    geometry = geometry_diagnostics(spec)
    assessment = design_assessment(spec, decision, evaluation_records, geometry)
    if assessment:
        write_json(package / "design-assessment.json", assessment)
    payload = json.dumps(
        {
            "spec": spec,
            "decision": decision,
            "records": records,
            "roi_views": roi_views,
            "geometry_diagnostics": geometry,
            "assessment": assessment,
        },
        ensure_ascii=False,
    )
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    (package / "report.html").write_text(_VIEWER.replace("__PAYLOAD__", payload))
    shutil.copyfile(
        Path(__file__).parent / "static" / "road-design-report.js",
        package / "road-design-report.js",
    )
    labels = {
        "improvement_confirmed": "개선 확인",
        "conditional_selection": "조건부 선택",
        "baseline_retained": "기준안 유지",
    }
    selection = decision.get("selection", {})
    evaluation = decision.get("evaluation", {})
    geometry_table = "\n".join(
        f"| {cid} | {value['declared_virtual_width_height_mm'][0]:.2f} × {value['declared_virtual_width_height_mm'][1]:.2f} | {value['declared_virtual_megapixels']:.2f} | {value['area_ratio_to_baseline']:.2f} | {value['pixel_count_ratio_to_baseline']:.2f} |"
        for cid, value in geometry.items()
    )
    failure_table = ""
    if assessment and assessment["contrast_failures"]:
        failure_table = (
            "**대비 95% 보존이 필수인 용도에서는 기준안을 유지한다. "
            "선택안은 저조도 이득과 아래 공간 대비 손실을 허용하는 조건부 대안이다.**\n\n"
            "| 평가 이미지 | ROI / 범주 | 가상 광량 | 대비 보존 |\n"
            "|---|---|---:|---:|\n"
        ) + "\n".join(
            f"| {r['image_id']} | {r['roi_id']} / {r['category']} | {r['brightness']:g}× | {r['contrast_ratio'] * 100:.2f}% |"
            for r in assessment["contrast_failures"]
        )
    text = f"""# {spec["title"]}

결론: **{labels.get(decision["status"], "첫 사진 계산 완료, 선정 전")}**. 권장안: **{decision.get("recommended_id") or "선정 전"}**. 기준안: {spec["baseline_id"]}.

{selection.get("reason", "실제 도로 입력에서 RAW와 고정 ISP까지 연결했다. 별도 장면 평가 전에는 설계안을 선택하지 않는다.")}

평가용 저광량 CNR 중앙값 변화: {evaluation.get("median_lowlight_cnr_gain", "미평가")}. 최소 국소 대비 비율: {evaluation.get("minimum_local_contrast_ratio", "미평가")}.

{failure_table}

보고서는 [report.html](report.html)에서 원본 위치와 계산한 ROI를 나란히 확인한다. 영상마다 최대값을 맞추거나 자동 노출·WB로 밝기를 보정하지 않았다. CNR은 반복 취득의 픽셀별 시간 분산이며, 자연 영상의 질감·공간 표준편차·검출기 mAP가 아니다.

실험 명세는 [specification.json](specification.json)에 있고, 모델 revision·원본·마스크·조건·seed가 보존돼 있다. 선정은 6장의 4조합에서 하고, 독립 6장에는 기준안과 고정된 선정안만 적용한다. 포화와 국소 대비 제약을 함께 확인한다.

## 재실행

저장소 루트에서 한 번의 진입점으로 실행한다. `run`은 완결된 package를 검증하거나, 중단된 계산을 같은 명세로 재개한다.

```sh
PYTHONPATH=src .venv-platform/bin/python -B tools/run_road_camera_design.py run --package {package}
```

캐시 없이 새로 계산할 때는 같은 진입점의 `replay --package {package} --destination <new directory> --stage full`을 사용한다. 첫 사진만 재현하려면 `--stage first-photo`를 지정한다. 원본 package는 보존한다.

처음 만드는 경우 `freeze --dataset-root <nuImages root> --package <new directory>`로 입력을 고정한 뒤 `first-photo`, `run`, `verify`를 사용할 수 있다. 공식 마스크 해석은 `pip install '.[platform,road-study]'`로 설치한다. GPU·새로운 FDTD·모델 weight는 필요하지 않다. 파일 해시 확인: 같은 명령의 `verify` action.

## 적용 범위와 손실

RGB-derived spectral proxy, 450/550/650 nm의 명시적 display basis, 저장된 기하 raytrace PSF와 추정 센서 QE/readout을 사용한다. 원본 촬영 당시의 스펙트럼·조도는 복원하지 않는다. lens 투과율·상대 조도는 unity 가정이며 회절·CRA 결합은 검증되지 않았다.

가상 센서의 pixel pitch는 저장 모델과 같고 원본 물체각과 주변부 위치를 유지한다. 공통 시야를 유지하는 데 필요한 센서 면적·native pixel 수는 렌즈에 따라 늘어날 수 있다. 실제 제품 active-array 크기에 장착 가능한 조합이나 전체 FOV를 인증하는 결과는 아니다. 이 제약 없이 제품 순위로 해석하면 안 된다.

| 후보 | 선언한 가상 영역 mm | 전체 영역 MP | 기준 대비 면적 | 기준 대비 픽셀 수 |
|---|---:|---:|---:|---:|
{geometry_table}

표의 전체 영역은 ROI 계산의 좌표계이며 전체 RAW를 계산·저장했다는 뜻이 아니다. 같은 화각에서 긴 초점거리를 쓰는 이득에는 더 큰 수광 면적과 native pixel 수의 비용이 포함된다. 실제 센서 크기 제약은 이후 별도로 적용해야 한다.

화면의 선 프로파일은 고정 물체각 격자에서의 잡음 없는 ISP 휘도 진단이다. 자연 영상의 이 선을 MTF나 검출 정확도로 표시하지 않으며, 사전 후보 선정 기준에 추가하지 않았다. 광전자 평균은 수치 주변 영역을 포함한 native ROI 평균이다.

대표 ROI의 입력 격자만 두 단계 비교했다. 차이가 수렴 오차보다 작거나 신뢰구간이 겹치면 개선 근거로 선택하지 않는다. 원본 정보량에 맞춰 공통 ROI는 32~93 표본이며, 화면 확대가 새로운 세부 정보를 뜻하지 않는다.

nuImages / Motional 자료는 비상업 연구 범위이다. [공식 이용 안내](https://www.nuscenes.org/nuimages). 상업적 권한은 확인되지 않았으며 외부 데모나 제품 데이터로 사용할 수 있다고 판단하지 않았다.
"""
    (package / "README.md").write_text(text)
    if "evaluation" in decision:
        source_root = Path(__file__).parent
        source_files = list(source_root.glob("road_*.py")) + [
            source_root / name
            for name in (
                "api.py",
                "sdk.py",
                "service.py",
                "worker.py",
                "lens_sensor_streamed_analytic_capture.py",
                "lens_sensor_streamed_offaxis_mean.py",
                "static/road-design.html",
                "static/road-design.js",
                "static/road-design-report.html",
                "static/road-design-report.js",
            )
        ]
        hashes = {
            "camera_platform/" + str(path.relative_to(source_root)): file_sha256(path)
            for path in source_files
        }
        script = source_root.parents[1] / "tools/run_road_camera_design.py"
        if script.exists():
            hashes["tools/run_road_camera_design.py"] = file_sha256(script)
        versions = {}
        for name in ("numpy", "scipy", "pillow", "pycocotools", "fastapi", "httpx"):
            try:
                versions[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                versions[name] = None
        write_json(
            package / "delivery-implementation.json",
            {
                "schema": "camera_road_design_delivery_sources_v1",
                "snapshot_scope": (
                    "Final delivery source files; retained calculations have their own pinned "
                    "input/model contracts and array hashes. This is not a claim that every "
                    "cached calculation ran at this exact source snapshot."
                ),
                "python": sys.version,
                "dependency_versions": versions,
                "files": hashes,
                "source_grid_and_raw_equivalence_tests": "tests/platform/test_road_design.py",
                "native_engine_binding": "Saved sensor snapshots require matching native core source hashes.",
            },
        )
        paths = sorted(
            path
            for path in package.rglob("*")
            if path.is_file()
            and "diagnostics" not in path.parts
            and ".tmp" not in path.name
            and path.name not in {"progress.json", "package-manifest.json", ".execution.lock"}
        )
        write_json(
            package / "package-manifest.json",
            {
                "schema": "camera_road_design_package_v1",
                "specification_sha256": decision["specification_sha256"],
                "files": {str(path.relative_to(package)): file_sha256(path) for path in paths},
                "total_bytes": sum(path.stat().st_size for path in paths),
                "commercial_export_allowed": False,
                "original_photos": "included for authorized noncommercial internal research; preserve dataset terms",
            },
        )
