"""Reference native RAW -> ISP v2 replay, with expensive optics replaced only.

The fixture reuses actual reviewed pixel response, electrical readout and ISP.
It is not evidence of physical optical accuracy.
"""

import copy
import json

import numpy as np
import pytest
from test_reference_workshop_engine import (  # noqa: F401 -- imported pytest fixture
    WORKSPACE,
    _apply_fixture,
    captured,
)

from camera_platform import reference_isp, reference_isp_color
from camera_platform import reference_workshop_engine as engine
from camera_platform.reference_isp_lab import Processor, common_mask
from camera_platform.virtual_lab_job import sha256


def _with_planes(solution, field, photons, response):
    result = _apply_fixture(solution, field, photons, response)
    wave = engine.WAVE
    qe = (
        engine.pixel.absolute_angular_response(response, 0, 0, wave)
        + engine.pixel.absolute_angular_response(response, 30, 75, wave)
    ) / 2
    widths = np.full(wave.size, 10.0)
    widths[[0, -1]] = 5
    transfer = qe * widths[:, None] * (2.9e-6) ** 2 * 0.01 * field["fixture_transmission"]
    result["planes_electron_rate"] = np.einsum("hwk,ks->hws", photons[4:68, 4:68], transfer)
    return result


@pytest.fixture
def lab_source(captured, tmp_path, monkeypatch):  # noqa: F811 -- pytest resolves imported fixture
    monkeypatch.setattr(engine, "_apply", _with_planes)
    specification = copy.deepcopy(captured["spec"])
    specification["chart_suite"] = "isp_lab_v1"
    specification["isp"] = reference_isp.default_profile(specification["isp"])
    specification["isp"]["demosaic_method"] = "bilinear"
    root = tmp_path / "capture"
    result = engine.run(WORKSPACE, root, specification, tmp_path / "cache")
    return root, specification, result


def _raw_hashes(root):
    return {str(p.relative_to(root)): sha256(p) for p in root.glob("fields/*/*/raw-*.npy")}


def test_v2_capture_retains_planes_stages_and_same_raw_replay(lab_source, tmp_path):
    source, specification, result = lab_source
    original_raw = _raw_hashes(source)
    assert result["successful_fields"] == 8
    center = result["fields"][0]
    record = center["capture_files"]["demosaic_chirp"]
    plane_path = source / "fields/center/demosaic_chirp/planes-electron-rate.npy"
    assert sha256(plane_path) == record["planes_sha256"]
    assert np.load(plane_path).shape == (64, 64, 4)
    assert center["tests"]["isp_lab"]["demosaic"]["noise_adc_bypassed"]
    assert center["tests"]["isp_lab"]["demosaic"]["status"] == "ok"
    assert center["tests"]["isp_lab"]["temporal_noise_rgb"]["r"] > 0
    support = center["tests"]["isp_lab"]["metric_support"]
    assert set(support) == {"color", "resolution", "texture"}
    assert not support["color"]["eligible"]  # 64px fixture patches intersect the 8px halo.
    assert support["texture"]["metric_halo_pixels"] == 4
    assert (source / "fields/center/texture/texture-metric-valid-mask.npy").is_file()
    specification = copy.deepcopy(specification)
    specification["isp"]["demosaic_method"] = "adaptive_laplacian"
    specification["isp"]["raw_denoise"]["strength"] = 0.4
    destination = tmp_path / "replay"
    replay = engine.replay(source, destination, specification)
    assert _raw_hashes(source) == original_raw == _raw_hashes(destination)
    assert replay["fields"][0]["tests"]["isp_lab"]["demosaic"]["method"] == "adaptive_laplacian"
    assert replay["fields"][0]["tests"]["texture"]["reference_policy"]
    first = np.load(source / "fields/center/texture/processed.npy")
    second = np.load(destination / "fields/center/texture/processed.npy")
    assert np.max(np.abs(first - second)) > 0


def test_color_matrix_binding_precedes_processing_and_changes_same_raw(
    lab_source, tmp_path, monkeypatch
):
    source, specification, _ = lab_source
    capture = json.loads((source / "capture.json").read_text())
    matrix = np.asarray(capture["fields"]["center"]["calibration"]["matrix"])
    calibration = {"id": "test-model-bound-color", "matrix": (matrix * 1.1).tolist()}
    called = []

    def validate(value, folder):
        assert value is calibration and folder == source.resolve()
        called.append(True)

    monkeypatch.setattr(reference_isp_color, "validate_binding", validate)
    actual_process = reference_isp.process

    def checked(*args, **kwargs):
        assert called == [True]
        expected = calibration["matrix"] if args[3]["color_calibration_id"] else matrix
        np.testing.assert_array_equal(args[2], expected)
        return actual_process(*args, **kwargs)

    monkeypatch.setattr(reference_isp, "process", checked)
    specification = copy.deepcopy(specification)
    specification["isp"]["color_calibration_id"] = calibration["id"]
    destination = tmp_path / "color-replay"
    engine.replay(source, destination, specification, color_calibration=calibration)
    original = np.load(source / "fields/center/color/linear.npy")
    candidate = np.load(destination / "fields/center/color/linear.npy")
    np.testing.assert_allclose(candidate, original * 1.1, rtol=2e-6, atol=1e-7)
    assert _raw_hashes(source) == _raw_hashes(destination)


