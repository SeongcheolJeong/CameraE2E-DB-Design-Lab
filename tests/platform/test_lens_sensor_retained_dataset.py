"""A saved Lens × Sensor integer RAW can feed the dataset workflow unchanged."""

from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from camera_platform import retained_raw_dataset_job
from camera_platform.api import create_app
from camera_platform.registry import canonical
from camera_platform.retained_raw_dataset import (
    export_retained_raw_array,
    load_retained_lens_sensor_raw,
    make_portable_bundle,
    raw_annotation_template,
    validate_raw_annotation,
)
from camera_platform.retained_raw_dataset_job import (
    default_lens_labeled_spec,
    default_lens_spec,
    specification,
)
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute
from pyisetcam.dataset import _sha256_array, camerae2e_dataset_validate

WORKSPACE = Path(__file__).resolve().parents[2]


def _client(transport):
    client = object.__new__(PlatformClient)
    client.http = transport
    transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
    return client


@pytest.mark.parametrize("scope", ["central", "local_field_psf"])
def test_lens_sensor_source_exports_exact_raw_and_lineage(tmp_path, monkeypatch, scope):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        template = client.lens_sensor_raw_template(scope=scope)
        lens = next(
            (
                row for row in template["lens_catalog"]["revisions"]
                if row["preview_status"] == "ready"
            ),
            None,
        )
        sensor = next(
            (row for row in template["sensor_catalog"] if row["report_code"] == "DEP-2209-801"),
            None,
        )
        if lens is None or sensor is None:
            pytest.skip("Required pinned revisions are unavailable")
        capture_spec = template["specification"]
        capture_spec.update(
            lens_revision_id=lens["revision_id"],
            sensor_revision={"identifier": "DEP-2209-801", "run_id": sensor["run_id"]},
            sensor_size=[8, 8],
            accept_approximate_lens_radiometry=True,
        )
        if scope == "local_field_psf":
            capture_spec.update(field_psf_index=2, field_azimuth_deg=90)
        capture = client.submit_lens_sensor_raw(capture_spec)
        assert app.state.service.registry.claim()["id"] == capture["id"]
        execute(app.state.service, capture["id"])
        source = client.wait(capture["id"], timeout=5, expected_kind="lens_sensor_raw")
        assert source["status"] == "succeeded", source.get("error")
        preview = source["result"]["summary"]["preview"]
        raw_bytes = transport.get(preview["raw_artifact"]["url"]).content
        source_raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)

        selected = client.retained_raw_dataset_template(source_kind="lens_sensor_raw")
        assert any(item["id"] == capture["id"] for item in selected["source_jobs"])
        dataset_spec = selected["specification"]
        assert dataset_spec["schema"] == default_lens_spec()["schema"]
        dataset_spec.update(source_job_id=capture["id"], acknowledge_simulation_limits=True)
        queued = client.submit_retained_raw_dataset(dataset_spec)
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], timeout=5, expected_kind="retained_raw_dataset")
        assert finished["status"] == "succeeded", finished.get("error")
        result = finished["result"]["summary"]
        identities = source["result"]["summary"]["source_identities"]
        assert result["source_kind"] == "lens_sensor_raw"
        assert result["source_binding"]["source_lens_sensor_job_id"] == capture["id"]
        assert result["source_binding"]["lens_identity"] == identities["lens"]
        assert result["source_binding"]["saved_sensor_revision"] == identities["sensor"]
        assert result["source_binding"]["lens_scope"] == preview["scope"]
        assert result["source_binding"]["lens_radiometry"] == "assumed_unity_not_validated"
        assert result["raw_values_byte_equal"] is True
        assert result["perception_metric"]["available"] is False
        bundle = transport.get(f"/api/v1/retained-raw-datasets/{queued['id']}/export").content
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        with ZipFile(io.BytesIO(bundle)) as archive:
            archive.extractall(extracted)
        with np.load(extracted / "raw/retained_0000.npz", allow_pickle=False) as data:
            assert data["raw"].dtype == np.int32
            assert data["raw"].tobytes(order="C") == source_raw.tobytes(order="C")
            assert data["seed"].tolist() == [capture_spec["seed"]]
        manifest = json.loads((extracted / "manifest.json").read_text())
        assert manifest["source_binding"] == result["source_binding"]
        monkeypatch.chdir(extracted)
        assert camerae2e_dataset_validate("manifest.json")["ok"]


