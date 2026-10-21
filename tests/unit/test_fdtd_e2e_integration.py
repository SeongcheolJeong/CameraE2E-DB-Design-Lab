"""Public DB/FACA/benchmark boundaries using explicitly synthetic optical fixtures."""

import json

import numpy as np
import pytest

from pyisetcam import (
    camera_get,
    camerae2e_compare_fdtd_sensors,
    camerae2e_run_scenario,
    hw_isp_simulate_frame,
    image_sensor_cfa_materials,
    image_sensor_db_config,
    scene_create,
    sensor_pupil_directions,
)


def _fixture_lut(tmp_path):
    bundle = image_sensor_cfa_materials("DEF-2011-804")
    rows = []
    for wavelength in (400, 550, 700):
        # Distinct spectra keep the fixed sensor-to-XYZ fit nondegenerate.
        spectrum = np.array({400: [.01, .025, .025, .16],
                             550: [.03, .18, .18, .02],
                             700: [.18, .035, .035, .01]}[wavelength])
        for theta in (0, 20, 40):
            for phi in ((0,) if theta == 0 else (0, 90, 180, 270)):
                for pol in ("s", "p"):
                    absorption = spectrum * (1 - theta / 100)
                    rows.append({
                        "wavelength_nm": wavelength, "theta_deg": theta, "phi_deg": phi,
                        "polarization": pol, "absorption_cell_fraction": absorption.tolist(),
                        "reflection": .1, "transmission": .8 - float(absorption.sum()),
                        "layer_absorption": {"cfa": .1},
                    })
    evidence = {}
    for name in ("energy_balance", "spatial", "time", "pml", "interpolation"):
        evidence[name] = {"sample_count": len(rows),
                          "max_abs_error" if name == "energy_balance"
                          else "max_relative_error": 0.0}
        if name != "energy_balance":
            evidence[name].update({
                "max_angular_ratio_relative_error": 0.0,
                "comparisons": [{"wavelength_nm": r["wavelength_nm"],
                                 "theta_deg": r["theta_deg"]} for r in rows],
            })
    payload = {
        "schema": "camerae2e_fdtd_response_v1", "sensor_id": bundle["sensor_id"],
        "report_code": bundle["report_code"], "pixel_pitch_um": bundle["pixel_pitch_um"],
        "cell_pixels": [2, 2], "site_names": ["R", "Gr", "Gb", "B"],
        "material_bundle_sha256": bundle["material_bundle_sha256"],
        "geometry_sha256": bundle["geometry_sha256"], "solver_code_sha256": "1" * 64,
        "estimate_only": True, "provenance": {"source_kind": "synthetic_unit_test"},
        "samples": rows, "validation": {
            "status": "passed", "gates": dict.fromkeys(evidence, True), "evidence": evidence,
        },
    }
    path = tmp_path / "synthetic_only.json"
    path.write_text(json.dumps(payload))
    return path


def test_fdtd_config_requires_explicit_validated_inputs():
    with pytest.raises(ValueError, match="lut_path"):
        image_sensor_db_config("DEF-2011-804", model="fdtd_estimate_v1")


def test_fdtd_public_faca_uses_spectral_angle_response(tmp_path):
    path = _fixture_lut(tmp_path)
    results = []
    for theta in (0, 20):
        config = image_sensor_db_config(
            "DEF-2011-804", model="fdtd_estimate_v1", lut_path=path,
            sensor_illumination={"directions": [{"theta_deg": theta, "phi_deg": 0,
                                                 "weight": 1}]},
            assumptions={"read_noise_e": 3},
        )
        scenario = {**config["scenario"], "scene": {"type": "uniform ee", "args": [8]},
                    "sensor": {"noise_flag": 0, "integration_time": .001}}
        results.append(camerae2e_run_scenario(scenario, include_arrays=False))
    sensors = [camera_get(r["camera"], "sensor") for r in results]
    np.testing.assert_allclose(
        sensors[1].data["mean_electrons"], .8 * sensors[0].data["mean_electrons"], rtol=1e-6,
    )
    assert sensors[0].fields["sensor_readout"]["read_noise_e"] == 3
    assert results[0]["sensor_estimate"]["kind"] == "fdtd_estimate_v1"
    assert results[0]["stages"]["sensor_raw"]["unit"] == "DN"
    for r in results:
        assert r["scenario"]["image_sensor_db"]["optical_response_binding"]["scope"] == (
            "sensor_specific"
        )
    a, b = [camera_get(r["camera"], "ip") for r in results]
    np.testing.assert_allclose(
        a.fields["sensor_conversion_matrix"], b.fields["sensor_conversion_matrix"],
    )
    hw_frame = hw_isp_simulate_frame(
        results[0]["camera"], scene_create("uniform ee", 8), sensor_resize=False,
    )
    assert hw_frame.sensor.fields["filter_names"] == ["R", "Gr", "Gb", "B"]
    assert hw_frame.sensor.data["raw_dn"].dtype == np.int32
    assert len(hw_frame.controls_applied["produced_stats"]["sensor_raw_rgb_means"]) == 3


