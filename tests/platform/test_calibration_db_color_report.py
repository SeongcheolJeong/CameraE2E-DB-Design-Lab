"""Replay reports keep model-change evidence separate from physical validation."""

import base64
import hashlib
from copy import deepcopy

import pytest

from camera_platform.calibration_db_color_report import render_db_color_report


def test_automatic_holdout_summary_and_manifested_report(summary):
    metric = {
        "before": {"rmse": 30},
        "after": {"rmse": 0.2},
        "replicate": {"rmse": 0.19},
        "reduction_percent": 99.3,
    }
    summary["files"]["holdout/report.html"] = "a" * 64
    summary["holdout"] = {
        "status": "synthetic_holdout_completed",
        "source": {"model_id": summary["model_id"], "replay_id": summary["run_id"]},
        "report_path": "holdout/report.html",
        "aggregate": {"raw": metric, "isp": metric},
    }
    text = render_db_color_report(summary).decode()
    assert 'href="holdout/report.html"' in text
    assert "독립 반복 차이" in text
    summary["holdout"]["source"]["model_id"] = "other-model"
    with pytest.raises(ValueError, match="binding"):
        render_db_color_report(summary)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def summary():
    variants = []
    for key in ("db_baseline", "qe_only_db_isp", "qe_ccm_baseline", "qe_ccm_fitted"):
        variants.append(
            {
                "variant": key,
                "output_space": "linear_srgb"
                if key in {"db_baseline", "qe_only_db_isp"}
                else "camera_linear_rgb",
                "images": {"raw_preview": "fields/center/raw.png", "isp": "fields/center/isp.png"},
                "raw": {
                    "frames": [{"path": "raw.npy", "sha256": "a" * 64}],
                    "temporal_noise_dn": 0.8,
                    "clipped_fraction": 0,
                },
            }
        )
    return {
        "schema": "camera_db_color_replay_v1",
        "run_id": "run-replay",
        "model_id": "model-new",
        "color_calibration_id": "color-original",
        "applied_profile_sha256": "b" * 64,
        "physical_validation": "not_established",
        "independent_db_validation": "not_established",
        "source_context": {
            "run_id": "run-parent",
            "candidate_id": "candidate-03",
            "condition_id": "condition-006",
        },
        "binding": {"mode": "synthetic_model_transfer", "target_camera_id": "synthetic-db-camera"},
        "configuration": {"frames": 8, "shape": [256, 256]},
        "files": {
            "fields/center/raw.png": hashlib.sha256(PNG).hexdigest(),
            "fields/center/isp.png": hashlib.sha256(PNG).hexdigest(),
        },
        "fields": [
            {
                "field_id": "center",
                "variants": variants,
                "raw_identity": {"qe_variants_identical": True},
            }
        ],
        "comparisons": [
            {
                "field_id": "center",
                "kind": "ccm_output_change",
                "unit": "camera_linear_rgb",
                "comparison_type": "change_only_no_reference_observation",
                "rmse_change": 0.2,
                "max_abs_change": 0.3,
                "baseline": [[-0.1, 0.2, 0.3]],
                "candidate": [[0.2, 0.3, 0.4]],
                "difference": [[0.3, 0.1, 0.1]],
            }
        ],
    }


def test_standalone_manifest_verified_images_and_no_mutation(summary):
    original = deepcopy(summary)
    html = render_db_color_report(summary, image_resolver=lambda _: PNG).decode()
    assert summary == original
    assert html.count('src="data:image/png;base64,') == 8
    assert "<script" not in html and 'src="http' not in html
    assert "run-replay" in html and "model-new" in html and "run-parent" in html
    assert "candidate-03" in html and "condition-006" in html
    assert "b" * 64 in html


