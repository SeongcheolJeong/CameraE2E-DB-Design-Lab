"""Hash-pinned saved sensor RAW is processed by ISP v2 without reacquisition."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import camera_platform.saved_sensor_isp_replay as replay_module
from camera_platform.api import create_app
from camera_platform.saved_sensor_isp_replay_job import default_spec, prepare, specification
from camera_platform.sdk import PlatformClient
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def test_isp_profile_requires_uncalibrated_stages_bypassed():
    spec = default_spec()
    spec.update(source_quality_job_id="quality-1", acknowledge_simulation_limits=True)
    assert specification(spec)["profile"]["spatial_lsc"]["mode"] == "bypass"
    spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
    spec["profile"]["calibration_id"] = "missing-calibration"
    with pytest.raises(ValueError, match="bypass LSC/denoise"):
        specification(spec)


def test_real_saved_quality_raw_replays_through_isp_v2_worker(tmp_path, monkeypatch):
    catalog = sensor_revision_catalog(root=WORKSPACE / "camerae2e_db", query="DEF-1801-802")
    selected = next((row for row in catalog if row["report_code"] == "DEF-1801-802"), None)
    if selected is None:
        pytest.skip("DEF-1801-802 saved sensor run is not installed")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        quality_spec = client.saved_sensor_quality_template(query="DEF-1801-802")[
            "specification"
        ]
        quality_spec.update(
            sensor_revisions=[{
                "identifier": "DEF-1801-802",
                "run_id": selected["run_id"],
            }],
            scenes=["uniform"],
            frames=2,
            output_size=[16, 16],
            acknowledge_simulation_limits=True,
        )
        source = client.submit_saved_sensor_quality(quality_spec)
        assert app.state.service.registry.claim()["id"] == source["id"]
        execute(app.state.service, source["id"])
        quality = client.wait(source["id"], timeout=5, expected_kind="saved_sensor_quality")
        assert quality["status"] == "succeeded", quality.get("error")

        template = client.saved_sensor_isp_replay_template()
        assert any(item["id"] == source["id"] for item in template["source_quality_jobs"])
        spec = template["specification"]
        spec.update(source_quality_job_id=source["id"], acknowledge_simulation_limits=True)
        spec["profile"]["white_balance"]["gains_rgb"] = [1.2, 1.0, 0.8]
        spec["profile"]["linear_sharpen"]["mode"] = "bounded_detail_v1"
        queued = client.submit_saved_sensor_isp_replay(spec)
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        result = client.wait(queued["id"], timeout=5, expected_kind="saved_sensor_isp_replay")
        assert result["status"] == "succeeded", result.get("error")
        summary = result["result"]["summary"]
        assert summary["source_binding"]["source_quality_job_id"] == source["id"]
        assert summary["source_binding"]["saved_sensor_revision"]["run_id"] == selected["run_id"]
        assert summary["raw_values_byte_equal"] is True
        assert summary["raw_format"]["cfa"] == "RGGB"
        assert summary["raw_format"]["black_level_dn"] == 64.0
        assert summary["geometry"]["crop_origin"] == [0, 0]
        assert summary["before_after"]["default_vs_candidate"]["available"]
        assert summary["before_after"]["default_vs_candidate"]["mean_absolute_rgb_difference"] > 0
        assert summary["candidate_rgb_consumer"]["detector_evaluation_performed"] is False
        assert summary["candidate_rgb_consumer"]["source_binding"] == summary["source_binding"]
        source_report = quality["result"]["summary"]["sensor_reports"][0]
        source_npz = transport.get(source_report["artifacts"]["npz"]["url"]).content
        replay_npz = transport.get(summary["evidence"]["npz"]["url"]).content
        with np.load(io.BytesIO(source_npz), allow_pickle=False) as original:
            with np.load(io.BytesIO(replay_npz), allow_pickle=False) as processed:
                assert original["case_000_raw_dn_frame"].tobytes(order="C") == (
                    processed["source_raw_dn_frame"].tobytes(order="C")
                )
                assert processed["candidate_rgb"].shape == (20, 20, 3)
        assert client.saved_sensor_isp_replay(queued["id"])["status"] == "succeeded"

        before = len(app.state.service.registry.list_runs())
        original_get = replay_module.sensor_simulation_db_get

        def incompatible_cfa(*args, **kwargs):
            saved = original_get(*args, **kwargs)
            saved["metadata"]["native_cfa"]["pattern"] = [[2, 1], [4, 3]]
            return saved

        monkeypatch.setattr(replay_module, "sensor_simulation_db_get", incompatible_cfa)
        with pytest.raises(ValueError, match="native CFA"):
            prepare(app.state.service, dict(spec, name="incompatible CFA"))
        assert len(app.state.service.registry.list_runs()) == before
