"""Reference field coordinates and same-RAW ISP with actual pixel/readout code.

Only the expensive optical solver is replaced by a deterministic angular-pupil
fixture. No test interprets this fixture as a validated lens prescription.
"""

import copy
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from camera_platform import reference_workshop_engine as engine
from camera_platform import reference_workshop_job as job
from camera_platform.registry import digest
from camera_platform.virtual_lab_job import sha256

WORKSPACE = Path(__file__).resolve().parents[2]


def _solution_fixture(spec):
    coordinates = [
        ("center", 0, 0),
        ("left", -0.5, 0),
        ("right", 0.5, 0),
        ("top", 0, 0.5),
        ("bottom", 0, -0.5),
        ("upper_left", -0.6, 0.6),
        ("upper_right", 0.6, 0.6),
        ("lower_left", -0.6, -0.6),
        ("lower_right", 0.6, -0.6),
    ]
    curvature = spec["lens"]["curvature_scale"]
    shift = spec["lens"]["sensor_shift_um"]
    fields = [
        {
            "id": name,
            "label": name,
            "status": "succeeded",
            "field_xy_normalized": [x, y],
            "object_direction_deg": [x * 12, y * 12],
            "image_xy_mm": [x * curvature + shift * 1e-4, y * curvature],
            "geometry": {
                "native_shape_row_col": [64, 64],
                "input_shape_row_col": [72, 72],
                "crop_bounds_row_col": [4, 68, 4, 68],
                "pixel_pitch_um": 2.9,
                "local_cfa_origin_row_col": [0, 0],
            },
            "fixture_transmission": (1 - 0.55 * (x * x + y * y)) * (1 + 20 * (curvature - 1)),
        }
        for name, x, y in coordinates
    ]
    # A refused field must remain an explicit hole, not receive the center image.
    fields[-1].update(status="failed", error="Fixture pupil refused this field")
    return {
        "result": {
            "revision": digest(spec["lens"]),
            "fields": fields,
            "reference": {"kind": "test angular-pupil fixture"},
        }
    }


def _apply_fixture(solution, field, photons, response):
    del solution
    wave = engine.WAVE
    qe = (
        engine.pixel.absolute_angular_response(response, 0, 0, wave)
        + engine.pixel.absolute_angular_response(response, 30, 75, wave)
    ) / 2
    # Apply pixel area and spectral integration exactly once in the mock optics,
    # mirroring ownership of the real adapter rather than fabricating RAW.
    widths = np.full(wave.size, 10.0)
    widths[[0, -1]] = 5
    transfer = qe * widths[:, None] * (2.9e-6) ** 2 * 0.01 * field["fixture_transmission"]
    source = np.asarray(photons)[4:68, 4:68]
    planes = np.einsum("hwk,ks->hws", source, transfer)
    yy, xx = np.indices(source.shape[:2])
    rate = np.take_along_axis(planes, ((yy % 2) * 2 + xx % 2)[..., None], axis=-1)[..., 0]
    return {
        "mean_electron_rate": rate,
        "center_electron_rate_per_band": source[32, 32, :, None] * transfer,
        "metadata": {
            "fixture": "mocked optics only",
            "pixel_aperture_count": 1,
            "qe_contract": "absolute replacement",
            "field_id": field["id"],
        },
    }


@pytest.fixture(scope="module")
def captured(tmp_path_factory):
    if not (WORKSPACE / "camerae2e_db/fdtd_tcad/image_sensor_db/sensor_catalog.json").exists():
        pytest.skip("Local reviewed sensor and spectral assets required")
    patch = pytest.MonkeyPatch()
    directory = tmp_path_factory.mktemp("reference-engine")
    optical_path = directory / "optical-source"
    optical_path.mkdir()
    (optical_path / "fixture.txt").write_text("Optical test fixture; not measured optics")
    patch.setattr(
        engine,
        "_solution",
        lambda workspace, spec, cache, callback: (_solution_fixture(spec), optical_path),
    )
    patch.setattr(engine, "_apply", _apply_fixture)
    # Stable test source identity while concurrent implementation files may change.
    patch.setattr(job, "fingerprint", lambda: {"fixture": "reference-integration-v1"})
    spec = job.specification(
        {"frames": 3, "luminance_cd_m2": 1000, "isp": {"display_white_level": 1}}
    )
    root = directory / "capture"
    result = engine.run(WORKSPACE, root, spec, directory / "cache")
    yield {"root": root, "result": result, "spec": spec, "cache": directory / "cache"}
    patch.undo()


