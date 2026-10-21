"""Actual stdlib-only venv tests for the execution verifier's import/payload probe.

The tiny fixture packages perform no camera calculations. These tests establish
only the probe's isolation and installed-byte boundaries, not execution acceptance.
"""

import ast
import hashlib
import json
import os
import subprocess
import venv
import zipfile
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools/verify_camera_environment_execution.py"
PROBE = next(
    ast.literal_eval(node.value)
    for node in ast.parse(TOOL.read_text()).body
    if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == "PROBE" for target in node.targets)
)


@pytest.fixture
def installed_probe_fixture(tmp_path):
    root = tmp_path / "independent-venv"
    venv.EnvBuilder(with_pip=False, system_site_packages=False).create(root)
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    outside = tmp_path / "outside-checkout"
    outside.mkdir()
    result = subprocess.run(
        [str(python), "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        cwd=outside,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    purelib = Path(result.stdout.strip())
    environment = {"platform_version": "probe-fixture", "source_hashes": {"fixture": "a" * 64}}
    payload = {
        "camera_platform/__init__.py": b'__version__ = "probe-fixture"\n',
        "camera_platform/service.py": (
            "def environment_fingerprint():\n    return " + repr(environment) + "\n"
        ).encode(),
        "camera_platform/static/index.html": b"<p>Probe fixture only</p>\n",
        "pyisetcam/__init__.py": b'__version__ = "probe-fixture"\n',
    }
    wheel = tmp_path / "pyisetcam-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
        archive.writestr(
            "pyisetcam-0.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: pyisetcam\nVersion: 0.0.0\n",
        )
        archive.writestr(
            "pyisetcam-0.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
    # Controlled fixture extraction installs only the tiny local payload, with no
    # pip, resolver, network or pre-existing application dependency reuse.
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(purelib)
    return {
        "root": root,
        "python": python,
        "purelib": purelib,
        "outside": outside,
        "wheel": wheel,
        "payload": payload,
        "environment": environment,
    }


def run_probe(fixture):
    return subprocess.run(
        [str(fixture["python"]), "-I", "-B", "-c", PROBE, str(fixture["wheel"])],
        cwd=fixture["outside"],
        capture_output=True,
        text=True,
        timeout=20,
    )


def assert_payload_unchanged(fixture):
    assert all(
        (fixture["purelib"] / name).read_bytes() == data
        for name, data in fixture["payload"].items()
    )


def test_actual_probe_accepts_exact_installed_bytes_and_ignores_checkout_pythonpath(
    installed_probe_fixture, monkeypatch
):
    fixture = installed_probe_fixture
    for name in ("camera_platform", "pyisetcam"):
        (fixture["outside"] / (name + ".py")).write_text(
            "raise RuntimeError('outside checkout must never be imported')\n"
        )
    monkeypatch.setenv("PYTHONPATH", str(fixture["outside"]))
    result = run_probe(fixture)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["prefix"] == str(fixture["root"].resolve())
    assert report["environment"] == fixture["environment"]
    assert report["payload_sha256"] == {
        name: hashlib.sha256(data).hexdigest() for name, data in fixture["payload"].items()
    }
    assert all(
        Path(path).is_relative_to(fixture["root"].resolve())
        for path in report["module_paths"].values()
    )
    assert str(fixture["outside"]) not in report["sys_path"]
    assert report["distributions"] == [
        {"name": "pyisetcam", "version": "0.0.0", "location": str(fixture["purelib"].resolve())}
    ]
    assert not list(fixture["purelib"].rglob("__pycache__"))


def test_actual_probe_rejects_modified_installed_payload(installed_probe_fixture):
    fixture = installed_probe_fixture
    name = "pyisetcam/__init__.py"
    (fixture["purelib"] / name).write_bytes(fixture["payload"][name] + b"# changed bytes\n")
    result = run_probe(fixture)
    assert result.returncode != 0
    assert "Installed wheel payload differs: " + name in result.stderr
    assert not result.stdout.strip()


def test_actual_probe_rejects_external_pth_path_with_original_payload(
    installed_probe_fixture, tmp_path
):
    fixture = installed_probe_fixture
    external = tmp_path / "external-package-directory"
    external.mkdir()
    (fixture["purelib"] / "external.pth").write_text(str(external) + "\n")
    assert_payload_unchanged(fixture)
    result = run_probe(fixture)
    assert result.returncode != 0
    assert "External import search path: " + str(external.resolve()) in result.stderr
    assert not result.stdout.strip()


def test_actual_probe_rejects_system_site_enabled_with_original_payload(installed_probe_fixture):
    fixture = installed_probe_fixture
    config = fixture["root"] / "pyvenv.cfg"
    original = config.read_text()
    assert "include-system-site-packages = false" in original
    config.write_text(
        original.replace(
            "include-system-site-packages = false", "include-system-site-packages = true"
        )
    )
    assert_payload_unchanged(fixture)
    result = run_probe(fixture)
    assert result.returncode != 0
    assert "AssertionError" in result.stderr
    assert not result.stdout.strip()
