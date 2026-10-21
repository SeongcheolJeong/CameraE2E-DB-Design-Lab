from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import sensor_structure_spectral as spectral

WORKER = Path(__file__).parents[2]/"simulations/fdtd_tcad/sensor_structure_periodic_spectral_worker.py"
_spec = importlib.util.spec_from_file_location("periodic_spectral_worker_under_test", WORKER)
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)


def bundle():
    geometry = dict(pitch_um=2.9, si_um=5., ocl_height_um=.8,
        ocl_semiaxis_um=1.2, cfa_um=.5, passivation_um=.3,
        dti_depth_um=4., dti_width_um=.22, dti_oxide_liner_um=.065)
    tables = {name: dict(wavelength_nm=[450., 550., 650.],
                        n=[1.5, 1.5, 1.5], k=[.1, .2, .3])
              for name in ("silicon", "passivation", "lens", "red", "green", "blue")}
    return dict(report_code="DEF-2206-805", revision="fixed-structure", geometry=geometry, materials=tables)


def task():
    return spectral.task_list(bundle(), {"center": {450: 0}},
                              resolutions=[60], wavelengths=[450], solver_sha256="solver")[0]


def test_all_color_and_wavelength_combinations_use_exact_physical_angles():
    angles = {"center": {450: 0, 550: 0, 650: 0},
              "right-edge": {450: 4.519230321611206, 550: 4.524756324933152,
                             650: 4.525141720848977}}
    tasks = spectral.task_list(bundle(), angles)
    assert len(tasks) == 144
    assert len({t["id"] for t in tasks}) == 144
    assert {t["resolution"] for t in tasks[:72]} == {60}
    assert {(t["cfa_channel"], t["wavelength_nm"]) for t in tasks} == {
        (color, wave) for color in spectral.COLORS for wave in spectral.WAVELENGTHS}
    for t in tasks:
        assert t["angle_deg"] == angles[t["field"]][t["wavelength_nm"]]
        worker.validate(t)
    changed = copy.deepcopy(angles)
    changed["right-edge"][450] += 1e-9
    updated = spectral.task_list(bundle(), changed)
    assert tasks[36]["id"] != updated[36]["id"]


@pytest.mark.parametrize("field_angles", [{}, {"edge": {550: 4.5}}, {"edge": {450: 41}}, {"edge": {450: np.nan}}])
def test_missing_or_invalid_exact_angle_is_not_interpolated(field_angles):
    with pytest.raises(ValueError):
        spectral.task_list(bundle(), field_angles, resolutions=[60], wavelengths=[450])


@pytest.mark.parametrize("meshes", [[60, 60], [60.5], [29], [201], [np.nan]])
def test_invalid_mesh_is_rejected(meshes):
    with pytest.raises(ValueError):
        spectral.task_list(bundle(), {"center": {450: 0}}, resolutions=meshes, wavelengths=[450])


@pytest.mark.parametrize("color", spectral.COLORS)
@pytest.mark.parametrize("variant", spectral.VARIANTS)
def test_scalar_and_vector_geometry_match_without_hidden_channel_substitution(color, variant):
    t = task()
    t.update(cfa_channel=color, variant=variant)
    rng = np.random.default_rng(617)
    x, y = rng.uniform(-4, 4, 1000), rng.uniform(-6, 2, 1000)
    materials, roles = worker.classify_arrays(t, x, y)
    for i in range(len(x)):
        material, role = worker.classify_point(t, x[i], y[i])
        assert materials[i] == material
        assert roles[i] == worker.ROLES.index(role)
    # Cross-section repeats physically, including the shifted lens cap.
    shifted_material, shifted_role = worker.classify_arrays(t, x+2.9, y)
    np.testing.assert_array_equal(materials, shifted_material)
    np.testing.assert_array_equal(roles, shifted_role)


