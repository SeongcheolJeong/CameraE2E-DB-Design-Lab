"""Native conversion preserves supplied physics and verifies its source evidence.

Synthetic declarations establish calculation/provenance behavior, not physical
sensor accuracy. No template GET may manufacture new acquisition evidence.
"""

from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_native_pixel_workflow import _pure_request

from camera_platform.api import create_app
from camera_platform.contracts import ModelCreate
from camera_platform.demo import measurement_template
from camera_platform.native_model_evidence import verify_models
from camera_platform.native_pixel_response import evaluate
from camera_platform.registry import canonical, digest
from camera_platform.runtime import ResponseDBRuntime


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "native-template-api", start_worker=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        bootstrap = client.get("/api/v1/bootstrap").json()
        client.headers["X-Platform-Token"] = bootstrap["token"]
        sensor = next(m for m in bootstrap["models"] if m["kind"] == "sensor")
        yield app.state.service, client, sensor


def _fields(record):
    return {key: deepcopy(value) for key, value in record.items()
            if key in ModelCreate.model_fields}


def _template(client, identity):
    return client.get("/api/v1/templates/native-pixel-model",
                      params={"base_sensor_model_id": identity})


def _create(client, payload):
    response = client.post("/api/v1/models", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_actual_http_template_then_registration_preserves_nodewise_and_interpolated_eqe(api):
    service, client, base = api
    before = service.registry.list("models")
    response = _template(client, base["id"])
    assert response.status_code == 200, response.text
    assert service.registry.list("models") == before
    template = response.json()
    native = template["payload"]["native_pixel_response"]
    values = np.asarray(native["values"])
    # Independent scalar expectation on the actual wavelength nodes: the legacy
    # runtime also requires exact wavelength coordinates. Theta is interpolated.
    legacy = base["payload"]
    for wave_index, wavelength in enumerate(legacy["wavelength_nm"]):
        for site, channel in enumerate((0, 1, 1, 2)):
            expected_nodes = (np.asarray(legacy["sensor_angular_response"]
                                        ["relative_response"][channel][wave_index])
                              * legacy["qe"][channel][wave_index])
            for phi_index in range(4):
                np.testing.assert_array_equal(values[wave_index, :, phi_index, site],
                                              expected_nodes)
            expected = np.interp([0, 7.5, 22.5, 30], native["theta_deg"], expected_nodes)
            actual = evaluate(native, wavelength, [0, 7.5, 22.5, 30], [-45, 27, 361, 900])
            np.testing.assert_allclose(actual[:, site], expected, rtol=2e-15, atol=0)
    np.testing.assert_array_equal(values[..., 1], values[..., 2])
    assert template["parents"] == [base["id"]]
    assert template["source_kind"] == native["source"]["kind"] == "estimated"
    assert "No new site/azimuth evidence" in native["source"]["description"]
    record = _create(client, template)
    assert record["payload"] == template["payload"]
    assert record["approval_status"] == "draft"
    assert record["factory"]["physical_validation"] == "not_established"
    descriptor, = verify_models(service, [record])
    assert descriptor["sha256"] == digest(native)
    actual_bytes = service.registry.blob_path(descriptor["sha256"]).read_bytes()
    assert actual_bytes == canonical(native).encode()
    assert not service.registry.list_runs()
    download = client.get(descriptor["url"])
    assert download.status_code == 200, download.text
    assert download.content == actual_bytes
    service.registry.blob_path(descriptor["sha256"]).write_bytes(b"corrupt native response")
    assert client.get(descriptor["url"]).status_code == 409
    assert service.registry.get("models", base["id"]) == base


def test_scientific_metadata_and_calibration_survive_without_claiming_measurement(api):
    service, client, base = api
    measurement = measurement_template(service)
    measurement.update(source_kind="simulated", dataset_ids=["synthetic-calibration-reference"])
    service.create_measurement(measurement)
    declared = _fields(base)
    declared.update(name="Supplied sensor provenance", parents=[base["id"]],
                    source_kind="measured", physical_instance="synthetic-test-declaration-only",
                    source_uri="fixture://declared-sensor-provenance",
                    method_and_version="declared-source-method:7",
                    calibration_dataset_ids=measurement["dataset_ids"])
    parent = _create(client, declared)
    response = _template(client, parent["id"])
    assert response.status_code == 200, response.text
    template = response.json()
    for key in ("asset_id", "kind", "license", "valid_domain", "spectral_range", "properties",
                "reference_plane", "included_effects", "raw_format", "calibration_dataset_ids"):
        assert template[key] == parent[key]
    assert template["physical_instance"] is None
    assert template["source_uri"] == "derived-model://" + parent["id"]
    assert template["parents"] == [parent["id"]]
    assert template["source_kind"] == "estimated"
    assert template["uncertainty"] == {"kind": "unknown"}
    assert template["parameter_uncertainty"] == {}
    created = _create(client, template)
    assert created["calibration_dataset_ids"] == measurement["dataset_ids"]
    assert service.registry.get("models", parent["id"]) == parent


def test_native_copy_preserves_anisotropy_and_distinct_green_sites_with_new_source_identity(api):
    service, client, base = api
    template = _template(client, base["id"]).json()
    native = template["payload"]["native_pixel_response"]
    values = np.asarray(native["values"])
    values[:, 1:, 1, 1] *= .7
    native["values"] = values.tolist()
    parent = _create(client, template)
    before = service.registry.list("models")
    copied = _template(client, parent["id"])
    assert copied.status_code == 200, copied.text
    assert service.registry.list("models") == before
    copy_response = copied.json()["payload"]["native_pixel_response"]
    assert {k: v for k, v in copy_response.items() if k != "source"} == {
        k: v for k, v in native.items() if k != "source"}
    assert parent["id"] in copy_response["source"]["description"]
    assert copy_response["source"]["kind"] == "estimated"
    child = _create(client, copied.json())
    assert child["parents"] == [parent["id"]]
    assert verify_models(service, [parent, child])


@pytest.mark.parametrize("damage,status", [("missing", 404), ("corrupt", 409)])
def test_template_cannot_copy_native_parent_with_unavailable_original_response(api, damage, status):
    service, client, base = api
    parent = _create(client, _template(client, base["id"]).json())
    before = service.registry.list("models")
    descriptor = parent["response_artifacts"]["native_pixel_response"]
    path = service.registry.blob_path(descriptor["sha256"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupted native source evidence")
    response = _template(client, parent["id"])
    assert response.status_code == status, response.text
    assert service.registry.list("models") == before
    if damage == "missing":
        assert not path.exists()
    else:
        assert path.read_bytes() == b"corrupted native source evidence"


@pytest.mark.parametrize("mismatch", ["spectral", "angle", "plane", "geometry", "raw"])
def test_inherited_conflicts_are_refused_during_get_without_inventing_coverage(api, mismatch):
    service, client, base = api
    declared = _fields(base)
    declared["name"] += " inconsistent inherited declaration"
    if mismatch == "spectral":
        declared["spectral_range"]["max"] = 700
    elif mismatch == "angle":
        declared["properties"]["accepted_angle_range"]["max"] = 40
    elif mismatch == "plane":
        declared["reference_plane"] = "silicon_after_microlens"
    elif mismatch == "geometry":
        declared["properties"]["active_width"]["value"] = 5.761
    else:
        declared["raw_format"]["black_level"] = 100
    parent = _create(client, declared)  # Legacy registration allows these declarations.
    before = service.registry.list("models")
    response = _template(client, parent["id"])
    assert response.status_code == 422, response.text
    assert service.registry.list("models") == before
    assert service.registry.get("models", parent["id"]) == parent


@pytest.mark.parametrize("kind", ["lens", "isp"])
def test_explicit_sensor_parent_is_required(api, kind):
    service, client, _ = api
    before = service.registry.list("models")
    parent = next(m for m in before if m["kind"] == kind)
    response = _template(client, parent["id"])
    assert response.status_code == 422
    assert service.registry.list("models") == before


def test_native_response_resource_estimate_covers_published_arrays_and_grows_with_capture():
    spatial = _pure_request()
    request = {k: deepcopy(spatial[k]) for k in (
        "wavelength_nm", "native_pixel_response", "pixel_pitch_um", "readout", "lens",
        "exposure_s", "noise_enabled")}
    request.update(spectral_irradiance_w_m2_nm=[1e-4, 1e-4], output_size=[8, 8], frames=1,
                   full_sensor_shape=[17, 17], crop_origin=[0, 0])
    request["lens"]["field_reference_radius_mm"] = .1
    directional = request["lens"]["directional_response"]
    directional.update(field_reference_radius_mm=.1, image_height_mm=[0, .1])
    runtime = ResponseDBRuntime()
    small = runtime.estimate_resources(request)
    actual = runtime.execute(request)
    published_bytes = sum(a.nbytes for a in actual["arrays"].values())
    assert small["estimate_kind"] == "array_budget_not_measured_peak"
    assert small["working_array_bytes_estimate"] > 64 * 1024 * 1024 + published_bytes
    assert small["native_direction_weighted_samples"] > 0
    larger = runtime.estimate_resources({**request, "output_size": [16, 16], "frames": 2})
    assert larger["working_array_bytes_estimate"] > small["working_array_bytes_estimate"]
    assert (larger["native_direction_weighted_samples"]
            == 4 * small["native_direction_weighted_samples"])
