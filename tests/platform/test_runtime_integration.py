"""Exercise immutable model translation through actual RAW/ISP and artifact storage."""

import copy
import json

import numpy as np
import pytest

from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.contracts import TestCreate as SpecificationContract
from camera_platform.demo import seed
from camera_platform.runtime import run_plugin
from camera_platform.service import PlatformService, PreflightError


@pytest.fixture
def platform_service(tmp_path):
    service = PlatformService(tmp_path / "runtime-integration")
    seed(service)
    return service


def _payload(record, contract):
    return {key: copy.deepcopy(record[key]) for key in contract.model_fields if key in record}


def _request(service, **parameters):
    assembly = next(
        item for item in service.registry.list("assemblies") if "Lens A" in item["name"]
    )
    return {
        "assembly_id": assembly["id"],
        "test_id": service.registry.list("tests")[0]["id"],
        "parameters": {"output_size": [32, 32], "frames": 4, "seed": 321, **parameters},
    }


def _execute(service, request):
    queued = service.submit_run(request)
    job = service.registry.claim()
    assert job is not None and job["id"] == queued["id"]
    result = service.execute_run(job)
    return service.registry.finish(job["id"], result=result)


def _artifact(service, run, name):
    artifact = next(item for item in run["artifacts"] if item["name"] == name)
    path = service.registry.blob_path(artifact["sha256"])
    return np.load(path, allow_pickle=False)


def test_seed_models_to_real_runtime_and_persisted_evidence(platform_service):
    service = platform_service
    run = _execute(service, _request(service))
    assert run["status"] == "succeeded"
    assert len(run["model_snapshots"]) == 3
    assert all(model["source_kind"] == "estimated" for model in run["model_snapshots"])
    assert not service.registry.list("measurements")
    sensor = next(model for model in run["model_snapshots"] if model["kind"] == "sensor")
    assert run["request"]["readout"] == sensor["payload"]["readout"]
    assert run["request"]["qe"] == sensor["payload"]["qe"]
    assert run["request"]["irradiance_reference_plane"] == "sensor_surface_on_axis"
    temperature = run["test_snapshot"]["conditions"]["temperature_c"]["value"]
    assert run["request"]["temperature_c"] == temperature
    raw = _artifact(service, run, "raw_frames.npy")
    rgb = _artifact(service, run, "rgb.npy")
    assert raw.shape == (4, 32, 32) and np.issubdtype(raw.dtype, np.integer)
    assert 0 <= raw.min() < raw.max() <= 4095
    assert np.max(rgb) > 0 and rgb.shape == (32, 32, 3)
    replay = run_plugin(run["plugin_id"], run["request"])
    np.testing.assert_array_equal(raw, replay["arrays"]["raw_frames"])
    np.testing.assert_array_equal(rgb, replay["arrays"]["rgb"])
    prediction = run["result"]["prediction"]
    assert prediction["conditions"] == run["test_snapshot"]["conditions"]
    assert prediction["uncertainty"] == {"kind": "unknown"}
    assert set(prediction["included_effects"]) == set(run["test_snapshot"]["included_effects"])
    assert run["result"]["verification"]["physical_validation"] == "not_established"
    assert service.registry.audit()["integrity"] == "verified"
    json.dumps(service.export_run(run["id"]), allow_nan=False)


def test_exposure_sweep_keeps_original_test_and_changes_actual_capture(platform_service):
    service = platform_service
    request = _request(service)
    original = service.registry.get("tests", request["test_id"])
    first = _execute(service, request)
    second = _execute(service, _request(service, exposure_ms=8.0))
    assert service.registry.get("tests", request["test_id"]) == original
    assert second["test_snapshot"]["conditions"]["exposure_s"] == {"value": 0.008, "unit": "s"}
    assert second["test_snapshot"]["derived_from_test"] == original["id"]
    assert first["cache_key"] != second["cache_key"]
    np.testing.assert_allclose(
        _artifact(service, second, "mean_electrons.npy"),
        2 * _artifact(service, first, "mean_electrons.npy"),
    )
    assert (
        _artifact(service, second, "raw_frames.npy").mean()
        > _artifact(service, first, "raw_frames.npy").mean()
    )
    assert (
        _artifact(service, second, "rgb.npy").mean() > _artifact(service, first, "rgb.npy").mean()
    )


