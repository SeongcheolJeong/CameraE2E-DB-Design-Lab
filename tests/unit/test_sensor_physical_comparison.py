"""Small actual optical/RAW comparisons; no synthetic replacement model."""

import numpy as np
import pytest

from pyisetcam.sensor_physical_comparison import camerae2e_compare_physical_sensors


def test_physical_comparison_keeps_photocharge_and_readout_attribution(tmp_path):
    report = camerae2e_compare_physical_sensors(
        output_dir=tmp_path,
        sensor_ids=("DEF-2206-805",),
        output_size=(8, 8),
        nframes=3,
        illuminants=("D65",),
        scene_types=("uniform",),
        fnumbers=(4,),
        cra_degrees=(0, 20),
        azimuth_degrees=(0,),
        sensitivity=False,
    )
    assert report["case_count"] == 8 and report["sample_count"] == 24
    assert report["measured_accuracy_verified"] is False
    assert report["reasonableness"]["numerical_checks_passed"]
    common = [c for c in report["cases"] if c["mode"] == "physical_pupil_common"]
    native = [c for c in report["cases"] if c["mode"] == "physical_pupil_native"]
    for c, n in zip(common, native, strict=True):
        assert n["mean_photoelectrons"] == pytest.approx(c["mean_photoelectrons"], rel=1e-14)
        assert n["readout"]["full_well_e"] != c["readout"]["full_well_e"]
        assert n["pupil_convergence"]["passed"]
        assert "readout" not in n["model_provenance"]
        assert n["readout_profile_reference"]["operating_point"] == "low_gain"
        assert c["readout_profile_reference"]["fixed_pattern"]["dsnu_e"] == 0
    assert common[1]["mean_photoelectrons"] < common[0]["mean_photoelectrons"]
    dark, bright = report["stress_cases"]
    assert dark["mean_photoelectrons"] == 0
    assert dark["native_noise_check"]["passed"]
    assert bright["saturation"]["well_fraction"] > 0.001
    assert not bright["snr_eligible"]
    with np.load(report["artifacts"]["arrays"]) as arrays:
        assert arrays["case_00000_raw_dn"].dtype == np.int32
        for c, n in zip(common, native, strict=True):
            np.testing.assert_array_equal(
                arrays[c["case_id"] + "_mean_electrons"], arrays[n["case_id"] + "_mean_electrons"]
            )


def test_physical_comparison_uses_standard_lights_and_reports_conditional_sensitivity(tmp_path):
    report = camerae2e_compare_physical_sensors(
        output_dir=tmp_path,
        sensor_ids=("DEF-2011-804",),
        output_size=(12, 16),
        nframes=2,
        illuminants=("A", "F11"),
        scene_types=("macbeth_d65",),
        fnumbers=(4,),
        cra_degrees=(0,),
        azimuth_degrees=(0,),
        sensitivity=False,
    )
    assert report["case_count"] == 8
    assert all(
        c["color"]["evaluation_contract"] == "same_surfaces_fixed_D65" for c in report["cases"]
    )
    assert report["illuminant_provenance"]["F11"]["bin_width_nm"] == 10


def test_physical_sensitivity_collection_is_linear_and_clipped_cases_are_excluded(tmp_path):
    report = camerae2e_compare_physical_sensors(
        output_dir=tmp_path,
        sensor_ids=("DEF-2011-804",),
        output_size=(8, 8),
        nframes=2,
        illuminants=("D65",),
        scene_types=("uniform",),
        fnumbers=(4,),
        cra_degrees=(0,),
        azimuth_degrees=(0,),
        exposure_s=100,
    )
    assert all(not c["snr_eligible"] for c in report["cases"])
    values = [c for c in report["sensitivity"] if c["parameter"] == "collection_efficiency"]
    assert [c["value"] for c in values] == [0.5, 0.75, 1]
    np.testing.assert_allclose(
        [c["mean_photoelectrons"] for c in values],
        values[1]["mean_photoelectrons"] * np.array([2 / 3, 1, 4 / 3]),
    )
    assert all(not c["snr_eligible"] for c in values)
    wells = [c for c in report["sensitivity"] if c["parameter"] == "full_well_e"]
    np.testing.assert_allclose([c["effective_dsnu_e"] for c in wells], 1)
    np.testing.assert_allclose(
        [c["realized_dsnu_map_rms_e"] for c in wells], wells[0]["realized_dsnu_map_rms_e"]
    )


@pytest.mark.parametrize(
    "kwargs", [{"nframes": 1}, {"output_size": (4, 8)}, {"cra_degrees": ()}, {"exposure_s": 0}]
)
def test_physical_comparison_rejects_invalid_controls(tmp_path, kwargs):
    with pytest.raises(ValueError):
        camerae2e_compare_physical_sensors(output_dir=tmp_path, **kwargs)