def test_lens_dataset_requires_explicit_source_and_no_labels():
    spec = default_lens_spec()
    with pytest.raises(ValueError, match="acknowledgement"):
        specification(spec)
    spec.update(source_job_id="capture-1", acknowledge_simulation_limits=True)
    with pytest.raises(ValueError, match="unlabeled"):
        specification(dict(spec, labels="automatic"))
    with pytest.raises(ValueError, match="source_kind"):
        specification(dict(spec, source_kind="saved_sensor_quality"))


def test_user_supplied_raw_boxes_are_hash_bound_and_strict(tmp_path):
    raw = np.arange(64, dtype=np.int32).reshape(8, 8)
    source_binding = {
        "source_lens_sensor_job_id": "capture-1",
        "source_raw_content_sha256": hashlib.sha256(raw.tobytes(order="C")).hexdigest(),
        "source_raw_artifact_sha256": "a" * 64,
        "lens_identity": {"revision_id": "lens-1"},
        "saved_sensor_revision": {"run_id": "sensor-1"},
        "nominal_value": 10000.0,
    }
    source_binding["source_raw_content_sha256"] = _sha256_array(raw)
    annotation = raw_annotation_template(source_binding, [8, 8])
    annotation["provenance"].update(provider_id="engineer-1", annotation_set_id="set-1")
    annotation["objects"] = [{"label": "target", "bbox_xyxy": [1, 2, 6, 7]}]
    validated = validate_raw_annotation(
        annotation, source_binding=source_binding, raw_shape=[8, 8]
    )
    browser_roundtrip = json.loads(json.dumps(annotation))
    browser_roundtrip["source_binding"]["nominal_value"] = 10000
    assert validate_raw_annotation(
        browser_roundtrip, source_binding=source_binding, raw_shape=[8, 8]
    ) == annotation
    browser_roundtrip["source_binding"]["nominal_value"] = True
    with pytest.raises(ValueError, match="source binding"):
        validate_raw_annotation(
            browser_roundtrip, source_binding=source_binding, raw_shape=[8, 8]
        )
    browser_roundtrip["source_binding"]["nominal_value"] = 10**1000
    with pytest.raises(ValueError, match="source binding"):
        validate_raw_annotation(
            browser_roundtrip, source_binding=source_binding, raw_shape=[8, 8]
        )
    annotation_sha = hashlib.sha256(canonical(validated).encode()).hexdigest()
    report = export_retained_raw_array(
        raw,
        source_binding=source_binding,
        seed=7,
        scene="test_scene",
        split="train",
        output_dir=tmp_path / "dataset",
        annotation_document=validated,
        annotation_artifact_sha256=annotation_sha,
    )
    assert report["labels"] == "user_provided_unverified"
    assert report["perception_metric"]["available"] is False
    assert report["annotation_artifact_sha256"] == annotation_sha
    archive = make_portable_bundle(tmp_path / "dataset", tmp_path / "dataset.zip")
    with ZipFile(archive["path"]) as bundle:
        source_bytes = bundle.read("annotations/retained_0000-source.json")
        assert hashlib.sha256(source_bytes).hexdigest() == annotation_sha
        label = json.loads(bundle.read("labels/retained_0000.json"))["labels"]
        manifest = json.loads(bundle.read("manifest.json"))
    assert label["objects"] == [{**annotation["objects"][0], "class_id": 0}]
    assert label["source"] == "user_provided_unverified"
    assert manifest["records"][0]["rgb"] is None
    assert manifest["split_policy"]["holdout_independence_claimed"] is False
    assert "not verified" in manifest["truth_boundary"]
    assert camerae2e_dataset_validate(tmp_path / "dataset" / "manifest.json")["ok"]

    wrong_hash = dict(source_binding, source_raw_content_sha256="0" * 64)
    with pytest.raises(ValueError, match="source binding"):
        validate_raw_annotation(annotation, source_binding=wrong_hash, raw_shape=[8, 8])
    with pytest.raises(ValueError, match="RAW shape"):
        validate_raw_annotation(annotation, source_binding=source_binding, raw_shape=[4, 16])
    bad_box = json.loads(json.dumps(annotation))
    bad_box["objects"][0]["bbox_xyxy"] = [1, 2, 9, 7]
    with pytest.raises(ValueError, match="outside"):
        validate_raw_annotation(bad_box, source_binding=source_binding, raw_shape=[8, 8])
    bad_box["objects"][0]["bbox_xyxy"] = [1, 2, 10**1000, 7]
    with pytest.raises(ValueError, match="outside"):
        validate_raw_annotation(bad_box, source_binding=source_binding, raw_shape=[8, 8])
    bad_box["objects"][0]["bbox_xyxy"] = [1, 2, True, 7]
    with pytest.raises(ValueError, match="invalid"):
        validate_raw_annotation(bad_box, source_binding=source_binding, raw_shape=[8, 8])
    with pytest.raises(ValueError, match="SHA"):
        export_retained_raw_array(
            raw, source_binding=source_binding, seed=7, scene="test_scene", split="train",
            output_dir=tmp_path / "wrong-hash", annotation_document=annotation,
            annotation_artifact_sha256="0" * 64,
        )