def _files(root):
    return {str(path.relative_to(root)): sha256(path) for path in root.rglob("*") if path.is_file()}


def test_field_coordinates_failed_regions_and_actual_sensor_capture(captured):
    result, root, spec = captured["result"], captured["root"], captured["spec"]
    assert result["successful_fields"] == 8
    assert result["failed_fields"] == 1
    assert result["pixel"]["cell"]["pixel_pitch_um"] == 2.9
    source_fields = _solution_fixture(spec)["result"]["fields"]
    for actual, source in zip(result["fields"], source_fields, strict=True):
        if source["status"] == "failed":
            assert actual["images"] == {}
            assert actual["metrics"] == {}
            assert not (root / "fields" / source["id"]).exists()
            continue
        assert actual["optics"]["object_direction_deg"] == source["object_direction_deg"]
        assert actual["optics"]["image_xy_mm"] == source["image_xy_mm"]
        assert actual["geometry"]["native_shape_row_col"] == [64, 64]
        assert actual["tests"]["noise"]["uniform"]["raw_green"]["repeat_count"] == 3
        assert actual["tests"]["noise"]["uniform"]["raw_green"]["temporal_std"] > 0
        raw = np.load(root / actual["images"]["uniform"]["raw"])
        assert raw.shape == (64, 64) and raw.dtype.kind in "iu"
        assert raw.min() >= 0 and raw.max() <= 4095
        assert Image.open(root / actual["images"]["uniform"]["display"]).size == (64, 64)
    center, edge = result["fields"][0], result["fields"][1]
    assert center["metrics"]["mean_electrons"] > edge["metrics"]["mean_electrons"]


def test_calibration_uses_actual_angular_pupil_and_stays_fixed_cross_design(captured):
    spec, root = captured["spec"], captured["root"]
    original = json.loads((root / "calibration.json").read_text())
    spectral = engine._spectra(WORKSPACE, "D65", 1000)
    cell = engine.pixel.build_cell(WORKSPACE)
    response = engine.pixel.build_response(cell)
    field = _solution_fixture(spec)["result"]["fields"][0]
    applied = _apply_fixture({}, field, np.broadcast_to(spectral["white"], (72, 72, 21)), response)
    sites = applied["center_electron_rate_per_band"] * 0.004
    rgb = sites[:, [0, 1, 3]].copy()
    rgb[:, 1] = sites[:, 1:3].mean(axis=1)
    assert rgb.sum(axis=0) @ np.asarray(original["color"]["matrix"]) == pytest.approx(
        np.ones(3), abs=2e-7
    )
    changed = copy.deepcopy(spec)
    changed["lens"]["curvature_scale"] = 1.005
    changed["lens"]["sensor_shift_um"] = 20
    changed["pixel"]["cfa_thickness_scale"] = 1.1
    changed["luminance_cd_m2"] = 10000
    calibration = engine._calibrations(WORKSPACE, changed, captured["cache"], None)
    assert calibration == original
    assert calibration["color"]["per_candidate_fit"] is False
    assert calibration["lsc"]["policy"]["candidate_refit"] is False


