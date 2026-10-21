"""Practical model selection, readout and four-site RAW across public APIs."""

import copy
import json

import numpy as np
import pytest

from pyisetcam import (
    camera_get,
    camerae2e_run_scenario,
    hw_isp_simulate_frame,
    image_sensor_db_config,
    scene_create,
)
from pyisetcam.image_sensor_db import image_sensor_db_reconcile_scenario


def _scenario(**options):
    config = image_sensor_db_config("DEF-2011-804", **options)
    scenario = config["scenario"]
    scenario.update(
        scene={"type": "uniform ee", "args": [8]},
        sensor={"noise_flag": 0, "integration_time": .001},
    )
    return scenario


@pytest.mark.parametrize("code", ["DEF-1807-801", "DEF-2011-804", "DEF-2206-805"])
def test_default_uses_fast_physics_with_explicit_normal_incidence_assumption(code):
    config = image_sensor_db_config(code)
    assert config["model"] == "physical_estimate_v1"
    assert config["model_selection"]["selected"] == "physical_estimate_v1"
    assert config["scenario"]["image_sensor_db"]["illumination_assumption"] == "normal_incidence"
    assert config["policy"]["estimate_only"] is True
    assert not config["optical_response_binding"]["measured_accuracy_verified"]
    assert not config["scenario"]["fdtd"]["enabled"]
    json.dumps(config, allow_nan=False)


def test_auto_fallback_reports_missing_geometry_and_rejects_discarded_settings(monkeypatch):
    import pyisetcam.cfa_materials as materials

    def missing(*args, **kwargs):
        raise FileNotFoundError("No local canonical stack")

    monkeypatch.setattr(materials, "image_sensor_cfa_materials", missing)
    config = image_sensor_db_config("DEF-2011-804")
    assert config["model"] == "db_estimate_v1"
    assert "No local canonical stack" in config["model_selection"]["reason"]
    with pytest.raises(ValueError, match="cannot be silently applied"):
        image_sensor_db_config("DEF-2011-804", collection_efficiency=.6)


def test_legacy_total_efficiency_is_never_reinterpreted_as_collection():
    config = image_sensor_db_config("DEF-2011-804", assumptions={"effective_efficiency": .5})
    assert config["model"] == "db_estimate_v1"
    assert config["scenario"]["sensor_model"]["assumptions"]["effective_efficiency"] == .5
    with pytest.raises(ValueError, match="Unknown readout profile overrides"):
        image_sensor_db_config(
            "DEF-2011-804", model="physical_estimate_v1",
            assumptions={"effective_efficiency": .5},
        )
    with pytest.raises(ValueError, match="legacy total effective_efficiency"):
        image_sensor_db_config(
            "DEF-2011-804", model="db_estimate_v1", collection_efficiency=.5,
        )


def test_bad_physical_settings_raise_without_automatic_fallback():
    with pytest.raises(ValueError, match="cfa_thickness_scale"):
        image_sensor_db_config("DEF-2011-804", optical_assumptions={"cfa_thickness_scale": -1})


def test_saved_scenario_rejects_changed_reviewed_evidence():
    scenario = _scenario()
    assert len(scenario["sensor_model"]["reviewed_evidence_sha256"]) == 64
    scenario["sensor_model"]["reviewed_evidence_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="TechInsights evidence changed"):
        camerae2e_run_scenario(scenario, include_arrays=False)


def test_native_profile_operating_point_changes_codes_without_changing_photocharge():
    sensors = []
    for point in ("low_gain", "hcg"):
        result = camerae2e_run_scenario(_scenario(operating_point=point), include_arrays=False)
        sensors.append(camera_get(result["camera"], "sensor"))
        assert result["sensor_estimate"]["kind"] == "physical_estimate_v1"
        binding = result["scenario"]["image_sensor_db"]["optical_response_binding"]
        assert binding["scope"] == "sensor_specific_estimate"
        assert result["stages"]["sensor_raw"]["unit"] == "DN"
    low, high = sensors
    np.testing.assert_allclose(
        low.data["mean_electrons"], high.data["mean_electrons"], rtol=1e-14,
    )
    assert low.fields["sensor_readout"]["full_well_e"] == 11200
    assert high.fields["sensor_readout"]["full_well_e"] == 3500
    assert high.data["raw_dn"].mean() > low.data["raw_dn"].mean()
    assert high.data["raw_dn"].dtype == np.int32
    assert high.metadata["sensor_readout_profile"]["provenance"]["source_operating_point_preserved"]


def test_final_readout_override_recomputes_electron_fpn_and_source_provenance():
    scenario = _scenario(assumptions={"dsnu_e": 3, "prnu_fraction": .02})
    scenario["readout"] = {"full_well_e": 20000, "read_noise_e": 5}
    result = camerae2e_run_scenario(scenario, include_arrays=False)
    sensor = camera_get(result["camera"], "sensor")
    pixel = sensor.fields["pixel"]
    assert pixel["dsnu_sigma_v"] / pixel["conversion_gain_v_per_electron"] == pytest.approx(3)
    assert pixel["prnu_sigma"] == .02
    provenance = sensor.metadata["sensor_readout_profile"]["provenance"]
    assert not provenance["source_operating_point_preserved"]
    assert provenance["fields"]["full_well_e"]["classification"] == "user_assumption"
    assert provenance["fields"]["read_noise_e"]["value"] == 5
    assert not provenance["adc_noise"]["deembedded"]


def test_legacy_sensor_fpn_units_have_precedence_over_profile():
    scenario = _scenario()
    scenario["sensor"].update(prnu_level=2, dsnu_level=.0002, column_fpn=[.0001, .01])
    scenario["readout"] = {"full_well_e": 20000}
    result = camerae2e_run_scenario(scenario, include_arrays=False)
    sensor = camera_get(result["camera"], "sensor")
    assert sensor.fields["pixel"]["prnu_sigma"] == .02
    assert sensor.fields["pixel"]["dsnu_sigma_v"] == pytest.approx(.0002)
    np.testing.assert_allclose(sensor.fields["column_fpn"], [.0001, .01])


def test_angular_response_reaches_raw_while_color_conversion_stays_fixed():
    results = []
    for theta in (0, 25):
        scenario = _scenario(sensor_illumination={
            "directions": [{"theta_deg": theta, "phi_deg": 45, "weight": 1}],
        })
        results.append(camerae2e_run_scenario(scenario, include_arrays=False))
    sensors = [camera_get(result["camera"], "sensor") for result in results]
    assert not np.allclose(sensors[0].data["mean_electrons"], sensors[1].data["mean_electrons"])
    ips = [camera_get(result["camera"], "ip") for result in results]
    np.testing.assert_allclose(
        ips[0].fields["sensor_conversion_matrix"], ips[1].fields["sensor_conversion_matrix"],
    )
    frame = hw_isp_simulate_frame(
        results[1]["camera"], scene_create("uniform ee", 8), sensor_resize=False,
    )
    assert frame.sensor.fields["filter_names"] == ["R", "Gr", "Gb", "B"]
    assert len(frame.controls_applied["produced_stats"]["sensor_raw_rgb_means"]) == 3


def test_descriptor_override_cannot_keep_original_optical_binding():
    original = _scenario()
    scenario = copy.deepcopy(original)
    scenario["sensor_model"]["collection_efficiency"] = .5
    changed = image_sensor_db_reconcile_scenario(scenario)
    assert changed["image_sensor_db"]["optical_response_binding"]["scope"] == "overridden"
    assert original["image_sensor_db"]["optical_response_binding"]["scope"] == (
        "sensor_specific_estimate"
    )
