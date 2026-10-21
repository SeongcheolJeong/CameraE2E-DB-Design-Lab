"""Real registry/API/Worker calibration loop, input independence and artifact binding."""

import io
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import calibration_lab as lab
from camera_platform.api import create_app
from camera_platform.calibration_lab_contracts import template
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = http
        yield app.state.service, http, sdk


def campaign(sdk):
    return sdk.create_calibration_campaign(template()["campaign_template"])


def measurement(campaign, *, role="training", session="session-a", mean=64.1):
    value = deepcopy(template()["measurement_templates"]["dark"])
    value.update(
        campaign_id=campaign["id"],
        physical_instance=campaign["camera_id"],
        role=role,
        session_id=session,
        acquisition_id=session + "-capture",
        observations={
            "exposure_s": [0.001, 0.01, 0.1],
            "mean_dn": [mean, mean + 0.01, mean + 0.1],
            "variance_dn2": [0.7, 0.71, 0.73],
        },
    )
    return value


def run_one(service, identity):
    claimed = service.registry.claim()
    assert claimed["id"] == identity
    execute(service, identity)
    result = lab.get(service, identity)
    assert result["status"] == "succeeded", result.get("error")
    return result


def test_campaign_validates_profile_and_frozen_conditions(api):
    _, http, sdk = api
    payload = template()["campaign_template"]
    payload["pixel_pitch_um"] = 99
    assert http.post(lab.ROUTE + "/campaigns", json=payload).status_code == 422
    created = campaign(sdk)
    item = measurement(created)
    item["conditions"]["gain"] = 2
    assert http.post(lab.ROUTE + "/measurements", json=item).status_code == 422
    assert sdk.calibration_campaign(created["id"])["measurements"] == []


def test_original_csv_is_preserved_and_bound_to_measurement(api):
    service, http, sdk = api
    created = campaign(sdk)
    meta = measurement(created)
    meta["observations"] = {}
    original = (
        b"exposure_s[s],mean_dn[DN],variance_dn2[DN^2]\r\n"
        b"0.001,64.1,0.7\r\n0.01,64.11,0.71\r\n0.1,64.2,0.73\r\n"
    )
    response = http.post(
        lab.ROUTE + "/measurements/import",
        data={"metadata": json.dumps(meta)},
        files={"file": ("dark.csv", original, "text/csv")},
    )
    assert response.status_code == 201, response.text
    imported = response.json()
    assert imported["import_status"] == "ready"
    blob = next(a for a in imported["artifacts"] if a["name"] == "dark.csv")
    assert http.get(blob["url"]).content == original
    service.registry.blob_path(blob["sha256"]).write_bytes(original.replace(b"64.1", b"65.1"))
    with pytest.raises(IntegrityError):
        lab._measurement(service, imported["id"])


def test_lineage_prevents_role_changes_and_observation_aliases(api):
    _, http, sdk = api
    created = campaign(sdk)
    item = measurement(created)
    sdk.import_calibration_measurement(item)
    # A new file name or acquisition name cannot make the same values independent.
    renamed = {
        **item,
        "role": "final_validation",
        "session_id": "renamed",
        "acquisition_id": "alias",
    }
    assert http.post(lab.ROUTE + "/measurements", json=renamed).status_code == 409
    # Changing numbers in another crop within the same acquisition/session cannot either.
    changed = measurement(created, role="final_validation", mean=65)
    assert http.post(lab.ROUTE + "/measurements", json=changed).status_code == 409
    parent = measurement(created, role="final_validation", session="new-session", mean=66)
    parent["parent_acquisition_ids"] = [item["acquisition_id"]]
    assert http.post(lab.ROUTE + "/measurements", json=parent).status_code == 409


def test_partial_input_retained_without_successful_fit(api):
    _, http, sdk = api
    created = campaign(sdk)
    meta = measurement(created)
    del meta["observations"]["variance_dn2"]
    imported = sdk.import_calibration_measurement(meta)
    assert imported["import_status"] == "partial"
    response = http.post(
        lab.ROUTE + "/studies",
        json={"campaign_id": created["id"], "measurement_ids": [imported["id"]]},
    )
    assert response.status_code == 422


def test_cancelled_job_cannot_apply_or_validate(api):
    service, http, sdk = api
    created = campaign(sdk)
    imported = sdk.import_calibration_measurement(measurement(created))
    job = sdk.submit_calibration_study(
        {"campaign_id": created["id"], "measurement_ids": [imported["id"]]}
    )
    assert http.post(lab.ROUTE + "/studies/" + job["id"] + "/cancel", json={}).status_code == 200
    assert http.post(lab.ROUTE + "/studies/" + job["id"] + "/apply", json={}).status_code == 422
    assert service.registry.claim() is None


