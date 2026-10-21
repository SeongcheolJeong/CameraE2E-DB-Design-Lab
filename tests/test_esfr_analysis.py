"""Independent analytic fixtures for the original-pixel eSFR estimator."""

import json

import cv2
import numpy as np
import pytest
from camera_platform.esfr_analysis import detect_rois, edge_metrics
from scipy.ndimage import gaussian_filter
from scipy.special import erf


def edge(angle=85, sigma=1.4, size=96):
    y, x = np.indices((size, size), dtype=float)
    rad = np.deg2rad(angle)
    distance = -(x - (size / 2 - 0.7)) * np.sin(rad) + (y - (size / 2 - 0.3)) * np.cos(rad)
    return 0.2 + 0.6 * (1 + erf(distance / (2**0.5 * sigma))) / 2


@pytest.mark.parametrize("angle", [5, 40, 50, 85, 95, 140, 175])
def test_original_pixel_gaussian_sfr_arbitrary_orientation(angle):
    sigma = 1.4
    result = edge_metrics(edge(angle, sigma))
    assert result["status"] == "ok", result
    frequency = np.asarray(result["frequency_cy_native_px"])
    expected = np.exp(-2 * np.pi**2 * sigma**2 * frequency**2)
    assert np.max(np.abs(np.asarray(result["sfr"]) - expected)) < 0.012
    assert abs(result["mtf50"] - np.sqrt(np.log(2)) / (np.sqrt(2) * np.pi * sigma)) < 0.002
    assert abs(result["edge_angle_deg"] - angle) < 0.05
    assert result["method_details"]["image_resampling"] is False
    assert result["method_details"]["iso_compliance"] == "not_established"
    json.dumps(result, allow_nan=False)


def test_cfa_pitch_has_native_units_and_lower_nyquist():
    fine = edge_metrics(edge())
    cfa = edge_metrics(edge(), pitch_native=2)
    assert np.allclose(
        np.asarray(cfa["frequency_cy_native_px"]), np.asarray(fine["frequency_cy_native_px"]) / 2
    )
    assert cfa["mtf50"] == fine["mtf50"] / 2
    assert cfa["frequency_cy_native_px"][-1] == 0.25


@pytest.mark.parametrize("angle", [0, 45, 90, 135])
def test_singular_sampling_is_not_invented_by_image_rotation(angle):
    result = edge_metrics(edge(angle))
    assert result["status"] == "unavailable"
    assert result["reason"] == "insufficient_subpixel_phase_coverage"


def test_known_blur_order_and_sharpening_are_measured_separately():
    image = edge(sigma=1)
    sharp = image + 1.5 * (image - gaussian_filter(image, 1.2))
    base, processed = edge_metrics(image), edge_metrics(sharp)
    assert processed["status"] == "ok"
    assert processed["mtf50"] > base["mtf50"]
    assert processed["overshoot"] > 0.02
    assert processed["undershoot"] > 0.02
    assert processed["mtf50p"] <= processed["mtf50"]
    assert edge_metrics(edge(sigma=2))["mtf50"] < base["mtf50"]


def test_noise_and_star_junction_do_not_produce_false_measurement():
    rng = np.random.default_rng(1903)
    assert edge_metrics(rng.normal(size=(96, 96)))["status"] == "unavailable"
    y, x = np.indices((96, 96))
    star = 0.5 + 0.3 * np.sign(np.cos(4 * (np.arctan2(y - 47.5, x - 47.5) + 0.08)))
    result = edge_metrics(gaussian_filter(star, 1))
    assert result["status"] == "unavailable"
    noisy = edge_metrics(edge() + rng.normal(0, 0.008, (96, 96)))
    assert noisy["status"] == "ok"
    assert abs(noisy["mtf50"] - edge_metrics(edge())["mtf50"]) < 0.01


def test_explicit_clipping_and_invalid_input():
    assert edge_metrics(np.ones((64, 64)))["reason"] == "no_edge_contrast"
    assert edge_metrics(np.ones((10, 10)))["status"] == "unavailable"
    assert edge_metrics(np.full((64, 64), np.nan))["reason"] == "nonfinite_input"
    result = edge_metrics(np.clip(edge(), 0.25, 0.75), black_level=0.25, white_level=0.75)
    assert result["reason"] == "clipped_edge"
    assert result["quality"]["clipped_fraction"] > 0.5
    with pytest.raises(ValueError):
        edge_metrics(edge(), pitch_native=0)


def fixture_chart():
    # Procedural non-certified fixture, not a copy of the proprietary chart.
    # Three separated slanted stars plus patch blocks exercise edge proposals.
    image = np.full((540, 960), 0.7)
    for cy, cx in ((120, 170), (270, 480), (410, 790)):
        yy, xx = np.indices((150, 150))
        angle = np.arctan2(yy - 74.5, xx - 74.5) + np.deg2rad(5)
        radius = np.hypot(yy - 74.5, xx - 74.5)
        tile = np.where(radius < 70, 0.5 + 0.3 * np.sign(np.cos(4 * angle)), 0.7)
        image[cy - 75 : cy + 75, cx - 75 : cx + 75] = gaussian_filter(tile, 0.7)
    for index in range(6):
        image[430:465, 100 + index * 38 : 130 + index * 38] = 0.2 + index * 0.1
    for x, y in ((55, 55), (900, 55), (55, 490), (900, 490)):
        cv2.circle(image, (x, y), 7, 0.1, -1)
    return image


def test_chart_detection_is_conservative_and_original_coordinate_based():
    image = fixture_chart()
    result = detect_rois(image, {"variant": "extended_esfr_iso2023_16_9"})
    assert len(result["rois"]) >= 6
    assert result["registration"]["requires_review"] is True
    assert result["registration"]["chart_identity_verified"] is False
    assert result["registration"]["transform_applied_to_measurements"] is False
    assert len(result["registration"]["fiducial_candidates"]) >= 4
    assert all(roi["kind"] == "edge" for roi in result["rois"])
    assert any(roi["orientation_label"] == "diagonal_edge" for roi in result["rois"])
    for roi in result["rois"]:
        row, col, height, width = roi["box"]
        assert edge_metrics(image[row : row + height, col : col + width])["status"] == "ok"
        # Junction coordinates must never lie inside an accepted star ROI.
        assert not any(
            row <= y < row + height and col <= x < col + width
            for y, x in ((120, 170), (270, 480), (410, 790))
        )
    json.dumps(result, allow_nan=False)


def test_manual_chart_roi_template_overrides_detection_and_retains_patch_identity():
    profile = {
        "rois_approved": True,
        "chart_identity_verified": True,
        "rois": [
            {
                "id": "gray-01",
                "kind": "gray",
                "box": [430, 100, 35, 30],
                "reference_reflectance": 0.18,
            },
            {"id": "color-01", "kind": "color", "box": [430, 138, 35, 30]},
        ],
    }
    result = detect_rois(fixture_chart(), profile)
    assert len(result["rois"]) == 2
    assert result["rois"][0]["reference_reflectance"] == 0.18
    assert result["registration"]["status"] == "explicit_rois"
    assert result["registration"]["requires_review"] is False
    with pytest.raises(ValueError, match="outside"):
        detect_rois(fixture_chart(), {"rois": [{"box": [520, 0, 30, 30]}]})
    with pytest.raises(ValueError, match="unique"):
        detect_rois(fixture_chart(), {"rois": [profile["rois"][0], profile["rois"][0]]})


def test_blank_chart_does_not_create_measurements():
    result = detect_rois(np.ones((100, 160)))
    assert result["rois"] == []
    assert result["registration"]["status"] == "unavailable"