def test_labeled_spec_keeps_v2_unlabeled_contract():
    labeled = default_lens_labeled_spec()
    labeled.update(source_job_id="capture-1", annotation_artifact_sha256="a" * 64,
                   acknowledge_simulation_limits=True)
    assert specification(labeled)["labels"] == "user_provided_unverified"
    with pytest.raises(ValueError, match="SHA-256"):
        specification(dict(labeled, annotation_artifact_sha256="not-a-hash"))
    with pytest.raises(ValueError, match="user_provided_unverified status"):
        specification(dict(labeled, labels="unlabeled"))


def test_annotation_import_normalizes_browser_integral_float_before_hash(monkeypatch):
    binding = {
        "source_lens_sensor_job_id": "capture-1",
        "source_raw_content_sha256": "a" * 64,
        "test_gain": 1.0,
    }
    original = raw_annotation_template(binding, [8, 8])
    original["provenance"].update(provider_id="engineer-1", annotation_set_id="set-1")
    browser = json.loads(json.dumps(original))
    browser["source_binding"]["test_gain"] = 1
    original_bytes = canonical(original).encode()
    original_sha = hashlib.sha256(original_bytes).hexdigest()
    browser_sha = hashlib.sha256(canonical(browser).encode()).hexdigest()
    assert browser_sha != original_sha

    class Registry:
        blobs = {}

        def store_blob(self, content, name, media_type):
            sha = hashlib.sha256(content).hexdigest()
            self.blobs[sha] = content
            return {"name": name, "sha256": sha, "media_type": media_type}

    registry = Registry()
    monkeypatch.setattr(
        retained_raw_dataset_job,
        "_lens_source",
        lambda _service, _spec: {
            "source_binding": binding, "source_raw_shape": [8, 8]
        },
    )
    app = FastAPI()
    retained_raw_dataset_job.install_routes(app, SimpleNamespace(registry=registry))
    with TestClient(app) as transport:
        imported = transport.post(
            "/api/v1/retained-raw-datasets/annotations", json={"document": browser}
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["computed_sha256"] == original_sha
        assert imported.json()["artifact"]["sha256"] == original_sha
        assert registry.blobs[original_sha] == original_bytes
        assert imported.json()["expected_sha256_checked"] is False
        rejected = transport.post(
            "/api/v1/retained-raw-datasets/annotations",
            json={"document": browser, "expected_sha256": browser_sha},
        )
        assert rejected.status_code == 422, rejected.text
        checked = transport.post(
            "/api/v1/retained-raw-datasets/annotations",
            json={"document": browser, "expected_sha256": original_sha},
        )
        assert checked.status_code == 201, checked.text
        assert checked.json()["expected_sha256_checked"] is True
        huge_source = json.loads(json.dumps(browser))
        huge_source["source_binding"]["test_gain"] = 10**1000
        rejected_source = transport.post(
            "/api/v1/retained-raw-datasets/annotations",
            json={"document": huge_source},
        )
        assert rejected_source.status_code == 422, rejected_source.text
        huge_box = json.loads(json.dumps(browser))
        huge_box["objects"] = [{"label": "target", "bbox_xyxy": [1, 1, 10**1000, 5]}]
        rejected_box = transport.post(
            "/api/v1/retained-raw-datasets/annotations",
            json={"document": huge_box},
        )
        assert rejected_box.status_code == 422, rejected_box.text


def test_lens_source_user_annotation_import_worker_and_export(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        capture_template = client.lens_sensor_raw_template(scope="central")
        lens = next(
            (row for row in capture_template["lens_catalog"]["revisions"]
             if row["preview_status"] == "ready"), None,
        )
        sensor = next(
            (row for row in capture_template["sensor_catalog"]
             if row["report_code"] == "DEP-2209-801"), None,
        )
        if lens is None or sensor is None:
            pytest.skip("Required pinned revisions are unavailable")
        capture_spec = capture_template["specification"]
        capture_spec.update(
            lens_revision_id=lens["revision_id"],
            sensor_revision={"identifier": "DEP-2209-801", "run_id": sensor["run_id"]},
            sensor_size=[8, 8],
            accept_approximate_lens_radiometry=True,
        )
        capture = client.submit_lens_sensor_raw(capture_spec)
        assert app.state.service.registry.claim()["id"] == capture["id"]
        execute(app.state.service, capture["id"])
        source = client.wait(capture["id"], timeout=5, expected_kind="lens_sensor_raw")
        assert source["status"] == "succeeded", source.get("error")

        template_response = transport.get(
            "/api/v1/retained-raw-datasets/template",
            params={"source_kind": "lens_sensor_raw_labeled", "source_job_id": capture["id"]},
        )
        assert template_response.status_code == 200, template_response.text
        template = template_response.json()
        annotation = template["annotation_template"]
        annotation["provenance"].update(
            provider_id="engineer-1", annotation_set_id="review-1",
            method="human_review", coverage="full_frame_reviewed",
        )
        annotation["objects"] = [{"label": "chart", "bbox_xyxy": [1, 1, 5, 5]}]
        annotation_sha = hashlib.sha256(canonical(annotation).encode()).hexdigest()
        wrong = json.loads(json.dumps(annotation))
        wrong["source_binding"]["source_raw_content_sha256"] = "0" * 64
        rejected = transport.post(
            "/api/v1/retained-raw-datasets/annotations",
            json={"document": wrong,
                  "expected_sha256": hashlib.sha256(canonical(wrong).encode()).hexdigest()},
        )
        assert rejected.status_code == 422, rejected.text
        mismatch = transport.post(
            "/api/v1/retained-raw-datasets/annotations",
            json={"document": annotation, "expected_sha256": "0" * 64},
        )
        assert mismatch.status_code == 422, mismatch.text
        imported = transport.post(
            "/api/v1/retained-raw-datasets/annotations",
            json={"document": annotation},
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["artifact"]["sha256"] == annotation_sha
        assert imported.json()["computed_sha256"] == annotation_sha
        assert imported.json()["expected_sha256_checked"] is False
        dataset_spec = template["specification"]
        dataset_spec.update(
            source_job_id=capture["id"],
            annotation_artifact_sha256=annotation_sha,
            acknowledge_simulation_limits=True,
        )
        queued = client.submit_retained_raw_dataset(dataset_spec)
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], timeout=5, expected_kind="retained_raw_dataset")
        assert finished["status"] == "succeeded", finished.get("error")
        summary = finished["result"]["summary"]
        assert summary["labels"] == "user_provided_unverified"
        assert summary["annotation_binding"]["annotation_artifact_sha256"] == annotation_sha
        assert summary["perception_metric"]["available"] is False
        exported = transport.get(f"/api/v1/retained-raw-datasets/{queued['id']}/export")
        assert exported.status_code == 200, exported.text
        with ZipFile(io.BytesIO(exported.content)) as archive:
            source_annotation = archive.read("annotations/retained_0000-source.json")
            labels = json.loads(archive.read("labels/retained_0000.json"))["labels"]
            manifest = json.loads(archive.read("manifest.json"))
            assert hashlib.sha256(source_annotation).hexdigest() == annotation_sha
            assert json.loads(source_annotation) == annotation
            assert labels["objects"] == [{**annotation["objects"][0], "class_id": 0}]
            assert labels["coordinate_frame"] == annotation["coordinate_frame"]
            assert manifest["annotation_artifact_sha256"] == annotation_sha
            assert manifest["records"][0]["rgb"] is None
            assert manifest["split_policy"]["holdout_independence_claimed"] is False
            raw_bytes = archive.read("raw/retained_0000.npz")
            with np.load(io.BytesIO(raw_bytes), allow_pickle=False) as data:
                assert data["raw"].shape == (8, 8)
                assert data["raw"].dtype == np.int32


def test_bounded_v3_loader_checks_execution_evidence_and_exact_raw(tmp_path):
    raw = np.arange(64, dtype=np.int32).reshape(8, 8)
    raw_path = tmp_path / "raw.npy"
    with raw_path.open("wb") as stream:
        np.save(stream, raw, allow_pickle=False)
    raw_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    resource = {"status": "within_development_budget", "scope": "central_bounded_strict_raytrace"}
    preview = {
        "resource_preflight": resource,
        "scope": "approximate_geometric_central_field_raw_preview",
        "raw_artifact": {"sha256": raw_hash, "shape": [8, 8]},
        "raw": {"dtype": "int32", "shape": [8, 8], "values": raw.tolist()},
        "lens_model": {
            "field_psf_selection": {"psf_compute_mode": "bounded_streaming"},
            "psf_execution": {
                "mode": "strict_bounded_streaming", "complete_precomputed_stack": False,
                "peak_cached_kernels": 4,
            },
        },
        "physical_validation": "not_established",
        "capture": {"seed": 7},
        "lens_identity": {"revision_id": "lens-1"},
        "sensor_revision": {"run_id": "sensor-1"},
        "scene": {"kind": "slanted_edge_d65"},
    }
    report = {
        "schema": "camerae2e_lens_sensor_raw_preview_study_v3",
        "status": "completed",
        "specification": {
            "schema": "camerae2e_lens_sensor_raw_preview_study_v3",
            "accept_approximate_lens_radiometry": True, "seed": 7,
        },
        "resource_preflight": resource,
        "preview": preview,
    }
    report_path = tmp_path / "report.json"

    def load():
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return load_retained_lens_sensor_raw(
            report_path, raw_path, source_lens_sensor_job_id="source-1"
        )

    _, saved, binding = load()
    assert saved.tobytes() == raw.tobytes()
    assert binding["source_lens_sensor_job_id"] == "source-1"
    preview["lens_model"]["psf_execution"]["mode"] = "complete_precomputed_stack"
    with pytest.raises(ValueError, match="Bounded"):
        load()
    preview["lens_model"]["psf_execution"]["mode"] = "strict_bounded_streaming"
    preview["resource_preflight"] = {**resource, "status": "resource_blocked"}
    with pytest.raises(ValueError, match="Bounded"):
        load()
    preview["resource_preflight"] = resource
    raw_path.write_bytes(raw_path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash differs"):
        load()


def test_common_scene_v4_loader_preserves_input_and_output_grid_limits(tmp_path):
    raw = np.arange(64, dtype=np.int32).reshape(8, 8)
    raw_path = tmp_path / "raw.npy"
    with raw_path.open("wb") as stream:
        np.save(stream, raw, allow_pickle=False)
    resource = {"status": "within_development_budget", "scope": "central_bounded_strict_raytrace"}
    grid = {
        "mode": "explicit_common_input_scene", "source_shape": [32, 32],
        "horizontal_fov_deg": 0.2, "vertical_fov_deg": 0.2,
        "center_angular_step_deg_row_col": [math.degrees(math.atan(
            2 * math.tan(math.radians(0.2) / 2) / 32
        ))] * 2,
        "native_output_grid_alignment": "not_established",
    }
    output = {
        "status": "paraxial_center_only_full_distortion_map_not_established",
        "sensor_resize": False, "pixel_pitch_m_row_col": [1e-6, 1e-6],
        "image_distance_m": 0.02,
        "center_angular_step_deg_row_col": [math.degrees(math.atan(1e-6 / 0.02))] * 2,
        "horizontal_fov_deg": math.degrees(2 * math.atan(8e-6 / 0.04)),
        "vertical_fov_deg": math.degrees(2 * math.atan(8e-6 / 0.04)),
    }
    schema = "camerae2e_lens_sensor_raw_preview_study_v4"
    report = {
        "schema": schema, "status": "completed",
        "specification": {"schema": schema, "scene_horizontal_fov_deg": 0.2,
                          "sensor_size": [8, 8],
                          "accept_approximate_lens_radiometry": True, "seed": 7},
        "resource_preflight": resource,
        "preview": {
            "resource_preflight": resource,
            "scope": "approximate_geometric_central_field_raw_preview",
            "raw_artifact": {"sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                             "shape": [8, 8]},
            "raw": {"dtype": "int32", "shape": [8, 8], "values": raw.tolist()},
            "lens_model": {
                "field_psf_selection": {"psf_compute_mode": "bounded_streaming"},
                "psf_execution": {"mode": "strict_bounded_streaming",
                                  "complete_precomputed_stack": False, "peak_cached_kernels": 4},
            },
            "physical_validation": "not_established",
            "capture": {"seed": 7, "native_output_angular_grid": output},
            "lens_identity": {"revision_id": "lens-1"},
            "sensor_model": {"pixel_pitch_um": 1.0},
            "sensor_revision": {"run_id": "sensor-1"},
            "scene": {"kind": "slanted_edge_d65", "horizontal_fov_deg": 0.2,
                      "angular_grid_contract": grid},
        },
    }
    report_path = tmp_path / "report.json"

    def load():
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return load_retained_lens_sensor_raw(
            report_path, raw_path, source_lens_sensor_job_id="source-1"
        )

    _, saved, binding = load()
    assert saved.tobytes() == raw.tobytes()
    assert binding["source_report_sha256"] == hashlib.sha256(report_path.read_bytes()).hexdigest()
    grid["native_output_grid_alignment"] = "matched"
    with pytest.raises(ValueError, match="Common input scene"):
        load()
    grid["native_output_grid_alignment"] = "not_established"
    output["center_angular_step_deg_row_col"] = [0.01, float("nan")]
    with pytest.raises(ValueError, match="Common input scene"):
        load()
    output["center_angular_step_deg_row_col"] = [math.degrees(math.atan(1e-6 / 0.02))] * 2
    output["horizontal_fov_deg"] *= 1.1
    with pytest.raises(ValueError, match="internally inconsistent"):
        load()
    output["horizontal_fov_deg"] /= 1.1
    grid["center_angular_step_deg_row_col"][0] *= 1.1
    with pytest.raises(ValueError, match="internally inconsistent"):
        load()
