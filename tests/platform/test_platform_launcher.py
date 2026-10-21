"""Trusted launcher persistence, serial-backend checks and occupied-port safety."""

import hashlib
import importlib.util
import io
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "tools/start_camera_platform.py"
SPEC = importlib.util.spec_from_file_location("platform_launcher", MODULE_PATH)
launcher = importlib.util.module_from_spec(SPEC)
with pytest.MonkeyPatch.context() as import_context:
    import_context.syspath_prepend(str(MODULE_PATH.parent))
    SPEC.loader.exec_module(launcher)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    script = workspace / "src/camera_platform/meep_factory.py"
    script.parent.mkdir(parents=True)
    script.write_text("# Fixed probe fixture; unit tests execute no solver\n")
    executable = tmp_path / "solver Python"
    executable.write_text("# Explicit executable fixture\n")
    executable.chmod(0o700)
    monkeypatch.setattr(launcher, "WORKSPACE", workspace)
    monkeypatch.delenv(launcher.MEEP_ENV, raising=False)
    monkeypatch.delenv(launcher.MITSUBA_ENV, raising=False)
    return workspace, executable, tmp_path / "state"


def _probe(workspace, executable):
    return {
        "available": True,
        "algorithm": "meep_planar_optical_absorption_v1",
        "meep_version": "1.33.0",
        "python_executable": str(executable),
        "python_version": "3.10.0",
        "numpy_version": "1.26.4",
        "mpi_enabled": False,
        "processor_count": 1,
        "dimensions": 1,
        "adapter_sha256": hashlib.sha256(
            (workspace / "src/camera_platform/meep_factory.py").read_bytes()
        ).hexdigest(),
        "meep_python_module_sha256": "1" * 64,
        "meep_compiled_extension_sha256": "2" * 64,
    }


def _unreachable(*args, **kwargs):
    pytest.fail("This operation must not occur")


def test_fixed_probe_uses_argument_list_isolation_and_timeout(setup, monkeypatch):
    workspace, executable, _ = setup
    expected = _probe(workspace, executable)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps(expected))

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.probe_meep_interpreter(str(executable)) == expected
    args, options = calls[0]
    assert args == [
        str(executable),
        "-I",
        str(workspace / "src/camera_platform/meep_factory.py"),
        "--probe",
    ]
    assert options == {"capture_output": True, "text": True, "timeout": 15, "check": True}


@pytest.mark.parametrize(
    "bad",
    [
        "version",
        "mpi",
        "processors",
        "bool_processors",
        "algorithm",
        "script_hash",
        "not_json",
        "failure",
        "timeout",
    ],
)
def test_invalid_or_nonserial_probe_is_refused(setup, monkeypatch, bad):
    workspace, executable, _ = setup
    result = _probe(workspace, executable)
    changes = {
        "version": ("meep_version", "1.32.0"),
        "mpi": ("mpi_enabled", True),
        "processors": ("processor_count", 2),
        "bool_processors": ("processor_count", True),
        "algorithm": ("algorithm", "different-solver"),
        "script_hash": ("adapter_sha256", "0" * 64),
    }
    if bad in changes:
        key, value = changes[bad]
        result[key] = value

    def run(*args, **kwargs):
        if bad == "failure":
            raise subprocess.CalledProcessError(1, args[0])
        if bad == "timeout":
            raise subprocess.TimeoutExpired(args[0], 15)
        return SimpleNamespace(stdout="not JSON" if bad == "not_json" else json.dumps(result))

    monkeypatch.setattr(launcher.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Meep"):
        launcher.probe_meep_interpreter(str(executable))


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"schema":"unknown","meep_python":"/tmp/python"}',
        '{"schema":"camera_platform_runtime_v1","meep_python":"relative/python"}',
        '{"schema":"camera_platform_runtime_v1","meep_python":null}',
        '{"schema":"camera_platform_runtime_v1","meep_python":"/tmp/python","env":{"SHELL":"bad"}}',
        " " * 8193,
    ],
)
def test_malformed_or_extra_configuration_is_not_interpreted(setup, content):
    _, _, state = setup
    state.mkdir()
    (state / "runtime-config.json").write_text(content)
    with pytest.raises(RuntimeError, match="configuration|runtime-config"):
        launcher.load_runtime_config(state)


