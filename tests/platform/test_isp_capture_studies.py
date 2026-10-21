"""Imported RAW Study identity and independent acquisition boundaries."""

import io
import json
import zipfile
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest
from test_hdr_workflow import workflow as workflow
from test_isp_studies import complete, rows

from camera_platform import (
    isp_charge_source,
    isp_replay_v3,
    nonlinear_captures,
    readout_calibrations,
)
from camera_platform import isp_studies as public_studies
from camera_platform import isp_studies_v2 as studies
from camera_platform.evidence_package import read_bundle
from camera_platform.isp_study_contracts_v2 import ISPReferenceImport
from camera_platform.registry import IntegrityError, digest


def source(*, identity="capture-a", group="group-a", path="hcg", decoder_fit=()):
    return {
        "schema": "camera_charge_isp_capture_source_v1",
        "source_kind": "capture",
        "source_id": identity,
        "source_snapshot": {"id": identity},
        "selector": {"mode": "shared_charge_path", "readout_id": path},
        "raw_sha256": "raw-" + identity,
        "raw_shape": [4, 16, 16],
        "acquisition_lineage": {
            "schema": "camera_capture_lineage_v1",
            "acquisition_group_id": group,
            "parent_capture_ids": [],
            "dataset_ids": [identity + "-dataset"],
            "exposure_tokens": [
                "capture-id:" + identity, "acquisition-group-id:" + group,
                "raw-frame-sha256:" + identity + "-hcg",
                "raw-frame-sha256:" + identity + "-lcg",
            ],
            "decoder_fitting_tokens": list(decoder_fit),
            "calibration_evidence_refs": [],
        },
    }


def dataset(value, *, reference=None):
    return {
        "source": value,
        "raw_sha256": value["raw_sha256"],
        "raw_frame_sha256": [value["source_id"] + value["selector"]["readout_id"]],
        "acquisition_lineage_ids": studies._lineage(None, value),
        "specification": {
            "acquisition_group_id": value["acquisition_lineage"]["acquisition_group_id"]
        },
        "reference_snapshot": reference,
    }


def test_sibling_path_and_transitive_capture_identity_are_never_independent():
    hcg, lcg = source(), source(path="lcg")
    # Deliberately different selected pixel hashes must not hide the paired exposure.
    assert dataset(hcg)["raw_frame_sha256"] != dataset(lcg)["raw_frame_sha256"]
    assert studies._overlap(dataset(hcg), dataset(lcg))
    derived = source(identity="crop", group="new-group")
    derived["acquisition_lineage"]["parent_capture_ids"] = [hcg["source_id"]]
    derived["acquisition_lineage"]["exposure_tokens"].extend(
        hcg["acquisition_lineage"]["exposure_tokens"]
    )
    assert studies._overlap(dataset(hcg), dataset(derived))
    assert not studies._overlap(dataset(hcg), dataset(source(identity="independent", group="new")))


def test_reencoded_paired_import_cannot_hide_run_sibling_frame_overlap():
    imported = dataset(source(path="lcg"))
    run = {
        "source": {"source_kind": "run"},
        "raw_sha256": "different-encoding-original-run-container",
        "raw_frame_sha256": ["capture-a-hcg"],
        "acquisition_lineage_ids": ["simulation-run"],
        "specification": {"acquisition_group_id": "unrelated-user-label"},
    }
    assert run["raw_sha256"] != imported["raw_sha256"]
    assert not set(run["raw_frame_sha256"]) & set(imported["raw_frame_sha256"])
    assert studies._overlap(run, imported)


def test_fit_aliases_include_original_exposure_but_not_shared_dark_artifacts():
    captures = [
        {
            "id": name, "dataset_ids": [label],
            "exposure_tokens": ["capture-id:" + name, "raw-frame-sha256:" + name],
            "raw_frame_hashes": [name],
            "artifacts": [
                {"name": "raw-capture.npy", "sha256": name + "-raw"},
                {"name": "dark-capture.npy", "sha256": "shared-dark"},
                {"name": "acquisition-metadata.json", "sha256": "shared-metadata"},
            ],
        }
        for name, label in [("fit", "fit-alias"), ("holdout", "holdout-alias")]
    ]
    service = SimpleNamespace(registry=SimpleNamespace(
        list=lambda kind: captures if kind == "captures" else [
            {"id": "measurement-alias", "dataset_ids": ["another-fit-alias"],
             "raw_capture_id": "fit"}
        ]
    ))
    closure = {
        "calibration_lineage": [{"dataset_ids": ["another-fit-alias"], "evidence_refs": []}],
        "decoder_fitting_tokens": ["dataset-id:decoder-training"],
    }
    actual = studies._capture_fit_tokens(service, closure)
    assert {"capture-id:fit", "raw-frame-sha256:fit", "fit-raw"} <= actual
    assert "dataset-id:decoder-training" in actual
    assert not {"capture-id:holdout", "holdout-raw", "shared-dark", "shared-metadata"} & actual


