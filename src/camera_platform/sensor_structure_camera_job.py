"""Source-bound OCL hypothesis -> fresh RAW/ISP demonstration jobs."""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

import numpy as np

from . import sensor_structure_camera as core
from . import design_workshop_storage as storage
from .registry import IntegrityError, ConflictError, canonical, digest
from .sensor_structure_job import sha

KIND = "sensor_structure_camera_demo"
ROUTE = "/api/v1/sensor-structure-camera-studies"
SCHEMA = "camera_ocl_assumption_projection_v1"
WORKER_TIMEOUT_S = 1800


def template():
    return dict(schema=SCHEMA, name="OCL 응답 가정 → RAW·ISP 민감도",
        trend_run_id="run-51b6c8b3a54a4bc8af0ab7c1fddb1315",
        camera_run_id="run-33817fe3ce14497d88aa4ef55e0b4cda",
        camera_candidate_id="candidate-01",
        camera_manifest_sha256="4ee41a9149624caca3b38164cbac372f521806f0b5b8ac10bdcb0c7a2873255d",
        fields=["center", "outer-ne"], charts=["uniform", "color", "texture", "resolution"],
        light_scales=[.1, 1.], profiles=list(core.PROFILES), seed=2718,
        frames=8, exposure_ms=4., gain=1.,
        projection_contract="finite_beam_central_ratio_as_local_gain_hypothesis",
        angular_mapping="center_0_outer_ne_20_declared_nodes_not_pupil_average")


def specification(payload):
    default = template()
    if not isinstance(payload, dict) or set(payload) != set(default):
        raise ValueError("Use the explicit OCL camera sensitivity template")
    s = copy.deepcopy(payload)
    # First deliverable is a pinned evidence pair. A new camera or structure
    # requires a new contract so the assumptions below cannot silently drift.
    for key in ("trend_run_id", "camera_run_id", "camera_candidate_id", "camera_manifest_sha256"):
        if s[key] != default[key]:
            raise ValueError("First OCL bridge pins its reviewed source pair: " + key)
    for key in ("schema", "projection_contract", "angular_mapping", "frames", "exposure_ms", "gain"):
        if type(s[key]) is bool or s[key] != default[key]:
            raise ValueError("Unsupported projection contract or fixed acquisition: " + key)
    for key in ("trend_run_id", "camera_run_id"):
        if not re.fullmatch(r"run-[0-9a-f]{32}", str(s[key])):
            raise ValueError("Invalid pinned source run")
    if not re.fullmatch(r"candidate-[0-9]{2}", str(s["camera_candidate_id"])) or not re.fullmatch(r"[a-f0-9]{64}", str(s["camera_manifest_sha256"])):
        raise ValueError("Pin a candidate and camera manifest hash")
    for key in ("fields", "charts", "light_scales", "profiles"):
        if (not isinstance(s[key], list) or not s[key] or len(s[key]) > len(default[key])
                or any(v not in default[key] or type(v) is bool for v in s[key])
                or len(set(s[key])) != len(s[key])):
            raise ValueError("Unsupported or repeated selections: " + key)
    if type(s["seed"]) is not int or not 0 <= s["seed"] < 2**31:
        raise ValueError("Invalid acquisition seed")
    if not isinstance(s["name"], str) or not 1 <= len(s["name"]) <= 160:
        raise ValueError("Name requires 1..160 characters")
    return s


def fingerprint():
    names = ("sensor_structure_camera.py", "sensor_structure_camera_job.py", "electrical.py",
             "reference_sensor_modes.py", "reference_workshop_sensor.py", "virtual_lab_metrics.py")
    paths = [Path(__file__).parent / n for n in names]
    paths += [Path(__file__).parents[1] / "pyisetcam" / n for n in ("ip.py", "bayer_demosaic.py", "metrics.py", "color.py")]
    return {str(p.relative_to(Path(__file__).parents[1])): sha(p) for p in paths}


