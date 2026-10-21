"""Explorer API/SDK and immutable shortlist contracts; no optical claims."""

import copy

import pytest
from fastapi.testclient import TestClient

from camera_platform import component_explorer as explorer
from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


@pytest.fixture
def app_and_items(tmp_path, monkeypatch):
    rows = [
        {
            "id": identity,
            "family": family,
            "component_id": identity + "-component",
            "revision_id": identity + "-revision",
            "configuration_id": None,
            "name": name,
            "manufacturer": "Example",
            "source_label": identity,
            "source_kind": "patent_prescription"
            if family == "lens"
            else "sensor_report_extraction",
            "content_sha256": digit * 64,
            "structure_available": True,
            "parameters": {
                "focal_length_mm": focal,
                "f_number": fnum,
                "pixel_pitch_um": pitch,
                "optical_stack_height_um": height,
            },
        }
        for identity, family, name, digit, focal, fnum, pitch, height in [
            ("lens-1", "lens", "Patent 1", "a", 4, 1.8, None, None),
            ("lens-2", "lens", "Patent 2", "b", 8, 2.8, None, None),
            ("lens-3", "lens", "Unknown aperture", "c", 6, None, None, None),
            ("sensor-1", "sensor", "Same model separate report 1", "d", None, None, 1.4, 3.5),
            ("sensor-2", "sensor", "Same model separate report 2", "e", None, None, 2.9, None),
        ]
    ]
    monkeypatch.setattr(
        explorer.assets,
        "index",
        lambda workspace: {
            "schema": "fixture",
            "snapshot_id": "snapshot",
            "counts": {"sensor_reports": 2},
            "items": copy.deepcopy(rows),
            "facets": {},
        },
    )
    monkeypatch.setattr(
        explorer.assets,
        "detail",
        lambda workspace, identity: {
            **copy.deepcopy(next(i for i in rows if i["id"] == identity)),
            "structure": {},
            "source_evidence": [],
            "revisions": [],
            "limitations": [],
        },
    )
    monkeypatch.setattr(
        explorer.evidence,
        "bindings",
        lambda service, items: {
            i["id"]: [
                {
                    "id": "result-1",
                    "run_id": "run-1",
                    "available": True,
                    "status": "stored_evidence",
                    "url": "/reference-workshop.html?run=run-1",
                }
            ]
            for i in items
            if i["id"] == "lens-1"
        },
    )
    monkeypatch.setattr(
        explorer.evidence,
        "validate_result",
        lambda service, item, identity: {
            "id": identity,
            "asset_id": item["id"],
            "status": "verified_files",
            "url": "/reference-workshop.html?run=run-1",
        },
    )
    return create_app(tmp_path / "state", tmp_path, start_worker=False, seed_demo=False), rows


def sdk(http):
    client = object.__new__(PlatformClient)
    client.http = http
    http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
    return client


def selection(items):
    return {
        "name": "Wide lens candidates",
        "question": "Find a compact low f-number design",
        "items": [
            {
                "asset_id": i["id"],
                "revision_id": i["revision_id"],
                "content_sha256": i["content_sha256"],
                "reason": "Source-supported option",
            }
            for i in items
        ],
    }


def test_sdk_filters_detail_result_and_shortlist_roundtrip(app_and_items):
    app, rows = app_and_items
    with TestClient(app) as http:
        client = sdk(http)
        found = client.search_design_assets(family="lens", f_number_max=2)
        assert [i["id"] for i in found["items"]] == ["lens-1"]
        assert found["counts"]["sensor_reports"] == 2
        assert client.search_design_assets(has_results=True)["total"] == 1
        assert client.search_design_assets(q="separate report")["total"] == 2
        assert client.search_design_assets(family="sensor", stack_height_min=0)["total"] == 1
        assert client.design_asset("lens-1")["results"][0]["id"] == "result-1"
        assert client.design_asset_result("lens-1", "result-1")["status"] == "verified_files"
        saved = client.save_design_shortlist(selection(rows[:2]))
        assert saved["items"][0]["result_references"][0]["id"] == "result-1"
        assert client.design_shortlists()["shortlists"][0]["id"] == saved["id"]
        restored = client.design_shortlist(saved["id"])
        assert restored["all_current"]
        assert restored["items"][0]["reason"] == "Source-supported option"
        assert restored["items"][1]["item"]["parameters"]["f_number"] == 2.8
        assert not any("results" in i for i in rows), "Enrichment must not mutate catalog cache"


