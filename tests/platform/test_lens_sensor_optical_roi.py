"""Pinned-lens proof for a globally located optical ROI assembled from source tiles."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from camera_platform.lens_catalog_bridge import _asset, _revision_source
from pyisetcam.lens_patents import lens_patent_raytrace_optics
from pyisetcam.optics import (
    _raytrace_expected_kernel_shape,
    _strict_raytrace_apply_psf_bounded,
    _strict_raytrace_apply_psf_bounded_roi,
)

WORKSPACE = Path(__file__).resolve().parents[2]
LENS_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"


def test_pinned_p0014_geometric_psf_roi_matches_full_convolution() -> None:
    root = WORKSPACE / "camerae2e_db"
    if not (root / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB is unavailable")
    revision, snapshot, lens_db, _ = _revision_source(LENS_REVISION, root)
    simulation_id, psf_path, check, _ = _asset(
        revision, snapshot, lens_db, root
    )
    optics = lens_patent_raytrace_optics(
        simulation_id, db_path=lens_db, psf_dir=psf_path.parent,
        allow_approximate_radiometry=True,
    )
    assert optics["raytrace"]["asset_sha256"] == check["asset_sha256"]

    sample_spacing_m = 1.151213027e-6
    kernel_shape = _raytrace_expected_kernel_shape(optics, sample_spacing_m)
    assert kernel_shape[0] > 8 and kernel_shape[1] > 8
    padding = tuple(side // 2 + 2 for side in kernel_shape)
    cube = (np.arange(16 * 16, dtype=float).reshape(16, 16, 1) / 255) + 0.1
    wave = np.array([450.0])
    angles = np.arange(0.0, 361.0, 90.0)
    full, _ = _strict_raytrace_apply_psf_bounded(
        cube, optics, wave, sample_spacing_m, padding, angles
    )
    roi, evidence = _strict_raytrace_apply_psf_bounded_roi(
        cube, optics, wave, sample_spacing_m, padding, angles,
        output_roi_row_col=(4, 12, 4, 12),
        source_tile_shape_row_col=(8, 8),
    )
    np.testing.assert_allclose(
        roi, full[padding[0] + 4:padding[0] + 12,
                  padding[1] + 4:padding[1] + 12],
        rtol=1e-12, atol=1e-12,
    )
    assert evidence["source_tiles_processed"] == 4
    assert evidence["full_source_cube_allocated_by_operator"] is False
    assert evidence["caller_source_cube_residency"] == "present"
    assert evidence["sensor_or_isp_executed"] is False