def _camera(service, spec):
    root = storage.root(service) / "reference-workshop" / spec["camera_run_id"]
    manifest = root / "manifest.json"
    if sha(manifest) != spec["camera_manifest_sha256"]:
        raise IntegrityError("Pinned camera source manifest changed")
    files = json.loads(manifest.read_text())["files"]
    prefix = "candidates/" + spec["camera_candidate_id"]
    directory = root / prefix
    required = ["capture.json", "calibration.json", "pixel-cell.json", "optical-response.json"]
    required += [f"fields/{f}/{c}/mean-electron-rate.npy" for f in spec["fields"] for c in spec["charts"]]
    refs = []
    for name in required:
        path = directory / name
        expected = files.get(prefix + "/" + name)
        if expected is None or sha(path) != expected:
            raise IntegrityError("Camera source file changed: " + name)
        refs.append(dict(name=name, sha256=expected))
    capture = json.loads((directory / "capture.json").read_text())
    cell = json.loads((directory / "pixel-cell.json").read_text())
    calibration = json.loads((directory / "calibration.json").read_text())
    config = capture["configuration"]
    if cell["source_report"] != "DEF-2206-805" or cell["pixel_pitch_um"] != 2.9:
        raise ValueError("This bridge requires the same IMX662 structure and native pitch")
    if config["exposure_ms"] != 4 or config["gain"] != 1 or config["luminance_cd_m2"] != 1000:
        raise ValueError("Pinned camera needs D65 1000 cd/m², 4 ms, gain 1")
    for field in spec["fields"]:
        g = capture["fields"][field]["geometry"]
        if g["native_shape_row_col"] != [256, 256] or g["cfa_origin"]["pattern"] != "RGGB":
            raise ValueError("Native 256-square RGGB probes required")
    return directory, capture, cell, calibration, refs


def _trend(service, identity, verify_arrays=False):
    job = service.registry.get_run(identity)
    if job.get("job_kind") != "sensor_structure_study" or job["status"] != "succeeded":
        raise ValueError("A completed trend study is required")
    summary = job["result"]["summary"]
    evidence = core.response_evidence(summary)
    artifact = next(a for a in job["result"]["artifacts"] if a["name"] == "sample-manifest.json")
    bound = service.registry.artifact_binding(artifact["sha256"], owner_kind="runs", owner_id=identity)
    records = json.loads(Path(bound["path"]).read_text())
    # Bind every completed numerical control, not just the illustrative anchors.
    if verify_arrays:
        for record in records:
            path = Path(record["path"])
            if sha(path) != record["json_sha256"] or sha(path.with_suffix(".npz")) != record["array_sha256"]:
                raise IntegrityError("Source numerical result changed: " + record["task_id"])
    return summary, evidence, dict(summary_sha256=digest(summary),
        manifest_sha256=artifact["sha256"], samples=records)


def submit(service, payload):
    from .service import environment_fingerprint
    if environment_fingerprint() != service.loaded_environment:
        raise ConflictError("Source changed; restart API before submitting")
    spec = specification(payload)
    _, _, _, _, refs = _camera(service, spec)
    _, _, numerical = _trend(service, spec["trend_run_id"])
    doc = dict(name=spec["name"], job_kind=KIND, specification=spec,
        source_code=fingerprint(), camera_files=refs, numerical_source=numerical,
        estimated_bytes=2_000_000_000, default_worker_timeout_seconds=WORKER_TIMEOUT_S)
    return service.registry.submit(doc, digest(doc))


