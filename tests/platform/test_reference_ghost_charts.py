"""Fixed analytic ghost chart masks and repeated-capture measurement contracts."""

import json

import numpy as np
import pytest

from camera_platform.reference_ghost_charts import chart_scene, measure_chart


def geometry(halo=16):
    return {
        "native_shape_row_col": [256, 256],
        "input_shape_row_col": [256 + 2 * halo, 256 + 2 * halo],
        "crop_bounds_row_col": [halo, halo + 256, halo, halo + 256],
    }


def chart(name="dark_contrast", halo=16):
    return chart_scene(name, geometry(halo), {"white": np.array([1.0, 2.0, 3.0])})


def captures(image, noise=1e-5):
    # Balanced deterministic temporal noise preserves the expected temporal mean.
    temporal = np.array([-1, 1, -1, 1, -1, 1, -1, 1])[:, None, None]
    samples = image[None, ...] + temporal * noise
    return {
        "electron_frames": samples * 1000,
        "linear_frames": np.repeat(samples[..., None], 3, axis=-1),
        "valid_frames": np.ones(samples.shape, dtype=bool),
        "raw_valid_frames": np.ones(samples.shape, dtype=bool),
        "well_saturated_frames": np.zeros(samples.shape, dtype=bool),
        "adc_saturated_frames": np.zeros(samples.shape, dtype=bool),
    }


def native_pattern():
    photons, metadata = chart()
    return photons[16:-16, 16:-16, 0], metadata


def test_black_is_zero_with_halo_and_compact_json_masks():
    photons, metadata = chart("black")
    assert photons.shape == (288, 288, 3)
    assert not photons.any()
    assert metadata["targets"] == []
    assert "external optical" in metadata["source"]
    json.dumps(metadata, allow_nan=False)


def test_halo_is_same_analytic_coordinate_function_for_larger_and_asymmetric_crops():
    a, ma = chart(halo=8)
    b, mb = chart(halo=24)
    np.testing.assert_array_equal(a, b[16:-16, 16:-16])
    g = geometry(8)
    g["input_shape_row_col"] = [281, 279]
    g["crop_bounds_row_col"] = [7, 263, 9, 265]
    c, _ = chart_scene("dark_contrast", g, {"white": np.array([1.0, 2.0, 3.0])})
    np.testing.assert_array_equal(a[8:-8, 8:-8], c[7:263, 9:265])
    assert ma["targets"] == mb["targets"]


def test_declared_masks_have_correct_levels_and_equal_cfa_site_counts():
    image, metadata = native_pattern()
    assert len(metadata["targets"]) == 12
    for target in metadata["targets"]:
        for kind, level in (
            ("object", target["object_level"]),
            ("background", target["background_level"]),
        ):
            r0, r1, c0, c1 = target[kind + "_roi"]
            assert all(v % 2 == 0 for v in (r0, r1, c0, c1))
            assert (r1 - r0) * (c1 - c0) > 0
            np.testing.assert_array_equal(image[r0:r1, c0:c1], level)
    assert len(json.dumps(metadata)) < 12000


def test_fixed_roi_contrast_loss_under_additive_veiling_light():
    image, metadata = native_pattern()
    baseline = captures(image)
    result = measure_chart(captures(image + 0.01), baseline, metadata)
    assert result["status"] == "ok" and result["valid_targets"] == 12
    for target in result["targets"]:
        level = target["background_level"]
        assert target["baseline_contrast"] == pytest.approx(target["declared_weber_contrast"])
        assert target["contrast_retention"] == pytest.approx(level / (level + 0.01))
        assert target["observed_contrast"] < target["baseline_contrast"]
    json.dumps(result, allow_nan=False)


def test_temporal_noise_does_not_count_spatial_texture_or_reduce_by_roi_size():
    image, metadata = native_pattern()
    yy, xx = np.indices(image.shape)
    texture = (np.sin(xx / 2) + np.cos(yy / 5)) * 0.001
    a = measure_chart(captures(image), None, metadata)
    b = measure_chart(captures(image + texture), None, metadata)
    expected_e = 0.01 * np.sqrt(8 / 7)
    for first, second in zip(a["targets"], b["targets"], strict=True):
        assert first["temporal_noise_e"] == pytest.approx(expected_e)
        assert second["temporal_noise_e"] == pytest.approx(expected_e)
        delta = first["object"]["mean_y"] - first["background"]["mean_y"]
        assert first["cnr"] == pytest.approx(delta / (1e-5 * np.sqrt(2 * 8 / 7)))


@pytest.mark.parametrize(
    "key", ["valid_frames", "raw_valid_frames", "well_saturated_frames", "adc_saturated_frames"]
)
def test_any_invalid_or_saturated_pixel_makes_its_target_unavailable(key):
    image, metadata = native_pattern()
    baseline = captures(image)
    current = captures(image + 0.01)
    r0, _, c0, _ = metadata["targets"][0]["object_roi"]
    current[key][2, r0, c0] = key.endswith("saturated_frames")
    result = measure_chart(current, baseline, metadata)
    assert result["status"] == "partial"
    assert result["targets"][0]["status"] == "unavailable"
    assert result["targets"][0]["observed_contrast"] is None
    assert result["targets"][0]["contrast_retention"] is None
    assert result["targets"][0]["cnr"] is None
    assert result["valid_targets"] == 11


def test_invalid_baseline_cannot_be_used_for_a_contrast_retention_claim():
    image, metadata = native_pattern()
    baseline = captures(image)
    r0, _, c0, _ = metadata["targets"][0]["background_roi"]
    baseline["valid_frames"][0, r0, c0] = False
    result = measure_chart(captures(image + 0.01), baseline, metadata)
    assert result["targets"][0]["status"] == "unavailable"
    assert result["targets"][0]["contrast_retention"] is None


def test_black_excess_retains_negative_values_instead_of_clipping_noise():
    _, metadata = chart("black")
    baseline = captures(np.zeros((256, 256)))
    current = captures(np.full((256, 256), -0.002))
    result = measure_chart(current, baseline, metadata)
    assert result["black_level"]["mean_excess_e"] == pytest.approx(-2.0)
    assert result["black_level"]["peak_excess_e"] == pytest.approx(-2.0)
    assert result["black_level"]["minimum_excess_e"] == pytest.approx(-2.0)
    assert measure_chart(current, None, metadata)["black_level"]["mean_excess_e"] is None


def test_zero_temporal_noise_does_not_create_infinite_cnr_or_a_pass():
    image, metadata = native_pattern()
    result = measure_chart(captures(image, noise=0), None, metadata)
    assert result["status"] == "partial"
    assert all(t["cnr"] is None and t["status"] == "partial" for t in result["targets"])


def test_wrong_geometry_or_frame_contract_is_rejected():
    with pytest.raises(ValueError, match="matching input crop"):
        chart_scene("black", {**geometry(), "native_shape_row_col": [32, 32]}, {"white": [1]})
    image, metadata = native_pattern()
    data = captures(image)
    data["linear_frames"] = data["linear_frames"][:, :, :, :2]
    with pytest.raises(ValueError, match="three channels"):
        measure_chart(data, None, metadata)
