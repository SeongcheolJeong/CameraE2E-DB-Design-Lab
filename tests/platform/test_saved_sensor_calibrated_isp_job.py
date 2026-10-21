"""SDK import, submit, Worker and GET pin one declared calibration to one RAW."""

from __future__ import annotations

import hashlib
import io
import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.saved_sensor_calibrated_isp import MAX_CALIBRATION_BYTES
from camera_platform.sdk import PlatformClient
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def _numeric(shape):
    return {
        "schema": "camera_isp_calibration_v1",
        "sites": ["R", "Gr", "Gb", "B"],
        "lsc": {
            "schema": "native_four_site_grid_v1",
            "row_nodes": [0, shape[0] - 1],
            "column_nodes": [0, shape[1] - 1],
            "gains": [
                [[1.0, 1.1, 1.2, 1.3], [1.1, 1.2, 1.3, 1.4]],
                [[1.2, 1.3, 1.4, 1.5], [1.3, 1.4, 1.5, 1.6]],
            ],
            "valid_mask": [[[True] * 4 for _ in range(2)] for _ in range(2)],
            "interpolation": "bilinear",
            "normalization": "declared_per_site_reference",
            "reference_roi": {
                "row_start": 0,
                "row_end": shape[0],
                "column_start": 0,
                "column_end": shape[1],
            },
        },
        "noise": {
            "schema": "native_four_site_noise_v1",
            "slope_dn": [0.5] * 4,
            "intercept_dn2": [4.0] * 4,
            "signal_interval_dn": [-64, 4000],
            "reference": "black_subtracted_dn_before_lsc",
            "variance_model": "slope_times_max_signal_zero_plus_intercept",
            "spatial_independence": "assumed",
        },
    }


def _record(binding):
    return {
        "schema": "camerae2e_saved_sensor_isp_calibration_v1",
        "provenance": {
            "source_kind": "estimated",
            "source_uri": "fixture://declared-independent-calibration",
            "method_and_version": "authored_numeric_fixture:1",
            "source_dataset_id": "separate-fixture",
            "source_dataset_sha256": "a" * 64,
            "target_raw_used_for_fitting": False,
            "uncertainty": {"kind": "unknown", "reason": "Authored numbers only."},
        },
        "binding": deepcopy(binding),
        "payload": _numeric(binding["geometry"]["full_sensor_shape"]),
    }


