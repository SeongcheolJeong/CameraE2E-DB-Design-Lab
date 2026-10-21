"""Executable manifest and screening contracts; all response fixtures are synthetic."""

import copy
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools/prepare_sensor_fdtd_sensitivity.py"
SPEC = importlib.util.spec_from_file_location("prepare_fdtd_sensitivity_test", SCRIPT)
sensitivity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sensitivity)


@pytest.fixture
def bundle_file(tmp_path):
    material = {"wavelength_nm": [400, 550, 700], "n": [1.7] * 3, "k": [0.1, 0.05, 0.01]}
    bundle = {
        "schema": "camerae2e_cfa_material_bundle_v1",
        "sensor_id": "synthetic",
        "report_code": "SYN-001",
        "pixel_pitch_um": 2.9,
        "wavelength_nm": material["wavelength_nm"],
        "estimate_only": True,
        "provenance": {"synthetic_test_fixture": True, "measured_accuracy_verified": False},
        "geometry_um": {
            "pitch": 2.9,
            "lens_height": 0.8,
            "lens_edge_gap": 0.08,
            "cfa_thickness": 0.5,
            "passivation_thickness": 0.8,
            "si_thickness": 3.0,
        },
        "channels": {
            name: {**copy.deepcopy(material), "thickness_um": 0.5, "thickness_range_um": [0.4, 0.6]}
            for name in sensitivity.CHANNELS
        },
        "materials": {name: copy.deepcopy(material) for name in ("lens", "passivation", "silicon")},
    }
    sensitivity._rehash_bundle(bundle)
    path = tmp_path / "canonical.json"
    path.write_text(json.dumps(bundle))
    return path, bundle


def test_variants_change_only_requested_material_or_geometry_and_rehash(bundle_file):
    _, bundle = bundle_file
    variants = dict(sensitivity.sensitivity_variants(bundle))
    assert len(variants) == 25
    assert variants["baseline"] == bundle
    red_k = variants["red_k_1.2"]
    np.testing.assert_allclose(
        red_k["channels"]["red"]["k"], np.array(bundle["channels"]["red"]["k"]) * 1.2
    )
    assert red_k["channels"]["green"] == bundle["channels"]["green"]
    assert red_k["geometry_sha256"] == bundle["geometry_sha256"]
    assert red_k["material_bundle_sha256"] != bundle["material_bundle_sha256"]
    thickness = variants["blue_thickness_low"]
    assert thickness["channels"]["blue"]["thickness_um"] == 0.4
    assert thickness["geometry_um"]["cfa_thickness_by_channel_um"] == {
        "red": 0.5,
        "green": 0.5,
        "blue": 0.4,
    }
    assert thickness["material_bundle_sha256"] == bundle["material_bundle_sha256"]
    assert thickness["geometry_sha256"] != bundle["geometry_sha256"]
    shift = variants["lens_shift_x_+0.05pitch"]
    np.testing.assert_allclose(shift["geometry_um"]["lens_shift_um"], [0.05 * 2.9, 0])
    assert bundle["geometry_um"].get("lens_shift_um") is None
    assert not any("efficiency" in name for name in variants)


def test_manifest_freezes_real_worker_inputs_and_eta_reuses_optics(bundle_file, tmp_path):
    path, _ = bundle_file
    manifest = sensitivity.prepare_sensitivity([path], tmp_path / "campaign", resolution=80)
    assert len(manifest["variants"]) == 25
    assert len(manifest["jobs"]) == 750
    assert manifest["collection_efficiency"]["optical_jobs"] == 0
    assert manifest["collection_efficiency"]["values"] == (0.5, 0.75, 1.0)
    assert manifest["production_jobs"] == 0
    runner = sensitivity._load_runner(Path(manifest["solver_snapshot"]))
    first = manifest["jobs"][0]
    assert first["solver_code_sha256"] == runner.file_hash(manifest["solver_snapshot"])
    assert first["bundle_file_sha256"] == runner.file_hash(first["bundle_path"])
    assert first["solver_path"] == manifest["solver_snapshot"]
    assert (
        runner.run_job(tmp_path / "campaign/sensitivity_campaign.json", first["job_id"])["status"]
        == "external_resources_required"
    )
    # A second identical preparation is idempotent; a different resolution cannot overwrite it.
    assert sensitivity.prepare_sensitivity([path], tmp_path / "campaign", resolution=80) == manifest
    with pytest.raises(ValueError, match="Frozen artifact changed"):
        sensitivity.prepare_sensitivity([path], tmp_path / "campaign", resolution=100)