def assumptions():
    return [dict(id=k, label=label, classification=status, description=text) for k, label, status, text in (
        ("source", "확인된 2D 응답", "computed_stable", "IMX662 구조, 550 nm all-green 단면, σ=0.87 µm 유한 Gaussian 빔, OCL +0.29 µm. 중심 흡수 변화는 공간 재분배이며 균일장 QE가 아닙니다."),
        ("gain", "영상 응답으로의 연결", "explicit_hypothesis", "유한 빔 중심 흡수 비율을 각 native pixel의 광전자율 배율로 가정합니다. 공간 전달 kernel·MTF·3D QE로 변환하지 않습니다."),
        ("spectrum", "색 응답 확장", "explicit_hypothesis", "green_only: 전체 Gr/Gb 대역에 배율을 적용하고 R/B는 고정. achromatic_shared: RGB 모두 동일 배율. 두 가정은 불확실성의 상·하한이 아닙니다. 색 변화는 이 가정의 민감도입니다."),
        ("angles", "중심·주변 각도 대응", "explicit_hypothesis", "중심 탐침=0°, outer-ne 탐침=+20°의 확인 노드를 배정. 실제 주변 CRA 약18.72°·방위45° 및 동공 전체 방향을 적분한 결과가 아닙니다."),
        ("host", "기존 카메라 응답", "reused_model", "Canon 특허 p0014 렌즈 + IMX662 구조 추정의 256×256 평균 전자율을 재사용. 기하 PSF, native 픽셀 적분과 기존 광량·색 모델을 유지합니다. 주변 탐침은 가상 영상면 위치로 실제 IMX662 다이 범위가 아닙니다."),
        ("readout", "전기·전하 수집", "model_proxy", "저장 ASI662MC 기반 readout proxy·전하 수집 모델 고정. 4ms/gain1/25°C, 8회 독립 reset. PRNU/DSNU는 저장 모델의0, A/B에 같은 seed 정책. 입력 RAW를 배율 조정하지 않습니다."),
        ("isp", "고정 ISP", "fixed_policy", "기존 IMX462 기준 공통 전자→RGB 행렬을 양쪽에 그대로 사용(662 실측 보정 아님). Bilinear, WB1, LSC OFF, NR/sharpen OFF, 동일 tone/white. 후보별 색·밝기를 다시 맞추지 않습니다."),
        ("limits", "미확인 영역", "not_validated", "3D/Bayer 배치, R/B 분광 응답, azimuth·동공 평균, 공간 crosstalk, 실제 제품·실측 정합은 미확인. 기존 qualified 3D adapter의 2D 거부는 유지됩니다."),
    )]


def _png(path, values):
    from PIL import Image
    Image.fromarray(np.uint8(np.rint(255*np.clip(values, 0, 1)))).save(path)


