"""Native wide ISP artifacts and immutable replay, using a small RAW fixture.

The reduced geometry only bounds test cost; public wide capture still requires
1920×1080. No optical calculation or public capture admission is mocked here.
"""

import copy
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from camera_platform import virtual_lab_engine as engine
from camera_platform.virtual_lab_job import default_specification
from camera_platform.virtual_lab_wide_geometry import resolution_sites

WORKSPACE = Path(__file__).resolve().parents[2]
NATIVE_SHAPE = (270, 480)


def _hash_tree(directory):
    return {
        str(path.relative_to(directory)): engine.file_sha256(path)
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.fixture(scope="module")
def wide_capture(tmp_path_factory):
    directory = tmp_path_factory.mktemp("wide_native_render")
    (directory / "raw").mkdir()
    (directory / "arrays").mkdir()
    spec = default_specification({"lenses": [{"id": "wide_parametric"}], "sensors": [{"id": "S1"}]})
    spec.update(scene="resolution", test_suite="resolution")
    spec["field_model"] = {"mode": "full_field", "horizontal_fov_deg": 120}
    spec["wide_angle"]["projection"] = "equidistant"
    geometry = {
        "optical_model": "wide_parametric",
        "projection": "equidistant",
        "native_shape": list(NATIVE_SHAPE),
        "pixel_pitch_um": 3.45,
        "focal_length_mm": NATIVE_SHAPE[1] * 0.00345 / np.radians(120),
        "horizontal_fov_deg": 120,
        "common_horizontal_fov_deg": 120,
        "source_scene_angular_extent_deg": [120, 67.5],
    }
    sites = resolution_sites(geometry)
    # A patterned native RGGB readout with independent synthetic code noise.
    # Its purpose is exercising real ISP/replay, not physical capture evidence.
    yy, xx = np.indices(NATIVE_SHAPE)
    mean = 800 + 300 * np.sin(xx / 8) + 200 * np.cos(yy / 7)
    raw = np.clip(
        np.round(64 + mean + np.random.default_rng(14).normal(0, 18, NATIVE_SHAPE)), 0, 4095
    )
    paths = {
        "raw": ("raw/resolution.npy", raw.astype(np.uint16)),
        "mean": ("arrays/resolution-mean-electrons.npy", mean.astype(np.float32)),
        "optical": ("arrays/resolution-optical.npy", (mean / 2000).astype(np.float32)),
        "psf": ("arrays/resolution-center-psf.npy", np.ones((5, 5), dtype=float) / 25),
    }
    artifacts = {}
    for label, (relative, values) in paths.items():
        np.save(directory / relative, values)
        artifacts[label] = engine._artifact(directory, directory / relative)
    capture = {
        "schema": "camera_virtual_lab_capture_v1",
        "specification": spec,
        "geometry": geometry,
        "lens": {"identity": {"id": "wide_parametric", "kind": "test_fixture"}},
        "sensor": {"revision": {"id": "S1-test-fixture"}},
        "readout": {"bit_depth": 12, "black_level_dn": 64, "gain": 1, "full_well_e": 4000},
        "calibration": {"matrix_sensor_electrons_to_linear_srgb": (np.eye(3) / 2000).tolist()},
        "captures": {
            "resolution": {
                "raw": artifacts["raw"],
                "mean": artifacts["mean"],
                "psf": artifacts["psf"],
                "wide": {
                    "resolution_sites": sites,
                    "sampling": {"selected_factor": 2, "status": "test_fixture"},
                    "resource": {"kind": "synthetic_render_fixture"},
                },
            },
        },
        "field_windows": [],
        "requested_tests": ["resolution"],
        "elapsed_s": 0,
        "optical_preview_reference_photons_mean_band": 1,
    }
    engine._json(directory / "capture.json", capture)
    result = engine._render(directory, directory, capture, spec)
    return directory, spec, capture, result


def test_wide_native_isp_is_real_process_output_and_preview_is_separate(wide_capture):
    directory, spec, capture, result = wide_capture
    raw = np.load(directory / "raw/resolution.npy")
    expected = engine._process(
        raw,
        capture["readout"],
        np.asarray(capture["calibration"]["matrix_sensor_electrons_to_linear_srgb"]),
        spec["isp"],
    )
    artifacts = result["native_isp_artifacts"]["resolution"]
    assert artifacts["native_shape"] == list(NATIVE_SHAPE)
    for index, name in enumerate(("linear", "processed")):
        path = directory / artifacts[name]["path"]
        saved = np.load(path)
        assert saved.shape == (*NATIVE_SHAPE, 3)
        np.testing.assert_array_equal(saved, expected[index].astype(np.float32))
        assert engine.file_sha256(path) == artifacts[name]["sha256"]
    native = np.asarray(Image.open(directory / artifacts["display"]["path"]))
    np.testing.assert_array_equal(native, np.round(expected[2] * 255).astype(np.uint8))
    preview = Image.open(directory / result["images"]["isp"])
    assert native.shape[:2] == NATIVE_SHAPE
    assert preview.size == (640, 360)
    assert len(result["tests"]["resolution"]["sites"]) == 9
    assert result["tests"]["resolution"]["native_shape"] == list(NATIVE_SHAPE)


def test_wide_preview_preserves_entire_equidistant_window(wide_capture):
    _, _, capture, _ = wide_capture
    # A linear native coordinate ramp exposes accidental tan(FOV/2) padding:
    # the exact center-region slope must represent the whole native width.
    ramp = np.broadcast_to(np.arange(NATIVE_SHAPE[1]), NATIVE_SHAPE).astype(float)
    preview = engine._common(ramp, capture["geometry"])
    columns = np.array([80, 160, 320, 480, 560])
    expected = (columns + 0.5) * NATIVE_SHAPE[1] / 640 - 0.5
    np.testing.assert_allclose(preview[180, columns], expected, atol=1e-8)


def test_changed_isp_replay_preserves_source_raw_mean_and_native_isp(wide_capture, tmp_path):
    source, spec, _, source_result = wide_capture
    original = _hash_tree(source)
    changed = copy.deepcopy(spec)
    changed["isp"].update(denoise=0.95, sharpen=0, tone_gamma=1.5)
    first = tmp_path / "first"
    result = engine.replay(WORKSPACE, source, first, changed)
    assert _hash_tree(source) == original
    assert result["provenance"]["raw_replay"]
    for relative in original:
        if relative.startswith(("raw/", "arrays/")):
            assert engine.file_sha256(first / relative) == original[relative]
    old = source_result["native_isp_artifacts"]["resolution"]
    new = result["native_isp_artifacts"]["resolution"]
    assert old["processed"]["sha256"] != new["processed"]["sha256"]
    assert old["display"]["sha256"] != new["display"]["sha256"]
    assert old["linear"]["sha256"] == new["linear"]["sha256"]
    assert (source / old["processed"]["path"]).stat().st_ino != (
        first / new["processed"]["path"]
    ).stat().st_ino
    first_hashes = _hash_tree(first)
    again = copy.deepcopy(changed)
    again["isp"].update(denoise=0, sharpen=1.2)
    second = tmp_path / "second"
    replayed = engine.replay(WORKSPACE, first, second, again)
    assert _hash_tree(first) == first_hashes
    assert _hash_tree(source) == original
    assert replayed["provenance"]["raw_replay"]
    for relative in original:
        if relative.startswith(("raw/", "arrays/")):
            assert engine.file_sha256(second / relative) == original[relative]
    assert json.loads((second / "capture.json").read_text())["specification"] == spec


@pytest.mark.parametrize("field,value", [("projection", "rectilinear"), ("radial_blur_um", 3)])
def test_wide_replay_rejects_changed_optical_acquisition(wide_capture, tmp_path, field, value):
    source, spec, _, _ = wide_capture
    before = _hash_tree(source)
    changed = copy.deepcopy(spec)
    changed["wide_angle"][field] = value
    with pytest.raises(ValueError, match="acquisition field wide_angle"):
        engine.replay(WORKSPACE, source, tmp_path / "invalid", changed)
    assert _hash_tree(source) == before
    assert not (tmp_path / "invalid").exists()
