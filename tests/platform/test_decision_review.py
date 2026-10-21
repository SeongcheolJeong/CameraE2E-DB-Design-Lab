"""Read-only saved decisions, metric meaning and selected original RAW access."""

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.decision_review import DecisionReview, EvidenceError, STUDIES

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from open_camera_decision_review import create_review_app


@pytest.fixture
def saved(tmp_path):
    for study in STUDIES.values():
        destination = tmp_path / study["directory"]
        destination.mkdir(parents=True)
        for name, _ in study["documents"].values():
            shutil.copyfile(ROOT / study["directory"] / name, destination / name)
    return DecisionReview(tmp_path, source="workspace")


def copy_raw(review, identity="motion"):
    r=json.loads(review.document(identity, "results"))
    row=(r["development"] if identity=="motion" else r["holdout"])[0]
    path=review._path(identity,row["arrays_file"])
    shutil.copyfile(ROOT / STUDIES[identity]["directory"] / row["arrays_file"],path)
    return row,path,hashlib.sha256(row["arrays_file"].encode()).hexdigest()[:24]


def test_catalog_never_loads_arrays_or_reselects(saved,monkeypatch):
    monkeypatch.setattr(np,"load",lambda *a,**k:pytest.fail("Catalog must not load RAW"))
    data=saved.catalog()
    assert data["read_only"] and not data["unavailable"]
    assert [d["selected_candidate_id"] for d in data["decisions"]]==["radial-e4-g1","clear-e2-g1"]
    assert [len(d["cases"]) for d in data["decisions"]]==[121,18]
    assert all(not d["automatic_approval"] for d in data["decisions"])


def test_failed_stress_noise_and_holdout_remain_distinct(saved):
    signal,motion=saved.catalog()["decisions"]
    assert {(g["split"],g["noise_enabled"],g["count"],g["passed"]) for g in signal["evaluation_groups"]} >= {
        ("holdout",False,3,3),("holdout",True,12,12),("stress",False,1,0),("stress",True,4,0)}
    assert ("stress",False,2,0) in {(g["split"],g["noise_enabled"],g["count"],g["passed"]) for g in motion["evaluation_groups"]}
    assert all(r["signed_charge_transfer"] is None for r in signal["cases"])


def test_signed_contrast_and_reference_color_are_not_reinterpreted(saved):
    data=saved.decision("motion")
    row=next(r for r in data["cases"] if r["scenario_id"]=="stress-contrast-inversion" and r["candidate_id"]=="radial-e4-g1")
    assert row["signed_charge_transfer"]<0 and row["signed_raw_transfer"]<0
    assert "dynamic_contrast_retention" in row["failures"]
    assert any("정의상 0" in n for n in data["scope_notes"])
    assert data["calibration"]["reference_candidate_id"]=="clear-e2-g1"
    radial=next(c for c in data["candidates"] if c["candidate_id"]=="radial-e4-g1")
    assert radial["prior_signal_feasible"] and not radial["motion_development_feasible"]


def test_changed_document_never_displays_a_cached_success(saved):
    saved.catalog()
    path=saved._path("motion","results.json")
    path.write_bytes(path.read_bytes()+b" ")
    with pytest.raises(EvidenceError,match="changed"):
        saved.catalog()


def test_only_selected_original_raw_member_is_returned(saved):
    row,path,identity=copy_raw(saved)
    with zipfile.ZipFile(path) as archive:
        expected=archive.read("raw_dn.npy")
    assert saved.raw_bytes("motion",identity)==expected
    data=saved.case("motion",identity)
    assert data["raw_verified"] and data["raw_sha256"]==row["raw_sha256"]
    assert data["display_range_dn"]==[64,4095] and data["shape"]==[16,16]
    assert "not a full-well" in data["rail_meaning"]


def test_modified_raw_fails_even_with_readable_archive(saved):
    _,path,identity=copy_raw(saved)
    raw=np.load(io.BytesIO(saved.raw_bytes("motion",identity)),allow_pickle=False)
    raw[0,0]+=1
    np.savez_compressed(path,raw_dn=raw)
    with pytest.raises(EvidenceError,match="fingerprint"):
        saved.raw_bytes("motion",identity)


