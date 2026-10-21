"""Evidence progression never promotes defaults, stale inputs or synthetic measurements."""

import copy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from camera_platform import design_asset_readiness as readiness
from camera_platform.electrical import default_electrical_model
from camera_platform.registry import ConflictError, SQLiteRegistry


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = tmp_path / "camerae2e_db/catalog.sqlite"
    db.parent.mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE spec_observations(observation_id,revision_id,"
            "operating_point_id,property_name,value_json,unit,origin_kind,source_id,"
            "conditions_json,missing_reason,component_id)"
        )
        connection.executemany(
            "INSERT INTO spec_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "observed",
                    "revision",
                    None,
                    "pixel_pitch_um",
                    "2.9",
                    "um",
                    "extracted",
                    "report",
                    "{}",
                    None,
                    "component",
                ),
                (
                    "assumed",
                    "readout-revision",
                    None,
                    "read_noise_e",
                    "10",
                    "electron",
                    "assumed",
                    "profile",
                    '{"temperature_c":25,"source_file":"/private/source.pdf"}',
                    "Not measured",
                    "component",
                ),
            ],
        )
    item = {
        "id": "sensor-1",
        "family": "sensor",
        "component_id": "component",
        "revision_id": "revision",
        "content_sha256": "a" * 64,
        "name": "IMX462",
        "source_label": "TechInsights DEF-2011-804",
        "report_code": "DEF-2011-804",
    }
    sources = {"component_catalog": {"status": "available", "sha256": "a" * 64}}
    view = {"items": [item], "snapshot_id": "snapshot"}
    service = SimpleNamespace(workspace=tmp_path, registry=SQLiteRegistry(tmp_path / "state"))
    electrical = default_electrical_model(
        {
            "full_well_e": 10000,
            "read_noise_e": 10,
            "dark_current_e_per_s": 10,
            "gain": 1,
            "bit_depth": 12,
            "black_level_dn": 64,
        }
    )
    cell = {
        "electrical_model": electrical,
        "field_classification": {
            "source_derived": {"pixel_pitch_um": {"value": 2.9}},
            "common_assumptions": {"readout": electrical},
        },
    }
    binding = {
        "run_id": "run-source",
        "candidate_id": None,
        "manifest_sha256": "b" * 64,
        "cell_sha256": "c" * 64,
        "source_report": "DEF-2011-804",
    }
    monkeypatch.setattr(readiness, "_index", lambda service: copy.deepcopy(view))
    monkeypatch.setattr(readiness, "_source_snapshot", lambda service: copy.deepcopy(sources))
    monkeypatch.setattr(
        readiness,
        "_reference_parameters",
        lambda service: {
            "status": "available",
            "values": {
                key: {"value": cell["electrical_model"][key], "origin_kind": "assumed"}
                for key in readiness.FIELDS
            },
        },
    )
    monkeypatch.setattr(
        readiness,
        "_reference",
        lambda service, selected, source_run_id=None: (
            copy.deepcopy(binding),
            copy.deepcopy(cell),
            {},
        ),
    )
    monkeypatch.setattr(
        readiness,
        "_applied",
        lambda service, selected: (
            {"values": {"pixel_pitch_um": 2.9, "read_noise_e": 10}, "cell": copy.deepcopy(cell)},
            None,
        ),
    )
    return service, item, sources, view, cell, binding, db


def payload(field="read_noise_e", value=3):
    return {
        "asset_id": "sensor-1",
        "name": "Readout experiment",
        "changes": [
            {
                "field": field,
                "value": value,
                "origin_kind": "user_assumption",
                "reason": "Compare a declared lower read-noise assumption",
            }
        ],
    }


