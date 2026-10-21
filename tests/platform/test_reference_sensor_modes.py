from copy import deepcopy

import numpy as np
import pytest

from camera_platform import electrical
from camera_platform import reference_sensor_modes as modes


def readout():
    return {
        "full_well_e": 10000.0,
        "read_noise_e": 3.0,
        "dark_current_e_per_s": 0.0,
        "gain": 1.0,
        "bit_depth": 16,
        "black_level_dn": 64.0,
    }


def test_same_site_sum_keeps_four_colors_separate():
    x = np.zeros((8, 8))
    for i, (r, c) in enumerate(modes.SITES):
        x[r::2, c::2] = i + 1
    result = modes.same_site_sum(x)
    assert result.shape == (4, 4)
    for i, (r, c) in enumerate(modes.SITES):
        np.testing.assert_array_equal(result[r::2, c::2], 4 * (i + 1))
    np.testing.assert_array_equal(modes.same_site_sum(np.ones((8, 8), bool)), 4)


def test_same_site_sum_is_disjoint_and_preserves_total():
    rng = np.random.default_rng(34)
    x = rng.random((3, 16, 20))
    np.testing.assert_allclose(modes.same_site_sum(x).sum(axis=(-1, -2)), x.sum(axis=(-1, -2)))
    with pytest.raises(ValueError, match="divisible"):
        modes.same_site_sum(np.zeros((6, 8)))


def test_digital_binning_preserves_original_raw_and_subtracts_black_per_site():
    r = readout()
    raw = np.full((2, 16, 16), 100, dtype=np.uint16)
    before = raw.tobytes()
    result = modes.binning_modes(np.ones((16, 16)), r, native_raw=raw, frames=2)
    native, digital = (result["modes"][key] for key in ("native", "digital_binning"))
    assert raw.tobytes() == before
    np.testing.assert_array_equal(native["raw_frames"], raw)
    np.testing.assert_allclose(
        digital["electron_frames"], 4 * native["electron_frames"][:, ::2, ::2]
    )
    assert "raw_frames" not in digital
    assert digital["metadata"]["output_kind"] == "derived_electron_array_not_new_sensor_RAW"


def test_pixel_clipping_precedes_charge_sum_and_node_clip():
    r = readout()
    photo = np.zeros((16, 16))
    # A single contributor exceeding full well cannot give excess charge to others.
    photo[0, 0] = 2 * r["full_well_e"]
    result = modes.binning_modes(photo / 0.004, r, noise_enabled=False, frames=1)
    charge = result["modes"]["charge_binning"]
    assert charge["well_saturated_frames"][0, 0, 0]
    assert charge["electron_frames"][0, 0, 0] == pytest.approx(r["full_well_e"], abs=0.4)
    other = modes.binning_modes(
        np.full((16, 16), 6000 / 0.004),
        r,
        frames=1,
        noise_enabled=False,
        charge_options={"node_capacity_multiplier": 2},
    )
    charge = other["modes"]["charge_binning"]
    assert charge["well_saturated_frames"].all()
    np.testing.assert_allclose(charge["electron_frames"], 20000, atol=0.4)


def test_charge_sum_has_one_read_noise_and_digital_has_four():
    r = readout()
    result = modes.binning_modes(np.full((16, 16), 10 / 0.004), r, frames=2)
    native, digital, charge = [
        result["modes"][k] for k in ("native", "digital_binning", "charge_binning")
    ]
    np.testing.assert_allclose(native["variance_e2"], 10 + 9)
    np.testing.assert_allclose(digital["variance_e2"], 40 + 36)
    np.testing.assert_allclose(charge["variance_e2"], 40 + 9)
    assert charge["metadata"]["dn_per_e"] == pytest.approx(native["metadata"]["dn_per_e"] / 4)


