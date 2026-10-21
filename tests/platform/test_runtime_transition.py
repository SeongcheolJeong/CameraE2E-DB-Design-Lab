"""Transition admission races preserve original history and newly accepted work."""

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

from camera_platform.registry import SQLiteRegistry


@pytest.fixture
def transition(monkeypatch):
    tools = Path(__file__).resolve().parents[2] / "tools"
    monkeypatch.syspath_prepend(str(tools))
    spec = importlib.util.spec_from_file_location(
        "runtime_transition", tools / "upgrade_camera_platform_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_requests_after_startup_do_not_force_rollback(tmp_path, transition):
    registry = SQLiteRegistry(tmp_path)
    original = registry.put("assets", {"name": "Original immutable acquisition"})
    before = transition.snapshot(tmp_path)
    registry.put("assets", {"name": "New request after the new API starts"})
    job = registry.submit({"kind": "admitted_after_startup"}, "new-work")
    with pytest.raises(RuntimeError, match="Active jobs"):
        transition.snapshot(tmp_path)
    after = transition.snapshot(tmp_path, require_idle=False)
    transition.verify_preserved(before, after)
    assert registry.get("assets", original["id"]) == original
    assert registry.get_run(job["id"])["status"] == "queued"


def test_missing_or_changed_original_rows_are_not_hidden_by_new_work(tmp_path, transition):
    registry = SQLiteRegistry(tmp_path)
    record = registry.put("assets", {"name": "Original"})
    before = transition.snapshot(tmp_path)
    registry.put("assets", {"name": "New"})
    after = transition.snapshot(tmp_path)
    altered = deepcopy(after)
    altered["records"][record["id"]] = "different-digest"
    with pytest.raises(RuntimeError, match="original records"):
        transition.verify_preserved(before, altered)
    missing = deepcopy(after)
    del missing["audit"][next(iter(before["audit"]))]
    with pytest.raises(RuntimeError, match="original records"):
        transition.verify_preserved(before, missing)


@pytest.mark.parametrize("previous", [None, "/previous/renderer-python"])
@pytest.mark.parametrize("failure", [False, True])
def test_new_renderer_binding_never_leaks_into_rollback_environment(
    tmp_path, transition, monkeypatch, previous, failure
):
    key = transition.launcher.MITSUBA_ENV
    if previous is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, previous)
    observed = []

    def launch(options):
        observed.append((options, transition.os.environ.get(key)))
        if failure:
            raise RuntimeError("new startup failure")

    monkeypatch.setattr(transition.launcher, "main", launch)
    if failure:
        with pytest.raises(RuntimeError, match="new startup failure"):
            transition.launch_bound(tmp_path, 8775, "/new/venv/bin/python")
    else:
        transition.launch_bound(tmp_path, 8775, "/new/venv/bin/python")
    assert observed[0][1] == "/new/venv/bin/python"
    assert transition.os.environ.get(key) == previous


