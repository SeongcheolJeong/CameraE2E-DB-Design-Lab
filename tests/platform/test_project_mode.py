"""R1 state isolation is part of the release boundary."""

import json
import sys
from types import SimpleNamespace

import pytest

from camera_platform.project_mode import configure, read, require_design_experiments


def _service(state, workspace):
    return SimpleNamespace(registry=SimpleNamespace(directory=state), workspace=workspace)


def test_r1_mode_requires_a_fresh_state_and_cannot_relabel_primary(tmp_path):
    primary = tmp_path / "outputs" / "camera-platform"
    primary.mkdir(parents=True)
    (primary / "registry.sqlite").write_bytes(b"existing-state")
    with pytest.raises(ValueError, match="new, empty"):
        configure(primary, project_id="camera-r1")
    assert read(primary) is None

    r1 = tmp_path / "outputs" / "camera-r1"
    marker = configure(r1, project_id="camera-r1")
    assert marker == read(r1)
    assert configure(r1, project_id="camera-r1") == marker
    with pytest.raises(ValueError, match="another mode or ID"):
        configure(r1, project_id="different-project")


def test_design_experiments_accept_only_explicit_r1_or_historical_isolated_state(tmp_path):
    workspace = tmp_path
    primary = tmp_path / "outputs" / "camera-platform"
    with pytest.raises(ValueError, match="explicit R1"):
        require_design_experiments(_service(primary, workspace))

    r1 = tmp_path / "outputs" / "camera-r1"
    configure(r1, project_id="camera-r1")
    assert require_design_experiments(_service(r1, workspace))["mode"] == "r1_engineering"

    legacy = tmp_path / "outputs" / "camera-platform-design-experiments-qa"
    assert require_design_experiments(_service(legacy, workspace))["schema"] == (
        "legacy_design_experiments_development_state_v1"
    )
    other_workspace = tmp_path / "elsewhere"
    with pytest.raises(ValueError, match="explicit R1"):
        require_design_experiments(_service(legacy, other_workspace))


def test_marker_rejects_symlinks_and_invalid_contract(tmp_path):
    state = tmp_path / "r1"
    state.mkdir()
    marker = state / "project-mode.json"
    marker.write_text('{"schema":"wrong","mode":"r1_engineering","project_id":"r1"}')
    with pytest.raises(ValueError, match="unsupported contract"):
        read(state)
    marker.unlink()
    marker.symlink_to(tmp_path / "external-marker")
    with pytest.raises(ValueError, match="regular file"):
        read(state)


def test_init_r1_command_creates_marker_without_bootstrapping_demo(tmp_path, monkeypatch, capsys):
    from camera_platform.__main__ import main

    state = tmp_path / "state"
    monkeypatch.setattr(
        sys,
        "argv",
        ["camera_platform", "init-r1", "--workspace", str(tmp_path),
         "--state-dir", str(state), "--r1-project-id", "pilot"],
    )
    main()
    assert json.loads(capsys.readouterr().out)["project_id"] == "pilot"
    assert read(state)["mode"] == "r1_engineering"
    assert sorted(path.name for path in state.iterdir()) == ["project-mode.json"]

    monkeypatch.setattr(
        sys,
        "argv",
        ["camera_platform", "init-r1", "--workspace", str(tmp_path),
         "--state-dir", str(tmp_path / "outputs/camera-platform"),
         "--r1-project-id", "pilot"],
    )
    with pytest.raises(SystemExit):
        main()
    assert not (tmp_path / "outputs/camera-platform/project-mode.json").exists()
