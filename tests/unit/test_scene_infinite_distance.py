"""An infinite object conjugate preserves angular scene geometry and radiance."""
from pathlib import Path

import numpy as np
import pytest

from pyisetcam import (
    camera_compute,
    camera_create,
    camera_get,
    camera_set,
    lens_patent_raytrace_optics,
    oi_create,
    scene_create,
    scene_get,
    scene_set,
)


@pytest.mark.parametrize("shape", [(16, 16), (12, 24), (24, 12)])
def test_infinite_scene_preserves_rectangular_angles_and_radiance(shape):
    scene = scene_create("uniform ee", shape, np.array([450., 550., 650.]))
    scene = scene_set(scene, "fov", 20.)
    photons = np.asarray(scene_get(scene, "photons")).copy()
    energy = np.asarray(scene_get(scene, "energy")).copy()
    finite_vfov = scene_get(scene, "vfov")
    finite_hfov = scene_get(scene, "hfov")
    infinite = scene_set(scene, "distance", np.inf)
    assert scene_get(infinite, "distance") == np.inf
    assert scene_get(infinite, "hfov") == finite_hfov
    assert scene_get(infinite, "vfov") == pytest.approx(finite_vfov, abs=1e-12)
    assert np.isfinite(scene_get(infinite, "vfov"))
    assert scene_get(infinite, "size") == shape
    assert scene_get(infinite, "width") == scene_get(infinite, "height") == np.inf
    np.testing.assert_array_equal(scene_get(infinite, "photons"), photons)
    np.testing.assert_array_equal(scene_get(infinite, "energy"), energy)


def test_infinite_scene_fov_and_resize_use_angular_aspect():
    scene = scene_set(scene_create("uniform ee", (12, 24), [550.]), "distance", np.inf)
    scene = scene_set(scene, "fov", 30.)
    scene = scene_set(scene, "resize", [24, 12])
    # Projected height is twice the projected width after portrait resize.
    half_hfov = np.deg2rad(scene_get(scene, "hfov")) / 2
    half_vfov = np.deg2rad(scene_get(scene, "vfov")) / 2
    assert np.tan(half_vfov) / np.tan(half_hfov) == pytest.approx(2.)
    assert scene_get(scene, "distance") == np.inf


@pytest.mark.parametrize("distance", [0.25, 1.2, 50.])
def test_returning_from_infinity_preserves_finite_geometry(distance):
    scene = scene_set(scene_create("uniform ee", (12, 24), [550.]), "fov", 20.)
    direct = scene_set(scene.clone(), "distance", distance)
    roundtrip = scene_set(scene_set(scene.clone(), "distance", np.inf), "distance", distance)
    for key in ("width", "height", "vfov", "hfov", "distance"):
        assert scene_get(roundtrip, key) == scene_get(direct, key)
    np.testing.assert_array_equal(scene_get(roundtrip, "photons"), scene_get(direct, "photons"))


def test_infinite_scene_runs_through_strict_v2_camera_sample():
    root = Path(__file__).resolve().parents[2]
    data = root / "camerae2e_db/lens_db/CameraE2E_Lens_DB_v9_20260627/data/lens_patents"
    database = data / "lens_patent_simulation_v9.sqlite"
    psf_dir = data / "raytrace_psf_v2"
    if not database.exists() or not (psf_dir / "p0019_intermediate.npz").exists():
        pytest.skip("Locally regenerated v2 patent fixture is not installed.")
    optics = lens_patent_raytrace_optics(
        "p0019:intermediate", db_path=database, psf_dir=psf_dir,
        target_psf_size=64, allow_approximate_radiometry=True,
    )
    scene = scene_set(scene_create("uniform ee", 16, [550.]), "distance", np.inf)
    assert scene_get(scene, "vfov") == pytest.approx(scene_get(scene, "hfov"))
    camera = camera_set(camera_create(), "oi", oi_create("ray trace"))
    camera = camera_set(camera, "optics", optics)
    camera = camera_set(camera, "sensor size", [16, 16])
    computed = camera_compute(camera, scene, sensor_resize=False)
    volts = np.asarray(camera_get(computed, "sensor volts"))
    image = np.asarray(camera_get(computed, "image"))
    assert volts.shape == (16, 16)
    assert image.shape == (16, 16, 3)
    assert np.isfinite(volts).all() and np.isfinite(image).all()
    assert volts.max() > 0 and image.max() > 0
    assert camera_get(computed, "optics")["raytrace"]["contract_version"] == 2
