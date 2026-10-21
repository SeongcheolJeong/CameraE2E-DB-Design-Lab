"""Installed code boundaries, tampering, environment drift and version selection."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
SPEC = importlib.util.spec_from_file_location(
    "installed_launcher", TOOLS / "start_camera_platform.py"
)
launcher = importlib.util.module_from_spec(SPEC)
with pytest.MonkeyPatch.context() as import_context:
    import_context.syspath_prepend(str(TOOLS))
    SPEC.loader.exec_module(launcher)


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "prepared"
    environment = {"platform_version": "fixture", "source_hashes": {"fixture": "1" * 64}}
    for name in ("camera_platform", "pyisetcam"):
        (root / name).mkdir(parents=True)
        (root / name / "__init__.py").write_text("")
    (root / "camera_platform/service.py").write_text(
        "def environment_fingerprint():\n    return " + repr(environment) + "\n"
    )
    (root / "camera_platform/__main__.py").write_text(
        "def main():\n    raise RuntimeError('must not start unverified code')\n"
    )
    static = root / "camera_platform/static"
    static.mkdir()
    (static / "app.js").write_text("/* fixture */")
    manifest = {
        "schema": launcher.PACKAGE_SCHEMA,
        "wheel_sha256": "0" * 64,
        "environment": environment,
        "files": launcher.package_files(root),
    }
    (root / "runtime-manifest.json").write_text(json.dumps(manifest))
    return root


@pytest.mark.parametrize("change", ["code", "static", "extra", "missing", "symlink", "fifo"])
def test_modified_payload_refused_before_running(package, tmp_path, change):
    state = tmp_path / "state"
    launcher.save_runtime_pin(state, package)
    if change == "code":
        (package / "camera_platform/service.py").write_text("raise RuntimeError('changed')")
    elif change == "static":
        (package / "camera_platform/static/app.js").write_text("changed")
    elif change == "extra":
        (package / "unexpected.bin").write_bytes(b"new data")
    elif change == "missing":
        (package / "pyisetcam/__init__.py").unlink()
    elif change == "symlink":
        (package / "alias").symlink_to(tmp_path)
    else:
        os.mkfifo(package / "pipe")
    with pytest.raises(RuntimeError, match="changed|symbolic|non-regular"):
        launcher.load_installed_runtime(state)


def test_changed_manifest_cannot_replace_the_pinned_environment(package, tmp_path):
    state = tmp_path / "state"
    launcher.save_runtime_pin(state, package)
    path = package / "runtime-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["environment"]["platform_version"] = "different"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="pinned checksum"):
        launcher.load_installed_runtime(state)


def test_probe_ignores_checkout_and_pythonpath(package, tmp_path, monkeypatch):
    source = tmp_path / "development"
    source.mkdir()
    (source / "camera_platform.py").write_text("raise RuntimeError('checkout imported')")
    monkeypatch.chdir(source)
    monkeypatch.setenv("PYTHONPATH", str(source))
    actual = launcher.probe_package(sys.executable, package)
    assert actual["module_paths"]["camera_platform"] == str(package / "camera_platform/__init__.py")
    assert actual["environment"] == launcher.verify_manifest(package)["environment"]
    assert not list(package.rglob("__pycache__"))


def test_dependency_drift_prevents_start(package, tmp_path, monkeypatch):
    state = tmp_path / "state"
    launcher.save_runtime_pin(state, package)
    monkeypatch.setattr(launcher, "WORKSPACE", tmp_path)
    monkeypatch.setattr(launcher, "ready", lambda *args: False)
    monkeypatch.setattr(launcher, "probe_package", lambda *args: {"environment": {"changed": True}})
    monkeypatch.setattr(
        launcher.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("started")
    )
    with pytest.raises(RuntimeError, match="dependencies or Python changed"):
        launcher.main(["--state-dir", str(state), "--no-open"])
    assert not (state / "server.pid").exists()


def test_selection_preserves_previous_pin_without_restarting(package, tmp_path, monkeypatch):
    import shutil

    state = tmp_path / "state"
    first = launcher.save_runtime_pin(state, package)
    (state / "server.pid").write_text("12345\n")
    other = tmp_path / "other-prepared"
    shutil.copytree(package, other)
    monkeypatch.setattr(
        launcher.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("started")
    )
    monkeypatch.setattr(launcher.os, "kill", lambda *args: pytest.fail("interrupted"))
    second = launcher.save_runtime_pin(state, other)
    assert second != first
    assert (state / "server.pid").read_text() == "12345\n"
    assert json.loads(next((state / "runtime-pin-history").glob("*.json")).read_text()) == first
    launcher.save_runtime_pin(state, package)
    assert launcher.load_installed_runtime(state)["runtime_root"] == str(package)


@pytest.mark.parametrize("invalid", [[], {}, {"schema": "unknown"}])
def test_invalid_manifest_never_overwrites_existing_pin(package, tmp_path, invalid):
    state = tmp_path / "state"
    launcher.save_runtime_pin(state, package)
    original = (state / "installed-runtime.json").read_bytes()
    (package / "runtime-manifest.json").write_text(json.dumps(invalid))
    with pytest.raises(RuntimeError, match="Unsupported"):
        launcher.save_runtime_pin(state, package)
    assert (state / "installed-runtime.json").read_bytes() == original


def test_bootstrap_checks_are_not_disabled_by_optimized_python(package, tmp_path):
    state = tmp_path / "state"
    pin = launcher.save_runtime_pin(state, package)
    (package / "camera_platform/static/app.js").write_text("tampered after preflight")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-O",
            "-B",
            "-c",
            launcher.INSTALLED_BOOTSTRAP,
            str(package),
            str(state),
            pin["manifest_sha256"],
            "serve",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "Runtime payload changed" in result.stderr
    assert "must not start unverified code" not in result.stderr
    assert not (state / "runtime-process.json").exists()
