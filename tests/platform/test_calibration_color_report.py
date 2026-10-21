"""The exported report preserves stage boundaries and untrusted evidence safely."""

from copy import deepcopy

import pytest

from camera_platform.calibration_color_report import render_color_report


@pytest.fixture
def record():
    matrix = [[1.1, -0.1, 0], [0, 1, 0], [0, 0, 0.9]]
    metrics = {
        "baseline": {
            "rmse": 0.12,
            "delta_e00_mean": None,
            "delta_e00_status": "not_applicable_camera_output_reproduction",
        },
        "candidate": {
            "rmse": 0.02,
            "delta_e00_mean": None,
            "delta_e00_status": "not_applicable_camera_output_reproduction",
        },
    }
    return {
        "id": "calibration-color-test",
        "name": "분리 보정 예제",
        "source_kind": "synthetic",
        "physical_validation": "not_established",
        "input_sha256": "a" * 64,
        "qe": {
            "profile": {
                "response_kind": "absolute_qe",
                "wavelength_nm": [450, 550, 650],
                "channels": ["R", "G", "B"],
                "qe": [[0.1, 0.2, 0.6], [0.2, 0.7, 0.1], [0.6, 0.2, 0.05]],
                "normalization": "none",
                "reference_plane": "sensor_surface",
                "conditions": {"camera_id": "synthetic-camera", "gain": 1},
                "profile_sha256": "b" * 64,
            }
        },
        "raw_validation": {
            "baseline": {
                "metrics": {"overall_rmse_dn": 20, "channel_rmse_dn": [10, 20, 30]},
                "predicted_raw_rgb_dn": [[90, 180, 270]],
            },
            "candidate": {
                "status": "pass",
                "channels": ["R", "G", "B"],
                "patch_ids": ["patch-1"],
                "measured_raw_rgb_dn": [[100, 200, 300]],
                "predicted_raw_rgb_dn": [[101, 199, 302]],
                "residual_dn": [[1, -1, 2]],
                "normalization": "none",
                "qe_unchanged": True,
                "metrics": {
                    "overall_rmse_dn": 1.414,
                    "channel_rmse_dn": [1, 1, 2],
                    "channel_bias_dn": [1, -1, 2],
                },
                "checks": [{"id": "rmse_dn", "status": "pass", "value": 1.414, "limit": 3}],
            },
        },
        "ccm": {
            "profile": {
                "matrix": matrix,
                "baseline_matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                "target_space": "camera_linear_rgb",
                "matrix_convention": "row_vector",
                "application": "(camera_rgb * white_balance * exposure_scale) @ matrix",
                "white_balance": [1, 1, 1],
                "exposure_scale": 1,
                "profile_sha256": "c" * 64,
            },
            "training": {"metrics": metrics},
            "validation": {
                "status": "pass",
                "refit_performed": False,
                "comparison": {
                    "metrics": metrics,
                    "target_rgb": [[0.1, 0.2, 0.3]],
                    "baseline": [[0.12, 0.23, 0.3]],
                    "candidate": [[0.1, 0.2, 0.3]],
                },
                "checks": [{"metric": "rmse", "value": 0.02, "limit": 0.03, "status": "pass"}],
            },
        },
        "evidence": {
            "inputs": {
                "qe_scan": {"source_kind": "synthetic", "provenance": {"session_id": "scan"}}
            },
            "raw_originals": {"validation": {"sha256": "d" * 64, "frame_count": 8}},
        },
    }


def test_three_stages_keep_units_and_fixed_model_scope(record):
    content = render_color_report(record).decode()
    for text in (
        "QE · 파장별 센서 응답",
        "RAW · 센서 모델 검증",
        "CCM · 출력 색상 보정",
        "RAW는 CCM 이전",
        "CCM으로 RAW 오차가 사라지지",
        "파장별 교정광량",
        "DN",
        "QE (e⁻ / photon)",
        "후보 평균 편차",
        "학습 차트",
        "독립 검증 차트",
        "실제 카메라 실측 검증이 아닙니다",
        "물리 검증 미확립",
    ):
        assert text in content
    assert content.count("<polyline") == 3
    assert "[101, 199, 302]" in content
    assert "@ matrix" in content
    assert "-0.1" in content  # Negative matrix entries are not clipped for presentation.
    assert "ΔE를 계산하지 않습니다" in content


