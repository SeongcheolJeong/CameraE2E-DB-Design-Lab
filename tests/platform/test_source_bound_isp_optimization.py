"""Source-bound advanced ISP recipe through real Study cells and Worker execution."""

import io
import threading
import time
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import workflow as workflow
from test_isp_studies_v2 import SEQUENTIAL, acquire_charge, prepare

from camera_platform import imaging, isp_charge_source, isp_studies, isp_study_metrics_v2, sequences
from camera_platform import source_bound_isp_optimization as recipe
from camera_platform.evidence_package import graph, read_bundle
from camera_platform.registry import canonical, digest
from camera_platform.service import PlatformService
from camera_platform.worker import execute


def _source(service, run):
    return isp_charge_source.resolve(service, "run", run["id"], SEQUENTIAL)


def _texture_scene(workflow, scale=1.0):
    service, _, assembly, *_ = workflow
    fixture = imaging.fixture_template(service, assembly["id"])
    fixture.update(shape=[16, 16], pattern="spectral_chart",
                   irradiance_scale=fixture["irradiance_scale"] * scale)
    image = imaging.create_fixture(service, fixture)
    draft = sequences.scene_template(service, assembly["id"])
    draft.update(name="Stationary authored texture", revision="texture-v1")
    for knot in draft["payload"]["knots"]:
        knot["spectral_image_id"] = image["id"]
    return sequences.create_sequence(service, "scene_sequences", draft)


def _reference(service, source, baseline, label):
    metadata = {
        "schema": "camera_isp_reference_v2",
        "name": "Authored harmonic reference " + label,
        "source_kind": "estimated", "source_uri": "fixture://harmonic/" + label,
        "license": "MIT", "method_and_version": "authored_harmonic_v1",
        "acquisition_group_id": "reference-" + label,
        "derivation": "authored_independent",
        "geometry": source["geometry"],
        "pixel_pitch_um": source["pixel_pitch_um"],
        "normalization_e": source["normalization_e"],
        "common_linear_profile": {
            key: baseline["payload"][key]
            for key in ("demosaic", "white_balance", "ccm")
        },
        "uncertainty": {"kind": "unknown", "reason": "Authored synthetic target"},
        "description": "Predeclared harmonic target, not derived from candidate output",
    }
    _, x = np.indices((16, 16))
    target = np.broadcast_to((0.2 + 0.02 * np.sin(2 * np.pi * x / 8))[..., None],
                             (16, 16, 3)).copy()
    stream = io.BytesIO()
    np.save(stream, target, allow_pickle=False)
    return isp_studies.import_reference(service, metadata, stream.getvalue())


def _dataset(source, key, purpose, *, reference_id=None):
    metric_spec = {
        "schema": "camera_isp_metric_spec_v2", "kind": key,
        "stationary": True,
        "roi": {"row_start": 4, "row_end": 12,
                "column_start": 4, "column_end": 12},
    }
    if key == "texture":
        metric_spec["texture"] = {"direction": "column", "period_px": 8}
    return {
        "key": key, "source_kind": "run", "source_id": source["source_id"],
        "selector": SEQUENTIAL, "purpose": purpose,
        "acquisition_group_id": "acquisition:" + source["source_id"],
        "stationarity": {
            "kind": "declared_static", "source_kind": "simulated",
            "description": "Frozen static authored scene repeated with independent noise frames",
        },
        "metric_spec": metric_spec, "reference_id": reference_id,
    }


def _request(workflow):
    service = workflow[0]
    flat, baseline, _, program = prepare(workflow)
    texture_scene = _texture_scene(workflow)
    texture_workflow = (*workflow[:3], texture_scene, *workflow[4:])
    texture_run, _ = acquire_charge(texture_workflow, seed=112, program=program)
    holdout_flat_run, _ = acquire_charge(workflow, seed=113, fresh_scene=True,
                                        program=program)
    holdout_texture_scene = _texture_scene(workflow, scale=1.1)
    holdout_texture_workflow = (*workflow[:3], holdout_texture_scene, *workflow[4:])
    holdout_texture_run, _ = acquire_charge(holdout_texture_workflow, seed=114,
                                           program=program)
    texture = _source(service, texture_run)
    holdout_flat = _source(service, holdout_flat_run)
    holdout_texture = _source(service, holdout_texture_run)
    reference_tune = _reference(service, texture, baseline, "tuning")
    reference_holdout = _reference(service, holdout_texture, baseline, "holdout")
    payload = {
        "schema": recipe.SCHEMA,
        "name": "Frozen RAW noise and texture tradeoff",
        "baseline_isp_model_id": baseline["id"],
        "calibration_id": None,
        "axes": [{"parameter": "raw_denoise.strength", "values": [0.0, 0.8]}],
        "tuning_datasets": [
            _dataset(flat, "flat", "tuning"),
            _dataset(texture, "texture", "tuning", reference_id=reference_tune["id"]),
        ],
        "holdout_datasets": [
            _dataset(holdout_flat, "flat", "independent_validation"),
            _dataset(holdout_texture, "texture", "independent_validation",
                     reference_id=reference_holdout["id"]),
        ],
        "limits": {
            "flat_noise_max_e": 1e12,
            "texture_ratio_min": 0.0,
            "texture_ratio_max": 1e12,
            "texture_abs_bias_max_e": 1e12,
            "holdout_noise_noninferiority_e": 1e12,
        },
    }
    return payload, [flat, texture, holdout_flat, holdout_texture]