def test_rb_missing_thickness_is_passivation_and_green_retains_reported_thickness():
    t = task()
    assert worker.classify_point(t, 0, .31) == ("passivation", "passivation")
    assert worker.classify_point(t, 0, .4) == ("red", "cfa")
    t["cfa_channel"] = "green"
    assert worker.classify_point(t, 0, .31) == ("green", "cfa")
    t["cfa_channel"] = "blue"
    assert worker.classify_point(t, 0, .4) == ("blue", "cfa")


def test_ocl_shift_changes_only_lens_placement():
    t = task()
    x, y = np.meshgrid(np.linspace(-1.45, 1.45, 129), np.linspace(-5, 1.8, 257))
    before_m, before_r = worker.classify_arrays(t, x, y)
    t["variant"] = "ocl_shift"
    after_m, after_r = worker.classify_arrays(t, x, y)
    np.testing.assert_array_equal(before_m[y < .8], after_m[y < .8])
    np.testing.assert_array_equal(before_r[y < .8], after_r[y < .8])
    assert np.any(before_m[y >= .8] != after_m[y >= .8])


def test_materials_evaluate_all_cfa_at_requested_wavelength_without_extrapolation():
    t = task()
    values = worker.material_values(t)
    assert values["blue"]["k"] == .1
    t["wavelength_nm"] = 600.
    assert worker.material_values(t)["red"]["k"] == .25
    t["wavelength_nm"] = 700.
    with pytest.raises(ValueError, match="extrapolation"):
        worker.material_values(t)


def sample_file(tmp_path, t):
    path = tmp_path/"sample.json"
    np.savez_compressed(path.with_suffix(".npz"), absorption_density=np.ones((3, 4)),
                        role=np.zeros((3, 4), dtype=np.uint8))
    value = {key: t[key] for key in ("dimension", "source_model", "wavelength_nm", "angle_deg",
                "cfa_channel", "variant", "polarization", "resolution", "control", "after_source_time", "pml_um")}
    value.update(schema=spectral.SAMPLE_SCHEMA, task_id=t["id"], worker_sha256="solver",
                 structure_revision=t["structure"]["revision"], R=.2, T=.2,
                 side_outflow=0., absorption=.6, active_si_absorption=.5,
                 layer_absorption={"active_si": .5, "cfa": .1}, energy_residual=0.,
                 incident_power=1., negative_dissipation_integral=0.,
                 support_tail_fraction=None, camera_response_eligible=False,
                 array_sha256=hashlib.sha256(path.with_suffix(".npz").read_bytes()).hexdigest())
    path.write_text(json.dumps(value))
    return path, value


