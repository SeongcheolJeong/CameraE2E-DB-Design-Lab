"""Physical reference checks and a real 3D -> PSF -> RAW/RGB evidence chain."""

import copy
import io
import json
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from camera_platform import (
    factory,
    imaging,
    prescription_psf,
    scene_camera,
    scene_render,
    scene_templates,
    spatial,
)
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import PROVENANCE, seed
from camera_platform.evidence import build_bundle
from camera_platform.scene_contracts import (
    Geometry,
    Light,
    SceneAssetCreate,
    SceneRenderCreate,
    Spectrum,
)
from camera_platform.service import PlatformService

PYTHON = Path(__file__).resolve().parents[2] / ".venv-scene-render/bin/python"
SCRIPT = Path(scene_render.__file__).with_name("mitsuba_scene.py")
WAVE = [450.0, 550.0, 650.0]


def spec(values):
    return {"wavelength_nm": WAVE, "values": values}


def plane(z, scale=1.0):
    return [
        [-scale, 0.0, 0.0, 0.0],
        [0.0, scale, 0.0, 0.0],
        [0.0, 0.0, -scale, z],
        [0.0, 0.0, 0.0, 1.0],
    ]


def basic_request():
    return {
        "shape": [8, 8],
        "wavelength_nm": WAVE,
        "samples_per_pixel": 4,
        "max_depth": 4,
        "seed": 321,
        "pose": {"origin_m": [0.0, 0.0, 0.0], "target_m": [0.0, 0.0, 3.0], "up": [0.0, 1.0, 0.0]},
        "projection": {"focal_length_mm": 4.0},
        "sampling": {"full_sensor_shape": [8, 8], "crop_origin": [0, 0], "pixel_pitch_um": 10.0},
        "assets": [
            {"id": "g", "payload": {"primitive": "rectangle"}},
            {"id": "m", "payload": {"bsdf": "diffuse", "spectrum": spec([0.5, 0.5, 0.5])}},
            {"id": "light", "payload": {"spectrum": spec([1.0, 2.0, 3.0])}},
        ],
        "scene": {
            "payload": {
                "instances": [
                    {
                        "geometry_id": "g",
                        "material_id": "m",
                        "area_light_id": "light",
                        "to_world": plane(3.0),
                    }
                ],
                "environment_light_id": None,
                "point_lights": [],
            }
        },
    }