def test_source_change_requires_server_restart_before_submission(api, monkeypatch):
    service, http, sdk = api
    created = campaign(sdk)
    imported = sdk.import_calibration_measurement(measurement(created))
    monkeypatch.setattr(service, "loaded_environment", {"stale": True})
    response = http.post(
        lab.ROUTE + "/studies",
        json={"campaign_id": created["id"], "measurement_ids": [imported["id"]]},
    )
    assert response.status_code == 409
    assert "restart" in response.text
    assert service.registry.claim() is None


def test_actual_worker_all_charts_apply_rollback_export_and_final_validation(api):
    service, http, sdk = api
    demo = sdk.calibration_demo()
    result = run_one(service, demo["study"]["id"])
    assert sdk.wait(result["id"], expected_kind="calibration_lab")["id"] == result["id"]
    summary = result["result"]["summary"]
    assert {row["chart"] for row in summary["comparisons"]} == {
        "dark",
        "ptc",
        "shading",
        "mtf",
        "geometry",
        "color",
        "tone",
        "texture",
    }
    assert summary["fresh_simulation"] is True
    assert summary["evidence_kind"] == "implementation_verification"
    assert summary["physical_validation"] == "not_established"
    assert summary["parameters"]
    assert any(name.endswith("raw.npy") for name in summary["files"])
    for relative in summary["files"]:
        response = http.get(summary["files_base"] + relative)
        assert response.status_code == 200, (relative, response.text[:200])
    assert http.get(summary["files_base"] + "unknown.npy").status_code == 404
    download = http.get(lab.ROUTE + "/studies/" + result["id"] + "/bundle")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert "study.json" in archive.namelist()
        assert "results/candidate-profile.json" in archive.namelist()
        assert any(name.startswith("originals/") for name in archive.namelist())
    cid = demo["campaign"]["id"]
    selected = sdk.apply_calibration_study(result["id"], cid)
    assert sdk.calibration_campaign(cid)["active_profile"]["id"] == selected["profile"]["id"]
    sdk.activate_calibration_profile(cid, None)
    assert sdk.calibration_campaign(cid)["active_profile"] is None
    final_ids = [m["id"] for m in demo["measurements"] if m["role"] == "final_validation"]
    validation = sdk.validate_calibration_study(result["id"], final_ids)
    checked = run_one(service, validation["id"])
    assert checked["result"]["summary"]["candidate_sha256"] == summary["candidate_sha256"]
    assert checked["result"]["summary"]["validation"]["independent"] is True
    assert checked["result"]["summary"]["physical_validation"] == "not_established"
    assert sdk.validate_calibration_study(result["id"], final_ids)["id"] == checked["id"]
    # A new candidate cannot reuse an exposed final validation session.
    second = sdk.submit_calibration_study(result["specification"])
    assert second["id"] != result["id"]
    run_one(service, second["id"])
    response = http.post(
        lab.ROUTE + "/studies/" + second["id"] + "/validate", json={"measurement_ids": final_ids}
    )
    assert response.status_code == 409


def test_uncertainty_label_does_not_make_physical_validation_pass():
    measurements = [
        {
            "id": "m",
            "source_kind": "measured",
            "uncertainty": {"kind": "standard_uncertainty", "value": 0.001},
        }
    ]
    summary = {
        "comparisons": [
            {
                "measurement_id": "m",
                "chart": "mtf",
                "metric_id": "raw.sfr",
                "residuals": {"candidate_rmse": 0},
            }
        ]
    }
    verdict = lab._verdict(summary, measurements, {"raw.sfr": 0.01}, independent=True)
    assert verdict["numerical_tolerance_pass"] is True
    assert verdict["physical_validation"] == "not_established"
    assert verdict["prediction_uncertainty_recorded"] is False


def test_unknown_validation_threshold_is_not_pass():
    measurements = [{"id": "m", "source_kind": "synthetic", "uncertainty": {"kind": "unknown"}}]
    summary = {
        "comparisons": [
            {"measurement_id": "m", "chart": "dark", "residuals": {"candidate_rmse": 0}}
        ]
    }
    verdict = lab._verdict(summary, measurements, {}, independent=True)
    assert verdict["status"] == "inconclusive"
    assert verdict["numerical_tolerance_pass"] is False


