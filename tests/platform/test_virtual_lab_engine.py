"""Critical native-capture and immutable ISP-replay integration checks."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import virtual_lab_engine as engine
from camera_platform.virtual_lab_job import default_specification

WORKSPACE = Path(__file__).resolve().parents[2]


def spec(**updates):
    value = default_specification(engine.catalog(WORKSPACE))
    value["optics"] = {"f_number": 4.12, "defocus_um": 0}
    value["field_model"] = {"mode": "center_local", "horizontal_fov_deg": None}
    value["angular_response"] = {"mode": "off", "exponents_r_gr_gb_b": [2, 2, 2, 2]}
    value.update(updates)
    return value


def test_catalog_keeps_db_optics_fixed_and_sensor_revisions():
    catalog = engine.catalog(WORKSPACE)
    assert [s["id"] for s in catalog["sensors"]] == ["S1", "S2"]
    for lens in catalog["lenses"][:2]:
        assert lens["supports"] == {
            "f_number": False, "defocus_um": False, "pupil_directions": True
        }
        assert lens["revision"]["asset_sha256"]
    assert catalog["lenses"][-1]["supports"]["f_number"]


def test_native_geometry_keeps_common_fov_and_saved_pitch():
    _, _, _, _, first = engine._load(WORKSPACE, spec())
    _, _, _, _, other = engine._load(
        WORKSPACE, spec(lens_id="L2", sensor_id="S2", optics={"f_number": 3.61, "defocus_um": 0})
    )
    assert first["native_shape"] == [360, 640]
    assert other["native_shape"][1] > 1000
    assert first["common_horizontal_fov_deg"] == other["common_horizontal_fov_deg"]
    assert other["pixel_pitch_um"] == 2.1


def test_db_controls_reject_silent_psf_mutation():
    with pytest.raises(ValueError, match="fixed f-number"):
        engine._load(WORKSPACE, spec(optics={"f_number": 2, "defocus_um": 0}))


def test_analytic_defocus_conserves_energy_and_broadens_psf():
    optics = {"f_number": 4}
    sharp = engine._kernel(optics, 550, 1, controls={"defocus_um": 0})
    soft = engine._kernel(optics, 550, 1, controls={"defocus_um": 80})

    def radius(k):
        y, x = np.meshgrid(
            np.arange(k.shape[0]) - k.shape[0] // 2,
            np.arange(k.shape[1]) - k.shape[1] // 2,
            indexing="ij",
        )
        return float(np.sum(k * (x * x + y * y)))

    assert sharp.sum() == pytest.approx(1)
    assert soft.sum() == pytest.approx(1)
    assert radius(soft) > radius(sharp)


@pytest.fixture(scope="module")
def capture(tmp_path_factory):
    directory = tmp_path_factory.mktemp("lab_native_capture")
    specification = spec(scene="uniform", test_suite="noise")
    result = engine.run(WORKSPACE, directory, specification)
    return directory, specification, result


def test_actual_native_capture_and_five_repeat_noise_levels(capture):
    directory, _, result = capture
    raw = np.load(directory / "raw/uniform.npy")
    assert raw.shape == (360, 640)
    assert raw.dtype == np.uint16
    noise = result["tests"]["noise"]
    assert noise["repeat_count"] == 8
    assert len(noise["photon_transfer_points"]) == 5
    assert all(x["repeat_count"] == 8 for x in noise["photon_transfer_points"])
    signals = [x["mean_electrons"] for x in noise["photon_transfer_points"]]
    assert np.all(np.diff(signals) > 0)
    assert len(result["raw_artifacts"]) == 41
    assert noise["channel"] == "Gr"
    assert set(noise["channels"]) == {"R", "Gr", "Gb", "B"}


def test_replay_changes_isp_preserves_raw_and_can_be_replayed(capture, tmp_path):
    directory, specification, _ = capture
    original_hash = engine.file_sha256(directory / "raw/uniform.npy")
    changed = copy.deepcopy(specification)
    changed["isp"].update(denoise=1, sharpen=0)
    result = engine.replay(WORKSPACE, directory, tmp_path / "first", changed)
    again = engine.replay(WORKSPACE, tmp_path / "first", tmp_path / "second", changed)
    assert result["provenance"]["raw_replay"] and again["provenance"]["raw_replay"]
    assert engine.file_sha256(directory / "raw/uniform.npy") == original_hash
    assert engine.file_sha256(tmp_path / "second/raw/uniform.npy") == original_hash
    assert (tmp_path / "second/capture.json").is_file()
    assert (
        result["tests"]["noise"]["after_isp"]["temporal_std"]
        < json.loads((directory / "result.json").read_text())["tests"]["noise"]["after_isp"][
            "temporal_std"
        ]
    )


def test_replay_rejects_new_exposure(capture, tmp_path):
    directory, specification, _ = capture
    changed = {**specification, "exposure_ms": 8}
    with pytest.raises(ValueError, match="exposure_ms"):
        engine.replay(WORKSPACE, directory, tmp_path, changed)


def test_replay_legacy_capture_defaults_new_field_settings(capture, tmp_path):
    """Old RAW remains replayable after API adds explicit local/off defaults."""
    import shutil

    directory, specification, _ = capture
    legacy = tmp_path / "legacy"
    shutil.copytree(directory, legacy)
    record = json.loads((legacy / "capture.json").read_text())
    record["specification"].pop("field_model")
    record["specification"].pop("angular_response")
    (legacy / "capture.json").write_text(json.dumps(record))
    original_hash = engine.file_sha256(legacy / "raw/uniform.npy")
    replay_spec = copy.deepcopy(specification)
    replay_spec["isp"].update(denoise=1, sharpen=0)
    result = engine.replay(WORKSPACE, legacy, tmp_path / "replayed", replay_spec)
    assert result["provenance"]["raw_replay"]
    assert engine.file_sha256(tmp_path / "replayed/raw/uniform.npy") == original_hash
    assert engine.file_sha256(legacy / "raw/uniform.npy") == original_hash
    # Compatibility must not permit changing the optical acquisition model.
    replay_spec["field_model"]["mode"] = "full_field"
    with pytest.raises(ValueError, match="field_model"):
        engine.replay(WORKSPACE, legacy, tmp_path / "invalid", replay_spec)


def test_replay_rejects_modified_raw(capture, tmp_path):
    directory, specification, _ = capture
    # Copy rather than hardlink so corruption never touches the fixture original.
    import shutil

    source = tmp_path / "corrupt"
    shutil.copytree(directory, source)
    raw = np.load(source / "raw/uniform.npy")
    raw[0, 0] += 1
    np.save(source / "raw/uniform.npy", raw)
    with pytest.raises(ValueError, match="hash changed"):
        engine.replay(WORKSPACE, source, tmp_path / "result", specification)


def test_illuminant_a_is_a_different_known_spectrum():
    d65 = engine._spectra(WORKSPACE, "D65", 1000)
    tungsten = engine._spectra(WORKSPACE, "A", 1000)
    assert d65["patches"].shape == (24, 21)
    assert not np.allclose(d65["white"], tungsten["white"])
    assert d65["white_xyz"][1] == pytest.approx(1)
    assert tungsten["source_white_xyz"][1] == pytest.approx(1)
    assert d65["chromatic_adaptation"] == tungsten["chromatic_adaptation"]


def test_fixed_d65_white_constraint_matches_actual_native_capture(capture):
    from pyisetcam.sensor import _sensor_qe_on_wave

    directory, specification, result = capture
    lens, _, optics, sensor, _ = engine._load(WORKSPACE, specification)
    spectral = engine._spectra(WORKSPACE, "D65", 1000)
    calibration, provenance = engine._calibration(WORKSPACE, sensor, optics)
    qe = _sensor_qe_on_wave(sensor, engine.WAVE)
    area = np.prod(sensor.fields["pixel"]["size_m"]) * sensor.fields["pixel"]["fill_factor"]
    radiometry = np.pi / (1 + 4 * lens["model"]["effective_f_number"] ** 2)
    white_electrons = spectral["white"] @ qe * 10 * area * radiometry * 0.004
    mean = np.load(directory / "arrays/uniform-mean-electrons.npy")
    native_channels = np.array(
        [
            mean[::2, ::2].mean(),
            (mean[::2, 1::2].mean() + mean[1::2, ::2].mean()) / 2,
            mean[1::2, 1::2].mean(),
        ]
    )
    assert np.allclose(native_channels / 0.18, white_electrons, rtol=1e-6)
    assert np.allclose(white_electrons @ calibration, [1, 1, 1], atol=2e-6)
    assert np.allclose(native_channels @ calibration, [0.18, 0.18, 0.18], atol=2e-6)
    assert provenance["test_chart_fit"] is False
    assert provenance["chromatic_adaptation"]["scene_white_estimated"] is False
    raw = np.load(directory / "raw/uniform.npy")
    linear = engine._process(
        raw, result["provenance"]["readout"], calibration, specification["isp"]
    )[0]
    assert np.allclose(linear[8:-8, 8:-8].mean(axis=(0, 1)), [0.18] * 3, atol=0.002)


def test_fixed_white_calibration_preserves_a_warmth_and_light_scale():
    from pyisetcam.sensor import _sensor_qe_on_wave

    lens, _, optics, sensor, _ = engine._load(WORKSPACE, spec())
    calibration, _ = engine._calibration(WORKSPACE, sensor, optics)
    qe = _sensor_qe_on_wave(sensor, engine.WAVE)
    area = np.prod(sensor.fields["pixel"]["size_m"]) * sensor.fields["pixel"]["fill_factor"]
    radiometry = np.pi / (1 + 4 * lens["model"]["effective_f_number"] ** 2)

    def white_rgb(illuminant, luminance):
        spectrum = engine._spectra(WORKSPACE, illuminant, luminance)["white"]
        return (spectrum @ qe * 10 * area * radiometry * 0.004) @ calibration

    d65 = white_rgb("D65", 1000)
    dim = white_rgb("D65", 100)
    warm = white_rgb("A", 1000)
    assert np.allclose(dim, d65 * 0.1, atol=1e-7)
    assert warm[0] > warm[2] * 1.2
    assert not np.allclose(warm, d65, atol=0.05)


def test_black_offset_and_manual_lsc_precede_demosaic():
    raw = np.full((64, 64), 164, np.uint16)
    readout = {"bit_depth": 12, "black_level_dn": 64, "gain": 1, "full_well_e": 10000}
    tuning = spec()["isp"]
    tuning.update(denoise=0, sharpen=0)
    reference = engine._process(raw, readout, np.eye(3), tuning)[0]
    shifted = engine._process(raw, readout, np.eye(3), {**tuning, "black_offset_dn": 10})[0]
    shaded = engine._process(raw, readout, np.eye(3), {**tuning, "lsc_strength": 1})[0]
    assert np.allclose(shifted[8:-8, 8:-8], reference[8:-8, 8:-8] * 0.9)
    assert shaded[4, 4].mean() > shaded[32, 32].mean() * 1.5
    assert np.all(raw == 164)
