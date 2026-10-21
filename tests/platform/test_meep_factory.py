"""Contract refusals plus actual serial-Meep checks against independent slab optics."""

import cmath
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from camera_platform import meep_factory as factory


def test_preflight_works_without_importing_meep_and_preserves_input():
    request = factory.request_template()
    original = copy.deepcopy(request)
    clean = factory.validate_request(request)
    assert request == original == clean
    assert clean is not request
    assert "meep" not in sys.modules
    assert factory.estimate_resources(clean)["simulation_count"] == 8


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda r: r.update(incidence_deg=10), "incidence_deg"),
        (lambda r: r.update(ambient_index=1.5), "ambient_index"),
        (lambda r: r.update(polarization="unpolarized"), "polarization"),
        (lambda r: r.update(executable="/bin/sh"), "exactly"),
        (lambda r: r.update(wavelength_nm=[550, 450]), "increasing"),
        (lambda r: r.update(wavelength_nm=[550, 550]), "increasing"),
        (lambda r: r.update(wavelength_nm=[float("nan")]), "finite"),
        (lambda r: r.update(wavelength_nm=["550"]), "finite"),
        (lambda r: r.update(wavelength_nm=[True]), "finite"),
        (lambda r: r.update(wavelength_nm=[200]), "wavelength_nm"),
        (lambda r: r["layers"][0]["optical_constants"].update(k=-0.1), "k"),
        (lambda r: r["layers"][0]["optical_constants"].update(n=1, k=1), "positive-real"),
        (lambda r: r["layers"][0]["optical_constants"].update(n=float("inf")), "finite"),
        (lambda r: r["layers"][0].update(qe=0.8), "exactly"),
        (lambda r: r["layers"][0].pop("provenance"), "exactly"),
        (lambda r: r["layers"][0]["provenance"].update(source_uri=""), "nonempty"),
        (lambda r: r["layers"][0].update(thickness_nm=20), "four baseline"),
        (lambda r: r["simulation"].update(resolution_px_per_um=20), "eight baseline"),
        (lambda r: r["simulation"].update(convergence_tolerance=0.5), "convergence_tolerance"),
        (lambda r: r["simulation"].update(after_source_time=True), "finite"),
    ],
)
def test_unsupported_or_poisoned_physics_is_refused_before_backend(mutate, match):
    request = factory.request_template()
    mutate(request)
    with pytest.raises(ValueError, match=match):
        factory.validate_request(request)


def test_material_tables_require_exact_samples_no_nearest_or_extrapolation():
    request = factory.request_template()
    request["wavelength_nm"] = [450, 650]
    request["simulation"]["resolution_px_per_um"] = 40
    request["layers"][0]["optical_constants"] = {
        "wavelength_nm": [450, 650],
        "n": [2, 2.1],
        "k": [0.08, 0.12],
    }
    assert factory.validate_request(request) == request
    request["layers"][0]["optical_constants"]["wavelength_nm"] = [450, 600]
    with pytest.raises(ValueError, match="exactly match"):
        factory.validate_request(request)


def test_geometry_and_cpu_cost_are_bounded():
    request = factory.request_template()
    request["simulation"]["resolution_px_per_um"] = 400
    with pytest.raises(ValueError, match="cell-step budget"):
        factory.validate_request(request)
    request = factory.request_template()
    request["layers"] = [
        {**copy.deepcopy(request["layers"][0]), "name": str(i), "thickness_nm": 5000}
        for i in range(3)
    ]
    with pytest.raises(ValueError, match="10 um"):
        factory.validate_request(request)


def test_resolution_pml_and_time_checks_vary_one_quantity_independently():
    configs = factory.configurations(factory.request_template())
    baseline = configs[0]
    for config, field in zip(
        configs[1:], ["resolution_px_per_um", "pml_thickness_um", "after_source_time"], strict=True
    ):
        assert {k for k in config if config[k] != baseline[k]} == {"name", field}
        assert config[field] > baseline[field]


def _assessment_rows():
    row = {
        "R": 0.2,
        "T": 0.5,
        "A": 0.3,
        "layer_absorption": [0.3],
        "energy_residual": 0.0,
        "normalization_residual": 0.0,
    }
    return [[copy.deepcopy(row)] for _ in range(4)]


def test_negative_absorption_is_retained_and_cannot_become_converged_by_abs_or_clipping():
    rows = _assessment_rows()
    rows[0][0]["A"] = rows[0][0]["layer_absorption"][0] = -0.001
    original = copy.deepcopy(rows)
    result = factory.assess_results(factory.request_template(), rows)
    assert result["status"] == "INCONCLUSIVE"
    assert not result["checks"][0]["passivity_pass"]
    assert result["checks"][0]["negative_values_retained"]
    assert rows == original


def test_unconverged_resolution_and_failed_energy_are_distinct():
    rows = _assessment_rows()
    rows[1][0]["R"] += 0.05
    result = factory.assess_results(factory.request_template(), rows)
    assert result["numerical_checks_pass"]
    assert not result["convergence_checks_pass"]
    assert result["status"] == "INCONCLUSIVE"
    rows = _assessment_rows()
    rows[2][0]["energy_residual"] = 0.03
    result = factory.assess_results(factory.request_template(), rows)
    assert not result["numerical_checks_pass"]
    assert result["convergence_checks_pass"]