def test_lsc_and_denoise_replay_preserve_raw_and_all_source_files(captured, tmp_path, monkeypatch):
    source, original, spec = captured["root"], captured["result"], captured["spec"]
    before = _files(source)
    changed = copy.deepcopy(spec)
    changed["isp"]["lsc_enabled"] = True
    changed["isp"]["denoise"] = 0.8
    monkeypatch.setattr(engine, "_solution", lambda *_: pytest.fail("Replay solved optics"))
    monkeypatch.setattr(engine, "_spectra", lambda *_: pytest.fail("Replay loaded current spectra"))
    target = tmp_path / "lsc-replay"
    result = engine.replay(source, target, changed)
    assert before == _files(source)
    assert result["calibration"] == original["calibration"]
    assert result["performance"]["acquisition_reused"]
    for field in result["fields"]:
        if field["status"] != "succeeded":
            continue
        assert field["lsc_applied"] is True
        old_field = next(item for item in original["fields"] if item["id"] == field["id"])
        assert field["capture_files"] == old_field["capture_files"]
        for kind, paths in field["images"].items():
            assert sha256(target / paths["raw"]) == sha256(source / paths["raw"])
            assert sha256(target / paths["mean"]) == sha256(source / paths["mean"])
            original_path = source / f"fields/{field['id']}/{kind}/source-reference-linear.npy"
            assert sha256(original_path) == sha256(target / original_path.relative_to(source))
        assert field["metrics"]["temporal_noise_dn"] == old_field["metrics"]["temporal_noise_dn"]
    assert sha256(source / "fields/left/texture/processed.npy") != sha256(
        target / "fields/left/texture/processed.npy"
    )


def test_display_white_is_explicit_not_per_frame_normalized(captured, tmp_path):
    source, spec = captured["root"], copy.deepcopy(captured["spec"])
    spec["isp"]["display_white_level"] = 2
    target = tmp_path / "display-replay"
    result = engine.replay(source, target, spec)
    for field, original in zip(result["fields"], captured["result"]["fields"], strict=True):
        if field["status"] != "succeeded":
            continue
        assert field["metrics"] == original["metrics"]
        folder = f"fields/{field['id']}/uniform"
        assert sha256(source / folder / "raw-00.npy") == sha256(target / folder / "raw-00.npy")
        assert sha256(source / folder / "processed.npy") == sha256(
            target / folder / "processed.npy"
        )
        processed = np.load(target / folder / "processed.npy")
        expected = np.round(np.clip(processed / 2, 0, 1) ** (1 / spec["isp"]["tone_gamma"]) * 255)
        assert np.asarray(Image.open(target / folder / "display.png")) == pytest.approx(
            expected, abs=1
        )
        assert sha256(source / folder / "display.png") != sha256(target / folder / "display.png")


@pytest.mark.parametrize("kind", ["raw", "reference", "calibration"])
def test_tampered_preserved_inputs_are_rejected(captured, tmp_path, kind):
    source = tmp_path / "tampered"
    shutil.copytree(captured["root"], source)
    paths = {
        "raw": source / "fields/center/uniform/raw-00.npy",
        "reference": source / "fields/center/uniform/source-reference-linear.npy",
        "calibration": source / "calibration.json",
    }
    path = paths[kind]
    if kind == "calibration":
        path.write_text(path.read_text() + " ")
    else:
        array = np.load(path)
        array.flat[0] += 1
        np.save(path, array)
    before = _files(source)
    with pytest.raises(ValueError, match="changed"):
        engine.replay(source, tmp_path / "replay", captured["spec"])
    assert _files(source) == before


def test_replay_rejects_acquisition_changes_and_overlapping_directories(captured, tmp_path):
    spec = copy.deepcopy(captured["spec"])
    spec["exposure_ms"] = 8
    with pytest.raises(ValueError, match="only ISP"):
        engine.replay(captured["root"], tmp_path / "changed-exposure", spec)
    for target in (captured["root"], captured["root"] / "nested", captured["root"].parent):
        with pytest.raises(ValueError, match="disjoint|overlap|preserved|source"):
            engine.replay(captured["root"], target, captured["spec"])
