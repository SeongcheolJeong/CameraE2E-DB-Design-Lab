"""Independent authored generation/collection registration and preserved evidence.

The fixture is a new synthetic declaration, not a decomposition of collected QE
or a measured sensor model. These checks establish contract and lineage behavior.
"""

from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_native_pixel_workflow import _pure_request

from camera_platform.api import create_app
from camera_platform.composer import _native_response_issues
from camera_platform.contracts import ModelCreate
from camera_platform.native_model_evidence import verify_models
from camera_platform.registry import canonical, digest
from camera_platform.service import PlatformService


def authored_generation_model(base):
    data = {k: deepcopy(v) for k, v in base.items() if k in ModelCreate.model_fields}
    data.update(
        name="Authored generation and carrier routing",
        revision="generation-1",
        parents=[base["id"]],
        source_kind="estimated",
        source_uri="fixture://independent-authored-generation-collection",
        physical_instance=None,
        method_and_version="authored-generation-collection-fixture:1",
        spectral_range={"min": 500, "max": 600, "unit": "nm"},
        included_effects=["sensor_charge_generation", "sensor_charge_collection",
                          "sensor_angular_response", "mean_dark_charge", "readout_clipping"],
    )
    sites = ["R", "Gr", "Gb", "B"]
    generation = {
        "schema": "camera_native_generation_response_v1",
        "site_names": sites,
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": 4,
        "reference_plane": "sensor_surface_before_pixel_optics",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "polarization": "unpolarized",
        "response_basis": "independent_site_generated_electrons_per_incident_pixel_photon",
        "wavelength_nm": [500, 600],
        "theta_deg": [0, 20],
        "phi_deg": [0, 90, 180, 270],
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
        "values": np.broadcast_to([.2, .35, .6, .8], (2, 2, 4, 4)).tolist(),
        "source": {"kind": "estimated", "description": "Independently authored generation"},
    }
    collection = {
        "schema": "camera_charge_collection_v1",
        "site_names": sites.copy(),
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": 4,
        "wavelength_nm": [500, 600],
        "destination_offsets_rc": [[0, 0], [0, 1]],
        "values_axis_order": ["wavelength", "source_site", "destination_offset"],
        "probabilities": [[[.8, .1] for _ in sites] for _ in (500, 600)],
        "interpolation": "linear_wavelength",
        "input_plane": "silicon_generated_charge",
        "output_plane": "collection_well_before_readout",
        "temporal_model": "instantaneous_memoryless",
        "boundary": "absorb_outside_full_sensor",
        "angular_assumption": "direction_independent_depth_marginal",
        "statistics": "single_destination_poisson_thinning",
        "regime": "linear_low_injection",
        "source": {"kind": "estimated", "description": "Authored carrier routing, not measured"},
    }
    data["payload"] = {
        "wavelength_nm": [500, 600], "pixel_pitch_um": 4,
        "readout": deepcopy(base["payload"]["readout"]),
        "native_pixel_response": generation, "charge_collection": collection,
    }
    data["properties"].update(
        active_height={"value": .064, "unit": "mm"},
        active_width={"value": .064, "unit": "mm"},
        accepted_angle_range={"min": 0, "max": 20, "unit": "deg"},
    )
    return data


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "collection-models", start_worker=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        bootstrap = client.get("/api/v1/bootstrap").json()
        client.headers["X-Platform-Token"] = bootstrap["token"]
        base = next(m for m in bootstrap["models"] if m["kind"] == "sensor")
        yield app.state.service, client, base, authored_generation_model(base)


def create(client, data):
    response = client.post("/api/v1/models", json=data)
    assert response.status_code == 201, response.text
    return response.json()


def test_api_registration_preserves_separate_canonical_blobs_and_restart(api):
    service, client, base, data = api
    before = deepcopy(data)
    model = create(client, data)
    assert data == before
    assert model["approval_status"] == "draft"
    assert model["factory"]["physical_validation"] == "not_established"
    assert set(model["response_artifacts"]) == {"native_pixel_response", "charge_collection"}
    verified = verify_models(service, [model])
    for key, artifact in model["response_artifacts"].items():
        expected = canonical(model["payload"][key]).encode()
        assert artifact in verified
        assert artifact["sha256"] == digest(model["payload"][key])
        assert artifact["bytes"] == len(expected)
        assert service.registry.blob_path(artifact["sha256"]).read_bytes() == expected
        download = client.get(artifact["url"])
        assert download.status_code == 200, download.text
        assert download.content == expected
    reopened = PlatformService(service.registry.directory)
    assert verify_models(reopened, [model]) == verified
    assert service.registry.get("models", base["id"]) == base
    assert service.registry.list_runs() == []


def test_generation_registration_validates_readout_without_claiming_quick_estimate_support(
    api, monkeypatch
):
    service, _, _, data = api
    monkeypatch.setattr("camera_platform.service.get_plugin",
                        lambda *args: pytest.fail("Registration invoked a collapsed estimate"))
    assert service.create_model(data)["payload"]["charge_collection"]