def test_propose_adopt_idempotent_and_default_provenance_stays_assumed(setup):
    service, item, _, _, cell, _, db = setup
    original = hashlib.sha256(db.read_bytes()).hexdigest()
    proposal = readiness.propose(service, payload())
    assert readiness.propose(service, payload())["id"] == proposal["id"]
    assert service.registry.list(readiness.MODEL_KIND) == []
    model = readiness.adopt(service, proposal["id"])
    assert readiness.adopt(service, proposal["id"])["id"] == model["id"]
    assert model["parent_revision_id"] == item["revision_id"]
    assert model["changes"][0]["origin_kind"] == "user_assumption"
    assert model["applied_inputs"] == {"read_noise_e": 3, "dark_current_e_per_s": 10}
    assert model["baseline_electrical_model"] == cell["electrical_model"]
    assert hashlib.sha256(db.read_bytes()).hexdigest() == original
    assert not readiness.detail(service, item["id"])["models"][0]["latest_check"]


def test_source_and_applied_values_are_separate_and_private_paths_omitted(setup):
    service, item, *_ = setup
    detail = readiness.detail(service, item["id"])
    observed = next(p for p in detail["parameters"] if p["observation_id"] == "observed")
    assert observed["source_value"] == observed["applied_value"] == 2.9
    assumption = next(
        p
        for p in detail["parameters"]
        if p["source_value"] is None and p["field"] == "read_noise_e"
    )
    assert assumption["origin_kind"] == "assumed"
    assert assumption["applied_value"] == 10
    assert "/private/" not in json.dumps(detail)
    assert detail["readiness"]["measurement"]["status"] == "not_established"
    overview = readiness.overview(service)
    assert overview["counts"]["classified_observations"] == {"assumed": 1, "extracted": 1}
    assert overview["asset_statuses"][item["id"]]["execution"] == "adapter_supported"


def test_db_execution_separate_from_readout_adoption_and_measurement(setup, monkeypatch):
    from camera_platform import db_design_job, db_design_optics, db_design_sensor
    from camera_platform.registry import digest

    service, item, *_ = setup
    item["report_code"] = "DEF-2206-805"
    frozen = {"snapshot": "pinned"}
    choice = {
        "key": "imx662",
        "asset_id": item["id"],
        "revision_id": item["revision_id"],
        "content_sha256": item["content_sha256"],
        "execution_ready": True,
        "parameter_sources": [
            {"parameter": "read_noise_e", "value": 6.35, "classification": "derived_camera_proxy"}
        ],
    }
    monkeypatch.setattr(db_design_sensor, "candidates", lambda *args: [copy.deepcopy(choice)])
    monkeypatch.setattr(db_design_optics, "candidates", lambda *args: [])
    records = []
    monkeypatch.setattr(
        readiness.bindings, "bindings", lambda *args: {item["id"]: copy.deepcopy(records)}
    )
    monkeypatch.setattr(db_design_job, "snapshot", lambda *args: copy.deepcopy(frozen))
    status = {"status": "succeeded", "specification": {}}
    monkeypatch.setattr(service.registry, "get_run", lambda *args: copy.deepcopy(status))
    detail = readiness.detail(service, item["id"])
    assert detail["db_execution"]["status"] == "adapter_supported"
    assert detail["models"] == []
    assert "Separate from adopted" in detail["readiness"]["execution"]["scope"]
    records.append(
        {
            "kind": "db_design_study",
            "run_id": "run-db",
            "available": True,
            "binding": {"source_snapshot_sha256": digest(frozen)},
        }
    )
    detail = readiness.detail(service, item["id"])
    assert detail["readiness"]["execution"]["status"] == "succeeded"
    assert detail["readiness"]["measurement"]["status"] == "not_established"
    assert (
        detail["db_execution"]["parameter_sources"][0]["classification"] == "derived_camera_proxy"
    )
    frozen["snapshot"] = "changed"
    detail = readiness.detail(service, item["id"])
    assert detail["readiness"]["execution"]["status"] == "stale"
    assert detail["db_execution"]["results"][0]["status"] == "stale"
    assert not detail["db_execution"]["results"][0]["available"]
    status["status"] = "cancelled"
    assert readiness.detail(service, item["id"])["db_execution"]["status"] != "succeeded"


