"""Persist real venv selection and preserve old interpreter identity on rollback."""

import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import venv
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_installed_runtime import package as package


@pytest.fixture
def modules(monkeypatch):
    tools = Path(__file__).resolve().parents[2] / "tools"
    monkeypatch.syspath_prepend(str(tools))
    import install_camera_platform_runtime as installer
    import start_camera_platform as launcher

    spec = importlib.util.spec_from_file_location(
        "python_binding_transition", tools / "upgrade_camera_platform_runtime.py"
    )
    transition = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transition)
    return launcher, installer, transition


@pytest.fixture
def independent(tmp_path):
    folder = tmp_path / "independent dependencies"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(folder)
    executable = folder / "bin/python"
    assert executable.is_symlink()
    assert executable.resolve() != executable
    return executable


@pytest.fixture
def fixture_assets(modules, tmp_path, monkeypatch):
    """Use the same validator on tiny authored identities, never production evidence."""
    import camera_runtime_assets as assets

    launcher, _, _ = modules
    archive = tmp_path / "authored-assets.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        root = "ISET-isetcam-" + assets.UPSTREAM_COMMIT[:7]
        folder = tarfile.TarInfo(root)
        folder.type = tarfile.DIRTYPE
        target.addfile(folder)
        for name, data in {"LICENSE": b"authored fixture", "README.md": b"unit test only"}.items():
            member = tarfile.TarInfo(root + "/" + name)
            member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    prepared = assets._prepare(archive, tmp_path / "authored-assets", checksum)
    candidate = assets._prepare(archive, tmp_path / "candidate-assets", checksum)
    prepared["candidate"] = candidate
    monkeypatch.setattr(
        launcher,
        "validate_asset_binding",
        lambda binding: assets._validate_binding(binding, checksum),
    )
    code = launcher.INSTALLED_BOOTSTRAP.replace(repr(assets.ARCHIVE_SHA256), repr(checksum))
    code = code.replace(repr(assets.MANIFEST_SHA256), repr(prepared["manifest_sha256"]))
    monkeypatch.setattr(launcher, "INSTALLED_BOOTSTRAP", code)
    yield prepared
    # Restore writability only for this test-owned tiny tree so pytest can clean it.
    for snapshot in (prepared, candidate):
        root = Path(snapshot["root"])
        root.chmod(0o700)
        for path in root.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)


def test_explicit_installer_cli_persists_real_venv_path_and_identity(
    modules, independent, package, tmp_path, capsys
):
    launcher, installer, _ = modules
    state = tmp_path / "state"
    installer.main(
        ["--select", str(package), "--python", str(independent), "--state-dir", str(state)]
    )
    output = json.loads(capsys.readouterr().out)
    pin = launcher.load_installed_runtime(state)
    assert output["interpreter_persisted"] is True
    assert pin["schema"] == launcher.PIN_SCHEMA_V2
    assert pin["python"]["executable"] == str(independent)
    assert pin["python"]["resolved_executable"] == str(independent.resolve())
    assert pin["python"]["prefix"] == str(independent.parent.parent)
    assert pin["python"]["prefix"] != pin["python"]["base_prefix"]
    assert launcher.verify_runtime_python(pin) == str(independent)
    assert not list(package.rglob("__pycache__"))


def test_legacy_selection_keeps_v1_and_default_policy(
    modules, independent, package, tmp_path, monkeypatch
):
    launcher, installer, _ = modules
    pin = installer.select(package, tmp_path / "state", independent)
    assert set(pin) == {"schema", "runtime_root", "manifest_sha256"}
    assert pin["schema"] == launcher.PIN_SCHEMA
    monkeypatch.setattr(launcher, "default_runtime_python", lambda: str(independent))
    assert launcher.verify_runtime_python(
        launcher.load_installed_runtime(tmp_path / "state")
    ) == str(independent)


def test_v2_dependency_mismatch_does_not_replace_previous_pin(
    modules, independent, package, tmp_path, monkeypatch
):
    launcher, _, _ = modules
    state = tmp_path / "state"
    launcher.save_runtime_pin(state, package)
    original = (state / "installed-runtime.json").read_bytes()
    monkeypatch.setattr(launcher, "probe_package", lambda *args: {"environment": {"changed": True}})
    with pytest.raises(RuntimeError, match="dependencies or Python changed"):
        launcher.save_runtime_pin(state, package, executable=independent)
    assert (state / "installed-runtime.json").read_bytes() == original