def _pump_owned_jobs(service, stopped, errors):
    while not stopped.is_set():
        try:
            job = service.registry.claim()
            if job is None:
                time.sleep(0.01)
                continue
            execute(service, job["id"])
        except Exception as error:  # Surface actual Worker/registry failure in test.
            errors.append(error)
            stopped.set()


def test_reference_texture_metric_is_graph_serializable():
    frames = np.full((4, 16, 16, 3), 0.2, dtype=np.float64)
    _, x = np.indices((16, 16))
    reference = np.broadcast_to((0.2 + 0.02 * np.sin(2 * np.pi * x / 8))[..., None],
                                (16, 16, 3)).copy()
    result = isp_study_metrics_v2.measure(
        frames, normalization_e=100.0,
        dataset={
            "schema": "camera_isp_metric_spec_v2", "kind": "texture",
            "stationary": True,
            "roi": {"row_start": 4, "row_end": 12,
                    "column_start": 4, "column_end": 12},
            "texture": {"direction": "column", "period_px": 8},
        },
        evaluation_mask=np.ones((16, 16), dtype=bool),
        valid_mask_frames=np.ones((4, 16, 16), dtype=bool),
        baseline_rgb_frames=frames,
        baseline_valid_mask_frames=np.ones((4, 16, 16), dtype=bool),
        reference_rgb=reference,
    )
    assert type(result["diagnostics"]["texture"]["reference_numerical_resolution_e"]) is float
    assert graph(result)["root"]["sha256"]


def test_recipe_runs_tuning_selection_and_disjoint_holdout_without_changing_raw(workflow):
    service = workflow[0]
    payload, sources = _request(workflow)
    original = {source["source_id"]: service.registry.blob_path(source["raw_sha256"]).read_bytes()
                for source in sources}
    before = service.registry.list("isp_studies")
    planned = recipe.plan_recipe(service, payload)
    assert planned["tuning_plan"]["job_count"] == 6
    assert planned["product_performance"]["available"] is False
    assert planned["product_performance"]["holdout_blinding_enforced"] is False
    assert {item["source_kind_declared"] for item in planned["source_evidence"]} == {
        "simulated"
    }
    assert {item["reference_derivation_declared"] for item in planned["source_evidence"]} == {
        None, "authored_independent"
    }
    assert service.registry.list("isp_studies") == before
    assert planned["holdout_source_bindings_sha256"]
    stopped = threading.Event()
    errors = []
    thread = threading.Thread(target=_pump_owned_jobs, args=(service, stopped, errors), daemon=True)
    thread.start()
    try:
        receipt = recipe.run_recipe(service, payload, timeout_seconds=120, poll_seconds=0.02)
    finally:
        stopped.set()
        thread.join(timeout=5)
    assert not errors
    checked = recipe.get_receipt(service, receipt["id"])
    assert checked == receipt
    report = receipt["payload"]
    assert report["schema"] == recipe.RECEIPT_SCHEMA
    assert report["physical_validation"] == "not_established"
    assert report["product_ranking_allowed"] is False
    assert report["product_performance"]["available"] is False
    assert report["product_performance"]["holdout_semantics"] == (
        "source_disjoint_retrospective_unblinded"
    )
    assert "one_or_more_sources_are_not_declared_measured_acquisitions" in (
        report["product_performance"]["blockers"]
    )
    assert "one_or_more_texture_references_are_authored_not_measured" in (
        report["product_performance"]["blockers"]
    )
    assert report["selection_id"] and report["holdout_study_id"]
    assert report["assessment"]["status"] in {
        "retrospective_criteria_pass", "retrospective_baseline_selected"
    }
    assert report["assessment"]["holdout_blinding_enforced"] is False
    assert report["assessment"]["product_performance_available"] is False
    tuning = isp_studies.get_study(service, report["tuning_study_id"])
    holdout = isp_studies.get_study(service, report["holdout_study_id"])
    assert len(tuning["cells"]) == 6
    assert len(holdout["cells"]) in {2, 4}
    assert isp_studies.analyze(service, tuning["id"])["coverage"]["succeeded"] == 6
    assert isp_studies.analyze(service, holdout["id"])["aggregate"]["pareto_scope"] == (
        "fixed_baseline_and_explicit_selection_no_validation_ranking"
    )
    for source in sources:
        assert (service.registry.blob_path(source["raw_sha256"]).read_bytes()
                == original[source["source_id"]])
    manifest = isp_studies.export_manifest(service, holdout["id"])
    bundle = isp_studies.export_bundle(service, holdout["id"])
    assert read_bundle(bundle, expected_manifest_sha256=digest(manifest)) == manifest


