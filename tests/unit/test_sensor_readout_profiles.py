"""Readout profiles must preserve operating-point and noise-domain semantics."""

import copy
import json

import numpy as np
import pytest

from pyisetcam.sensor import sensor_create, sensor_set
from pyisetcam.sensor_capture import resample_sensor_capture
from pyisetcam.sensor_readout_profiles import apply_readout_profile, resolve_sensor_readout


@pytest.mark.parametrize(
    "sensor,code,capacity,total",
    [
        ("IMX462", "DEF-2011-804", 11200, 2.45),
        ("IMX662", "DEF-2206-805", 38200, 6.9),
    ],
)
def test_default_pairs_capacity_with_noise_at_same_vendor_gain(sensor, code, capacity, total):
    result = resolve_sensor_readout(sensor)
    assert result["report_code"] == code
    assert resolve_sensor_readout(code) == result
    assert result["operating_point"] == "low_gain"
    assert result["readout"]["full_well_e"] == capacity
    assert result["provenance"]["conditions"]["vendor_gain_register"] == 0
    assert result["provenance"]["fields"]["reported_total_read_noise_e"]["value"] == total
    assert result["provenance"]["bare_sensor_measurement"] is False
    assert result["provenance"]["source_operating_point_preserved"] is True
    assert all(len(source["sha256"]) == 64 for source in result["provenance"]["sources"].values())


@pytest.mark.parametrize("sensor,register", [("IMX462", 100), ("IMX662", 300)])
def test_hcg_uses_lower_capacity_without_applying_vendor_gain_twice(sensor, register):
    low = resolve_sensor_readout(sensor)
    high = resolve_sensor_readout(sensor, operating_point="hcg")
    assert high["readout"]["full_well_e"] < low["readout"]["full_well_e"]
    assert high["readout"]["read_noise_e"] < low["readout"]["read_noise_e"]
    assert high["readout"]["gain"] == low["readout"]["gain"] == 1
    assert high["provenance"]["conditions"]["vendor_gain_register"] == register
    assert high["provenance"]["conditions"]["conversion_mode"] == "HCG"


def test_adc_quantization_is_removed_from_reported_total_noise():
    result = resolve_sensor_readout("IMX662")
    adc = result["provenance"]["adc_noise"]
    fields = result["provenance"]["fields"]
    expected = np.sqrt(6.9**2 - 9.3**2 / 12)
    assert result["readout"]["read_noise_e"] == pytest.approx(expected)
    assert adc["deembedded"] is True
    assert adc["simulation_lsb_e"] != adc["source_lsb_e"]
    assert adc["simulation_total_read_noise_e_white_quantization"] == pytest.approx(6.9, rel=0.005)
    lower, upper = fields["read_noise_e"]["range"]
    assert lower < expected < upper


def test_unknown_imx307_electron_parameters_remain_assumptions():
    result = resolve_sensor_readout("IMX307")
    assert result["profile"] == "generic_assumption"
    assert result["operating_point"] == "assumed_reference"
    assert result["provenance"]["classification"] == "unmeasured_assumption"
    assert result["provenance"]["fields"]["full_well_e"]["classification"] == "assumption"
    assert result["provenance"]["adc_noise"]["deembedded"] is False
    assert result["provenance"]["conditions"]["vendor_gain_register"] is None
    with pytest.raises(ValueError, match="Unsupported operating point"):
        resolve_sensor_readout("IMX307", operating_point="hcg")


def test_other_sensor_can_explicitly_use_generic_without_borrowing_camera_measurements():
    result = resolve_sensor_readout("IMX662", profile="generic_assumption")
    assert result["provenance"]["sources"] == {}
    assert result["provenance"]["classification"] == "unmeasured_assumption"
    with pytest.raises(ValueError, match="different sensor"):
        resolve_sensor_readout("IMX307", profile="zwo_asi462mc_v1_1")


@pytest.mark.parametrize(
    "overrides",
    [
        {"vendor_gain": 100},
        {"gain": 0},
        {"full_well_e": -1},
        {"read_noise_e": float("nan")},
        {"bit_depth": 12.5},
        {"prnu_fraction": -0.01},
        {"column_dsnu_e": -1},
        {"black_level_dn": 4095},
    ],
)
def test_invalid_or_ambiguous_parameters_rejected(overrides):
    with pytest.raises(ValueError):
        resolve_sensor_readout("IMX462", overrides=overrides)


def test_override_is_pre_adc_and_reclassifies_source_equivalence():
    result = resolve_sensor_readout("IMX662", overrides={"read_noise_e": 2, "gain": 2})
    assert result["readout"]["read_noise_e"] == 2
    assert result["provenance"]["adc_noise"]["deembedded"] is False
    assert result["provenance"]["source_operating_point_preserved"] is False
    assert result["provenance"]["fields"]["read_noise_e"]["classification"] == "user_assumption"
    assert result["provenance"]["conditions"]["vendor_gain_register"] == 0
    with pytest.raises(ValueError, match="not interpolated"):
        resolve_sensor_readout("IMX462", operating_point="gain50")


