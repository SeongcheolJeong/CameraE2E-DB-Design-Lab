"""Native ISP calibration asset authority and numeric domains without API wrappers."""

import io
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import imaging
from camera_platform import isp_calibration as calibration
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.service import PlatformService
from camera_platform.spatial import request_template


def numeric():
    return {
        "schema": "camera_isp_calibration_v1",
        "sites": ["R", "Gr", "Gb", "B"],
        "lsc": {
            "schema": "native_four_site_grid_v1",
            "row_nodes": [0, 7],
            "column_nodes": [0, 9],
            "gains": [[[1.0, 1.2, 1.4, 1.6]] * 2] * 2,
            "valid_mask": [[[True, False, True, True]] * 2] * 2,
            "interpolation": "bilinear",
            "normalization": "declared_per_site_reference",
            "reference_roi": {"row_start": 0, "row_end": 2, "column_start": 0, "column_end": 2},
        },
        "noise": {
            "schema": "native_four_site_noise_v1",
            "slope_dn": [1.0, 2.0, 3.0, 4.0],
            "intercept_dn2": [4.0, 9.0, 16.0, 25.0],
            "signal_interval_dn": [-64.0, 4031.0],
            "reference": "black_subtracted_dn_before_lsc",
            "variance_model": "slope_times_max_signal_zero_plus_intercept",
            "spatial_independence": "assumed",
        },
    }


