"""Immutable input boundaries through model/test registration, execution and evidence."""

from copy import deepcopy

import pytest

from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.contracts import TestCreate as SpecificationContract
from camera_platform.demo import seed
from camera_platform.service import PlatformService
from camera_platform.uncertainty import unsupported_budget
from camera_platform.validation import _uncertainty


def definition(value, unit):
    return {
        "kind": "absolute_bound",
        "unit": unit,
        "value": value,
        "source_kind": "estimated",
        "source_uri": "fixture://input-bounds",
        "method_and_version": "authored-bounds/1",
    }


@pytest.fixture
def setup(tmp_path):
    service = PlatformService(tmp_path)
    seed(service)
    assembly = service.registry.list("assemblies")[0]
    models = [service.registry.get("models", i) for i in assembly["model_ids"]]
    sensor = next(m for m in models if m["kind"] == "sensor")
    test = service.registry.list("tests")[0]
    return service, assembly, sensor, test


def fields(record, contract):
    return {k: deepcopy(v) for k, v in record.items() if k in contract.model_fields}


def create_bounded(setup):
    service, assembly, sensor, test = setup
    model = fields(sensor, ModelCreate)
    model.update(revision="bounded", parents=[sensor["id"]])
    model["parameter_uncertainty"] = {"qe": definition(0.005, "electron/photon")}
    new = service.create_model(model)
    assembly = fields(assembly, AssemblyCreate)
    assembly["revision"] = "bounded"
    assembly["model_ids"] = [new["id"] if i == sensor["id"] else i for i in assembly["model_ids"]]
    return service.create_assembly(assembly), new, test


def test_uncertainty_is_frozen_in_run_and_changes_cache_identity(setup):
    service, original, sensor, test = setup
    assembly, new, test = create_bounded(setup)
    queued = service.submit_run({"assembly_id": assembly["id"], "test_id": test["id"]})
    source = queued["request"]["parameter_uncertainty"]["qe"]
    assert (
        source["source_record_id"] == new["id"]
        and source["source_content_hash"] == new["content_hash"]
    )
    assert service.registry.get("models", sensor["id"])["parameter_uncertainty"] == {}
    result = service.execute_run(queued)
    budget = result["prediction"]["uncertainty_budget"]
    assert budget["propagated_paths"] == ["qe"] and budget["total_uncertainty"] == {
        "kind": "unknown"
    }
    assert budget["axes"] == result["prediction"]["axes"]
    assert (
        service.submit_run({"assembly_id": assembly["id"], "test_id": test["id"]})["id"]
        == queued["id"]
    )
    baseline = service.submit_run({"assembly_id": original["id"], "test_id": test["id"]})
    assert baseline["id"] != queued["id"]
    numerical = service.execute_run(baseline)
    a = next(a for a in result["artifacts"] if a["name"] == "raw_frames.npy")
    b = next(a for a in numerical["artifacts"] if a["name"] == "raw_frames.npy")
    assert a["sha256"] == b["sha256"]


@pytest.mark.parametrize(
    "path,unit,value",
    [
        ("qe", "percent", 0.01),
        ("qe", "electron/photon", -0.01),
        ("qe", "electron/photon", 0.4),
        ("pixel_pitch_um", "um", 10),
        ("readout.black_level_dn", "DN", 4095),
        ("readout.bit_depth", "bit", 1),
        ("qe", "electron/photon", [[0.01, 0.02]]),
        ("sensor_angular_response.relative_response", "1", 0.001),
    ],
)
def test_invalid_model_bound_is_refused_before_registry_write(setup, path, unit, value):
    service, _, sensor, _ = setup
    payload = fields(sensor, ModelCreate)
    payload["parameter_uncertainty"] = {path: definition(value, unit)}
    count = len(service.registry.list("models"))
    with pytest.raises((ValueError, KeyError)):
        service.create_model(payload)
    assert len(service.registry.list("models")) == count


def test_operating_domain_and_sweep_cannot_relabel_existing_bound(setup):
    service, assembly, _, test = setup
    payload = fields(test, SpecificationContract)
    payload["revision"] = "exposure-bound"
    payload["parameter_uncertainty"] = {"conditions.exposure_s.value": definition(0.0001, "s")}
    new = service.create_test(payload)
    service.submit_run({"assembly_id": assembly["id"], "test_id": new["id"]})
    with pytest.raises(ValueError, match="new test revision"):
        service.submit_run(
            {"assembly_id": assembly["id"], "test_id": new["id"], "parameters": {"exposure_ms": 8}}
        )
    payload["revision"] = "outside"
    payload["parameter_uncertainty"]["conditions.exposure_s.value"]["value"] = 0.00395
    outside = service.create_test(payload)
    with pytest.raises(ValueError, match="OUT_OF_DOMAIN"):
        service.submit_run({"assembly_id": assembly["id"], "test_id": outside["id"]})


def test_partial_budget_cannot_be_promoted_to_total_by_relabeling(setup):
    service, _, _, _ = setup
    assembly, _, test = create_bounded(setup)
    result = service.execute_run(
        service.submit_run({"assembly_id": assembly["id"], "test_id": test["id"]})
    )
    prediction = result["prediction"]
    prediction["uncertainty"] = {"kind": "absolute_bound", "value": 0.0, "unit": "1"}
    value, issues = _uncertainty(
        prediction,
        len(prediction["values"]),
        (len(prediction["values"]),),
        "prediction.uncertainty",
    )
    assert value is None and issues[0]["code"] == "incomplete_uncertainty_budget"


def test_unsupported_runtime_preserves_supplied_bounds_as_not_propagated(setup):
    service, _, _, _ = setup
    assembly, _, test = create_bounded(setup)
    models = [service.registry.get("models", i) for i in assembly["model_ids"]]
    for runtime in ["spatial_scene_v1", "temporal_scene_v1"]:
        budget = unsupported_budget(models, test, runtime)
        assert budget["status"] == "not_propagated" and "qe" in budget["sources"]
        assert budget["total_uncertainty"] == {"kind": "unknown"}