def test_texture_reference_is_fixed_across_demosaic_and_color_changes(captured):  # noqa: F811
    source = captured["root"]
    capture = json.loads((source / "fields/center/capture.json").read_text())
    matrix = np.asarray(capture["calibration"]["matrix"])
    raw = np.load(source / "fields/center/texture/raw-00.npy")
    first = reference_isp.default_profile(captured["spec"]["isp"])
    first["demosaic_method"] = "bilinear"
    second = copy.deepcopy(first)
    second["demosaic_method"] = "laplacian"
    second["raw_denoise"]["strength"] = 0.8
    second["linear_sharpen"]["amount"] = 0.6
    second["color_calibration_id"] = "test-color"
    a = Processor(capture, first, None, matrix=matrix, reference_matrix=matrix)
    b = Processor(capture, second, None, matrix=matrix * 1.1, reference_matrix=matrix)
    reference_a = a.bypass(raw, capture["readout"], matrix, first)
    reference_b = b.bypass(raw, capture["readout"], matrix * 1.1, second)
    for expected, actual in zip(reference_a, reference_b, strict=True):
        np.testing.assert_array_equal(actual, expected)


def test_common_mask_covers_square_eight_pixel_dependency():
    raws = np.full((3, 64, 64), 1000, dtype=np.uint16)
    raws[:, 30, 30] = 4095
    mask = common_mask(raws, {"bit_depth": 12})
    assert not mask[:8].any() and not mask[:, :8].any()
    assert not mask[24, 24]  # diagonal support must not use a diamond approximation
    assert mask[15, 15]


def test_color_and_edge_metrics_require_every_measured_roi_pixel_supported():
    processor = Processor({"calibration": {"matrix": np.eye(3)}}, {}, None)
    mask = np.ones((64, 64), dtype=bool)
    mask[20, 20] = False
    processor.masks = {"color": mask, "resolution": mask}
    measurements = {
        "color": {"patches": [{"roi": [12, 18, 12, 18]}, {"roi": [18, 24, 18, 24]}]},
        "resolution": {
            "sites": {
                "center": {"roi": [12, 18, 12, 18]},
                "outer": {"roi": [18, 24, 18, 24]},
            }
        },
    }
    processor.finalize_tests(measurements)
    support = processor.tests["metric_support"]
    assert support["color"]["invalid_roi_ids"] == [1]
    assert support["resolution"]["invalid_roi_ids"] == ["outer"]
    assert not support["color"]["eligible"] and not support["resolution"]["eligible"]
    mask[20, 20] = True
    processor.finalize_tests(measurements)
    assert all(processor.tests["metric_support"][key]["eligible"] for key in support)
    processor.finalize_tests({})
    assert all(not processor.tests["metric_support"][key]["eligible"] for key in support)


def test_texture_highpass_uses_additional_four_pixel_support(tmp_path):
    capture = {"calibration": {"matrix": np.eye(3)}, "readout": {"bit_depth": 12}}
    processor = Processor(capture, {}, None)
    raws = np.full((3, 64, 64), 1000, dtype=np.uint16)
    raws[:, 30, 30] = 4095
    frames = np.ones((3, 64, 64, 3))
    images = {}
    processor.chart("texture", tmp_path, raws, frames, frames, images)
    base = processor.masks["texture"]
    mask = np.load(tmp_path / "texture-metric-valid-mask.npy")
    assert base[10, 10] and not mask[10, 10]
    assert base[21, 21] and not mask[21, 21]
    assert mask[15, 15]
    assert processor.tests["metric_support"]["texture"]["valid_pixels"] == int(mask.sum())
    yy, xx = np.indices(mask.shape)
    reference = np.sin(xx * 0.8) + np.cos(yy * 0.6)
    record = {}
    processor.texture(reference, reference * 0.75, record)
    assert processor.tests["texture_retention"] == pytest.approx(0.75)
    assert record["comparison_valid_pixels"] == int(mask.sum())
    assert images["metric_valid_mask"] == "texture/texture-metric-valid-mask.png"
