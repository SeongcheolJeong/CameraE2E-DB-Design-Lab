"""Package proof refuses stale payloads, unsafe ZIPs and reused output paths."""

import importlib.util
import io
import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
SPEC = importlib.util.spec_from_file_location(
    "case_package", TOOLS / "verify_camera_case_package.py"
)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


def archive(payload):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        for name, content in payload.items():
            bundle.writestr(name, content)
    return stream.getvalue()


@pytest.fixture
def wheel(tmp_path):
    source = tmp_path / "source"
    payload = {name: b"# package fixture\n" for name in package.REQUIRED}
    payload["pyisetcam/data/example.bin"] = b"\x00immutable data\xff"
    for name, content in payload.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    file = tmp_path / "pyisetcam-0.1.0-py3-none-any.whl"
    file.write_bytes(archive(payload))
    return file, source, payload


@pytest.mark.parametrize("mutation", ["changed", "missing", "stale_extra"])
def test_source_and_wheel_require_exact_text_set_before_install(
    wheel, tmp_path, monkeypatch, mutation
):
    file, source, payload = wheel
    if mutation == "changed":
        payload["camera_platform/sdk.py"] = b"stale source"
    elif mutation == "missing":
        (source / "camera_platform/added.py").write_text("missing from wheel")
    else:
        payload["camera_platform/removed.py"] = b"old module"
    file.write_bytes(archive(payload))
    monkeypatch.setattr(
        package.subprocess, "run", lambda *a, **k: pytest.fail("installed stale wheel")
    )
    output = tmp_path / "proof"
    with pytest.raises(RuntimeError, match="Source and wheel product text differ"):
        package.verify(file, output, source_root=source)
    assert not output.exists()


@pytest.mark.parametrize(
    "name",
    ["../escape.py", "/absolute.py", "camera_platform/../alias.py", "camera_platform\\bad.py"],
)
def test_unsafe_wheel_paths_are_rejected(wheel, name):
    _, _, payload = wheel
    with pytest.raises(RuntimeError, match="Unsupported wheel"):
        package._wheel_payload(archive({**payload, name: b"bad"}))


@pytest.mark.parametrize("kind", ["duplicate", "symlink"])
def test_ambiguous_wheel_entries_are_rejected(wheel, kind):
    file, _, _ = wheel
    stream = io.BytesIO(file.read_bytes())
    with zipfile.ZipFile(stream, "a") as bundle:
        if kind == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                bundle.writestr("camera_platform/sdk.py", b"duplicate")
        else:
            entry = zipfile.ZipInfo("camera_platform/alias.py")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(entry, "../outside")
    with pytest.raises(RuntimeError, match="Unsupported wheel"):
        package._wheel_payload(stream.getvalue())


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_existing_output_is_never_reused(wheel, tmp_path, monkeypatch, kind):
    file, source, _ = wheel
    original = tmp_path / "original"
    original.mkdir()
    (original / "keep").write_bytes(b"existing evidence")
    output = tmp_path / "proof"
    if kind == "directory":
        output.mkdir()
    elif kind == "file":
        output.write_bytes(b"existing report")
    else:
        output.symlink_to(original, target_is_directory=True)
    monkeypatch.setattr(
        package.subprocess, "run", lambda *a, **k: pytest.fail("ran with reused output")
    )
    with pytest.raises(FileExistsError):
        package.verify(file, output, source_root=source)
    assert (original / "keep").read_bytes() == b"existing evidence"
    if kind == "file":
        assert output.read_bytes() == b"existing report"


@pytest.mark.parametrize("mutation", [None, "installed", "source", "wheel", "child_failure"])
def test_fresh_execution_is_isolated_and_cleanup_never_publishes_false_success(
    wheel, tmp_path, monkeypatch, mutation
):
    file, source, payload = wheel
    outside_paths, commands = [], []

    def run(command, *, cwd, **kwargs):
        commands.append(command)
        outside_paths.append(cwd)
        assert not cwd.is_relative_to(package.ROOT)
        assert command[1:3] == ["-I", "-B"]
        if "pip" in command:
            assert {"--no-index", "--no-deps", "--no-compile"} <= set(command)
            target = Path(command[command.index("--target") + 1])
            assert Path(command[-1]).parent == cwd
            for name, content in payload.items():
                destination = target / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            if mutation == "installed":
                (target / "pyisetcam/data/example.bin").write_bytes(b"tampered package data")
            return subprocess.CompletedProcess(command, 0, "installed", "")
        assert Path(command[3]).read_text() == package.CHILD
        if mutation == "source":
            (source / "camera_platform/sdk.py").write_bytes(b"concurrent source edit")
        elif mutation == "wheel":
            file.write_bytes(b"replaced wheel")
        if mutation == "child_failure":
            return subprocess.CompletedProcess(command, 1, "", "calculation failed")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "status": "passed",
                    "environment": {"fixture": "same-host"},
                    "raw_and_rgb_artifact_http_hashes_verified": True,
                    "case_retry_and_history_verified": True,
                    "registry_integrity": "verified",
                }
            ),
            "",
        )

    monkeypatch.setattr(package.subprocess, "run", run)
    output = tmp_path / "proof"
    if mutation:
        with pytest.raises(RuntimeError):
            package.verify(file, output, source_root=source)
        assert not (output / "package-proof.json").exists()
        assert json.loads((output / "failure.json").read_text())["status"] == "failed"
    else:
        report = package.verify(file, output, source_root=source)
        assert json.loads((output / "package-proof.json").read_text()) == report
        assert report["temporary_state_removed"] is True
        assert report["all_checked_source_static_bytes_match"] is True
        assert report["detached_worker_verified"] is False
        assert report["physical_validation"] is False
        assert len(commands) == 2
        monkeypatch.syspath_prepend(str(TOOLS))
        from install_camera_platform_runtime import verify_wheel

        assert verify_wheel(file, report) == report["wheel_sha256"]
    assert outside_paths and all(not path.exists() for path in outside_paths)


def test_child_is_valid_python_and_cli_requires_explicit_artifacts():
    compile(package.CHILD, "installed-child", "exec")
    with pytest.raises(SystemExit):
        package.main([])
    assert Path(sys.executable).is_absolute()
