"""Focused safety checks for the installed R1 evidence collector."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "r1_installed_verifier", ROOT / "tools/verify_camera_platform_r1_installed.py"
)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def make_wheel(tmp_path):
    wheel = tmp_path / "camera.whl"
    files = {
        "camera_platform/__init__.py": b"installed package\n",
        "camera_platform/static/index.html": b"<html>installed UI</html>",
        "camera_platform/data/r1/manifest.json": b"{}",
        "pyisetcam/__init__.py": b"sensor package\n",
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("pyisetcam-0.16.1rc2.dist-info/METADATA",
                         "Metadata-Version: 2.3\nName: pyisetcam\nVersion: 0.16.1rc2\n")
    purelib = tmp_path / "venv/lib/python3.12/site-packages"
    for name, content in files.items():
        path = purelib / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return wheel, files, purelib


def test_complete_installed_payload_and_tamper_detection(tmp_path):
    wheel, files, purelib = make_wheel(tmp_path)
    assert verifier.wheel_version(wheel) == "0.16.1rc2"
    assert verifier.required_checks_for_version("0.16.1rc2") == (
        verifier.CHECKS + verifier.RC2_CHECKS
    )
    assert len(verifier.wheel_package_files(wheel)) == len(files)
    manifest_path = tmp_path / "installed-payload.json"
    payload, proof = verifier.installed_payload(
        wheel,
        {"purelib": str(purelib), "prefix": str(tmp_path / "venv"), "version": "0.1"},
        manifest_path,
    )
    assert proof["changed"] == proof["missing"] == proof["extra"] == []
    assert (
        payload["files"]["camera_platform/static/index.html"]
        == hashlib.sha256(files["camera_platform/static/index.html"]).hexdigest()
    )
    (purelib / "camera_platform/static/index.html").write_text("tampered")
    with pytest.raises(ValueError, match="complete wheel payload"):
        verifier.installed_payload(
            wheel,
            {"purelib": str(purelib), "prefix": "x", "version": "0.1"},
            tmp_path / "never.json",
        )


def test_numerical_behavior_checks_gain_exposure_and_scale():
    rows = [
        {
            "split": "train",
            "exposure_ms": exposure,
            "gain": gain,
            "radiance_scale": scale,
            "green_photocharge_mean_e": exposure * scale * 23.0,
        }
        for exposure in (1.0, 2.0)
        for gain in (1.0, 2.0)
        for scale in (1.0, 2.0)
    ]
    assert all(verifier.numerical_behavior(rows).values())
    rows[-1]["green_photocharge_mean_e"] *= 1.25
    assert not all(verifier.numerical_behavior(rows).values())


def test_integer_raw_descriptor_accepts_the_installed_int32_code_container():
    assert verifier.integer_raw_dtype("int32")
    assert verifier.integer_raw_dtype("uint16")
    assert not verifier.integer_raw_dtype("float32")
    assert not verifier.integer_raw_dtype("object")


def test_installed_npy_identity_checks_header_shape_and_complete_payload():
    stream = io.BytesIO()
    np.save(stream, np.zeros((4, 16, 16), dtype=np.int32), allow_pickle=False)
    payload = stream.getvalue()
    assert verifier.npy_identity(payload) == {"shape": [4, 16, 16], "dtype": "int32"}
    with pytest.raises(ValueError, match="payload length"):
        verifier.npy_identity(payload[:-1])


def test_package_proof_requires_exact_wheel_hash_and_core_claims(tmp_path):
    proof = tmp_path / "core.json"
    data = {
        "schema": "camera_platform_r1_package_proof_v1",
        "status": "passed",
        "wheel_sha256": "a" * 64,
        "all_checked_source_static_bytes_match": True,
        "source_static_file_count": 12,
        "temporary_target_removed": True,
        "install": {"no_deps": True},
        "physical_validation": "not_established",
        "environment": {"platform_version": "test"},
        "registry_integrity": "verified",
        "raw_and_rgb_artifact_http_hashes_verified": True,
        "r1_registered_design_job_succeeded": True,
        "r1_integer_raw_and_float64_rgb_verified": True,
        "r1_fixed_isp_consumed_original_raw": True,
        "r1_selection_frozen_before_evaluation": True,
        "r1_saved_result_reopened": True,
        "r1_workbench_static_http_matches_package": True,
    }
    proof.write_text(json.dumps(data))
    assert verifier.package_proof_record(proof, "a" * 64)["status"] == "passed"
    assert verifier.package_proof_record(proof, "b" * 64)["status"] == "blocked"
    data["schema"] = "legacy_broad_package_proof"
    proof.write_text(json.dumps(data))
    assert verifier.package_proof_record(proof, "a" * 64)["status"] == "blocked"
    data["schema"] = "camera_platform_r1_package_proof_v1"
    data["r1_fixed_isp_consumed_original_raw"] = False
    proof.write_text(json.dumps(data))
    assert verifier.package_proof_record(proof, "a" * 64)["status"] == "blocked"
    assert verifier.package_proof_record(None, "a" * 64)["status"] == "blocked"


def test_evidence_does_not_promote_unobserved_checks(tmp_path):
    evidence = verifier.Evidence(tmp_path)
    evidence.check("isolated_install", True, {"observed": True})
    evidence.fill_blocked()
    assert len(evidence.checks) == len(verifier.CHECKS)
    assert evidence.checks["failure_paths"]["status"] == "blocked"
    path = Path(evidence.checks["isolated_install"]["evidence_path"])
    assert verifier.sha(path) == evidence.checks["isolated_install"]["evidence_sha256"]
    rc2 = verifier.Evidence(tmp_path / "rc2", verifier.CHECKS + verifier.RC2_CHECKS)
    rc2.fill_blocked()
    assert all(rc2.checks[name]["status"] == "blocked" for name in verifier.RC2_CHECKS)


def test_harnesses_parse_and_maximum_grid_is_bounded():
    compile(verifier.NUMERICAL_PROBE, "numerical_probe", "exec")
    compile(verifier.R1_BRIDGE_PROBE, "r1_bridge_probe", "exec")
    compile(verifier.FAULT_HARNESS, "fault_harness", "exec")
    template = {
        "specification": {
            "schema": "camera_design_experiment_v2",
            "name": "base",
            "model_ids": ["model-a"],
            "scene_id": "scene-a",
            "exposures_ms": [1.0],
            "gains": [1.0],
            "training_scales": [1.0],
            "evaluation_scales": [1.0],
            "evaluation_noise_repeats": 0,
            "seed": 1,
            "limits": {},
            "acknowledge_simulation_limits": False,
        }
    }
    spec = verifier.maximum_spec(template, ["model-a", "model-b", "model-c"], "scene-a", "maximum")
    assert len(spec["model_ids"]) * len(spec["exposures_ms"]) * len(spec["gains"]) == 36
    assert (
        36 * len(spec["training_scales"])
        + len(spec["evaluation_scales"]) * (1 + spec["evaluation_noise_repeats"])
        == 123
    )
    assert verifier.maximum_reachable_captures() == 123


def test_design_support_scope_requires_document_ui_browser_and_registered_axis():
    doc = (
        "R1 RC의 정식 지원 선언 대상은 등록된 16×16·500, 600 nm "
        "설계 실험 경로 하나입니다. 최대 36후보·최대 128 captures. "
        "기본 공간·시간/HDR/DCG는 후속 파일럿입니다."
    )
    ui = (
        "R1 설계 실험 범위: 등록된 16×16·500/600 nm 정적 예제, "
        "최대 36개 후보·128회 취득, 고정 ISP. "
        "움직임·HDR/LFM 결합과 실물 예측 정확도 검증은 포함되지 않습니다."
    )
    template = {
        "maximum_captures": 128,
        "specification": {"schema": "camera_design_experiment_v2"},
        "limitation": "physical prediction accuracy is not established",
    }
    browser = {
        "status": "passed",
        "contract_schema": "camera_design_experiment_v2",
        "new_captures": 5,
        "checks": ["R1 browser states its spectral, capture and physical limits"],
    }
    job = {"result": {"summary": {"physical_validation": "not_established"}}}
    values = dict(
        doc=doc,
        ui=ui,
        template=template,
        browser=browser,
        browser_job=job,
        wavelength_nm=[500, 600],
    )
    assert verifier.design_support_consistency(**values)["passed"] is True
    values["doc"] = doc.replace("설계 실험 경로 하나", "모든 카메라 경로")
    assert verifier.design_support_consistency(**values)["passed"] is False
    values["doc"] = doc
    values["browser"]["checks"] = []
    assert verifier.design_support_consistency(**values)["passed"] is False


def test_registered_pixel_and_electrical_variants_change_real_raw(tmp_path):
    from camera_platform import design_experiments as design
    from camera_platform.project_mode import configure
    from camera_platform.r1_design_assets import catalog
    from camera_platform.service import PlatformService

    state = tmp_path / "state"
    configure(state, project_id="r1-component-test")
    service = PlatformService(state, tmp_path)
    registered = catalog(tmp_path)
    selected = verifier.component_variant_ids(registered)
    assert set(selected) == {"base", "pixel", "electrical", "pixel_electrical"}
    template = {"specification": design.default_spec(registered), "catalog": registered}

    class LocalAPI:
        def post(self, _path, spec):
            queued = design.submit(service, spec)
            running = service.registry.claim()
            assert running["id"] == queued["id"]
            service.registry.finish(running["id"], result=service.execute_run(running))
            return queued

        def get(self, path):
            return design.get(service, path.rsplit("/", 1)[-1])

        def bytes(self, path):
            return service.registry.blob_path(path.rsplit("/", 1)[-1]).read_bytes()

    api = LocalAPI()
    base_spec = verifier.spec_from_template(
        template, selected["base"], registered["scenes"][0]["id"], "r1-base"
    )
    base = api.get(
        "/api/v1/design-experiments/" + api.post("/api/v1/design-experiments", base_spec)["id"]
    )
    result = verifier.verify_component_variants(api, template, registered, base, tmp_path)
    assert result["passed"] is True
    assert all(
        item["raw_changed_from_base"] and item["identity_bound"] for item in result["jobs"].values()
    )


def test_stop_server_terminates_its_process_group():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    verifier.stop_server(process)
    assert process.poll() is not None
