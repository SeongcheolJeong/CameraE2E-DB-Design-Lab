"""Native model registration, immutable response blobs and actual Factory authority."""

from copy import deepcopy

import numpy as np
import pytest
from test_native_pixel_response import response

from camera_platform import factory, lens_factory, pupil_transmission
from camera_platform.composer import check_assembly
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.contracts import TestCreate as FixedTestContract
from camera_platform.demo import seed
from camera_platform.native_model_evidence import verify_models
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.service import PlatformService


def fields(record, contract):
    return {k: deepcopy(v) for k, v in record.items() if k in contract.model_fields}


@pytest.fixture
def platform(tmp_path):
    service = PlatformService(tmp_path / "native-evidence")
    seed(service)
    originals = service.registry.list("models")
    base = next(m for m in originals if m["kind"] == "sensor")
    data = fields(base, ModelCreate)
    pixel = response()
    data.update(
        name="Four native sites",
        revision="native",
        parents=[base["id"]],
        spectral_range={"min": 500, "max": 600, "unit": "nm"},
    )
    data["payload"] = {
        "wavelength_nm": pixel["wavelength_nm"],
        "pixel_pitch_um": pixel["pixel_pitch_um"],
        "readout": deepcopy(base["payload"]["readout"]),
        "native_pixel_response": pixel,
    }
    data["properties"].update(
        active_height={"value": 0.064, "unit": "mm"},
        active_width={"value": 0.064, "unit": "mm"},
        accepted_angle_range={"min": 0, "max": 40, "unit": "deg"},
    )
    return service, data, originals


def test_native_registration_creates_exact_canonical_response_blob_and_restart_proof(platform):
    service, data, _ = platform
    original = deepcopy(data)
    record = service.create_model(data)
    assert data == original
    descriptors = verify_models(service, [record])
    assert descriptors == [record["response_artifacts"]["native_pixel_response"]]
    descriptor = descriptors[0]
    expected = canonical(record["payload"]["native_pixel_response"]).encode()
    assert service.registry.blob_path(descriptor["sha256"]).read_bytes() == expected
    assert descriptor["sha256"] == digest(record["payload"]["native_pixel_response"])
    assert descriptor["bytes"] == len(expected)
    reopened = PlatformService(service.registry.directory)
    assert verify_models(reopened, [record]) == descriptors
    assert record["approval_status"] == "draft"
    assert record["factory"]["physical_validation"] == "not_established"