def execute(service, job):
    from .sensor_structure_execution import StatusProbe
    from .reference_workshop_engine import write_json
    if job["source_code"] != fingerprint():
        raise IntegrityError("Projection implementation changed since submission")
    spec = specification(job["specification"])
    source, capture, cell, calibration, refs = _camera(service, spec)
    trend, evidence, numerical = _trend(service, spec["trend_run_id"], verify_arrays=True)
    if refs != job["camera_files"] or numerical != job["numerical_source"]:
        raise IntegrityError("Bound source evidence changed")
    started = time.monotonic()
    probe = StatusProbe(service.registry, job["id"])
    with storage.reserve(service, job["estimated_bytes"], allow_internal_aliases=True) as (root, check):
        directory = root / "sensor-structure-camera" / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        optics = json.loads((source / "optical-response.json").read_text())
        write_json(directory / "specification.json", spec)
        write_json(directory / "calibration.json", calibration)
        write_json(directory / "pixel-cell.json", cell)
        write_json(directory / "numerical-source.json", numerical)
        cases = []
        written_bytes = 0
        for field in spec["fields"]:
            optical = next(f for f in optics["fields"] if f["id"] == field)
            angle = 0. if field == "center" else 20.
            effect = next(e for e in evidence if e["angle_deg"] == angle)
            for chart in spec["charts"]:
                rate = np.load(source / "fields" / field / chart / "mean-electron-rate.npy", allow_pickle=False)
                for light in spec["light_scales"]:
                    seed = spec["seed"] + (10000 if field != "center" else 0) + template()["charts"].index(chart)*100 + (10 if light == 1 else 0)
                    base = core.acquire(rate*light, cell["electrical_model"], seed)
                    pb = core.process(base, calibration["color"]["matrix"], None)
                    for profile in spec["profiles"]:
                        probe.check()
                        projected_rate = core.project_rate(rate*light, effect["central_gain"], profile)
                        changed = core.acquire(projected_rate, cell["electrical_model"], seed)
                        pc = core.process(changed, calibration["color"]["matrix"], None)
                        common_raw = np.all(base["valid_frames"] & changed["valid_frames"], axis=0)
                        common_rgb = np.all(pb["valid"] & pc["valid"], axis=0)
                        for mask in (common_raw, common_rgb):
                            mask[:8] = False; mask[-8:] = False; mask[:, :8] = False; mask[:, -8:] = False
                        mb = core.measure(base, pb, common_raw, common_rgb)
                        mc = core.measure(changed, pc, common_raw, common_rgb)
                        comparison = core.compare(base, changed, pb, pc, mb, mc, common_rgb, chart, light)
                        identity = f"case-{len(cases):03d}"
                        folder = directory / "cases" / identity
                        folder.mkdir(parents=True, exist_ok=True)
                        case = dict(id=identity, field_id=field, applied_angle_deg=angle,
                            host_cra_deg=optical["chief_ray_angle_deg"], chart=chart,
                            light_scale=light, profile_id=profile, response_gain=effect["central_gain"],
                            source_geometry=capture["fields"][field]["geometry"],
                            frame_seeds=list(range(seed, seed+8)),
                            metrics=dict(baseline=mb, projected=mc, comparison=comparison))
                        arrays = dict(baseline_mean_rate=rate*light, projected_mean_rate=projected_rate,
                            common_raw_valid=common_raw, common_rgb_valid=common_rgb)
                        for name, acquired, processed in (("baseline", base, pb), ("projected", changed, pc)):
                            for key in ("raw_frames", "electron_frames", "well_frames", "adc_frames", "valid_frames", "variance_e2", "expected_photo_e", "photo_response_map"):
                                arr = acquired[key]
                                arrays[name+"_"+key] = arr.astype(np.float32) if arr.dtype.kind == "f" else arr
                            arrays[name+"_linear_frames"] = processed["linear"].astype(np.float32)
                            arrays[name+"_expected_linear"] = processed["expected_linear"].astype(np.float32)
                            arrays[name+"_calibration_dark_e"] = acquired["calibration"]["dark_e"]
                            np.save(folder / (name+"-raw.npy"), acquired["raw_frames"])
                            write_json(folder / (name+"-capture.json"), dict(
                                summary=acquired["capture_summary"], calibration={
                                    k: v for k, v in acquired["calibration"].items() if k != "dark_e"},
                                dark_e_array=name+"_calibration_dark_e in arrays.npz"))
                            _png(folder / (name+"-raw.png"), acquired["electron_frames"][0] / cell["readout"]["full_well_e"])
                            _png(folder / (name+"-isp.png"), processed["display"])
                        # Signed deterministic difference, fixed 5x gain, neutral0.5.
                        _png(folder / "difference.png", .5 + 5*(pc["expected_linear"]-pb["expected_linear"]))
                        np.savez_compressed(folder / "arrays.npz", **arrays)
                        prefix = f"cases/{identity}/"
                        case["files"] = {k: prefix+v for k, v in dict(
                            baseline_raw="baseline-raw.png", projected_raw="projected-raw.png",
                            baseline_isp="baseline-isp.png", projected_isp="projected-isp.png",
                            difference="difference.png", arrays="arrays.npz", metrics="metrics.json",
                            baseline_raw_array="baseline-raw.npy", projected_raw_array="projected-raw.npy").items()}
                        case["difference_display"] = "0.5 + 5×(projected − baseline), noiseless linear RGB; fixed scale"
                        write_json(folder / "metrics.json", case)
                        written_bytes += sum(p.stat().st_size for p in folder.iterdir()
                                             if p.is_file() and not p.name.startswith("._"))
                        if written_bytes > job["estimated_bytes"] - 100_000_000:
                            raise ValueError("Reserved output budget exceeded; precision unchanged")
                        cases.append(case)
                        write_json(directory / "progress.json", dict(completed=len(cases),
                            total=len(spec["fields"])*len(spec["charts"])*len(spec["light_scales"])*len(spec["profiles"]),
                            stage=field+" · "+chart+" · "+profile))
            # Writer reservation stays held. Check total project allocation per
            # field, while checking this job's reserved bytes per case above.
            check()
        summary = dict(schema=SCHEMA, title=spec["name"], status="assumption_driven_sensitivity_only",
            camera_response_eligible=False, measured_accuracy=False, product_mode=False,
            source=dict(trend_run_id=spec["trend_run_id"], camera_run_id=spec["camera_run_id"],
                camera_candidate_id=spec["camera_candidate_id"], structure_revision=trend["structure"]["revision"],
                host_sensor="IMX662 structure · ASI662MC readout proxy", optical_revision=optics["revision"],
                notes="Source optical mean response reused; fresh integer RAW; 2D qualification not promoted",
                camera_files=refs, numerical_summary_sha256=numerical["summary_sha256"]),
            assumptions=assumptions(), evidence=evidence, cases=cases,
            conditions=dict(exposure_ms=4., gain=1., frames=8, illumination="D65 / 1000 cd/m² × light scale",
                light_scales=spec["light_scales"], display_white_level=1., lsc_enabled=False,
                demosaic="bilinear", color_calibration="fixed source common IMX462 spectral matrix",
                raw_preview="signed electrons / source full-well; fixed scale",
                difference_preview="signed noiseless linear RGB difference, 5×, neutral gray0.5"),
            elapsed_s=time.monotonic()-started)
        write_json(directory / "result.json", summary)
        files = {str(p.relative_to(directory)): sha(p) for p in directory.rglob("*")
                 if p.is_file() and not p.name.startswith("._") and p.name != "progress.json"}
        write_json(directory / "manifest.json", dict(schema=SCHEMA+"_files", files=files))
        summary["manifest_sha256"] = sha(directory / "manifest.json")
        summary["stored_bytes"] = sum((directory / p).stat().st_size for p in files)
        artifacts = [service.registry.store_file(directory / "manifest.json", "manifest.json", "application/json"),
                     service.registry.store_blob(canonical(summary).encode(), "study.json", "application/json")]
        probe.check()
        return dict(summary=summary, artifacts=artifacts)