@pytest.mark.parametrize("assets", ["missing-key", {}, {"root": "/unverified"}])
def test_v2_asset_binding_is_explicit_and_strict(modules, independent, package, tmp_path, assets):
    launcher, _, _ = modules
    state = tmp_path / "state"
    pin = launcher.save_runtime_pin(state, package, executable=independent)
    if assets == "missing-key":
        del pin["assets"]
    else:
        pin["assets"] = assets
    (state / "installed-runtime.json").write_text(json.dumps(pin))
    with pytest.raises(RuntimeError, match="identity fields|asset binding"):
        launcher.load_installed_runtime(state)


def test_asset_selection_requires_persisted_python(modules, package, tmp_path):
    _, installer, _ = modules
    state = tmp_path / "state"
    with pytest.raises(SystemExit):
        installer.main(
            [
                "--select",
                str(package),
                "--state-dir",
                str(state),
                "--assets-manifest",
                str(tmp_path / "asset-manifest.json"),
            ]
        )
    assert not state.exists()


def test_installer_cli_persists_verified_assets_without_changing_shell_environment(
    modules, independent, package, fixture_assets, tmp_path, monkeypatch, capsys
):
    launcher, installer, _ = modules
    monkeypatch.setenv(launcher.ASSET_ENV, "/original-shell-root")
    monkeypatch.setenv(launcher.ASSET_CACHE_ENV, "/original-shell-cache")
    state = tmp_path / "state"
    installer.main(
        [
            "--select",
            str(package),
            "--python",
            str(independent),
            "--state-dir",
            str(state),
            "--assets-manifest",
            fixture_assets["manifest_path"],
        ]
    )
    report = json.loads(capsys.readouterr().out)
    pin = launcher.load_installed_runtime(state)
    assert pin["assets"] == report["assets"] == fixture_assets["binding"]
    assert launcher.os.environ[launcher.ASSET_ENV] == "/original-shell-root"
    assert launcher.os.environ[launcher.ASSET_CACHE_ENV] == "/original-shell-cache"


@pytest.mark.parametrize("change", ["pyvenv_cfg", "binary_hash", "prefix", "missing", "extra"])
def test_identity_drift_or_malformed_binding_refuses_before_start(
    modules, independent, package, tmp_path, monkeypatch, change
):
    launcher, _, _ = modules
    state = tmp_path / "state"
    pin = launcher.save_runtime_pin(state, package, executable=independent)
    if change == "pyvenv_cfg":
        config = independent.parent.parent / "pyvenv.cfg"
        config.write_text(config.read_text() + "\n# changed after qualification\n")
    elif change == "binary_hash":
        pin["python"]["executable_sha256"] = "0" * 64
    elif change == "prefix":
        pin["python"]["prefix"] = str(tmp_path / "other-prefix")
    elif change == "missing":
        independent.unlink()
    else:
        pin["python"]["command"] = "unexpected"
    if change in ("binary_hash", "prefix", "extra"):
        (state / "installed-runtime.json").write_text(json.dumps(pin))
    monkeypatch.setattr(launcher, "ready", lambda *a: False)
    real_popen = subprocess.Popen

    def forbid_server(args, **kwargs):
        if launcher.INSTALLED_BOOTSTRAP in args:
            pytest.fail("server started")
        return real_popen(args, **kwargs)

    monkeypatch.setattr(launcher.subprocess, "Popen", forbid_server)
    with pytest.raises(RuntimeError, match="Python|identity"):
        launcher.main(["--state-dir", str(state), "--no-open"])
    assert not (state / "server.pid").exists()


def test_explicit_conflicting_python_cannot_override_v2(
    modules, independent, package, tmp_path, monkeypatch
):
    launcher, _, _ = modules
    state = tmp_path / "state"
    launcher.save_runtime_pin(state, package, executable=independent)
    monkeypatch.setattr(launcher, "ready", lambda *a: False)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("server started"))
    with pytest.raises(RuntimeError, match="pinned interpreter"):
        launcher.main(["--state-dir", str(state), "--python", sys.executable, "--no-open"])