def test_db_adapter_does_not_downgrade_adopted_trial_or_hide_stale_inputs():
    item = {"id": "sensor-1", "report_code": "DEF-2011-804"}
    model = {
        "asset_id": item["id"],
        "trial_completed": True,
        "latest_check": {"execution_ready": True, "status": "passed"},
    }
    db = {item["id"]: {"status": "adapter_supported"}}
    assert (
        readiness._statuses([item], [model], [], db)[item["id"]]["execution"] == "trial_completed"
    )
    model["trial_completed"] = False
    assert readiness._statuses([item], [model], [], db)[item["id"]]["execution"] == "ready"
    model["latest_check"] = {"execution_ready": False, "status": "stale"}
    assert readiness._statuses([item], [model], [], db)[item["id"]]["execution"] == "stale"
    db[item["id"]]["status"] = "succeeded"
    assert readiness._statuses([item], [model], [], db)[item["id"]]["execution"] == "succeeded"


def test_adoption_refuses_stale_revision_or_reference(setup):
    service, item, _, _, _, binding, _ = setup
    proposal = readiness.propose(service, payload())
    item["content_sha256"] = "f" * 64
    with pytest.raises(ConflictError, match="Source changed"):
        readiness.adopt(service, proposal["id"])
    item["content_sha256"] = "a" * 64
    binding["cell_sha256"] = "e" * 64
    with pytest.raises(ConflictError, match="Reference inputs"):
        readiness.adopt(service, proposal["id"])
    assert service.registry.list(readiness.MODEL_KIND) == []


def test_unsupported_change_retained_but_never_ready(setup):
    service, *_ = setup
    proposal = readiness.propose(service, payload("pixel_pitch_um", 1.4))
    assert not proposal["execution_supported"]
    model = readiness.adopt(service, proposal["id"])
    check = readiness.check(service, model["id"])
    assert not check["execution_ready"]
    assert check["status"] == "blocked"
    assert not check["physical_validation_pass"]


def test_check_is_model_specific_cancellable_and_stale_after_source_change(setup):
    service, item, sources, *_ = setup
    model = readiness.adopt(service, readiness.propose(service, payload())["id"])

    def cancel(stage):
        if stage["stage"] == "publishing_model_check":
            raise ConflictError("Cancelled")

    with pytest.raises(ConflictError, match="Cancelled"):
        readiness.check(service, model["id"], progress=cancel)
    assert service.registry.list(readiness.CHECK_KIND) == []
    first = readiness.check(service, model["id"])
    assert first["execution_ready"]
    assert readiness.detail(service, item["id"])["models"][0]["latest_check"]["execution_ready"]
    sources["component_catalog"]["sha256"] = "d" * 64
    assert not readiness.detail(service, item["id"])["models"][0]["latest_check"]["execution_ready"]
    second = readiness.check(service, model["id"])
    assert second["id"] != first["id"]
    assert readiness.detail(service, item["id"])["models"][0]["latest_check"]["id"] == second["id"]


def test_scan_unchanged_no_new_model_unavailable_not_deletion(setup, monkeypatch):
    service, item, sources, view, *_ = setup
    first = readiness.scan(service)
    assert not first["unchanged"]
    second = readiness.scan(service)
    assert second["unchanged"] and second["id"] == first["id"]
    assert service.registry.list(readiness.MODEL_KIND) == []
    assert service.registry.list(readiness.PROPOSAL_KIND) == []
    monkeypatch.setattr(readiness, "_index", lambda service: None)
    sources["component_catalog"] = {"status": "unavailable", "sha256": None}
    third = readiness.scan(service)
    assert third["source_status"] == "unavailable"
    assert third["assets"] == first["assets"]
    assert all(c["status"] != "deleted" for c in third["changes"])