def test_distinguishes_qe_change_fair_ccm_change_and_unvalidated_db(summary):
    html = render_db_color_report(summary).decode()
    for text in (
        "같은 DB ISP",
        "같은 RAW와 출력 공간",
        "정답 대비 오차 아님",
        "DB 조건 독립 실측 정합: 미확립",
        "원래 보정 데이터",
        "camera_linear_rgb",
        "CCM은 RAW를 바꾸지 않습니다.",
        "저장 원본 동일 확인",
    ):
        assert text in html
    assert "-0.1" in html
    assert 'class="pass"' not in html


def test_report_does_not_upgrade_untrusted_success_labels(summary):
    summary["physical_validation"] = "pass"
    summary["independent_db_validation"] = "pass"
    summary["fields"][0]["raw_identity"] = {}
    html = render_db_color_report(summary).decode()
    assert "물리 검증 미확립" in html
    assert "DB 조건 독립 실측 정합: 미확립" in html
    assert "확인 근거 없음" in html
    assert "저장 원본 동일 확인" not in html


def test_rejects_missing_run_model_or_nonfinite_evidence(summary):
    for key in ("run_id", "model_id"):
        broken = deepcopy(summary)
        del broken[key]
        with pytest.raises(ValueError):
            render_db_color_report(broken)
    summary["comparisons"][0]["rmse_change"] = float("nan")
    with pytest.raises(ValueError):
        render_db_color_report(summary)


def test_image_hash_mismatch_or_non_png_fails_export(summary):
    with pytest.raises(ValueError, match="preserved manifest"):
        render_db_color_report(summary, image_resolver=lambda _: b"wrong bytes")
    content = b"not a png"
    summary["files"] = {name: hashlib.sha256(content).hexdigest() for name in summary["files"]}
    with pytest.raises(ValueError, match="not PNG"):
        render_db_color_report(summary, image_resolver=lambda _: content)


@pytest.mark.parametrize(
    "path",
    [
        "../private.png",
        "/private.png",
        "https://host/file.png",
        "a/%2e%2e/private.png",
        "a\\private.png",
    ],
)
def test_unsafe_image_path_never_resolved(summary, path):
    for item in summary["fields"][0]["variants"]:
        item["images"] = {"raw_preview": path, "isp": path}
    summary["files"][path] = hashlib.sha256(PNG).hexdigest()
    called = []
    html = render_db_color_report(
        summary, image_resolver=lambda name: called.append(name) or PNG
    ).decode()
    assert not called
    assert "<img" not in html


def test_all_untrusted_text_escaped_and_missing_images_not_invented(summary):
    summary["model_id"] = '<img src=x onerror="alert(1)">'
    summary["fields"][0]["field_id"] = "<script>alert(1)</script>"
    summary["limitations"] = ['<svg onload="alert(1)">']
    summary["files"] = {}
    html = render_db_color_report(summary).decode()
    assert "<script>" not in html and "<svg" not in html and "<img" not in html
    assert "&lt;img" in html and "&lt;script&gt;" in html and "&lt;svg" in html
    assert "검증된 이미지 파일이 제공되지 않았습니다." in html


def test_empty_results_do_not_look_completed(summary):
    summary["fields"] = []
    html = render_db_color_report(summary).decode()
    assert "제공된 계산 결과가 없습니다." in html
    assert "<img" not in html


def test_large_source_keeps_exact_parent_identity_and_report_creation_time(summary):
    summary["source_context"]["candidate"] = {"large": "x" * 8000}
    summary["source_context"]["manifest_sha256"] = "c" * 64
    summary["report_generated_at"] = "2026-10-17T12:34:56Z"
    summary["application"] = {"qe_profile_sha256": "d" * 64, "ccm_profile_sha256": "e" * 64}
    html = render_db_color_report(summary).decode()
    header = html.split("</header>")[0]
    for value in ("run-parent", "candidate-03", "condition-006", "c" * 64, "2026-10-17T12:34:56Z"):
        assert value in header
    assert "x" * 8000 not in html
    assert "d" * 64 in html and "e" * 64 in html
    assert "적용 모델 스냅샷" not in html