@pytest.mark.parametrize("change", [
    "missing_collection", "collected_qe", "extra_qe", "wrong_effects", "duplicate_effects",
    "collection_wave", "collection_pitch", "collection_sites", "collection_efficiency",
    "generation_basis", "readout", "raw_format", "spectral_declaration",
])
def test_registration_refuses_mismatched_pair_without_publishing_model(api, change):
    service, client, _, data = api
    before = service.registry.list("models")
    payload = data["payload"]
    if change == "missing_collection":
        payload.pop("charge_collection")
    elif change == "collected_qe":
        payload["native_pixel_response"].update(
            schema="camera_native_pixel_response_v1",
            response_basis="independent_site_collected_electrons_per_incident_pixel_photon",
        )
        data["included_effects"] = ["sensor_effective_qe", "sensor_angular_response",
                                    "mean_dark_charge", "readout_clipping"]
    elif change == "extra_qe":
        payload["qe"] = [[.1, .1]] * 3
    elif change == "wrong_effects":
        data["included_effects"][0] = "sensor_effective_qe"
    elif change == "duplicate_effects":
        data["included_effects"].append("sensor_charge_collection")
    elif change == "collection_wave":
        payload["charge_collection"]["wavelength_nm"] = [500, 601]
    elif change == "collection_pitch":
        payload["charge_collection"]["pixel_pitch_um"] = 2
    elif change == "collection_sites":
        payload["charge_collection"]["site_names"] = ["R", "Gb", "Gr", "B"]
    elif change == "collection_efficiency":
        payload["charge_collection"]["probabilities"][0][0] = [.9, .2]
    elif change == "generation_basis":
        payload["native_pixel_response"]["response_basis"] = (
            "independent_site_collected_electrons_per_incident_pixel_photon")
    elif change == "readout":
        payload["readout"]["gain"] = -1
    elif change == "raw_format":
        data["raw_format"]["black_level"] += 1
    else:
        data["spectral_range"]["max"] = 650
    response = client.post("/api/v1/models", json=data)
    assert response.status_code == 422, response.text
    assert service.registry.list("models") == before


@pytest.mark.parametrize("field", ["native_pixel_response", "charge_collection"])
@pytest.mark.parametrize("damage,status", [("missing", 404), ("corrupt", 409)])
def test_template_and_model_verification_refuse_unavailable_pair_evidence(
    api, field, damage, status
):
    service, client, _, data = api
    model = create(client, data)
    before = service.registry.list("models")
    path = service.registry.blob_path(model["response_artifacts"][field]["sha256"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"damaged collection evidence")
    response = client.get("/api/v1/templates/native-pixel-model",
                          params={"base_sensor_model_id": model["id"]})
    assert response.status_code == status, response.text
    assert service.registry.list("models") == before
    with pytest.raises((KeyError, ValueError)):
        verify_models(service, [model])


@pytest.mark.parametrize("damage", ["field", "descriptor", "missing_descriptor", "missing_pair"])
def test_canonical_registry_records_cannot_substitute_old_collection_artifacts(api, damage):
    service, client, _, data = api
    model = create(client, data)
    altered = {k: deepcopy(v) for k, v in model.items()
               if k not in {"id", "content_hash", "created_at"}}
    altered["name"] += " damaged fixture"
    if damage == "field":
        altered["payload"]["charge_collection"]["probabilities"][0][0][0] = .7
    elif damage == "descriptor":
        altered["response_artifacts"]["charge_collection"]["name"] = "other.json"
    elif damage == "missing_descriptor":
        altered["response_artifacts"].pop("charge_collection")
    else:
        altered["payload"].pop("charge_collection")
        altered["response_artifacts"].pop("charge_collection")
    stored = service.registry.put("models", altered)
    with pytest.raises(ValueError):
        verify_models(service, [stored])
    assert verify_models(service, [model])


def test_generation_template_copies_pair_and_legacy_template_does_not_infer_generation(api):
    service, client, base, data = api
    model = create(client, data)
    before = service.registry.list("models")
    response = client.get("/api/v1/templates/native-pixel-model",
                          params={"base_sensor_model_id": model["id"]})
    assert response.status_code == 200, response.text
    draft = response.json()
    assert service.registry.list("models") == before
    assert draft["payload"]["charge_collection"] == model["payload"]["charge_collection"]
    generated = draft["payload"]["native_pixel_response"]
    assert generated["schema"] == "camera_native_generation_response_v1"
    assert generated["values"] == model["payload"]["native_pixel_response"]["values"]
    assert generated["source"]["kind"] == "estimated"
    assert model["id"] in generated["source"]["description"]
    assert draft["parents"] == [model["id"]]
    assert draft["included_effects"] == model["included_effects"]
    assert draft["method_and_version"] == "native-generation-collection-copy-template:1"
    assert verify_models(service, [create(client, draft)])
    response = client.get("/api/v1/templates/native-pixel-model",
                          params={"base_sensor_model_id": base["id"]})
    assert response.status_code == 200, response.text
    legacy = response.json()["payload"]
    assert legacy["native_pixel_response"]["schema"] == "camera_native_pixel_response_v1"
    assert "charge_collection" not in legacy


@pytest.mark.parametrize("change", [None, "missing_pair", "pair_pitch", "effect", "collected_pair"])
def test_composer_checks_generation_pair_and_effect_ownership(api, change):
    _, _, _, sensor = api
    lens = {"payload": _pure_request()["lens"]}
    if change == "missing_pair":
        sensor["payload"].pop("charge_collection")
    elif change == "pair_pitch":
        sensor["payload"]["charge_collection"]["pixel_pitch_um"] = 2
    elif change == "effect":
        sensor["included_effects"][0] = "sensor_effective_qe"
    elif change == "collected_pair":
        sensor["payload"]["native_pixel_response"].update(
            schema="camera_native_pixel_response_v1",
            response_basis="independent_site_collected_electrons_per_incident_pixel_photon",
        )
    issues = _native_response_issues(lens, sensor)
    if change is None:
        assert issues == []
    else:
        assert len(issues) == 1 and issues[0]["code"] == "native_response_mismatch"


def test_legacy_model_records_do_not_acquire_new_evidence_requirements(api, monkeypatch):
    service, _, base, _ = api
    monkeypatch.setattr(service.registry, "blob_path",
                        lambda *args: pytest.fail("Legacy model read collection evidence"))
    assert verify_models(service, [base]) == []