@pytest.fixture
def preparation_evidence(tmp_path):
    environment = {"platform_version": "0.12.0", "source_hashes": {"scene_camera.py": "a" * 64}}
    backend = {"executable": "/solver/bin/python", "versions": {"mitsuba": "3.9.1"}}
    artifacts = {
        "render": {
            name: "b" * 64
            for name in ("spectral-radiance.npy", "spectral-image.npy", "first-hit-depth-m.npy")
        },
        "camera": {name: "c" * 64 for name in ("raw_frames.npy", "rgb_frames.npy")},
    }
    proof = {
        "schema": "camera_scene_preparation_verification_v1",
        "status": "passed",
        "state_dir": str(tmp_path / "worker-state"),
        "environment": environment,
        "backend": backend,
        "preview_sha256": "d" * 64,
        "assembly_sha256": "e" * 64,
        "artifact_sha256": artifacts,
    }
    repeat = {
        key: deepcopy(proof[key])
        for key in ("status", "preview_sha256", "assembly_sha256", "artifact_sha256")
    }
    installed = {
        "status": "passed",
        "environment": environment,
        "state_dir": proof["state_dir"],
        "scene": {"backend": backend, "status": "passed"},
        "preparation": proof,
        "preparation_after_development_edit": deepcopy(repeat),
        "preparation_after_restart": deepcopy(repeat),
    }
    checks = [{"name": str(i), "status": "passed"} for i in range(36)]
    ui = {
        "schema": "camera_scene_authoring_ui_v1",
        "status": "passed",
        "state_dir": str(tmp_path / "ui-state"),
        "base_url": "http://127.0.0.1:8776",
        "environment": environment,
        "checks": checks,
        "errors": [],
    }
    preparation_ui = {
        **deepcopy(ui),
        "schema": "camera_scene_preparation_ui_v1",
        "backend": backend,
        "checks": deepcopy(checks[:20]),
        "static_sha256": {"scenes.js": "f" * 64, "style.css": "1" * 64},
    }
    return {
        "package": {"scene_camera_preparation_api_idempotent_save_to_raw_verified": True},
        "installed": installed,
        "ui": ui,
        "preparation_ui": preparation_ui,
        "manifest": {
            "environment": environment,
            "files": {
                "camera_platform/scene_camera.py": "a" * 64,
                "camera_platform/static/scenes.js": "f" * 64,
                "camera_platform/static/style.css": "1" * 64,
            },
        },
    }


def test_preparation_upgrade_requires_all_matching_installed_evidence(
    transition, preparation_evidence
):
    transition.verify_preparation_evidence(**preparation_evidence)
    # A trailing slash and another lexical spelling of the exact same state
    # are harmless; a different server or state must fail below.
    preparation_evidence["preparation_ui"]["base_url"] += "/"
    preparation_evidence["preparation_ui"]["state_dir"] += "/."
    transition.verify_preparation_evidence(**preparation_evidence)


@pytest.mark.parametrize(
    ("keys", "value"),
    [
        (("preparation_ui",), None),
        (("package", "scene_camera_preparation_api_idempotent_save_to_raw_verified"), False),
        (("package", "scene_camera_preparation_api_idempotent_save_to_raw_verified"), "passed"),
        (("installed", "preparation"), None),
        (("installed", "preparation", "status"), "failed"),
        (("installed", "preparation", "environment"), {"platform_version": "0.11.0"}),
        (("installed", "preparation", "state_dir"), "/different/worker-state"),
        (("installed", "preparation", "backend"), {"executable": "/different/solver"}),
        (("installed", "preparation_after_development_edit"), None),
        (("installed", "preparation_after_restart", "status"), "failed"),
        (("installed", "preparation_after_restart", "preview_sha256"), "different"),
        (("installed", "preparation_after_restart", "assembly_sha256"), "different"),
        (
            (
                "installed",
                "preparation_after_development_edit",
                "artifact_sha256",
                "camera",
                "raw_frames.npy",
            ),
            "different",
        ),
        (("installed", "preparation", "artifact_sha256"), {}),
        (("ui", "checks"), [{"status": "passed"}] * 35),
        (("preparation_ui", "checks"), [{"status": "passed"}] * 19),
        (("preparation_ui", "checks", 0, "status"), "failed"),
        (("preparation_ui", "schema"), "camera_scene_authoring_ui_v1"),
        (("preparation_ui", "status"), "failed"),
        (("preparation_ui", "errors"), ["Uncaught rendering exception"]),
        (("preparation_ui", "environment"), {"platform_version": "0.11.0"}),
        (("preparation_ui", "backend"), {"executable": "/different/solver"}),
        (("preparation_ui", "state_dir"), "/different/qa-state"),
        (("preparation_ui", "base_url"), "http://127.0.0.1:8777"),
        (("preparation_ui", "static_sha256"), {}),
        (("preparation_ui", "static_sha256", "scenes.js"), "development-source"),
        (("preparation_ui", "static_sha256", "style.css"), "old-package"),
    ],
)
def test_preparation_upgrade_rejects_missing_failed_or_mixed_evidence(
    transition, preparation_evidence, keys, value
):
    target = preparation_evidence
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(RuntimeError, match="camera preparation"):
        transition.verify_preparation_evidence(**preparation_evidence)


