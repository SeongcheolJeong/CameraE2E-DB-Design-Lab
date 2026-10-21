"""Portable report completeness using explicit artificial view fixtures, not optical evidence."""

import csv

import numpy as np
import pytest

from camera_platform import calibration_anchor_density as density
from camera_platform import calibration_anchor_report as report


@pytest.mark.parametrize("passed", [True, False])
def test_report_has_all_mtf_csv_rows_and_blocked_or_selected_state(tmp_path, passed):
    g = {"full_sensor_shape": [1097, 1937], "fields": []}
    for r in range(5):
        for c in range(5):
            g["fields"].append(
                {
                    "id": f"a-{r}-{c}",
                    "role": "anchor",
                    "optical_anchor_active_row_col": [274 * r, 484 * c],
                }
            )
    for r in range(4):
        for c in range(4):
            g["fields"].append(
                {
                    "id": f"v-{r}-{c}",
                    "role": "refinement_anchor",
                    "optical_anchor_active_row_col": [274 * r + 137, 484 * c + 242],
                }
            )
    for r in range(4):
        for c in range(4):
            g["fields"].append(
                {
                    "id": f"check-{r}-{c}",
                    "role": "validation",
                    "optical_anchor_active_row_col": [274 * r + 68.5, 484 * c + 121],
                }
            )
    folder = tmp_path / "anchor-density"
    folder.mkdir()
    k = np.zeros((1025, 1025, 3), np.float32)
    k[512, 512] = 1.0
    np.savez_compressed(tmp_path / "kernel.npz", mass_rgb=k)
    kernels = [
        {
            "field": c["id"],
            "file": "kernel.npz",
            "sha256": density.optics.sha(tmp_path / "kernel.npz"),
        }
        for c in g["fields"]
    ]
    curve = {"x": [0, 0.25, 0.5], "y": [1, 0.8, 0.6]}
    metric = {"max_abs": 0.0, "mtf50_relative": None}
    checks = [
        {
            "psf_l1_rgb": [0, 0, 0],
            "comparison": {
                "checks": [{"stage": s, "max_abs": 0} for s in ("optical", "raw", "isp")]
            },
        }
        for _ in range(16)
    ]
    comparisons = []
    for angle in (0, 90):
        for phase in (0, 25):
            for r in range(5):
                for c in range(5):
                    comparisons.append(
                        {
                            "field": f"roi-{r}-{c}",
                            "chart": f"edge-{angle}-phase-{phase:02d}",
                            "candidate_grid_size": 5,
                            "reference_grid_size": "refined",
                            "origin_row_col": [r * 242, c * 452],
                            "raw_max_abs_dn": 0,
                            "isp_max_abs": 0,
                            "curves": {"raw": curve, "isp": curve},
                            "reference_curves": {"raw": curve, "isp": curve},
                            "mtf": {"raw": metric, "isp": metric},
                        }
                    )
    np.save(tmp_path / "raw.npy", np.zeros((65, 97), np.uint16))
    np.save(tmp_path / "linear.npy", np.zeros((65, 97, 3)))
    capture = {
        "id": "fixture",
        "raw": "raw.npy",
        "linear": "linear.npy",
        "kernel_cache_peak_bytes": k.nbytes,
        "maximum_array_working_bytes_estimate": k.nbytes,
        "elapsed_s": 1.0,
    }
    s = {
        "status": "passed" if passed else "not_converged",
        "geometry": g,
        "selected_anchor_count": 41 if passed else None,
        "selected_grid_size": "refined" if passed else None,
        "kernels": kernels,
        "candidates": [
            {"grid_size": n, "anchor_count": v, "checks": checks, "passed": passed}
            for n, v in [(3, 9), (5, 25), ("refined", 41)]
        ],
        "captures": [capture],
        "full_sensor_density_comparisons": comparisons,
        "source": {"fields": [{"elapsed_s": 1}]},
        "partition_check": {
            "active_pixels_compared": 2124889,
            "raw_max_abs_dn": 0,
            "isp_max_abs": 0,
        }
        if passed
        else None,
        "independent_window_checks": [{"passed": True}] * 100 if passed else [],
    }
    text = report.render(tmp_path, s)
    assert 'id="anchor-density"' in text and ("미수렴 · 자동 적용 차단" in text) == (not passed)
    assert "새 16개 직접 PSF" in text and "실물 카메라 검증은 미확립" in text
    rows = list(csv.DictReader((folder / "full-sensor-mtf.csv").open()))
    assert len(rows) == 100 and {v["candidate_grid_size"] for v in rows} == {"5"}
    assert len(list(folder.glob("*-all-rois.png"))) == 4
    assert (folder / "direct-psfs.png").stat().st_size > 1000
