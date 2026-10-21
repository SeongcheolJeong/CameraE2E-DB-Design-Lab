"""Independent Worker evidence admission; no server, environment install or primary access."""

import copy
import hashlib
import importlib.util
import os
import sys
import zipfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
try:
    SPEC = importlib.util.spec_from_file_location(
        "independent_worker", TOOLS / "verify_camera_independent_worker.py"
    )
    tool = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(tool)
finally:
    sys.path.remove(str(TOOLS))


@pytest.fixture
def evidence(tmp_path):
    python = tmp_path / "env/bin/python"
    wheel = tmp_path / "pyisetcam-0.1.0-py3-none-any.whl"
    payload = {"camera_platform/__init__.py": b"fixture", "pyisetcam/__init__.py": b"fixture"}
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in payload.items():
            archive.writestr(name, content)
    environment = {
        "platform_version": "fixture",
        "packages": {"numpy": "new"},
        "source_hashes": {"fixture": "a" * 64},
    }
    calculated = {
        "status": "passed",
        "environment": environment,
        "raw_and_rgb_artifact_http_hashes_verified": True,
        "registry_integrity": "verified",
    }
    execution = {
        "schema": "camera_independent_environment_execution_v1",
        "status": "passed",
        "original_proof_reused_as_execution_evidence": False,
        "process_stopped": True,
        "cleanup": {"process_group_stopped": True},
        "python": str(python),
        "calculation": calculated,
        "wheel_sha256": tool.sha(wheel),
        "probe": {
            "environment": environment,
            "payload_sha256": {
                name: hashlib.sha256(value).hexdigest() for name, value in payload.items()
            },
        },
    }
    prepared = {
        "status": "prepared",
        "pip_check": "passed",
        "system_site_packages": False,
        "destination": str(python.parent.parent),
        "environment": environment,
        "wheel_sha256": tool.sha(wheel),
    }
    return execution, prepared, wheel, python


def test_acceptance_is_derived_from_actual_new_environment_execution(evidence):
    execution, prepared, wheel, python = evidence
    proof = tool.package_proof(execution, prepared, wheel, python)
    assert proof["environment"] == execution["calculation"]["environment"]
    assert proof["original_candidate_execution_reused"] is False
    assert proof["independent_payload_file_count"] == 2
    assert proof["wheel_sha256"] == tool.sha(wheel)


@pytest.mark.parametrize(
    "fault",
    [
        "old-proof",
        "failed",
        "not-stopped",
        "old-environment",
        "missing-payload",
        "different-payload",
        "wrong-python",
        "shared-site",
        "wrong-wheel",
        "missing-raw-evidence",
    ],
)
def test_unproven_or_misbound_environment_cannot_prepare_runtime(evidence, fault):
    execution, prepared, wheel, python = copy.deepcopy(evidence)
    if fault == "old-proof":
        execution["original_proof_reused_as_execution_evidence"] = True
    elif fault == "failed":
        execution["calculation"]["status"] = "failed"
    elif fault == "not-stopped":
        execution["cleanup"]["process_group_stopped"] = False
    elif fault == "old-environment":
        execution["calculation"]["environment"] = {"other": True}
    elif fault == "missing-payload":
        execution["probe"]["payload_sha256"].pop("pyisetcam/__init__.py")
    elif fault == "different-payload":
        execution["probe"]["payload_sha256"]["pyisetcam/__init__.py"] = "f" * 64
    elif fault == "wrong-python":
        execution["python"] = "/existing/python"
    elif fault == "shared-site":
        prepared["system_site_packages"] = True
    elif fault == "wrong-wheel":
        prepared["wheel_sha256"] = "0" * 64
    else:
        execution["calculation"]["raw_and_rgb_artifact_http_hashes_verified"] = False
    with pytest.raises(RuntimeError):
        tool.package_proof(execution, prepared, wheel, python)


def test_command_uses_explicit_venv_and_verified_bootstrap_not_launcher_cli(tmp_path):
    python = tmp_path / "independent/bin/python"
    command = tool.launch_command(
        python,
        tmp_path / "runtime",
        tmp_path / "external-qa-state",
        "a" * 64,
        tmp_path / "workspace",
        48901,
    )
    assert command[0] == str(python)
    assert tool.launcher.INSTALLED_BOOTSTRAP in command
    assert str(tmp_path / "runtime") in command
    assert not any("start_camera_platform.py" in part for part in command)


@pytest.mark.parametrize("fault", ["primary-port", "non-qa", "relative-python"])
def test_command_refuses_primary_or_ambiguous_launch(tmp_path, fault):
    python = tmp_path / "python"
    state = tmp_path / "external-qa-state"
    port = 48901
    if fault == "primary-port":
        port = 8770
    elif fault == "non-qa":
        state = tmp_path / "state"
    else:
        python = Path("relative/python")
    with pytest.raises(RuntimeError):
        tool.launch_command(python, tmp_path / "runtime", state, "a" * 64, tmp_path, port)


@pytest.mark.parametrize("restart", [False, True])
def test_readiness_failure_cleans_newly_spawned_process_even_during_restart(tmp_path, restart):
    owner = tool.OwnedAPI()
    command = [sys.executable, "-I", "-c", "import time; time.sleep(60)"]
    old = None
    with (tmp_path / "owned.log").open("w") as log:
        if restart:
            old = owner.spawn(command, cwd=tmp_path, env=os.environ.copy(), log=log)
            assert owner.cleanup()["process_group_stopped"]
        try:
            current = owner.spawn(command, cwd=tmp_path, env=os.environ.copy(), log=log)
            assert owner.process is current
            if old is not None:
                assert current.pid != old.pid and old.poll() is not None
            # Failure occurs after Popen but before launch() could return its PID.
            raise RuntimeError("readiness or running binding check failed")
        except RuntimeError:
            cleanup = owner.cleanup()
        assert cleanup["process_group"] == current.pid
        assert cleanup["process_group_stopped"] is True
        assert current.poll() is not None
