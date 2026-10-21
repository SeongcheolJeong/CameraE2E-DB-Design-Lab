#!/usr/bin/env python3
"""Start the Camera Platform detached, verifying workspace and server identity."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

from camera_runtime_assets import (
    BINDING_SCHEMA as ASSET_BINDING_SCHEMA,
)
from camera_runtime_assets import (
    BOOTSTRAP_ASSET_VERIFY_CODE,
)
from camera_runtime_assets import (
    MANIFEST_NAME as ASSET_MANIFEST_NAME,
)
from camera_runtime_assets import (
    validate_binding as validate_asset_binding,
)
from camera_runtime_rollback import transition_lock

WORKSPACE = Path(__file__).resolve().parents[1]
CONFIG_SCHEMA = "camera_platform_runtime_v1"
MEEP_ENV = "CAMERA_PLATFORM_MEEP_PYTHON"
MITSUBA_ENV = "CAMERA_PLATFORM_MITSUBA_PYTHON"
PACKAGE_SCHEMA = "camera_platform_installed_package_v1"
PIN_SCHEMA = "camera_platform_runtime_pin_v1"
PIN_SCHEMA_V2 = "camera_platform_runtime_pin_v2"
ASSET_ENV = "PYISETCAM_UPSTREAM_ROOT"
ASSET_CACHE_ENV = "PYISETCAM_CACHE_ROOT"


def asset_binding_from_manifest(value):
    """Bind only the verified prepared asset manifest at its canonical root."""
    path = Path(value).expanduser().absolute()
    if (
        path.name != ASSET_MANIFEST_NAME
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 2_097_152
    ):
        raise RuntimeError("Runtime assets require the prepared asset-manifest.json file")
    root = path.parent.resolve()
    binding = {
        "schema": ASSET_BINDING_SCHEMA,
        "root": str(root),
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    validate_asset_binding(binding)
    return binding


def runtime_asset_environment(binding):
    """A v2 selection overrides ambient assets, including its explicit absence."""
    environment = dict(os.environ)
    if binding and binding["schema"] == PIN_SCHEMA_V2:
        environment.pop(ASSET_CACHE_ENV, None)
        assets = binding["assets"]
        if assets is not None:
            validate_asset_binding(assets)
            environment[ASSET_ENV] = assets["root"]
        else:
            environment.pop(ASSET_ENV, None)
    return environment


# Shared by the isolated preflight and the actual installed process. Keep the
# invoked venv path: resolving bin/python before execution loses pyvenv.cfg.
PYTHON_IDENTITY_CODE = r"""
import hashlib, json, os, sys
from pathlib import Path
def runtime_python_identity():
    executable = Path(os.path.abspath(sys.executable))
    prefix, base_prefix = Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()
    config = prefix / "pyvenv.cfg"
    with executable.open("rb") as stream:
        binary_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"executable": str(executable), "resolved_executable": str(executable.resolve()),
            "executable_sha256": binary_hash, "prefix": str(prefix),
            "base_prefix": str(base_prefix), "version": sys.version,
            "implementation": sys.implementation.name,
            "pyvenv_cfg_sha256": hashlib.sha256(config.read_bytes()).hexdigest()
                if config.is_file() else None}