def test_older_scene_package_does_not_require_preparation_evidence(transition):
    transition.verify_preparation_evidence(
        {}, {}, {}, None, {"files": {"camera_platform/scene_render.py": "old-scene"}}
    )


def test_apply_rejects_wrong_preparation_ui_before_backup_or_state_changes(
    transition, preparation_evidence, tmp_path, monkeypatch
):
    data = preparation_evidence
    old, new, state = tmp_path / "old", tmp_path / "new", tmp_path / "primary-state"
    data["manifest"]["files"]["camera_platform/scene_render.py"] = "scene-render"
    data["manifest"]["wheel_sha256"] = "new-wheel"
    data["package"].update(
        status="passed",
        wheel_sha256="new-wheel",
        environment=data["manifest"]["environment"],
        response_original_file_common_operator_and_reusable_analysis_verified=True,
        scene_authoring_api_and_renderer_to_raw_verified=True,
        scene_backend=data["installed"]["scene"]["backend"],
    )
    data["installed"].update(
        runtime_root=str(new),
        isolated_server_stopped=True,
        response={"status": "passed"},
        scene_after_restart={"status": "passed"},
        scene_after_development_edit={"status": "passed"},
    )
    data["preparation_ui"]["static_sha256"]["scenes.js"] = "wrong-browser-package"
    monkeypatch.setattr(
        transition.launcher, "load_installed_runtime", lambda _state: {"runtime_root": str(old)}
    )
    monkeypatch.setattr(transition.launcher, "verify_running_package", lambda *_args: None)
    monkeypatch.setattr(
        transition.launcher,
        "verify_manifest",
        lambda root: (
            {"environment": {"platform_version": "0.11.0"}} if root == old else data["manifest"]
        ),
    )
    monkeypatch.setattr(transition.launcher, "_scene_interpreter", lambda _p: "/solver/bin/python")
    monkeypatch.setattr(
        transition.launcher,
        "probe_scene_interpreter",
        lambda *_a, **_k: data["installed"]["scene"]["backend"],
    )
    monkeypatch.setattr(transition, "read", lambda p: data[str(p)])

    def unexpected(*_args, **_kwargs):
        pytest.fail("Unverified preparation evidence reached old-state work")

    monkeypatch.setattr(transition, "snapshot", unexpected)
    monkeypatch.setattr(transition, "stop_verified", unexpected)
    monkeypatch.setattr(transition, "select", unexpected)
    monkeypatch.setattr(transition, "launch_bound", unexpected)
    monkeypatch.setattr(
        transition.sys,
        "argv",
        [
            "upgrade",
            "--runtime-root",
            str(new),
            "--state-dir",
            str(state),
            "--port",
            "8770",
            "--expected-old-version",
            "0.11.0",
            "--package-proof",
            "package",
            "--runtime-proof",
            "installed",
            "--ui-proof",
            "ui",
            "--preparation-ui-proof",
            "preparation_ui",
            "--mitsuba-python",
            "/solver/bin/python",
            "--output",
            str(tmp_path / "transition.json"),
            "--apply",
        ],
    )
    with pytest.raises(RuntimeError, match="camera preparation"):
        transition.main()