def test_report_is_standalone_and_does_not_mutate_preserved_record(record):
    before = deepcopy(record)
    content = render_color_report(record).decode()
    assert record == before
    assert '<html lang="ko">' in content and "<svg" in content
    assert "<script" not in content
    assert "http://" not in content and "https://" not in content
    assert "default-src 'none'" in content
    assert record["input_sha256"] in content
    assert record["qe"]["profile"]["profile_sha256"] in content


def test_all_untrusted_text_is_escaped(record):
    attack = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    record["name"] = attack
    record["qe"]["profile"]["channels"][0] = attack
    record["raw_validation"]["candidate"]["patch_ids"][0] = attack
    record["ccm"]["profile"]["application"] = attack
    record["limitations"] = [attack]
    content = render_color_report(record).decode()
    assert attack not in content
    assert "<img" not in content and "<script" not in content
    assert "&lt;img" in content and "&quot;" in content


def test_relative_response_never_shows_fabricated_raw_or_ccm_success(record):
    profile = record["qe"]["profile"]
    profile["response_kind"] = "relative_spectral_response"
    profile["relative_spectral_response"] = profile.pop("qe")
    profile["normalization"] = "per_channel_max"
    reason = "Absolute RAW prediction requires absolute QE"
    record["raw_validation"] = {"candidate": {"status": "not_evaluated", "reason": reason}}
    record["ccm"] = {"status": "not_evaluated", "reason": reason}
    content = render_color_report(record).decode()
    assert "상대 분광 응답" in content
    assert "절대 RAW 예측에 사용할 수 없습니다" in content
    assert content.count(reason) == 2
    assert "수치 기준 충족" not in content
    assert "[101, 199, 302]" not in content


def test_raw_failure_is_not_overwritten_by_ccm_success(record):
    record["raw_validation"]["candidate"]["status"] = "fail"
    content = render_color_report(record).decode()
    raw = content.split("02 / BEFORE ISP CCM", 1)[1].split("03 / ISP COLOR TRANSFORM", 1)[0]
    ccm = content.split("03 / ISP COLOR TRANSFORM", 1)[1]
    assert 'class="status fail"' in raw
    assert 'class="status pass"' in ccm


def test_xyz_outputs_have_xyz_matrix_labels(record):
    record["ccm"]["profile"]["target_space"] = "xyz"
    record["ccm"]["validation"]["comparison"]["metrics"]["candidate"].update(
        delta_e00_mean=0.8, delta_e00_max=1.2, delta_e00_status="computed_d65_2deg_white_y_1"
    )
    content = render_color_report(record).decode()
    assert "<th>X</th><th>Y</th><th>Z</th>" in content
    assert "평균 ΔE00" in content and "0.8" in content
    assert "camera_linear_rgb는 색도 기준 공간" not in content


def test_invalid_nonfinite_or_unpreserved_records_are_refused(record):
    with pytest.raises(ValueError, match="preserved"):
        render_color_report({})
    record["qe"]["profile"]["qe"][0][0] = float("nan")
    with pytest.raises(ValueError):
        render_color_report(record)


def test_large_provenance_is_bounded_but_referenced(record):
    record["evidence"]["source_environment"] = {
        "source_files": {f"file-{i}": "e" * 64 for i in range(400)}
    }
    content = render_color_report(record).decode()
    assert "상세 값은 결과 JSON에 보존" in content
    assert "표시값 SHA256" in content
    assert len(content) < 35000


def test_raw_original_counts_and_patch_residual_curves_are_explicit(record):
    record["evidence"]["raw_originals"]["validation"].update(
        patch_count=24, frame_count=8, cfa="RGGB", bit_depth=12
    )
    record["raw_validation"]["candidate"]["residual_dn"] = [[1, -1, 2], [-2, 0, 1]]
    content = render_color_report(record).decode()
    assert "원본 RAW 역할" in content and "패치 수" in content and "프레임 수" in content
    assert "<td>24</td><td>8</td>" in content
    assert "d" * 64 in content
    assert content.count("<polyline") == 6
    assert "패치별 후보 RAW 잔차" in content


def test_optional_missing_ccm_uses_nested_not_evaluated_status(record):
    record["ccm"] = {
        "profile": None,
        "validation": {"status": "not_evaluated", "reason": "No CCM data supplied"},
    }
    content = render_color_report(record).decode()
    ccm = content.split("03 / ISP COLOR TRANSFORM", 1)[1]
    assert "미평가" in ccm
    assert "No CCM data supplied" in ccm
