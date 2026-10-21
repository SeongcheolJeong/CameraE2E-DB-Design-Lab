"""R1 engineering manifest must fail closed without bound installed-flow proof."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools/create_camera_platform_r1_release.py"
SPEC = importlib.util.spec_from_file_location("create_camera_platform_r1_release", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def candidate(tmp_path):
    project_id = "camera-r1-test"
    state = tmp_path / "r1-state"
    state.mkdir()
    mode = save(state / "project-mode.json", {
        "schema": release.MODE_SCHEMA, "mode": release.MODE, "project_id": project_id,
    })
    wheel = tmp_path / "camera-r1.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("camera_platform/__init__.py", "__version__ = 'R1rc1'\n")
        archive.writestr("camera_platform/static/index.html", "<title>R1</title>")
        archive.writestr("pyisetcam/__init__.py", "")
        archive.writestr("pyisetcam-0.16.1rc1.dist-info/METADATA",
                         "Metadata-Version: 2.3\nName: pyisetcam\nVersion: 0.16.1rc1\n")
    scope = tmp_path / "support.md"
    scope.write_text("# Frozen R1 support\n", encoding="utf-8")
    payload = save(tmp_path / "runtime-manifest.json", {
        "schema": release.PACKAGE_SCHEMA,
        "wheel_sha256": sha(wheel), "environment": {"python": "3.12"},
        "files": release._wheel_package_hashes(wheel),
    })
    proof = save(tmp_path / "installed-proof.json", {"status": "passed"})
    checks = {}
    for name in release.REQUIRED_CHECKS:
        artifact = payload if name == "release_file_identity" else proof
        checks[name] = {
            "status": "passed", "evidence_path": str(artifact),
            "evidence_sha256": sha(artifact),
        }
    checks["isolated_install"].update(fresh_environment=True,
                                      no_system_site_packages=True, no_checkout_imports=True)
    checks["required_assets"].update(no_qa_dependency=True, no_checkout_dependency=True)
    checks["two_model_scene"].update(model_ids=["lens-a", "lens-b"],
                                     scene_ids=["scene-a", "scene-b"])
    checks["complete_user_journey"]["steps"] = {
        step: "passed" for step in release.JOURNEY_STEPS
    }
    checks["failure_paths"]["cases"] = {
        case: "passed" for case in release.FAILURE_CASES
    }
    checks["performance"].update(
        published_max_input="64x64, five wavelengths, three frames",
        runtime_seconds=2.5, peak_memory_bytes=10_000_000, saved_bytes=20_000,
        list_read_seconds=0.03, list_avoids_raw_recalculation=True,
    )
    checks["release_file_identity"].update(
        wheel_sha256=sha(wheel), installed_payload_manifest_sha256=sha(payload),
    )
    evidence = tmp_path / "installed-evidence.json"
    data = {
        "schema": release.EVIDENCE_SCHEMA, "status": "passed",
        "project_id": project_id, "mode": release.MODE, "state_dir": str(state.resolve()),
        "project_mode_sha256": sha(mode), "wheel_sha256": sha(wheel),
        "support_doc_sha256": sha(scope), "physical_validation": "not_established",
        "installed_payload_manifest_path": str(payload),
        "installed_payload_manifest_sha256": sha(payload), "checks": checks,
    }
    save(evidence, data)
    return {"project_id": project_id, "state_dir": state, "wheel": wheel,
            "install_evidence": evidence, "support_doc": scope}


def build(candidate):
    return release.build_manifest(**candidate)


def codes(manifest):
    return {item["code"] for item in manifest["blockers"]}


def test_complete_bound_candidate_is_rc_ready_but_never_approved(candidate):
    manifest = build(candidate)
    assert manifest["status"] == "rc_ready"
    assert manifest["approval_status"] == "unapproved"
    assert manifest["physical_validation"] == "not_established"
    assert manifest["uncertainty"] == {"kind": "unknown"}
    assert manifest["files"]["wheel"]["sha256"] == sha(candidate["wheel"])
    assert manifest["files"]["install_evidence"]["sha256"] == sha(
        candidate["install_evidence"]
    )
    assert manifest["files"]["support_doc"]["sha256"] == sha(candidate["support_doc"])
    assert not manifest["blockers"]


def test_rc2_wheel_requires_installed_replay_and_selected_mode_gates(candidate):
    wheel = candidate["wheel"]
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("camera_platform/__init__.py", "__version__ = '0.16.1rc2'\n")
        archive.writestr("camera_platform/static/index.html", "<title>R1 RC2</title>")
        archive.writestr("pyisetcam/__init__.py", "")
        archive.writestr("pyisetcam-0.16.1rc2.dist-info/METADATA",
                         "Metadata-Version: 2.3\nName: pyisetcam\nVersion: 0.16.1rc2\n")
    payload_path = Path(json.loads(candidate["install_evidence"].read_text())[
        "installed_payload_manifest_path"])
    payload = json.loads(payload_path.read_text())
    payload["wheel_sha256"] = sha(wheel)
    payload["files"] = release._wheel_package_hashes(wheel)
    save(payload_path, payload)
    evidence = json.loads(candidate["install_evidence"].read_text())
    evidence["wheel_sha256"] = sha(wheel)
    evidence["installed_payload_manifest_sha256"] = sha(payload_path)
    evidence["checks"]["release_file_identity"].update(
        wheel_sha256=sha(wheel),
        installed_payload_manifest_sha256=sha(payload_path),
        evidence_sha256=sha(payload_path),
    )
    save(candidate["install_evidence"], evidence)
    assert release._wheel_version(wheel) == "0.16.1rc2"
    blocked = build(candidate)
    assert blocked["status"] == "blocked"
    assert sum(entry["code"] == "required_check_missing" for entry in blocked["blockers"]) == 2
    proof = candidate["install_evidence"].parent / "installed-proof.json"
    for name in release.RC2_REQUIRED_CHECKS:
        evidence["checks"][name] = {
            "status": "passed", "evidence_path": str(proof),
            "evidence_sha256": sha(proof),
        }
    evidence["checks"]["design_isp_replay"].update(
        input_unchanged=True, no_new_captures=True, rgb_delta_observed=True,
        browser_replay=True, saved_reopen=True,
    )
    evidence["checks"]["selected_temporal_hdr_dcg"].update(
        registered_source=True, numerical_bridge=True, browser_hdr=True,
        modes={key: "passed" for key in ("temporal", "hdr", "dcg")},
    )
    save(candidate["install_evidence"], evidence)
    assert build(candidate)["status"] == "rc_ready"
    evidence["checks"]["selected_temporal_hdr_dcg"]["modes"]["dcg"] = "blocked"
    save(candidate["install_evidence"], evidence)
    assert "required_check_unbound" in codes(build(candidate))


@pytest.mark.parametrize("change,expected", [
    ("wheel_change", "install_binding_mismatch"),
    ("scope_change", "install_binding_mismatch"),
    ("project_change", "project_mode_unbound"),
    ("missing_failure", "required_check_unbound"),
    ("forged_artifact_hash", "required_check_unbound"),
    ("missing_payload", "installed_payload_unbound"),
    ("physical_claim", "physical_claim_mismatch"),
])
def test_mismatch_or_incomplete_gate_blocks_without_claiming_release(candidate, change, expected):
    data = json.loads(candidate["install_evidence"].read_text())
    if change == "wheel_change":
        with zipfile.ZipFile(candidate["wheel"], "a") as archive:
            archive.writestr("camera_platform/static/app.js", "new bytes")
    elif change == "scope_change":
        candidate["support_doc"].write_text("# Different scope\n")
    elif change == "project_change":
        marker = candidate["state_dir"] / "project-mode.json"
        value = json.loads(marker.read_text())
        value["project_id"] = "another-project"
        save(marker, value)
    elif change == "missing_failure":
        del data["checks"]["failure_paths"]["cases"]["save_failure"]
    elif change == "forged_artifact_hash":
        data["checks"]["complete_user_journey"]["evidence_sha256"] = "0" * 64
    elif change == "missing_payload":
        data["installed_payload_manifest_sha256"] = "0" * 64
    elif change == "physical_claim":
        data["physical_validation"] = "passed"
    save(candidate["install_evidence"], data)
    manifest = build(candidate)
    assert manifest["status"] == "blocked"
    assert manifest["approval_status"] == "unapproved"
    assert manifest["physical_validation"] == "not_established"
    assert expected in codes(manifest)


def test_missing_evidence_and_default_state_cannot_be_ready(candidate, tmp_path):
    candidate["install_evidence"] = tmp_path / "not-produced.json"
    manifest = build(candidate)
    assert manifest["status"] == "blocked"
    assert "install_evidence_missing" in codes(manifest)
    candidate["state_dir"] = release.DEFAULT_STATE
    manifest = build(candidate)
    assert "r1_state_not_separate" in codes(manifest)


def test_cli_writes_blocked_record_once_and_refuses_overwrite(candidate, tmp_path):
    candidate["install_evidence"] = tmp_path / "not-produced.json"
    output = tmp_path / "release.json"
    args = ["--project-id", candidate["project_id"], "--state-dir",
            str(candidate["state_dir"]), "--wheel", str(candidate["wheel"]),
            "--install-evidence", str(candidate["install_evidence"]),
            "--support-doc", str(candidate["support_doc"]), "--output", str(output)]
    assert release.main(args) == 2
    first = output.read_bytes()
    assert json.loads(first)["status"] == "blocked"
    with pytest.raises(SystemExit):
        release.main(args)
    assert output.read_bytes() == first