@pytest.mark.parametrize("changed", ["exposure_ns", "gain", "temperature_c", "isp_model_id"])
def test_imported_repeats_require_actual_fixed_settings(changed):
    value = source()
    value["frame_conditions"] = [
        {"frame_id": str(i), "frame_start_ns": i * 100,
         "exposure_ns": 10, "gain": 1.0, "temperature_c": 25.0,
         "isp_model_id": None, "readout_analog_gain": 1.0}
        for i in range(4)
    ]
    calibration = {"model_sha256": "model", "acquisition": {"gain": 1}, "dark_e_sha256": "dark"}
    # Absence of a simulation plugin/scene is legitimate for an imported capture.
    studies._fixed_acquisition(None, value, [calibration] * 4)
    value["frame_conditions"][2][changed] = "changed" if changed == "isp_model_id" else 2
    with pytest.raises(ValueError, match="fixed exposure"):
        studies._fixed_acquisition(None, value, [calibration] * 4)


def test_imported_repeats_do_not_accept_changed_dark_or_decoder():
    value = source()
    value["frame_conditions"] = [{"exposure_ns": 10}] * 4
    cal = {"model_sha256": "model", "acquisition": {"gain": 1}, "dark_e_sha256": "dark"}
    for key in ("model_sha256", "acquisition", "dark_e_sha256"):
        cals = [deepcopy(cal) for _ in range(4)]
        cals[1][key] = "changed"
        with pytest.raises(ValueError, match="identical bound"):
            studies._fixed_acquisition(None, value, cals)


def reference(value, *, parents=()):
    return {
        "acquisition_group_id": "reference-group",
        "acquisition_lineage": {"dataset_ids": []},
        "source_lineage_snapshots": [value],
        "parent_snapshots": list(parents),
    }


def test_reference_sibling_and_parent_ancestry_cannot_leak_into_study():
    target = source()
    sibling = source(path="lcg")
    ref = reference(source(identity="ref", group="ref-group"), parents=[reference(sibling)])
    with pytest.raises(ValueError, match="reference overlaps"):
        studies._reference_independence(None, [dataset(target, reference=ref)], set())


def test_reference_calibration_cannot_be_fitted_on_evaluated_capture():
    target = source()
    ref = reference(source(identity="ref", group="ref-group", decoder_fit=["capture-id:capture-a"]))
    with pytest.raises(ValueError, match="fitted on Study"):
        studies._reference_independence(None, [dataset(target, reference=ref)], set())


def test_reference_can_share_fixed_decoder_but_not_its_fitting_acquisition():
    target = source(decoder_fit=["capture-id:training"])
    ref_source = source(identity="ref", group="ref-group", decoder_fit=["capture-id:training"])
    ref = reference(ref_source)
    studies._reference_independence(None, [dataset(target, reference=ref)], {"capture-id:training"})
    with pytest.raises(ValueError, match="reference overlaps"):
        studies._reference_independence(None, [dataset(target, reference=ref)], {"capture-id:ref"})


def test_legacy_reference_parser_has_no_new_default_lineage_fields():
    assert "acquisition_lineage" not in ISPReferenceImport.model_fields
    assert studies._reference_contract({"schema": "camera_isp_reference_v2"}) is ISPReferenceImport


def npy(array):
    result = io.BytesIO()
    np.save(result, array, allow_pickle=False)
    return result.getvalue()


