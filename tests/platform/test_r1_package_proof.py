"""The R1 package proof must bind real wheel bytes and installed observations."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "r1_package_proof", ROOT / "tools/verify_camera_platform_r1_package.py"
)
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


def fixture_wheel(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "ROOT", tmp_path)
    files = {name: name.encode() for name in proof.REQUIRED}
    for name, content in files.items():
        source = tmp_path / "src" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
    wheel = tmp_path / "camera.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel, files


def test_wheel_source_bytes_must_match_all_shipped_package_files(tmp_path, monkeypatch):
    wheel, files = fixture_wheel(tmp_path, monkeypatch)
    report = proof.source_bytes_match_wheel(wheel)
    assert report["all_checked_source_static_bytes_match"] is True
    assert report["source_static_file_count"] == len(files)
    (tmp_path / "src" / "camera_platform/static/design-experiments.js").write_text("changed")
    report = proof.source_bytes_match_wheel(wheel)
    assert report["all_checked_source_static_bytes_match"] is False
    assert report["changed_source"] == ["camera_platform/static/design-experiments.js"]


def test_wheel_rejects_traversal_and_duplicate_members(tmp_path, monkeypatch):
    wheel, files = fixture_wheel(tmp_path, monkeypatch)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("../escape", b"bad")
    with pytest.raises(ValueError, match="unsafe"):
        proof.source_bytes_match_wheel(wheel)
    wheel2 = tmp_path / "duplicate.whl"
    with zipfile.ZipFile(wheel2, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("camera_platform/__init__.py", b"other")
    with pytest.raises(ValueError, match="unsafe"):
        proof.source_bytes_match_wheel(wheel2)


def test_child_proof_requires_r1_specific_installed_observations(tmp_path):
    target = tmp_path / "installed"
    report = {
        "schema": "camera_platform_r1_installed_child_v1",
        "status": "passed",
        "registry_integrity": "verified",
        "physical_validation": "not_established",
        "new_captures": 5,
        "environment": {"python": "test"},
        "installed_module_paths": {
            "camera_platform": str(target / "camera_platform/__init__.py"),
            "pyisetcam": str(target / "pyisetcam/__init__.py"),
        },
    }
    for name in (
        "r1_registered_design_job_succeeded",
        "r1_integer_raw_and_float64_rgb_verified",
        "r1_fixed_isp_consumed_original_raw",
        "r1_selection_frozen_before_evaluation",
        "r1_saved_result_reopened",
        "r1_workbench_static_http_matches_package",
        "raw_and_rgb_artifact_http_hashes_verified",
    ):
        report[name] = True
    assert proof.accepted_child(report, target)
    report["r1_fixed_isp_consumed_original_raw"] = False
    assert not proof.accepted_child(report, target)
    report["r1_fixed_isp_consumed_original_raw"] = True
    report["installed_module_paths"]["camera_platform"] = str(tmp_path / "checkout/__init__.py")
    assert not proof.accepted_child(report, target)


def test_installed_child_code_parses():
    compile(proof.CHILD, "r1_installed_child", "exec")
