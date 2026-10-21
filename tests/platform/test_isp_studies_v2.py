"""Actual nonlinear RAW → v3 ISP → immutable candidate/validation Study evidence."""

import io
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import workflow as workflow
from test_isp_studies import complete, rows
from test_nonlinear_workflow import worker_finish

from camera_platform import capture_programs, isp_charge_source, isp_replay_v3, sequences
from camera_platform import isp_studies as studies
from camera_platform import isp_studies_v2 as charge_studies
from camera_platform.evidence_package import read_bundle
from camera_platform.registry import IntegrityError, digest

SEQUENTIAL = {"schema": "camera_nonlinear_raw_selection_v1", "mode": "sequential_frames"}


def acquire_charge(
    workflow,
    *,
    mode="independent_bracket_nonlinear",
    seed=927,
    repeated=True,
    fresh_scene=False,
    program=None,
):
    service, sdk, assembly, scene, *_ = workflow
    if fresh_scene:
        scene = sequences.create_fixture(
            service,
            sequences.fixture_template(service, assembly["id"])
            | {"shape": [16, 16], "knot_count": 2, "pattern": "static"},
        )
        fields = {
            k: deepcopy(v)
            for k, v in scene.items()
            if k in sequences.SceneSequenceCreate.model_fields
        }
        fields["payload"]["knots"][1]["spectral_image_id"] = fields["payload"]["knots"][0][
            "spectral_image_id"
        ]
        scene = sequences.create_sequence(service, "scene_sequences", fields)
    draft = sequences.control_template(service, assembly["id"], scene["id"])
    for index, frame in enumerate(draft["payload"]["frames"]):
        frame["applied"]["exposure_ns"] = (4 if repeated or index % 2 == 0 else 8) * 1_000_000
    control = sdk.create_control_sequence(draft)
    if program is None:
        program = sdk.create_capture_program(
            sdk.template("capture-program", assembly_id=assembly["id"], mode=mode)
        )
    test = service.create_test(
        sequences.test_template(service, assembly["id"], scene["id"], control["id"], program["id"])
    )
    run = worker_finish(
        service,
        {
            "assembly_id": assembly["id"],
            "test_id": test["id"],
            "scene_sequence_id": scene["id"],
            "control_sequence_id": control["id"],
            "capture_program_id": program["id"],
            "plugin_id": capture_programs.runtime_for_payload(program["payload"]),
            "parameters": {"seed": seed},
        },
    )
    return run, program


def prepare(workflow, mode="independent_bracket_nonlinear", readout=None, repeated=True):
    service = workflow[0]
    run, program = acquire_charge(workflow, mode=mode, repeated=repeated)
    selector = (
        SEQUENTIAL
        if readout is None
        else {"schema": SEQUENTIAL["schema"], "mode": "shared_charge_path", "readout_id": readout}
    )
    source = isp_charge_source.resolve(service, "run", run["id"], selector)
    draft = isp_replay_v3.template(
        service, source_kind="run", source_id=run["id"], selector=selector
    )
    draft["payload"]["raw_denoise"].update(mode="local_charge_variance_filter_v1", strength=0.2)
    baseline = service.create_model(draft)
    spec = charge_studies.template(service, "run", run["id"], None, baseline["id"], selector)
    assert spec["axes"] == [{"parameter": "raw_denoise.strength", "values": [0.0, 0.4, 0.8]}]
    return source, baseline, spec, program


def submit(service, spec):
    plan = studies.plan(service, spec)
    return studies.submit(service, {"specification": spec, "plan_hash": plan["plan_hash"]})