def test_multiview_coordinate_raster_must_match_campaign(api):
    _, http, sdk = api
    created = campaign(sdk)
    meta = measurement(created)
    meta["chart"] = "geometry"
    meta["observations"] = {
        "mode": "multiview_planar",
        "native_shape": [100, 100],
        "object_xy": [[0, 0], [1, 0], [0, 1], [1, 1]],
        "image_xy": [[20, 20], [60, 20], [20, 60], [60, 60]],
        "view_ids": ["v1"] * 4,
    }
    response = http.post(lab.ROUTE + "/measurements", json=meta)
    assert response.status_code == 422
    assert "raster differs" in response.text


def test_original_raw_import_reaches_fresh_shared_observer_worker(api):
    service, http, sdk = api
    payload = template()["campaign_template"]
    payload["native_shape"] = [32, 32]
    payload["baseline_profile"]["native_shape"] = [32, 32]
    payload["baseline_profile"]["render_shape"] = [32, 32]
    created = sdk.create_calibration_campaign(payload)
    meta = measurement(created)
    times = [0.001, 0.1, 0.5, 1.0]
    meta.update(
        observations={},
        raw_format={
            "cfa": "RGGB",
            "bit_depth": 12,
            "black_level_dn": 72,
            "response_type": "linear",
        },
        geometry={"full_sensor_shape": [32, 32], "crop_origin": [0, 0]},
        analysis={"exposure_s": times},
    )
    rng = np.random.default_rng(1803)
    frames = np.stack(
        [
            np.rint(72 + 0.5 * (rng.poisson(12 * t, (16, 32, 32)) + rng.normal(0, 3, (16, 32, 32))))
            for t in times
        ]
    ).astype(np.uint16)
    original = io.BytesIO()
    np.save(original, frames, allow_pickle=False)
    response = http.post(
        lab.ROUTE + "/measurements/import",
        data={"metadata": json.dumps(meta)},
        files={"file": ("dark.npy", original.getvalue(), "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    imported = response.json()
    assert imported["quality"]["method"] == "native_bayer_dark_v1"
    job = sdk.submit_calibration_study(
        {"campaign_id": created["id"], "measurement_ids": [imported["id"]]}
    )
    result = run_one(service, job["id"])
    summary = result["result"]["summary"]
    assert all(
        row["operator_parity"] == "same_native_raw_observer" for row in summary["comparisons"]
    )
    assert summary["candidate_profile"]["electronics"]["black_level_dn"] == pytest.approx(
        72, abs=0.2
    )
    assert len(summary["images"]) == 2


def test_standard_completion_report_is_bound_downloadable_and_immutable(api):
    import hashlib

    from camera_platform.calibration_completion import COLLECTION

    service, http, sdk = api
    fixture = sdk.calibration_demo()
    study = run_one(service, fixture["study"]["id"])
    validation = sdk.validate_calibration_study(
        study["id"], [m["id"] for m in fixture["measurements"] if m["role"] == "final_validation"]
    )
    run_one(service, validation["id"])
    selected = sdk.apply_calibration_study(study["id"], fixture["campaign"]["id"])
    report = sdk.create_calibration_completion_report(
        study["id"], validation["id"], selected["profile"]["id"]
    )
    assert report["physical_validation"] == "not_established"
    assert report["candidate_sha256"] == study["result"]["summary"]["candidate_sha256"]
    assert sdk.create_calibration_completion_report(
        study["id"], validation["id"], selected["profile"]["id"]
    ) == report
    assert len(service.registry.list(COLLECTION)) == 1
    base = lab.ROUTE + "/completion-reports/" + report["id"]
    for name, checksum in report["files"].items():
        data = http.get(base + "/files/" + name)
        assert data.status_code == 200, data.text[:200]
        assert hashlib.sha256(data.content).hexdigest() == checksum
    assert http.get(base + "/files/unknown.json").status_code == 404
    html = http.get(base + "/files/report.html").text
    assert "<svg" in html
    assert "data:image/png;base64," in html
    assert "127.0.0.1" not in html
    package = http.get(base + "/bundle")
    assert package.status_code == 200
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        assert json.loads(archive.read("final-profile.json")) == selected["profile"]["profile"]
        assert "fit/study.json" in archive.namelist()
        assert "validation/study.json" in archive.namelist()
        assert any(n.startswith("originals/") for n in archive.namelist())
    sdk.activate_calibration_profile(fixture["campaign"]["id"], None)
    assert http.get(base).json() == report
    assert sdk.calibration_campaign(fixture["campaign"]["id"])["completion_reports"] == [report]
    blob = next(a for a in report["artifacts"] if a["name"] == "parameters.csv")
    service.registry.blob_path(blob["sha256"]).write_bytes(b"tampered")
    assert http.get(base + "/files/parameters.csv").status_code == 409