def _write(path, payload):
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_sdk_import_submit_worker_get_pins_exact_raw_and_calibration(tmp_path):
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
            sensor_revisions=[{"identifier": "DEF-1801-802", "run_id": selected["run_id"]}],
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

        template = client.saved_sensor_calibrated_isp_template(
            source_quality_job_id=source["id"], sensor_report_index=0, case_index=0
        )
        binding = template["expected_calibration_binding"]
        assert binding["source_quality_job_id"] == source["id"]
        assert binding["saved_sensor_revision"]["run_id"] == selected["run_id"]
        assert template["calibration_document_template"]["binding"] == binding
        assert template["calibration_document_template"]["payload"] is None
        assert binding["geometry"]["full_sensor_shape"] == [20, 20]

        wrong = _record(binding)
        wrong["binding"]["acquisition"]["exposure_s"] = 0.01
        wrong_path = tmp_path / "wrong-calibration.json"
        _write(wrong_path, wrong)
        imported_wrong = client.import_saved_sensor_isp_calibration(wrong_path)
        assert imported_wrong["verification"]["source_applicability"] == (
            "checked_only_when_job_is_submitted"
        )
        wrong_spec = template["specification"]
        wrong_spec.update(
            calibration_sha256=imported_wrong["calibration_sha256"],
            acknowledge_simulation_limits=True,
        )
        wrong_spec["profile"]["calibration_id"] = (
            "sha256:" + imported_wrong["calibration_sha256"]
        )
        wrong_spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
        mismatch = transport.post("/api/v1/saved-sensor-calibrated-isp", json=wrong_spec)
        assert mismatch.status_code == 422
        assert "Calibration binding differs" in mismatch.json()["error"]

        record = _record(binding)
        calibration_path = tmp_path / "calibration.json"
        calibration_sha = _write(calibration_path, record)
        imported = client.import_saved_sensor_isp_calibration(calibration_path)
        assert imported["calibration_sha256"] == calibration_sha
        assert imported["artifact"]["sha256"] == calibration_sha
        assert imported["verification"]["file_bytes"] == "sha256_verified"
        assert imported["verification"]["provenance"].startswith("supplier_declared")
        spec = template["specification"]
        spec.update(calibration_sha256=calibration_sha, acknowledge_simulation_limits=True)
        spec["profile"]["calibration_id"] = "sha256:" + calibration_sha
        spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
        spec["profile"]["raw_denoise"].update(
            mode="calibrated_same_site_filter_v1", strength=0.8
        )
        queued = client.submit_saved_sensor_calibrated_isp(spec)
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        completed = client.wait(
            queued["id"], timeout=5, expected_kind="saved_sensor_calibrated_isp"
        )
        assert completed["status"] == "succeeded", completed.get("error")
        assert client.saved_sensor_calibrated_isp(queued["id"])["status"] == "succeeded"
        summary = completed["result"]["summary"]
        assert summary["source_binding"] == binding
        assert summary["calibration_artifact"]["sha256"] == calibration_sha
        assert summary["calibration"]["file_sha256"] == calibration_sha
        assert summary["verification"]["calibration_file_bytes"] == "sha256_verified"
        assert summary["verification"]["physical_validation"] == "not_established"
        assert summary["raw_values_byte_equal"] is True
        assert summary["before_after"]["default_vs_candidate"]["available"]
        assert summary["before_after"]["default_vs_candidate"][
            "quality_improvement_claimed"
        ] is False
        source_report = quality["result"]["summary"]["sensor_reports"][0]
        source_npz = transport.get(source_report["artifacts"]["npz"]["url"]).content
        result_npz = transport.get(summary["evidence"]["npz"]["url"]).content
        with np.load(io.BytesIO(source_npz), allow_pickle=False) as original:
            with np.load(io.BytesIO(result_npz), allow_pickle=False) as processed:
                assert original["case_000_raw_dn_frame"].tobytes(order="C") == processed[
                    "source_raw_dn_frame"
                ].tobytes(order="C")
                assert processed["candidate_rgb"].shape == (20, 20, 3)
        # A historical result remains readable using frozen source/calibration
        # artifacts when the live saved DB workspace is no longer available.
        app.state.service.workspace = tmp_path / "no-live-db"
        assert client.saved_sensor_calibrated_isp(queued["id"])["status"] == "succeeded"


def test_calibration_import_rejects_wrong_hash_and_oversize(tmp_path):
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        wrong_hash = transport.post(
            "/api/v1/saved-sensor-calibrated-isp/calibrations/import",
            files={"calibration": ("calibration.json", b"{}", "application/json")},
            data={"sha256": "0" * 64},
        )
        assert wrong_hash.status_code == 422
        assert "submitted SHA-256" in wrong_hash.json()["error"]
        invalid = b"\xff\xfe{}"
        bad_json = transport.post(
            "/api/v1/saved-sensor-calibrated-isp/calibrations/import",
            files={"calibration": ("invalid.json", invalid, "application/json")},
            data={"sha256": hashlib.sha256(invalid).hexdigest()},
        )
        assert bad_json.status_code == 422
        assert "not valid UTF-8 JSON" in bad_json.json()["error"]
        huge = b" " * (MAX_CALIBRATION_BYTES + 1)
        oversize = transport.post(
            "/api/v1/saved-sensor-calibrated-isp/calibrations/import",
            files={"calibration": ("large.json", huge, "application/json")},
            data={"sha256": hashlib.sha256(huge).hexdigest()},
        )
        assert oversize.status_code in {413, 422}