"""


def runtime_python_path(value):
    """Normalize lexical spelling without dereferencing the selected venv path."""
    try:
        path = Path(os.path.abspath(Path(value).expanduser()))
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError("not executable")
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeError("Runtime Python must name an executable file") from error
    return str(path)


def default_runtime_python():
    python = WORKSPACE / ".venv-platform/bin/python"
    return str(python) if python.is_file() else sys.executable


def _validate_python_identity(value):
    keys = {
        "executable",
        "resolved_executable",
        "executable_sha256",
        "prefix",
        "base_prefix",
        "version",
        "implementation",
        "pyvenv_cfg_sha256",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise RuntimeError("Runtime Python binding requires its exact identity fields")
    for key in ("executable", "resolved_executable", "prefix", "base_prefix"):
        path = value[key]
        if (
            not isinstance(path, str)
            or not path
            or "\x00" in path
            or not Path(path).is_absolute()
            or os.path.abspath(path) != path
        ):
            raise RuntimeError("Runtime Python binding requires absolute normalized paths")
    for key in ("executable_sha256", "pyvenv_cfg_sha256"):
        checksum = value[key]
        if key == "pyvenv_cfg_sha256" and checksum is None:
            continue
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(c not in "0123456789abcdef" for c in checksum)
        ):
            raise RuntimeError("Runtime Python binding has an invalid checksum")
    if any(
        not isinstance(value[k], str) or not value[k] or len(value[k]) > 1024
        for k in ("version", "implementation")
    ):
        raise RuntimeError("Runtime Python binding has an invalid version identity")


def probe_runtime_python(executable):
    selected = runtime_python_path(executable)
    try:
        result = subprocess.run(
            [
                selected,
                "-I",
                "-B",
                "-c",
                PYTHON_IDENTITY_CODE + "\nprint(json.dumps(runtime_python_identity()))",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        if len(result.stdout) > 8192:
            raise ValueError("oversized interpreter identity")
        identity = json.loads(result.stdout)
        _validate_python_identity(identity)
        if identity["executable"] != selected:
            raise ValueError("interpreter wrapper changed the selected executable")
        return identity
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        raise RuntimeError("Runtime Python failed its isolated identity probe") from error


def verify_runtime_python(binding, executable=None):
    """Resolve one installed selection; v1 retains its historical default policy."""
    pinned = binding.get("python") if binding["schema"] == PIN_SCHEMA_V2 else None
    if pinned and binding["assets"] is not None:
        validate_asset_binding(binding["assets"])
    selected = runtime_python_path(
        executable
        if executable is not None
        else pinned["executable"]
        if pinned
        else default_runtime_python()
    )
    if pinned and (selected != pinned["executable"] or probe_runtime_python(selected) != pinned):
        raise RuntimeError("Runtime Python differs from the pinned interpreter identity")
    actual = (
        probe_package(selected, binding["runtime_root"], runtime_asset_environment(binding))
        if pinned
        else probe_package(selected, binding["runtime_root"])
    )
    if actual["environment"] != binding["manifest"]["environment"]:
        raise RuntimeError(
            "Installed package dependencies or Python changed; "
            "prepare and verify a new runtime before launch"
        )
    if pinned and probe_runtime_python(selected) != pinned:
        raise RuntimeError("Runtime Python changed during package verification")
    return selected


def _json_file(path, limit=2_097_152):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise RuntimeError("Runtime metadata requires a bounded regular JSON file")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("Invalid installed runtime metadata") from error


def package_files(root):
    """Hash the exact installed payload; bytecode and unexpected files are not ignored."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Installed runtime must be a regular directory")
    files = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Installed runtime payload must not contain symbolic links")
        if path.is_file() and path != root / "runtime-manifest.json":
            total += path.stat().st_size
            if total > 512 * 1024**2 or len(files) >= 20000:
                raise RuntimeError("Installed runtime exceeds the supported payload size")
            with path.open("rb") as stream:
                files[path.relative_to(root).as_posix()] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
        elif not path.is_dir() and path != root / "runtime-manifest.json":
            raise RuntimeError("Installed runtime contains a non-regular payload entry")
    if not files or len(files) > 20000:
        raise RuntimeError("Installed runtime payload has an invalid file inventory")
    return files


