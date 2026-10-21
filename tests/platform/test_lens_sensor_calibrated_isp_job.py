"""A separately declared Lens-bound calibration survives Worker/API/SDK replay."""

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
from camera_platform.lens_sensor_calibrated_isp_job import default_spec, specification
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def _client(transport):
    client = object.__new__(PlatformClient)
    client.http = transport
    transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
    return client


def _capture_source(app, client, *, scene_kind="uniform", seed=7):
    template = client.lens_sensor_raw_template()
    lens = next(
        (row for row in template["lens_catalog"]["revisions"]
         if row["preview_status"] == "ready"), None,
    )
    sensor = next(
        (row for row in template["sensor_catalog"]
         if row["report_code"] == "DEF-2512-801"), None,
    )
    if lens is None or sensor is None:
        pytest.skip("Required pinned Lens and 2x2 Bayer Sensor DB revisions are unavailable")
    spec = template["specification"]
    spec.update(
        lens_revision_id=lens["revision_id"],
        sensor_revision={"identifier": sensor["report_code"], "run_id": sensor["run_id"]},
        scene_kind=scene_kind, sensor_size=[8, 8], noise_flag=2, seed=seed,
        accept_approximate_lens_radiometry=True,
    )
    queued = client.submit_lens_sensor_raw(spec)
    assert app.state.service.registry.claim()["id"] == queued["id"]
    execute(app.state.service, queued["id"])
    source = client.wait(queued["id"], timeout=5, expected_kind="lens_sensor_raw")
    assert source["status"] == "succeeded", source.get("error")
    return source


def _calibration(binding, *, schema="camerae2e_lens_sensor_isp_calibration_v1"):
    rows, columns = binding["geometry"]["full_sensor_shape"]
    return {
        "schema": schema,
        "provenance": {
            "source_kind": "simulated",
            "source_uri": "fixture://independent-lens-flat-dark-series",
            "method_and_version": "authored_numeric_fixture:1",
            "source_dataset_id": "independent-lens-flat-dark-series",
            "source_dataset_sha256": "a" * 64,
            "target_raw_used_for_fitting": False,
            "uncertainty": {
                "kind": "unknown", "reason": "Fixture independence is declared only.",
            },
        },
        "binding": binding,
        "payload": {
            "schema": "camera_isp_calibration_v1",
            "sites": ["R", "Gr", "Gb", "B"],
            "lsc": {
                "schema": "native_four_site_grid_v1",
                "row_nodes": [0, rows - 1], "column_nodes": [0, columns - 1],
                "gains": [
                    [[1.0, 1.1, 1.1, 1.2], [1.1, 1.2, 1.2, 1.3]],
                    [[1.2, 1.3, 1.3, 1.4], [1.3, 1.4, 1.4, 1.5]],
                ],
                "valid_mask": [[[True] * 4 for _ in range(2)] for _ in range(2)],
                "interpolation": "bilinear",
                "normalization": "declared_per_site_reference",
                "reference_roi": {
                    "row_start": 0, "row_end": rows,
                    "column_start": 0, "column_end": columns,
                },
            },
            "noise": {
                "schema": "native_four_site_noise_v1",
                "slope_dn": [0.5] * 4, "intercept_dn2": [4.0] * 4,
                "signal_interval_dn": [-64, 4000],
                "reference": "black_subtracted_dn_before_lsc",
                "variance_model": "slope_times_max_signal_zero_plus_intercept",
                "spatial_independence": "assumed",
            },
        },
    }


def _write_calibration(path, document):
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_requires_explicit_source_calibration_and_acknowledgement():
    with pytest.raises(ValueError, match="acknowledgement"):
        specification(default_spec())


