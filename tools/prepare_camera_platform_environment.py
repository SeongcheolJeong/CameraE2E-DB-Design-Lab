#!/usr/bin/env python3
"""Resolve a verified Camera Platform wheel into a new, independent dependency venv.

This prepares dependencies, not a validated camera runtime. Existing execution
proofs bind another environment and are never inherited. No live selection or
launcher configuration is changed. The destination must not already exist.

The saved pip report is evidence, not itself a lock. ``--lock`` replays the
separate artifact lock using direct URLs and pip's required SHA256 checks.
Source archive hashes pin inputs; isolated build dependencies and generated
wheels are not locked. Reproduction requires the same Python/platform and
continued access to the recorded artifacts. No network install runs on import.

References:
https://pip.pypa.io/en/stable/reference/installation-report/
https://pip.pypa.io/en/stable/topics/secure-installs/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from install_camera_platform_runtime import verify_wheel
from start_camera_platform import WORKSPACE, _json_file

DEFAULT_EXTRAS = ["platform", "factory-rayoptics", "factory-prysm"]
EXTRAS = DEFAULT_EXTRAS.copy()
LOCK_SCHEMA = "camera_platform_dependency_artifacts_v1"
REPORT_SCHEMA = "camera_platform_independent_environment_v1"
MODULES = [
    "camera_platform",
    "camera_platform.service",
    "pyisetcam",
    "numpy",
    "scipy",
    "skimage",
    "PIL",
    "fastapi",
    "pydantic",
    "uvicorn",
    "httpx",
    "multipart",
    "rayoptics",
    "opticalglass",
    "transforms3d",
    "prysm",
    "setuptools",
]
FACTORY_MODULES = {
    "factory-rayoptics": {"rayoptics", "opticalglass", "transforms3d"},
    "factory-prysm": {"prysm", "setuptools"},
}


def required_modules():
    """Probe only imports promised by the selected wheel extras."""
    optional = set().union(*FACTORY_MODULES.values())
    enabled = set().union(*(FACTORY_MODULES.get(extra, set()) for extra in EXTRAS))
    return [name for name in MODULES if name not in optional or name in enabled]
PROBE = r"""
import importlib, importlib.metadata, json, pathlib, site, sys, sysconfig
modules = json.loads(sys.argv[1])
paths = {}
for name in modules:
    module = importlib.import_module(name)
    paths[name] = str(pathlib.Path(module.__file__).resolve())
from camera_platform.service import environment_fingerprint
def inventory(search_paths=None):
    kwargs = {} if search_paths is None else {"path": search_paths}
    return [{"name": distribution.metadata["Name"], "version": distribution.version,
        "location": str(pathlib.Path(distribution.locate_file("")).resolve())}
        for distribution in importlib.metadata.distributions(**kwargs)]
# setuptools can append its _vendor directory to sys.path when imported. Its
# bundled dist-info is visible, but is not a separately installed distribution.
installation_paths = sorted({str(pathlib.Path(sysconfig.get_path(key)).resolve())
    for key in ("purelib", "platlib")})
print(json.dumps({"prefix": str(pathlib.Path(sys.prefix).resolve()),
    "base_prefix": str(pathlib.Path(sys.base_prefix).resolve()),
    "user_site_enabled": site.ENABLE_USER_SITE,
    "sys_path": [str(pathlib.Path(path).resolve()) for path in sys.path],
    "module_paths": paths, "installation_paths": installation_paths,
    "distributions": inventory(installation_paths),
    "visible_distributions": inventory(),
    "environment": environment_fingerprint()}))