def test_launcher_passes_pinned_executable_despite_changed_shared_default(
    modules, independent, package, tmp_path, monkeypatch
):
    launcher, _, _ = modules
    state = tmp_path / "state"
    pin = launcher.save_runtime_pin(state, package, executable=independent)
    statuses = iter([False, True])
    monkeypatch.setattr(launcher, "ready", lambda *a: next(statuses))
    monkeypatch.setattr(launcher, "default_runtime_python", lambda: "/missing/shared/python")
    monkeypatch.setattr(launcher, "verify_running_package", lambda *a: None)
    calls = []
    real_popen = subprocess.Popen

    def capture_server(args, **kwargs):
        if launcher.INSTALLED_BOOTSTRAP in args:
            calls.append(args)
            return SimpleNamespace(pid=4242, poll=lambda: None)
        return real_popen(args, **kwargs)

    monkeypatch.setattr(launcher.subprocess, "Popen", capture_server)
    launcher.main(["--state-dir", str(state), "--no-open"])
    assert calls[0][0] == str(independent)
    index = calls[0].index("--runtime-python-binding-json")
    assert json.loads(calls[0][index + 1]) == pin["python"]


@pytest.mark.parametrize("with_assets", [False, True])
def test_actual_bootstrap_child_inherits_venv_and_rejects_wrong_python(
    modules, independent, package, tmp_path, monkeypatch, fixture_assets, with_assets
):
    launcher, _, _ = modules
    # Exercise the same sys.executable inheritance used by API/Worker launch,
    # with a finite stdlib child instead of starting a server or real worker.
    (package / "camera_platform/__main__.py").write_text(
        "import subprocess,sys,json\n"
        "def main():\n"
        "    child=subprocess.check_output([sys.executable,'-I','-B','-c',"
        "\"import sys,json,os;print(json.dumps({'executable':sys.executable,"
        "'prefix':sys.prefix,'assets':os.environ.get('PYISETCAM_UPSTREAM_ROOT'),"
        "'cache':os.environ.get('PYISETCAM_CACHE_ROOT')}))\"],text=True)\n"
        "    print(child.strip())\n"
    )
    expected_root = fixture_assets["root"] if with_assets else None
    (package / "camera_platform/__init__.py").write_text(
        "import os\nassert os.environ.get('PYISETCAM_UPSTREAM_ROOT') == " + repr(expected_root)
    )
    manifest = json.loads((package / "runtime-manifest.json").read_text())
    manifest["files"] = launcher.package_files(package)
    (package / "runtime-manifest.json").write_text(json.dumps(manifest))
    state = tmp_path / "state"
    monkeypatch.setenv(launcher.ASSET_ENV, str(tmp_path / "unbound shell assets"))
    monkeypatch.setenv(launcher.ASSET_CACHE_ENV, str(tmp_path / "unbound cache"))
    pin = launcher.save_runtime_pin(
        state,
        package,
        executable=independent,
        assets=fixture_assets["binding"] if with_assets else None,
    )
    arguments = [
        "-I",
        "-O",
        "-B",
        "-c",
        launcher.INSTALLED_BOOTSTRAP,
        str(package),
        str(state),
        pin["manifest_sha256"],
        "--runtime-python-binding-json",
        json.dumps(pin["python"]),
        "--runtime-assets-binding-json",
        json.dumps(pin["assets"]),
        "serve",
    ]
    wrong = subprocess.run([sys.executable, *arguments], capture_output=True, text=True, timeout=10)
    assert wrong.returncode != 0 and "Runtime Python identity changed" in wrong.stderr
    assert not (state / "runtime-process.json").exists()
    actual = subprocess.run(
        [str(independent), *arguments], capture_output=True, text=True, check=True, timeout=10
    )
    child = json.loads(actual.stdout)
    assert child == {
        "executable": str(independent),
        "prefix": str(independent.parent.parent),
        "assets": expected_root,
        "cache": None,
    }
    witness = json.loads((state / "runtime-process.json").read_text())
    assert witness["schema"] == "camera_platform_runtime_process_v2"
    assert witness["python"] == pin["python"]
    assert "assets" in witness and witness["assets"] == pin["assets"]
    if with_assets:
        (state / "runtime-process.json").unlink()
        changed = Path(fixture_assets["root"]) / "LICENSE"
        changed.chmod(0o600)
        changed.write_text("changed after preflight")
        refused = subprocess.run(
            [str(independent), *arguments], capture_output=True, text=True, timeout=10
        )
        assert refused.returncode != 0 and "Asset" in refused.stderr
        assert not (state / "runtime-process.json").exists()
        with pytest.raises(RuntimeError, match="Asset"):
            launcher.load_installed_runtime(state)