def test_pupil_quadrature_is_normalized_and_contains_angle_extent():
    rays = sensor_pupil_directions(20, 45, 2)["directions"]
    assert sum(r["weight"] for r in rays) == pytest.approx(1)
    assert min(r["theta_deg"] for r in rays) < 20 < max(r["theta_deg"] for r in rays)
    with pytest.raises(ValueError, match="nonpropagating"):
        sensor_pupil_directions(80, 0, 1)


def test_missing_fdtd_report_is_pending_without_fabricated_cases(tmp_path):
    report = camerae2e_compare_fdtd_sensors(
        {}, output_dir=tmp_path, sensor_ids=("DEF-2011-804",), nframes=2,
        output_size=(8, 8),
    )
    assert report["status"] == "pending"
    assert report["case_count"] == report["sample_count"] == 0
    assert report["artifacts"]["arrays"] is None


def test_fdtd_benchmark_stores_actual_integer_raw(tmp_path):
    path = _fixture_lut(tmp_path)
    report = camerae2e_compare_fdtd_sensors(
        {"DEF-2011-804": path}, sensor_ids=("DEF-2011-804",),
        output_dir=tmp_path / "comparison", nframes=2, output_size=(8, 8),
        illuminants=("D65",), scene_types=("uniform",), fnumbers=(4,),
        cra_degrees=(0, 20), azimuth_degrees=(0,),
    )
    assert report["status"] == "complete" and report["case_count"] == 6
    with np.load(report["artifacts"]["arrays"]) as arrays:
        assert arrays["case_00000_raw_dn"].dtype == np.int32
        assert np.all(arrays["case_00000_raw_variance"] >= 0)
    cases = [c for c in report["cases"] if c["mode"] == "fdtd_pupil"]
    assert cases[1]["mean_photoelectrons"] < cases[0]["mean_photoelectrons"]
    assert all(c["pupil_convergence"]["passed"] for c in cases)
    assert [s["filter_name"] for s in cases[0]["native_cfa_sites"]] == ["R", "Gr", "Gb", "B"]


def test_cie_standard_illuminants_and_macbeth_run_without_external_assets(tmp_path):
    from pyisetcam.sensor_fdtd_comparison import _comparison_illuminant

    path = _fixture_lut(tmp_path)
    wave = np.arange(400, 701, 10)
    for name in ("A", "F11"):
        spectrum, ideal, provenance = _comparison_illuminant(name, wave)
        assert np.all(spectrum > 0) and spectrum.shape == (31,)
        assert ideal["name"] == name and provenance["bin_width_nm"] == 10
        assert provenance["source_url"].startswith("https://files.cie.co.at/")
    report = camerae2e_compare_fdtd_sensors(
        {"DEF-2011-804": path}, sensor_ids=("DEF-2011-804",),
        output_dir=tmp_path / "colors", nframes=2, output_size=(12, 16),
        illuminants=("A", "F11"), scene_types=("macbeth_d65",), fnumbers=(4,),
        cra_degrees=(0,), azimuth_degrees=(0,),
    )
    assert report["case_count"] == 6
    assert all("color" in c for c in report["cases"])


def test_saturated_burst_is_not_eligible_for_snr_comparison(tmp_path):
    path = _fixture_lut(tmp_path)
    report = camerae2e_compare_fdtd_sensors(
        {"DEF-2011-804": path}, sensor_ids=("DEF-2011-804",),
        output_dir=tmp_path / "clipping", nframes=3, output_size=(8, 8),
        illuminants=("D65",), scene_types=("uniform",), fnumbers=(4,),
        cra_degrees=(0,), azimuth_degrees=(0,), exposure_s=100,
    )
    assert all(not c["snr_eligible"] for c in report["cases"])
    assert all(c["saturation"]["well_fraction"] == 1 for c in report["cases"])
    assert all("clipped capture" in c["snr_interpretation"] for c in report["cases"])