@pytest.fixture
def source(tmp_path):
    from camera_platform.demo import seed

    service = PlatformService(tmp_path / "calibration-unit")
    seed(service)
    info = service.bootstrap()
    original = service.registry.get("assemblies", info["demo"]["assembly_ids"][0])
    lens = next(
        service.registry.get("models", i)
        for i in original["model_ids"]
        if service.registry.get("models", i)["kind"] == "lens"
    )
    sensor = next(
        service.registry.get("models", i)
        for i in original["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    draft = {k: deepcopy(v) for k, v in lens.items() if k in ModelCreate.model_fields}
    psf = request_template()["psf"]
    psf.update(
        sample_pitch_um=sensor["payload"]["pixel_pitch_um"],
        wavelength_nm=sensor["payload"]["wavelength_nm"],
    )
    draft.update(
        name="Declared calibration fixture optical response",
        revision="calibration",
        source_kind="estimated",
        source_uri="fixture://calibration-optics",
    )
    draft["payload"]["psf"] = psf
    draft["included_effects"] += ["lens_psf", "pixel_aperture"]
    lens = service.create_model(draft)
    assembled = {k: deepcopy(v) for k, v in original.items() if k in AssemblyCreate.model_fields}
    assembled["model_ids"] = [
        lens["id"] if service.registry.get("models", i)["kind"] == "lens" else i
        for i in original["model_ids"]
    ]
    assembled.update(name="Calibration native camera", revision="calibration")
    assembly = service.create_assembly(assembled)
    fixture = imaging.fixture_template(service, assembly["id"])
    fixture.update(shape=[8, 8], pattern="flat")
    image = imaging.create_fixture(service, fixture)
    test = service.create_test(imaging.spatial_test_template(service, assembly["id"], image["id"]))
    queued = service.submit_run(
        {
            "assembly_id": assembly["id"],
            "test_id": test["id"],
            "scene_id": image["id"],
            "plugin_id": "spatial_scene_v1",
            "parameters": {"seed": 96},
        }
    )
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    run = service.registry.finish(job["id"], result=service.execute_run(job))
    value = imaging.replay_source(service, "run", run["id"])
    return service, value


def test_numeric_contract_preserves_four_sites_signed_domain_and_detaches():
    original = numeric()
    normalized = calibration.validate_numeric_payload(original)
    assert normalized == original
    original["lsc"]["gains"][0][0][1] = 8
    assert normalized["lsc"]["gains"][0][0] == [1.0, 1.2, 1.4, 1.6]
    assert normalized["noise"]["signal_interval_dn"][0] < 0
    assert calibration.validate_numeric_payload({**numeric(), "lsc": None, "noise": None}) == {
        "schema": "camera_isp_calibration_v1",
        "sites": ["R", "Gr", "Gb", "B"],
        "lsc": None,
        "noise": None,
    }


def test_unrepresentable_json_integer_is_a_declared_calibration_validation_error():
    data = numeric()
    data["noise"]["slope_dn"][0] = 10**400
    with pytest.raises(ValueError, match="slope_dn requires a finite number"):
        calibration.validate_numeric_payload(data)


@pytest.mark.parametrize(
    "fault",
    [
        "unknown",
        "sites",
        "descending",
        "too_many",
        "shape",
        "gain_low",
        "gain_high",
        "gain_bool",
        "mask",
        "nan",
        "noise_negative",
        "noise_channels",
        "reference",
        "domain",
        "normalization",
        "roi",
    ],
)
def test_numeric_malformed_or_ambiguous_calibration_refused(fault):
    data = numeric()
    if fault == "unknown":
        data["fit_current_frame"] = True
    elif fault == "sites":
        data["sites"] = ["R", "G", "B"]
    elif fault == "descending":
        data["lsc"]["row_nodes"] = [7, 0]
    elif fault == "too_many":
        data["lsc"]["row_nodes"] = list(range(66))
    elif fault == "shape":
        data["lsc"]["gains"] = [[[1, 1, 1, 1]]]
    elif fault.startswith("gain_"):
        data["lsc"]["gains"][0][0][0] = {"gain_low": 0.249, "gain_high": 16.01, "gain_bool": True}[
            fault
        ]
    elif fault == "mask":
        data["lsc"]["valid_mask"][0][0][0] = 1
    elif fault == "nan":
        data["noise"]["slope_dn"][0] = float("nan")
    elif fault == "noise_negative":
        data["noise"]["intercept_dn2"][0] = -0.1
    elif fault == "noise_channels":
        data["noise"]["slope_dn"] = [1, 2, 3]
    elif fault == "reference":
        data["noise"]["reference"] = "after_adaptive_denoise"
    elif fault == "domain":
        data["noise"]["signal_interval_dn"] = [0, 0]
    elif fault == "normalization":
        data["lsc"]["normalization"] = "automatic_frame_maximum"
    else:
        data["lsc"]["reference_roi"]["row_end"] = 0
    with pytest.raises(ValueError):
        calibration.validate_numeric_payload(data)


def test_asset_roundtrip_revision_parent_and_restart_preserve_exact_evidence(source):
    service, raw = source
    before = deepcopy(raw["source_snapshot"])
    payload = calibration.template(service, "run", raw["source_id"])
    payload["name"] = "교정 원문 · 선언한 가정"
    assert payload["source_kind"] == "estimated"
    first = calibration.import_calibration(service, payload)
    assert first["physical_validation"] == "not_established"
    assert first["authority"] == {"physical_validation": False, "release": False}
    assert len(first["artifacts"]) == 2
    evidence = first["evidence_artifacts"][0]
    assert evidence["sha256"] == raw["raw_sha256"]
    assert evidence["name"] == "isp-calibration-evidence-000.bin"
    assert evidence["bytes"] == service.registry.blob_path(evidence["sha256"]).stat().st_size
    assert evidence["role"] == payload["evidence_refs"][0]["role"]
    for artifact in first["artifacts"]:
        content = service.registry.blob_path(artifact["sha256"]).read_bytes()
        assert content == canonical(payload).encode()
    changed = deepcopy(payload)
    changed.update(parents=[first["id"]], revision="2")
    changed["payload"]["lsc"]["gains"][0][0][2] = 1.5
    second = calibration.import_calibration(service, changed)
    assert first["id"] != second["id"] and first["content_hash"] != second["content_hash"]
    restarted = PlatformService(service.registry.directory)
    assert calibration.get_calibration(restarted, first["id"]) == first
    assert calibration.resolve_for_source(restarted, second["id"], raw) == second
    assert restarted.registry.get_run(raw["source_id"]) == before


@pytest.mark.parametrize(
    "artifact_name",
    [
        "isp-calibration-source.json",
        "isp-calibration-normalized.json",
        "reference-raw",
        "independent-evidence",
    ],
)
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_evidence_bytes_rechecked_after_import(source, artifact_name, damage):
    service, raw = source
    data = calibration.template(service, "run", raw["source_id"])
    evidence = service.registry.store_blob(
        b"independent explicit calibration source", "flat.bin", "application/octet-stream"
    )
    data["evidence_refs"].append({"sha256": evidence["sha256"], "role": "declared model input"})
    record = calibration.import_calibration(service, data)
    if artifact_name == "reference-raw":
        sha = raw["raw_sha256"]
    elif artifact_name == "independent-evidence":
        sha = evidence["sha256"]
    else:
        sha = next(a["sha256"] for a in record["artifacts"] if a["name"] == artifact_name)
    path = service.registry.blob_path(sha)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    with pytest.raises((KeyError, IntegrityError)):
        calibration.resolve_for_source(service, record["id"], raw)


@pytest.mark.parametrize(
    "fault",
    [
        "sensor",
        "lens",
        "pitch",
        "readout",
        "format",
        "full_shape",
        "condition",
        "unit",
        "missing_condition",
        "grid_crop",
        "grid_full",
        "missing_evidence",
        "missing_parent",
        "measured_unqualified",
    ],
)
def test_wrong_binding_domain_or_missing_evidence_refused_before_publish(source, fault):
    service, raw = source
    data = calibration.template(service, "run", raw["source_id"])
    if fault in {"sensor", "lens"}:
        data["binding"][fault + "_model_id"] = "model-not-this-camera"
    elif fault == "pitch":
        data["binding"]["pixel_pitch_um"] *= 2
    elif fault == "readout":
        data["binding"]["readout_mode"]["readout_sha256"] = "0" * 64
    elif fault == "format":
        data["binding"]["raw_format"]["black_level_dn"] += 1
    elif fault == "full_shape":
        data["binding"]["full_sensor_shape"][0] += 2
    elif fault == "condition":
        data["domain"]["conditions"]["gain"]["value"]["value"] += 1
    elif fault == "unit":
        data["domain"]["conditions"]["gain"]["value"]["unit"] = "DN"
    elif fault == "missing_condition":
        del data["domain"]["conditions"]["temperature_c"]
    elif fault == "grid_crop":
        data["payload"]["lsc"]["row_nodes"] = [0, 1]
        data["payload"]["lsc"]["reference_roi"]["row_end"] = 2
    elif fault == "grid_full":
        data["payload"]["lsc"]["row_nodes"][-1] += 1
    elif fault == "missing_evidence":
        data["evidence_refs"].append({"sha256": "0" * 64, "role": "missing"})
    elif fault == "missing_parent":
        data["parents"] = ["isp_calibration-does-not-exist"]
    else:
        data["source_kind"] = "measured"
    count = len(service.registry.list("isp_calibrations"))
    with pytest.raises((ValueError, KeyError)):
        calibration.import_calibration(service, data)
    assert len(service.registry.list("isp_calibrations")) == count


@pytest.mark.parametrize(
    "field", ["geometry", "raw_format", "conditions", "raw_sha256", "source_snapshot"]
)
def test_worker_context_cannot_replace_preserved_source_fields(source, field):
    service, raw = source
    record = calibration.import_calibration(
        service, calibration.template(service, "run", raw["source_id"])
    )
    altered = deepcopy(raw)
    if field == "geometry":
        altered[field]["crop_origin"][0] += 1
    elif field == "raw_format":
        altered[field]["black_level_dn"] += 1
    elif field == "conditions":
        altered[field]["gain"]["value"] += 1
    elif field == "raw_sha256":
        altered[field] = "0" * 64
    else:
        altered[field]["result"]["summary"]["geometry"]["crop_origin"][0] += 1
    with pytest.raises(ValueError, match="source|snapshot"):
        calibration.resolve_for_source(service, record["id"], altered)


def test_native_registered_capture_uses_pinned_assembly_and_refuses_missing_identity(source):
    service, raw = source
    # A minimal registry fixture for the capture resolver contract, not an import-path test.
    stream = io.BytesIO()
    np.save(stream, np.full((1, 8, 8), 100, dtype=np.uint16), allow_pickle=False)
    artifact = service.registry.store_blob(
        stream.getvalue(), "raw-capture.npy", "application/octet-stream"
    )
    data = {
        "name": "Explicit synthetic capture binding fixture",
        "source_kind": "estimated",
        "source_uri": "fixture://capture-binding",
        "license": "MIT",
        "physical_instance": None,
        "method_and_version": "declared_fixture:1",
        "assembly_id": raw["source_snapshot"]["assembly_id"],
        "raw_shape": [1, 8, 8],
        "observation": {
            "geometry": raw["geometry"],
            "raw_format": raw["raw_format"],
            "conditions": raw["conditions"],
        },
        "artifacts": [artifact],
    }
    capture = service.registry.put("captures", data)
    template = calibration.template(service, "capture", capture["id"])
    record = calibration.import_calibration(service, template)
    assert (
        calibration.resolve_for_source(
            service, record["id"], imaging.replay_source(service, "capture", capture["id"])
        )
        == record
    )
    already_applied = service.registry.put(
        "captures", data | {"applied_calibration_ids": [record["id"]]}
    )
    with pytest.raises(ValueError, match="already applied"):
        calibration.resolve_for_source(
            service, record["id"], imaging.replay_source(service, "capture", already_applied["id"])
        )
    data.pop("assembly_id")
    unknown = service.registry.put("captures", data)
    with pytest.raises(ValueError, match="fixed model/assembly"):
        calibration.template(service, "capture", unknown["id"])


def test_parent_blob_integrity_and_immutable_payload_hash(source):
    service, raw = source
    draft = calibration.template(service, "run", raw["source_id"])
    first = calibration.import_calibration(service, draft)
    child = calibration.import_calibration(
        service, draft | {"parents": [first["id"]], "revision": "2"}
    )
    blob = service.registry.blob_path(first["artifacts"][0]["sha256"])
    blob.unlink()
    with pytest.raises(KeyError):
        calibration.get_calibration(service, child["id"])
    assert first["source_sha256"] == digest(raw["source_snapshot"])


def test_other_raw_same_fixed_camera_allowed_but_new_exposure_outside_domain_refused(source):
    service, raw = source
    record = calibration.import_calibration(
        service, calibration.template(service, "run", raw["source_id"])
    )
    original = raw["source_snapshot"]
    for exposure_ms, accepted in ((4, True), (8, False)):
        queued = service.submit_run(
            {
                "assembly_id": original["assembly_id"],
                "test_id": original["test_id"],
                "scene_id": original["scene_id"],
                "plugin_id": original["plugin_id"],
                "parameters": {"seed": 109, "exposure_ms": exposure_ms},
            }
        )
        job = service.registry.claim()
        assert job["id"] == queued["id"]
        result = service.registry.finish(job["id"], result=service.execute_run(job))
        target = imaging.replay_source(service, "run", result["id"])
        assert target["raw_sha256"] != raw["raw_sha256"]
        if accepted:
            assert calibration.resolve_for_source(service, record["id"], target) == record
        else:
            with pytest.raises(ValueError, match="outside declared domain: exposure_s"):
                calibration.resolve_for_source(service, record["id"], target)


def test_evidence_descriptor_and_frozen_snapshot_edits_rejected(source):
    service, raw = source
    record = calibration.import_calibration(
        service, calibration.template(service, "run", raw["source_id"])
    )
    data = {
        k: deepcopy(v) for k, v in record.items() if k not in {"id", "created_at", "content_hash"}
    }
    data["evidence_artifacts"][0]["bytes"] += 1
    changed = service.registry.put("isp_calibrations", data)
    with pytest.raises(ValueError, match="evidence descriptors"):
        calibration.get_calibration(service, changed["id"])
    data["evidence_artifacts"] = record["evidence_artifacts"]
    data["model_snapshots"][0]["name"] += " changed"
    changed = service.registry.put("isp_calibrations", data)
    with pytest.raises(ValueError, match="snapshot identity"):
        calibration.get_calibration(service, changed["id"])
