"""Four-point chart layout proposals in the original image coordinate system.

A projective map locates candidate patches; it never rectifies camera pixels.
Lens distortion, tilted surfaces and inaccurate fiducial picks require review.
"""

from __future__ import annotations

import numpy as np


def register_chart(package, fiducials_native_xy, image_shape):
    import cv2

    src = np.asarray(package["registration_template"]["fiducials_xy"], dtype=float)
    dst = np.asarray(fiducials_native_xy, dtype=float)
    height, width = map(int, image_shape)
    if src.shape != (4, 2) or dst.shape != (4, 2) or not np.isfinite(dst).all():
        raise ValueError("Four finite fiducial points [x,y] in TL,TR,BL,BR order are required")
    if np.any(dst < 0) or np.any(dst[:, 0] >= width) or np.any(dst[:, 1] >= height):
        raise ValueError("Fiducials must lie inside the original native image")
    # A simple, positively oriented quadrilateral rejects crossed and mirrored picks.
    poly = dst[[0, 1, 3, 2]]
    area = cv2.contourArea(poly.astype(np.float32), oriented=True)
    if area < 64 or not cv2.isContourConvex(poly.astype(np.float32)):
        raise ValueError("Fiducial order is crossed, reflected or degenerate")
    if min(np.linalg.norm(a - b) for i, a in enumerate(dst) for b in dst[i + 1 :]) < 8:
        raise ValueError("Fiducials are too close to define a stable chart map")
    transform = cv2.getPerspectiveTransform(src.astype(np.float32), dst.astype(np.float32))
    if not np.isfinite(transform).all() or abs(np.linalg.det(transform)) < 1e-10:
        raise ValueError("Degenerate chart homography")
    result, excluded = [], []
    for item in package["registration_template"]["rois"]:
        row, col, rh, rw = item["box"]
        corners = np.array(
            [[col, row], [col + rw - 1, row], [col + rw - 1, row + rh - 1], [col, row + rh - 1]],
            dtype=float,
        )
        homogeneous = np.c_[corners, np.ones(4)] @ transform.T
        divisor = homogeneous[:, 2]
        if np.min(divisor) * np.max(divisor) <= 0 or np.min(np.abs(divisor)) < 1e-8:
            excluded.append({"id": item["id"], "reason": "outside_projective_domain"})
            continue
        projected = homogeneous[:, :2] / divisor[:, None]
        if not np.isfinite(projected).all():
            excluded.append({"id": item["id"], "reason": "nonfinite_projection"})
            continue
        # An inscribed axis-aligned rectangle never includes pixels outside the
        # source safe-ROI quadrilateral. Uniformly shrink around the polygon centre.
        centre = projected.mean(axis=0)
        extent = (projected.max(axis=0) - projected.min(axis=0)) / 2
        factor = 1.0
        contour = projected.astype(np.float32)
        found = False
        while factor >= 0.2:
            candidates = centre + factor * extent * np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]])
            if all(
                cv2.pointPolygonTest(contour, tuple(map(float, p)), False) >= 0 for p in candidates
            ):
                found = True
                break
            factor *= 0.95
        if not found:
            excluded.append({"id": item["id"], "reason": "excessive_projective_skew"})
            continue
        lo = np.ceil(centre - factor * extent - 1e-8).astype(int)
        hi = np.floor(centre + factor * extent + 1e-8).astype(int)
        if (lo < 0).any() or hi[0] >= width or hi[1] >= height:
            excluded.append({"id": item["id"], "reason": "outside_native_image"})
            continue
        integer_corners = [
            tuple(map(float, p))
            for p in ((lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1]))
        ]
        if any(cv2.pointPolygonTest(contour, p, True) < -1e-5 for p in integer_corners):
            excluded.append({"id": item["id"], "reason": "integer_box_outside_safe_polygon"})
            continue
        size = hi - lo + 1
        if min(size) < (32 if item["kind"] == "edge" else 8):
            excluded.append({"id": item["id"], "reason": "insufficient_native_samples"})
            continue
        result.append(
            {
                **item,
                "box": [int(lo[1]), int(lo[0]), int(size[1]), int(size[0])],
                "projected_safe_polygon_xy": projected.tolist(),
                "registration_status": "proposed_requires_review",
            }
        )
    if not result:
        raise ValueError("No chart ROI remains within the native image at usable sampling")
    return {
        "rois": result,
        "requires_roi_review": True,
        "registration": {
            "method": "four_fiducial_coordinate_homography_v1",
            "fiducial_order": ["top_left", "top_right", "bottom_left", "bottom_right"],
            "fiducials_native_xy": dst.tolist(),
            "homography": transform.tolist(),
            "transform_applied_to_measurements": False,
            "chart_identity_verified": False,
            "excluded_rois": excluded,
            "image_shape": [height, width],
        },
        "warnings": [
            "ROI proposals require visual review; four picks do not verify chart identity.",
            "Lens distortion and non-planarity are not corrected; "
            "measurement pixels are unchanged.",
            "Excluded or insufficiently sampled edges are not counted as passing.",
        ],
    }