@pytest.mark.parametrize(
    "change",
    [
        "wave",
        "pitch",
        "range",
        "angle",
        "plane",
        "effects",
        "duplicate",
        "noninteger",
        "raw",
        "bool_angle",
        "readout",
        "huge_geometry",
    ],
)
def test_native_registration_refuses_metadata_that_disagrees_with_actual_response(platform, change):
    service, data, originals = platform
    if change == "wave":
        data["payload"]["wavelength_nm"] = [500, 551, 600]
    elif change == "pitch":
        data["payload"]["pixel_pitch_um"] = 3
    elif change == "range":
        data["spectral_range"]["max"] = 700
    elif change == "angle":
        data["properties"]["accepted_angle_range"]["max"] = 60
    elif change == "plane":
        data["reference_plane"] = "silicon_after_microlens"
    elif change == "effects":
        data["included_effects"].append("microlens_extra_gain")
    elif change == "duplicate":
        data["payload"]["qe"] = [[0.1] * 3] * 3
    elif change == "noninteger":
        data["properties"]["active_width"]["value"] = 0.063
    elif change == "raw":
        data["raw_format"]["black_level"] = 65
    elif change == "readout":
        data["payload"]["readout"] = None
    elif change == "huge_geometry":
        data["properties"]["active_width"]["value"] = 10**1000
    else:
        data["properties"]["accepted_angle_range"]["min"] = False
    with pytest.raises(ValueError):
        service.create_model(data)
    assert service.registry.list("models") == originals


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_native_response_missing_or_corrupt_blob_refused_without_rewriting_record(platform, damage):
    service, data, _ = platform
    record = service.create_model(data)
    descriptor = record["response_artifacts"]["native_pixel_response"]
    path = service.registry.blob_path(descriptor["sha256"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt response fixture")
    with pytest.raises((KeyError, IntegrityError)):
        verify_models(service, [record])
    assert service.registry.get("models", record["id"]) == record


@pytest.mark.parametrize("damage", ["field", "descriptor", "missing_descriptor"])
def test_valid_registry_record_cannot_bind_another_response_to_old_artifact(platform, damage):
    service, data, _ = platform
    original = service.create_model(data)
    altered = {
        k: deepcopy(v) for k, v in original.items() if k not in {"id", "content_hash", "created_at"}
    }
    altered["name"] += " malformed fixture"
    if damage == "field":
        altered["payload"]["native_pixel_response"]["source"]["description"] = "changed provenance"
    elif damage == "descriptor":
        altered["response_artifacts"]["native_pixel_response"]["name"] = "different.json"
    else:
        altered.pop("response_artifacts")
    stored = service.registry.put("models", altered)
    with pytest.raises(ValueError, match="artifact"):
        verify_models(service, [stored])
    assert verify_models(service, [original])


def test_frozen_model_modification_is_rejected_and_legacy_has_no_new_blob_requirement(
    platform, monkeypatch
):
    service, data, originals = platform
    native = service.create_model(data)
    altered = deepcopy(native)
    altered["payload"]["native_pixel_response"]["values"][0][1][0][0] += 0.001
    with pytest.raises(ValueError, match="frozen registry"):
        verify_models(service, [altered])
    monkeypatch.setattr(
        service.registry, "blob_path", lambda *a: pytest.fail("Legacy checked new blob")
    )
    assert verify_models(service, originals) == []


def test_directional_lens_cannot_enter_by_response_table_import(platform):
    service, _, originals = platform
    lens = fields(next(m for m in originals if m["kind"] == "lens"), ModelCreate)
    lens["payload"]["directional_response"] = {"claim": "unproven authored directions"}
    with pytest.raises(ValueError, match="Factory provenance"):
        service.create_model(lens)


def joint_fixture():
    # A literal declared-contract fixture, not a Factory/physical verification.
    angles = np.deg2rad([10, 20])
    return {
        "schema": "camera_lens_directional_response_v1",
        "wavelength_nm": [500, 550, 600],
        "field_radius": [0, 1],
        "image_height_mm": [0, 0.1],
        "field_reference_radius_mm": 0.1,
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "reference_plane": "sensor_surface_before_pixel_optics",
        "field_reference": "positive_x_column",
        "symmetry": "centered_spherical_or_plane_prescription_radial_stop",
        "normalization": "conditional_transmitted_irradiance_fraction",
        "prescription_sha256": "1" * 64,
        "stop_transmission_sha256": "2" * 64,
        "directions_xyz": np.stack(
            [np.sin(angles) * [-1, 1], np.zeros(2), np.cos(angles)], axis=-1
        )[:, None].tolist(),
        "conditional_weights": [[[1], [1]]] * 3,
        "on_axis_rotation_phi_deg": list(range(0, 360, 45)),
    }


def compatible_models(platform):
    service, data, originals = platform
    sensor = service.create_model(data)
    assembly = deepcopy(service.registry.list("assemblies")[0])
    models = [deepcopy(service.registry.get("models", i)) for i in assembly["model_ids"]]
    lens = next(m for m in models if m["kind"] == "lens")
    joint = joint_fixture()
    lens["spectral_range"] = data["spectral_range"]
    lens["payload"].update(
        wavelength_nm=joint["wavelength_nm"],
        field_radius=joint["field_radius"],
        field_reference_radius_mm=0.1,
        directional_response=joint,
    )
    models = [sensor if m["kind"] == "sensor" else m for m in models]
    assembly.update(model_ids=[m["id"] for m in models], spectral_range=data["spectral_range"])
    return assembly, models


@pytest.mark.parametrize("change", [None, "legacy", "wave", "pitch", "theta", "plane"])
def test_composer_requires_joint_direction_and_native_response_correspondence(platform, change):
    assembly, models = compatible_models(platform)
    lens, sensor = [next(m for m in models if m["kind"] == kind) for kind in ("lens", "sensor")]
    if change == "legacy":
        lens["payload"].pop("directional_response")
    elif change == "wave":
        lens["payload"]["wavelength_nm"] = [500, 551, 600]
    elif change == "pitch":
        sensor["payload"]["pixel_pitch_um"] = 3
    elif change == "theta":
        sensor["payload"]["native_pixel_response"]["theta_deg"] = [0, 5, 9]
    elif change == "plane":
        lens["payload"]["directional_response"]["reference_plane"] = "silicon"
    report = check_assembly(assembly, models)
    if change is None:
        assert report["status"] == "PASS", report["issues"]
    else:
        assert report["status"] == "FAIL"
        assert any(i["code"] == "native_response_mismatch" for i in report["issues"])


def finish(service, queued):
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    return service.registry.finish(job["id"], result=service.execute_run(job))


@pytest.fixture
def actual(platform):
    service, sensor_data, _ = platform
    original = service.registry.list("assemblies")[0]
    sensor = service.create_model(sensor_data)
    request = lens_factory.request_template(version=3)
    request.update(wavelength_nm=[500, 550, 600], field_radius=[0, 1], image_height_mm=[0, 0.1])
    stop = request["surfaces"][0]
    stop.update(thickness_mm=100, semi_diameter_mm=2, refractive_index_after=[1] * 3)
    request["surfaces"] = [stop]
    request["stop_transmission"] = pupil_transmission.uniform_transmission(request)
    request["sampling"].update(polar_rings=8, azimuth_samples=8, on_axis_azimuth_samples=8)
    job_request = factory.template(service, "rayoptics_lens_v1", request_version=3)
    job_request.update(name="Actual tiny joint-direction characterization", request=request)
    ray_job = finish(service, factory.submit(service, job_request))
    assert ray_job["result"]["summary"]["status"] == "PROPOSED"
    lens = factory.publish(
        service,
        {
            "job_id": ray_job["id"],
            "name": "Joint ray lens",
            "revision": "1",
            "conditions": original["conditions"],
        },
    )
    isp = next(
        service.registry.get("models", i)
        for i in original["model_ids"]
        if service.registry.get("models", i)["kind"] == "isp"
    )
    assembly_data = fields(original, AssemblyCreate)
    assembly_data.update(
        model_ids=[lens["id"], sensor["id"], isp["id"]],
        spectral_range=sensor_data["spectral_range"],
        name="Native four-site camera",
    )
    assembly = service.create_assembly(assembly_data)
    assert assembly["compatibility"]["status"] == "PASS", assembly["compatibility"]
    test_data = fields(service.registry.list("tests")[0], FixedTestContract)
    test_data["stimulus"]["wavelength_nm"] = [500, 550, 600]
    test_data["stimulus"]["spectral_irradiance_w_m2_nm"] = [0.0001] * 3
    test = service.create_test(test_data)
    payload = {
        "assembly_id": assembly["id"],
        "test_id": test["id"],
        "plugin_id": "response_db_v1",
        "parameters": {"output_size": [8, 8], "frames": 1},
    }
    return service, sensor, lens, assembly, test, payload


def test_actual_factory_publication_binds_joint_blob_and_native_run_request(actual):
    service, sensor, lens, _, _, payload = actual
    artifacts = verify_models(service, [lens, sensor])
    assert len(artifacts) == 2
    queued = service.submit_run(payload)
    assert queued["input_artifacts"] == artifacts
    assert queued["request"]["full_sensor_shape"] == [32, 32]
    assert queued["request"]["crop_origin"] == [12, 12]
    assert queued["request"]["native_pixel_response"] == sensor["payload"]["native_pixel_response"]
    assert "qe" not in queued["request"] and "sensor_angular_response" not in queued["request"]
    run = finish(service, queued)
    raw_artifact = next(a for a in run["artifacts"] if a["name"] == "raw_frames.npy")
    raw = np.load(service.registry.blob_path(raw_artifact["sha256"]), allow_pickle=False)
    assert raw.shape == (1, 8, 8) and raw.dtype.kind in "iu"
    assert raw[0, 0::2, 1::2].mean() != raw[0, 1::2, 0::2].mean()
    assert all(artifact in run["artifacts"] for artifact in artifacts)


@pytest.mark.parametrize("field", ["native_pixel_response", "directional_response"])
@pytest.mark.parametrize("stage", ["submit", "cached", "worker"])
def test_response_blob_corruption_rechecked_at_submission_cache_and_worker(actual, field, stage):
    service, sensor, lens, _, _, payload = actual
    queued = service.submit_run(payload) if stage != "submit" else None
    if stage == "cached":
        original = finish(service, queued)
    model = sensor if field == "native_pixel_response" else lens
    descriptor = model["response_artifacts"][field]
    service.registry.blob_path(descriptor["sha256"]).write_bytes(b"corrupt test fixture")
    with pytest.raises(IntegrityError):
        if stage == "worker":
            service.execute_run(service.registry.claim())
        else:
            service.submit_run(payload)
    if stage == "cached":
        assert service.registry.get_run(original["id"]) == original


def test_worker_refuses_request_not_matching_frozen_native_model(actual):
    service, _, _, _, _, payload = actual
    service.submit_run(payload)
    job = service.registry.claim()
    job["request"]["native_pixel_response"]["source"]["description"] = "different response"
    with pytest.raises(ValueError, match="frozen|Frozen"):
        service.execute_run(job)


def test_native_quick_uses_exact_model_blob_and_declares_normal_only_expectation(actual):
    service, _, _, _, test, payload = actual
    data = fields(test, FixedTestContract)
    data.update(
        name="Native normal-only quick",
        revision="quick",
        included_effects=["sensor_effective_qe", "mean_dark_charge", "readout_clipping"],
    )
    quick = service.create_test(data)
    payload.update(plugin_id="quick_estimate", test_id=quick["id"], parameters={})
    run = finish(service, service.submit_run(payload))
    assert run["result"]["summary"]["channels"] == ["R", "Gr", "Gb", "B"]
    assert run["result"]["summary"]["native_pixel_response"]["angular_condition"] == (
        "normal_incidence_only_quick_estimate"
    )
    assert not any(a["name"] == "raw_frames.npy" for a in run["artifacts"])
    signal = next(a for a in run["artifacts"] if a["name"] == "field_channel_electrons.npy")
    assert signal["unit"] == "electron/pixel"
    assert signal["spatial_sampling"] == "native_site_normal_incidence_point"