def test_legacy_v1_receipt_remains_readable(tmp_path):
    service = PlatformService(tmp_path / "state", tmp_path)
    payload = {
        "schema": recipe.LEGACY_RECEIPT_SCHEMA,
        "assessment": {"status": "pass", "scope": "old_fixed_condition_result"},
    }
    artifact = service.registry.store_blob(
        canonical(payload).encode(), "source-bound-isp-optimization.json", "application/json"
    )
    record = service.registry.put_verified_record(
        recipe.RECEIPT_KIND,
        {"schema": recipe.LEGACY_RECEIPT_SCHEMA, "payload": payload, "artifact": artifact},
        [], identity="isp_optimization_receipt-legacy-test",
    )
    assert recipe.get_receipt(service, record["id"]) == record


def test_request_refuses_fake_source_and_reused_holdout_without_publication(workflow):
    service = workflow[0]
    payload, _ = _request(workflow)
    assert recipe.plan_recipe(service, payload)["tuning_plan"]["job_count"] == 6
    invalid = deepcopy(payload)
    invalid["holdout_datasets"][0]["source_kind"] = "faca_report"
    with pytest.raises(ValueError):
        recipe.plan_recipe(service, invalid)
    repeated = deepcopy(payload)
    repeated["holdout_datasets"][0]["source_id"] = repeated["tuning_datasets"][0]["source_id"]
    with pytest.raises(ValueError, match="distinct"):
        recipe.plan_recipe(service, repeated)
    overflow = deepcopy(payload)
    overflow["limits"]["flat_noise_max_e"] = 10**400
    with pytest.raises(ValueError, match="finite nonnegative"):
        recipe.validate_request(overflow)
    name_limit = deepcopy(payload)
    name_limit["name"] = "n" * 135
    assert recipe.validate_request(name_limit)["name"] == name_limit["name"]
    name_limit["name"] += "n"
    with pytest.raises(ValueError, match="bounded nonblank"):
        recipe.validate_request(name_limit)
    assert service.registry.list("isp_studies") == []


def test_recipe_refuses_mixed_readout_binding_across_tuning_and_holdout(workflow):
    service, sdk, assembly, *_ = workflow
    payload, sources = _request(workflow)
    altered_program = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode="independent_bracket_nonlinear"
    )
    altered_program["payload"]["electrical_model"]["digital_gain"] = 2
    altered_program = sdk.create_capture_program(altered_program)
    altered_scene = _texture_scene(workflow, scale=1.2)
    altered_workflow = (*workflow[:3], altered_scene, *workflow[4:])
    altered_run, _ = acquire_charge(altered_workflow, seed=115, program=altered_program)
    altered_source = _source(service, altered_run)
    baseline = service.registry.get("models", payload["baseline_isp_model_id"])
    altered_reference = _reference(service, altered_source, baseline, "altered-readout")
    payload["tuning_datasets"][1] = _dataset(
        altered_source, "texture", "tuning", reference_id=altered_reference["id"]
    )
    assert altered_source["source_id"] not in {source["source_id"] for source in sources}
    tuning_flat = isp_charge_source.resolve(
        service, "run", payload["tuning_datasets"][0]["source_id"], SEQUENTIAL
    )
    assert altered_source["normalization_e"] == tuning_flat["normalization_e"]
    assert altered_program["payload"]["electrical_model"]["digital_gain"] == 2
    before = service.registry.list("isp_studies")
    with pytest.raises(ValueError, match="one common selector, readout, geometry"):
        recipe.plan_recipe(service, payload)
    assert service.registry.list("isp_studies") == before