@pytest.mark.parametrize(
    "mode,readout",
    [
        ("independent_bracket_nonlinear", None),
        ("reference_guarded_nonlinear", None),
        ("shared_charge_nonlinear", "hcg"),
        ("shared_charge_nonlinear", "lcg"),
    ],
)
def test_four_actual_candidate_cells_share_raw_decode_units_and_exact_bundle(
    workflow, mode, readout
):
    service = workflow[0]
    source, _, spec, _ = prepare(workflow, mode, readout)
    before = rows(service)
    blobs = sorted(service.registry.blobs.rglob("*"))
    plan = studies.plan(service, spec)
    assert plan["job_count"] == 4
    assert plan["plan_hash"] == studies.plan(service, spec)["plan_hash"]
    assert rows(service) == before and sorted(service.registry.blobs.rglob("*")) == blobs
    assert plan["resources"]["estimated_bundle_bytes"] < charge_studies.MAX_ARCHIVE_BYTES
    original = service.registry.blob_path(source["raw_sha256"]).read_bytes()
    study = studies.submit(service, {"specification": spec, "plan_hash": plan["plan_hash"]})
    assert studies.submit(service, {"specification": spec, "plan_hash": plan["plan_hash"]}) == study
    analysis = complete(service, study)
    assert analysis["coverage"]["succeeded"] == 4
    for cell in study["cells"]:
        job = studies.get_cell(service, cell["job_id"])
        result = job["result"]
        assert result["raw_sha256"] == source["raw_sha256"]
        assert result["selected_array_sha256"] == source["selected_array_sha256"]
        assert result["normalization_e"] == source["normalization_e"]
        assert (
            result["metric_result"]["metrics"]["temporal_residual_std_e"]["unit"]
            == "electron_equivalent"
        )
        assert "normalization_dn" not in result["metric_result"]
        assert not result["release_authority"]
        if job["replay_request"]["profile"]["raw_denoise"]["strength"] == 0:
            actual, _ = charge_studies._array(service, result, "sharpened_rgb_frames")
            baseline, _ = charge_studies._array(service, result, "baseline_ccm_rgb_frames")
            np.testing.assert_array_equal(actual, baseline)
    assert service.registry.blob_path(source["raw_sha256"]).read_bytes() == original
    manifest = studies.export_manifest(service, study["id"])
    bundle = studies.export_bundle(service, study["id"])
    assert (
        read_bundle(io.BytesIO(bundle.read_bytes()), expected_manifest_sha256=digest(manifest))
        == manifest
    )
    import zipfile

    with zipfile.ZipFile(bundle) as archive:
        physical = sum(item.file_size for item in archive.infolist())
    assert physical <= plan["resources"]["estimated_bundle_bytes"]
    assert study == studies.get_study(service, study["id"])
    assert service.registry.audit()["integrity"] == "verified"


def test_charge_study_refuses_brackets_without_changing_queue_or_raw(workflow):
    service = workflow[0]
    _, _, spec, _ = prepare(workflow, repeated=False)
    before = rows(service)
    with pytest.raises(ValueError, match="fixed exposure|identical bound"):
        studies.plan(service, spec)
    assert rows(service) == before


def test_explicit_selection_holdout_rejects_shared_acquisition_and_preserves_path(workflow):
    service = workflow[0]
    source, _, spec, program = prepare(workflow, "shared_charge_nonlinear", "hcg")
    spec["axes"][0]["values"] = [0.0]
    study = submit(service, spec)
    analysis = complete(service, study)
    body = {
        "candidate_id": study["candidates"][1]["candidate_id"],
        "rationale": "Authored charge-domain choice; physical validation remains absent",
        "analysis_sha256": analysis["analysis_sha256"],
    }
    selected = studies.select_candidate(service, study["id"], body)
    assert selected["schema"] == "camera_isp_selection_v2"
    assert studies.get_selection(service, selected["id"]) == selected
    assert selected["analysis_snapshot"]["schema"] == "camera_isp_study_analysis_v2"
    assert all(
        not name.endswith("_dn")
        for row in selected["analysis_snapshot"]["rows"]
        for name in row["metrics"]
    )
    assert studies.select_candidate(service, study["id"], body) == selected
    validation = deepcopy(spec)
    validation.update(
        phase="independent_validation",
        axes=[],
        selection_id=selected["id"],
        parent_study_id=study["id"],
    )
    validation["datasets"][0].update(
        purpose="independent_validation", acquisition_group_id="holdout"
    )
    with pytest.raises(ValueError, match="overlaps"):
        studies.plan(service, validation)
    validation["datasets"][0]["selector"]["readout_id"] = "lcg"
    with pytest.raises(ValueError, match="selected path"):
        studies.plan(service, validation)
    validation["datasets"][0]["selector"]["readout_id"] = "hcg"
    run, _ = acquire_charge(
        workflow, mode="shared_charge_nonlinear", seed=984, fresh_scene=True, program=program
    )
    validation["datasets"][0]["source_id"] = run["id"]
    checked = submit(service, validation)
    result = complete(service, checked)
    assert all(r["pareto_status"] == "not_ranked_validation" for r in result["aggregate"]["rows"])
    assert source["selector"]["readout_id"] == "hcg"