def imported_capture(workflow, *, shared=False, seed=201, calibration=None, parents=()):
    """Authored imported files exercise the hardware-data path without claiming a measurement."""
    service, _, assembly, *_, test, _ = workflow
    mode = "shared_charge_paths" if shared else "sequential_frames"
    if calibration is None:
        sensor = next(service.registry.get("models", identity) for identity in assembly["model_ids"]
                      if service.registry.get("models", identity)["kind"] == "sensor")
        metadata = readout_calibrations.template(service, sensor["id"], mode)
        metadata["dark_rate_shape"] = [16, 16]
        calibration = readout_calibrations.import_calibration(
            service, metadata, npy(np.full((16, 16), 2.0)),
            json.dumps(readout_calibrations.evidence_template(metadata)).encode(),
        )
    data = nonlinear_captures.template(service, assembly["id"], test["id"], mode)
    data.update(name=f"Authored imported Study fixture {seed}", source_kind="simulated",
                source_uri=f"fixture://imported-study/{seed}",
                dataset_ids=[f"capture-dataset-{seed}"],
                acquisition_group_id=f"capture-group-{seed}",
                parents=list(parents), raw_formats=deepcopy(calibration["raw_formats"]))
    original = data["frames"][0]
    data["frames"] = []
    settings = [
        {"analog_gain": (1.0 if path["analog_gain"][0] <= 1 <= path["analog_gain"][1]
                         else path["analog_gain"][0]),
         "digital_gain": path["digital_gain"]}
        for path in calibration["applicability"]["readouts"]
    ]
    for index in range(4):
        frame = deepcopy(original)
        frame.update(frame_id=f"exposure-{seed}-{index}", raw_index=index)
        frame["applied"].update(
            exposure_ns=4_000_000, gain=1.0 if shared else settings[0]["analog_gain"],
            temperature_c=25.0, readout_settings=deepcopy(settings),
        )
        frame["settings_evidence"].update(
            status="recorded_applied", source_uri=f"fixture://settings/{seed}/{index}",
            method_and_version="authored-fixture:1",
            description="Authored settings; not physical evidence",
        )
        data["frames"].append(frame)
    shape = (4, 2, 16, 16) if shared else (4, 16, 16)
    raw = np.random.default_rng(seed).integers(390, 410, size=shape, dtype=np.uint16)
    capture = nonlinear_captures.import_capture(service, data, npy(raw))
    return capture, calibration


def selector(calibration, path=None):
    return {
        "schema": "camera_nonlinear_raw_selection_v2",
        "mode": "shared_charge_path" if path else "sequential_frames",
        "readout_calibration_id": calibration["id"],
        **({"readout_id": path} if path else {}),
    }


def prepare_imported(workflow, path=None):
    service = workflow[0]
    capture, calibration = imported_capture(workflow, shared=bool(path))
    selected = selector(calibration, path)
    draft = isp_replay_v3.template(
        service, source_kind="capture", source_id=capture["id"], selector=selected,
    )
    draft["payload"]["raw_denoise"].update(mode="local_charge_variance_filter_v1", strength=0.2)
    baseline = service.create_model(draft)
    spec = studies.template(service, "capture", capture["id"], None, baseline["id"], selected)
    return capture, calibration, baseline, spec


def submit(service, spec):
    plan = public_studies.plan(service, spec)
    return public_studies.submit(service, {"specification": spec, "plan_hash": plan["plan_hash"]})


@pytest.mark.parametrize("path", [None, "hcg", "lcg"])
def test_imported_raw_four_candidates_preserve_decoder_and_export_closure(workflow, path):
    service = workflow[0]
    capture, calibration, _, spec = prepare_imported(workflow, path)
    before = rows(service)
    plan = public_studies.plan(service, spec)
    assert plan["job_count"] == 4 and rows(service) == before
    original = deepcopy(capture)
    study = public_studies.submit(service, {"specification": spec, "plan_hash": plan["plan_hash"]})
    assert complete(service, study)["coverage"]["succeeded"] == 4
    source_snapshot = study["datasets"][0]["source"]
    assert source_snapshot["source_snapshot"] == original
    assert "plugin_id" not in original and "capture_program_snapshot" not in original
    assert study["calibration_snapshot"] is None
    closure = study["plan_hash_basis"]["provenance_closure"]
    assert closure["imported_readout_evidence"][0]["response_calibration_snapshot"] == calibration
    assert "dataset-id:authored-readout-response-1" in closure["decoder_fitting_tokens"]
    for cell in study["cells"]:
        job = public_studies.get_cell(service, cell["job_id"])
        assert job["result"]["raw_sha256"] == source_snapshot["raw_sha256"]
        assert job["result"]["selected_array_sha256"] == source_snapshot["selected_array_sha256"]
        assert (
            job["replay_request"]["source"]["decoder_binding"] == source_snapshot["decoder_binding"]
        )
        metric = job["result"]["metric_result"]["metrics"]["temporal_residual_std_e"]
        assert metric["unit"] == "electron_equivalent"
        assert not job["result"]["release_authority"]
    assert nonlinear_captures.get(service, capture["id"]) == original
    manifest = public_studies.export_manifest(service, study["id"])
    bundle = public_studies.export_bundle(service, study["id"])
    assert read_bundle(bundle, expected_manifest_sha256=digest(manifest)) == manifest
    with zipfile.ZipFile(bundle) as archive:
        assert (
            sum(v.file_size for v in archive.infolist())
            <= plan["resources"]["estimated_bundle_bytes"]
        )