def render(tmp_path, request):
    if not PYTHON.exists():
        pytest.skip("Isolated Mitsuba environment is not installed")
    tmp_path.mkdir(exist_ok=True)
    source = tmp_path / "input.json"
    source.write_text(json.dumps(request))
    result = subprocess.run(
        [str(PYTHON), "-I", str(SCRIPT), str(source), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return {
        name: np.load(tmp_path / (name + ".npy"), allow_pickle=False)
        for name in [
            "spectral-radiance",
            "radiance-mc-standard-error",
            "first-hit-depth-m",
            "first-hit-object-index",
        ]
    }


def test_fixed_wavelength_emission_units_and_exact_seed_replay(tmp_path):
    request = basic_request()
    a, b = render(tmp_path / "a", request), render(tmp_path / "b", request)
    np.testing.assert_allclose(a["spectral-radiance"], np.broadcast_to([1.0, 2.0, 3.0], (8, 8, 3)))
    assert not np.any(a["radiance-mc-standard-error"])
    for name in a:
        np.testing.assert_array_equal(a[name], b[name])
    assert np.all(a["first-hit-depth-m"] >= 3.0)


def test_point_light_matches_lambertian_inverse_square_not_rgb(tmp_path):
    req = basic_request()
    req["scene"]["payload"]["instances"][0]["area_light_id"] = None
    req["scene"]["payload"]["point_lights"] = [{"light_id": "light", "position_m": [0.0, 0.0, 0.0]}]
    a = render(tmp_path / "near", req)
    y, x = np.indices((8, 8))
    xy2 = ((x + 0.5 - 4) ** 2 + (4 - y - 0.5) ** 2) * (0.01 / 4 * 3) ** 2
    distance = np.sqrt(9 + xy2)
    expected = (0.5 / np.pi * 3 / distance**3)[..., None] * [1.0, 2.0, 3.0]
    np.testing.assert_allclose(a["spectral-radiance"], expected, rtol=2e-5)
    req["scene"]["payload"]["point_lights"][0]["position_m"] = [0.0, 0.0, -3.0]
    b = render(tmp_path / "far", req)
    expected_far = (0.5 / np.pi * 6 / (36 + xy2) ** 1.5)[..., None] * [1.0, 2.0, 3.0]
    np.testing.assert_allclose(b["spectral-radiance"], expected_far, rtol=2e-5)


def test_triangle_geometry_occludes_and_preserves_material_reuse(tmp_path):
    req = basic_request()
    # A separately placed black triangle blocks only the left half of the emitter.
    req["assets"].extend(
        [
            {
                "id": "mesh",
                "payload": {
                    "primitive": "triangle_mesh",
                    "vertices": [[-0.2, -0.2, 0.0], [0.0, 0.2, 0.0], [0.0, -0.2, 0.0]],
                    "faces": [[0, 1, 2]],
                },
            },
            {"id": "black", "payload": {"bsdf": "diffuse", "spectrum": spec([0.0, 0.0, 0.0])}},
        ]
    )
    req["scene"]["payload"]["instances"].append(
        {
            "geometry_id": "mesh",
            "material_id": "black",
            "area_light_id": None,
            "to_world": [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 1.0],
                [0.0, 0.0, 0.0, 1.0],
            ],
        }
    )
    result = render(tmp_path, req)
    assert set(np.unique(result["first-hit-object-index"])) == {0, 1}
    foreground = result["first-hit-object-index"] == 1
    assert not np.any(result["spectral-radiance"][foreground])
    np.testing.assert_allclose(
        result["spectral-radiance"][~foreground],
        np.broadcast_to([1.0, 2.0, 3.0], (np.sum(~foreground), 3)),
    )


def half_plane_request(bright_fraction):
    """An opaque edge at a known fraction of native pixel (4, 4)."""
    req = basic_request()
    # At z=1 m, a 10 um sensor pixel at f=4 mm spans 0.0025 m.
    pixel_width_m = 0.010 / 4.0
    pixel_left_m = (4.0 - 8.0 / 2) * pixel_width_m
    edge_m = pixel_left_m + bright_fraction * pixel_width_m
    req["assets"].extend([
        {
            "id": "half_plane",
            "payload": {
                "primitive": "triangle_mesh",
                "vertices": [
                    [edge_m, -1.0, 1.0], [1.0, -1.0, 1.0],
                    [1.0, 1.0, 1.0], [edge_m, 1.0, 1.0],
                ],
                "faces": [[0, 1, 2], [0, 2, 3]],
            },
        },
        {"id": "black", "payload": {"bsdf": "diffuse", "spectrum": spec([0.0, 0.0, 0.0])}},
    ])
    req["scene"]["payload"]["instances"].append({
        "geometry_id": "half_plane", "material_id": "black", "area_light_id": None,
        "to_world": [
            [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0],
        ],
    })
    return req


def test_opt_in_native_source_area_sampling_resolves_half_plane_with_known_bias(tmp_path):
    expected_spectrum = np.array([1.0, 2.0, 3.0])
    centre = render(tmp_path / "centre", half_plane_request(0.5001))
    two_request = half_plane_request(0.5001)
    two_request["source_pixel_quadrature"] = {
        "model": "native_sensor_area_midpoint_grid_v1", "grid_side": 2,
    }
    two = render(tmp_path / "two", two_request)
    np.testing.assert_allclose(centre["spectral-radiance"][4, 4], expected_spectrum)
    np.testing.assert_allclose(two["spectral-radiance"][4, 4], expected_spectrum / 2)
    np.testing.assert_allclose(two["spectral-radiance"][4, 3], expected_spectrum)
    np.testing.assert_allclose(two["spectral-radiance"][4, 5], 0)
    assert two["first-hit-object-index"][4, 4] == centre["first-hit-object-index"][4, 4]
    assert np.all(two["radiance-mc-standard-error"] == 0)
    summary = json.loads((tmp_path / "two" / "renderer-summary.json").read_text())
    assert summary["source_pixel_quadrature"] == {
        "model": "native_sensor_area_midpoint_grid_v1",
        "grid_side": 2,
        "integrator_path_samples": 8 * 8 * 3 * 4 * 4,
        "stochastic_paths_per_subcell_wavelength": 4,
        "sampler_seed": 321,
        "sampler_order": "crop_row_column_subcell_row_column_wavelength_path",
        "subcell_centres_yx": [[0.25, 0.25], [0.25, 0.75], [0.75, 0.25], [0.75, 0.75]],
        "subcell_weight": 0.25,
        "subcell_coordinates": "native_sensor_pixel_yx_from_top_left",
        "projection_geometry_interpretation": "pinhole_pixel_centre_reference_with_subcell_offsets",
        "source_cell_representation": "area_mean_radiance_assigned_to_native_cell_centre",
        "first_hit_auxiliary": "pixel_centre_ray_only",
        "spatial_quadrature_error": "unknown",
        "continuous_scene_psf_integration": False,
        "receiver_aperture_filter_applied_here": False,
        "physical_validation": "not_established",
    }
    # An unaligned 30% bright half-plane exposes remaining midpoint-grid bias.
    # This is an independent area reference, not a convergence certificate.
    results = {}
    for side in (2, 4):
        req = half_plane_request(0.3)
        req["source_pixel_quadrature"] = {
            "model": "native_sensor_area_midpoint_grid_v1", "grid_side": side,
        }
        results[side] = render(tmp_path / f"fraction-{side}", req)["spectral-radiance"][4, 4]
    np.testing.assert_allclose(results[2], expected_spectrum * 0.5)
    np.testing.assert_allclose(results[4], expected_spectrum * 0.25)
    assert np.max(np.abs(results[4] - expected_spectrum * 0.3)) < np.max(
        np.abs(results[2] - expected_spectrum * 0.3)
    )
    # A one-cell pixel-integrated PSF is identity; the receiver is not filtered
    # a second time after source-cell averaging.
    projection = {
        **PROVENANCE,
        "projection_model": "paraxial_on_axis_projection_v1",
        "focal_length_mm": 4.0, "f_number": 8.0,
        "on_axis_transmittance": spec([0.8, 0.8, 0.8]),
        "geometry_model": "pinhole_pixel_centre",
        "irradiance_model": "pi_L_T_over_4N2_before_relative_illumination",
        "approximation_uncertainty": "unknown",
    }
    irradiance = scene_render.project_radiance(two["spectral-radiance"], projection)
    identity_psf = spatial.validate_psf({
        "schema": "intensity_psf_v1", "wavelength_nm": WAVE,
        "values": np.ones((3, 1, 1)).tolist(), "sample_pitch_um": 10.0,
        "normalization": "unit_sum", "centering": "array_center",
        "spatial_model": "locally_shift_invariant",
        "boundary": "zero_outside_supplied_image",
        "sampling_model": "pixel_integrated_optical_impulse",
        "source": {"kind": "simulated", "description": "Synthetic identity PSF for area-edge test"},
    })
    blurred, _ = spatial._convolve(
        irradiance, np.asarray(identity_psf["values"]), identity_psf["sample_pitch_um"]
    )
    np.testing.assert_allclose(blurred, irradiance, atol=1e-15)
    np.testing.assert_allclose(
        blurred[4, 4], expected_spectrum * 0.5 * np.pi * 0.8 / 256,
        atol=1e-15,
    )


def test_source_area_edge_psf_oracle_exposes_grid_bias_and_flux(tmp_path):
    """Compare the real renderer/FFT path with analytic area and direct PSF sums."""
    kernel = np.array([
        [0.0, 0.1, 0.0],
        [0.1, 0.5, 0.2],
        [0.0, 0.1, 0.0],
    ])
    kernel = np.repeat(kernel[None, ...], len(WAVE), axis=0)
    spectrum = np.array([1.0, 2.0, 3.0])
    pitch_um = 10.0
    pixel_area_m2 = (pitch_um * 1e-6) ** 2
    # The black foreground covers x >= 4.3 native cells, so the true bright
    # source fraction at column 4 is 0.3, independent of the renderer grid.
    fraction = np.broadcast_to(np.clip(4.3 - np.arange(8), 0, 1), (8, 8))
    oracle_irradiance = fraction[..., None] * spectrum * (np.pi * 0.8 / 256)

    def direct_psf(source):
        # Independent receiver-by-receiver sum: no scipy convolution/FFT path.
        full = np.zeros((10, 10, len(WAVE)))
        for y in range(8):
            for x in range(8):
                for ky in range(3):
                    for kx in range(3):
                        full[y + ky, x + kx] += source[y, x] * kernel[:, ky, kx]
        return full

    reference = direct_psf(oracle_irradiance)
    results = {}
    for side, sampled_fraction in [
        (2, 0.5), (4, 0.25), (8, 0.25), (16, 0.3125),
    ]:
        request = half_plane_request(0.3)
        request["source_pixel_quadrature"] = {
            "model": "native_sensor_area_midpoint_grid_v1", "grid_side": side,
        }
        directory = tmp_path / f"grid-{side}"
        radiance = render(directory, request)["spectral-radiance"]
        summary = json.loads((directory / "renderer-summary.json").read_text())
        assert summary["source_pixel_quadrature"]["integrator_path_samples"] == (
            8 * 8 * len(WAVE) * request["samples_per_pixel"] * side**2
        )
        np.testing.assert_allclose(radiance[4, 4], spectrum * sampled_fraction)
        projected = radiance * (np.pi * 0.8 / 256)
        actual, diagnostic = spatial._convolve(projected, kernel, pitch_um)
        discrete_full = direct_psf(projected)
        np.testing.assert_allclose(actual, discrete_full[1:9, 1:9], rtol=0, atol=1e-15)
        np.testing.assert_allclose(
            diagnostic["input_spectral_flux"],
            projected.sum(axis=(0, 1)) * pixel_area_m2, rtol=1e-12,
        )
        np.testing.assert_allclose(
            np.asarray(diagnostic["input_spectral_flux"])
            - oracle_irradiance.sum(axis=(0, 1)) * pixel_area_m2,
            8 * (sampled_fraction - 0.3) * spectrum
            * (np.pi * 0.8 / 256) * pixel_area_m2,
            rtol=1e-12, atol=1e-24,
        )
        np.testing.assert_allclose(
            diagnostic["full_convolution_spectral_flux"],
            discrete_full.sum(axis=(0, 1)) * pixel_area_m2, rtol=1e-12,
        )
        np.testing.assert_allclose(
            diagnostic["retained_crop_spectral_flux"],
            discrete_full[1:9, 1:9].sum(axis=(0, 1)) * pixel_area_m2,
            rtol=1e-12,
        )
        assert np.all(np.asarray(diagnostic["outside_crop_spectral_flux"]) > 0)
        results[side] = np.abs(actual - reference[1:9, 1:9]).sum()
    np.testing.assert_allclose(results[4], results[2] / 4, rtol=1e-12)
    np.testing.assert_allclose(results[8], results[4], rtol=1e-12)
    np.testing.assert_allclose(results[16], results[2] / 16, rtol=1e-12)
    # Conservation is relative to the sampled discrete input. The 16x16 path
    # still has a known +0.0125-cell physical area bias at this edge.
    assert results[16] > 0


def test_absent_source_quadrature_keeps_legacy_renderer_bytes(tmp_path):
    request = basic_request()
    without = render(tmp_path / "without", request)
    with_none = render(tmp_path / "with-none", {**request, "source_pixel_quadrature": None})
    for name in without:
        np.testing.assert_array_equal(without[name], with_none[name])
        suffix = name + ".npy"
        assert (tmp_path / "without" / suffix).read_bytes() == (
            tmp_path / "with-none" / suffix
        ).read_bytes()
    assert (tmp_path / "without" / "renderer-summary.json").read_bytes() == (
        tmp_path / "with-none" / "renderer-summary.json"
    ).read_bytes()


def test_rough_conductor_changes_spectral_response_on_same_geometry(tmp_path):
    req = basic_request()
    req["scene"]["payload"]["instances"][0]["area_light_id"] = None
    req["scene"]["payload"]["environment_light_id"] = "light"
    req["samples_per_pixel"] = 64
    diffuse = render(tmp_path / "diffuse", req)["spectral-radiance"]
    req["assets"][1]["payload"] = {
        "bsdf": "roughconductor",
        "spectrum": spec([0.2, 0.5, 2.0]),
        "extinction_k": spec([4.0, 3.0, 2.0]),
        "roughness": 0.1,
    }
    conductor = render(tmp_path / "conductor", req)["spectral-radiance"]
    assert np.isfinite(conductor).all() and np.all(conductor >= 0)
    # Constant incident radiance on a Lambertian plane has L_out = rho L_in.
    np.testing.assert_allclose(diffuse.mean(axis=(0, 1)), [0.5, 1.0, 1.5], rtol=0.04)
    assert conductor[..., 0].mean() > diffuse[..., 0].mean() * 1.5
    assert not np.allclose(conductor / [1.0, 2.0, 3.0], 0.5)


@pytest.mark.parametrize(
    "payload",
    [
        {"wavelength_nm": [550.0, 450.0], "values": [1.0, 1.0]},
        {"wavelength_nm": [350.0, 550.0], "values": [1.0, 1.0]},
        {"wavelength_nm": [450.0, 550.0], "values": [-1.0, 1.0]},
    ],
)
def test_spectral_contract_rejects_undefined_samples(payload):
    with pytest.raises(ValidationError):
        Spectrum.model_validate(payload)


def test_asset_units_mesh_and_roles_are_not_interchangeable():
    with pytest.raises(ValidationError, match="different units"):
        Light.model_validate(
            {
                "kind": "light",
                "distribution": "point",
                "spectrum": spec([1.0, 1.0, 1.0]),
                "unit": "W/m^2/sr/nm",
                "time_model": "static",
            }
        )
    with pytest.raises(ValidationError, match="Degenerate"):
        Geometry.model_validate(
            {
                "kind": "geometry",
                "unit": "m",
                "primitive": "triangle_mesh",
                "vertices": [[0.0, 0.0, 0.0]] * 3,
                "faces": [[0, 1, 2]],
            }
        )
    with pytest.raises(ValidationError):
        SceneAssetCreate.model_validate(
            {
                **PROVENANCE,
                "name": "file bypass",
                "payload": {"kind": "geometry", "filename": "/tmp/arbitrary.xml"},
            }
        )


def test_psf_halo_source_crop_clips_to_sensor_and_keeps_even_bayer_sides():
    origin, shape = scene_render._halo_source_crop(
        [20, 20], [0, 1], [8, 8], [3, 5]
    )
    assert origin == [0, 0]
    assert shape == [10, 12]
    with pytest.raises(ValueError, match="256-pixel"):
        scene_render._halo_source_crop([300, 300], [20, 20], [256, 256], [3, 3])


def test_carrier_and_psf_source_crop_uses_asymmetric_native_offsets():
    full, capture_origin, capture_shape = [30, 36], [5, 8], [8, 10]
    offsets = [[-2, 3], [1, -4]]
    generation = scene_render._carrier_generation_crop(
        full, capture_origin, capture_shape, offsets
    )
    assert generation == ([4, 5], [11, 17])
    origin, shape = scene_render._halo_source_crop(
        full, capture_origin, capture_shape, [3, 5], generation
    )
    assert origin == [3, 3]
    assert shape == [14, 22]
    assert all(side % 2 == 0 for side in shape)
    # The receiver itself must remain in the published source if every carrier
    # offset is nonzero; otherwise capture_crop cannot retain the intended ROI.
    displaced = scene_render._carrier_generation_crop(
        [40, 40], [16, 16], [8, 8], [[4, 4]]
    )
    assert displaced == ([12, 12], [8, 8])
    assert scene_render._halo_source_crop(
        [40, 40], [16, 16], [8, 8], [1, 1], displaced
    ) == ([12, 12], [12, 12])
    # A receiver crop may satisfy the render limit before the carrier halo,
    # yet exceed it after support expansion. Reject instead of truncating rays.
    oversized_generation = scene_render._carrier_generation_crop(
        [300, 300], [20, 20], [248, 248], [[-4, -4], [4, 4]]
    )
    with pytest.raises(ValueError, match="256-pixel"):
        scene_render._halo_source_crop(
            [300, 300], [20, 20], [248, 248], [3, 3], oversized_generation
        )


def finish(service, queued):
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    return service.registry.finish(job["id"], result=service.execute_run(job))


@pytest.fixture
def scene_workflow(tmp_path, monkeypatch):
    if not PYTHON.exists():
        pytest.skip("Isolated Mitsuba environment is not installed")
    monkeypatch.setenv("CAMERA_PLATFORM_MITSUBA_PYTHON", str(PYTHON))
    service = PlatformService(tmp_path / "state")
    seed(service)
    original = service.registry.list("assemblies")[0]
    base = next(
        service.registry.get("models", i)
        for i in original["model_ids"]
        if service.registry.get("models", i)["kind"] == "lens"
    )
    data = {k: copy.deepcopy(v) for k, v in base.items() if k in ModelCreate.model_fields}
    data["properties"].update(
        focal_length={"value": 4.0, "unit": "mm"}, f_number={"value": 8.0, "unit": "1"}
    )
    base = service.create_model(data)
    job = finish(service, factory.submit(service, factory.template(service, "prysm_psf_v1")))
    conditions = {**original["conditions"], "f_number": {"value": 8.0, "unit": "1"}}
    lens = factory.publish(
        service,
        {
            "job_id": job["id"],
            "base_lens_model_id": base["id"],
            "name": "Declared 4 mm projection and pupil",
            "revision": "1",
            "conditions": conditions,
        },
    )
    fields = {k: copy.deepcopy(v) for k, v in original.items() if k in AssemblyCreate.model_fields}
    fields.update(
        conditions=conditions,
        model_ids=[
            lens["id"] if service.registry.get("models", i)["kind"] == "lens" else i
            for i in fields["model_ids"]
        ],
    )
    assembly = service.create_assembly(fields)

    def asset(name, payload):
        return scene_render.create_asset(service, {**PROVENANCE, "name": name, "payload": payload})

    geometry = asset("Reusable plane", {"kind": "geometry", "unit": "m", "primitive": "rectangle"})
    material = asset(
        "Declared diffuse",
        {
            "kind": "material",
            "bsdf": "diffuse",
            "spectrum": spec([0.2, 0.5, 0.8]),
            "surface_state": "synthetic dry diffuse",
        },
    )
    light = asset(
        "Uniform spectral environment",
        {
            "kind": "light",
            "distribution": "constant_environment",
            "spectrum": spec([0.002, 0.002, 0.002]),
            "unit": "W/m^2/sr/nm",
            "time_model": "static",
        },
    )
    scene = asset(
        "Plane lit in 3D",
        {
            "kind": "scene",
            "coordinate_system": "right_handed_y_up_metres",
            "instances": [
                {
                    "geometry_id": geometry["id"],
                    "material_id": material["id"],
                    "to_world": plane(3.0),
                }
            ],
            "environment_light_id": light["id"],
        },
    )
    conditions["light_id"] = {"value": scene["illumination_id"], "unit": "1"}
    lens = factory.publish(
        service,
        {
            "job_id": job["id"],
            "base_lens_model_id": base["id"],
            "name": "Scene-bound declared pupil",
            "revision": "2",
            "conditions": conditions,
        },
    )
    fields.update(
        conditions=conditions,
        model_ids=[
            lens["id"] if service.registry.get("models", i)["kind"] == "lens" else i
            for i in fields["model_ids"]
        ],
    )
    assembly = service.create_assembly(fields)
    req = {
        "name": "3D spectral integration",
        "scene_asset_id": scene["id"],
        "assembly_id": assembly["id"],
        "shape": [8, 8],
        "samples_per_pixel": 8,
        "pose": basic_request()["pose"],
        "projection": {
            **PROVENANCE,
            "projection_model": "paraxial_on_axis_projection_v1",
            "focal_length_mm": 4.0,
            "f_number": 8.0,
            "on_axis_transmittance": spec([0.8, 0.8, 0.8]),
            "geometry_model": "pinhole_pixel_centre",
            "irradiance_model": "pi_L_T_over_4N2_before_relative_illumination",
            "approximation_uncertainty": "unknown",
        },
    }
    sensor = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    wavelengths = sensor["payload"]["wavelength_nm"]
    req["projection"]["on_axis_transmittance"] = {
        "wavelength_nm": wavelengths,
        "values": [0.8] * len(wavelengths),
    }
    return service, assembly, req


def test_durable_scene_to_prysm_raw_rgb_and_portable_original_evidence(scene_workflow):
    service, assembly, req = scene_workflow
    job = finish(service, scene_render.submit(service, req))
    assert "source_pixel_quadrature" not in job["request"]
    assert service.list_simulation_runs() == []
    image = scene_render.publish(service, {"job_id": job["id"], "name": "Rendered input"})
    with pytest.raises(ValueError, match="pinned projection lens"):
        imaging._scene_input(service, image, lens_model_id="different-lens")
    with pytest.raises(ValueError, match="pinned projection lens"):
        imaging._scene_input(service, image)
    test = service.create_test(imaging.spatial_test_template(service, assembly["id"], image["id"]))
    run = finish(
        service,
        service.submit_run(
            {
                "assembly_id": assembly["id"],
                "test_id": test["id"],
                "plugin_id": "spatial_scene_v1",
                "scene_id": image["id"],
                "parameters": {"frames": 2, "seed": 5},
            }
        ),
    )

    def array(name):
        a = next(a for a in run["artifacts"] if a["name"] == name)
        return np.load(
            io.BytesIO(service.registry.blob_path(a["sha256"]).read_bytes()), allow_pickle=False
        )

    assert array("raw_frames.npy").shape == (2, 8, 8)
    assert array("rgb_frames.npy").shape == (2, 8, 8, 3)
    assert array("raw_frames.npy").std() > 1
    assert image["render_job_snapshot"]["id"] == job["id"]
    assert run["result"]["prediction"]["uncertainty"] == {"kind": "unknown"}
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as z:
        names = z.namelist()
        for suffix in [
            "scene-render-input.json",
            "spectral-radiance.npy",
            "renderer-summary.json",
            "raw_frames.npy",
        ]:
            assert any(n.endswith("/" + suffix) for n in names)
    # The physical projection is explicit and testable, not a hidden brightness knob.
    a = np.ones((8, 8, len(req["projection"]["on_axis_transmittance"]["values"])))
    p = req["projection"]
    np.testing.assert_allclose(scene_render.project_radiance(a, p), np.pi * 0.8 / 256)
    np.testing.assert_allclose(
        scene_render.project_radiance(a, {**p, "f_number": 16.0}), np.pi * 0.8 / 1024
    )
    invalid = copy.deepcopy(req)
    invalid["projection"]["focal_length_mm"] = 5.0
    before = len(service.registry.list_runs())
    with pytest.raises(ValueError, match="focal_length"):
        scene_render.submit(service, invalid)
    assert len(service.registry.list_runs()) == before


def test_source_pixel_quadrature_freezes_halo_budget_and_publication(scene_workflow):
    service, _, req = scene_workflow
    request = copy.deepcopy(req)
    request.update(
        shape=[8, 8], crop_origin=[0, 1], render_psf_halo=True,
        samples_per_pixel=2,
        source_pixel_quadrature={
            "model": "native_sensor_area_midpoint_grid_v1", "grid_side": 2,
        },
    )
    checked = SceneRenderCreate.model_validate(request)
    assert checked.source_pixel_quadrature.grid_side == 2
    for side in (8, 16):
        supported = copy.deepcopy(request)
        supported["source_pixel_quadrature"]["grid_side"] = side
        assert SceneRenderCreate.model_validate(supported).source_pixel_quadrature.grid_side == side
    invalid = copy.deepcopy(request)
    invalid["source_pixel_quadrature"]["grid_side"] = 3
    with pytest.raises(ValidationError):
        SceneRenderCreate.model_validate(invalid)
    invalid = copy.deepcopy(request)
    invalid["source_pixel_quadrature"]["model"] = "unverified_fractional_psf"
    with pytest.raises(ValidationError):
        SceneRenderCreate.model_validate(invalid)
    over = copy.deepcopy(request)
    over["samples_per_pixel"] = 256
    over["source_pixel_quadrature"]["grid_side"] = 4
    # Capture dimensions alone are allowed; the actual expanded source and
    # wavelength count push this over the bounded integrator-path budget.
    SceneRenderCreate.model_validate(over)
    with pytest.raises(ValueError, match="262144 fixed-wavelength primary paths"):
        scene_render.submit(service, over)
    over16 = copy.deepcopy(request)
    over16["source_pixel_quadrature"]["grid_side"] = 16
    with pytest.raises(ValueError, match="262144 fixed-wavelength primary paths"):
        scene_render.submit(service, over16)
    queued = scene_render.submit(service, request)
    frozen = queued["request"]
    assert frozen["source_pixel_quadrature"] == request["source_pixel_quadrature"]
    assert frozen["capture_crop"] == {"crop_origin": [0, 1], "shape": [8, 8]}
    assert frozen["shape"] != request["shape"]
    paths = int(np.prod(frozen["shape"])) * len(frozen["wavelength_nm"]) * 2 * 4
    assert paths <= 262144
    job = finish(service, queued)
    assert job["result"]["summary"]["source_pixel_quadrature"]["integrator_path_samples"] == paths
    assert (
        job["result"]["summary"]["source_pixel_quadrature"]["physical_validation"]
        == "not_established"
    )
    image = scene_render.publish(service, {"job_id": job["id"], "name": "Opt-in area source"})
    assert image["render_job_snapshot"]["request"]["source_pixel_quadrature"] == (
        request["source_pixel_quadrature"]
    )
    artifact = {a["name"]: a for a in job["artifacts"]}
    radiance = np.load(io.BytesIO(service.registry.blob_path(
        artifact["spectral-radiance.npy"]["sha256"]).read_bytes()), allow_pickle=False)
    irradiance = np.load(io.BytesIO(service.registry.blob_path(
        artifact["spectral-image.npy"]["sha256"]).read_bytes()), allow_pickle=False)
    np.testing.assert_allclose(irradiance, scene_render.project_radiance(
        radiance, frozen["projection"]
    ))
    high_resolution = copy.deepcopy(req)
    high_resolution.update(
        samples_per_pixel=2,
        source_pixel_quadrature={
            "model": "native_sensor_area_midpoint_grid_v1", "grid_side": 16,
        },
    )
    high_job = finish(service, scene_render.submit(service, high_resolution))
    high_request = high_job["request"]
    assert high_job["result"]["summary"]["source_pixel_quadrature"][
        "integrator_path_samples"
    ] == int(np.prod(high_request["shape"])) * len(high_request["wavelength_nm"]) * 2 * 16**2


def test_scene_render_supplies_pinned_psf_halo_for_native_capture_roi(scene_workflow):
    service, assembly, req = scene_workflow
    req.update(shape=[8, 8], crop_origin=[0, 1], render_psf_halo=True,
               samples_per_pixel=2)
    assert SceneRenderCreate.model_validate(req).render_psf_halo is True
    queued = scene_render.submit(service, req)
    frozen = queued["request"]
    assert frozen["capture_crop"] == {"crop_origin": [0, 1], "shape": [8, 8]}
    assert frozen["sampling"]["crop_origin"] == frozen["crop_origin"]
    assert frozen["shape"] != req["shape"]
    assert all(side % 2 == 0 for side in frozen["shape"])
    radius = [side // 2 for side in frozen["psf_halo"]["kernel_shape"]]
    for axis in range(2):
        native = frozen["sampling"]["full_sensor_shape"][axis]
        assert frozen["crop_origin"][axis] <= max(0, req["crop_origin"][axis] - radius[axis])
        assert frozen["crop_origin"][axis] + frozen["shape"][axis] >= min(
            native, req["crop_origin"][axis] + req["shape"][axis] + radius[axis]
        )
    job = finish(service, queued)
    assert job["result"]["capture_crop"] == frozen["capture_crop"]
    assert job["result"]["psf_source_support"]["shape"] == frozen["shape"]
    image = scene_render.publish(service, {"job_id": job["id"], "name": "Halo source"})
    assert image["sampling"]["crop_origin"] == frozen["crop_origin"]
    assert "capture_crop" not in image["sampling"]
    test = service.create_test(imaging.spatial_test_template(service, assembly["id"], image["id"]))
    run = finish(service, service.submit_run({
        "assembly_id": assembly["id"], "test_id": test["id"],
        "plugin_id": "spatial_scene_v1", "scene_id": image["id"],
        "parameters": {"frames": 1, "seed": 5, "capture_crop": frozen["capture_crop"]},
    }))
    assert run["result"]["summary"]["output_size"] == [8, 8]


def test_carrier_halo_render_requires_pinned_generation_collection_pair(scene_workflow):
    service, _, req = scene_workflow
    req.update(shape=[8, 8], crop_origin=[0, 1],
               render_charge_collection_halo=True, samples_per_pixel=2)
    assert SceneRenderCreate.model_validate(req).render_charge_collection_halo is True
    before = len(service.registry.list_runs())
    with pytest.raises(ValueError, match="pinned Sensor charge_collection"):
        scene_render.submit(service, req)
    assert len(service.registry.list_runs()) == before


def test_pinned_carrier_halo_scene_to_native_raw_and_isp(tmp_path, monkeypatch):
    if not PYTHON.exists():
        pytest.skip("Isolated Mitsuba environment is not installed")
    monkeypatch.setenv("CAMERA_PLATFORM_MITSUBA_PYTHON", str(PYTHON))
    from test_charge_collection_models import authored_generation_model

    service = PlatformService(tmp_path / "state")
    seed(service)
    original = service.registry.list("assemblies")[0]
    source_sensor = next(
        service.registry.get("models", identity)
        for identity in original["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    light = scene_render.create_asset(service, {
        **PROVENANCE, "name": "Constant two-band scene light",
        "payload": {
            "kind": "light", "distribution": "constant_environment",
            "spectrum": {"wavelength_nm": [500, 600], "values": [0.002, 0.003]},
            "unit": "W/m^2/sr/nm", "time_model": "static",
        },
    })
    scene = scene_render.create_asset(service, {
        **PROVENANCE, "name": "Uniform scene for carrier support",
        "payload": {
            "kind": "scene", "coordinate_system": "right_handed_y_up_metres",
            "instances": [], "environment_light_id": light["id"], "point_lights": [],
        },
    })
    prescription = prescription_psf.request_template(version=3)
    prescription["prescription"]["sampling"].update(
        polar_rings=20, azimuth_samples=20
    )
    ray_input = factory.template(service, "rayoptics_lens_v1", request_version=3)
    ray_input["request"] = prescription["prescription"]
    ray_job = finish(service, factory.submit(service, ray_input))
    psf_input = factory.template(service, "prescription_psf_v1", request_version=3)
    psf_input["request"] = prescription
    psf_job = finish(service, factory.submit(service, psf_input))
    conditions = copy.deepcopy(original["conditions"])
    conditions.update(
        light_id={"value": scene["illumination_id"], "unit": "1"},
        f_number={
            "value": psf_job["result"]["summary"]["geometric_reference_f_number"],
            "unit": "1",
        },
    )
    parent = factory.publish(service, {
        "job_id": ray_job["id"], "name": "Scene-matched joint directions", "revision": "1",
        "conditions": {k: v for k, v in conditions.items()
                       if k not in {"exposure_s", "gain"}},
    })
    lens = factory.publish(service, {
        "job_id": psf_job["id"], "base_lens_model_id": parent["id"],
        "name": "Scene-matched PSF", "revision": "1", "conditions": conditions,
        "condition_scope": "optical_response",
    })
    sensor_data = authored_generation_model(source_sensor)
    for key in ("active_height", "active_width"):
        sensor_data["properties"][key] = {"value": 0.192, "unit": "mm"}
    sensor_data["payload"]["pixel_pitch_um"] = 3
    sensor_data["payload"]["native_pixel_response"]["pixel_pitch_um"] = 3
    collection = sensor_data["payload"]["charge_collection"]
    collection["pixel_pitch_um"] = 3
    collection["destination_offsets_rc"] = [[0, 0], [2, -1]]
    collection["probabilities"] = [
        [[0.7, 0.2] for _ in range(4)] for _ in range(2)
    ]
    sensor = service.create_model(sensor_data)
    assembly_data = {
        k: copy.deepcopy(v) for k, v in original.items()
        if k in AssemblyCreate.model_fields
    }
    assembly_data.update(
        name="Scene-to-carrier support camera", conditions=conditions,
        spectral_range={"min": 500, "max": 600, "unit": "nm"},
        model_ids=[lens["id"], sensor["id"], original["model_ids"][2]],
    )
    assembly = service.create_assembly(assembly_data)
    assert assembly["compatibility"]["status"] == "PASS"
    preview = scene_camera.preview(
        service, scene_camera.template(service, scene["id"], assembly["id"])
    )
    assert preview["status"] == "ready", preview["issues"]
    template = scene_templates.render_template(
        service, scene["id"], assembly["id"]
    )
    assert template["issues"] == []
    request = template["request"]
    request.update(shape=[8, 8], crop_origin=[28, 28], samples_per_pixel=2,
                   render_charge_collection_halo=True)
    queued = scene_render.submit(service, request)
    frozen = queued["request"]
    assert frozen["capture_crop"] == {"crop_origin": [28, 28], "shape": [8, 8]}
    assert frozen["carrier_halo"]["sensor_model_id"] == sensor["id"]
    assert frozen["carrier_halo"]["sensor_model_sha256"] == sensor["content_hash"]
    assert frozen["carrier_halo"]["charge_collection_sha256"] == (
        sensor["response_artifacts"]["charge_collection"]["sha256"]
    )
    assert frozen["carrier_halo"]["generation_crop"] == {
        "crop_origin": [26, 28], "shape": [10, 9],
    }
    assert frozen["crop_origin"] == [6, 8]
    assert frozen["shape"] == [50, 50]
    altered = copy.deepcopy(queued)
    altered["request"]["carrier_halo"]["sensor_model_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source support differs"):
        scene_render._verify_carrier_halo(service, altered)
    job = finish(service, queued)
    assert job["result"]["psf_source_support"]["carrier_halo"] == frozen["carrier_halo"]
    image = scene_render.publish(service, {"job_id": job["id"], "name": "Carrier halo source"})
    assert image["sampling"]["crop_origin"] == [6, 8]
    test = service.create_test(
        imaging.spatial_test_template(service, assembly["id"], image["id"])
    )
    run = finish(service, service.submit_run({
        "assembly_id": assembly["id"], "test_id": test["id"],
        "plugin_id": "spatial_scene_v1", "scene_id": image["id"],
        "parameters": {"frames": 1, "seed": 7},
    }))
    assert run["parameters"]["capture_crop"] == frozen["capture_crop"]
    assert run["result"]["summary"]["output_size"] == [8, 8]
    assert run["result"]["summary"]["charge_collection"]["capture_collected"] > 0
    artifacts = {a["name"]: a for a in run["artifacts"]}
    raw = np.load(io.BytesIO(service.registry.blob_path(
        artifacts["raw_frames.npy"]["sha256"]).read_bytes()), allow_pickle=False)
    rgb = np.load(io.BytesIO(service.registry.blob_path(
        artifacts["rgb_frames.npy"]["sha256"]).read_bytes()), allow_pickle=False)
    assert raw.shape == (1, 8, 8) and rgb.shape == (1, 8, 8, 3)
    assert raw.mean() > sensor["payload"]["readout"]["black_level_dn"]
    changed_sensor_data = {
        k: copy.deepcopy(v) for k, v in sensor.items()
        if k in ModelCreate.model_fields
    }
    changed_sensor_data.update(name="Different carrier routing", revision="generation-2",
                               parents=[sensor["id"]])
    changed_sensor_data["payload"]["charge_collection"]["probabilities"][0][0] = [0.6, 0.2]
    changed_sensor = service.create_model(changed_sensor_data)
    changed_assembly_data = copy.deepcopy(assembly_data)
    changed_assembly_data.update(name="Different pinned Sensor",
                                 model_ids=[lens["id"], changed_sensor["id"],
                                            original["model_ids"][2]])
    changed_assembly = service.create_assembly(changed_assembly_data)
    changed_test = service.create_test(imaging.spatial_test_template(
        service, changed_assembly["id"], image["id"]
    ))
    with pytest.raises(ValueError, match="different pinned Sensor revision"):
        service.submit_run({
            "assembly_id": changed_assembly["id"], "test_id": changed_test["id"],
            "plugin_id": "spatial_scene_v1", "scene_id": image["id"],
            "parameters": {"frames": 1, "seed": 7},
        })


def test_cancelled_render_keeps_originals_and_cannot_publish(scene_workflow):
    service, _, req = scene_workflow
    job = scene_render.submit(service, req)
    service.registry.cancel(job["id"])
    with pytest.raises(ValueError, match="completed"):
        scene_render.publish(service, {"job_id": job["id"], "name": "not completed"})
    with zipfile.ZipFile(build_bundle(service, "scene_renders", job["id"])) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["run"]["status"] == "cancelled"
        for a in job["input_artifacts"]:
            assert (
                z.read("artifacts/" + a["sha256"] + "/" + a["name"])
                == service.registry.blob_path(a["sha256"]).read_bytes()
            )