def test_fpn_electrons_mapping_and_point_changes_invalidate_maps(asset_store):
    sensor = sensor_create("default", {"size": (16, 16)}, asset_store=asset_store)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", 2)
    sensor.fields["fixed_pattern_seed"] = 21
    sensor.data["mean_electrons"] = np.full((16, 16), 100.0)
    profile = resolve_sensor_readout("IMX462", overrides={"dsnu_e": 2, "prnu_fraction": 0.01})
    result = apply_readout_profile(sensor, profile)
    conversion = result.fields["pixel"]["conversion_gain_v_per_electron"]
    assert result.fields["pixel"]["dsnu_sigma_v"] == 2 * conversion
    assert result.fields["pixel"]["prnu_sigma"] == 0.01
    np.testing.assert_array_equal(result.data["mean_electrons"], sensor.data["mean_electrons"])
    capture = resample_sensor_capture(result, seed=123)
    assert "offset_fpn_image" in capture.fields
    same = apply_readout_profile(capture, profile)
    np.testing.assert_array_equal(
        capture.fields["offset_fpn_image"], same.fields["offset_fpn_image"]
    )
    changed = apply_readout_profile(
        capture, resolve_sensor_readout("IMX462", operating_point="hcg")
    )
    assert "offset_fpn_image" not in changed.fields
    assert "gain_fpn_image" not in changed.fields
    assert changed.fields["fixed_pattern_seed"] == 21
    assert capture.fields["pixel"]["dsnu_sigma_v"] == 2 * conversion
    rerun = resample_sensor_capture(apply_readout_profile(sensor, profile), seed=123)
    np.testing.assert_array_equal(rerun.data["raw_dn"], capture.data["raw_dn"])


@pytest.mark.parametrize("sensor_name", ["IMX462", "IMX662"])
def test_actual_dark_raw_noise_reconstructs_total_without_double_quantization(
    asset_store, sensor_name
):
    sensor = sensor_create("default", {"size": (128, 128)}, asset_store=asset_store)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", 2)
    sensor.data["mean_electrons"] = np.zeros((128, 128))
    profile = resolve_sensor_readout(
        sensor_name,
        overrides={
            "dark_current_e_per_s": 0,
            "prnu_fraction": 0,
            "dsnu_e": 0,
            "column_prnu_fraction": 0,
            "column_dsnu_e": 0,
        },
    )
    sensor = apply_readout_profile(sensor, profile)
    step = profile["provenance"]["adc_noise"]["simulation_lsb_e"]
    samples = []
    for seed in range(16):
        raw = resample_sensor_capture(sensor, seed=seed).data["raw_dn"]
        samples.append((raw.astype(float) - 64) * step)
    rms = float(np.std(samples))
    source_total = profile["provenance"]["fields"]["reported_total_read_noise_e"]["value"]
    assert rms == pytest.approx(source_total, rel=0.03)


def test_resolved_results_do_not_mutate_packaged_defaults():
    result = resolve_sensor_readout("IMX462")
    original = copy.deepcopy(result)
    result["provenance"]["fields"]["full_well_e"]["value"] = 1
    result["readout"]["full_well_e"] = 1
    assert resolve_sensor_readout("IMX462") == original


def test_final_scenario_overrides_update_profile_provenance_and_adc_scale(asset_store):
    sensor = sensor_create("default", {"size": (8, 8)}, asset_store=asset_store)
    resolved = resolve_sensor_readout("IMX462")
    resolved["readout"]["full_well_e"] = 9000
    resolved["readout"]["read_noise_e"] = 3
    resolved["fixed_pattern"]["dsnu_e"] = 2
    result = apply_readout_profile(sensor, resolved)
    applied = result.metadata["sensor_readout_profile"]
    assert applied["provenance"]["fields"]["full_well_e"]["value"] == 9000
    assert applied["provenance"]["fields"]["full_well_e"]["classification"] == "user_assumption"
    assert applied["provenance"]["fields"]["dsnu_e"]["value"] == 2
    assert applied["provenance"]["source_operating_point_preserved"] is False
    assert applied["provenance"]["adc_noise"]["deembedded"] is False
    assert applied["provenance"]["adc_noise"]["simulation_lsb_e"] == pytest.approx(9000 / 4031)
    assert resolved["provenance"]["fields"]["full_well_e"]["value"] == 11200


def test_apply_rejects_mismatched_optical_sensor_identity(asset_store):
    sensor = sensor_create("default", {"size": (8, 8)}, asset_store=asset_store)
    sensor.metadata["sensor_estimate"] = {"sensor_id": "def_2011_804_sony_imx462"}
    with pytest.raises(ValueError, match="identity"):
        apply_readout_profile(sensor, resolve_sensor_readout("IMX662"))


def test_custom_catalog_root_resolves_additional_generic_sensor_identity(tmp_path):
    catalog = {
        "records": [
            {
                "code": "CUSTOM-123",
                "metadata": {"manufacturer": "Example", "device_name": "CIS"},
            }
        ]
    }
    (tmp_path / "sensor_catalog.json").write_text(json.dumps(catalog))
    result = resolve_sensor_readout("CUSTOM-123", root=tmp_path)
    assert result["sensor_id"] == "custom_123_example_cis"
    assert result["report_code"] == "CUSTOM-123"
    assert result["profile"] == "generic_assumption"
    assert result["provenance"]["classification"] == "unmeasured_assumption"
    assert resolve_sensor_readout("custom_123_example_cis", root=tmp_path) == result
    with pytest.raises(KeyError, match="Unknown image sensor"):
        resolve_sensor_readout("CUSTOM-unknown", root=tmp_path)