def test_holdout_preserves_readout_revision_and_rejects_paired_sibling_and_parent(workflow):
    service = workflow[0]
    capture, calibration, _, spec = prepare_imported(workflow, "hcg")
    spec["axes"][0]["values"] = [0.0]
    study = submit(service, spec)
    analysis = complete(service, study)
    selected = public_studies.select_candidate(service, study["id"], {
        "candidate_id": study["candidates"][1]["candidate_id"],
        "rationale": "Authored integration evidence, no physical release",
        "analysis_sha256": analysis["analysis_sha256"],
    })
    validation = deepcopy(spec)
    validation.update(phase="independent_validation", axes=[], selection_id=selected["id"],
                      parent_study_id=study["id"])
    ds = validation["datasets"][0]
    ds["purpose"] = "independent_validation"
    with pytest.raises(ValueError, match="overlaps"):
        public_studies.plan(service, validation)
    ds["selector"]["readout_id"] = "lcg"
    with pytest.raises(ValueError, match="selected path"):
        public_studies.plan(service, validation)
    ds["selector"]["readout_id"] = "hcg"
    child, _ = imported_capture(workflow, shared=True, seed=202, calibration=calibration,
                                parents=[capture["id"]])
    ds.update(source_id=child["id"], acquisition_group_id=child["acquisition_group_id"])
    with pytest.raises(ValueError, match="overlaps"):
        public_studies.plan(service, validation)
    heldout, _ = imported_capture(workflow, shared=True, seed=203, calibration=calibration)
    ds.update(source_id=heldout["id"], acquisition_group_id=heldout["acquisition_group_id"])
    accepted = submit(service, validation)
    assert complete(service, accepted)["coverage"]["succeeded"] == 2
    assert (
        accepted["datasets"][0]["processing_binding"] == study["datasets"][0]["processing_binding"]
    )


def test_same_raw_alternate_calibration_does_not_create_independent_exposure(workflow):
    service = workflow[0]
    capture, calibration, _, spec = prepare_imported(workflow)
    metadata = {key: deepcopy(calibration[key]) for key in readout_calibrations.FIELDS}
    metadata.update(revision="alternate", name="Alternate interpretation of the same RAW")
    alternate = readout_calibrations.import_calibration(
        service, metadata, npy(np.full((16, 16), 2.0)),
        json.dumps(readout_calibrations.evidence_template(metadata)).encode(),
    )
    original = isp_charge_source.resolve(service, "capture", capture["id"], selector(calibration))
    reinterpreted = isp_charge_source.resolve(
        service, "capture", capture["id"], selector(alternate)
    )
    assert original["raw_sha256"] == reinterpreted["raw_sha256"]
    assert (
        original["acquisition_lineage"]["exposure_tokens"]
        == reinterpreted["acquisition_lineage"]["exposure_tokens"]
    )
    assert original["decoder_binding"] != reinterpreted["decoder_binding"]
    a = studies._dataset(service, spec["datasets"][0], None)
    changed = {**spec["datasets"][0], "selector": selector(alternate)}
    b = studies._dataset(service, changed, None)
    assert studies._overlap(a, b) and a["processing_binding"] != b["processing_binding"]