def test_quick_estimate_submission_has_no_fabricated_frame_artifacts(platform_service):
    service = platform_service
    request = {**_request(service), "plugin_id": "quick_estimate"}
    original = service.registry.get("tests", request["test_id"])
    test_payload = _payload(original, SpecificationContract)
    test_payload.update(
        name="On-axis estimate",
        revision="quick-1",
        included_effects=["sensor_effective_qe", "mean_dark_charge", "readout_clipping"],
    )
    request["test_id"] = service.create_test(test_payload)["id"]
    run = _execute(service, request)
    assert run["plugin_id"] == "quick_estimate_v1"
    assert run["result"]["field_radius"] == [0.0]
    assert {item["name"] for item in run["artifacts"]} == {
        "field_channel_electrons.npy",
        "summary.json",
    }


def test_duplicate_physical_effect_is_refused_before_queue(platform_service):
    service = platform_service
    assembly = service.registry.get("assemblies", _request(service)["assembly_id"])
    models = [service.registry.get("models", identity) for identity in assembly["model_ids"]]
    lens = next(model for model in models if model["kind"] == "lens")
    sensor = next(model for model in models if model["kind"] == "sensor")
    candidate = _payload(lens, ModelCreate)
    candidate.update(
        name="Lens table already containing sensor response", revision="2", parents=[lens["id"]]
    )
    candidate["included_effects"].append(sensor["included_effects"][0])
    # This concrete adapter rejects already-composed lens/sensor responses at
    # import, before the generic Composer duplicate-effect guard is needed.
    with pytest.raises(ValueError, match="actual implemented effects"):
        service.create_model(candidate)
    assert service.registry.list_runs() == []


def test_declared_readout_format_cannot_override_actual_sampler_contract(platform_service):
    service = platform_service
    assembly = service.registry.get("assemblies", _request(service)["assembly_id"])
    sensor = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    candidate = _payload(sensor, ModelCreate)
    candidate.update(
        name="Readout payload and RAW contract disagree", revision="2", parents=[sensor["id"]]
    )
    candidate["payload"]["readout"]["black_level_dn"] = 32
    changed = service.create_model(candidate)
    draft = _payload(assembly, AssemblyCreate)
    draft.update(
        name="Mismatched black pedestal",
        model_ids=[
            changed["id"] if identity == sensor["id"] else identity
            for identity in assembly["model_ids"]
        ],
    )
    invalid = service.create_assembly(draft)
    with pytest.raises(ValueError, match="Readout payload disagrees"):
        service.submit_run({**_request(service), "assembly_id": invalid["id"]})
    assert service.registry.list_runs() == []


@pytest.mark.parametrize(
    "change,reason",
    [
        ("wavelength", "wavelength coordinates differ"),
        ("reference_plane", "sensor-surface spectral irradiance"),
        ("exposure_unit", "exposure_s must be specified in s"),
        ("irradiance_unit", "spectral_irradiance_unit must be W/m^2/nm"),
        ("wavelength_unit", "wavelength_unit must be nm"),
    ],
)
def test_stimulus_and_condition_contracts_are_not_silently_reinterpreted(
    platform_service, change, reason
):
    service = platform_service
    request = _request(service)
    test = _payload(service.registry.get("tests", request["test_id"]), SpecificationContract)
    test.update(name="Deliberately mismatched condition fixture", revision="2")
    if change == "wavelength":
        test["stimulus"]["wavelength_nm"][0] = 440
    elif change == "reference_plane":
        test["stimulus"]["reference_plane"] = "scene_radiance"
    elif change == "irradiance_unit":
        test["stimulus"]["spectral_irradiance_unit"] = "W/m^2/um"
    elif change == "wavelength_unit":
        test["stimulus"]["wavelength_unit"] = "um"
    else:
        test["conditions"]["exposure_s"]["unit"] = "ms"
    invalid = service.create_test(test)
    # Composer can refuse incompatible domain units before the runtime-specific guard.
    with pytest.raises(ValueError) as error:
        service.submit_run({**request, "test_id": invalid["id"]})
    assert reason in str(error.value) or isinstance(error.value, PreflightError)
    assert service.registry.list_runs() == []
