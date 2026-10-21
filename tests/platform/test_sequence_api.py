"""Real HTTP routing and source preservation for temporal sequence registrations."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "sequence-api", start_worker=False)
    with TestClient(app) as client:
        boot = client.get("/api/v1/bootstrap").json()
        client.headers["X-Platform-Token"] = boot["token"]
        yield app.state.service, client, boot


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def scene_fixture(client, assembly_id):
    payload = checked(client.get(
        "/api/v1/templates/temporal-fixture", params={"assembly_id": assembly_id},
    ))
    payload.update(shape=[8, 8], knot_count=2, pattern="static")
    return checked(client.post("/api/v1/scene_sequences/fixture", json=payload), 201)


def test_specific_templates_and_direct_sequence_routes(api):
    _, client, boot = api
    assembly_id = boot["assemblies"][0]["id"]
    draft = checked(client.get(
        "/api/v1/templates/temporal-scene", params={"assembly_id": assembly_id},
    ))
    assert draft["payload"]["schema"] == "timed_spectral_scene_v1"
    scene = scene_fixture(client, assembly_id)
    assert scene["source_kind"] == "estimated"
    assert len(scene["image_snapshots"]) == 2
    assert checked(client.get("/api/v1/scene_sequences/" + scene["id"])) == scene
    assert scene["id"] in {r["id"] for r in checked(client.get("/api/v1/scene_sequences"))}

    draft = checked(client.get(
        "/api/v1/templates/temporal-scene", params={"assembly_id": assembly_id},
    ))
    draft["name"] = "Direct fixed-image timeline"
    direct_scene = checked(client.post("/api/v1/scene_sequences", json=draft), 201)
    payload = checked(client.get("/api/v1/templates/control-sequence", params={
        "assembly_id": assembly_id, "scene_sequence_id": direct_scene["id"],
    }))
    control = checked(client.post("/api/v1/control_sequences", json=payload), 201)
    assert control["assembly_id"] == assembly_id
    assert control["scene_sequence_id"] == direct_scene["id"]
    assert checked(client.get("/api/v1/control_sequences/" + control["id"])) == control
    assert control["id"] in {r["id"] for r in checked(client.get("/api/v1/control_sequences"))}
    specification = checked(client.get("/api/v1/templates/temporal-test", params={
        "assembly_id": assembly_id,
        "scene_sequence_id": direct_scene["id"],
        "control_sequence_id": control["id"],
    }))
    assert specification["metric_id"] == "raw.channel_mean_dn_sequence"
    assert specification["stimulus"]["control_sequence_hash"] == control["content_hash"]
    assert checked(client.post("/api/v1/tests", json=specification), 201)["clock"] == (
        control["payload"]["clock"]
    )
    assert client.get("/api/v1/templates/temporal-test").status_code == 422
    assert client.get("/api/v1/templates/control-sequence").status_code == 422
    assert client.get("/api/v1/scene_sequences/not-registered").status_code == 404


def test_imported_sequence_text_and_artifacts_are_preserved(api):
    service, client, boot = api
    assembly_id = boot["assemblies"][0]["id"]
    scene_fixture(client, assembly_id)
    scene_payload = checked(client.get("/api/v1/templates/temporal-scene", params={
        "assembly_id": assembly_id,
    }))
    scene_payload["name"] = "원문 · 줄바꿈 preserved"
    original = "  \r\n" + json.dumps(scene_payload, ensure_ascii=False, indent=3) + "\r\n"
    scene = checked(client.post("/api/v1/scene_sequences/import", json={
        "document_text": original,
    }), 201)
    control_payload = checked(client.get("/api/v1/templates/control-sequence", params={
        "assembly_id": assembly_id, "scene_sequence_id": scene["id"],
    }))
    control_text = json.dumps(control_payload, indent=2).replace("\n", "\r\n") + "\r\n"
    control = checked(client.post("/api/v1/control_sequences/import", json={
        "document_text": control_text,
    }), 201)
    for record, text in [(scene, original), (control, control_text)]:
        artifact = next(a for a in record["artifacts"] if a["name"].endswith("-source.json"))
        content = text.encode("utf-8")
        assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
        assert client.get(artifact["url"]).content == content
        assert service.registry.blob_path(artifact["id"]).read_bytes() == content
        assert any(a["name"].endswith("-normalized.json") for a in record["artifacts"])
        assert record["physical_validation"] == "not_established"
    artifact = scene["artifacts"][0]
    service.registry.blob_path(artifact["id"]).write_bytes(b"corrupted")
    assert client.get(artifact["url"]).status_code == 409


def test_sequence_boundary_rejects_missing_token_bad_text_and_unknown_fields(api):
    _, client, boot = api
    assembly_id = boot["assemblies"][0]["id"]
    fixture = checked(client.get("/api/v1/templates/temporal-fixture", params={
        "assembly_id": assembly_id,
    }))
    before = checked(client.get("/api/v1/scene_sequences"))
    assert client.post("/api/v1/scene_sequences/fixture", json=fixture, headers={
        "X-Platform-Token": "invalid",
    }).status_code == 403
    assert client.post("/api/v1/scene_sequences/fixture", json={
        **fixture, "hardware_capture": True,
    }).status_code == 422
    for kind in ["scene_sequences", "control_sequences"]:
        for text in ["{broken", "[]", "null", " " * 1_000_001]:
            assert client.post("/api/v1/" + kind + "/import", json={
                "document_text": text,
            }).status_code == 422
        assert client.post("/api/v1/" + kind + "/import", json={
            "document_text": "{}", "ignored_field": True,
        }).status_code == 422
    assert checked(client.get("/api/v1/scene_sequences")) == before


def test_sdk_sequence_conveniences_use_real_routes(api):
    _, client, boot = api
    sdk = PlatformClient.__new__(PlatformClient)
    sdk.http = client
    assembly_id = boot["assemblies"][0]["id"]
    fixture = sdk.template("temporal-fixture", assembly_id=assembly_id)
    fixture.update(shape=[8, 8], knot_count=2, pattern="static")
    original = sdk.create_temporal_fixture(fixture)
    assert original["id"]
    payload = sdk.template("temporal-scene", assembly_id=assembly_id)
    direct = sdk.create_scene_sequence(payload)
    imported = sdk.import_scene_sequence(json.dumps({**payload, "name": "SDK imported"}))
    for sequence in [direct, imported]:
        control = sdk.template("control-sequence", assembly_id=assembly_id,
                               scene_sequence_id=sequence["id"])
        assert sdk.create_control_sequence(control)["scene_sequence_id"] == sequence["id"]
        assert sdk.import_control_sequence(json.dumps(control))["assembly_id"] == assembly_id

    # This test verifies submission intent before the optics runtime is involved.
    captured = []
    sdk.post = lambda path, payload: captured.append((path, payload)) or payload
    submission = sdk.submit_temporal(assembly_id, "test-explicit", direct["id"], "control-fixed",
                                     seed=71, force=True)
    assert captured[0][0] == "runs"
    assert submission["plugin_id"] == "temporal_scene_v1"
    assert submission["scene_id"] is None
    assert submission["scene_sequence_id"] == direct["id"]
    assert submission["control_sequence_id"] == "control-fixed"
    assert submission["parameters"] == {"seed": 71}
    assert submission["force"] is True