def get(service, identity):
    job = service.registry.get_run(identity)
    if job.get("job_kind") != KIND:
        raise ValueError("Select an OCL camera sensitivity study")
    if job["status"] == "running":
        p = storage.root(service) / "sensor-structure-camera" / identity / "progress.json"
        if p.exists(): job["progress"] = json.loads(p.read_text())
    return job


def install_routes(app, service):
    from fastapi.responses import FileResponse
    @app.get(ROUTE+"/template", tags=["sensor_structure_camera"])
    def example(): return dict(specification=template())
    @app.get(ROUTE+"/latest", tags=["sensor_structure_camera"])
    def latest():
        jobs = service.registry.list_runs(job_kinds=[KIND])
        done = [j for j in jobs if j["status"] == "succeeded"]
        return dict(id=done[0]["id"] if done else None)
    @app.post(ROUTE, status_code=202, tags=["sensor_structure_camera"])
    def create(payload: dict): return submit(service, payload)
    @app.get(ROUTE+"/{identity}", tags=["sensor_structure_camera"])
    def detail(identity: str): return get(service, identity)
    @app.post(ROUTE+"/{identity}/cancel", tags=["sensor_structure_camera"])
    def cancel(identity: str): get(service, identity); return service.registry.cancel(identity)
    @app.get(ROUTE+"/{identity}/files/{name:path}", tags=["sensor_structure_camera"])
    def file(identity: str, name: str):
        job = get(service, identity)
        if job["status"] != "succeeded": raise ValueError("Results are not complete")
        artifact = next(a for a in job["result"]["artifacts"] if a["name"] == "manifest.json")
        bound = service.registry.artifact_binding(artifact["sha256"], owner_kind="runs", owner_id=identity)
        manifest = json.loads(Path(bound["path"]).read_text())
        if name not in manifest["files"]: raise ValueError("Unknown bound artifact")
        root = (storage.root(service) / "sensor-structure-camera" / identity).resolve()
        p = (root / name).resolve()
        if not p.is_relative_to(root) or sha(p) != manifest["files"][name]:
            raise IntegrityError("Bound artifact changed")
        return FileResponse(p, media_type="image/png" if p.suffix == ".png" else "application/json" if p.suffix == ".json" else "application/octet-stream",
            filename=p.name, content_disposition_type="inline" if p.suffix in (".png", ".json") else "attachment")
