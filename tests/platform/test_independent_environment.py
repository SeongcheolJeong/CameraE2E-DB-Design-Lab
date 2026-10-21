"""Independent dependency preparation boundaries and fail-closed artifact replay."""

import copy
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
try:
    SPEC = importlib.util.spec_from_file_location(
        "independent_environment", TOOLS / "prepare_camera_platform_environment.py"
    )
    tool = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(tool)
finally:
    sys.path.remove(str(TOOLS))


@pytest.fixture
def inputs(tmp_path):
    wheel = tmp_path / "pyisetcam-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("camera_platform/__init__.py", "")
        archive.writestr("pyisetcam/__init__.py", "")
    environment = {
        "platform_version": "fixture",
        "python": "3.12.13",
        "system": "test",
        "packages": {"numpy": "2.4.2"},
        "source_hashes": {"fixture": "a" * 64},
    }
    report = {
        "status": "passed",
        "wheel_sha256": tool.checksum(wheel),
        "all_checked_source_static_bytes_match": True,
        "raw_and_rgb_artifact_http_hashes_verified": True,
        "registry_integrity": "verified",
        "environment": environment,
    }
    path = tmp_path / "package.json"
    tool.write_json(path, report)
    pip = {
        "version": "1",
        "pip_version": "25.0",
        "environment": {
            "python_full_version": "3.12.13",
            "implementation_name": "cpython",
            "platform_machine": "arm64",
            "sys_platform": "darwin",
        },
        "install": [
            {
                "metadata": {"name": "pyisetcam", "version": "0.1.0"},
                "download_info": {
                    "url": wheel.as_uri(),
                    "archive_info": {"hashes": {"sha256": tool.checksum(wheel)}},
                },
            },
            {
                "metadata": {"name": "numpy", "version": "2.4.3", "requires_python": ">=3.11"},
                "download_info": {
                    "url": "https://example.org/numpy-2.4.3.whl",
                    "archive_info": {"hashes": {"sha256": "b" * 64}},
                },
            },
        ],
    }
    return wheel, path, report, pip


def valid_probe(destination, old_environment):
    env = copy.deepcopy(old_environment)
    env["packages"]["numpy"] = "2.4.3"
    site = destination / "lib/python3.12/site-packages"
    result = {
        "prefix": str(destination),
        "base_prefix": "/base/python",
        "user_site_enabled": False,
        "sys_path": ["/base/python/lib/python3.12", str(site)],
        "installation_paths": [str(site)],
        "module_paths": {name: str(site / name / "__init__.py") for name in tool.MODULES},
        "distributions": [
            {"name": "pyisetcam", "version": "0.1.0", "location": str(site)},
            {"name": "numpy", "version": "2.4.3", "location": str(site)},
        ],
        "environment": env,
    }
    result["visible_distributions"] = copy.deepcopy(result["distributions"])
    return result


def test_platform_only_probe_excludes_optional_factory_imports(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "EXTRAS", ["platform"])
    optional = set().union(*tool.FACTORY_MODULES.values())
    assert set(tool.required_modules()) == set(tool.MODULES) - optional

    environment = {
        "platform_version": "fixture",
        "packages": {"numpy": "2.4.3"},
        "source_hashes": {"fixture": "a" * 64},
    }
    probe = valid_probe(tmp_path, environment)
    for name in optional:
        probe["module_paths"].pop(name)
    assert tool.verify_probe(probe, tmp_path, environment) == environment