@pytest.fixture
def projection_evidence(preparation_evidence):
    data = deepcopy(preparation_evidence)
    environment = data["manifest"]["environment"]
    environment["platform_version"] = "0.13.0"
    for proof in (data["installed"], data["installed"]["preparation"], data["ui"],
                  data["preparation_ui"]):
        proof["environment"] = deepcopy(environment)
    data["manifest"]["files"]["camera_platform/scene_projection.py"] = "2" * 64
    data["package"]["scene_projection_api_to_raw_without_renderer_verified"] = True
    proof = {
        "schema": "camera_scene_projection_verification_v1",
        "status": "passed",
        "environment": deepcopy(environment),
        "state_dir": data["installed"]["state_dir"],
        "records": {
            "source_render": "original-render",
            "assembly": "target-camera",
            "projection": "new-projection",
            "image": "new-image",
            "test": "new-test",
            "run": "new-camera-run",
        },
        "artifact_sha256": {
            "original_radiance": "3" * 64,
            "original_irradiance": "4" * 64,
            "original_mc_standard_error": "a" * 64,
            "new_irradiance": "5" * 64,
            "new_mc_standard_error": "b" * 64,
            "raw": "6" * 64,
            "rgb": "7" * 64,
        },
    }
    data["installed"]["projection"] = proof
    for suffix in ("after_development_edit", "after_restart"):
        repeat = deepcopy(proof)
        repeat["records"].update({
            kind: "forced-" + suffix + "-" + kind
            for kind in ("projection", "image", "test", "run")
        })
        data["installed"]["projection_" + suffix] = repeat
    data["projection_ui"] = {
        **deepcopy(data["ui"]),
        "schema": "camera_scene_projection_ui_v1",
        "checks": [{"name": str(i), "status": "passed"} for i in range(22)],
        "static_sha256": deepcopy(data["preparation_ui"]["static_sha256"]),
    }
    return data


def verify_projection(transition, evidence):
    transition.verify_projection_evidence(**{
        key: evidence[key] for key in ("package", "installed", "ui", "projection_ui", "manifest")
    })


def test_projection_upgrade_accepts_new_runs_with_identical_source_and_output(
    transition, projection_evidence
):
    verify_projection(transition, projection_evidence)
    projection_evidence["projection_ui"]["base_url"] += "/"
    projection_evidence["projection_ui"]["state_dir"] += "/."
    verify_projection(transition, projection_evidence)


@pytest.mark.parametrize(
    ("keys", "value"),
    [
        (("projection_ui",), None),
        (("package", "scene_projection_api_to_raw_without_renderer_verified"), False),
        (("package", "scene_projection_api_to_raw_without_renderer_verified"), "passed"),
        (("installed", "projection"), None),
        (("installed", "projection", "schema"), "camera_scene_preparation_verification_v1"),
        (("installed", "projection", "status"), "failed"),
        (("installed", "projection", "environment"), {"platform_version": "0.12.0"}),
        (("installed", "projection", "state_dir"), "/different/worker-state"),
        (("installed", "projection", "records"), {}),
        (("installed", "projection", "records", "source_render"), None),
        (("installed", "projection", "records", "image"), ""),
        (("installed", "projection", "artifact_sha256"), {}),
        (("installed", "projection", "artifact_sha256", "original_irradiance"), None),
        (("installed", "projection", "artifact_sha256", "original_mc_standard_error"), None),
        (("installed", "projection", "artifact_sha256", "new_mc_standard_error"), None),
        (("installed", "projection", "artifact_sha256", "new_irradiance"),
         "claimed-but-not-a-hash"),
        (("installed", "projection_after_development_edit"), None),
        (("installed", "projection_after_restart", "status"), "failed"),
        (("installed", "projection_after_restart", "environment"),
         {"platform_version": "0.12.0"}),
        (("installed", "projection_after_restart", "state_dir"), "/different/worker-state"),
        (("installed", "projection_after_restart", "records", "source_render"),
         "different-original-render"),
        (("installed", "projection_after_restart", "records", "assembly"),
         "different-target-camera"),
        (("installed", "projection_after_restart", "records", "run"), None),
        (("installed", "projection_after_development_edit", "artifact_sha256",
          "original_radiance"), "8" * 64),
        (("installed", "projection_after_restart", "artifact_sha256", "new_irradiance"),
         "9" * 64),
        (("installed", "projection_after_restart", "artifact_sha256", "raw"), "a" * 64),
        (("installed", "projection_after_restart", "artifact_sha256", "rgb"), "b" * 64),
        (("projection_ui", "schema"), "camera_scene_preparation_ui_v1"),
        (("projection_ui", "status"), "failed"),
        (("projection_ui", "checks"), [{"status": "passed"}] * 21),
        (("projection_ui", "checks", 0, "status"), "failed"),
        (("projection_ui", "errors"), ["Uncaught browser error"]),
        (("projection_ui", "environment"), {"platform_version": "0.12.0"}),
        (("projection_ui", "state_dir"), "/different/qa-state"),
        (("projection_ui", "base_url"), "http://127.0.0.1:8777"),
        (("projection_ui", "static_sha256"), {}),
        (("projection_ui", "static_sha256", "scenes.js"), "c" * 64),
        (("projection_ui", "static_sha256", "style.css"), "d" * 64),
    ],
)
def test_projection_upgrade_rejects_missing_failed_or_mixed_evidence(
    transition, projection_evidence, keys, value
):
    target = projection_evidence
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(RuntimeError, match="radiance projection"):
        verify_projection(transition, projection_evidence)


