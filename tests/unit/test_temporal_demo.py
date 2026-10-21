"""The public temporal demo must export real captures with measurable motion."""

import json

import numpy as np

from tools.render_temporal_capture_demo import render


def test_demo_exports_physical_skew_blur_and_common_dn_scale(tmp_path):
    result = render(tmp_path)
    saved = json.loads((tmp_path / "temporal_capture_metrics.json").read_text())
    assert saved == result
    assert saved["display_scale_dn"] == [0.0, 1000.0]
    assert (tmp_path / "temporal_capture_montage.png").stat().st_size > 1000
    captures = saved["captures"]
    with np.load(tmp_path / "temporal_capture_raw.npz") as archive:
        for name, metrics in captures.items():
            raw = archive[f"{name}_raw_dn"]
            assert np.issubdtype(raw.dtype, np.integer)
            assert raw.shape == (32, 64)
            assert raw.max() < 4095
            np.testing.assert_array_equal(raw, np.round(archive[f"{name}_reference_dn"]))
            # Known motion: 500 px/s * 0.001 s/row * 31 rows = 15.5 px.
            expected_skew = 15.5 if metrics["rolling_shutter"] else 0.0
            assert abs(metrics["row_centroid_skew_px"] - expected_skew) < 0.05
        for mode in ("global", "rolling"):
            short_width = captures[f"{mode}_short"]["mean_row_sigma_px"]
            long_width = captures[f"{mode}_long"]["mean_row_sigma_px"]
            assert long_width > short_width + 1.0