def test_charge_reference_has_its_own_scale_and_no_legacy_blc(workflow):
    service = workflow[0]
    source, baseline, spec, _ = prepare(workflow)
    metadata = {
        "schema": "camera_isp_reference_v2",
        "name": "Independent charge target",
        "source_kind": "estimated",
        "source_uri": "fixture://independent-charge",
        "license": "MIT",
        "method_and_version": "authored_charge_v1",
        "acquisition_group_id": "independent-charge-ref",
        "derivation": "authored_independent",
        "geometry": source["geometry"],
        "pixel_pitch_um": source["pixel_pitch_um"],
        "normalization_e": source["normalization_e"],
        "common_linear_profile": {
            k: baseline["payload"][k] for k in ("demosaic", "white_balance", "ccm")
        },
        "uncertainty": {"kind": "unknown", "reason": "Authored only"},
        "description": "Independent authored pre-tone charge RGB",
    }
    stream = io.BytesIO()
    np.save(stream, np.full((16, 16, 3), 0.1))
    reference = studies.import_reference(service, metadata, stream.getvalue())
    assert studies.get_reference(service, reference["id"]) == reference
    spec["datasets"][0]["reference_id"] = reference["id"]
    assert studies.plan(service, spec)["job_count"] == 4
    wrong = studies.import_reference(
        service, {**metadata, "normalization_e": 2 * source["normalization_e"]}, stream.getvalue()
    )
    spec["datasets"][0]["reference_id"] = wrong["id"]
    with pytest.raises(ValueError, match="normalization"):
        studies.plan(service, spec)
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        studies.import_reference(service, {**metadata, "normalization_dn": 4095}, stream.getvalue())


def test_readback_does_not_migrate_frozen_environment_and_fresh_artifact_damage_fails(
    workflow, monkeypatch
):
    service = workflow[0]
    source, _, spec, _ = prepare(workflow)
    spec["axes"][0]["values"] = [0.0]
    study = submit(service, spec)
    complete(service, study)
    from camera_platform import imaging

    monkeypatch.setattr(imaging, "_current", lambda s: {"another": "current environment"})
    assert studies.get_study(service, study["id"]) == study
    assert studies.export_manifest(service, study["id"])["study"] == study
    path = service.registry.blob_path(source["dark_artifact"]["sha256"])
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(IntegrityError, match="checksum"):
        studies.get_study(service, study["id"])
    path.write_bytes(original)


def test_forecast_descriptor_oracle_and_large_calibration_refusal_before_writes(
    workflow, monkeypatch
):
    service = workflow[0]
    _, _, spec, _ = prepare(workflow)
    for shape, dtype in (((4, 16, 16), "float64"), ((16, 16), "bool"), ((4, 16, 16, 3), "uint16")):
        stream = io.BytesIO()
        np.save(stream, np.zeros(shape, dtype=dtype), allow_pickle=False)
        assert charge_studies._npy_bytes(shape, dtype) == len(stream.getvalue())
    before = rows(service)
    blobs = sorted(service.registry.blobs.rglob("*"))
    actual = isp_replay_v3.prepare
    large_calibration = {"retained_evidence_notes": "雪" * (24 * 1024 * 1024)}

    def enlarged(*args, **kwargs):
        replay = actual(*args, **kwargs)
        replay["calibration_snapshot"] = large_calibration
        # Exact copied snapshot bytes per cell exceed the archive cap across
        # four candidates, even though the graph can deduplicate the snapshot.
        forecast = isp_replay_v3.summary_forecast(replay)
        assert forecast["copied_summary_bytes"] >= 72 * 1024 * 1024
        return replay

    monkeypatch.setattr(isp_replay_v3, "prepare", enlarged)
    with pytest.raises(ValueError, match="256 MiB"):
        studies.plan(service, spec)
    assert rows(service) == before and sorted(service.registry.blobs.rglob("*")) == blobs


def test_unversioned_metric_and_bypassed_axis_never_silently_change_profile(workflow):
    service = workflow[0]
    _, baseline, spec, _ = prepare(workflow)
    before = rows(service)
    wrong = deepcopy(spec)
    wrong["datasets"][0]["metric_spec"].pop("schema")
    with pytest.raises(ValueError, match="camera_isp_metric_spec_v2"):
        studies.plan(service, wrong)
    wrong = deepcopy(spec)
    wrong["objectives"][0]["metric"] = "temporal_residual_std_dn"
    with pytest.raises(ValueError, match="Unknown Study metric"):
        studies.plan(service, wrong)
    wrong = deepcopy(spec)
    wrong["axes"] = [{"parameter": "linear_sharpen.amount", "values": [0.4]}]
    with pytest.raises(ValueError, match="bypassed"):
        studies.plan(service, wrong)
    assert rows(service) == before
    assert service.registry.get("models", baseline["id"]) == baseline
