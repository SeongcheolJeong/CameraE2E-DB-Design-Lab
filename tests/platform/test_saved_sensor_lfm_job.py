"""Pinned sensor PWM uses temporal RAW and a constant-light control in Worker."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def test_saved_lfm_real_temporal_capture_via_api_sdk_worker(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Saved sensor DB is not installed")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        token = transport.get("/api/v1/session").json()["token"]
        transport.headers["X-Platform-Token"] = token
        template = client.saved_sensor_lfm_template(query="DEF-1801-802")
        selected = next(
            (row for row in template["sensor_catalog"]
             if row["report_code"] == "DEF-1801-802"), None
        )
        if selected is None:
            pytest.skip("Example saved physical sensor run is not installed")
        spec = template["specification"]
        spec.update(
            sensor_revision={"identifier": "DEF-1801-802", "run_id": selected["run_id"]},
            acknowledge_simulation_limits=True,
        )
        queued = client.submit_saved_sensor_lfm(spec)
        status = client.job_status(queued["id"], expected_kind="saved_sensor_lfm")
        assert status["detail_path"] == "saved-sensor-lfm/" + queued["id"]
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], timeout=5, expected_kind="saved_sensor_lfm")
        assert finished["status"] == "succeeded", finished.get("error")
        summary = finished["result"]["summary"]
        report = summary["report"]
        assert report["schema"] == "camerae2e_saved_sensor_pwm_lfm_v1"
        assert report["revision"]["run_id"] == selected["run_id"]
        assert report["source_boundary"][
            "saved_fpn_parameters_applied_to_generated_virtual_window_map"
        ] is True
        assert report["source_boundary"]["saved_fpn_parameter_provenance"] == (
            "saved_simulation_profile_assumptions"
        )
        assert report["source_boundary"]["sensor_readout_profile"] == (
            "generic_assumption"
        )
        assert report["source_boundary"]["sensor_readout_classification"] == (
            "unmeasured_assumption"
        )
        assert report["source_boundary"]["measured_fpn_map_reused"] is False
        assert report["source_boundary"]["fpn_physical_validation"] == "not_established"
        assert report["controls"]["saved_fixed_pattern"]["prnu_fraction"] == 0.005
        assert "virtual sensor window" in report["controls"]["crop_provenance"]
        assert report["verification"]["endpoint_linearity_check"] == "passed"
        assert report["verification"]["physical_validation"] == "not_established"
        assert report["controls"]["timing_provenance"].startswith("authored")
        assert "not independent" in report["controls"]["noise_seed_policy"]
        short, long = report["cases"]
        assert short["phase_modulation_fraction_black_relative"] > 0.1
        assert long["phase_modulation_fraction_black_relative"] < short[
            "phase_modulation_fraction_black_relative"
        ]
        assert all(case["endpoint_linearity_relative_l2_error"] <= 1e-6
                   for case in report["cases"])
        assert all(len(case["phases"]) == 4 for case in report["cases"])
        assert all(case["phases"][0]["row_timing"]["row_exposure_start_s"]
                   for case in report["cases"])
        response = transport.get(report["artifacts"]["npz"]["url"])
        assert response.status_code == 200
        with np.load(io.BytesIO(response.content), allow_pickle=False) as arrays:
            assert arrays["original_pwm_raw_dn"].shape == (2, 4, 8, 8)
            assert arrays["original_constant_raw_dn"].shape == (2, 8, 8)
            assert arrays["integrated_mean_electrons"].shape == (2, 4, 8, 8)
        assert client.saved_sensor_lfm(queued["id"])["result"]["summary"] == summary