def test_older_scene_package_does_not_require_projection_evidence(transition):
    transition.verify_projection_evidence(
        {}, {}, {}, None, {"files": {"camera_platform/scene_camera.py": "old-preparation"}}
    )


@pytest.mark.parametrize("failure", ["missing", "different-static"])
def test_projection_gate_runs_before_backup_or_state_changes(
    transition, projection_evidence, tmp_path, monkeypatch, failure
):
    data = projection_evidence
    old, new, state = tmp_path / "old", tmp_path / "new", tmp_path / "primary-state"
    data["manifest"]["files"]["camera_platform/scene_render.py"] = "scene-render"
    data["manifest"]["wheel_sha256"] = "new-wheel"
    data["package"].update(
        status="passed",
        wheel_sha256="new-wheel",
        environment=data["manifest"]["environment"],
        response_original_file_common_operator_and_reusable_analysis_verified=True,
        scene_authoring_api_and_renderer_to_raw_verified=True,
        scene_backend=data["installed"]["scene"]["backend"],
    )
    data["installed"].update(
        runtime_root=str(new),
        isolated_server_stopped=True,
        response={"status": "passed"},
        scene_after_restart={"status": "passed"},
        scene_after_development_edit={"status": "passed"},
    )
    if failure == "different-static":
        data["projection_ui"]["static_sha256"]["scenes.js"] = "8" * 64
    monkeypatch.setattr(
        transition.launcher, "load_installed_runtime", lambda _state: {"runtime_root": str(old)}
    )
    monkeypatch.setattr(transition.launcher, "verify_running_package", lambda *_args: None)
    monkeypatch.setattr(
        transition.launcher,
        "verify_manifest",
        lambda root: (
            {"environment": {"platform_version": "0.12.0"}} if root == old else data["manifest"]
        ),
    )
    monkeypatch.setattr(transition.launcher, "_scene_interpreter", lambda _p: "/solver/bin/python")
    monkeypatch.setattr(
        transition.launcher,
        "probe_scene_interpreter",
        lambda *_a, **_k: data["installed"]["scene"]["backend"],
    )
    monkeypatch.setattr(transition, "read", lambda p: data[str(p)])

    def unexpected(*_args, **_kwargs):
        pytest.fail("Unverified projection evidence reached old-state work")

    monkeypatch.setattr(transition, "snapshot", unexpected)
    monkeypatch.setattr(transition, "stop_verified", unexpected)
    monkeypatch.setattr(transition, "select", unexpected)
    monkeypatch.setattr(transition, "launch_bound", unexpected)
    monkeypatch.setattr(transition.subprocess, "run", unexpected)
    arguments = [
        "upgrade",
        "--runtime-root", str(new),
        "--state-dir", str(state),
        "--port", "8770",
        "--expected-old-version", "0.12.0",
        "--package-proof", "package",
        "--runtime-proof", "installed",
        "--ui-proof", "ui",
        "--preparation-ui-proof", "preparation_ui",
        "--mitsuba-python", "/solver/bin/python",
        "--output", str(tmp_path / "transition.json"),
        "--apply",
    ]
    if failure != "missing":
        arguments.extend(["--projection-ui-proof", "projection_ui"])
    monkeypatch.setattr(transition.sys, "argv", arguments)
    with pytest.raises(RuntimeError, match="radiance projection"):
        transition.main()