def test_numerical_roundoff_is_reported_with_explicit_tolerance_without_changing_value():
    rows = _assessment_rows()
    for group in rows:
        group[0].update(A=-1e-8, layer_absorption=[-1e-8])
    result = factory.assess_results(factory.request_template(), rows)
    assert result["passivity_tolerance"] == 1e-6
    assert result["checks"][0]["negative_values_retained"]
    assert rows[0][0]["A"] == -1e-8


def test_duplicate_json_keys_and_nonfinite_constants_cannot_be_parsed(tmp_path):
    path = tmp_path / "input.json"
    for text in ['{"n":1,"n":2}', '{"n":NaN}']:
        path.write_text(text)
        with pytest.raises(ValueError):
            factory._read_json(path)


def _interpreter():
    candidate = os.environ.get("CAMERA_PLATFORM_MEEP_PYTHON")
    path = Path(candidate) if candidate else Path.home() / "FDTD/.meep-env/bin/python"
    if not path.is_file():
        pytest.skip("Serial Meep interpreter is not installed; no solver evidence claimed")
    return path


def _run_cli(request, tmp_path):
    source, target = tmp_path / "request.json", tmp_path / "output.json"
    source.write_text(json.dumps(request))
    result = subprocess.run(
        [str(_interpreter()), factory.__file__, "--request", str(source), "--output", str(target)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["output"] == str(target)
    return json.loads(target.read_text())


def _slab_exact(n, k, thickness_nm, wavelength_nm):
    index = complex(n, k)
    r01, r12 = (1 - index) / (1 + index), (index - 1) / (index + 1)
    phase = cmath.exp(2j * cmath.pi * index * thickness_nm / wavelength_nm)
    denom = 1 + r01 * r12 * phase**2
    r = (r01 + r12 * phase**2) / denom
    t = 2 / (1 + index) * 2 * index / (index + 1) * phase / denom
    reflectance, transmittance = abs(r) ** 2, abs(t) ** 2
    return reflectance, transmittance, 1 - reflectance - transmittance


@pytest.mark.parametrize("n,k", [(1.0, 0.0), (2.0, 0.0), (2.0, 0.1)])
def test_actual_meep_matches_independent_fresnel_slab_and_never_returns_qe(n, k, tmp_path):
    request = factory.request_template()
    request["layers"][0]["optical_constants"] = {"n": n, "k": k}
    result = _run_cli(request, tmp_path)
    summary = result["summary"]
    assert summary["status"] == "NUMERICALLY_CONVERGED"
    assert summary["backend"]["meep_version"] == "1.33.0"
    assert summary["resources"]["simulation_count"] == 8
    assert not summary["physical_validation_pass"]
    assert summary["uncertainty"] == {"kind": "unknown"}
    assert result["model_candidate"] is None
    assert summary["metric"] == "optical.absorbed_power_fraction"
    expected = _slab_exact(n, k, 200, 550)
    for group in result["arrays"]["results"]:
        row = group[0]
        assert [row[q] for q in ["R", "T", "A"]] == pytest.approx(expected, abs=0.01)
        assert abs(row["energy_residual"]) < 1e-5
        assert row["incident_flux"] > 0
        assert len(row["stack_flux"]) == len(row["reference_flux"]) == 4


def test_probe_is_machine_readable_without_running_a_simulation():
    process = subprocess.run(
        [str(_interpreter()), factory.__file__, "--probe"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr
    probe = json.loads(process.stdout)
    assert probe["available"] and probe["processor_count"] == 1
    assert probe["mpi_enabled"] is False
    assert len(probe["adapter_sha256"]) == 64


def test_actual_per_wavelength_material_values_are_used_without_interpolation(tmp_path):
    request = factory.request_template()
    request["wavelength_nm"] = [550.0, 650.0]
    request["simulation"]["resolution_px_per_um"] = 60
    request["layers"][0]["optical_constants"] = {
        "wavelength_nm": [550.0, 650.0],
        "n": [2.0, 2.1],
        "k": [0.1, 0.2],
    }
    result = _run_cli(request, tmp_path)
    assert result["arrays"]["wavelength_nm"] == [550.0, 650.0]
    for row, wave, n, k in zip(
        result["arrays"]["results"][1], [550.0, 650.0], [2.0, 2.1], [0.1, 0.2], strict=True
    ):
        assert row["material_values"][0]["n"] == n
        assert row["material_values"][0]["k"] == k
        assert [row[q] for q in ["R", "T", "A"]] == pytest.approx(
            _slab_exact(n, k, 200.0, wave), abs=0.01
        )


def test_actual_two_lossy_layers_have_ordered_flux_partition_and_conserve_energy(tmp_path):
    request = factory.request_template()
    request["simulation"]["resolution_px_per_um"] = 60
    second = copy.deepcopy(request["layers"][0])
    second.update(name="Separately declared second layer", optical_constants={"n": 1.5, "k": 0.05})
    request["layers"].append(second)
    result = _run_cli(request, tmp_path)
    assert result["arrays"]["layer"] == [layer["name"] for layer in request["layers"]]
    for group in result["arrays"]["results"]:
        row = group[0]
        assert len(row["layer_absorption"]) == 2
        assert all(value > 0 for value in row["layer_absorption"])
        assert sum(row["layer_absorption"]) == pytest.approx(row["A"], abs=1e-14)
        assert row["R"] + row["T"] + row["A"] == pytest.approx(1, abs=1e-5)
    assert result["model_candidate"] is None