def test_configuration_symlink_is_not_followed_or_overwritten(setup, tmp_path):
    _, executable, state = setup
    state.mkdir()
    other = tmp_path / "other.json"
    other.write_text("keep original")
    (state / "runtime-config.json").symlink_to(other)
    with pytest.raises(RuntimeError, match="regular local file"):
        launcher.load_runtime_config(state)
    with pytest.raises(RuntimeError, match="regular local file"):
        launcher.save_runtime_config(state, str(executable))
    assert other.read_text() == "keep original"


def test_explicit_environment_precedes_saved_binding_and_conflicting_cli_refuses(setup):
    workspace, executable, _ = setup
    alternate = workspace / "other-python"
    alternate.write_text("fixture")
    alternate.chmod(0o700)
    environment = {launcher.MEEP_ENV: str(executable)}
    assert launcher.select_meep_binding(None, environment, str(alternate)) == str(executable)
    assert launcher.select_meep_binding(executable, environment, str(alternate)) == str(executable)
    assert launcher.select_meep_binding(alternate, {}, str(executable)) == str(alternate)
    with pytest.raises(RuntimeError, match="takes precedence and conflicts"):
        launcher.select_meep_binding(alternate, environment, str(executable))
    with pytest.raises(RuntimeError, match="empty value"):
        launcher.select_meep_binding(None, {launcher.MEEP_ENV: ""}, str(executable))


def test_missing_or_nonexecutable_selected_path_refuses_before_probe(setup):
    workspace, executable, _ = setup
    executable.chmod(0o600)
    with pytest.raises(RuntimeError, match="executable file"):
        launcher.select_meep_binding(executable, {}, None)
    with pytest.raises(RuntimeError, match="executable file"):
        launcher.select_meep_binding(workspace / "missing", {}, None)


def test_cli_saves_after_verified_start_and_finder_relaunch_uses_it(setup, monkeypatch):
    workspace, executable, state = setup
    probes, starts, checks = [], [], []
    states = iter([False, True])
    monkeypatch.setattr(launcher, "ready", lambda *args: next(states))

    def probe(selected):
        probes.append(selected)
        return _probe(workspace, executable)

    def popen(args, **kwargs):
        starts.append((args, kwargs))
        assert not (state / "runtime-config.json").exists()
        return SimpleNamespace(pid=12345, poll=lambda: None)

    monkeypatch.setattr(launcher, "probe_meep_interpreter", probe)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "verify_running_meep_binding", lambda *args: checks.append(args))
    assert (
        launcher.main(["--state-dir", str(state), "--meep-python", str(executable), "--no-open"])
        == 0
    )
    saved = json.loads((state / "runtime-config.json").read_text())
    assert saved == {"schema": launcher.CONFIG_SCHEMA, "meep_python": str(executable)}
    assert starts[0][1]["env"][launcher.MEEP_ENV] == str(executable)
    assert starts[0][1]["start_new_session"] is True
    assert len(checks) == len(probes) == 1
    assert not list(state.glob("runtime-config-*.tmp"))
    monkeypatch.setattr(launcher, "ready", lambda *args: True)
    assert launcher.main(["--state-dir", str(state), "--no-open"]) == 0
    assert probes == [str(executable), str(executable)]
    assert len(starts) == 1 and len(checks) == 2


def test_invalid_probe_does_not_save_or_start_server(setup, monkeypatch):
    _, executable, state = setup
    monkeypatch.setattr(launcher, "ready", lambda *args: False)

    def reject(*args):
        raise RuntimeError("invalid probe")

    monkeypatch.setattr(launcher, "probe_meep_interpreter", reject)
    monkeypatch.setattr(launcher.subprocess, "Popen", _unreachable)
    with pytest.raises(RuntimeError, match="invalid probe"):
        launcher.main(["--state-dir", str(state), "--meep-python", str(executable), "--no-open"])
    assert not state.exists()


