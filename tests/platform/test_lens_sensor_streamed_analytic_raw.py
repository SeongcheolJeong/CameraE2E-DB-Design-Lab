"""One pinned Scene -> Lens -> Sensor diagnostic without a full source callback."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from camera_platform.lens_catalog_bridge import _asset, _revision_source
from camera_platform.lens_sensor_roi_capture import capture_fixed_sensor_from_optical_roi
from camera_platform.lens_sensor_scene_geometry_tile import (
    d65_analytic_bright_photons,
    make_analytic_strict_geometry_provider,
)
from camera_platform.lens_sensor_streamed_analytic_capture import (
    AnalyticCaptureResourcePolicy,
    preflight_streamed_analytic_capture,
)
from camera_platform.offline_assets import offline_asset_store
from pyisetcam.camera import camera_create, camera_get
from pyisetcam.image_sensor_db import image_sensor_db_reconcile_scenario
from pyisetcam.lens_patents import lens_patent_raytrace_optics
from pyisetcam.optics import _strict_raytrace_apply_psf_bounded_roi_provider, oi_compute
from pyisetcam.sensor import sensor_compute
from pyisetcam.sensor_simulation_db import sensor_simulation_db_config
from pyisetcam.system_faca import (
    _apply_physics_and_sensor_overrides,
    _configure_scenario_readout,
)
from pyisetcam.types import OpticalImage, Scene

WORKSPACE = Path(__file__).resolve().parents[2]
LENS_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
SENSOR_RUN = "360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601"


def test_pinned_analytic_scene_strict_lens_and_sensor_raw_match_full_path() -> None:
    root = WORKSPACE / "camerae2e_db"
    if not (root / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens and Sensor DB unavailable")
    revision, snapshot, lens_db, _ = _revision_source(LENS_REVISION, root)
    simulation_id, psf_path, check, _ = _asset(revision, snapshot, lens_db, root)
    optics = lens_patent_raytrace_optics(
        simulation_id,
        db_path=lens_db,
        psf_dir=psf_path.parent,
        allow_approximate_radiometry=True,
    )
    assert optics["raytrace"]["asset_sha256"] == check["asset_sha256"]
    assert optics["offaxis_method"] == "skip"

    scenario = sensor_simulation_db_config(
        "DEF-2512-801", root=root, run_id=SENSOR_RUN
    )["scenario"]
    scenario = {
        **scenario,
        "sensor": {
            **scenario.get("sensor", {}),
            "size": [16, 16],
            "integration_time": 0.004,
            "noise_flag": 2,
        },
    }
    scenario = image_sensor_db_reconcile_scenario(scenario)
    scenario["sensor_output_mode"] = "digital_raw"
    assets = offline_asset_store(workspace=WORKSPACE)
    camera, _ = _apply_physics_and_sensor_overrides(
        camera_create(asset_store=assets), scenario, assets
    )
    sensor = _configure_scenario_readout(
        camera_get(camera, "sensor"), scenario, "digital_raw"
    )
    assert sensor.metadata["sensor_simulation_db"]["run_id"] == SENSOR_RUN

    shape = (32, 32)
    fov_deg = 0.05
    wave = np.array([450.0, 550.0, 650.0])
    bright = d65_analytic_bright_photons(
        workspace=WORKSPACE,
        horizontal_fov_deg=fov_deg,
        luminance_cd_m2=1000.0,
        wavelengths_nm=wave,
    )
    provider = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=shape,
        scene_horizontal_fov_deg=fov_deg,
        wavelengths_nm=wave,
        bright_side_photons=bright,
        analytic_slanted_edge_offset_deg=0.0,
    )
    angles = np.arange(0.0, 361.0, 90.0)
    full_scene = Scene(
        fields={
            "wave": wave,
            "fov_deg": fov_deg,
            "vfov_deg": provider.vertical_fov_deg,
            "distance_m": float(optics["raytrace"]["object_distance_m"]),
            "rows": shape[0],
            "cols": shape[1],
        },
        data={"photons": provider.read_scene_tile(0, shape[0], 0, shape[1])},
    )
    full_oi = oi_compute(
        OpticalImage(
            fields={
                "optics": optics,
                "psf_compute_mode": "bounded_streaming",
                "psf_sample_angles_deg": angles,
            }
        ),
        full_scene,
        crop=False,
    )
    reference = sensor_compute(sensor, full_oi, seed=71)
    assert np.ptp(reference.data["mean_electrons"]) > 0

    requested: list[tuple[int, int, int, int]] = []

    def read_distorted_tile(r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        requested.append((r0, r1, c0, c1))
        return provider.read_tile(r0, r1, c0, c1)

    bounds = (2, 30, 2, 30)
    spacing = float(full_oi.fields["sample_spacing_m"])
    assert math.isclose(spacing * 1e6, provider.oi_pitch_um, rel_tol=1e-12)
    pad = tuple(full_oi.fields["padding_pixels"])
    roi, optical_evidence = _strict_raytrace_apply_psf_bounded_roi_provider(
        provider.global_shape,
        read_distorted_tile,
        optics,
        wave,
        spacing,
        pad,
        angles,
        output_roi_row_col=bounds,
        source_identity=provider.source_identity,
        source_tile_shape_row_col=(16, 16),
    )
    expected_roi = full_oi.data["photons"][
        pad[0] + bounds[0]:pad[0] + bounds[1],
        pad[1] + bounds[2]:pad[1] + bounds[3],
    ]
    assert np.max(np.abs(roi - expected_roi)) / np.max(np.abs(expected_roi)) < 2e-12
    streamed, sensor_evidence = capture_fixed_sensor_from_optical_roi(
        roi,
        shape,
        bounds,
        optics,
        wave,
        spacing,
        float(full_oi.fields["image_distance_m"]),
        sensor,
        optical_evidence,
        seed=71,
    )
    np.testing.assert_allclose(
        streamed.data["mean_electrons"],
        reference.data["mean_electrons"],
        rtol=5e-12,
        atol=1e-18,
    )
    np.testing.assert_array_equal(streamed.data["raw_dn"], reference.data["raw_dn"])
    assert requested
    assert all((r1 - r0) <= 16 and (c1 - c0) <= 16 for r0, r1, c0, c1 in requested)
    assert optical_evidence["source_identity"] == provider.source_identity
    assert optical_evidence["full_source_cube_allocated_by_operator"] is False
    assert optical_evidence["caller_source_cube_residency"] == "unknown"
    assert provider.metadata["full_optical_state_sha256"]
    assert sensor_evidence["full_oi_photon_array_allocated_by_adapter"] is False
    assert sensor_evidence["full_frame_execution_qualified"] is False

    # A separate high-resolution *preflight* checks the full stored PSF
    # envelope plus wavelength-specific inverse geometry, without computing
    # or claiming a full 1024 Scene/OI reference image.
    high_pitch_um = 0.575606513
    distance_mm = float(optics["raytrace"]["effective_focal_length_m"]) * 1000
    for side in (1024, 2048):
        high_fov = math.degrees(
            2 * math.atan(side * high_pitch_um * 1e-3 / (2 * distance_mm))
        )
        large_provider = make_analytic_strict_geometry_provider(
            optics,
            scene_shape_row_col=(side, side),
            scene_horizontal_fov_deg=high_fov,
            wavelengths_nm=wave,
            bright_side_photons=bright,
            analytic_slanted_edge_offset_deg=0.0,
            declared_oi_pitch_um=high_pitch_um,
        )
        start, stop = side // 2 - 18, side // 2 + 18
        large_preflight = preflight_streamed_analytic_capture(
            large_provider,
            sensor,
            optical_roi_bounds=(start, stop, start, stop),
            source_tile_shape_row_col=(128, 128),
            sample_angles_deg=angles,
            resource_policy=AnalyticCaptureResourcePolicy(
                estimated_working_budget_bytes=3 * 1024**3,
                max_geometry_query_cells=4_000_000,
                max_source_tiles=64,
                max_output_roi_cells=36**2,
                max_source_tile_cells=65536,
            ),
        )
        assert large_preflight["status"] == "ready_under_declared_policy"
        halo = large_preflight["stored_psf_numeric_halo"]
        assert halo["stored_psf_numeric_halo_inside_declared_scene"] is True
        assert halo["physical_scene_halo_qualified"] is False
        assert large_preflight["full_frame_execution_qualified"] is False
