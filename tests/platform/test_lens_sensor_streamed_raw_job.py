"""A persisted, typed, virtual Lens DB x Sensor DB stitched RAW diagnostic."""

from __future__ import annotations

import io
import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_streamed_raw_job import (
    JOB_KIND,
    default_spec,
    get,
    specification,
)
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.sensor_db_bridge import inspect_sensor_revision
from camera_platform.worker import execute as worker_execute

WORKSPACE = Path(__file__).resolve().parents[2]
LENS_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
SENSOR_CODE = "DEP-2209-801"


def _spec() -> dict:
    spec = default_spec()
    spec["lens_revision_id"] = LENS_REVISION
    spec["sensor_revision"] = {"identifier": SENSOR_CODE, "run_id": "a" * 64}
    spec["virtual_sensor_geometry"].update(mode_id="virtual-8x8", pixel_pitch_um=1.22)
    spec["analytic_scene"].update(shape_row_col=[256, 256], horizontal_fov_deg=0.6)
    spec["accept_approximate_lens_radiometry"] = True
    spec["acknowledge_virtual_geometry_and_limits"] = True
    return spec


def test_schema_requires_explicit_virtual_geometry_and_rejects_paths():
    spec = _spec()
    assert specification(spec)["virtual_sensor_geometry"]["kind"] == (
        "user_declared_virtual_centered_crop"
    )
    with pytest.raises(ValueError, match="acknowledgements"):
        specification(dict(spec, acknowledge_virtual_geometry_and_limits=False))
    with pytest.raises(ValueError, match="never a local path"):
        specification(dict(spec, lens_revision_id="/private/tmp/other-lens.json"))
    with pytest.raises(ValueError, match="never a path"):
        specification(dict(spec, sensor_revision={"identifier": "/tmp/model", "run_id": "a" * 64}))
    bad = _spec()
    bad["virtual_sensor_geometry"]["optical_axis_offset_um_row_col"] = [1.0, 0.0]
    with pytest.raises(ValueError, match=r"\[0, 0\]"):
        specification(bad)
    bad = _spec()
    bad["analytic_scene"]["shape_row_col"] = [513, 513]
    with pytest.raises(ValueError, match="analytic_scene.shape_row_col"):
        specification(bad)
    bad = _spec()
    bad["analytic_scene"]["object_distance_m"] = 1.0
    with pytest.raises(ValueError, match="infinity-conjugate"):
        specification(bad)


def test_saved_models_execute_one_native_raw_and_sdk_reads_hash_checked_artifacts(tmp_path):
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
        template = sdk.lens_sensor_streamed_raw_template()
        assert template["specification"]["schema"] == default_spec()["schema"]
        assert "lens_catalog" in template and "sensor_catalog" in template
        searched = sdk.lens_sensor_streamed_raw_template(query=SENSOR_CODE)
        assert searched["sensor_catalog"]
        spec = _spec()
        spec["sensor_revision"]["run_id"] = selected["revision"]["run_id"]
        option = sdk.lens_sensor_streamed_raw_sensor_option(
            SENSOR_CODE, spec["sensor_revision"]["run_id"]
        )
        assert option["supported_streamed_raw_diagnostic"] is True
        assert option["pixel_pitch_um"] == spec["virtual_sensor_geometry"]["pixel_pitch_um"]
        assert option["active_product_mode_inferred"] is False
        coarse = deepcopy(spec)
        coarse["analytic_scene"].update(
            shape_row_col=[128, 128], horizontal_fov_deg=1.0
        )
        rejected = transport.post("/api/v1/lens-sensor-streamed-raw", json=coarse)
        assert rejected.status_code == 422
        assert "coarser than the saved sensor pixel pitch" in rejected.text
        assert sdk.list_lens_sensor_streamed_raw() == []
        queued = sdk.submit_lens_sensor_streamed_raw(spec)
        assert queued["job_kind"] == JOB_KIND
        assert queued["status"] == "queued"
        assert queued["resource_preflight"]["status"] == "ready_under_declared_policy"
        assert queued["sampling"]["input_scene_samples_per_native_pixel"] > 1
        assert queued["sampling"]["scene_grid_convergence"] == "not_assessed"
        assert str(WORKSPACE) not in json.dumps(queued)
        assert sdk.job_status(queued["id"], expected_kind=JOB_KIND)["detail_path"] == (
            f"lens-sensor-streamed-raw/{queued['id']}"
        )
        assert app.state.service.registry.claim()["id"] == queued["id"]
        worker_execute(app.state.service, queued["id"])
        finished = sdk.wait(queued["id"], expected_kind=JOB_KIND, timeout=5)
        assert finished["status"] == "succeeded", finished.get("error")
        assert sdk.lens_sensor_streamed_raw(queued["id"]) == finished
        assert len(sdk.list_lens_sensor_streamed_raw()) == 1
        summary = finished["result"]["summary"]
        assert summary["status"] == "completed_diagnostic"
        assert summary["execution"]["native_readout_calls"] == 1
        assert summary["physical_fov_qualified"] is False
        assert summary["product_full_frame_qualified"] is False
        assert summary["hard_process_rss_limit_enforced"] is False
        assert str(WORKSPACE) not in json.dumps(summary)
        arrays = {}
        for kind in ("raw", "mean_electrons"):
            descriptor = summary["array_artifacts"][kind]
            arrays[kind] = np.load(
                io.BytesIO(transport.get(descriptor["url"]).content), allow_pickle=False
            )
            assert arrays[kind].shape == (8, 8)
        assert arrays["raw"].dtype == np.dtype("int32")
        assert arrays["mean_electrons"].dtype == np.dtype("float64")
        assert np.isfinite(arrays["mean_electrons"]).all()
        assert arrays["mean_electrons"].max() > 0
        assert arrays["raw"].min() >= 0
        raw_path = app.state.service.registry.blob_path(
            summary["array_artifacts"]["raw"]["sha256"]
        )
        raw_path.write_bytes(raw_path.read_bytes() + b"tampered")
        with pytest.raises(IntegrityError, match="Artifact checksum mismatch"):
            get(app.state.service, queued["id"])
