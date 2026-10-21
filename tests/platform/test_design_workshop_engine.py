"""Real sensor/ISP integration with a bounded deterministic optical test fixture."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from camera_platform import design_workshop_engine as engine
from camera_platform import design_workshop_job as job
from camera_platform.virtual_lab_job import sha256


def _optical_fixture():
    size, halo = [96, 144], 4
    psf = np.zeros((21, 3, 3, 3, 3))
    psf[..., 1, 1] = 1
    angle = np.deg2rad(30)
    # Deliberately non-normal rays expose a calibration using the wrong normal QE.
    directions = np.tile([[0, 0, 1], [np.sin(angle), 0, np.cos(angle)]], (3, 1, 1))
    return {
        "result": {
            "status": "PROPOSED",
            "revision": "test-pupil-two-rays",
            "geometry": {
                "native_shape_row_col": size,
                "input_shape_row_col": [n + 2 * halo for n in size],
                "crop_bounds_row_col": [halo, halo + size[0], halo, halo + size[1]],
                "pixel_pitch_um": 3.45,
            },
        },
        "arrays": {
            "psf": psf,
            "field_x_mm": np.array([-0.5, 0, 0.5]),
            "field_y_mm": np.array([-0.5, 0, 0.5]),
            "image_height_mm": np.array([0, 0.35, 0.71]),
            "irradiance_per_radiance_sr": np.full((21, 3), 0.01),
            "directions_xyz": directions,
            "conditional_weights": np.full((21, 3, 2), 0.5),
        },
    }


@pytest.fixture()
def captured(tmp_path, monkeypatch):
    workspace = Path(__file__).resolve().parents[2]
    if not (workspace / "camerae2e_db/fdtd_tcad/image_sensor_db/sensor_catalog.json").exists():
        pytest.skip("Local canonical sensor and spectral assets are required")
    solution = _optical_fixture()
    monkeypatch.setattr(engine, "_solution", lambda *_: solution)
    spec = job.specification({"frames": 3})
    root = tmp_path / "capture"
    result = engine.run(workspace, root, spec, tmp_path / "cache")
    return root, result, spec, workspace, solution


def test_actual_pupil_calibration_and_pinned_reference(captured):
    root, result, spec, workspace, solution = captured
    calibration = result["calibration"]
    assert (
        calibration["spectral_response_basis"] == "actual_center_pupil_averaged_electrons_per_band"
    )
    spectral = engine._spectra(workspace, "D65", 1000)
    cell = result["pixel"]["cell"]
    response = result["pixel"]["response"]
    shape = solution["result"]["geometry"]["input_shape_row_col"]
    photons = np.broadcast_to(spectral["white"], (*shape, 21))
    applied = engine._apply(solution, photons, response)
    bands = applied["center_electron_rate_per_band"] * 0.004
    rgb = bands[:, [0, 1, 3]].copy()
    rgb[:, 1] = bands[:, 1:3].mean(axis=1)
    assert rgb.sum(axis=0) @ np.asarray(calibration["matrix"]) == pytest.approx(
        np.ones(3), abs=2e-7
    )
    capture = json.loads((root / "capture.json").read_text())
    assert capture["scene_reference"]["source_workspace"] == str(workspace)
    assert capture["calibration"]["baseline_cell_sha256"] == cell["cell_sha256"]
    assert capture["configuration"] == spec
    assert result["tests"]["texture"]["noise_enabled"] is False
    assert result["tests"]["noise"]["uniform"]["raw_green"]["repeat_count"] == 3
    assert result["tests"]["noise"]["uniform"]["raw_green"]["temporal_std"] > 0


def test_native_reference_raw_preview_and_same_raw_replay(captured, tmp_path, monkeypatch):
    root, result, spec, *_ = captured
    before = {str(p.relative_to(root)): sha256(p) for p in root.rglob("*") if p.is_file()}
    changed = copy.deepcopy(spec)
    changed["isp"]["denoise"] = 0.9
    changed["isp"]["sharpen"] = 0.5
    # Replay must use captured spectral references, not reopen current workspace assets.
    monkeypatch.setattr(engine, "_spectra", lambda *_: pytest.fail("Replay reloaded reference"))
    replay_root = tmp_path / "replay"
    replayed = engine.replay(root, replay_root, changed)
    assert replayed["calibration"] == result["calibration"]
    assert before == {str(p.relative_to(root)): sha256(p) for p in root.rglob("*") if p.is_file()}
    for kind, images in result["images"].items():
        assert Image.open(root / images["reference"]).size == (144, 96)
        assert Image.open(root / images["raw_preview"]).size == (144, 96)
        raw = np.load(root / images["raw"])
        black, white = result["raw_preview"]["black_dn"], result["raw_preview"]["white_dn"]
        expected = np.round(np.clip((raw.astype(float) - black) / (white - black), 0, 1) * 255)
        assert np.asarray(Image.open(root / images["raw_preview"])) == pytest.approx(expected)
        for name, expected_hash in before.items():
            if name.startswith(kind + "/") and (
                name.endswith(".npy")
                and "/raw-" in name
                or name.endswith("mean-electron-rate.npy")
                or name.endswith("source-reference-linear.npy")
            ):
                assert sha256(replay_root / name) == expected_hash
    assert sha256(root / "texture/processed.npy") != sha256(replay_root / "texture/processed.npy")
    assert replayed["tests"]["texture"]["contrast_ratio_to_linear"] != pytest.approx(
        result["tests"]["texture"]["contrast_ratio_to_linear"]
    )
    invalid = copy.deepcopy(changed)
    invalid["exposure_ms"] *= 2
    with pytest.raises(ValueError, match="only ISP"):
        engine.replay(root, tmp_path / "invalid", invalid)


def test_declared_source_white_keeps_color_error_scale_invariant_without_output_fitting():
    workspace = Path(__file__).resolve().parents[2]
    normal = engine._spectra(workspace, "D65", 1000)
    bright = engine._spectra(workspace, "D65", 10000)
    assert normal["white_xyz"] == pytest.approx([0.95047, 1, 1.08883])
    assert bright["white_xyz"] == pytest.approx(10 * normal["white_xyz"])
    assert bright["reference_xyz"] == pytest.approx(10 * normal["reference_xyz"])
    assert bright["chromatic_adaptation"] == normal["chromatic_adaptation"]

    xyz_to_rgb = np.linalg.inv(engine.metrics.LINEAR_SRGB_TO_XYZ.T)
    measured = (normal["reference_xyz"] @ xyz_to_rgb) * np.array([1.04, 0.97, 1.01])
    rois = [[0, 1, index, index + 1] for index in range(24)]

    def evaluate(image, reference):
        return engine.metrics.color_metrics(
            image[None, ...],
            reference["reference_xyz"],
            patch_rois=rois,
            white_xyz=reference["white_xyz"],
        )["delta_e00_mean"]

    original_error = evaluate(measured, normal)
    assert original_error > 0.1
    assert evaluate(10 * measured, bright) == pytest.approx(original_error, abs=1e-10)
    # Half the measured response with unchanged declared illumination is still
    # an exposure/throughput error, rather than an independently normalized image.
    assert evaluate(5 * measured, bright) > original_error + 5


def test_explicit_display_range_preserves_raw_and_measurements(captured, tmp_path):
    root, original, spec, *_ = captured
    changed = copy.deepcopy(spec)
    changed["isp"]["display_white_level"] = 10
    target = tmp_path / "display-range"
    result = engine.replay(root, target, changed)
    assert result["metrics"] == original["metrics"]
    assert result["capture_files"] == original["capture_files"]
    for kind in original["images"]:
        for name in ("raw-00.npy", "linear.npy", "processed.npy", "source-reference-linear.npy"):
            assert sha256(root / kind / name) == sha256(target / kind / name)
        for name in ("display.png", "linear.png", "source-reference.png"):
            assert sha256(root / kind / name) != sha256(target / kind / name)
