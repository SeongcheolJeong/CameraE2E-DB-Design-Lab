from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[2] / "simulations/fdtd_tcad/run_sensor_response.py"
spec = importlib.util.spec_from_file_location("sensor_fdtd_runner_test", RUNNER_PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture
def bundle():
    from pyisetcam.cfa_materials import image_sensor_cfa_materials

    return image_sensor_cfa_materials("DEF-2011-804")


def test_sampling_counts_and_normal_direction_basis():
    assert len(runner.direction_grid(10)) == 33
    assert len(runner.direction_grid(5)) == 65
    assert 31 * len(runner.direction_grid(5)) * 2 == 4030
    assert sum(theta == 0 for theta, _ in runner.direction_grid(5)) == 1


def test_canonical_material_range_is_not_silently_extrapolated(bundle):
    material = bundle["channels"]["green"]
    with pytest.raises(ValueError, match="outside"):
        runner.material_nk(material, 350)
    with pytest.raises(ValueError, match="outside"):
        runner.material_nk(material, float("nan"))
    assert runner.material_nk(material, 550).imag >= 0


def test_planar_reference_matches_independent_fresnel_recursion(bundle):
    from pyisetcam.cfa_materials import planar_cfa_reference

    expected = planar_cfa_reference(bundle, 550)
    actual = runner.planar_tmm(bundle, 550)
    assert actual["reflection"] == pytest.approx(expected["R"], abs=1e-10)
    assert actual["transmission"] == pytest.approx(expected["T"], abs=1e-10)
    assert sum(actual["layer_absorption"].values()) == pytest.approx(expected["A"], abs=1e-10)
    assert actual["layer_absorption"]["lens"] == 0
    assert actual["layer_absorption"]["cfa"] > 0
    assert actual["layer_absorption"]["silicon"] > 0


def test_manifest_freezes_canonical_geometry_and_has_pending_validation_jobs(bundle, tmp_path):
    source = tmp_path / "input.json"
    runner.write_json(source, bundle)
    result = runner.plan_campaign([source], tmp_path / "campaign", sampling="final")
    assert result["production_jobs"] == 4030
    assert {job["role"] for job in result["jobs"]} == {
        "production",
        "planar_calibration",
        "3d_process_check",
        "spatial",
        "time",
        "pml",
        "interpolation",
    }
    assert result["jobs"][0]["geometry_sha256"] == bundle["geometry_sha256"]
    assert Path(result["solver_snapshot"]).is_file()
    assert runner.file_hash(result["solver_snapshot"]) == result["solver_code_sha256"]
    response = runner.assemble_response(tmp_path / "campaign/campaign.json", bundle["report_code"])
    assert response["samples"] == []
    assert response["validation"]["status"] == "unverified"
    assert not any(response["validation"]["gates"].values())
    assert response["validation"]["missing_production_samples"] == 4030
    assert (
        runner.campaign_status(tmp_path / "campaign/campaign.json")["quantitative_ready"] is False
    )
    changed = dict(bundle)
    changed["geometry_um"] = {**bundle["geometry_um"], "si_thickness": 1.0}
    runner.write_json(source, changed)
    with pytest.raises(ValueError, match="changed"):
        runner.plan_campaign([source], tmp_path / "campaign")


def test_resource_limits_reject_unsafe_local_overrides(tmp_path):
    with pytest.raises(ValueError, match="two hours"):
        runner.run_job(tmp_path / "absent.json", max_wall_seconds=7201)
    with pytest.raises(ValueError, match="24 GiB"):
        runner.run_job(tmp_path / "absent.json", max_rss_gib=25)
    with pytest.raises(ValueError, match="four cores"):
        runner.run_job(tmp_path / "absent.json", cores=5)


def test_full_resolution_memory_estimate_is_explicit(bundle):
    estimate = runner.estimate_resources(runner.geometry_from_bundle(bundle), 80)
    assert estimate["cell_count_estimate"] > 1e8
    assert estimate["requires_external_resources"]
    assert estimate["runtime_estimate_seconds"] is None


def test_local_job_lock_prevents_two_simultaneous_jobs(tmp_path):
    with runner._local_job_lock(tmp_path):
        with pytest.raises(RuntimeError, match="Another local"):
            with runner._local_job_lock(tmp_path):
                pass


def test_watchdog_terminates_a_job_at_wall_limit(bundle, tmp_path, monkeypatch):
    source = tmp_path / "input.json"
    runner.write_json(source, bundle)
    runner.plan_campaign([source], tmp_path / "campaign", sampling="initial")
    real_popen = subprocess.Popen

    def sleeping_worker(command, **kwargs):
        return real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", sleeping_worker)
    result = runner.run_job(tmp_path / "campaign/campaign.json", max_wall_seconds=0.1)
    assert result["status"] == "wall_time_limit"
    assert result["wall_seconds"] < 6
    assert result["peak_rss_bytes"] > 0
    assert not Path(result["result_path"]).exists()


def test_failed_energy_cannot_be_marked_passed(bundle, tmp_path):
    source = tmp_path / "input.json"
    runner.write_json(source, bundle)
    manifest = runner.plan_campaign([source], tmp_path / "campaign", sampling="initial")
    job = next(job for job in manifest["jobs"] if job["role"] == "production")
    sample = {
        "job_sha256": job["job_sha256"],
        "absorption_cell_fraction": [0.3, 0.3, 0.3, 0.3],
        "reflection": 0.2,
        "transmission": 0.1,
        "layer_absorption": {"lens": 0.0, "cfa": 0.1, "passivation": 0.0, "silicon": 1.2},
    }
    runner.write_json(tmp_path / "campaign/jobs" / job["job_id"] / "result.json", sample)
    response = runner.assemble_response(tmp_path / "campaign/campaign.json", bundle["report_code"])
    assert response["validation"]["evidence"]["energy_balance"]["max_abs_error"] > 0.5
    assert response["validation"]["gates"]["energy_balance"] is False
    assert response["validation"]["status"] == "unverified"


@pytest.mark.parametrize("wave", [400, 550, 700])
@pytest.mark.parametrize("pol", ["s", "p"])
def test_oblique_transfer_matrix_matches_independent_reference(bundle, wave, pol):
    from pyisetcam.cfa_materials import planar_cfa_reference

    expected = planar_cfa_reference(bundle, wave, angle_deg=40, polarization=pol)
    actual = runner.planar_tmm(bundle, wave, angle_deg=40, polarization=pol)
    import json

    json.dumps(actual, allow_nan=False)
    assert actual["reflection"] == pytest.approx(expected["R"], abs=1e-8)
    assert actual["transmission"] == pytest.approx(expected["T"], abs=1e-8)
    for name, reference in zip(
        ("cfa", "passivation", "silicon"), expected["layer_absorption"], strict=True
    ):
        assert actual["layer_absorption"][name] == pytest.approx(reference, abs=1e-8)


def test_weak_site_error_is_not_hidden_by_strong_channel():
    error = runner.response_errors(
        [0.001, 0.2, 0.2, 0.2], [0.0005, 0.2, 0.2, 0.2], [0.1] * 4, [0.1] * 4
    )
    assert error["max_relative_error"] == 0
    assert error["max_weak_response_abs_error"] == pytest.approx(0.002)
    evidence, passed = runner._comparison_evidence([error], True)
    assert evidence["policy"] == "native_response_mixed_v1"
    assert not passed
    strong = runner.response_errors([0.004, 0.2, 0.2, 0.2], [0.003, 0.2, 0.2, 0.2])
    assert strong["max_relative_error"] == pytest.approx(1 / 3)


def test_unstable_normal_denominator_uses_absolute_native_response():
    error = runner.response_errors([0.003] * 4, [0.002] * 4, [1e-15] * 4, [2e-15] * 4)
    assert error["max_angular_ratio_relative_error"] == 0
    assert error["max_angular_ratio_weak_response_abs_error"] == pytest.approx(0.004)
    assert not runner._comparison_evidence([error], True)[1]


def test_mesh_levels_cannot_collapse_into_identical_jobs():
    with pytest.raises(ValueError, match="distinct"):
        runner.spatial_levels(2)
    assert runner.spatial_levels(4) == [3, 4, 5]
    assert runner.spatial_levels(80) == [60, 80, 100]


def test_refined_sampling_and_midpoint_contract():
    assert len(runner.direction_grid(2.5, 11.25)) == 513
    assert len(runner._regular_axis(400, 700, 5, "wavelength")) == 61
    with pytest.raises(ValueError, match="divide"):
        runner.direction_grid(3, 11.25)


@pytest.mark.parametrize("count", [5, 6, 35, 36])
def test_regional_quadrature_preserves_equal_volume_on_odd_and_even_grids(count):
    import numpy as np

    axis = np.linspace(-2.9, 2.9, count)
    weight = np.ones((count, 3, count))
    region = runner.regional_integrals(weight, axis, axis)
    assert np.asarray(region) / np.sum(region) == pytest.approx([0.25] * 4, abs=1e-14)
    assert sum(region) == pytest.approx(weight.sum())


def test_geometry_sensitivity_changes_cfa_and_periodic_lens_centers(bundle):
    import copy

    changed = copy.deepcopy(bundle)
    changed["channels"]["red"]["thickness_um"] *= 1.1
    changed["geometry_um"]["lens_shift_um"] = [0.2, 0]
    geometry = runner.geometry_from_bundle(changed)
    assert (
        geometry["cfa_thickness_by_channel_um"]["red"]
        != geometry["cfa_thickness_by_channel_um"]["green"]
    )
    centers = runner.periodic_lens_centers(geometry)
    assert len(centers) > 4  # Wrapped lens support has a periodic image at the cell edge.
    assert geometry["cfa_thickness_rule"].startswith("fixed CFA top")


def test_portable_external_bundle_and_result_import_roundtrip(bundle, tmp_path):
    import json
    import os
    import shutil

    source = tmp_path / "input.json"
    runner.write_json(source, bundle)
    manifest = runner.plan_campaign([source], tmp_path / "local", sampling="initial")
    manifest_path = tmp_path / "local/campaign.json"
    runner.prepare_external(manifest_path, tmp_path / "export")
    moved = tmp_path / "remote host"
    shutil.move(tmp_path / "export", moved)
    job = manifest["jobs"][0]
    # A tiny fake executable verifies the portable launch arguments and emits a
    # coordinate-complete sample. It does not impersonate a numerical solver check.
    fake = tmp_path / "dummy_worker.py"
    fake.write_text("""#!/usr/bin/env python3
import json,sys,pathlib
args=sys.argv[1:]
def arg(name):return args[args.index(name)+1]
manifest=json.loads(pathlib.Path(arg('--manifest')).read_text())
job=next(j for j in manifest['jobs'] if j['job_id']==arg('--job-id'))
assert pathlib.Path(arg('--bundle-dir'),job['report_code']+'.json').is_file()
assert pathlib.Path(args[0]).is_file()
keys=('job_id','job_sha256','wavelength_nm','theta_deg','phi_deg','polarization','role')
result={k:job[k] for k in keys}
result.update(schema='camerae2e_fdtd_solver_sample_v1',dummy_process_test=True)
pathlib.Path(arg('--output')).write_text(json.dumps(result))
""")
    fake.chmod(0o755)
    subprocess.run(
        [str(moved / "run_job.sh"), job["job_id"]],
        env={**os.environ, "MEEP_PYTHON": str(fake)},
        check=True,
    )
    imported = runner.import_results(manifest_path, moved / "results")
    assert imported["imported"] == 1 and not imported["quantitative_ready"]
    saved = json.loads((tmp_path / "local/jobs" / job["job_id"] / "result.json").read_text())
    assert saved["job_sha256"] == job["job_sha256"]
    saved["job_sha256"] = "wrong"
    runner.write_json(moved / "results" / f"{job['job_id']}.json", saved)
    with pytest.raises(ValueError, match="identity"):
        runner.import_results(manifest_path, moved / "results")


def test_external_manifest_cannot_be_silently_replaced(bundle, tmp_path):
    import json

    source = tmp_path / "input.json"
    runner.write_json(source, bundle)
    runner.plan_campaign([source], tmp_path / "a", sampling="initial", resolution=80)
    runner.plan_campaign([source], tmp_path / "b", sampling="initial", resolution=60)
    external = tmp_path / "external"
    runner.prepare_external(tmp_path / "a/campaign.json", external)
    before = (external / "campaign.json").read_bytes()
    with pytest.raises(ValueError, match="External campaign changed"):
        runner.prepare_external(tmp_path / "b/campaign.json", external)
    assert (external / "campaign.json").read_bytes() == before
    manifest = json.loads((tmp_path / "a/campaign.json").read_text())
    Path(manifest["jobs"][0]["bundle_path"]).write_text("{}")
    with pytest.raises(ValueError, match="Source material bundle hash"):
        runner.prepare_external(tmp_path / "a/campaign.json", tmp_path / "changed")