def test_decoder_fitting_capture_rejected_without_optional_lsc(workflow):
    service = workflow[0]
    capture, calibration, _, spec = prepare_imported(workflow)
    metadata = {key: deepcopy(calibration[key]) for key in readout_calibrations.FIELDS}
    metadata.update(revision="fit-on-target", reference_capture_ids=[capture["id"]])
    fitted = readout_calibrations.import_calibration(
        service, metadata, npy(np.full((16, 16), 2.0)),
        json.dumps(readout_calibrations.evidence_template(metadata)).encode(),
    )
    spec["datasets"][0]["selector"] = selector(fitted)
    before = rows(service)
    with pytest.raises(ValueError, match="calibration fitting"):
        public_studies.plan(service, spec)
    assert rows(service) == before


def test_imported_study_readback_detects_calibration_damage_after_success(workflow):
    service = workflow[0]
    _, calibration, _, spec = prepare_imported(workflow)
    spec["axes"][0]["values"] = [0.0]
    study = submit(service, spec)
    complete(service, study)
    assert public_studies.get_study(service, study["id"]) == study
    artifact = next(a for a in calibration["artifacts"] if a["name"] == "expected-dark-rate.npy")
    path = service.registry.blob_path(artifact["sha256"])
    original = path.read_bytes()
    path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    try:
        with pytest.raises((ValueError, IntegrityError)):
            public_studies.get_study(service, study["id"])
    finally:
        path.write_bytes(original)


def reference_metadata(source, baseline, *, lineage=True):
    return {
        "schema": "camera_isp_reference_v2", "name": "Authored independent charge target",
        "source_kind": "estimated", "source_uri": "fixture://independent-charge-reference",
        "license": "Authored fixture", "method_and_version": "declared-charge-reference:1",
        "acquisition_group_id": "reference-group", "derivation": "authored_independent",
        "geometry": source["geometry"], "pixel_pitch_um": source["pixel_pitch_um"],
        "normalization_e": source["normalization_e"],
        "common_linear_profile": {
            key: baseline["payload"][key] for key in ("demosaic", "white_balance", "ccm")
        },
        "uncertainty": {"kind": "unknown", "reason": "Authored fixture only"},
        "description": "Reference import/ancestry implementation check, no physical claim",
        **({"acquisition_lineage": {
            "schema": "camera_isp_reference_lineage_v1", "sources": [], "dataset_ids": [],
        }} if lineage else {}),
    }


def test_capture_reference_requires_explicit_lineage_and_refuses_known_candidate_output(workflow):
    service = workflow[0]
    capture, calibration, baseline, spec = prepare_imported(workflow)
    selected = isp_charge_source.resolve(service, "capture", capture["id"], selector(calibration))
    old = public_studies.import_reference(
        service, reference_metadata(selected, baseline, lineage=False),
        npy(np.full((16, 16, 3), 0.1)),
    )
    spec["datasets"][0]["reference_id"] = old["id"]
    with pytest.raises(ValueError, match="versioned acquisition lineage"):
        public_studies.plan(service, spec)
    metadata = reference_metadata(selected, baseline)
    metadata["acquisition_lineage"]["sources"] = [{
        "source_kind": "capture", "source_id": capture["id"], "selector": selector(calibration),
    }]
    overlap = public_studies.import_reference(service, metadata, npy(np.full((16, 16, 3), 0.2)))
    spec["datasets"][0]["reference_id"] = overlap["id"]
    with pytest.raises(ValueError, match="reference overlaps"):
        public_studies.plan(service, spec)
    independent = public_studies.import_reference(
        service, reference_metadata(selected, baseline), npy(np.full((16, 16, 3), 0.3)),
    )
    spec["datasets"][0]["reference_id"] = independent["id"]
    spec["axes"][0]["values"] = [0.0]
    study = submit(service, spec)
    complete(service, study)
    job = public_studies.get_cell(service, study["cells"][0]["job_id"])
    output = next(a for a in job["result"]["artifacts"] if a["name"] == "rgb.npy")
    before = rows(service)
    with pytest.raises(ValueError, match="Known ISP candidate output"):
        public_studies.import_reference(
            service, reference_metadata(selected, baseline),
            service.registry.blob_path(output["sha256"]).read_bytes(),
        )
    assert rows(service) == before
    # The legitimate independent reference is also present in a completed job;
    # that usage must not turn its original input into a forbidden candidate output.
    assert public_studies.get_reference(service, independent["id"]) == independent