@pytest.mark.parametrize("old_v2,old_assets", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize(
    "failure_case",
    ["candidate_start", "partial_old_stop", "recovery_refused", "process_closure_refused"],
)
def test_upgrade_failure_preserves_old_binding_or_keeps_candidate_for_forward_recovery(
    modules,
    independent,
    package,
    tmp_path,
    monkeypatch,
    old_v2,
    old_assets,
    fixture_assets,
    failure_case,
):
    launcher, _, transition = modules
    old_python = independent
    candidate_folder = tmp_path / "candidate dependencies"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(candidate_folder)
    candidate_python = candidate_folder / "bin/python"
    new = tmp_path / "candidate"
    shutil.copytree(package, new)
    manifest = json.loads((new / "runtime-manifest.json").read_text())
    manifest["environment"]["platform_version"] = "candidate"
    (new / "camera_platform/service.py").write_text(
        "def environment_fingerprint():\n    return " + repr(manifest["environment"]) + "\n"
    )
    manifest["files"] = launcher.package_files(new)
    (new / "runtime-manifest.json").write_text(json.dumps(manifest))
    state, qa = tmp_path / "state", tmp_path / "candidate-qa"
    original = launcher.save_runtime_pin(
        state,
        package,
        executable=old_python if old_v2 else None,
        assets=fixture_assets["binding"] if old_assets else None,
    )
    candidate_assets = fixture_assets["candidate"]["binding"]
    launcher.save_runtime_pin(qa, new, executable=candidate_python, assets=candidate_assets)
    (state / "runtime-process.json").write_text(
        json.dumps({"pid": 4242, "runtime_root": str(package)})
    )
    monkeypatch.setattr(launcher, "WORKSPACE", tmp_path)
    monkeypatch.setattr(launcher, "default_runtime_python", lambda: str(old_python))
    monkeypatch.setattr(launcher, "verify_running_package", lambda *a: None)
    monkeypatch.setenv(launcher.ASSET_ENV, "/original-ambient-assets")
    monkeypatch.setenv(launcher.ASSET_CACHE_ENV, "/original-ambient-cache")
    proofs = {
        "package": {
            "status": "passed",
            "wheel_sha256": manifest["wheel_sha256"],
            "environment": manifest["environment"],
            "response_original_file_common_operator_and_reusable_analysis_verified": True,
        },
        "installed": {
            "status": "passed",
            "environment": manifest["environment"],
            "runtime_root": str(new),
            "isolated_server_stopped": True,
            "response": {"status": "passed"},
        },
        "ui": {
            "status": "passed",
            "errors": [],
            "state_dir": str(qa),
            "base_url": "http://127.0.0.1:65501",
        },
    }
    for label, proof in proofs.items():
        (tmp_path / (label + ".json")).write_text(json.dumps(proof))
    monkeypatch.setattr(
        transition, "snapshot", lambda *a, **k: {"records": {}, "jobs": {}, "audit": {}}
    )
    # This test isolates interpreter/asset restoration. Actual stopped SQLite,
    # process closure and fail-closed schema recovery are tested separately.
    recovery = transition.recovery
    monkeypatch.setattr(recovery, "capture_state", lambda *a: {"fixture": "identity-only"})
    monkeypatch.setattr(recovery, "original_prefix_preserved", lambda *a: True)
    monkeypatch.setattr(
        recovery, "stop_runtime", lambda *a, **k: {"stopped": True, "private_groups": []}
    )
    monkeypatch.setattr(recovery, "quiescent_state", lambda *a, **k: nullcontext("fixture-lease"))
    recovered = []

    def recover(*args):
        recovered.append(args)
        if failure_case == "recovery_refused":
            raise recovery.RecoveryRefusedError(
                "new candidate work must be retained", {"status": "forward_recovery_required"}
            )
        return {"status": "same_schema_code_rollback", "database_changed": False}

    monkeypatch.setattr(recovery, "recover_schema", recover)
    stopped, backups, resumed = [], [], []

    def stop(*args):
        stopped.append(args)
        if failure_case == "partial_old_stop":
            raise RuntimeError("old stop partially completed")
        if failure_case == "process_closure_refused":
            raise recovery.RecoveryRefusedError(
                "unknown orphan remains",
                {"process_closure": {"stopped": False, "private_groups": [{"pgid": 12345}]}},
            )
        return 4242

    monkeypatch.setattr(transition, "stop_verified", stop)
    real_run = subprocess.run

    def run(args, **kwargs):
        if any("from camera_platform.backup import backup_state" in arg for arg in args):
            backups.append((args, kwargs))
            return SimpleNamespace(stdout=json.dumps({"manifest_hash": "backup-proof"}))
        return real_run(args, **kwargs)

    monkeypatch.setattr(transition.subprocess, "run", run)

    def fail_new(*args):
        chosen = launcher.load_installed_runtime(state)
        assert chosen["schema"] == launcher.PIN_SCHEMA_V2
        assert chosen["python"]["executable"] == str(candidate_python)
        assert chosen["assets"] == candidate_assets
        # Even if a default changes, rollback must explicitly use the old Python.
        monkeypatch.setattr(launcher, "default_runtime_python", lambda: str(candidate_python))
        raise RuntimeError("candidate start failed")

    monkeypatch.setattr(transition, "launch_bound", fail_new)

    def resume(args):
        executable = args[args.index("--python") + 1]
        restored = launcher.load_installed_runtime(state)
        resumed.append(launcher.verify_runtime_python(restored, executable))

    monkeypatch.setattr(launcher, "main", resume)
    output = tmp_path / "transition.json"
    args = [
        "upgrade",
        "--runtime-root",
        str(new),
        "--state-dir",
        str(state),
        "--port",
        "65501",
        "--expected-old-version",
        "fixture",
        "--python",
        str(candidate_python),
        "--assets-manifest",
        fixture_assets["candidate"]["manifest_path"],
        "--output",
        str(output),
        "--apply",
    ]
    for name in proofs:
        args.extend(
            [
                "--" + ("runtime" if name == "installed" else name) + "-proof",
                str(tmp_path / (name + ".json")),
            ]
        )
    monkeypatch.setattr(transition.sys, "argv", args)
    original_error = {
        "partial_old_stop": "old stop partially completed",
        "process_closure_refused": "unknown orphan remains",
    }.get(failure_case, "candidate start failed")
    with pytest.raises(RuntimeError, match=original_error):
        transition.main()
    assert backups[0][0][0] == str(old_python)
    backup_environment = backups[0][1]["env"]
    expected_assets = (
        fixture_assets["root"] if old_assets else None if old_v2 else "/original-ambient-assets"
    )
    assert backup_environment.get(launcher.ASSET_ENV) == expected_assets
    assert backup_environment.get(launcher.ASSET_CACHE_ENV) == (
        None if old_v2 else "/original-ambient-cache"
    )
    assert len(stopped) == 1
    if failure_case == "process_closure_refused":
        assert recovered == [] and resumed == []
        assert json.loads((state / "installed-runtime.json").read_text()) == original
        report = json.loads(output.read_text())
        assert report["status"] == "forward_recovery_required"
        assert report["error"] == original_error
        assert report["recovery_evidence"]["process_closure"]["private_groups"] == [{"pgid": 12345}]
        return
    assert len(recovered) == 1
    report = json.loads(output.read_text())
    assert report["error"] == original_error
    assert report["old_python"] == str(old_python)
    assert report["candidate_python"] == str(candidate_python)
    if failure_case == "partial_old_stop":
        assert recovered[0][2]["runtime_root"] == str(package)
        assert recovered[0][3] == str(old_python)
        assert recovered[0][4].get(launcher.ASSET_ENV) == expected_assets
    else:
        assert recovered[0][2]["runtime_root"] == str(new)
        assert recovered[0][3] == str(candidate_python)
        assert recovered[0][4].get(launcher.ASSET_ENV) == candidate_assets["root"]
    if failure_case == "recovery_refused":
        assert resumed == []  # Never construct/start the old runtime after a failed proof.
        assert report["status"] == "forward_recovery_required"
        assert report["old_runtime_restored"] is False
        assert report["recovery_error"] == "new candidate work must be retained"
        retained = json.loads((state / "installed-runtime.json").read_text())
        assert retained["runtime_root"] == str(new)
        assert retained["python"]["executable"] == str(candidate_python)
        assert retained["assets"] == candidate_assets
    else:
        assert resumed == [str(old_python)]
        assert json.loads((state / "installed-runtime.json").read_text()) == original
        assert report["old_runtime_restored"] is True