def test_shortlist_keeps_old_evidence_and_marks_changed_or_missing(app_and_items):
    app, rows = app_and_items
    with TestClient(app) as http:
        client = sdk(http)
        saved = client.save_design_shortlist(selection(rows[:2]))
        rows[0]["parameters"]["f_number"] = 2.1
        rows.pop(1)
        got = client.design_shortlist(saved["id"])
        assert not got["all_current"]
        assert [i["current_status"] for i in got["items"]] == ["stale", "missing"]
        assert got["items"][0]["item"]["parameters"]["f_number"] == 1.8
        assert got["content_hash"] == saved["content_hash"]


def test_shortlist_retains_saved_links_but_refreshes_current_availability(
    app_and_items, monkeypatch
):
    app, rows = app_and_items
    with TestClient(app) as http:
        client = sdk(http)
        saved = client.save_design_shortlist(selection(rows[:1]))
        monkeypatch.setattr(
            explorer.evidence,
            "bindings",
            lambda service, items: {
                "lens-1": [{"id": "result-1", "available": False, "status": "unavailable"}]
            },
        )
        got = client.design_shortlist(saved["id"])
        entry = got["items"][0]
        assert entry["result_references"][0]["available"]
        assert not entry["current_result_references"][0]["available"]
        assert not entry["results_verified"]
        assert got["content_hash"] == saved["content_hash"]


def test_missing_catalog_preserves_historical_shortlist(app_and_items, monkeypatch):
    app, rows = app_and_items
    with TestClient(app) as http:
        client = sdk(http)
        saved = client.save_design_shortlist(selection(rows[:1]))

        def unavailable(workspace):
            raise FileNotFoundError("private source path")

        monkeypatch.setattr(explorer.assets, "index", unavailable)
        got = client.design_shortlist(saved["id"])
        assert not got["source_available"] and not got["all_current"]
        assert got["items"][0]["current_status"] == "source_unavailable"
        assert got["items"][0]["item"] == saved["items"][0]["item"]
        assert got["items"][0]["current_result_references"] == []
        response = http.get(explorer.ROUTE + "/catalog")
        assert response.status_code == 503
        assert "private source path" not in response.text


@pytest.mark.parametrize(
    "mutation,status",
    [
        (lambda p, r: p["items"].append(p["items"][0]), 422),
        (lambda p, r: p["items"][0].update(revision_id="different"), 409),
        (lambda p, r: p["items"][0].update(content_sha256="f" * 64), 409),
        (lambda p, r: p["items"].append(selection([r[3]])["items"][0]), 422),
        (lambda p, r: p.update(items=[]), 422),
        (lambda p, r: p.update(question=""), 422),
        (lambda p, r: p.update(unexpected="anything"), 422),
    ],
)
def test_rejects_unpinned_or_ambiguous_shortlists(app_and_items, mutation, status):
    app, rows = app_and_items
    payload = selection(rows[:2])
    mutation(payload, rows)
    with TestClient(app) as http:
        sdk(http)
        reply = http.post(explorer.ROUTE + "/shortlists", json=payload)
        assert reply.status_code == status, reply.text
        assert http.get(explorer.ROUTE + "/shortlists").json() == {"shortlists": []}


@pytest.mark.parametrize(
    "query",
    [
        "family=product",
        "f_number_min=3&f_number_max=1",
        "f_number_max=nan",
        "f_number_min=-1",
        "has_results=maybe",
        "offset=-1",
        "limit=0",
        "bad_field=3",
    ],
)
def test_invalid_filters_fail_instead_of_showing_unfiltered_results(app_and_items, query):
    app, _ = app_and_items
    with TestClient(app) as http:
        reply = http.get(explorer.ROUTE + "/catalog?" + query)
        assert reply.status_code == 422


def test_save_requires_existing_csrf_session(app_and_items):
    app, rows = app_and_items
    with TestClient(app) as http:
        assert (
            http.post(explorer.ROUTE + "/shortlists", json=selection(rows[:1])).status_code == 403
        )