def test_real_lens_raw_calibration_worker_api_sdk_and_exact_artifacts(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        source = _capture_source(app, client)
        source_raw = np.load(io.BytesIO(transport.get(
            source["result"]["summary"]["preview"]["raw_artifact"]["url"]
        ).content), allow_pickle=False)

        template = client.lens_sensor_calibrated_isp_template(
            source_lens_sensor_job_id=source["id"]
        )
        assert any(item["id"] == source["id"] for item in template["source_lens_sensor_jobs"])
        binding = template["expected_calibration_binding"]
        assert binding["source_binding"]["source_lens_sensor_job_id"] == source["id"]
        assert binding["native_cfa"]["pattern"] == [[1, 2], [2, 3]]
        assert binding["lens_model"]["relative_illumination"] == "unknown_unity_assumption"
        calibration_path = tmp_path / "independent-lens-calibration.json"
        calibration = _calibration(binding)
        sha = _write_calibration(calibration_path, calibration)
        imported = client.import_lens_sensor_isp_calibration(calibration_path)
        assert imported["calibration_sha256"] == sha
        assert imported["verification"]["provenance"] == (
            "supplier_declared_not_independently_verified"
        )

        spec = template["specification"]
        spec.update(
            source_lens_sensor_job_id=source["id"], calibration_sha256=sha,
            acknowledge_simulation_limits=True,
        )
        spec["profile"]["calibration_id"] = "sha256:" + sha
        spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
        spec["profile"]["raw_denoise"].update(
            mode="calibrated_same_site_filter_v1", strength=0.8
        )

        bad = deepcopy(calibration)
        bad["binding"]["lens_model"]["f_number"] = 8.0
        wrong_path = tmp_path / "wrong-lens-f-number.json"
        wrong_sha = _write_calibration(wrong_path, bad)
        client.import_lens_sensor_isp_calibration(wrong_path)
        wrong_spec = deepcopy(spec)
        wrong_spec["calibration_sha256"] = wrong_sha
        wrong_spec["profile"]["calibration_id"] = "sha256:" + wrong_sha
        before = len(app.state.service.registry.list_runs())
        refused = transport.post("/api/v1/lens-sensor-calibrated-isp", json=wrong_spec)
        assert refused.status_code == 422
        assert "Calibration binding differs" in refused.text
        assert len(app.state.service.registry.list_runs()) == before

        queued = client.submit_lens_sensor_calibrated_isp(spec)
        assert client.job_status(
            queued["id"], expected_kind="lens_sensor_calibrated_isp"
        )["detail_path"] == f"lens-sensor-calibrated-isp/{queued['id']}"
        assert transport.get(
            f"/api/v1/lens-sensor-calibrated-isp/{queued['id']}/export"
        ).status_code == 409
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(
            queued["id"], timeout=5, expected_kind="lens_sensor_calibrated_isp"
        )
        assert finished["status"] == "succeeded", finished.get("error")
        summary = finished["result"]["summary"]
        assert summary["source_binding"] == binding
        assert summary["raw_values_byte_equal"] is True
        assert summary["calibration"]["file_sha256"] == sha
        assert summary["candidate_isp"]["stage_outputs"]["spatial_lsc"]["applied"]
        assert summary["candidate_isp"]["stage_outputs"]["raw_denoise"]["applied"]
        assert summary["before_after"]["default_vs_candidate"][
            "quality_improvement_claimed"
        ] is False
        with np.load(io.BytesIO(transport.get(
            summary["evidence"]["npz"]["url"]
        ).content), allow_pickle=False) as evidence:
            assert evidence["source_raw_dn_frame"].dtype == np.int32
            assert evidence["source_raw_dn_frame"].tobytes(order="C") == (
                source_raw.tobytes(order="C")
            )
            assert evidence["candidate_lsc_gain_map"].max() > 1
        assert client.lens_sensor_calibrated_isp(queued["id"])["result"]["summary"] == (
            summary
        )
        assert len(client.list_lens_sensor_calibrated_isp()) == 1
        assert transport.get(
            f"/api/v1/lens-sensor-calibrated-isp/{queued['id']}/export"
        ).status_code == 200


def test_one_reusable_calibration_serves_two_distinct_lens_raw_jobs(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        first = _capture_source(app, client, scene_kind="uniform", seed=7)
        second = _capture_source(app, client, scene_kind="slanted_edge", seed=17)
        first_template = client.lens_sensor_calibrated_isp_template(
            source_lens_sensor_job_id=first["id"]
        )
        second_template = client.lens_sensor_calibrated_isp_template(
            source_lens_sensor_job_id=second["id"]
        )
        binding = first_template["expected_reusable_calibration_binding"]
        assert binding == second_template["expected_reusable_calibration_binding"]
        assert (
            first_template["expected_calibration_binding"]
            != second_template["expected_calibration_binding"]
        )
        calibration = _calibration(
            binding, schema="camerae2e_lens_sensor_isp_calibration_v2"
        )
        calibration_path = tmp_path / "reusable-lens-calibration.json"
        sha = _write_calibration(calibration_path, calibration)
        assert client.import_lens_sensor_isp_calibration(calibration_path)["schema"] == (
            "camerae2e_lens_sensor_isp_calibration_v2"
        )
        originals = {}
        for source, template in ((first, first_template), (second, second_template)):
            original = np.load(io.BytesIO(transport.get(
                source["result"]["summary"]["preview"]["raw_artifact"]["url"]
            ).content), allow_pickle=False)
            originals[source["id"]] = original
            spec = template["specification"]
            spec.update(
                source_lens_sensor_job_id=source["id"], calibration_sha256=sha,
                acknowledge_simulation_limits=True,
            )
            spec["profile"]["calibration_id"] = "sha256:" + sha
            spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
            spec["profile"]["raw_denoise"].update(
                mode="calibrated_same_site_filter_v1", strength=0.8,
            )
            queued = client.submit_lens_sensor_calibrated_isp(spec)
            assert app.state.service.registry.claim()["id"] == queued["id"]
            execute(app.state.service, queued["id"])
            finished = client.wait(
                queued["id"], timeout=5, expected_kind="lens_sensor_calibrated_isp"
            )
            assert finished["status"] == "succeeded", finished.get("error")
            summary = client.lens_sensor_calibrated_isp(queued["id"])["result"]["summary"]
            assert summary["calibration_binding"] == binding
            assert summary["calibration"]["schema"] == (
                "camerae2e_lens_sensor_isp_calibration_v2"
            )
            assert summary["raw_values_byte_equal"] is True
            with np.load(io.BytesIO(transport.get(
                summary["evidence"]["npz"]["url"]
            ).content), allow_pickle=False) as evidence:
                assert evidence["source_raw_dn_frame"].tobytes() == (
                    originals[source["id"]].tobytes()
                )
