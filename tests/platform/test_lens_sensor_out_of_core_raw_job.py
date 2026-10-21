"""Saved DB optical tiles and full disk-backed RAW through the Platform job."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from camera_platform import worker
from camera_platform.lens_sensor_out_of_core_raw_job import (
    JOB_KIND,
    WORKER_TIMEOUT_S,
    cleanup_work_root,
    default_spec,
    get,
    prepare,
    specification,
)
from camera_platform.registry import IntegrityError, digest
from camera_platform.sensor_db_bridge import inspect_sensor_revision
from camera_platform.service import PlatformService
from camera_platform.worker import execute as worker_execute

WORKSPACE = Path(__file__).resolve().parents[2]
LENS_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
SENSOR_CODE = "DEP-2209-801"


def _spec() -> dict:
    spec = default_spec()
    spec["lens_revision_id"] = LENS_REVISION
    spec["sensor_revision"] = {"identifier": SENSOR_CODE, "run_id": "a" * 64}
    spec["virtual_sensor_geometry"].update(
        mode_id="virtual-32x32", pixel_pitch_um=1.22
    )
    spec["accept_approximate_lens_radiometry"] = True
    spec["acknowledge_virtual_geometry_and_limits"] = True
    return spec


def test_typed_request_requires_virtual_geometry_and_declared_limits():
    spec = _spec()
    assert spec["reuse_optical_groups"] is False
    assert specification(spec)["virtual_sensor_geometry"]["kind"] == (
        "user_declared_virtual_centered_crop"
    )
    legacy = dict(spec)
    del legacy["reuse_optical_groups"]
    assert specification(legacy)["reuse_optical_groups"] is False
    assert specification(dict(spec, reuse_optical_groups=True))["reuse_optical_groups"] is True
    with pytest.raises(ValueError, match="boolean opt-in"):
        specification(dict(spec, reuse_optical_groups=1))
    with pytest.raises(ValueError, match="boolean opt-in"):
        specification(dict(spec, reuse_optical_groups="true"))
    with pytest.raises(ValueError, match="acknowledgements"):
        specification(dict(spec, acknowledge_virtual_geometry_and_limits=False))
    with pytest.raises(ValueError, match="never a local path"):
        specification(dict(spec, lens_revision_id="/private/tmp/lens.json"))
    bad = _spec()
    bad["virtual_sensor_geometry"]["optical_axis_offset_um_row_col"] = [1, 0]
    with pytest.raises(ValueError, match=r"\[0, 0\]"):
        specification(bad)
    bad = _spec()
    bad["readout_row_chunk_rows"] = 32
    with pytest.raises(ValueError, match="smaller than full sensor rows"):
        specification(bad)
    bad = _spec()
    bad["sensor_tile_shape_row_col"] = [32, 32]
    with pytest.raises(ValueError, match="complete sensor tiles"):
        from camera_platform.lens_sensor_out_of_core_raw_job import _tiles

        _tiles((32, 32), bad["sensor_tile_shape_row_col"])


def test_saved_db_opt_in_preflight_admits_bounded_reuse_without_optical_execution(
    tmp_path: Path,
):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens DB and Sensor DB are unavailable")
    selected = inspect_sensor_revision(SENSOR_CODE, root=WORKSPACE / "camerae2e_db")
    if not selected["executable"]:
        pytest.skip("Saved sensor run is incompatible with this runtime")
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = _spec()
    spec["sensor_revision"]["run_id"] = selected["revision"]["run_id"]
    legacy = prepare(service, spec)
    assert legacy["resource_preflight"]["status"] == "ready_under_declared_policy"
    assert "optical_reuse_group_plans" not in legacy["resource_preflight"]
    spec["reuse_optical_groups"] = True
    admitted = prepare(service, spec)
    assert admitted["specification"]["reuse_optical_groups"] is True
    assert admitted["resource_preflight"]["optical_reuse_group_count"] == 2
    assert admitted["resource_preflight"]["optical_reuse_group_plan_sha256"]
    assert len(admitted["resource_preflight"]["optical_reuse_group_plans"]) == 2


def test_run_owned_scratch_cleanup_rejects_symlink_and_preserves_other_runs(tmp_path: Path):
    own = "run-" + "a" * 32
    other = "run-" + "b" * 32
    parent = tmp_path / "out_of_core_work"
    (parent / own / ".opticalraw_tmp_abc").mkdir(parents=True)
    (parent / other).mkdir()
    assert cleanup_work_root(tmp_path, own) is True
    assert not (parent / own).exists()
    assert (parent / other).is_dir()
    assert cleanup_work_root(tmp_path, own) is False
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (parent / own).symlink_to(outside, target_is_directory=True)
    with pytest.raises(IntegrityError, match="not a real directory"):
        cleanup_work_root(tmp_path, own)
    assert (outside / "keep.txt").read_text() == "keep"
    assert (parent / other).is_dir()


@pytest.mark.parametrize(
    "job_kind", (JOB_KIND, "lens_sensor_registered_roi_disk_raw")
)
def test_worker_timeout_cleans_only_claimed_disk_raw_run_root(
    job_kind: str,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if job_kind == JOB_KIND:
        from camera_platform import lens_sensor_out_of_core_raw_job as job_module
    else:
        from camera_platform import lens_sensor_registered_roi_disk_raw_job as job_module

    monkeypatch.setattr(job_module, "WORKER_TIMEOUT_S", 0.3)
    service = PlatformService(tmp_path / "state", WORKSPACE)
    request = {"job_kind": job_kind, "name": "timeout scratch probe"}
    queued = service.registry.submit(
        request, digest(request),
    )
    root = service.registry.directory / "out_of_core_work" / queued["id"]
    (root / ".opticalraw_tmp_interrupted").mkdir(parents=True)
    (root / ".opticalraw_tmp_interrupted" / "mean.npy").write_bytes(b"partial")
    other = service.registry.directory / "out_of_core_work" / ("run-" + "f" * 32)
    other.mkdir()
    original = subprocess.Popen

    def sleeping_child(command, **kwargs):
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(worker.subprocess, "Popen", sleeping_child)
    worker.work(service, once=True)
    assert service.registry.get_run(queued["id"])["status"] == "failed"
    assert not root.exists()
    assert other.is_dir()


def test_saved_db_32x32_disk_raw_job_and_artifact_integrity(tmp_path: Path):
    from fastapi.testclient import TestClient

    from camera_platform.api import create_app
    from camera_platform.sdk import PlatformClient

    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned local Lens DB and Sensor DB are unavailable")
    selected = inspect_sensor_revision(SENSOR_CODE, root=WORKSPACE / "camerae2e_db")
    if not selected["executable"]:
        pytest.skip("Saved sensor run is incompatible with this runtime")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        sdk = object.__new__(PlatformClient)
        sdk.http = transport
        template = sdk.lens_sensor_disk_raw_template()
        assert template["specification"]["schema"] == default_spec()["schema"]
        assert template["resource_policy_is_hardware_limit"] is False
        assert template["default_worker_timeout_seconds"] == WORKER_TIMEOUT_S
        assert sdk.lens_sensor_disk_raw_template(query=SENSOR_CODE)["sensor_catalog"]
        spec = _spec()
        spec["sensor_revision"]["run_id"] = selected["revision"]["run_id"]
        option = sdk.lens_sensor_disk_raw_sensor_option(
            SENSOR_CODE, spec["sensor_revision"]["run_id"]
        )
        assert option["supported_disk_raw_diagnostic"] is True
        assert option["pixel_pitch_um"] == 1.22
        assert option["active_product_mode_inferred"] is False
        too_narrow = json.loads(json.dumps(spec))
        too_narrow["analytic_scene"].update(
            shape_row_col=[512, 512], horizontal_fov_deg=0.6
        )
        rejected = transport.post("/api/v1/lens-sensor-disk-raw", json=too_narrow)
        assert rejected.status_code == 422
        assert "blocked_scene_halo" in rejected.text
        assert sdk.list_lens_sensor_disk_raw() == []
        queued = sdk.submit_lens_sensor_disk_raw(spec)
        assert queued["job_kind"] == JOB_KIND
        assert queued["status"] == "queued"
        assert queued["resource_preflight"]["status"] == "ready_under_declared_policy"
        assert queued["sampling"]["input_scene_samples_per_native_pixel"] > 2
        assert queued["sampling"]["sampling_convergence"] == "not_assessed"
        assert str(WORKSPACE) not in json.dumps(queued)
        assert sdk.job_status(queued["id"], expected_kind=JOB_KIND)["detail_path"] == (
            f"lens-sensor-disk-raw/{queued['id']}"
        )
        assert app.state.service.registry.claim()["id"] == queued["id"]
        worker_execute(app.state.service, queued["id"])
        finished = sdk.wait_lens_sensor_disk_raw(queued["id"], timeout=5)
        assert finished["status"] == "succeeded", finished.get("error")
        assert sdk.lens_sensor_disk_raw(queued["id"]) == finished
        assert len(sdk.list_lens_sensor_disk_raw()) == 1
        summary = finished["result"]["summary"]
        assert summary["status"] == "completed_numerical_raw_diagnostic"
        assert summary["execution"]["sensor_tile_execution_count"] == 4
        assert summary["execution"]["full_sensor_mean_or_raw_materialized_in_ram"] is False
        assert summary["execution"]["native_seed_bit_parity"] is False
        assert summary["physical_fov_qualified"] is False
        assert summary["product_full_frame_qualified"] is False
        assert str(WORKSPACE) not in json.dumps(summary)
        tile_descriptor = summary["array_artifacts"]["tile_execution"]
        tile_bytes = transport.get(tile_descriptor["url"]).content
        assert str(WORKSPACE).encode() not in tile_bytes
        assert str(app.state.service.registry.directory).encode() not in tile_bytes
        arrays = {}
        for name in ("mean_electrons", "raw_dn", "reference_dn", "volts"):
            descriptor = summary["array_artifacts"][name]
            path = app.state.service.registry.blob_path(descriptor["sha256"])
            arrays[name] = np.load(path, mmap_mode="r", allow_pickle=False)
            assert arrays[name].shape == (32, 32)
            assert transport.get(descriptor["url"]).status_code == 200
        assert arrays["raw_dn"].dtype == np.dtype("int32")
        assert np.isfinite(arrays["mean_electrons"]).all()
        assert arrays["mean_electrons"].max() > 0
        assert arrays["raw_dn"].min() >= 0
        work_root = app.state.service.registry.directory / "out_of_core_work" / queued["id"]
        assert not work_root.exists()
        raw_path = app.state.service.registry.blob_path(
            summary["array_artifacts"]["raw_dn"]["sha256"]
        )
        with raw_path.open("ab") as handle:
            handle.write(b"tampered")
        with pytest.raises(IntegrityError, match="Artifact checksum mismatch"):
            get(app.state.service, queued["id"])
