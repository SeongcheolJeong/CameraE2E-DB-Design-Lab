"""End-to-end spectral-QE / native RAW / frozen CCM separation and evidence."""

import base64
import io
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.calibration_color import ROUTE
from camera_platform.calibration_color_demo import template
from camera_platform.calibration_color_raw import observe_raw
from pyisetcam.assets import AssetStore


@pytest.fixture
def fixture(monkeypatch):
    class Store:
        _sha = "authored-test-spectra"

        def load_reflectances(self, name, wave_nm):
            rng = np.random.default_rng(42)
            bands = np.column_stack(
                [np.exp(-0.5 * ((wave_nm - center) / 45) ** 2) for center in (450, 540, 615)]
            )
            values = bands @ rng.uniform(0.02, 0.35, (3, 24)) + 0.02
            return wave_nm, values

        def load_illuminant(self, name, wave_nm):
            return wave_nm, np.ones_like(wave_nm)

    monkeypatch.setattr(AssetStore, "default", classmethod(lambda cls: Store()))
    return template(seed=7712)


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/bootstrap").json()
        client.headers["X-Platform-Token"] = bootstrap["token"]
        yield app, client


def test_spectral_raw_ccm_complete_and_immutable(api, fixture):
    _, client = api
    response = client.post(ROUTE, json=fixture)
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["status"] == "pass"
    assert record["physical_validation"] == "not_established"
    assert record["qe"]["profile"]["response_kind"] == "absolute_qe"
    assert record["raw_validation"]["baseline"]["status"] == "fail"
    assert record["raw_validation"]["candidate"]["status"] == "pass"
    assert record["ccm"]["validation"]["status"] == "pass"
    assert record["ccm"]["validation"]["refit_performed"] is False
    assert record["raw_unchanged_by_ccm"] is True
    assert "qe" not in record["ccm"]["profile"]
    assert client.post(ROUTE, json=fixture).json()["id"] == record["id"]
    assert client.get(ROUTE + "/" + record["id"]).json() == record
    report = client.get(record["report_url"])
    assert report.status_code == 200 and "CCM" in report.text and "QE" in report.text
    assert record["report_generated_at"] in report.text
    with zipfile.ZipFile(io.BytesIO(client.get(record["bundle_url"]).content)) as package:
        assert package.testzip() is None
        assert {
            "raw-training.npy",
            "raw-validation.npy",
            "qe-profile.json",
            "ccm-profile.json",
            "report.html",
            "inputs.json",
        } <= set(package.namelist())
        assert json.loads(package.read("inputs.json")) == fixture


def test_browser_text_transport_preserves_pinned_profile_and_input_hash(api, fixture):
    from camera_platform.registry import digest

    _, client = api
    example = client.get("/api/v1/calibration-lab/color-template")
    assert example.status_code == 200
    text = example.text
    fixture = json.loads(text)
    response = client.post(ROUTE, json={"input_json": text})
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["status"] == "pass"
    assert record["input_sha256"] == digest(fixture)
    assert (
        json.loads(client.get(ROUTE + "/" + record["id"] + "/files/inputs.json").content) == fixture
    )
    bad = client.post(ROUTE, json={"input_json": '{"name":"a","name":"b"}'})
    assert bad.status_code == 422


def test_raw_is_not_changed_by_ccm_and_bad_raw_not_hidden(api, fixture):
    _, client = api
    changed = deepcopy(fixture)
    # Incorrect photon dose produces an explicit RAW fail even though CCM can still fit.
    changed["raw_validation"]["photon_spectral_dose"] = (
        np.asarray(changed["raw_validation"]["photon_spectral_dose"]) * 1.3
    ).tolist()
    response = client.post(ROUTE, json=changed)
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["raw_validation"]["candidate"]["status"] == "fail"
    assert record["ccm"]["validation"]["status"] == "pass"
    assert record["status"] == "fail"
    assert record["raw_unchanged_by_ccm"] is True


def test_relative_scan_cannot_claim_absolute_raw_success(api, fixture):
    _, client = api
    scan = deepcopy(fixture["qe_scan"])
    scan.pop("incident_photons_per_pixel")
    scan["photon_flux_per_second"] = [300000.0] * len(scan["wavelength_nm"])
    scan["radiometry"] = {
        "kind": "relative_source_corrected",
        "calibration_id": "relative-test",
        "unit": "relative_photons/s",
        "source_spectrum_corrected": True,
    }
    payload = {"schema": fixture["schema"], "name": "relative only", "qe_scan": scan}
    response = client.post(ROUTE, json=payload)
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["status"] == "inconclusive"
    assert record["qe"]["profile"]["response_kind"] == "relative_spectral_response"
    assert record["raw_validation"]["candidate"]["status"] == "not_evaluated"


def test_reused_raw_frames_cannot_be_declared_independent(api, fixture):
    _, client = api
    fixture["raw_originals"]["validation"] = fixture["raw_originals"]["training"]
    result = client.post(ROUTE, json=fixture)
    assert result.status_code == 422
    assert "share original RAW frames" in result.text


def test_bayer_observer_has_no_ccm_and_respects_crop_phase():
    mosaic = np.tile(np.array([[10, 20], [30, 40]], dtype=np.uint16), (4, 4))
    stream = io.BytesIO()
    np.save(stream, mosaic)
    descriptor = {
        "base64": base64.b64encode(stream.getvalue()).decode(),
        "cfa": "RGGB",
        "bit_depth": 12,
        "crop_origin": [1, 1],
        "rois": [[0, 0, 4, 4], [0, 4, 4, 4], [4, 0, 4, 4], [4, 4, 4, 4]],
    }
    _, result = observe_raw(descriptor)
    assert result["rgb_dn"] == [[40.0, 25.0, 10.0]] * 4
    assert result["frame_count"] == 1
    descriptor["rois"][1] = descriptor["rois"][0]
    with pytest.raises(ValueError, match="overlap"):
        observe_raw(descriptor)


def test_qe_scan_and_raw_validation_must_be_independent(api, fixture):
    _, client = api
    fixture["raw_validation"]["provenance"]["session_id"] = fixture["qe_scan"]["provenance"][
        "session_id"
    ]
    result = client.post(ROUTE, json=fixture)
    assert result.status_code == 422
    assert "independent acquisitions" in result.text


def test_training_raw_adc_must_match_scan(api, fixture):
    _, client = api
    fixture["raw_originals"]["training"]["bit_depth"] = 16
    result = client.post(ROUTE, json=fixture)
    assert result.status_code == 422
    assert "fixed QE scan ADC" in result.text


def test_holdout_cannot_test_a_new_candidate_after_renaming(api, fixture):
    _, client = api
    assert client.post(ROUTE, json=fixture).status_code == 201
    changed = deepcopy(fixture)
    changed["ccm_training"]["target_rgb"] = (
        np.asarray(changed["ccm_training"]["target_rgb"]) * 1.05
    ).tolist()
    changed["ccm_validation"]["session_id"] += "-renamed"
    changed["ccm_validation"]["acquisition_id"] += "-renamed"
    changed["raw_validation"]["provenance"]["session_id"] += "-renamed"
    changed["raw_validation"]["provenance"]["acquisition_id"] += "-renamed"
    result = client.post(ROUTE, json=changed)
    assert result.status_code == 409
    assert "already evaluated another candidate" in result.text
