"""Persistence, trust boundaries and HTTP guards for the local workbench."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
from contextlib import closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pyisetcam._catalog_store import CatalogWriter, connect
from pyisetcam.design_workbench import (
    ConflictError,
    DesignWorkbench,
    contained_file,
    evaluate_requirements,
    validate_conditions,
)


@pytest.fixture
def workbench(tmp_path):
    root = tmp_path / "camerae2e_db"
    with closing(connect(root / "catalog.sqlite", write=True)) as conn, conn:
        writer = CatalogWriter(conn, root)
        source = writer.source("fixture", "test-only")
        lens = writer.component("lens", "lens", "Test", "Lens", "patent_design")
        sensor = writer.component("sensor", "sensor", "Test", "Sensor", "report_subject")
        lens_rev = writer.revision(
            lens,
            "patent_prescription",
            {"specifications": {"focal_length_mm": 35.14, "f_number": 3.61}},
            source_id=source,
        )
        sensor_rev = writer.revision(sensor, "sensor_report_extraction", {}, source_id=source)
        point = writer.operating_point(lens_rev, "focus", {"focal_length_mm": 35.14})
    svc = DesignWorkbench(tmp_path)
    yield svc, lens_rev, sensor_rev, point
    svc.executor.shutdown(wait=True)


def test_study_persists_and_catalog_is_unchanged(workbench):
    svc, lens, sensor, point = workbench
    before = hashlib.sha256((svc.catalog_root / "catalog.sqlite").read_bytes()).hexdigest()
    study = svc.create_study({"kind": "concept", "title": "저장 검토"})
    study = svc.add_variant(
        study["id"],
        {
            "version": study["version"],
            "label": "기준안",
            "lens_revision_id": lens,
            "lens_point_id": point,
            "sensor_revision_id": sensor,
        },
    )
    restored = DesignWorkbench(svc.workspace)
    assert restored.get_study(study["id"]) == study
    restored.executor.shutdown(wait=True)
    frozen = study["variants"][0]
    assert frozen["lens"]["snapshot"]["specifications"]["focal_length_mm"] == 35.14
    assert frozen["compatibility"]["status"] == "unknown"
    assert hashlib.sha256((svc.catalog_root / "catalog.sqlite").read_bytes()).hexdigest() == before


def test_reject_stale_edits_and_wrong_model_identity(workbench):
    svc, lens, sensor, point = workbench
    study = svc.create_study({"kind": "edge"})
    svc.update_study(study["id"], {"version": 1, "title": "변경"})
    with pytest.raises(ConflictError):
        svc.update_study(study["id"], {"version": 1, "title": "오래된 변경"})
    with pytest.raises(ValueError):
        svc.freeze_revision(sensor, "lens")
    with pytest.raises(ValueError):
        svc.freeze_revision(sensor, "sensor", point)
    with pytest.raises(ValueError):
        svc.start_experiment(study["id"], {"version": 2, "variant_ids": [lens, lens]})


def test_evidence_freezes_selected_case_and_decision_requires_real_refs(workbench):
    svc, *_ = workbench
    folder = svc.workspace / "reports/physical_sensor_e2e/comparison"
    folder.mkdir(parents=True)
    source = {
        "schema": "fixture",
        "controls": {"exposure_s": 0.004},
        "cases": [{"case_id": "case_a", "cra_deg": 20, "mean_photoelectrons": 17.0}],
    }
    (folder / "comparison.json").write_text(json.dumps(source))
    study = svc.create_study({"kind": "edge"})
    with pytest.raises(ValueError):
        svc.attach_evidence(
            study["id"], {"version": 1, "report_id": "physical", "case_ids": ["invented"]}
        )
    study = svc.attach_evidence(
        study["id"],
        {"version": 1, "report_id": "physical", "case_ids": ["case_a"], "note": "각도 확인"},
    )
    frozen = study["evidence"][0]
    assert frozen["case_snapshots"][0]["cra_deg"] == 20
    assert frozen["method"]["controls"] == {"exposure_s": 0.004}
    (folder / "comparison.json").write_text('{"cases": []}')
    assert svc.get_study(study["id"])["evidence"][0] == frozen
    with pytest.raises(ValueError):
        svc.add_decision(
            study["id"], {"version": 2, "conclusion": "확정", "evidence_ids": ["fake"]}
        )
    study = svc.add_decision(
        study["id"],
        {
            "version": 2,
            "conclusion": "추가 실험",
            "remaining": "실측 필요",
            "evidence_ids": [frozen["id"]],
        },
    )
    assert study["decisions"][0]["evidence_ids"] == [frozen["id"]]


def test_unknown_metric_stays_unknown_and_nonfinite_inputs_fail():
    requirements = [{"metric": "raw_snr_db", "operator": ">=", "threshold": 20}]
    result = evaluate_requirements(requirements, [{"variant_id": "a", "metrics": {}}])
    assert result[0]["status"] == "unknown"
    conditions = {
        "scene": "uniform",
        "luminance": 10,
        "exposure_ms": 4,
        "frames": 4,
        "seed": 42,
        "output_size": 32,
    }
    for key, value in (("luminance", float("nan")), ("frames", 1000), ("seed", True)):
        with pytest.raises(ValueError):
            validate_conditions({**conditions, key: value})


def test_path_traversal_and_symlink_escape_fail(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (root / "link.txt").symlink_to(secret)
    for relative in ("../secret.txt", "link.txt", str(secret)):
        with pytest.raises((ValueError, FileNotFoundError)):
            contained_file(root, relative)


def test_http_host_origin_token_and_json_size_guards(workbench):
    svc, *_ = workbench
    path = Path(__file__).resolve().parents[2] / "tools/run_design_workbench.py"
    spec = importlib.util.spec_from_file_location("design_server_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_port
    server.RequestHandlerClass = module.make_handler(svc, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, route, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", port)
        conn.request(method, route, body=body, headers=headers or {})
        response = conn.getresponse()
        status, data = response.status, json.loads(response.read())
        conn.close()
        return status, data

    try:
        code, bootstrap = request("GET", "/api/bootstrap")
        assert code == 200
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{port}",
            "X-Workbench-Token": bootstrap["token"],
        }
        assert request("GET", "/api/bootstrap", headers={"Host": "evil.example"})[0] == 403
        assert request("POST", "/api/studies", '{"kind":"concept"}', headers)[0] == 201
        assert (
            request("POST", "/api/studies", "{}", {**headers, "Origin": "https://evil.example"})[0]
            == 403
        )
        assert (
            request("POST", "/api/studies", "{}", {**headers, "X-Workbench-Token": "bad"})[0] == 403
        )
        assert request("POST", "/api/studies", '{"value":NaN}', headers)[0] == 400
        assert (
            request("POST", "/api/studies", "{}", {**headers, "Content-Length": "2000000"})[0]
            == 413
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
