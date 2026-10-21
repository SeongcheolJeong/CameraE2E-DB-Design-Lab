"""Pinned sensor HDR crosses typed API, SDK, Worker and blob retrieval."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.saved_sensor_hdr_job import default_spec, specification
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def test_saved_hdr_spec_requires_acknowledged_pinned_bounded_bracket():
    spec = default_spec()
    spec["sensor_revision"] = {"identifier": "DEF-1801-802", "run_id": "run-a"}
    with pytest.raises(ValueError, match="acknowledgement"):
        specification(spec)
    spec["acknowledge_simulation_limits"] = True
    assert specification(spec)["exposure_s"] == [0.001, 0.016]
    with pytest.raises(ValueError, match="distinct and ascending"):
        specification(dict(spec, exposure_s=[0.016, 0.001]))
    with pytest.raises(ValueError, match="zero_fpn_override"):
        specification(dict(spec, fixed_pattern_mode="native_saved_fpn"))
    with pytest.raises(ValueError, match="32x32"):
        specification(dict(spec, output_size=[64, 64]))


@pytest.mark.parametrize("fpn_mode", ["zero_fpn_override", "model_known_native_fpn"])
def test_saved_hdr_real_run_api_sdk_worker_and_integrity(tmp_path, fpn_mode):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Saved sensor DB is not installed")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        token = transport.get("/api/v1/session").json()["token"]
        transport.headers["X-Platform-Token"] = token
        template = client.saved_sensor_hdr_template(query="DEF-1801-802")
        assert fpn_mode in template["fixed_pattern_modes"]
        selected = next(
            (row for row in template["sensor_catalog"]
             if row["report_code"] == "DEF-1801-802"), None
        )
        if selected is None:
            pytest.skip("Example saved physical sensor run is not installed")
        spec = template["specification"]
        spec.update(
            sensor_revision={"identifier": "DEF-1801-802", "run_id": selected["run_id"]},
            exposure_s=[0.0005, 0.008],
            fixed_pattern_mode=fpn_mode,
            acknowledge_simulation_limits=True,
        )
        queued = client.submit_saved_sensor_hdr(spec)
        status = client.job_status(queued["id"], expected_kind="saved_sensor_hdr")
        assert status["detail_path"] == "saved-sensor-hdr/" + queued["id"]
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], timeout=5, expected_kind="saved_sensor_hdr")
        assert finished["status"] == "succeeded", finished.get("error")
        summary = finished["result"]["summary"]
        assert summary["source_identity"]["run_id"] == selected["run_id"]
        assert summary["report"]["fusion"]["frames"] == 2
        assert summary["report"]["controls"]["fixed_pattern_mode"] == fpn_mode
        response = transport.get(summary["report"]["artifacts"]["npz"]["url"])
        assert response.status_code == 200
        with np.load(io.BytesIO(response.content), allow_pickle=False) as arrays:
            assert arrays["original_raw_dn"].shape == (2, 36, 36)
            assert arrays["hdr_merged_rate"].shape == (36, 36)
            if fpn_mode == "model_known_native_fpn":
                assert arrays["model_fpn_response_gain"].shape == (36, 36)
                calibration = summary["report"]["controls"]["model_fpn_calibration"]
                assert calibration["independent_calibration"] is False
        assert client.saved_sensor_hdr(queued["id"])["result"]["summary"] == summary
        service: PlatformService = app.state.service
        evidence = summary["report"]["artifacts"]["npz"]
        service.registry.blob_path(evidence["sha256"]).write_bytes(b"corrupt")
        with pytest.raises(httpx.HTTPStatusError) as error:
            client.saved_sensor_hdr(queued["id"])
        assert error.value.response.status_code == 409