def test_source_change_creates_proposal_and_snapshot_adoption_without_execution(setup):
    service, item, _, _, *_ = setup
    readiness.scan(service)
    item["content_sha256"] = "e" * 64
    readiness.scan(service)
    proposals = service.registry.list(readiness.PROPOSAL_KIND)
    assert len(proposals) == 1 and proposals[0]["kind"] == "source_change"
    assert service.registry.list(readiness.MODEL_KIND) == []
    adopted = readiness.adopt(service, proposals[0]["id"])
    assert not adopted["execution_supported"]
    assert adopted["baseline_electrical_model"] is None
    assert not readiness.check(service, adopted["id"])["execution_ready"]


@pytest.mark.parametrize(
    "change",
    [
        {"origin_kind": "measured"},
        {"value": float("nan")},
        {"value": True},
        {"value": -1},
        {"reason": ""},
    ],
)
def test_manual_input_cannot_claim_measurement_or_invalid_values(setup, change):
    service, *_ = setup
    request = payload()
    request["changes"][0].update(change)
    with pytest.raises(ValueError):
        readiness.propose(service, request)


def test_validation_link_requires_exact_model_and_does_not_promote_synthetic(setup, monkeypatch):
    service, item, *_ = setup
    model = readiness.adopt(service, readiness.propose(service, payload())["id"])
    measurement = service.registry.put(
        "measurements",
        {
            "source_kind": "synthetic",
            "physical_instance": None,
            "conditions": {"temperature_c": 25},
        },
    )
    test = service.registry.put(
        "tests", {"metric_id": "read_noise_e", "validation_kind": "implementation_verification"}
    )
    run = {
        "id": "run-evaluation",
        "content_hash": "a" * 64,
        "design_asset_model_id": model["id"],
        "design_asset_model_content_hash": model["content_hash"],
    }
    monkeypatch.setattr(service.registry, "get_run", lambda identity: copy.deepcopy(run))
    validation = service.registry.put(
        "validations",
        {
            "run_id": run["id"],
            "measurement_id": measurement["id"],
            "test_id": test["id"],
            "evidence_hashes": {
                "run": run["content_hash"],
                "measurement": measurement["content_hash"],
                "test": test["content_hash"],
            },
            "physical_validation_pass": True,
            "comparison_valid": True,
        },
    )
    link = readiness.link_validation(
        service, {"model_id": model["id"], "validation_id": validation["id"]}
    )
    assert not link["physical_match"]
    assert (
        readiness.detail(service, item["id"])["readiness"]["measurement"]["status"]
        == "not_established"
    )
    run["design_asset_model_content_hash"] = "b" * 64
    with pytest.raises(ValueError, match="exact adopted model"):
        readiness.link_validation(
            service, {"model_id": model["id"], "validation_id": validation["id"]}
        )


def test_source_file_only_change_records_applied_diff_and_stale_adoption(setup):
    service, _, sources, _, cell, *_ = setup
    sources["reference_adapter"] = {"status": "available", "sha256": "1" * 64}
    readiness.scan(service)
    sources["reference_adapter"]["sha256"] = "2" * 64
    cell["electrical_model"]["read_noise_e"] = 5
    scan = readiness.scan(service)
    assert not any(c["kind"] == "asset" for c in scan["changes"])
    proposal = service.registry.list(readiness.PROPOSAL_KIND)[0]
    change = proposal["changes"][0]
    assert change["field"] == "read_noise_e"
    assert change["before"]["value"] == 10
    assert change["after"]["value"] == 5
    assert not proposal["execution_supported"]
    sources["reference_adapter"]["sha256"] = "3" * 64
    with pytest.raises(ConflictError, match="Registered inputs changed"):
        readiness.adopt(service, proposal["id"])
    assert service.registry.list(readiness.MODEL_KIND) == []