def test_fixed_native_prnu_and_dark_are_applied_before_charge_sum():
    r = readout()
    model = electrical.default_electrical_model(r)
    model["fixed_pattern"] = {"prnu_sigma": 0.2, "dsnu_e_per_s": 10.0, "seed": 192}
    model["dark_current_e_per_s"] = 50.0
    result = modes.binning_modes(
        np.full((16, 16), 200 / 0.004), r, electrical_model=model, frames=1, noise_enabled=False
    )
    prnu, dark, _ = electrical._patterns(model, [16, 16], [0, 0], (16, 16), 25.0)
    expected = modes.same_site_sum(200 * prnu)
    np.testing.assert_allclose(
        result["modes"]["charge_binning"]["electron_frames"][0], expected, atol=0.4
    )
    assert np.ptp(dark) > 0
    assert result["modes"]["charge_binning"]["metadata"]["fixed_pattern"] == model["fixed_pattern"]


def test_binned_lsc_uses_aggregated_uniform_and_not_arithmetic_gain():
    gain = np.ones((16, 16))
    gain[0::4, 0::4] = 4
    binned = modes.binned_lsc(gain)
    assert binned[0, 0] == pytest.approx(4 / (1 / 4 + 1 + 1 + 1))
    np.testing.assert_allclose(binned * modes.same_site_sum(1 / gain) / 4, 1)


def test_binning_centroids_are_not_a_uniform_bayer_grid():
    geometry = modes.binning_geometry((16, 16))
    np.testing.assert_array_equal(
        geometry["sample_centers_native_col"][0], [1, 2, 5, 6, 9, 10, 13, 14]
    )
    np.testing.assert_array_equal(geometry["display_centers_native_col"], 0.5 + 2 * np.arange(8))


def test_coordinate_aware_reconstruction_preserves_affine_spatial_ramp():
    yy, xx = np.indices((32, 32))
    # Each color has the same affine continuous field; four samples average at centroid.
    photo = 50 + yy * 2 + xx * 3
    sums = modes.same_site_sum(photo)
    output = modes.process_electrons(sums, np.eye(3), contribution_count=4, display_white=1000)
    yo, xo = np.indices((16, 16))
    expected = 50 + (0.5 + 2 * yo) * 2 + (0.5 + 2 * xo) * 3
    for channel in range(3):
        np.testing.assert_allclose(
            output["linear"][..., channel][output["valid"]], expected[output["valid"]], atol=1e-10
        )
    assert not output["valid"][0].any()
    assert not output["valid"][-1].any()


def test_common_electron_isp_lsc_once_and_four_sum_brightness():
    native = np.full((16, 16), 100.0)
    a = modes.process_electrons(native, np.eye(3), display_white=1000)
    b = modes.process_electrons(
        modes.same_site_sum(native), np.eye(3), contribution_count=4, display_white=1000
    )
    np.testing.assert_allclose(a["linear"][2:-2, 2:-2], 100)
    np.testing.assert_allclose(b["linear"][b["valid"]], 100)
    c = modes.process_electrons(
        native, np.eye(3), lsc_gain=np.full((16, 16), 2.0), display_white=1000
    )
    np.testing.assert_allclose(c["linear"][2:-2, 2:-2], 200)
    with pytest.raises(ValueError, match="coordinate-aware"):
        modes.process_electrons(
            modes.same_site_sum(native), np.eye(3), contribution_count=4, demosaic="laplacian"
        )


def test_dcg_shares_shot_population_and_reduces_only_read_term():
    result = modes.dual_gain_modes(np.full((64, 64), 1000 / 0.004), readout(), frames=4)
    hcg, lcg, merged = [result["modes"][k] for k in ("hcg", "lcg", "dcg")]
    high, low = hcg["electron_frames"], lcg["electron_frames"]
    assert np.corrcoef(high.ravel(), low.ravel())[0, 1] > 0.98
    # The path difference contains read noise, not two independent shot samples.
    assert np.var(high - low) == pytest.approx(9 + 9 / 4, rel=0.07)
    weights = result["diagnostics"]["weights"]
    np.testing.assert_allclose(weights[:, 0], 0.8)
    np.testing.assert_allclose(weights[:, 1], 0.2)
    assert np.mean(merged["variance_e2"]) > 990  # Shared shot variance was not halved.
    assert merged["metadata"]["shot_noise_shared"]