"""


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def canonical_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise RuntimeError("Invalid dependency name")
    return re.sub(r"[-_.]+", "-", name).lower()


def artifact_entries(pip_report):
    """Validate pip's stable report and retain exact artifact and metadata evidence."""
    if (
        not isinstance(pip_report, dict)
        or pip_report.get("version") != "1"
        or not isinstance(pip_report.get("environment"), dict)
        or not isinstance(pip_report.get("install"), list)
        or not pip_report["install"]
    ):
        raise RuntimeError("A nonempty pip installation report version 1 is required")
    entries = []
    names = set()
    for item in pip_report["install"]:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid pip installation entry")
        metadata = item.get("metadata", {})
        download = item.get("download_info", {})
        if not isinstance(metadata, dict) or not isinstance(download, dict):
            raise RuntimeError("Invalid dependency artifact metadata")
        name = canonical_name(metadata.get("name"))
        version = metadata.get("version")
        url = download.get("url")
        archive = download.get("archive_info", {})
        hashes = archive.get("hashes", {}) if isinstance(archive, dict) else {}
        sha = hashes.get("sha256") if isinstance(hashes, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise RuntimeError(f"Dependency {name} lacks an exact archive SHA256")
        if not isinstance(version, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version
        ):
            raise RuntimeError(f"Invalid exact version for {name}")
        if not isinstance(url, str) or any(character.isspace() for character in url):
            raise RuntimeError(f"Invalid artifact URL for {name}")
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "file"} or parsed.username or parsed.password:
            raise RuntimeError("Artifacts require credential-free HTTPS or local file URLs")
        if parsed.fragment or (parsed.scheme == "https" and not parsed.hostname):
            raise RuntimeError("Artifact URL fragments or missing hosts are unsupported")
        if name in names:
            raise RuntimeError("Duplicate normalized dependency name")
        if "vcs_info" in download or "dir_info" in download:
            raise RuntimeError("Only hashed archive artifacts can be locked")
        names.add(name)
        entries.append(
            {"name": name, "version": version, "url": url, "sha256": sha, "metadata": metadata}
        )
    return sorted(entries, key=lambda entry: entry["name"])


def make_lock(pip_report, wheel_sha):
    entries = artifact_entries(pip_report)
    camera = [entry for entry in entries if entry["name"] == "pyisetcam"]
    if len(camera) != 1 or camera[0]["sha256"] != wheel_sha:
        raise RuntimeError("Resolved Camera Platform artifact differs from verified wheel")
    return {
        "schema": LOCK_SCHEMA,
        "wheel_sha256": wheel_sha,
        "extras": EXTRAS,
        "pip_version": pip_report.get("pip_version"),
        "marker_environment": pip_report["environment"],
        "artifacts": entries,
        "scope": "Exact downloaded runtime artifacts; build dependencies are not locked",
    }


def validate_lock(lock, wheel_sha):
    if (
        not isinstance(lock, dict)
        or lock.get("schema") != LOCK_SCHEMA
        or lock.get("wheel_sha256") != wheel_sha
        or lock.get("extras") != EXTRAS
        or not isinstance(lock.get("artifacts"), list)
    ):
        raise RuntimeError("Lock does not match the verified wheel and requested extras")
    # Reuse one parser so replay cannot bypass URL, duplicate or hash validation.
    report = {"version": "1", "environment": lock.get("marker_environment"), "install": []}
    for entry in lock["artifacts"]:
        if not isinstance(entry, dict):
            raise RuntimeError("Invalid lock artifact")
        metadata = entry.get("metadata", {})
        if (
            not isinstance(metadata, dict)
            or canonical_name(metadata.get("name")) != entry.get("name")
            or metadata.get("version") != entry.get("version")
        ):
            raise RuntimeError("Lock identity differs from preserved package metadata")
        report["install"].append(
            {
                "metadata": metadata,
                "download_info": {
                    "url": entry.get("url"),
                    "archive_info": {"hashes": {"sha256": entry.get("sha256")}},
                },
            }
        )
    return make_lock(report, wheel_sha)["artifacts"]


def requirements_text(lock, wheel):
    entries = validate_lock(lock, checksum(wheel))
    lines = ["# Exact runtime artifacts. Isolated build dependencies are not locked."]
    for entry in entries:
        name = entry["name"]
        url = entry["url"]
        if name == "pyisetcam":
            name += "[" + ",".join(EXTRAS) + "]"
            url = Path(wheel).resolve().as_uri()
        lines.append(f"{name} @ {url} --hash=sha256:{entry['sha256']}")
    return "\n".join(lines) + "\n"


def validate_destination(destination):
    raw = Path(os.path.abspath(destination))
    if raw.exists() or raw.is_symlink():
        raise RuntimeError("Destination must be a new path; existing directories are never reused")
    for parent in raw.parents:
        if parent.is_symlink():
            raise RuntimeError("Destination ancestors must not be symbolic links")
        if any(
            (parent / name).exists()
            for name in (
                "pyvenv.cfg",
                "installed-runtime.json",
                "runtime-manifest.json",
                "registry.sqlite",
                "registry.sqlite3",
            )
        ):
            raise RuntimeError("Destination must not be inside an existing environment or state")
    for protected in (
        WORKSPACE / "outputs/camera-platform",
        WORKSPACE / "outputs/camera-platform-runtimes",
    ):
        if raw == protected or protected in raw.parents:
            raise RuntimeError(
                "Destination must not be inside the primary state or installed runtimes"
            )
    if not raw.parent.is_dir():
        raise RuntimeError("Create the destination parent directory first")
    return raw