def test_cancelled_job_cannot_unlock_even_after_check_record_published(setup, monkeypatch):
    service, item, *_ = setup
    model = readiness.adopt(service, readiness.propose(service, payload())["id"])
    job = {"status": "running"}
    monkeypatch.setattr(service.registry, "get_run", lambda identity: copy.deepcopy(job))
    check = readiness.check(service, model["id"], job_id="run-check")
    assert check["execution_ready"]
    assert readiness.detail(service, item["id"])["models"][0]["latest_check"] is None
    job["status"] = "cancelled"
    assert readiness.detail(service, item["id"])["models"][0]["latest_check"] is None
    job["status"] = "succeeded"
    assert readiness.detail(service, item["id"])["models"][0]["latest_check"]["execution_ready"]


def test_trial_completion_needs_matching_model_hash_and_completed_run(setup, monkeypatch):
    service, item, *_ = setup
    model = readiness.adopt(service, readiness.propose(service, payload())["id"])
    readiness.check(service, model["id"])
    service.registry.put(
        "design_asset_trial_links",
        {
            "model_revision_id": model["id"],
            "model_content_hash": model["content_hash"],
            "run_id": "run-trial",
        },
    )
    trial = {
        "design_asset_model_id": model["id"],
        "design_asset_model_content_hash": model["content_hash"],
        "status": "running",
    }
    monkeypatch.setattr(service.registry, "get_run", lambda identity: copy.deepcopy(trial))
    assert not readiness.detail(service, item["id"])["models"][0]["trial_completed"]
    trial["status"] = "succeeded"
    assert readiness.detail(service, item["id"])["models"][0]["trial_completed"]
    assert (
        readiness.overview(service)["asset_statuses"][item["id"]]["execution"] == "trial_completed"
    )
    trial["design_asset_model_content_hash"] = "different"
    assert not readiness.detail(service, item["id"])["models"][0]["trial_completed"]


def test_selected_revision_only_and_actual_electrical_mapping_do_not_claim_evidence_use(setup):
    service, item, _, _, _, _, db = setup
    with sqlite3.connect(db) as connection:
        connection.executemany(
            "INSERT INTO spec_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "current-adc",
                    "revision",
                    None,
                    "adc_bit_depth",
                    "10",
                    "bit",
                    "extracted",
                    "report",
                    "{}",
                    None,
                    "component",
                ),
                (
                    "current-black",
                    "revision",
                    None,
                    "black_level_dn",
                    "32",
                    "DN",
                    "extracted",
                    "report",
                    "{}",
                    None,
                    "component",
                ),
                (
                    "current-total",
                    "revision",
                    None,
                    "reported_total_read_noise_e",
                    "2.3",
                    "electron",
                    "reported",
                    "report",
                    "{}",
                    None,
                    "component",
                ),
                (
                    "historical-adc",
                    "old-revision",
                    None,
                    "bit_depth",
                    "14",
                    "bit",
                    "assumed",
                    "profile",
                    '{"scenario":"historical"}',
                    None,
                    "component",
                ),
            ],
        )
    rows = readiness.detail(service, item["id"])["parameters"]
    by_id = {row["observation_id"]: row for row in rows if row["observation_id"]}
    assert "historical-adc" not in by_id and "assumed" not in by_id
    assert by_id["current-adc"]["source_value"] == 10
    assert by_id["current-adc"]["applied_value"] == 12
    assert by_id["current-black"]["applied_value"] == 64
    assert by_id["current-black"]["applied_origin_kind"] == "assumed"
    assert by_id["current-black"]["applied_mapping_status"] == "mapped"
    assert by_id["current-black"]["evidence_used_for_reference"] is False
    assert by_id["observed"]["evidence_used_for_reference"] is False
    assert by_id["current-total"]["applied_to_reference"] is None
    assert by_id["current-total"]["applied_mapping_status"] == "mapping_unknown"
    actual = next(row for row in rows if row["field"] == "full_well_e")
    assert actual["applied_value"] == 10000
    assert actual["evidence_used_for_reference"] is True
