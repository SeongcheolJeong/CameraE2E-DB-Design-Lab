"""Pixel study lifecycle, pinned sources and immutable manual comparisons."""

import copy
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform import reference_pixel_job as pixel
from camera_platform import reference_workshop_job as workshop
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient


def test_pixel_specification_requires_explicit_hashes_and_fixed_baseline():
    spec = pixel.template()
    assert pixel.specification(spec) == spec
    for mutation in (
        lambda s: s.update(baseline_candidate_id="candidate-00"),
        lambda s: s["sources"][0].update(source_manifest_sha256="latest"),
        lambda s: s["sources"][0].update(id="../escape"),
        lambda s: s["sources"][0].update(cfa_thickness_scale=True),
        lambda s: s["sources"].pop(),
    ):
        changed = copy.deepcopy(spec)
        mutation(changed)
        with pytest.raises(ValueError):
            pixel.specification(changed)


def test_source_manifest_is_pinned_before_processing(monkeypatch):
    source = pixel.template()["sources"][0]
    monkeypatch.setattr(
        workshop,
        "get",
        lambda *_: {"status": "succeeded", "result": {"summary": {"manifest_sha256": "0" * 64}}},
    )
    with pytest.raises(IntegrityError, match="manifest pin"):
        pixel._load_source(None, source)


def test_pixel_job_common_lifecycle_lazy_download_save_and_cancel(tmp_path, monkeypatch):
    from camera_platform import reference_pixel_lab as lab
    from camera_platform import reference_pixel_report as report

    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path / "external"))
    calls = []

    def load(service, pin):
        config = workshop.defaults()
        config.update(luminance_cd_m2=1000)
        config["pixel"]["cfa_thickness_scale"] = pin["cfa_thickness_scale"]
        selected = {"id": pin["id"], "configuration": config}
        return tmp_path, selected, tmp_path, {"candidates": [selected]}

    def build(directory, selected, **kwargs):
        kwargs["cancel"]()
        calls.append(selected["id"])
        return {
            "id": selected["id"],
            "cfa_thickness_scale": selected["configuration"]["pixel"]["cfa_thickness_scale"],
            "fields": [{"id": "center", "status": "available"}],
            "dependencies": [],
        }

    monkeypatch.setattr(pixel, "_load_source", load)
    monkeypatch.setattr(pixel, "_verify_source_files", lambda *args: None)
    monkeypatch.setattr(lab, "build_candidate", build)
    monkeypatch.setattr(lab, "add_delta", lambda value, baseline: value)
    monkeypatch.setattr(
        report,
        "write_report",
        lambda result, directory: (directory / "report.html").write_text("Pixel report"),
    )
    app = create_app(
        tmp_path / "state", workspace=Path(__file__).parents[2], start_worker=False, seed_demo=False
    )
    service = app.state.service
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        client = object.__new__(PlatformClient)
        client.http = http
        record = client.submit_reference_pixel_study(pixel.template())
        claimed = service.registry.claim()
        assert claimed["id"] == record["id"]
        service.registry.finish(record["id"], result=service.execute_run(claimed))
        complete = client.wait_reference_workshop(record["id"], timeout=1, poll_interval=0)
        assert complete["status"] == "succeeded"
        assert calls == ["candidate-00", "candidate-01", "candidate-02"]
        summary = complete["result"]["summary"]
        assert not summary["raw_copied"] and summary["new_acquisitions"] == 0
        assert client.reference_pixel_studies()["studies"][0]["id"] == record["id"]
        detail = client.reference_pixel_candidate(record["id"], "candidate-00")
        assert (
            detail["source_refs"]["acquisition"]["run_id"]
            == pixel.template()["sources"][0]["source_run_id"]
        )
        selection = {
            "candidate_id": "candidate-00",
            "baseline_candidate_id": "candidate-01",
            "field_id": "center",
        }
        saved = client.save_reference_pixel_comparison(record["id"], selection)
        assert saved["selection"]["site"] == "Gr"
        assert http.get(saved["download_url"]).json()["id"] == saved["id"]
        assert http.get(saved["report_url"]).status_code == 200
        assert http.get(summary["report_url"]).status_code == 200
        bundle = http.get(workshop.ROUTE + "/runs/" + record["id"] + "/bundle")
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as exported:
            assert "candidates/candidate-00.json" in exported.namelist()
            assert not any("raw-" in p for p in exported.namelist())
        invalid = http.post(
            pixel.ROUTE + "/" + record["id"] + "/comparisons",
            json={**selection, "field_id": "missing"},
        )
        assert invalid.status_code >= 400
        pending = client.submit_reference_pixel_study(
            {**pixel.template(), "name": "Cancelled Pixel study"}
        )
        assert client.cancel_reference_workshop(pending["id"])["status"] == "cancelled"
        path = workshop._directory(service, record["id"]) / "candidates/candidate-00.json"
        changed = json.loads(path.read_text())
        changed["cfa_thickness_scale"] = 999
        path.write_text(json.dumps(changed))
        assert http.get(saved["download_url"]).status_code >= 400
        assert (
            http.get(pixel.ROUTE + "/" + record["id"] + "/candidates/candidate-00").status_code
            >= 400
        )


def test_source_file_mutation_and_cancel_interrupt_verification(tmp_path, monkeypatch):
    from camera_platform.virtual_lab_job import sha256

    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path))
    spec = pixel.template()
    for source in spec["sources"]:
        for key in ("source_run_id", "color_run_id"):
            directory = workshop._directory(None, source[key])
            directory.mkdir(parents=True, exist_ok=True)
            raw = directory / "raw.npy"
            raw.write_bytes(b"retained raw")
            (directory / "manifest.json").write_text(
                json.dumps({"files": {"raw.npy": sha256(raw)}})
            )
    pixel._verify_source_files(None, spec, lambda *_: None)

    def cancel(*_):
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        pixel._verify_source_files(None, spec, cancel)
    raw.write_bytes(b"tampered")
    with pytest.raises(IntegrityError, match="artifact changed"):
        pixel._verify_source_files(None, spec, lambda *_: None)