def test_paths_and_unknown_identities_fail_closed(saved,tmp_path):
    with pytest.raises(KeyError):saved.decision("../../outside")
    with pytest.raises(EvidenceError):saved._path("motion","../results.json")
    _,path,identity=copy_raw(saved)
    target=tmp_path/"other.npz";path.replace(target);path.symlink_to(target)
    with pytest.raises(EvidenceError):saved.raw_bytes("motion",identity)
    with pytest.raises(KeyError):saved.raw_bytes("motion","unknown")


def test_missing_archives_are_unavailable_not_synthetic_replacement(tmp_path):
    value=DecisionReview(tmp_path, source="workspace").catalog()
    assert value["decisions"]==[] and len(value["unavailable"])==2


def test_packaged_decisions_open_without_checkout_reports(tmp_path):
    review = DecisionReview(tmp_path)
    assert review.source == "package" and review.root != tmp_path
    catalog = review.catalog()
    assert [d["selected_candidate_id"] for d in catalog["decisions"]] == [
        "radial-e4-g1", "clear-e2-g1"]
    assert not catalog["unavailable"]
    motion_case = catalog["decisions"][1]["cases"][0]
    assert review.case("motion", motion_case["case_id"])["raw_verified"]


def test_packaged_manifest_and_missing_archive_fail_closed(tmp_path):
    package = Path(__file__).resolve().parents[2] / "src/camera_platform/data/r1/decisions"
    copy = tmp_path / "decisions"
    shutil.copytree(package, copy)
    (copy / "manifest.json").write_bytes((copy / "manifest.json").read_bytes() + b" ")
    with pytest.raises(EvidenceError, match="manifest changed"):
        DecisionReview(tmp_path, package_root=copy)
    shutil.copyfile(package / "manifest.json", copy / "manifest.json")
    inventory = json.loads((copy / "manifest.json").read_text())["files"]
    archive = next(name for name in inventory if name.startswith("signal/") and name.endswith(".npz"))
    (copy / archive).unlink()
    # All packaged files are checked before the review is made available.
    with pytest.raises(EvidenceError, match="missing or changed"):
        DecisionReview(tmp_path, package_root=copy)


def test_packaged_modified_archive_fails_before_raw_preview(tmp_path):
    package = Path(__file__).resolve().parents[2] / "src/camera_platform/data/r1/decisions"
    copy = tmp_path / "decisions"
    shutil.copytree(package, copy)
    review = DecisionReview(tmp_path, package_root=copy)
    result = json.loads(review.document("motion", "results"))
    row = result["development"][0]
    path = copy / "motion" / row["arrays_file"]
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    case_id = hashlib.sha256(row["arrays_file"].encode()).hexdigest()[:24]
    with pytest.raises(EvidenceError, match="asset changed"):
        review.raw_bytes("motion", case_id)


def test_standalone_is_get_only_and_creates_no_registry(saved):
    before=set(saved.root.rglob("*"))
    with TestClient(create_review_app(saved.root)) as client:
        assert client.get("/api/v1/decision-review").status_code==200
        assert client.post("/api/v1/decision-review",json={}).status_code==405
        assert client.get("/decision-review.html").status_code==200
        assert client.get("/api/v1/decision-review/motion/documents/choice").content==saved.document("motion","choice")
        assert client.get("/api/v1/decision-review/motion/documents/other").status_code==404
        assert client.get("/api/v1/decision-review",headers={"Host":"example.com"}).status_code==403
        assert client.get("/api/v1/decision-review",headers={"Origin":"https://example.com"}).status_code==403
    assert set(saved.root.rglob("*"))==before


def test_main_api_routes_precede_generic_registry_and_no_new_jobs(saved,tmp_path):
    from camera_platform.api import create_app
    app=create_app(tmp_path/"isolated-registry",workspace=saved.root,start_worker=False,seed_demo=False)
    service=app.state.service
    with TestClient(app) as client:
        before=service.registry.audit()
        response=client.get("/api/v1/decision-review")
        assert response.status_code==200,response.text
        assert len(response.json()["decisions"])==2
        assert client.get("/decision-review.html").status_code==200
        assert client.get("/decision-review.js").status_code==200
        assert service.registry.audit()==before and not service.registry.list_runs()
