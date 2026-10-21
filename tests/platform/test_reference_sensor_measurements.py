"""Unit, sampling, support and standalone-report semantics for sensor modes."""

import base64
import json

import numpy as np
import pytest
from scipy.ndimage import gaussian_filter

from camera_platform.reference_sensor_measurements import measure, write_report


def inputs(signal, repeats=8):
    electrons = np.broadcast_to(signal, (repeats, *np.asarray(signal).shape)).copy()
    linear = np.repeat((electrons / 1000)[..., None], 3, axis=-1)
    valid = np.ones_like(electrons, dtype=bool)
    return electrons, linear, valid


def test_temporal_noise_does_not_measure_spatial_texture_or_static_fpn():
    y, x = np.indices((64, 64))
    result = measure(*inputs(100 + 3 * x + 2 * y))
    assert result["temporal_noise_e"] == 0
    assert result["linear_y_temporal_noise"] == pytest.approx(0, abs=1e-15)
    assert result["snr"] is None
    assert result["local_contrast"] > 0
    assert result["valid_pixels"] == 32 * 32
    json.dumps(result, allow_nan=False)


def test_binned_sum_and_noise_use_the_same_per_native_pixel_unit():
    electrons, linear, valid = inputs(np.full((64, 64), 100.0))
    offsets = np.arange(8) - 3.5
    electrons += offsets[:, None, None]
    native = measure(electrons, linear, valid)
    binned = measure(
        electrons[:, ::2, ::2] * 4,
        linear[:, ::2, ::2],
        valid[:, ::2, ::2],
        contribution_count=4,
        native_pitch_factor=2,
    )
    assert native["mean_electrons"] == binned["mean_electrons"] == 100
    assert native["temporal_noise_e"] == binned["temporal_noise_e"]
    assert binned["temporal_noise_e"] == pytest.approx(np.std(offsets, ddof=1))
    assert native["snr"] == binned["snr"]
    assert native["measurement_roi"] == [16, 48, 16, 48]
    assert binned["measurement_roi"] == [8, 24, 8, 24]
    assert native["native_equivalent_shape"] == binned["native_equivalent_shape"]
    assert binned["nyquist_cycles_per_native_pixel"] == 0.25


def test_saturation_includes_invalid_samples_and_preserves_missing_flag():
    electrons, linear, valid = inputs(np.ones((64, 64)))
    saturated = np.zeros_like(valid)
    saturated[:, :, 32:] = True
    valid[saturated] = False
    result = measure(electrons, linear, valid, adc_saturated_frames=saturated)
    assert result["valid_pixels"] == 32 * 16
    assert result["adc_saturation_fraction"] == 0.5
    assert result["well_saturation_fraction"] is None
    valid[:] = False
    empty = measure(electrons, linear, valid, adc_saturated_frames=saturated)
    assert empty["status"] == "unavailable"
    assert empty["temporal_noise_e"] is None
    assert empty["adc_saturation_fraction"] == 0.5


def test_single_repeat_and_small_input_do_not_claim_noise_measurements():
    one = measure(*inputs(np.full((64, 64), 30), repeats=1))
    assert one["mean_electrons"] == 30
    assert one["temporal_noise_e"] is None
    assert one["temporal_noise_reason"] == "at_least_two_repeats_required"
    small = measure(*inputs(np.ones((32, 32))))
    assert small["status"] == "unavailable"
    assert small["measurement_roi"] is None


def test_luma_noise_retains_cross_channel_covariance():
    from camera_platform.virtual_lab_metrics import LUMA

    electrons, linear, valid = inputs(np.ones((64, 64)))
    offsets = (np.arange(8) - 3.5) * 0.01
    # Perfect anticorrelation makes Y exactly constant while RGB remains noisy.
    linear[..., 0] += offsets[:, None, None]
    linear[..., 1] -= offsets[:, None, None] * LUMA[0] / LUMA[1]
    result = measure(electrons, linear, valid)
    assert result["linear_rgb_temporal_noise"][0] > 0.01
    assert result["linear_y_temporal_noise"] == pytest.approx(0, abs=1e-15)


def test_nonfinite_pixels_are_excluded_from_common_repeat_support():
    electrons, linear, valid = inputs(np.ones((64, 64)))
    electrons[0, 30, 30] = np.nan
    linear[2, 29, 29] = np.inf
    result = measure(electrons, linear, valid)
    assert result["linear_valid_pixels"] == 32 * 32 - 1
    assert result["electron_valid_pixels"] == 32 * 32 - 1
    assert result["mean_electrons"] == 1
    json.dumps(result, allow_nan=False)