def test_ready_server_changed_binding_is_not_saved_or_restarted(setup, monkeypatch):
    workspace, executable, state = setup
    state.mkdir()
    original = {"schema": launcher.CONFIG_SCHEMA, "meep_python": str(executable)}
    config = state / "runtime-config.json"
    config.write_text(json.dumps(original))
    original_bytes = config.read_bytes()
    other = workspace / "other-python"
    other.write_text("fixture")
    other.chmod(0o700)
    monkeypatch.setattr(launcher, "ready", lambda *args: True)
    monkeypatch.setattr(launcher, "probe_meep_interpreter", lambda *args: _probe(workspace, other))

    def reject(*args):
        raise RuntimeError("different Meep binding; restart required")

    monkeypatch.setattr(launcher, "verify_running_meep_binding", reject)
    monkeypatch.setattr(launcher.subprocess, "Popen", _unreachable)
    with pytest.raises(RuntimeError, match="different Meep binding"):
        launcher.main(["--state-dir", str(state), "--meep-python", str(other), "--no-open"])
    assert config.read_bytes() == original_bytes


def test_running_binding_compares_actual_backend_and_probe(setup, monkeypatch):
    workspace, executable, _ = setup
    probe = _probe(workspace, executable)
    response = [
        {
            "plugin_id": "meep_planar_stack_v1",
            "available": True,
            "backend": {"executable": str(executable), "probe": probe},
        }
    ]
    monkeypatch.setattr(
        launcher,
        "build_opener",
        lambda *args: SimpleNamespace(
            open=lambda *args, **kwargs: io.BytesIO(json.dumps(response).encode())
        ),
    )
    launcher.verify_running_meep_binding("http://127.0.0.1:8770/", str(executable), probe)
    response[0]["backend"]["executable"] = "/different/python"
    with pytest.raises(RuntimeError, match="different or unavailable Meep binding"):
        launcher.verify_running_meep_binding("http://127.0.0.1:8770/", str(executable), probe)


def test_unrelated_occupied_http_port_is_left_untouched(setup, monkeypatch):
    _, executable, state = setup

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"service":"unrelated fixture"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(launcher, "probe_meep_interpreter", _unreachable)
    monkeypatch.setattr(launcher.subprocess, "Popen", _unreachable)
    try:
        with pytest.raises(RuntimeError, match="different service or workspace"):
            launcher.main(
                [
                    "--port",
                    str(server.server_port),
                    "--state-dir",
                    str(state),
                    "--meep-python",
                    str(executable),
                    "--no-open",
                ]
            )
        assert thread.is_alive()
        assert not state.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_unrecognized_timeout_does_not_treat_an_occupied_port_as_free(setup, monkeypatch):
    _, _, state = setup

    def fail(*args, **kwargs):
        raise URLError(TimeoutError("unrelated listener did not speak HTTP"))

    monkeypatch.setattr(launcher, "build_opener", lambda *args: SimpleNamespace(open=fail))
    with pytest.raises(RuntimeError, match="recognized Camera Platform"):
        launcher.ready("http://127.0.0.1:8770/", state)


def test_solver_config_extension_preserves_legacy_setting_and_venv_selection(setup):
    workspace, executable, state = setup
    venv = workspace / "venv/bin/python"
    venv.parent.mkdir(parents=True)
    venv.symlink_to(executable)
    launcher.save_runtime_config(state, str(executable))
    assert launcher.load_runtime_config(state, "mitsuba_python") is None
    assert launcher.select_scene_binding(venv, {}, None) == str(venv)
    launcher.save_runtime_config(state, str(venv), "mitsuba_python")
    launcher.save_runtime_config(state, str(executable))
    assert launcher.load_runtime_config(state) == str(executable)
    assert launcher.load_runtime_config(state, "mitsuba_python") == str(venv)
    with pytest.raises(RuntimeError, match="conflicts"):
        launcher.select_scene_binding(executable, {launcher.MITSUBA_ENV: str(venv)}, None)
    assert launcher.select_scene_binding(None, {}, str(venv)) == str(venv)


