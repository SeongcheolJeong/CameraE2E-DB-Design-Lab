from pathlib import Path

import numpy as np

from camera_platform.virtual_lab_metrics import optical_psf_metrics, slanted_edge_metrics
from camera_platform.virtual_lab_report import render


def test_offline_report_renders_engine_shapes_and_escapes_content(tmp_path):
    y, x = np.indices((64, 64))
    edge = slanted_edge_metrics(0.1 + 0.7 * (x > 32 + (y - 32) / 12), degrees_per_pixel=0.05)
    optical = optical_psf_metrics(
        np.ones((5, 5)), sample_pitch_um=0.5, sensor_pitch_um=1, focal_length_mm=5
    )
    point = {
        "mean_signal": 100,
        "temporal_variance": 20,
        "temporal_std": 20**0.5,
        "snr_db": 27,
        "fixed_pattern_and_shading_std_estimate": 2,
        "repeat_count": 8,
    }
    result = {
        "configuration": {"name": '<script>alert("bad")</script>', "frames": 8},
        "images": {
            "RAW": "images/raw.png",
            "bad": "https://invalid.example/private.png",
            "escape": "../private.png",
        },
        "provenance": {"geometry": {"native_shape": [360, 640], "pixel_pitch_um": 0.61}},
        "tests": {
            "resolution": {
                "sites": {"center": {"linear_system": edge, "processed_system": edge}},
                "field_windows": [
                    {
                        "id": "center",
                        "metrics": {
                            "sites": {"center": {"linear_system": edge, "processed_system": edge}},
                            "optical": optical,
                        },
                    }
                ],
            },
            "noise": {
                **point,
                "unit": "DN",
                "photon_transfer_points": [
                    point,
                    {**point, "mean_signal": 200, "temporal_variance": 40},
                ],
            },
        },
        "limitations": ['<img src=x onerror="alert(1)">'],
    }
    render(result, tmp_path)
    report = (tmp_path / "report.html").read_text()
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert 'src="images/raw.png"' in report
    assert "https://invalid.example" not in report
    assert "../private.png" not in report
    assert "<svg" in report and "<polyline" in report
    assert "Mean–variance" in report and "cycles / degree" in report
    assert "native shape" in report
    assert "별도 계산한 작은 native 관측창" in report


def test_empty_partial_report_is_usable(tmp_path):
    render({"configuration": {"name": "Partial"}, "tests": {}, "images": {}}, Path(tmp_path))
    report = (tmp_path / "report.html").read_text()
    assert "색 시험이 실행되지 않았습니다" in report
    assert "NaN" not in report


def test_wide_report_preserves_native_directions_and_unqualified_frequency(tmp_path):
    native_sfr = {
        "frequency": {"cycles_per_pixel": [0, 0.25, 0.5]},
        "sfr": [1, 0.7, 0.3],
        "mtf50_cycles_per_pixel": 0.4,
    }
    qualified_sfr = {
        **native_sfr,
        "frequency": {**native_sfr["frequency"], "cycles_per_degree": [0, 2.5, 5]},
        "mtf50_cycles_per_degree": 4,
    }
    sites = {}
    for row in range(1, 4):
        for col in range(1, 4):
            sites[f"row{row}_col{col}"] = {
                "radial": {
                    "linear_system": native_sfr,
                    "processed_system": native_sfr,
                    "angular_geometry": {
                        "angular_frequency_qualified": False,
                        "roi_relative_scale_variation": 0.08,
                    },
                },
                "tangential": {
                    "linear_system": qualified_sfr,
                    "processed_system": qualified_sfr,
                    "angular_geometry": {
                        "angular_frequency_qualified": True,
                        "roi_relative_scale_variation": 0.02,
                    },
                },
                "linear_system": native_sfr,
            }
    render(
        {
            "configuration": {
                "name": "Wide",
                "lens_id": "wide_parametric",
                "field_model": {"mode": "full_field"},
                "angular_response": {"mode": "physical_pupil"},
            },
            "provenance": {
                "geometry": {
                    "projection": "equidistant",
                    "native_shape": [1080, 1920],
                    "horizontal_fov_deg": 120,
                    "vertical_fov_deg": 67.5,
                    "diagonal_fov_deg": 137.68,
                    "pixel_pitch_um": 0.61,
                    "focal_length_mm": 0.5592,
                }
            },
            "images": {"isp": "images/grid-isp.png", "native_isp": "images/grid-native-isp.png"},
            "image_sets": {
                "grid": {"native_isp": "images/grid-native-isp.png"},
                "unsafe": {"native_isp": "../secret.png"},
            },
            "tests": {
                "resolution": {"schema": "camera_virtual_lab_wide_resolution_v1", "sites": sites},
                "field_response": {
                    "mode": "full_field",
                    "angular_mode": "physical_pupil",
                    "pupil_sampling": {"source_pupil_sample_count": 9},
                },
            },
        },
        tmp_path,
    )
    report = (tmp_path / "report.html").read_text()
    assert "1920 × 1080" in report and "640 × 360 · preview only" in report
    assert 'href="images/grid-native-isp.png" download' in report
    assert 'src="images/grid-native-isp.png"' not in report
    assert "../secret.png" not in report
    assert "Processed radial SFR" in report and "Processed tangential SFR" in report
    assert "cycles / native pixel" in report and "각도 재표본화 필요" in report
    assert "FOV/640" not in report
    assert "Declared exit-pupil sampling" in report
    assert "Stored pupil sampling" not in report
    assert "선언한 합성 출사동공 원판" in report