def test_irregular_cfa_and_regular_rgb_masks_are_not_indexwise_combined():
    electrons, linear, rgb_valid = inputs(np.full((64, 64), 100.0))
    electron_valid = rgb_valid.copy()
    # Sample (30,30) is invalid in the irregular CFA lattice. The reconstructed
    # invalid location is elsewhere; neither mask can be copied to the other grid.
    electrons[:, 30, 30] = 10000
    electron_valid[:, 30, 30] = False
    rgb_valid[:, 29, 29] = False
    linear[:, 29, 29] = np.arange(8)[:, None] * 100
    electrons[:, 29, 29] += np.arange(8)
    result = measure(electrons, linear, rgb_valid, electron_valid_frames=electron_valid)
    assert result["linear_valid_pixels"] == result["electron_valid_pixels"] == 32 * 32 - 1
    assert result["mean_electrons"] == pytest.approx(100 + 3.5 / 1023)
    assert result["temporal_noise_e"] > 0
    assert result["linear_y_temporal_noise"] == pytest.approx(0, abs=1e-15)
    rgb_valid[:] = False
    only_electrons = measure(electrons, linear, rgb_valid, electron_valid_frames=electron_valid)
    assert only_electrons["status"] == "partial"
    assert only_electrons["mean_electrons"] is not None
    assert only_electrons["linear_y_temporal_noise"] is None


def test_sfr_preserves_native_and_binned_frequency_units_and_nyquist():
    y, x = np.indices((128, 128))
    signal = 100 + 700 * (x > 64 + (y - 64) / 12)
    blurred = gaussian_filter(signal.astype(float), 2)
    native = measure(*inputs(blurred), chart="edge", horizontal_fov_deg=1.28)["sfr"]
    binned = measure(
        *inputs(blurred[::2, ::2]), chart="edge", native_pitch_factor=2, horizontal_fov_deg=1.28
    )["sfr"]
    assert native["status"] == binned["status"] == "ok"
    assert native["nyquist_cycles_per_native_pixel"] == 0.5
    assert binned["nyquist_cycles_per_native_pixel"] == 0.25
    assert max(binned["frequency_cycles_per_native_pixel"]) <= 0.25
    assert native["mtf50_cycles_per_native_pixel"] == pytest.approx(
        binned["mtf50_cycles_per_native_pixel"], rel=0.15
    )
    assert native["frequency_cycles_per_degree"][1] == pytest.approx(
        native["frequency_cycles_per_native_pixel"][1] / 0.01
    )
    assert binned["frequency_cycles_per_degree"][1] == pytest.approx(
        binned["frequency_cycles_per_native_pixel"][1] / 0.01
    )


def test_resolution_chart_measures_five_existing_edge_windows():
    from camera_platform.design_workshop_engine import _patterns

    pattern = _patterns("resolution", (256, 256))
    result = measure(*inputs(gaussian_filter(pattern, 1)), chart="resolution")
    assert result["sfr"]["status"] == "ok"
    assert len(result["sfr"]["sites"]) == 5
    assert all(site["status"] == "ok" for site in result["sfr"]["sites"].values())
    electrons, linear, valid = inputs(pattern)
    valid[:, 128, 128] = False
    refused = measure(electrons, linear, valid, chart="resolution")["sfr"]
    assert refused["status"] == "unavailable"
    assert refused["reason"] == "edge_outside_valid_support"
    assert refused["sites"]["upper_left"]["status"] == "ok"


@pytest.mark.parametrize(
    "keyword,value",
    [
        ("contribution_count", 0),
        ("contribution_count", np.inf),
        ("native_pitch_factor", 3),
        ("native_pitch_factor", True),
        ("horizontal_fov_deg", 0),
    ],
)
def test_bad_units_are_rejected(keyword, value):
    with pytest.raises(ValueError):
        measure(*inputs(np.ones((64, 64))), **{keyword: value})


def test_report_embeds_images_and_curves_with_escaped_provenance(tmp_path):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=="
    )
    (tmp_path / "display.png").write_bytes(png)
    metrics = measure(*inputs(np.ones((64, 64))))
    mode = {
        "id": "native",
        "label": "Native <script>",
        "shape": [64, 64],
        "metrics": metrics,
        "files": {"display": "display.png"},
    }
    cases = [
        {
            "id": "case-1",
            "field_id": "center",
            "chart": "uniform",
            "light_scale": 1,
            "modes": [mode],
            "source_refs": {"hash": "123"},
            "assumptions": {"model": "research"},
        }
    ]
    path = write_report({"recipe": "binning", "source": {"model": "<script>"}}, cases, tmp_path)
    document = path.read_text()
    assert "data:image/png;base64," in document
    assert "<script>" not in document
    assert "&lt;script&gt;" in document
    assert "123" in document
    assert "Case assumptions" in document
    assert len(list((tmp_path / "plots").glob("*.png"))) == 2
    assert 'src="display.png"' not in document


def test_report_includes_dynamic_range_step_curves(tmp_path):
    tone = [
        {
            "input_mean_electrons": v,
            "mean_electrons": min(v, 1000),
            "temporal_noise_e": np.sqrt(v),
            "well_saturation_fraction": float(v > 1000),
            "adc_saturation_fraction": float(v > 1000),
            "valid_fraction": float(v <= 1000),
            "step": i,
        }
        for i, v in enumerate((1, 10, 100, 1000, 10000))
    ]
    case = {
        "id": "tone",
        "field_id": "center",
        "chart": "dynamic_range",
        "light_scale": 1,
        "modes": [{"id": "hcg", "metrics": {}, "tone_curve": tone}],
    }
    document = write_report({"recipe": "dual_gain"}, [case], tmp_path).read_text()
    assert len(list((tmp_path / "plots").glob("*.png"))) == 5
    assert "Measured response (e / native pixel)" in document