def fake_steps(monkeypatch, inputs, *, failure=None, probe_change=None, pip_change=None):
    wheel, path, report, pip = inputs
    calls = []

    def step(command, destination, label, timeout):
        calls.append((label, command))
        output = destination / (label + ".log")
        output.write_text("log\n")
        if label == failure:
            raise RuntimeError(f"{label} failed")
        if label == "create-venv":
            (destination / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        elif label == "install":
            data = copy.deepcopy(pip)
            if pip_change:
                pip_change(data)
            tool.write_json(destination / "pip-installation-report.json", data)
        elif label == "isolated-probe":
            data = valid_probe(destination, report["environment"])
            if probe_change:
                probe_change(data)
            output.write_text("warning\n" + json.dumps(data) + "\n")
        return output

    monkeypatch.setattr(tool, "run_step", step)
    return calls


def test_preparation_records_dependency_evidence_without_inheriting_calculation_proof(
    inputs,
    tmp_path,
    monkeypatch,
):
    calls = fake_steps(monkeypatch, inputs)
    destination = tmp_path / "new"
    result = tool.prepare(inputs[0], inputs[1], destination)
    assert result["status"] == "prepared"
    assert result["environment_differs_from_prior"] is True
    assert result["environment_changes"]["packages"] == {
        "prior": {"numpy": "2.4.2"},
        "new": {"numpy": "2.4.3"},
    }
    assert result["prior_calculation_proof_applies"] is False
    assert result["calculation_validation"] == "pending_in_this_environment"
    assert result["activated"] is False
    assert result["pip_check"] == "passed"
    assert result["pyvenv_config"] == "include-system-site-packages = false\n"
    assert [label for label, _ in calls] == [
        "create-venv",
        "install",
        "pip-check",
        "isolated-probe",
    ]
    install = calls[1][1]
    assert "--no-deps" not in install and "--system-site-packages" not in calls[0][1]
    assert "--report" in install and "--ignore-installed" in install
    assert install[-1].endswith("[platform,factory-rayoptics,factory-prysm]")
    assert all("-I" in command for _, command in calls)
    lock = json.loads((destination / "dependency-lock.json").read_text())
    assert lock["artifacts"][0]["metadata"]["requires_python"] == ">=3.11"
    assert "--hash=sha256:" in (destination / "requirements.lock.txt").read_text()
    assert "build dependencies" in (destination / "REPRODUCE.md").read_text()
    assert result["dependency_lock_sha256"] == tool.checksum(destination / "dependency-lock.json")


def test_platform_only_preparation_replays_the_same_extra(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "EXTRAS", ["platform"])
    optional = set().union(*tool.FACTORY_MODULES.values())
    calls = fake_steps(
        monkeypatch,
        inputs,
        probe_change=lambda probe: [probe["module_paths"].pop(name) for name in optional],
    )
    destination = tmp_path / "platform-only"
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "cached.whl").write_bytes(b"fixture")
    tool.write_json(wheelhouse / "wheelhouse-manifest.json", {
        "schema": "camera_platform_r1_offline_wheelhouse_v1",
        "r1_wheel_sha256": tool.checksum(inputs[0]),
        "artifact_count": 1,
        "artifacts": [{
            "filename": "cached.whl", "bytes": 7,
            "sha256": tool.checksum(wheelhouse / "cached.whl"),
        }],
    })
    result = tool.prepare(
        inputs[0], inputs[1], destination, offline_wheelhouse=wheelhouse,
    )
    assert result["status"] == "prepared"
    assert result["extras"] == ["platform"]
    assert calls[1][1][-1].endswith("[platform]")
    assert calls[1][1][calls[1][1].index("install") + 1:][:3] == [
        "--no-index", "--find-links", str(wheelhouse),
    ]
    assert json.loads(calls[-1][1][-1]) == tool.required_modules()
    assert result["replay_command"][-3:] == [
        "--platform-only", "--offline-wheelhouse", str(wheelhouse),
    ]


@pytest.mark.parametrize("failure", ["create-venv", "install", "pip-check", "isolated-probe"])
def test_step_failure_preserves_failed_report_and_never_claims_success(
    inputs,
    tmp_path,
    monkeypatch,
    failure,
):
    calls = fake_steps(monkeypatch, inputs, failure=failure)
    destination = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="failed"):
        tool.prepare(inputs[0], inputs[1], destination)
    report = json.loads((destination / "environment-report.json").read_text())
    assert report["status"] == "failed"
    assert report["calculation_validation"] == "pending_in_this_environment"
    assert "completed_at" not in report
    assert "pip_check" not in report
    assert calls[-1][0] == failure