def test_missing_jobs_never_report_zero_sensitivity(bundle_file, tmp_path):
    path, _ = bundle_file
    sensitivity.prepare_sensitivity([path], tmp_path / "campaign")
    report = sensitivity.assess_sensitivity(tmp_path / "campaign/sensitivity_campaign.json")
    assert report["pending_variants"] == 24
    assert all(row["expansion_required"] is None for row in report["variants"])
    assert all("max_relative_response_change" not in row for row in report["variants"])
    assert report["quantitative_ready"] is False


def _write_responses(manifest, variant_id, factor):
    variant = next(row for row in manifest["variants"] if row["variant_id"] == variant_id)
    jobs = {job["job_id"]: job for job in manifest["jobs"]}
    for job_id in variant["job_ids"]:
        job = jobs[job_id]
        scale = factor(job) if callable(factor) else factor
        response = np.array([0.08, 0.12, 0.1, 0.14]) * scale
        result = {
            "job_sha256": job["job_sha256"],
            "synthetic_test_fixture": True,
            **{key: job[key] for key in ("wavelength_nm", "theta_deg", "phi_deg", "polarization")},
            "absorption_cell_fraction": response.tolist(),
            "reflection": 0.1,
            "transmission": 0.8 - float(np.sum(response)),
            "layer_absorption": {"cfa": 0.1, "silicon": float(np.sum(response))},
            "energy_balance_residual": 0.0,
        }
        path = Path(manifest["output_dir"]) / "jobs" / job_id / "result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result))


def test_completed_anchor_change_requires_expansion_without_claiming_validation(
    bundle_file, tmp_path
):
    path, _ = bundle_file
    manifest = sensitivity.prepare_sensitivity([path], tmp_path / "campaign")
    _write_responses(manifest, "baseline", 1.0)
    _write_responses(manifest, "red_k_1.2", 1.06)
    _write_responses(manifest, "red_k_0.8", 1.02)
    _write_responses(
        manifest, "lens_height_1.1", lambda job: 1.03 if job["theta_deg"] > 0 else 0.97
    )
    report = sensitivity.assess_sensitivity(tmp_path / "campaign/sensitivity_campaign.json")
    rows = {row["variant_id"]: row for row in report["variants"]}
    assert rows["red_k_1.2"]["status"] == "expansion_required"
    assert rows["red_k_1.2"]["max_relative_response_change"] == pytest.approx(0.06)
    assert rows["red_k_0.8"]["expansion_required"] is False
    assert rows["lens_height_1.1"]["max_relative_response_change"] == pytest.approx(0.03)
    assert rows["lens_height_1.1"]["max_relative_angular_ratio_change"] > 0.05
    assert rows["lens_height_1.1"]["expansion_required"] is True
    assert report["expansion_required_count"] == 2
    assert report["pending_variants"] == 21
    assert not rows["red_k_1.2"]["quantitative_ready"]
    bounds = rows["red_k_1.2"]["electron_response_bounds"]["by_collection_efficiency"]
    np.testing.assert_allclose(bounds[0]["minimum_per_site"], [0.1696, 0.2544, 0.212, 0.2968])
    np.testing.assert_allclose(
        bounds[-1]["maximum_per_site"], 2 * np.array(bounds[0]["minimum_per_site"])
    )


def test_tampered_result_and_manifest_are_rejected(bundle_file, tmp_path):
    path, _ = bundle_file
    manifest = sensitivity.prepare_sensitivity([path], tmp_path / "campaign")
    _write_responses(manifest, "baseline", 1.0)
    job_id = manifest["variants"][0]["job_ids"][0]
    result_path = tmp_path / "campaign/jobs" / job_id / "result.json"
    result = json.loads(result_path.read_text())
    result["job_sha256"] = "wrong"
    result_path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="immutable input job"):
        sensitivity.assess_sensitivity(tmp_path / "campaign/sensitivity_campaign.json")
    _write_responses(manifest, "baseline", 1.0)
    result = json.loads(result_path.read_text())
    result["reflection"] = 0.9
    result_path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="reported energy residual"):
        sensitivity.assess_sensitivity(tmp_path / "campaign/sensitivity_campaign.json")
    manifest_path = tmp_path / "campaign/sensitivity_campaign.json"
    manifest["resolution_px_per_um"] = 40
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest hash"):
        sensitivity.assess_sensitivity(manifest_path)