def verify_manifest(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Installed runtime must be a regular directory")
    manifest = _json_file(root / "runtime-manifest.json")
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema", "wheel_sha256", "environment", "files"}
        or manifest["schema"] != PACKAGE_SCHEMA
        or not isinstance(manifest["environment"], dict)
        or not isinstance(manifest["wheel_sha256"], str)
        or len(manifest["wheel_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in manifest["wheel_sha256"])
    ):
        raise RuntimeError("Unsupported installed runtime manifest")
    if package_files(root) != manifest["files"]:
        raise RuntimeError("Installed runtime files changed; restore the verified package")
    return manifest


def _fingerprint(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def probe_package(executable, root, environment=None):
    code = """
import sys, json
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import camera_platform, pyisetcam
from camera_platform.service import environment_fingerprint
paths = {m.__name__: str(Path(m.__file__).resolve()) for m in [camera_platform, pyisetcam]}
assert all(Path(p).is_relative_to(root) for p in paths.values())
print(json.dumps({"environment": environment_fingerprint(), "module_paths": paths}))
"""
    try:
        result = subprocess.run(
            [str(executable), "-I", "-B", "-c", code, str(root)],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
            env=environment,
        )
        data = json.loads(result.stdout)
        if set(data) != {"environment", "module_paths"} or not data["environment"]["source_hashes"]:
            raise ValueError("Unexpected package probe")
        return data
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            "Installed Camera Platform package failed its isolated import probe"
        ) from error


def load_installed_runtime(state_dir):
    pin_path = Path(state_dir) / "installed-runtime.json"
    if not pin_path.exists() and not pin_path.is_symlink():
        return None
    pin = _json_file(pin_path, 8192)
    return verify_runtime_pin(pin)


def verify_runtime_pin(pin):
    keys = {"schema", "runtime_root", "manifest_sha256"}
    if isinstance(pin, dict) and pin.get("schema") == PIN_SCHEMA_V2:
        keys.update({"python", "assets"})
    if not isinstance(pin, dict) or set(pin) != keys:
        raise RuntimeError("Runtime pin accepts only its versioned identity fields")
    if pin["schema"] not in (PIN_SCHEMA, PIN_SCHEMA_V2) or not isinstance(pin["runtime_root"], str):
        raise RuntimeError("Unknown installed runtime pin")
    if pin["schema"] == PIN_SCHEMA_V2:
        _validate_python_identity(pin["python"])
        if pin["assets"] is not None:
            validate_asset_binding(pin["assets"])
    root = Path(pin["runtime_root"])
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise RuntimeError("Runtime pin requires a canonical absolute package path")
    manifest_path = root / "runtime-manifest.json"
    manifest = verify_manifest(root)
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != pin["manifest_sha256"]:
        raise RuntimeError("Installed runtime manifest failed its pinned checksum")
    return {**pin, "manifest": manifest}


def save_runtime_pin(state_dir, root, *, executable=None, assets=None):
    """Atomically select a prepared package. This does not restart a live server."""
    manifest = verify_manifest(root)
    root = Path(root).resolve()
    pin = {
        "schema": PIN_SCHEMA,
        "runtime_root": str(root),
        "manifest_sha256": hashlib.sha256(
            (root / "runtime-manifest.json").read_bytes()
        ).hexdigest(),
    }
    if assets is not None and executable is None:
        raise RuntimeError("Runtime assets require an explicit verified runtime Python")
    if executable is not None:
        pin.update(schema=PIN_SCHEMA_V2, python=probe_runtime_python(executable), assets=assets)
        verify_runtime_python({**pin, "manifest": manifest})
    return _write_runtime_pin(state_dir, pin)


def restore_runtime_pin(state_dir, pin, *, executable):
    """Restore the exact validated v1/v2 binding, not a candidate interpreter."""
    binding = verify_runtime_pin(pin)
    verify_runtime_python(binding, executable)
    return _write_runtime_pin(state_dir, pin)


def _write_runtime_pin(state_dir, pin):
    with transition_lock(state_dir):
        return _write_runtime_pin_locked(state_dir, pin)


def _write_runtime_pin_locked(state_dir, pin):
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    target = state / "installed-runtime.json"
    if target.is_symlink():
        raise RuntimeError("Runtime pin must be a regular local file")
    temporary = None
    if target.exists():
        previous = _json_file(target, 8192)
        if previous != pin:
            history = state / "runtime-pin-history"
            history.mkdir(exist_ok=True)
            archived = history / (_fingerprint(previous) + ".json")
            if not archived.exists():
                with archived.open("x") as stream:
                    json.dump(previous, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=state, prefix="runtime-pin-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(pin, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return pin


INSTALLED_BOOTSTRAP = (
    PYTHON_IDENTITY_CODE
    + BOOTSTRAP_ASSET_VERIFY_CODE
    + r"""
import sys, os, json, hashlib, tempfile
from pathlib import Path
root, state = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
expected_manifest = sys.argv[3]
arguments = sys.argv[4:]
expected_python = None
expected_assets = None
if arguments[:1] == ["--runtime-python-binding-json"]:
    expected_python = json.loads(arguments[1])
    arguments = arguments[2:]
    if arguments[:1] != ["--runtime-assets-binding-json"]:
        raise RuntimeError("Runtime asset binding missing")
    expected_assets = json.loads(arguments[1])
    arguments = arguments[2:]
def require(condition, message):
    if not condition:
        raise RuntimeError(message)
if expected_python is not None:
    require(runtime_python_identity() == expected_python, "Runtime Python identity changed")
    os.environ.pop("PYISETCAM_CACHE_ROOT", None)
    if expected_assets is not None:
        validate_binding(expected_assets)
        os.environ["PYISETCAM_UPSTREAM_ROOT"] = expected_assets["root"]
    else:
        os.environ.pop("PYISETCAM_UPSTREAM_ROOT", None)
manifest_bytes = (root / "runtime-manifest.json").read_bytes()
require(hashlib.sha256(manifest_bytes).hexdigest() == expected_manifest, "Runtime manifest changed")
manifest = json.loads(manifest_bytes)
files = {}
for p in root.rglob("*"):
    require(not p.is_symlink(), "Runtime symbolic link")
    if p.is_file() and p != root / "runtime-manifest.json":
        files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
require(files == manifest["files"], "Runtime payload changed")
sys.path.insert(0, str(root))
import camera_platform, pyisetcam
from camera_platform.service import environment_fingerprint
paths = {m.__name__: str(Path(m.__file__).resolve()) for m in [camera_platform, pyisetcam]}
require(all(Path(p).is_relative_to(root) for p in paths.values()), "Wrong imported package")
environment = environment_fingerprint()
require(environment == manifest["environment"], "Runtime environment changed")
witness = {"schema":"camera_platform_runtime_process_v1", "pid":os.getpid(),
           "runtime_root":str(root), "manifest_sha256":expected_manifest,
           "module_paths":paths, "environment":environment}
if expected_python is not None:
    require(runtime_python_identity() == expected_python, "Runtime Python identity changed")
    witness.update(schema="camera_platform_runtime_process_v2", python=expected_python,
                   assets=expected_assets)
with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as f:
    json.dump(witness, f)
    f.flush()
    os.fsync(f.fileno())
os.replace(f.name, state / "runtime-process.json")
sys.argv = ["camera_platform", *arguments]
from camera_platform.__main__ import main
main()
"""
)


def verify_running_package(url, state_dir, binding):
    """Match a live managed process and served package bytes to the selected pin."""
    try:
        witness = _json_file(Path(state_dir) / "runtime-process.json")
        pid = int((Path(state_dir) / "server.pid").read_text())
        pinned = binding.get("python") if binding["schema"] == PIN_SCHEMA_V2 else None
        if pinned:
            verify_runtime_python(binding)
        if (
            witness["schema"]
            != (
                "camera_platform_runtime_process_v2"
                if pinned
                else "camera_platform_runtime_process_v1"
            )
            or (pinned is not None and witness.get("python") != pinned)
            or (pinned is not None and witness.get("assets") != binding["assets"])
            or (pinned is not None and "assets" not in witness)
            or type(witness["pid"]) is not int
            or witness["pid"] != pid
            or pid <= 1
            or witness["runtime_root"] != binding["runtime_root"]
            or witness["manifest_sha256"] != binding["manifest_sha256"]
            or witness["environment"] != binding["manifest"]["environment"]
        ):
            raise ValueError("Different runtime process")
        os.kill(pid, 0)
        root = Path(binding["runtime_root"])
        for name in ["camera_platform", "pyisetcam"]:
            if Path(witness["module_paths"][name]) != root / name / "__init__.py":
                raise ValueError("Different imported package")
        with build_opener(ProxyHandler({})).open(url + "openapi.json", timeout=10) as response:
            version = json.load(response)["info"]["version"]
        if version != binding["manifest"]["environment"]["platform_version"]:
            raise ValueError("Different API version")
        for entry in binding["manifest"]["files"]:
            if not entry.startswith("camera_platform/static/"):
                continue
            name = entry.removeprefix("camera_platform/static/")
            with build_opener(ProxyHandler({})).open(url + name, timeout=10) as response:
                actual = hashlib.sha256(response.read()).hexdigest()
            if actual != binding["manifest"]["files"][entry]:
                raise ValueError("Different served static file")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        raise RuntimeError(
            "Running server does not match the selected installed package; "
            "stop it and relaunch. The running server was left untouched"
        ) from error


def ready(url, state_dir):
    try:
        with build_opener(ProxyHandler({})).open(url + "api/v1/health", timeout=2) as response:
            data = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Port is occupied by another HTTP service ({exc.code})") from exc
    except URLError as exc:
        if getattr(exc.reason, "errno", None) == errno.ECONNREFUSED:
            return False
        raise RuntimeError("Port did not return a recognized Camera Platform response") from exc
    except (TimeoutError, ConnectionError) as exc:
        raise RuntimeError("Port did not return a recognized Camera Platform response") from exc
    except ValueError as exc:
        raise RuntimeError("Port returned an unrecognized server response") from exc
    if (
        not isinstance(data, dict)
        or data.get("schema") != "camera_platform_v1"
        or data.get("workspace") != str(WORKSPACE)
        or data.get("state_dir") != str(state_dir)
    ):
        raise RuntimeError("This port belongs to a different service or workspace")
    if data.get("worker") != "running":
        raise RuntimeError(
            "The platform API is responding but its managed worker is stopped; "
            "inspect worker-supervisor.log"
        )
    return True


def _runtime_settings(state_dir):
    """Read only the two supported solver bindings, including legacy Meep-only files."""
    config = Path(state_dir) / "runtime-config.json"
    if config.is_symlink():
        raise RuntimeError("Runtime configuration must be a regular local file")
    if not config.exists():
        return {"schema": CONFIG_SCHEMA}
    if not config.is_file() or config.stat().st_size > 8192:
        raise RuntimeError("Runtime configuration must be a JSON file of at most 8192 bytes")
    try:
        data = _json_file(config, limit=8192)
    except (ValueError, UnicodeError, RuntimeError) as exc:
        raise RuntimeError("Invalid runtime-config.json; expected known solver settings") from exc
    allowed = {"meep_python", "mitsuba_python"}
    if (
        not isinstance(data, dict)
        or not set(data) <= allowed | {"schema"}
        or data.get("schema") != CONFIG_SCHEMA
        or not set(data) & allowed
        or any(
            not isinstance(data[k], str) or not data[k] or not Path(data[k]).is_absolute()
            for k in set(data) & allowed
        )
    ):
        raise RuntimeError("Runtime configuration accepts only schema and absolute solver paths")
    return data


def load_runtime_config(state_dir, setting="meep_python"):
    if setting not in {"meep_python", "mitsuba_python"}:
        raise RuntimeError("Unknown runtime configuration setting")
    return _runtime_settings(state_dir).get(setting)


def _interpreter(value):
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Meep interpreter path does not resolve to an executable file") from exc
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("Meep interpreter path must be an executable file")
    return str(path)


def select_meep_binding(cli_path, environment, saved_path):
    """An explicit environment override outranks persisted configuration."""
    supplied = environment.get(MEEP_ENV)
    if supplied is not None:
        if not isinstance(supplied, str) or not supplied:
            raise RuntimeError(f"{MEEP_ENV} must name an executable, not an empty value")
        selected = _interpreter(supplied)
        if cli_path is not None and _interpreter(cli_path) != selected:
            raise RuntimeError(
                f"{MEEP_ENV} takes precedence and conflicts with --meep-python; "
                "remove or align that environment variable before saving"
            )
        return selected
    if cli_path is not None:
        return _interpreter(cli_path)
    return _interpreter(saved_path) if saved_path is not None else None


def _scene_interpreter(value):
    # A venv's symlink path selects pyvenv.cfg; resolving it changes its dependencies.
    path = Path(os.path.abspath(Path(value).expanduser()))
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("Mitsuba interpreter path must be an executable file")
    return str(path)


def select_scene_binding(cli_path, environment, saved_path):
    supplied = environment.get(MITSUBA_ENV)
    if supplied is not None:
        if not isinstance(supplied, str) or not supplied:
            raise RuntimeError(f"{MITSUBA_ENV} must name an executable, not an empty value")
        selected = _scene_interpreter(supplied)
        if cli_path is not None and _scene_interpreter(cli_path) != selected:
            raise RuntimeError(
                f"{MITSUBA_ENV} takes precedence and conflicts with --mitsuba-python"
            )
        return selected
    selected = cli_path if cli_path is not None else saved_path
    return _scene_interpreter(selected) if selected is not None else None


def probe_scene_interpreter(executable, *, runtime_root=None):
    script = (
        Path(runtime_root) if runtime_root else WORKSPACE / "src"
    ) / "camera_platform/mitsuba_scene.py"
    try:
        result = subprocess.run(
            [executable, "-I", str(script), "--probe"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        if len(result.stdout) > 1_048_576:
            raise ValueError("oversized probe response")
        versions = json.loads(result.stdout)
        if (
            not isinstance(versions, dict)
            or versions.get("mitsuba") != "3.9.1"
            or versions.get("drjit") != "1.5.0"
            or versions.get("variant") != "scalar_spectral"
            or not isinstance(versions.get("numpy"), str)
            or not isinstance(versions.get("python"), str)
        ):
            raise ValueError("Unexpected spectral backend versions")
        return {
            "kind": "isolated_interpreter",
            "executable": executable,
            "executable_sha256": hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
            "adapter_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "versions": versions,
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Mitsuba interpreter failed the fixed scalar spectral probe") from exc


def verify_running_scene_binding(url, backend):
    try:
        with build_opener(ProxyHandler({})).open(
            url + "api/v1/scene_renders/catalog", timeout=20
        ) as response:
            catalog = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Cannot verify running Mitsuba binding; no setting was saved") from exc
    if (
        not isinstance(catalog, dict)
        or catalog.get("available") is not True
        or catalog.get("plugin_id") != "mitsuba_fixed_wavelength_scene_v1"
        or catalog.get("backend") != backend
    ):
        raise RuntimeError(
            "Running server has a different or unavailable Mitsuba binding; no setting was saved"
        )


def probe_meep_interpreter(executable, *, runtime_root=None):
    """Only the repository-owned, fixed probe script is executed, without a shell."""
    script = (
        Path(runtime_root) if runtime_root else WORKSPACE / "src"
    ) / "camera_platform/meep_factory.py"
    try:
        completed = subprocess.run(
            [executable, "-I", str(script), "--probe"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        if len(completed.stdout) > 1_048_576:
            raise ValueError("oversized probe response")
        probe = json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Meep interpreter failed the fixed serial-backend probe") from exc
    if (
        not isinstance(probe, dict)
        or probe.get("available") is not True
        or probe.get("meep_version") != "1.33.0"
        or probe.get("mpi_enabled") is not False
        or type(probe.get("processor_count")) is not int
        or probe["processor_count"] != 1
        or type(probe.get("dimensions")) is not int
        or probe["dimensions"] != 1
        or probe.get("algorithm") != "meep_planar_optical_absorption_v1"
        or probe.get("adapter_sha256") != hashlib.sha256(script.read_bytes()).hexdigest()
        or not isinstance(probe.get("python_executable"), str)
        or _interpreter(probe["python_executable"]) != executable
    ):
        raise RuntimeError("Meep configuration requires the fixed adapter with serial Meep 1.33.0")
    return probe


def save_runtime_config(state_dir, executable, setting="meep_python"):
    """Called only after the fixed probe and any live-server binding check pass."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "runtime-config.json"
    if target.is_symlink():
        raise RuntimeError("Runtime configuration must be a regular local file")
    if (
        setting not in {"meep_python", "mitsuba_python"}
        or not isinstance(executable, str)
        or not Path(executable).is_absolute()
    ):
        raise RuntimeError("Runtime configuration requires a known solver and absolute executable")
    data = {**_runtime_settings(state_dir), setting: executable}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=state_dir, prefix="runtime-config-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_running_meep_binding(url, executable, probe):
    try:
        with build_opener(ProxyHandler({})).open(
            url + "api/v1/factory/plugins", timeout=20
        ) as response:
            plugins = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "Cannot verify the running server's Meep binding; restart is required"
        ) from exc
    candidates = (
        [p for p in plugins if isinstance(p, dict) and p.get("plugin_id") == "meep_planar_stack_v1"]
        if isinstance(plugins, list)
        else []
    )
    backend = candidates[0].get("backend") if len(candidates) == 1 else None
    if (
        len(candidates) != 1
        or candidates[0].get("available") is not True
        or not isinstance(backend, dict)
        or backend.get("executable") != executable
        or backend.get("probe") != probe
    ):
        raise RuntimeError(
            "The running server has a different or unavailable Meep binding; "
            "stop it and relaunch before configuring this interpreter. No setting was saved"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument(
        "--development",
        action="store_true",
        help="Use checkout source instead of a saved installed package",
    )
    parser.add_argument("--state-dir", type=Path, default=WORKSPACE / "outputs/camera-platform")
    parser.add_argument(
        "--python",
        type=Path,
        help="Use this runtime Python; must match an explicit installed v2 pin",
    )
    parser.add_argument(
        "--meep-python",
        type=Path,
        help="Validate and save a local serial Meep 1.33.0 interpreter for future Finder launches",
    )
    parser.add_argument(
        "--mitsuba-python",
        type=Path,
        help="Validate and save the Mitsuba 3.9.1 spectral interpreter for Finder launches",
    )
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535")
    state = args.state_dir.resolve()
    with transition_lock(state):
        return _launch(args, state)


def _launch(args, state):
    url = f"http://127.0.0.1:{args.port}/"
    is_running = ready(url, state)
    binding = None if args.development else load_installed_runtime(state)
    executable = runtime_python_path(args.python) if args.python else default_runtime_python()
    if binding:
        executable = verify_runtime_python(binding, args.python)
        if is_running:
            verify_running_package(url, state, binding)
    elif args.development and is_running and (state / "runtime-process.json").exists():
        witness = _json_file(state / "runtime-process.json")
        if str(witness.get("pid")) == (state / "server.pid").read_text().strip():
            raise RuntimeError(
                "An installed runtime is running; stop it before using --development"
            )
    selected = select_meep_binding(args.meep_python, os.environ, load_runtime_config(state))
    probe = (
        probe_meep_interpreter(selected, runtime_root=binding["runtime_root"])
        if selected and binding
        else probe_meep_interpreter(selected)
        if selected
        else None
    )
    if is_running and selected:
        verify_running_meep_binding(url, selected, probe)
    scene_python = select_scene_binding(
        args.mitsuba_python, os.environ, load_runtime_config(state, "mitsuba_python")
    )
    scene_backend = (
        probe_scene_interpreter(
            scene_python, runtime_root=binding["runtime_root"] if binding else None
        )
        if scene_python
        else None
    )
    if is_running and scene_backend:
        verify_running_scene_binding(url, scene_backend)
    if not is_running:
        state.mkdir(parents=True, exist_ok=True)
        runtime_root = binding["runtime_root"] if binding else str(WORKSPACE / "src")
        env = {
            **runtime_asset_environment(binding),
            "PYTHONPATH": runtime_root,
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if selected:
            env[MEEP_ENV] = selected
        if scene_python:
            env[MITSUBA_ENV] = scene_python
        with (state / "server.log").open("ab") as log:
            command = (
                [
                    executable,
                    "-P",
                    "-B",
                    "-c",
                    INSTALLED_BOOTSTRAP,
                    runtime_root,
                    str(state),
                    binding["manifest_sha256"],
                ]
                if binding
                else [executable, "-m", "camera_platform"]
            )
            if binding and binding["schema"] == PIN_SCHEMA_V2:
                command += [
                    "--runtime-python-binding-json",
                    json.dumps(binding["python"]),
                    "--runtime-assets-binding-json",
                    json.dumps(binding["assets"]),
                ]
            process = subprocess.Popen(
                command
                + [
                    "serve",
                    "--workspace",
                    str(WORKSPACE),
                    "--state-dir",
                    str(state),
                    "--port",
                    str(args.port),
                ],
                cwd=WORKSPACE,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Server exited. See " + str(state / "server.log"))
            if ready(url, state):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Server start timed out; see " + str(state / "server.log"))
        (state / "server.pid").write_text(str(process.pid) + "\n")
        if binding:
            verify_running_package(url, state, binding)
        if selected:
            verify_running_meep_binding(url, selected, probe)
        if scene_backend:
            verify_running_scene_binding(url, scene_backend)
    if args.meep_python is not None:
        save_runtime_config(state, selected)
    if args.mitsuba_python is not None:
        save_runtime_config(state, scene_python, "mitsuba_python")
    print(url)
    if not args.no_open:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
