"""Candidate model applicability is explicit, read-only and conservative."""

from copy import deepcopy

import pytest
from test_composer import assembly_fixture
from test_hdr_workflow import workflow as hdr_workflow
from test_retained_charge_workflow import workflow

from camera_platform.condition_space import REQUEST_SCHEMA, analyze_records, validate_request


def request(assembly_id, values):
    return {"schema": REQUEST_SCHEMA, "assembly_id": assembly_id,
            "candidates": {"gain": values}}


def test_saved_model_gain_domain_is_reported_without_changing_it(workflow):
    service, sdk, assembly, *_ = workflow
    before = service.registry.audit()
    response = sdk.http.post("/api/v1/assemblies/condition-space",
                             json=request(assembly["id"], [0.5, 1.0, 1.5]))
    assert response.status_code == 200, response.text
    result = response.json()
    gain = result["axes"]["gain"]
    assert gain["space_status"] == "covered"
    assert gain["intersection"] == {"min": 1.0, "max": 1.0, "unit": "1"}
    assert [point["status"] for point in gain["candidates"]] == [
        "blocked", "compatible", "blocked",
    ]
    assert all(len(point["blocking_models"]) == 3 for point in
               (gain["candidates"][0], gain["candidates"][2]))
    assert result["physical_validation"] == "not_established"
    assert result["execution_feasibility"] == "not_established_by_model_domain_check"
    assert result["automatic_model_revision"] is False
    assert service.registry.audit() == before


def test_missing_axis_is_unknown_even_when_composer_does_not_gate_it():
    assembly, models = assembly_fixture()
    result = analyze_records(assembly, models, request(assembly["id"], [0.5]))
    gain = result["axes"]["gain"]
    assert gain["space_status"] == "unknown_missing_declaration"
    assert gain["intersection"] is None
    assert gain["candidates"][0]["status"] == "unknown"
    assert len(gain["candidates"][0]["blocking_models"]) == 3


def test_disjoint_or_mixed_unit_model_domains_never_appear_feasible():
    assembly, models = assembly_fixture()
    assembly["conditions"]["gain"] = {"value": 1.0, "unit": "1"}
    for model, bounds in zip(models, [(0.4, 0.8), (1.0, 2.0), (0.5, 2.0)], strict=True):
        model["valid_domain"]["gain"] = {"min": bounds[0], "max": bounds[1], "unit": "1"}
    result = analyze_records(assembly, models, request(assembly["id"], [0.5, 1.0]))
    gain = result["axes"]["gain"]
    assert gain["space_status"] == "empty_intersection"
    assert gain["intersection"] is None
    assert [point["status"] for point in gain["candidates"]] == ["blocked", "blocked"]
    assert gain["candidates"][0]["blocking_models"] == ["sensor:1"]
    assert gain["candidates"][1]["blocking_models"] == ["lens:1"]
    changed = deepcopy(models)
    changed[1]["valid_domain"]["gain"]["unit"] = "dB"
    result = analyze_records(assembly, changed, request(assembly["id"], [1.0]))
    assert result["axes"]["gain"]["space_status"] == "unit_mismatch"
    assert result["axes"]["gain"]["intersection"] is None
    assert result["axes"]["gain"]["candidates"][0]["status"] == "unknown"


@pytest.mark.parametrize("values", [[True], [float("nan")], [1.0, 1.0], list(range(33))])
def test_invalid_candidate_set_is_rejected(values):
    with pytest.raises(ValueError):
        validate_request(request("assemblie-test", values))
