"""Chart ingestion invariants: native Bayer coordinates and safe registered ROIs."""

import numpy as np
import pytest
from camera_platform.esfr_chart_registration import register_chart
from camera_platform.esfr_demo import chart_capture


def package():
    return {
        "registration_template": {
            "fiducials_xy": [[100, 100], [300, 100], [100, 300], [300, 300]],
            "rois": [
                {"id": "edge", "field_id": "star-1", "kind": "edge", "box": [160, 180, 64, 80]},
                {"id": "gray", "field_id": "gray-1", "kind": "gray", "box": [50, 50, 20, 20]},
            ],
        }
    }


def test_identity_registration_preserves_pixels_and_native_safe_boxes():
    p = package()
    result = register_chart(p, p["registration_template"]["fiducials_xy"], (400, 400))
    assert [r["box"] for r in result["rois"]] == [
        r["box"] for r in p["registration_template"]["rois"]
    ]
    assert result["registration"]["transform_applied_to_measurements"] is False
    assert result["requires_roi_review"]


def test_perspective_proposals_are_inscribed_not_warped():
    import cv2

    result = register_chart(package(), [[90, 95], [310, 110], [80, 320], [295, 290]], (400, 400))
    assert result["rois"]
    for roi in result["rois"]:
        y, x, h, w = roi["box"]
        poly = np.asarray(roi["projected_safe_polygon_xy"], np.float32)
        assert all(
            cv2.pointPolygonTest(poly, p, False) >= -1e-5
            for p in [(x, y), (x + w - 1, y), (x + w - 1, y + h - 1), (x, y + h - 1)]
        )


@pytest.mark.parametrize(
    "points",
    [
        [[100, 100], [100, 300], [300, 100], [300, 300]],
        [[100, 100], [100, 100], [100, 300], [300, 300]],
        [[100, 100], [300, 100], [100, 300], [np.nan, 300]],
        [[100, 100], [300, 100], [100, 300], [401, 300]],
    ],
)
def test_registration_rejects_bad_fiducial_correspondence(points):
    with pytest.raises(ValueError):
        register_chart(package(), points, (400, 400))


def test_full_chart_capture_global_cfa_and_independent_repeat_noise():
    rgb = np.broadcast_to(np.array([0.1, 0.3, 0.7], np.float32), (80, 82, 3)).copy()
    raw, isp = chart_capture(rgb, 123)
    other, _ = chart_capture(rgb, 124)
    for r, c, v in [(0, 0, 0.1), (0, 1, 0.3), (1, 0, 0.3), (1, 1, 0.7)]:
        assert abs((float(raw[r::2, c::2].mean()) - 64) / 4031 - v) < 0.0001
    assert raw.shape == (80, 82) and isp.shape == (80, 82, 3)
    assert not np.array_equal(raw, other)
    assert np.allclose(isp[4:-4, 4:-4].mean(axis=(0, 1)) / 65535, [0.1, 0.3, 0.7], atol=0.0002)


def test_full_scene_convolution_does_not_replace_roi_contents():
    rgb = np.zeros((80, 80, 3), np.float32) + 0.05
    rgb[:, 40:] = 0.2
    raw, _ = chart_capture(rgb, 4, sigma=1.4)
    assert raw[:, 38].mean() > 64 + 0.05 * 4031 + 10
    assert raw[:, 41].mean() < 64 + 0.2 * 4031 - 10
    assert abs(raw[:, 20].mean() - (64 + 0.05 * 4031)) < 1


def test_extreme_skew_never_returns_box_outside_safe_polygon():
    p = {
        "registration_template": {
            "fiducials_xy": [[0, 0], [100, 0], [0, 100], [100, 100]],
            "rois": [{"id": "edge", "field_id": "star", "kind": "edge", "box": [0, 0, 101, 101]}],
        }
    }
    with pytest.raises(ValueError, match="No chart ROI"):
        register_chart(p, [[50, 50], [350, 350], [60, 80], [360, 380]], (500, 500))