def test_mitsuba_probe_uses_pinned_adapter_and_exact_backend_identity(setup, monkeypatch):
    workspace, executable, _ = setup
    installed = workspace / "installed"
    script = installed / "camera_platform/mitsuba_scene.py"
    script.parent.mkdir(parents=True)
    script.write_text("# Installed trusted adapter")
    versions = {
        "mitsuba": "3.9.1",
        "drjit": "1.5.0",
        "variant": "scalar_spectral",
        "numpy": "2.4.2",
        "python": "3.12.13",
    }
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps(versions))

    monkeypatch.setattr(launcher.subprocess, "run", run)
    backend = launcher.probe_scene_interpreter(str(executable), runtime_root=installed)
    assert calls[0][0] == [str(executable), "-I", str(script), "--probe"]
    assert calls[0][1]["timeout"] == 15 and calls[0][1]["check"]
    assert backend["adapter_sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()
    versions["variant"] = "scalar_rgb"
    with pytest.raises(RuntimeError, match="Mitsuba"):
        launcher.probe_scene_interpreter(str(executable), runtime_root=installed)


def test_mitsuba_live_binding_mismatch_is_not_saved_or_restarted(setup, monkeypatch):
    _, executable, state = setup
    launcher.save_runtime_config(state, str(executable), "mitsuba_python")
    original = (state / "runtime-config.json").read_bytes()
    monkeypatch.setattr(launcher, "ready", lambda *a: True)
    monkeypatch.setattr(launcher, "probe_scene_interpreter", lambda *a, **k: {"versions": {}})

    def reject(*a):
        raise RuntimeError("different Mitsuba binding")

    monkeypatch.setattr(launcher, "verify_running_scene_binding", reject)
    monkeypatch.setattr(launcher.subprocess, "Popen", _unreachable)
    with pytest.raises(RuntimeError, match="different Mitsuba"):
        launcher.main(["--state-dir", str(state), "--mitsuba-python", str(executable), "--no-open"])
    assert (state / "runtime-config.json").read_bytes() == original


def test_saved_mitsuba_binding_is_restored_without_shell_environment(setup, monkeypatch):
    _, executable, state = setup
    launcher.save_runtime_config(state, str(executable), "mitsuba_python")
    statuses = iter([False, True])
    starts, checks = [], []
    monkeypatch.setattr(launcher, "ready", lambda *a: next(statuses))
    monkeypatch.setattr(launcher, "probe_scene_interpreter", lambda *a, **k: {"verified": True})
    monkeypatch.setattr(launcher, "verify_running_scene_binding", lambda *a: checks.append(a))

    def start(args, **kwargs):
        starts.append(kwargs)
        return SimpleNamespace(pid=12345, poll=lambda: None)

    monkeypatch.setattr(launcher.subprocess, "Popen", start)
    launcher.main(["--state-dir", str(state), "--no-open"])
    assert starts[0]["env"][launcher.MITSUBA_ENV] == str(executable)
    assert checks == [("http://127.0.0.1:8770/", {"verified": True})]


def test_mitsuba_failed_probe_never_saves_binding(setup, monkeypatch):
    _, executable, state = setup
    monkeypatch.setattr(launcher, "ready", lambda *a: False)

    def reject(*a, **k):
        raise RuntimeError("invalid Mitsuba probe")

    monkeypatch.setattr(launcher, "probe_scene_interpreter", reject)
    monkeypatch.setattr(launcher.subprocess, "Popen", _unreachable)
    with pytest.raises(RuntimeError, match="invalid Mitsuba"):
        launcher.main(["--state-dir", str(state), "--mitsuba-python", str(executable), "--no-open"])
    assert not state.exists()