def test_sample_validation_binds_source_and_arrays_without_qualifying_camera(tmp_path):
    t = task()
    path, _ = sample_file(tmp_path, t)
    result = spectral.validate_sample(t, path)
    assert result["numerical_valid"]
    assert result["camera_response_eligible"] is False
    assert result["support_tail_fraction"] is None
    assert result["field"] == "center"
    path.with_suffix(".npz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        spectral.validate_sample(t, path)


@pytest.mark.parametrize("change", [
    {"worker_sha256": "other"}, {"cfa_channel": "green"}, {"wavelength_nm": 550.},
    {"camera_response_eligible": True}, {"support_tail_fraction": 0.},
    {"energy_residual": .1}, {"active_si_absorption": .2}])
def test_identity_and_physical_accounting_mismatches_rejected(tmp_path, change):
    t = task()
    path, value = sample_file(tmp_path, t)
    value.update(change)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        spectral.validate_sample(t, path)


def test_one_percent_energy_boundary_is_not_relaxed_for_trend(tmp_path):
    t = task()
    path, value = sample_file(tmp_path, t)
    value.update(T=.18, energy_residual=.02)
    path.write_text(json.dumps(value))
    result = spectral.validate_sample(t, path)
    assert result["numerical_valid"] is False


def pair_samples(base=(.2, .4), changed=(.22, .48), valid=True):
    return [dict(field="center", wavelength_nm=450., angle_deg=0., cfa_channel="red",
                 resolution=60, control="mesh", variant=variant, polarization=pol,
                 active_si_absorption=values[i], numerical_valid=valid)
            for variant, values in (("reviewed", base), ("ocl_shift", changed))
            for i, pol in enumerate(("s", "p"))]


def test_unpolarized_ratio_is_ratio_of_averaged_powers_not_mean_of_ratios():
    row = spectral.ratios(pair_samples())[0]
    assert row["gain"] == pytest.approx(.35/.3)
    assert row["gain"] != pytest.approx((1.1+1.2)/2)
    assert row["camera_response_eligible"] is False
    assert len(row["polarized"]) == 2


@pytest.mark.parametrize("samples", [pair_samples()[:3], pair_samples()+pair_samples()[:1], pair_samples(valid=False)])
def test_missing_duplicate_or_invalid_pair_is_never_gain(samples):
    row = spectral.ratios(samples)[0]
    assert row["gain"] is None
    assert row["numerical_valid"] is False


def test_weak_response_preserves_absolute_difference_without_exaggerated_gain():
    row = spectral.ratios(pair_samples(base=(.001, .003), changed=(.002, .004)))[0]
    assert row["weak_response"]
    assert row["gain"] is None
    assert row["absolute_delta"] == pytest.approx(.001)


def test_campaign_atomic_resume_and_persistent_deadline(tmp_path, monkeypatch):
    import argparse
    import contextlib
    module_spec = importlib.util.spec_from_file_location("spectral_campaign_test", WORKER.parents[2]/"tools/run_ocl_spectral_campaign.py")
    campaign = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(campaign)
    task_value = task()
    task_value["worker_sha256"] = hashlib.sha256(WORKER.read_bytes()).hexdigest()
    from camera_platform.registry import digest
    task_value["id"] = digest({k: v for k, v in task_value.items() if k != "id"})
    tasks_path = tmp_path/"tasks.json"
    tasks_path.write_text(json.dumps([task_value]))
    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path/"ssd"))
    project = tmp_path/"ssd"
    project.mkdir()
    @contextlib.contextmanager
    def reserved(*args, **kwargs):
        yield project, lambda: 0
    monkeypatch.setattr(campaign.storage, "reserve", reserved)
    monkeypatch.setattr(campaign, "other_meep_processes", lambda: [])
    calls = []
    class Process:
        returncode = 0
        def __init__(self, argv, **kwargs):
            calls.append(argv)
            value = json.loads(Path(argv[-2]).read_text())
            target = Path(argv[-1])
            fixture, sample = sample_file(tmp_path, value)
            sample["worker_sha256"] = value["worker_sha256"]
            sample["elapsed_s"] = 1.25
            target.write_text(json.dumps(sample))
            target.with_suffix(".npz").write_bytes(fixture.with_suffix(".npz").read_bytes())
        def poll(self):
            return 0
    monkeypatch.setattr(campaign.subprocess, "Popen", Process)
    args = argparse.Namespace(workspace=str(WORKER.parents[2]), tasks=str(tasks_path),
        output=str(project/"campaign"), status=str(tmp_path/"progress.json"), max_seconds=28800)
    first = campaign.run(args)
    ledger_path = project/"campaign/ledger.json"
    ledger = json.loads(ledger_path.read_text())
    assert first["status"] == "completed"
    assert first["completed"] == 1 and len(calls) == 1
    assert first["numerical_valid"] == 1
    assert len(list((project/"campaign/samples").glob("*.partial.json"))) == 0
    second = campaign.run(args)
    assert second["completed"] == 1 and len(calls) == 1
    assert json.loads(ledger_path.read_text())["deadline_epoch"] == ledger["deadline_epoch"]
    # Budget mutation cannot give the completed campaign a new allowance.
    args.max_seconds = 28799
    with pytest.raises(ValueError, match="cannot reset"):
        campaign.run(args)
