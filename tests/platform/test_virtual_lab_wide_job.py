"""Wide-angle requests must describe an actual acquisition, not a preview resize."""

import copy
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient
from camera_platform.virtual_lab_job import default_specification, specification

CATALOG = {
    "lenses": [
        {"id": "L1", "f_number": 4.12},
        {"id": "wide_parametric", "supports": {"pupil_directions": True}},
    ],
    "sensors": [{"id": "S1"}, {"id": "S2"}],
}


def test_wide_defaults_and_projection_are_explicit():
    spec = specification({"lens_id": "wide_parametric"}, CATALOG)
    assert spec["field_model"] == {"mode": "full_field", "horizontal_fov_deg": 120}
    assert spec["wide_angle"]["native_shape_row_col"] == [1080, 1920]
    assert spec["optics"]["f_number"] == 2.8
    for projection in ("rectilinear", "equidistant"):
        request = {**spec, "wide_angle": {"projection": projection}, "scene": "grid"}
        actual = specification(request, CATALOG)
        assert actual["wide_angle"]["projection"] == projection
        assert actual["wide_angle"]["max_working_bytes"] == 4 * 1024**3


@pytest.mark.parametrize(
    "change",
    [
        {"projection": "unknown"},
        {"native_shape_row_col": [360, 640]},
        {"sampling": True},
        {"sampling": 3},
        {"sampling": 2.0},
        {"tile_size": 255},
        {"tile_size": True},
        {"transmission": 1.1},
        {"exit_pupil_distance_mm": float("nan")},
        {"radial_blur_um": -1},
        {"max_working_bytes": -1},
        {"min_free_disk_bytes": -1},
        {"invented": 1},
    ],
)
def test_invalid_wide_controls_fail_without_silent_downgrade(change):
    with pytest.raises(ValueError):
        specification({"lens_id": "wide_parametric", "wide_angle": change}, CATALOG)


def test_wide_does_not_expand_existing_db_support():
    with pytest.raises(ValueError, match="Wide-angle controls"):
        specification({"lens_id": "L1", "wide_angle": {"projection": "equidistant"}}, CATALOG)
    legacy = default_specification(CATALOG)
    legacy["field_model"]["horizontal_fov_deg"] = 120
    with pytest.raises(ValueError, match="FOV"):
        specification(legacy, CATALOG)
    wide = specification({"lens_id": "wide_parametric"}, CATALOG)
    for fov in (0, 121, float("inf")):
        request = copy.deepcopy(wide)
        request["field_model"]["horizontal_fov_deg"] = fov
        with pytest.raises(ValueError, match="FOV"):
            specification(request, CATALOG)
    wide["field_model"]["mode"] = "center_local"
    with pytest.raises(ValueError, match="full_field"):
        specification(wide, CATALOG)


def test_normalization_does_not_mutate_nested_request():
    request = {"lens_id": "wide_parametric", "wide_angle": {"sampling": 4}}
    before = copy.deepcopy(request)
    specification(request, CATALOG)
    assert request == before


def test_resume_binds_partial_files_and_common_lifecycle(tmp_path, monkeypatch):
    import camera_platform

    def run(workspace, output, spec, on_progress=None):
        assert (output / "wide-checkpoints/chart/tile.bin").read_bytes() == b"saved-native-tile"
        on_progress("Resumed verified native tile")
        return {"images": {}, "tests": {}}

    fake = SimpleNamespace(catalog=lambda workspace: CATALOG, run=run)
    monkeypatch.setitem(sys.modules, "camera_platform.virtual_lab_engine", fake)
    monkeypatch.setattr(camera_platform, "virtual_lab_engine", fake, raising=False)
    app = create_app(tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = http
        spec = specification({"lens_id": "wide_parametric"}, CATALOG)
        stopped = sdk.submit_virtual_lab(spec)
        assert (
            http.post(f"/api/v1/virtual-lab/runs/{stopped['id']}/resume", json={}).status_code
            == 422
        )
        sdk.cancel_virtual_lab(stopped["id"])
        source = app.state.service.registry.directory / "virtual-lab" / stopped["id"]
        checkpoint = source / "wide-checkpoints/chart/tile.bin"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_bytes(b"saved-native-tile")
        resumed = sdk.resume_virtual_lab(stopped["id"])
        assert resumed["id"] != stopped["id"]
        claimed = app.state.service.registry.claim()
        app.state.service.registry.finish(
            resumed["id"], result=app.state.service.execute_run(claimed)
        )
        result = sdk.wait_virtual_lab(resumed["id"], timeout=1, poll_interval=0)
        assert result["status"] == "succeeded"
        assert result["result"]["summary"]["resumed_from_run_id"] == stopped["id"]
        assert checkpoint.read_bytes() == b"saved-native-tile"


def test_changed_checkpoint_is_rejected_after_resume_submission(tmp_path, monkeypatch):
    from camera_platform import virtual_lab_job as job
    from camera_platform.registry import IntegrityError

    source = tmp_path / "virtual-lab/run-abc/wide-checkpoints"
    source.mkdir(parents=True)
    path = source / "tile.bin"
    path.write_bytes(b"before")
    expected = job.sha256(path)
    path.write_bytes(b"after")
    service = SimpleNamespace(registry=SimpleNamespace(directory=tmp_path))
    output = tmp_path / "resumed"
    output.mkdir()
    request = {
        "resume_source": {"run_id": "run-abc", "files": {"tile.bin": expected}},
        "specification": {"wide_angle": {"min_free_disk_bytes": 0}},
    }
    with pytest.raises(IntegrityError, match="checkpoint changed"):
        job._copy_resume_checkpoints(service, request, output)