def test_bad_wheel_never_starts_install(inputs, tmp_path, monkeypatch):
    calls = fake_steps(monkeypatch, inputs)
    inputs[0].write_bytes(b"tampered")
    destination = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="does not match"):
        tool.prepare(inputs[0], inputs[1], destination)
    assert not calls
    assert json.loads((destination / "environment-report.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("existing", ["directory", "file", "symlink", "dangling"])
def test_existing_destination_is_never_modified(inputs, tmp_path, monkeypatch, existing):
    calls = fake_steps(monkeypatch, inputs)
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("preserve")
    destination = tmp_path / "existing"
    if existing == "directory":
        destination.mkdir()
    elif existing == "file":
        destination.write_text("preserve")
    else:
        destination.symlink_to(target if existing == "symlink" else tmp_path / "missing")
    with pytest.raises(RuntimeError, match="new path"):
        tool.prepare(inputs[0], inputs[1], destination)
    assert sentinel.read_text() == "preserve"
    assert not calls


@pytest.mark.parametrize(
    "marker",
    [
        "pyvenv.cfg",
        "installed-runtime.json",
        "runtime-manifest.json",
        "registry.sqlite",
        "registry.sqlite3",
    ],
)
def test_nested_environment_or_state_destination_refused(tmp_path, marker):
    (tmp_path / marker).write_text("protected")
    with pytest.raises(RuntimeError, match="existing environment or state"):
        tool.validate_destination(tmp_path / "new")


def test_symlink_parent_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    with pytest.raises(RuntimeError, match="symbolic"):
        tool.validate_destination(alias / "new")


@pytest.mark.parametrize(
    "change",
    [
        "schema",
        "hash",
        "duplicate",
        "credentials",
        "newline",
        "vcs",
        "metadata",
        "wheel",
    ],
)
def test_unreplayable_artifact_report_rejected(inputs, change):
    data = copy.deepcopy(inputs[3])
    item = data["install"][1]
    if change == "schema":
        data["version"] = "2"
    elif change == "hash":
        item["download_info"]["archive_info"]["hashes"] = {}
    elif change == "duplicate":
        data["install"].append(copy.deepcopy(item))
    elif change == "credentials":
        item["download_info"]["url"] = "https://secret:password@example.org/a.whl"
    elif change == "newline":
        item["download_info"]["url"] = "https://example.org/a.whl\n--index-url=bad"
    elif change == "vcs":
        item["download_info"]["vcs_info"] = {"vcs": "git"}
    elif change == "metadata":
        item["metadata"] = []
    else:
        data["install"][0]["download_info"]["archive_info"]["hashes"]["sha256"] = "f" * 64
    with pytest.raises(RuntimeError):
        tool.make_lock(data, inputs[2]["wheel_sha256"])


def test_lock_replay_enforces_hashes_and_resolver(inputs, tmp_path, monkeypatch):
    lock = tmp_path / "lock.json"
    tool.write_json(lock, tool.make_lock(inputs[3], inputs[2]["wheel_sha256"]))
    calls = fake_steps(monkeypatch, inputs)
    result = tool.prepare(inputs[0], inputs[1], tmp_path / "replay", lock_path=lock)
    install = calls[1][1]
    assert "--require-hashes" in install and "-r" in install and "--no-deps" not in install
    assert result["input_lock_sha256"] == tool.checksum(lock)


@pytest.mark.parametrize("change", ["version", "hash", "platform"])
def test_lock_replay_refuses_changed_resolution(inputs, tmp_path, monkeypatch, change):
    lock = tmp_path / "lock.json"
    tool.write_json(lock, tool.make_lock(inputs[3], inputs[2]["wheel_sha256"]))

    def mutate(data):
        if change == "version":
            data["install"][1]["metadata"]["version"] = "99.0"
        elif change == "hash":
            data["install"][1]["download_info"]["archive_info"]["hashes"]["sha256"] = "c" * 64
        else:
            data["environment"]["platform_machine"] = "x86_64"

    calls = fake_steps(monkeypatch, inputs, pip_change=mutate)
    with pytest.raises(RuntimeError, match="differs|differ"):
        tool.prepare(inputs[0], inputs[1], tmp_path / "replay", lock_path=lock)
    assert calls[-1][0] == "install"


@pytest.mark.parametrize(
    "change",
    [
        "prefix",
        "site",
        "module",
        "distribution",
        "checkout",
        "base-site",
        "source",
        "version",
    ],
)
def test_probe_refuses_external_imports_and_wrong_payload(inputs, tmp_path, change):
    destination = tmp_path / "new"
    probe = valid_probe(destination, inputs[2]["environment"])
    if change == "prefix":
        probe["prefix"] = str(tmp_path)
    elif change == "site":
        probe["user_site_enabled"] = True
    elif change == "module":
        probe["module_paths"]["numpy"] = "/outside/numpy/__init__.py"
    elif change == "distribution":
        probe["distributions"][0]["location"] = "/outside"
    elif change == "checkout":
        probe["sys_path"].append(str(tmp_path / "src"))
    elif change == "base-site":
        probe["sys_path"].append("/base/python/lib/python3.12/site-packages")
    elif change == "source":
        probe["environment"]["source_hashes"] = {"fixture": "changed"}
    else:
        probe["environment"]["platform_version"] = "different"
    with pytest.raises(RuntimeError):
        tool.verify_probe(probe, destination, inputs[2]["environment"])


def test_subprocess_options_remove_external_pip_and_python_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("PIP_TARGET", "/existing")
    monkeypatch.setenv("PIP_USER", "1")
    monkeypatch.setenv("PIP_CONFIG_FILE", "/existing/pip.conf")
    monkeypatch.setenv("PYTHONPATH", "/checkout/src")
    monkeypatch.setenv("VIRTUAL_ENV", "/existing")
    observed = {}

    class FakeProcess:
        def wait(self, *, timeout):
            observed["timeout"] = timeout
            return 0

    def popen(command, **kwargs):
        observed.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(tool.subprocess, "Popen", popen)
    tool.run_step(["python", "-I"], tmp_path, "step", 3)
    assert "PIP_TARGET" not in observed["env"] and "PIP_USER" not in observed["env"]
    assert "PYTHONPATH" not in observed["env"] and "VIRTUAL_ENV" not in observed["env"]
    assert observed["env"]["PIP_CONFIG_FILE"] == tool.os.devnull
    assert observed["cwd"] == tmp_path
    assert observed["timeout"] == 3
    assert observed["start_new_session"] is True


def test_timeout_stops_pip_style_child_process_group(tmp_path):
    child = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        tool.run_step([sys.executable, "-I", "-c", child], tmp_path, "timeout", 0.4)
    cleanup = json.loads((tmp_path / "timeout-cleanup.json").read_text())
    assert cleanup["process_group_stopped"] is True
    assert cleanup["signals"][0] == "SIGTERM"
    assert not tool.group_exists(cleanup["process_group"])


def test_input_lock_tampering_cannot_inject_requirements(inputs):
    lock = tool.make_lock(inputs[3], inputs[2]["wheel_sha256"])
    lock["artifacts"][0]["name"] = "numpy\n--index-url=bad"
    with pytest.raises(RuntimeError, match="identity"):
        tool.requirements_text(lock, inputs[0])


def test_relative_python_is_absolute_without_dereferencing_venv_symlink(
    inputs,
    tmp_path,
    monkeypatch,
):
    calls = fake_steps(monkeypatch, inputs)
    monkeypatch.chdir(tmp_path)
    python = tmp_path / "selected-python"
    python.symlink_to(sys.executable)
    tool.prepare(inputs[0], inputs[1], tmp_path / "new", executable=Path("selected-python"))
    assert calls[0][1][0] == str(python)
    assert calls[0][1][0] != str(python.resolve())


def test_probe_records_vendor_metadata_without_treating_it_as_installed_closure(tmp_path):
    """Real isolated interpreter reproduces setuptools adding a vendored dist-info path."""
    destination = tmp_path / "isolated"
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--without-pip", str(destination)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    python = destination / "bin/python"
    result = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    site = Path(result.stdout.strip())
    environment = {
        "platform_version": "fixture",
        "packages": {},
        "source_hashes": {"fixture": "a" * 64},
    }
    for name in tool.MODULES:
        module = site.joinpath(*name.split("."))
        module.mkdir(parents=True, exist_ok=True)
        (module / "__init__.py").write_text("")
    (site / "camera_platform/service/__init__.py").write_text(
        "def environment_fingerprint():\n    return " + repr(environment) + "\n"
    )
    (site / "setuptools/__init__.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.append(str(Path(__file__).parent / '_vendor'))\n"
    )
    vendor = site / "setuptools/_vendor"
    for root, version in ((site, "26.3"), (vendor, "26.0")):
        metadata = root / ("packaging-" + version + ".dist-info")
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: packaging\nVersion: " + version + "\n"
        )
    result = subprocess.run(
        [str(python), "-I", "-B", "-c", tool.PROBE, json.dumps(tool.MODULES)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    probe = json.loads(result.stdout)
    assert probe["distributions"] == [
        {"name": "packaging", "version": "26.3", "location": str(site)}
    ]
    assert {entry["version"] for entry in probe["visible_distributions"]} == {"26.0", "26.3"}
    assert str(vendor) in probe["sys_path"]
    assert probe["installation_paths"] == [str(site)]
    assert tool.verify_probe(probe, destination, environment) == environment


def test_real_installed_duplicate_is_still_rejected(inputs, tmp_path, monkeypatch):
    def duplicate(probe):
        probe["distributions"].append(copy.deepcopy(probe["distributions"][0]))

    fake_steps(monkeypatch, inputs, probe_change=duplicate)
    with pytest.raises(RuntimeError, match="Duplicate installed distribution identities"):
        tool.prepare(inputs[0], inputs[1], tmp_path / "new")


def test_external_visible_vendor_metadata_is_rejected(inputs, tmp_path):
    destination = tmp_path / "new"
    probe = valid_probe(destination, inputs[2]["environment"])
    probe["visible_distributions"].append(
        {"name": "packaging", "version": "26.0", "location": "/external/setuptools/_vendor"}
    )
    with pytest.raises(RuntimeError, match="escape"):
        tool.verify_probe(probe, destination, inputs[2]["environment"])


def test_vendor_metadata_cannot_be_substituted_for_installed_closure(inputs, tmp_path):
    destination = tmp_path / "new"
    probe = valid_probe(destination, inputs[2]["environment"])
    probe["distributions"][0]["location"] += "/setuptools/_vendor"
    with pytest.raises(RuntimeError, match="installation roots"):
        tool.verify_probe(probe, destination, inputs[2]["environment"])