def test_dcg_hcg_saturation_uses_lcg_and_both_invalid_stays_invalid():
    rate = np.full((16, 16), 5000 / 0.004)
    rate[:, 8:] = 20000 / 0.004
    result = modes.dual_gain_modes(rate, readout(), frames=1, noise_enabled=False)
    merged = result["modes"]["dcg"]
    np.testing.assert_array_equal(result["diagnostics"]["weights"][0, 0, :, :8], 0)
    np.testing.assert_array_equal(result["diagnostics"]["weights"][0, 1, :, :8], 1)
    assert merged["valid_frames"][0, :, :8].all()
    assert not merged["valid_frames"][0, :, 8:].any()
    assert np.isnan(merged["electron_frames"][0, :, 8:]).all()
    np.testing.assert_allclose(merged["electron_frames"][0, :, :8], 5000, atol=0.2)


def test_temporal_noise_does_not_treat_spatial_texture_as_noise():
    yy, xx = np.indices((16, 16))
    result = modes.binning_modes((100 + xx + yy) / 0.004, readout(), frames=3, noise_enabled=False)
    for mode in result["modes"].values():
        assert modes.summarize_mode(mode)["temporal_noise_e_per_contributor"] == pytest.approx(
            0, abs=1e-12
        )


def test_new_acquisitions_do_not_modify_stored_readout_models():
    r = readout()
    model = electrical.default_electrical_model(r)
    before = deepcopy(model)
    modes.binning_modes(np.full((16, 16), 1.0), r, electrical_model=model, frames=1)
    modes.dual_gain_modes(np.full((16, 16), 1.0), r, electrical_model=model, frames=1)
    assert model == before


def test_lsc_propagates_variance_by_gain_squared_and_area_normalization():
    result = modes.process_electrons(
        np.full((8, 8), 400.0),
        np.eye(3),
        contribution_count=4,
        lsc_gain=np.full((8, 8), 3.0),
        variance_e2=np.full((8, 8), 16.0),
    )
    np.testing.assert_allclose(result["lsc_mosaic_variance_e2_per_contributor"], 9.0)


def test_dcg_remerge_uses_preserved_raw_and_never_calls_capture(monkeypatch):
    result = modes.dual_gain_modes(np.full((16, 16), 1000 / 0.004), readout(), frames=2)
    paired = np.stack(
        (result["modes"]["hcg"]["raw_frames"], result["modes"]["lcg"]["raw_frames"]), axis=1
    )
    before = paired.tobytes()

    def refuse(*args, **kwargs):
        raise AssertionError("RAW-only replay must never recapture")

    monkeypatch.setattr(modes.dcg_electrical, "capture_shared_electrons", refuse)
    replay = modes.replay_dual_gain(paired, result["calibration"], result["model"])
    assert paired.tobytes() == before
    for name in ("hcg", "lcg", "dcg"):
        np.testing.assert_array_equal(
            replay["modes"][name]["electron_frames"], result["modes"][name]["electron_frames"]
        )
        assert replay["modes"][name]["metadata"]["acquisition_performed"] is False
    np.testing.assert_array_equal(
        replay["diagnostics"]["weights"], result["diagnostics"]["weights"]
    )


def test_charge_read_noise_zero_is_supported_as_an_explicit_assumption():
    result = modes.binning_modes(
        np.full((16, 16), 10 / 0.004),
        readout(),
        frames=2,
        charge_options={"read_noise_multiplier": 0.0},
    )
    np.testing.assert_allclose(result["modes"]["charge_binning"]["variance_e2"], 40.0)
