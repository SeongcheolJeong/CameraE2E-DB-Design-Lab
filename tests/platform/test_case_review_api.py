"""Case review transport preserves exact evidence and immutable revision identity."""

from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient


@pytest.fixture
def api(tmp_path):
    workspace = tmp_path / "workspace"
    app = create_app(tmp_path / "state", workspace, start_worker=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        session = client.get("/api/v1/session").json()
        headers = {"x-platform-token": session["token"]}
        yield app.state.service, client, headers, workspace


def draft(**changes):
    return {
        "request_id": uuid4().hex,
        "name": "주변부 색 응답 검토",
        "question": "동일 시험에서 렌즈 교체의 영향을 어떻게 구분할까?",
        "decision": {"status": "open", "rationale": "독립 실측을 확보하기 전 검토 단계"},
        **changes,
    }


def save(client, headers, payload):
    response = client.post("/api/v1/case_reviews", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("headers", [{}, {"x-platform-token": "wrong"}])
def test_case_mutation_requires_session_token(api, headers):
    service, client, _, _ = api
    before = service.registry.audit()
    assert client.post("/api/v1/case_reviews", json=draft(), headers=headers).status_code == 403
    assert client.get("/api/v1/case_reviews").json() == []
    assert service.registry.audit() == before


def test_case_token_does_not_grant_cross_origin_access(api):
    _, client, headers, _ = api
    response = client.post(
        "/api/v1/case_reviews", json=draft(),
        headers={**headers, "origin": "https://unrelated.example"},
    )
    assert response.status_code == 403


def test_revision_routes_retry_conflict_and_restart(api):
    service, client, headers, workspace = api
    payload = draft()
    original = save(client, headers, payload)
    audit = service.registry.audit()
    assert save(client, headers, payload) == original
    assert service.registry.audit() == audit
    changed_request = {**payload, "question": "changed under same request identity"}
    assert client.post(
        "/api/v1/case_reviews", json=changed_request, headers=headers
    ).status_code == 409
    next_payload = draft(
        previous_revision_id=original["id"],
        decision={"status": "deferred", "rationale": "교정된 실측 데이터가 더 필요함"},
        unresolved=["고온 조건의 실제 RAW 미확보"],
        next_experiments=[{
            "question": "고온에서 같은 경향이 유지되는가?",
            "reason": "현재 조건 밖의 성능 확인", "mode": "measurement",
            "conditions": {"temperature_c": {"value": 60, "unit": "degC"}},
        }],
    )
    revised = save(client, headers, next_payload)
    assert revised["case_id"] == original["case_id"]
    assert revised["revision"] == original["revision"] + 1
    assert client.get("/api/v1/case_reviews").json() == [revised]
    assert client.get(f"/api/v1/case_reviews/{original['id']}").json() == original
    assert client.get(f"/api/v1/case_reviews/{revised['id']}/history").json() == [
        original, revised,
    ]
    stale = {**next_payload, "request_id": uuid4().hex}
    assert client.post("/api/v1/case_reviews", json=stale, headers=headers).status_code == 409
    reopened = create_app(service.registry.directory, workspace, start_worker=False)
    with TestClient(reopened) as other:
        assert other.get("/api/v1/case_reviews").json() == [revised]
        assert other.get(f"/api/v1/case_reviews/{original['id']}").json() == original


def test_saved_case_workbench_projection_restores_full_calculated_evidence(api):
    service, client, headers, _ = api
    bootstrap = client.get("/api/v1/bootstrap").json()
    assembly = bootstrap["assemblies"][0]
    specification = bootstrap["tests"][0]
    request = {
        "assembly_id": assembly["id"], "test_id": specification["id"],
        "plugin_id": "response_db", "parameters": {"frames": 2, "output_size": [8, 8]},
    }
    submitted = client.post("/api/v1/runs", json=request, headers=headers)
    assert submitted.status_code == 202, submitted.text
    job = service.registry.claim()
    service.registry.finish(job["id"], result=service.execute_run(job))
    completed = service.registry.get_run(job["id"])
    review = save(client, headers, draft(
        requirements=[{"key": "R1", "description": "주변부 채널 비율 변화 확인",
                       "test_id": specification["id"]}],
        assembly_ids=[assembly["id"]],
        evidence=[{"kind": "runs", "id": completed["id"], "requirement_keys": ["R1"]}],
    ))
    frozen = review["snapshots"]["evidence"][0]
    assert frozen["snapshot"] == completed
    assert frozen["record_sha256"] == digest(completed)
    assert frozen["record_sha256"] != completed["content_hash"]
    projection = client.get("/api/v1/workbench").json()["case_reviews"][0]
    assert "snapshots" not in projection and "evidence" not in projection
    assert projection["display_summary"]["evidence_count"] == 1
    assert projection["_projection"]["record_sha256"] == digest(review)
    detail = client.get("/api/v1" + projection["_projection"]["detail_path"])
    assert detail.json()["record"] == review
    assert detail.json()["record_sha256"] == digest(review)
    before = deepcopy(review)
    # A separate case cannot become evidence or a new decision for this case.
    save(client, headers, draft(name="別の設計条件", question="独立した設計レビュー"))
    assert client.get(f"/api/v1/case_reviews/{review['id']}").json() == before
    assert before["authority"] == {
        "engineering_note": True, "physical_validation": False, "release_approval": False,
    }


def test_unknown_case_and_invalid_client_snapshot_are_rejected(api):
    service, client, headers, _ = api
    assert client.get("/api/v1/case_reviews/missing").status_code == 404
    assert client.get("/api/v1/case_reviews/missing/history").status_code == 404
    before = service.registry.audit()
    response = client.post(
        "/api/v1/case_reviews", headers=headers,
        json=draft(snapshots={"evidence": []}, authority={"physical_validation": True}),
    )
    assert response.status_code == 422
    assert service.registry.audit() == before


def test_sdk_uses_the_same_case_api_and_session_token(api, monkeypatch):
    _, client, _, _ = api

    def forward(request):
        response = client.request(
            request.method, request.url.path,
            headers=dict(request.headers), content=request.content,
        )
        return httpx.Response(response.status_code, json=response.json())

    transport = httpx.MockTransport(forward)
    http = httpx.Client(base_url="http://testserver", transport=transport)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    with PlatformClient("http://testserver") as sdk:
        review = sdk.create_case_review(draft())
        assert sdk.get_case_review(review["id"]) == review
        assert sdk.list_case_reviews() == [review]
        assert sdk.case_review_history(review["id"]) == [review]


def test_new_case_script_is_served_to_the_browser(api):
    _, client, _, _ = api
    assert '/cases.js' in client.get('/').text
    script = client.get('/cases.js')
    assert script.status_code == 200
    assert 'CameraCaseUI' in script.text


def test_missing_case_raw_does_not_block_other_cases_or_workbench(api):
    service, client, headers, _ = api
    bootstrap = client.get('/api/v1/bootstrap').json()
    queued = client.post('/api/v1/runs', headers=headers, json={
        'assembly_id': bootstrap['assemblies'][0]['id'],
        'test_id': bootstrap['tests'][0]['id'], 'plugin_id': 'response_db',
        'parameters': {'frames': 2, 'output_size': [8, 8]},
    })
    assert queued.status_code == 202
    job = service.registry.claim()
    service.registry.finish(job['id'], result=service.execute_run(job))
    run = service.registry.get_run(job['id'])
    damaged = save(client, headers, draft(
        requirements=[{'key': 'R1', 'description': 'Fixed test', 'test_id': run['test_id']}],
        assembly_ids=[run['assembly_id']],
        evidence=[{'kind': 'runs', 'id': run['id'], 'requirement_keys': ['R1']}],
    ))
    healthy = save(client, headers, draft(name='Independent unaffected case'))
    raw = next(a for a in run['artifacts'] if a['name'] == 'raw_frames.npy')
    service.registry.blob_path(raw['sha256']).unlink()
    before = service.registry.audit()
    response = client.get('/api/v1/workbench')
    assert response.status_code == 200, response.text
    summary = next(x for x in response.json()['case_reviews'] if x['id'] == damaged['id'])
    assert summary['schema'] == 'camera_case_review_unavailable_v1'
    assert summary['availability']['status'] == 'unavailable'
    assert not {'_projection', 'snapshots', 'decision', 'display_summary'} & summary.keys()
    assert client.get('/api/v1/case_reviews/' + healthy['id']).json() == healthy
    # Existing artifact routes distinguish a missing original (404) from
    # checksum corruption (409); neither can be returned as valid evidence.
    assert client.get('/api/v1/case_reviews/' + damaged['id']).status_code == 404
    assert client.get('/api/v1/workbench/case_reviews/' + damaged['id']).status_code == 404
    assert service.registry.audit() == before