def clean_environment():
    # Do not inherit pip target/prefix/user/index/config options from another venv.
    result = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "PYTHON")) and key != "VIRTUAL_ENV"
    }
    result.update({"PIP_CONFIG_FILE": os.devnull, "PYTHONNOUSERSITE": "1"})
    return result


def group_exists(group):
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS can briefly report EPERM for a terminating orphaned group.
        # This is not proof of absence; retry until ESRCH or report incomplete cleanup.
        return True


def stop_process_group(process):
    """Stop only this step's new session, including pip build descendants."""
    signals = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_exists(process.pid):
            break
        try:
            os.killpg(process.pid, sig)
            signals.append(sig.name)
        except ProcessLookupError:
            break
        except PermissionError:
            signals.append(sig.name + "_permission_denied")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            process.poll()  # Reap the session leader before checking for remaining children.
            if not group_exists(process.pid):
                break
            time.sleep(0.05)
    process.wait(timeout=3)
    return {
        "process_group": process.pid,
        "signals": signals,
        "process_group_stopped": not group_exists(process.pid),
    }


def run_step(command, destination, label, timeout):
    log_path = destination / (label + ".log")
    with log_path.open("x") as log:
        process = subprocess.Popen(
            command,
            cwd=destination,
            env=clean_environment(),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            cleanup = stop_process_group(process)
            write_json(destination / (label + "-cleanup.json"), cleanup)
            if not cleanup["process_group_stopped"]:
                raise RuntimeError(
                    f"{label} interrupted; process group cleanup is incomplete"
                ) from None
            raise
        if returncode:
            cleanup = stop_process_group(process)
            write_json(destination / (label + "-cleanup.json"), cleanup)
            raise RuntimeError(f"{label} failed with exit {returncode}; see {log_path}")
    return log_path


def verify_probe(probe, destination, expected):
    root = destination.resolve()
    if (
        not isinstance(probe, dict)
        or probe.get("prefix") != str(root)
        or probe.get("base_prefix") == str(root)
        or probe.get("user_site_enabled") is not False
        or set(probe.get("module_paths", {})) != set(required_modules())
        or not isinstance(probe.get("distributions"), list)
        or not probe["distributions"]
        or not isinstance(probe.get("visible_distributions"), list)
        or not probe["visible_distributions"]
        or not isinstance(probe.get("installation_paths"), list)
        or not probe["installation_paths"]
    ):
        raise RuntimeError("Python probe did not prove an isolated venv")
    paths = list(probe["module_paths"].values())
    paths += [entry.get("location", "") for entry in probe["distributions"]]
    paths += [entry.get("location", "") for entry in probe["visible_distributions"]]
    paths += probe["installation_paths"]
    if any(not isinstance(path, str) or root not in Path(path).resolve().parents for path in paths):
        raise RuntimeError("Installed imports or distribution metadata escape the new venv")
    if any(
        entry["location"] not in probe["installation_paths"] for entry in probe["distributions"]
    ):
        raise RuntimeError("Installed distributions must come from venv installation roots")
    base = Path(probe["base_prefix"])
    if not isinstance(probe.get("sys_path"), list) or not probe["sys_path"]:
        raise RuntimeError("Python probe did not record its import search paths")
    for value in probe["sys_path"]:
        path = Path(value).resolve()
        if root in path.parents:
            continue
        if base not in path.parents or {"site-packages", "dist-packages"} & set(path.parts):
            raise RuntimeError("Python import search path includes an external package location")
    actual = probe.get("environment", {})
    if (
        not isinstance(actual, dict)
        or not isinstance(actual.get("packages"), dict)
        or actual.get("source_hashes") != expected.get("source_hashes")
        or not actual.get("source_hashes")
        or actual.get("platform_version") != expected.get("platform_version")
    ):
        raise RuntimeError("Independent environment does not contain the verified source payload")
    return actual


def prepare(
    wheel,
    verification_report,
    destination,
    *,
    executable=sys.executable,
    lock_path=None,
    offline_wheelhouse=None,
    timeout=1800,
):
    """Return a preparation report. Failure preserves evidence and never marks success."""
    executable = Path(executable).absolute()  # Preserve a venv symlink's interpreter identity.
    destination = validate_destination(destination)
    destination.mkdir(mode=0o700)  # Atomic claim; a concurrent creator cannot be overwritten.
    report_path = destination / "environment-report.json"
    report = {
        "schema": REPORT_SCHEMA,
        "status": "pending",
        "destination": str(destination),
        "started_at": datetime.now(UTC).isoformat(),
        "activated": False,
        "calculation_validation": "pending_in_this_environment",
        "prior_calculation_proof_applies": False,
        "scope": "Dependency preparation only; not camera accuracy or runtime acceptance",
    }
    write_json(report_path, report)
    try:
        wheel = Path(wheel).absolute()
        verification_report = Path(verification_report).absolute()
        prior = _json_file(verification_report)
        wheel_sha = verify_wheel(wheel, prior)
        report.update(
            {
                "wheel": str(wheel),
                "wheel_sha256": wheel_sha,
                "verification_report": str(verification_report),
                "verification_report_sha256": checksum(verification_report),
                "prior_environment": prior["environment"],
                "extras": EXTRAS,
            }
        )
        if offline_wheelhouse is not None:
            offline_wheelhouse = Path(offline_wheelhouse).absolute()
            if (offline_wheelhouse.is_symlink()
                    or not offline_wheelhouse.is_dir()
                    or not any(offline_wheelhouse.glob("*.whl"))):
                raise RuntimeError("Offline wheelhouse must be a nonempty regular directory")
            manifest_path = offline_wheelhouse / "wheelhouse-manifest.json"
            manifest = _json_file(manifest_path, limit=1024 * 1024)
            entries = manifest.get("artifacts") if isinstance(manifest, dict) else None
            if (not isinstance(manifest, dict)
                    or manifest.get("schema") != "camera_platform_r1_offline_wheelhouse_v1"
                    or manifest.get("r1_wheel_sha256") != wheel_sha
                    or not isinstance(entries, list)
                    or len(entries) != manifest.get("artifact_count")
                    or not entries):
                raise RuntimeError("Offline wheelhouse manifest does not match the R1 wheel")
            seen = set()
            for entry in entries:
                name = entry.get("filename") if isinstance(entry, dict) else None
                if (not isinstance(name, str) or not name.endswith(".whl")
                        or Path(name).name != name or name in seen):
                    raise RuntimeError("Offline wheelhouse has an invalid wheel filename")
                seen.add(name)
                item = offline_wheelhouse / name
                if (item.is_symlink() or not item.is_file()
                        or item.stat().st_size != entry.get("bytes")
                        or checksum(item) != entry.get("sha256")):
                    raise RuntimeError("Offline wheelhouse file differs from its manifest")
            report["offline_wheelhouse"] = str(offline_wheelhouse)
            report["offline_wheelhouse_manifest_sha256"] = checksum(manifest_path)
        lock = _json_file(lock_path, limit=16 * 1024**2) if lock_path else None
        if lock is not None:
            validate_lock(lock, wheel_sha)
            report["input_lock_sha256"] = checksum(lock_path)
        write_json(report_path, report)
        run_step(
            [str(executable), "-I", "-B", "-m", "venv", str(destination)],
            destination,
            "create-venv",
            min(timeout, 180),
        )
        config = (destination / "pyvenv.cfg").read_text().lower()
        if not re.search(r"(?m)^include-system-site-packages\s*=\s*false\s*$", config):
            raise RuntimeError("Fresh venv unexpectedly allows system site packages")
        report["pyvenv_config"] = (destination / "pyvenv.cfg").read_text()
        python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        pip_report_path = destination / "pip-installation-report.json"
        command = [
            str(python),
            "-I",
            "-B",
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-cache-dir",
            "--no-compile",
            "--ignore-installed",
            "--report",
            str(pip_report_path),
        ]
        if lock is None:
            command += [str(wheel) + "[" + ",".join(EXTRAS) + "]"]
        else:
            requirements = destination / "replay-requirements.txt"
            requirements.write_text(requirements_text(lock, wheel))
            command += ["--require-hashes", "-r", str(requirements)]
        if offline_wheelhouse is not None:
            command[command.index("install") + 1:command.index("install") + 1] = [
                "--no-index", "--find-links", str(offline_wheelhouse)
            ]
        report["install_command"] = command
        write_json(report_path, report)
        run_step(command, destination, "install", timeout)
        pip_report = _json_file(pip_report_path, limit=16 * 1024**2)
        resolved_lock = make_lock(pip_report, wheel_sha)
        if lock is not None:
            expected = validate_lock(lock, wheel_sha)
            actual = resolved_lock["artifacts"]

            def identity(entries):
                return {entry["name"]: (entry["version"], entry["sha256"]) for entry in entries}

            if identity(actual) != identity(expected):
                raise RuntimeError("Resolved artifacts differ from replay lock")
            # Full patch Python and target OS/architecture are part of the replay contract.
            for key in (
                "python_full_version",
                "implementation_name",
                "platform_machine",
                "sys_platform",
            ):
                if pip_report["environment"].get(key) != lock["marker_environment"].get(key):
                    raise RuntimeError(f"Replay environment differs for {key}")
        lock_output = destination / "dependency-lock.json"
        write_json(lock_output, resolved_lock)
        (destination / "requirements.lock.txt").write_text(requirements_text(resolved_lock, wheel))
        run_step(
            [str(python), "-I", "-B", "-m", "pip", "--isolated", "check"],
            destination,
            "pip-check",
            min(timeout, 180),
        )
        probe_log = run_step(
            [str(python), "-I", "-B", "-c", PROBE, json.dumps(required_modules())],
            destination,
            "isolated-probe",
            min(timeout, 180),
        )
        # Some optional libraries print warnings. The final line is our JSON payload.
        probe = json.loads(probe_log.read_text().splitlines()[-1])
        actual_environment = verify_probe(probe, destination, prior["environment"])
        installed = {
            canonical_name(entry["name"]): entry["version"] for entry in probe["distributions"]
        }
        if len(installed) != len(probe["distributions"]):
            raise RuntimeError("Duplicate installed distribution identities")
        if any(
            installed.get(entry["name"]) != entry["version"] for entry in resolved_lock["artifacts"]
        ):
            raise RuntimeError("Installed distribution versions differ from the resolver report")
        if (
            checksum(wheel) != wheel_sha
            or checksum(verification_report) != report["verification_report_sha256"]
        ):
            raise RuntimeError("Verified inputs changed during preparation")
        report.update(
            {
                "status": "prepared",
                "pip_check": "passed",
                "system_site_packages": False,
                "probe": probe,
                "environment": actual_environment,
                "environment_differs_from_prior": actual_environment != prior["environment"],
                "environment_changes": {
                    key: {"prior": prior["environment"].get(key), "new": value}
                    for key, value in actual_environment.items()
                    if value != prior["environment"].get(key)
                },
                "dependency_lock": str(lock_output),
                "dependency_lock_sha256": checksum(lock_output),
                "pip_report": str(pip_report_path),
                "pip_report_sha256": checksum(pip_report_path),
                "artifact_count": len(resolved_lock["artifacts"]),
                "completed_at": datetime.now(UTC).isoformat(),
            }
        )
        replay = [
            str(executable),
            str(Path(__file__).resolve()),
            "--wheel",
            str(wheel),
            "--verification-report",
            str(verification_report),
            "--destination",
            str(destination.with_name(destination.name + "-replay")),
            "--lock",
            str(lock_output),
        ]
        if EXTRAS == ["platform"]:
            replay.append("--platform-only")
        if offline_wheelhouse is not None:
            replay += ["--offline-wheelhouse", str(offline_wheelhouse)]
        report["replay_command"] = replay
        (destination / "REPRODUCE.md").write_text(
            "# Independent dependency environment\n\n"
            "Prepared dependencies do not prove camera calculations in this environment. "
            "The previous package proof establishes the wheel identity only. Re-run numerical, "
            "installed-runtime and UI validation before release selection.\n\n"
            "Replay into a new destination using the same Python/platform:\n\n```sh\n"
            + shlex.join(replay)
            + "\n```\n\n"
            "The lock pins each downloaded runtime artifact URL, version and SHA256. "
            "It requires those URLs to remain available. Source archives may build different "
            "wheels: isolated build dependencies and OS libraries are not locked. "
            "pip and the base interpreter come from venv bootstrap and are recorded, not "
            "replaced by the lock. Meep/Mitsuba external workers are not installed here.\n"
        )
        write_json(report_path, report)
        return report
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report.update(
            {"status": "failed", "error": str(error), "failed_at": datetime.now(UTC).isoformat()}
        )
        write_json(report_path, report)
        raise


def main(argv=None):
    global EXTRAS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--verification-report", type=Path, required=True)
    parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="New venv path with an existing parent; never modifies an existing venv",
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--lock", type=Path, help="Replay the separately generated artifact lock")
    parser.add_argument(
        "--offline-wheelhouse", type=Path,
        help="Install from a verified local wheelhouse without contacting an index",
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument(
        "--platform-only", action="store_true",
        help="Resolve only the R1 platform runtime; optional Factory solvers are excluded",
    )
    args = parser.parse_args(argv)
    EXTRAS = ["platform"] if args.platform_only else DEFAULT_EXTRAS.copy()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    try:
        report = prepare(
            args.wheel,
            args.verification_report,
            args.destination,
            executable=args.python,
            lock_path=args.lock,
            offline_wheelhouse=args.offline_wheelhouse,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "destination",
                    "activated",
                    "environment_differs_from_prior",
                    "calculation_validation",
                    "dependency_lock",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
