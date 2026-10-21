"""Physical coordinates, native area integration and verified tile reuse."""

import copy
from pathlib import Path

import numpy as np
import pytest

from camera_platform import virtual_lab_engine as engine
from camera_platform.virtual_lab_job import default_specification
from camera_platform.virtual_lab_wide import TileOptics, build_angular_table, capture, preflight
from camera_platform.virtual_lab_wide_geometry import direction_to_sensor, sensor_to_direction

WORKSPACE = Path(__file__).resolve().parents[2]


def configured(shape=(1080, 1920), **wide_updates):
    specification = default_specification(engine.catalog(WORKSPACE))
    specification.update(lens_id="wide_parametric", scene="grid", test_suite="resolution")
    specification["optics"]["f_number"] = 2.8
    specification["field_model"]["horizontal_fov_deg"] = 120
    specification["angular_response"]["mode"] = "off"
    specification["wide_angle"].update(
        native_shape_row_col=list(shape), sampling=2, min_free_disk_bytes=0, **wide_updates
    )
    lens, saved, optics, sensor, geometry = engine._load(WORKSPACE, specification)
    spectral = engine._spectra(WORKSPACE, "D65", 1000)
    context = {
        "angular_response": specification["angular_response"],
        "specification": specification,
        "wide_angular_table": build_angular_table(
            geometry, specification["optics"], specification["angular_response"], None, engine.WAVE
        ),
    }
    evaluator = TileOptics(optics, sensor, spectral, geometry, specification["optics"], context)
    return specification, optics, sensor, spectral, geometry, context, evaluator


@pytest.mark.parametrize("projection", ["rectilinear", "equidistant"])
def test_projection_roundtrip_and_horizontal_boundary(projection):
    *_, geometry, _, _ = configured(projection=projection)
    pitch = geometry["pixel_pitch_um"] * 0.001
    x = np.array([0, 1920 * pitch / 2, -1920 * pitch / 2, 0.8])
    y = np.array([0, 0, 0, 0.5])
    directions = sensor_to_direction(x, y, geometry)
    np.testing.assert_allclose(
        direction_to_sensor(directions, geometry), np.stack([x, y], -1), atol=1e-12
    )
    assert np.degrees(np.arccos(directions[1, 2])) == pytest.approx(60)


def test_uniform_qe_area_exposure_applied_once_at_global_cfa_sites():
    spec, _, sensor, spectral, geometry, _, evaluator = configured()
    values = evaluator.evaluate("uniform", [201, 213, 303, 319], 2)["mean"]
    irradiance = np.pi / (1 + 4 * spec["optics"]["f_number"] ** 2)
    expected = spectral["white"] @ evaluator.qe * 10 * irradiance * 0.18
    expected *= (geometry["pixel_pitch_um"] * 1e-6) ** 2 * sensor.fields["integration_time"]
    r, c = np.meshgrid(np.arange(201, 213), np.arange(303, 319), indexing="ij")
    channel = np.array([[0, 1], [1, 2]])[r % 2, c % 2]
    np.testing.assert_allclose(values, expected[channel], rtol=2e-12)


def test_nonzero_field_blur_is_independent_of_tile_partition():
    *_, evaluator = configured(radial_blur_um=5, tangential_blur_um=0.5)
    whole = evaluator.evaluate("grid", [170, 188, 250, 274], 2)["mean"]
    split = np.concatenate(
        [
            evaluator.evaluate("grid", [170, 179, 250, 274], 2)["mean"],
            evaluator.evaluate("grid", [179, 188, 250, 274], 2)["mean"],
        ]
    )
    np.testing.assert_allclose(split, whole, rtol=2e-12, atol=1e-12)


def test_parametric_blur_radial_axis_uses_physical_sensor_aspect():
    *_, evaluator = configured(radial_blur_um=5, tangential_blur_um=0.5)
    kernel = evaluator.kernel(10, 2, (4, 4))
    y, x = np.meshgrid(
        np.arange(kernel.shape[0]) - kernel.shape[0] // 2,
        np.arange(kernel.shape[1]) - kernel.shape[1] // 2,
        indexing="ij",
    )
    moment = np.array(
        [
            [np.sum(kernel * x * x), np.sum(kernel * x * y)],
            [np.sum(kernel * x * y), np.sum(kernel * y * y)],
        ]
    )
    vector = np.linalg.eigh(moment)[1][:, -1]
    angle = np.degrees(np.arctan2(vector[1], vector[0])) % 180
    assert angle == pytest.approx(np.degrees(np.arctan2(1080, 1920)), abs=0.2)


def test_resource_preflight_refuses_instead_of_reducing_sampling(tmp_path):
    spec, _, _, _, geometry, _, _ = configured(max_working_bytes=1000)
    with pytest.raises(ValueError, match="resource policy"):
        preflight(tmp_path, geometry, spec["optics"], chart_count=4, noise_repeats=80)
    assert geometry["wide_angle"]["sampling"] == 2


def test_verified_tiles_resume_and_changed_exposure_refuses(tmp_path):
    spec, optics, sensor, spectral, geometry, context, _ = configured((72, 96), tile_size=32)
    context.update(
        output_dir=str(tmp_path), wide_preflight=preflight(tmp_path, geometry, spec["optics"])
    )
    first, _, _ = capture(
        optics, sensor, spectral, "uniform", geometry, spec["optics"], 42, context
    )
    raw = np.array(first.data["raw_dn"])
    second, _, _ = capture(
        optics, sensor, spectral, "uniform", geometry, spec["optics"], 42, context
    )
    np.testing.assert_array_equal(second.data["raw_dn"], raw)
    assert second.metadata["virtual_lab_wide"]["checkpoint"]["reused_tiles"] == 9
    changed = sensor.clone()
    changed.fields["integration_time"] *= 2
    with pytest.raises(ValueError, match="checkpoint identity mismatch"):
        capture(optics, changed, spectral, "uniform", geometry, spec["optics"], 42, context)
    tile = next((tmp_path / "wide-checkpoints/uniform").glob("tile-*.npz"))
    tile.write_bytes(tile.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="tile hash"):
        capture(
            optics,
            sensor,
            spectral,
            "uniform",
            geometry,
            spec["optics"],
            42,
            copy.deepcopy(context),
        )
