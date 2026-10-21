"""Installed case qualifier boundaries; no primary, wheel install or server activation."""

import hashlib
import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
try:
    spec = importlib.util.spec_from_file_location(
        "case_runtime", TOOLS / "verify_camera_case_runtime.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
finally:
    sys.path.remove(str(TOOLS))


@pytest.fixture
def package(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    files = {
        "camera_platform/__init__.py": b"# camera fixture\n",
        "camera_platform/case_contracts.py": b"# contract fixture\n",
        "camera_platform/case_reviews.py": b"# review fixture\n",
        "camera_platform/static/cases.js": b"// installed case UI\n",
        "pyisetcam/__init__.py": b"# imaging fixture\n",
        "pyisetcam-0.1.0.dist-info/METADATA": b"Name: pyisetcam\nVersion: 0.1.0\n",
        "pyisetcam-0.1.0.dist-info/RECORD": b"wheel record\n",
    }
    wheel = tmp_path / "pyisetcam-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
            target = runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
    manifest = {
        "schema": tool.launcher.PACKAGE_SCHEMA,
        "wheel_sha256": tool.sha(wheel),
        "environment": {
            "platform_version": "0.15.0",
            "packages": {"numpy": "fixture"},
            "source_hashes": {
                name: hashlib.sha256(value).hexdigest()
                for name, value in files.items()
                if name.endswith(".py")
            },
        },
        "files": tool.launcher.package_files(runtime),
    }
    (runtime / "runtime-manifest.json").write_text(json.dumps(manifest))
    return wheel, runtime, manifest


def rewrite_manifest(runtime, manifest):
    (runtime / "runtime-manifest.json").write_text(json.dumps(manifest))


def verifier(tmp_path, package):
    wheel, runtime, _ = package
    return tool.Verification(
        tmp_path / "evidence", wheel=wheel, runtime_root=runtime, python=Path(sys.executable)
    )


def test_every_code_data_static_and_source_hash_is_bound_to_exact_wheel(package):
    wheel, runtime, manifest = package
    actual, proof = tool.verify_inputs(wheel, runtime)
    assert actual == manifest
    assert proof["wheel_members_compared"] == 6
    assert len(proof["package_payload_sha256"]) == 5
    assert proof["source_hashes"] == manifest["environment"]["source_hashes"]


@pytest.mark.parametrize(
    "name",
    [
        "camera_platform/case_reviews.py",
        "camera_platform/static/cases.js",
        "pyisetcam/__init__.py",
        "pyisetcam-0.1.0.dist-info/METADATA",
    ],
)
def test_even_rehashed_installed_changes_cannot_replace_wheel_contents(package, name):
    wheel, runtime, manifest = package
    (runtime / name).write_bytes(b"changed")
    manifest["files"] = tool.launcher.package_files(runtime)
    rewrite_manifest(runtime, manifest)
    with pytest.raises(RuntimeError, match="differs"):
        tool.verify_inputs(wheel, runtime)


def test_extra_installed_product_module_is_not_silently_ignored(package):
    wheel, runtime, manifest = package
    (runtime / "camera_platform/extra.py").write_text("# unexpected")
    manifest["files"] = tool.launcher.package_files(runtime)
    rewrite_manifest(runtime, manifest)
    with pytest.raises(RuntimeError, match="full wheel payload"):
        tool.verify_inputs(wheel, runtime)


@pytest.mark.parametrize("fault", ["wheel", "version", "source"])
def test_wrong_wheel_version_or_source_identity_refuses_admission(package, fault):
    wheel, runtime, manifest = package
    if fault == "wheel":
        manifest["wheel_sha256"] = "0" * 64
    elif fault == "version":
        manifest["environment"]["platform_version"] = "0.14.0"
    else:
        manifest["environment"]["source_hashes"]["camera_platform/case_reviews.py"] = "0" * 64
    rewrite_manifest(runtime, manifest)
    with pytest.raises(RuntimeError):
        tool.verify_inputs(wheel, runtime)


def test_unsafe_duplicate_wheel_members_are_rejected(package):
    wheel, runtime, manifest = package
    with pytest.warns(UserWarning), zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("camera_platform/__init__.py", b"duplicate")
    manifest["wheel_sha256"] = tool.sha(wheel)
    rewrite_manifest(runtime, manifest)
    with pytest.raises(RuntimeError, match="Unsafe"):
        tool.verify_inputs(wheel, runtime)


def test_runtime_symlink_refused_without_resolving_away_identity(tmp_path, package):
    wheel, runtime, _ = package
    linked = tmp_path / "linked-runtime"
    linked.symlink_to(runtime, target_is_directory=True)
    with pytest.raises(RuntimeError, match="canonical"):
        tool.verify_inputs(wheel, linked)


@pytest.mark.parametrize("target", ["existing", "runtime", "state", "symlink_parent"])
def test_outputs_never_overwrite_protected_or_existing_paths(tmp_path, package, target):
    wheel, runtime, _ = package
    output = tmp_path / "out"
    if target == "existing":
        output.mkdir()
        (output / "original.json").write_text("unchanged")
    elif target == "runtime":
        output = runtime / "new-evidence"
    elif target == "state":
        state = tmp_path / "existing-state"
        state.mkdir()
        (state / "registry.sqlite").write_bytes(b"preserved")
        output = state / "new-evidence"
    else:
        link = tmp_path / "linked-parent"
        link.symlink_to(runtime, target_is_directory=True)
        output = link / "new-evidence"
    with pytest.raises(RuntimeError):
        tool.Verification(output, wheel=wheel, runtime_root=runtime, python=Path(sys.executable))
    if target == "existing":
        assert (output / "original.json").read_text() == "unchanged"


def test_cleanup_failure_preserves_original_failure_and_owned_process_identity(tmp_path, package):
    value = verifier(tmp_path, package)
    value.owner = SimpleNamespace(
        process=SimpleNamespace(pid=12345),
        cleanup=Mock(side_effect=PermissionError("cleanup denied")),
    )
    value.record_failure(ValueError("original startup error"))
    report = json.loads((value.output / "verification.json").read_text())
    assert report["status"] == "failed" and "original startup error" in report["error"]
    assert report["cleanup"]["process_group"] == 12345
    assert report["cleanup"]["process_group_stopped"] is False
    assert "PermissionError" in report["cleanup"]["error"]


@pytest.mark.parametrize("phase", ["initial", "restart"])
def test_failed_owned_launch_stops_actual_process_group(tmp_path, package, phase):
    value = verifier(tmp_path, package)
    value.log = (value.output / "test-process.log").open("wb")
    command = [sys.executable, "-I", "-c", "import time; time.sleep(30)"]
    if phase == "restart":
        previous = value.owner.spawn(command, cwd=tmp_path, env=os.environ.copy(), log=value.log)
        assert value.owner.cleanup()["process_group_stopped"]
        assert previous.poll() is not None
    current = value.owner.spawn(command, cwd=tmp_path, env=os.environ.copy(), log=value.log)
    try:
        value.record_failure(RuntimeError("readiness failed"))
        assert current.poll() is not None
        assert value.report["cleanup"]["process_group_stopped"]
        assert not [p for p in tool.processes() if p["pgid"] == current.pid]
    finally:
        value.owner.cleanup()
        value.log.close()


def test_restart_requires_confirmed_group_absence_before_new_launch(tmp_path, package):
    value = verifier(tmp_path, package)
    value.owner = SimpleNamespace(
        process=SimpleNamespace(pid=12345), cleanup=lambda: {"process_group_stopped": False}
    )
    with pytest.raises(RuntimeError, match="not confirmed stopped"):
        value.stop_for_restart()


def test_probe_rejects_another_interpreter_or_checkout_modules(tmp_path, package, monkeypatch):
    value = verifier(tmp_path, package)
    value.workspace = tmp_path
    value.manifest = package[2]
    observed = {
        "environment": package[2]["environment"],
        "python": str(value.python),
        "isolated": 1,
        "module_paths": {"camera_platform": str(tool.ROOT / "src/camera_platform/__init__.py")},
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(observed), stderr="")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="outside installed"):
        value.probe("bad-probe")
    assert calls[0][0][1:4] == ["-I", "-B", "-c"]
    assert calls[0][1]["cwd"] == tmp_path
    observed["module_paths"] = {
        "camera_platform": str(value.runtime / "camera_platform/__init__.py")
    }
    observed["python"] = "/another/python"
    with pytest.raises(RuntimeError, match="interpreter differs"):
        value.probe("other-python")


def test_wrong_served_static_cannot_claim_installed_ui_identity(tmp_path, package):
    value = verifier(tmp_path, package)
    value.manifest, _ = tool.verify_inputs(*package[:2])
    value.url = "http://127.0.0.1:9000"
    value.client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"other UI"))
    )
    try:
        with pytest.raises(RuntimeError, match="Served static"):
            value.static_http()
    finally:
        value.client.close()


def test_changed_helper_blocks_before_any_spawn(tmp_path, package, monkeypatch):
    value = verifier(tmp_path, package)
    value.owner = SimpleNamespace(spawn=Mock())
    monkeypatch.setattr(tool, "source_files", lambda: {"changed": "source"})
    with pytest.raises(RuntimeError, match="source changed"):
        value.start()
    value.owner.spawn.assert_not_called()


def test_post_intention_is_durable_when_response_is_lost(tmp_path, package):
    value = verifier(tmp_path, package)
    value.token = "test-token"
    value.client = SimpleNamespace(post=Mock(side_effect=httpx.ReadError("response lost")))
    payload = {"request_id": "stable-request-123", "question": "retained draft"}
    with pytest.raises(httpx.ReadError):
        value.post("/case_reviews", payload, 201)
    report = json.loads((value.output / "verification.json").read_text())
    assert report["post_intents"] == [
        {
            "path": "/case_reviews",
            "payload": payload,
            "expected_status": 201,
            "state": "pending",
            "at": report["post_intents"][0]["at"],
        }
    ]
    assert report["post_count"] == 0
