"""Explicit, immutable calibration completion evidence and standalone Korean HTML.

A saved profile, completed computation, numerical tolerance pass and physical
validation are separate states. This module reads exact IDs and never activates,
selects the latest profile, writes a registry record or mutates an experiment.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math
from collections.abc import Callable
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any

from . import calibration_lab as lab
from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_calibration_completion_report_v1"
CHARTS = ("dark", "ptc", "geometry", "shading", "mtf", "color", "tone", "texture")
CHART_NAMES = {
    "dark": "암전 차트 · 블랙 레벨 / 암전류",
    "ptc": "균일광 PTC · 변환 계수 / 읽기 노이즈",
    "geometry": "기하 차트 · 좌표 / 왜곡",
    "shading": "균일광 · 주변 밝기",
    "mtf": "Slanted edge · MTF / 유효 PSF",
    "color": "색상 차트 · 색 응답",
    "tone": "계조 차트 · 톤 응답",
    "texture": "텍스처 · 비교 검증 전용",
}
CHART_CONVERSION = {
    "dark": "노출시간별 평균·시간 분산 → 블랙 DN, 암전류 e/s, 조건부 읽기 노이즈",
    "ptc": "신호 평균과 시간 분산 → DN/e 기울기, 양자화 근사를 뺀 읽기 노이즈",
    "geometry": "선언한 기준점과 관측점 → 알려진 좌표 변형 또는 다중 자세 조건부 내부 파라미터",
    "shading": "중앙 대비 ROI 밝기 → 방사형 r²/r⁴ 계수; 현재 균일광 차트 재현에 적용",
    "mtf": "필드·방향별 MTF → Gaussian 유효 PSF 폭·방향; DB 수치 PSF 초기화와 구분",
    "color": "기준·관측 RGB → 선언한 RAW 또는 ISP 단계의 색 응답 행렬",
    "tone": "입력·출력 계조 → 선언한 RAW 또는 ISP 단계의 단조 톤 LUT",
    "texture": "기준 대비 주파수 응답 → 비교만 수행; 임의 노이즈 제거 파라미터를 피팅하지 않음",
}
PARAMETER_PREFIXES = {
    "dark": ("electronics.black_level_dn", "electronics.dark_current_e_per_s"),
    "ptc": ("electronics.dn_per_e", "electronics.read_noise_e"),
    "geometry": ("geometry.",),
    "shading": ("shading.",),
    "mtf": ("blur.",),
    "color": ("isp.color_matrix", "raw_response.color_matrix"),
    "tone": ("isp.tone_", "raw_response.tone_"),
    "texture": (),
}
LIMITATIONS = [
    "합성 입력 영상의 재현 성공은 실제 카메라 실측 검증이 아닙니다.",
    "실측 입력이어도 수치 허용오차 합격과 물리 검증 완료는 별개입니다.",
    "측정 조건·관측 연산·파라미터 모델 범위 안에서만 결과를 해석해야 합니다.",
    "Full-well, 실제 V/e, 분리된 spectral QE, PRNU/DSNU 지도는 이 피팅으로 식별하지 않습니다.",
    "실측 MTF 피팅은 제한된 Gaussian 계열이며 유일한 광학 PSF 복원이 아닙니다.",
    "RAW slanted edge의 CFA 부분격자 주파수 상한은 native pixel 기준 0.25 cycles/pixel입니다.",
    "Shading 계수는 현재 균일광 차트 재현용이며 모든 영상에 적용되는 ISP LSC 보정은 아닙니다.",
    "렌즈 기준 필드 좌표와 실제 센서 좌표는 다르며 미등록 필드 보간·외삽은 지원하지 않습니다.",
    "DB 재실행 연결은 명시적 전기 파라미터 범위입니다. 광학/ISP 역방향 갱신을 의미하지 않습니다.",
]
ImageResolver = Callable[[dict[str, Any]], tuple[bytes, str] | None]
LABELS = {
    "training": "학습",
    "development": "개발 확인",
    "final_validation": "최종 독립 검증",
    "study": "학습/개발 실행",
    "independent_validation": "독립 검증 실행",
    "baseline": "기준 모델",
    "candidate": "후보 모델",
    "original": "원본",
    "synthetic": "합성",
    "measured": "실측",
    "pass": "합격",
    "fail": "실패",
    "inconclusive": "판단 보류",
    "unknown": "미확인",
    "not_evaluated": "미평가",
    "validation_only": "비교 검증 전용",
    "fit_and_independent_comparison": "피팅 및 독립 비교 완료",
    "partial": "일부 비교만 완료",
    "fixed_not_identified_by_this_fit": "고정값 · 이번 피팅에서 미추정",
    "not_identified": "미추정",
    "validation_only_not_fitted": "검증 전용 · 피팅하지 않음",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IntegrityError(message)


def _native(value: Any) -> Any:
    """Produce a detached finite JSON value; do not silently stringify unknown types."""
    return json.loads(canonical(value))


def _compact(value: Any) -> Any:
    data = canonical(value).encode()
    if len(data) <= 8192:
        return deepcopy(value)
    shape, item = [], value
    while isinstance(item, list) and item:
        shape.append(len(item))
        item = item[0]
    return {
        "representation": "large_value_hash_reference",
        "shape_hint": shape,
        "json_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "note": "전체 값은 고정된 기준/최종 profile 원본에 보존됩니다.",
    }


def _record_identity(record: dict) -> dict:
    return {key: record.get(key) for key in ("id", "created_at", "content_hash")}


def _expected_measurement_lineage(
    service: Any, measurement: dict, capture_cache: dict[str, dict] | None = None
) -> set[str]:
    """Reconstruct repeat lineage; never trust an arbitrary stored token superset.

    E-SFR repeat means deliberately retain the union of every original capture's
    session/acquisition/curve tokens as well as the aggregate's own lineage.
    Those originals remain immutable capture records and owned measurement blobs.
    """
    expected = lab._lineage(measurement)
    if "constituent_lineage_tokens" not in measurement:
        return expected
    from . import esfr_lab

    provenance = measurement.get("provenance", {})
    method = provenance.get("aggregation_method")
    expected_method = {
        "arithmetic_mean_all_same_role_native_roi_repeats": "external_esfr_repeat_mean_gr_sfr",
        "individual_capture": "external_esfr_native_gr_sfr",
    }.get(method)
    _require(
        measurement.get("chart") == "mtf"
        and expected_method is not None
        and measurement.get("method") == {"id": expected_method, "version": "1"}
        and provenance.get("import_classification")
        == "external_camera_raw_curve_not_legacy_image_operator",
        "Constituent lineage requires the declared E-SFR aggregation contract",
    )
    constituents = provenance.get("constituents")
    _require(
        isinstance(constituents, list) and bool(constituents)
        and provenance.get("constituent_count") == len(constituents)
        and (method != "individual_capture" or len(constituents) == 1),
        "E-SFR constituent count differs from declared aggregation",
    )
    cache = capture_cache if capture_cache is not None else {}
    capture_ids: set[str] = set()
    original_tokens: set[str] = set()
    original_frames: set[str] = set()
    owned_hashes = {a["sha256"] for a in measurement.get("artifacts", [])}
    for constituent in constituents:
        identity = constituent.get("capture_id")
        _require(isinstance(identity, str) and identity not in capture_ids,
                 "E-SFR constituents contain an invalid or duplicate capture")
        capture_ids.add(identity)
        if identity not in cache:
            cache[identity] = esfr_lab.capture(service, identity)
        capture = cache[identity]
        _require(
            constituent.get("capture_sha256") == capture.get("content_hash")
            and constituent.get("metadata") == capture.get("metadata")
            and constituent.get("source_files") == capture.get("files")
            and constituent.get("native_pixel_sha256") == capture.get("native_pixel_sha256"),
            "E-SFR constituent differs from its immutable source capture",
        )
        metadata = capture["metadata"]
        _require(
            metadata.get("camera_id") == measurement.get("physical_instance")
            and metadata.get("source_kind") == measurement.get("source_kind")
            and {"development": "training", "validation": "final_validation"}.get(
                metadata.get("role")) == measurement.get("role")
            and constituent.get("operator")
            == {"id": "external_esfr_native_gr_sfr", "version": "1"}
            and isinstance(constituent.get("observations"), dict),
            "E-SFR constituent camera, role or observation method differs from aggregate",
        )
        raw, isp = capture["files"]["raw"], capture["files"]["isp"]
        _require({raw["sha256"], isp["sha256"]} <= owned_hashes,
                 "E-SFR constituent source bytes are not owned by the measurement")
        frames = [capture["native_pixel_sha256"], raw["sha256"]]
        original_frames.update(frames)
        parents = list(dict.fromkeys([
            *metadata.get("parent_acquisition_ids", []),
            "raw-sha256:" + raw["sha256"],
            "native-pixels-sha256:" + capture["native_pixel_sha256"],
        ]))
        original_tokens.update(lab._lineage({
            "physical_instance": metadata["camera_id"],
            "session_id": metadata["session_id"],
            "acquisition_id": metadata["acquisition_id"],
            "acquisition_group_id": metadata["acquisition_id"],
            "parent_acquisition_ids": parents,
            "chart": "mtf",
            "observations": constituent["observations"],
            "source_frame_hashes": frames,
            "provenance": {"source_frame_hashes": frames},
            "artifacts": [raw, isp],
        }))
    _require(
        set(measurement.get("source_frame_hashes", [])) == original_frames
        and set(provenance.get("source_frame_hashes", [])) == original_frames,
        "E-SFR aggregate frame lineage differs from its constituents",
    )
    _require(
        set(measurement["constituent_lineage_tokens"]) == original_tokens,
        "E-SFR constituent lineage differs from reconstructed original tokens",
    )
    return expected | original_tokens


def _check_measurements(service: Any, run: dict, roles: set[str], campaign: dict) -> list[dict]:
    snapshots = run.get("measurement_snapshots", [])
    _require(bool(snapshots), "Completion report requires preserved measurement snapshots")
    ids = [row.get("id") for row in snapshots]
    _require(len(set(ids)) == len(ids), "Measurement snapshots contain duplicate IDs")
    _require(
        ids == run["specification"]["measurement_ids"],
        "Measurement snapshots differ from explicit run measurement IDs",
    )
    capture_cache: dict[str, dict] = {}
    for frozen in snapshots:
        current = lab._measurement(service, frozen["id"])
        _require(current == frozen, "Measurement differs from its frozen run snapshot")
        _require(
            frozen["campaign_id"] == campaign["id"]
            and frozen.get("campaign_sha256") == campaign["content_hash"],
            "Measurement is not bound to this campaign revision",
        )
        _require(frozen["role"] in roles, "Measurement role is incompatible with this run")
        _require(
            set(frozen["lineage_tokens"])
            == _expected_measurement_lineage(service, frozen, capture_cache),
            "Measurement lineage differs from its preserved originals and observations",
        )
    return snapshots


def _input_evidence(record: dict) -> dict:
    provenance = record.get("provenance", {})
    checksum = provenance.get("source_sha256")
    originals = [a for a in record["artifacts"] if a["sha256"] == checksum]
    _require(bool(originals), "Measurement source bytes are absent from its owned artifacts")
    image = (
        PurePosixPath(provenance.get("filename", "")).suffix.lower() == ".npy"
        and bool(record.get("source_frame_hashes"))
        and record.get("quality", {}).get("method", "").startswith("native_bayer_")
    )
    return {
        "measurement_id": record["id"],
        "chart": record["chart"],
        "role": record["role"],
        "source_kind": record["source_kind"],
        "evidence_type": "original_raw_image_stack" if image else "measurement_table_or_metadata",
        "original_image_available": image,
        "physical_instance": record["physical_instance"],
        "session_id": record["session_id"],
        "acquisition_id": record["acquisition_id"],
        "conditions": record["conditions"],
        "measurement_domain": record.get("measurement_domain"),
        "field_coordinate_domain": record.get("observations", {}).get("field_coordinate_domain"),
        "field_xy": record.get("observations", {}).get("field_xy"),
        "orientation_deg": record.get("observations", {}).get("orientation_deg"),
        "provenance": provenance,
        "frame_hashes": record.get("source_frame_hashes", []),
        "artifacts": [
            {k: a.get(k) for k in ("name", "sha256", "bytes", "media_type")}
            for a in record["artifacts"]
        ],
        "method": record.get("method"),
        "instrument": record.get("instrument"),
        "reference": record.get("reference"),
        "uncertainty": record.get("uncertainty", {"kind": "unknown"}),
        "quality": record.get("quality", {}),
        "import_status": record.get("import_status"),
        "original_binding_verified": True,
    }


def _outputs(run: dict) -> dict:
    summary = run["result"]["summary"]
    owned = {
        item["sha256"] for item in [*run["result"]["artifacts"], *run.get("input_artifacts", [])]
    }
    files = summary.get("files", {})
    _require(bool(files), "Completion report requires a preserved output manifest")
    for name, checksum in files.items():
        relative = PurePosixPath(name)
        _require(
            not relative.is_absolute() and ".." not in relative.parts and str(relative) == name,
            "Output manifest contains an unsafe relative path",
        )
        _require(checksum in owned, "Output manifest hash lacks a run artifact binding")
    return {
        "study_id": run["id"],
        "result_sha256": digest(run["result"]),
        "summary_sha256": digest(summary),
        "summary_artifact_sha256": run["result"]["artifacts"][0]["sha256"],
        "files": deepcopy(files),
        "file_count": len(files),
        "verification": "saved_summary_and_recorded_artifact_manifest_bound",
        "every_output_byte_rehashed_by_report_builder": False,
    }


def build_completion_report(
    service: Any, study_id: str, validation_id: str, profile_id: str
) -> dict[str, Any]:
    """Validate exact completed study/holdout/saved-profile lineage and return evidence.

    Failed or inconclusive numerical validation still produces an honest process
    report. Unfinished jobs, refitted validation, inconsistent lineage or an
    unapplied/unbound profile do not qualify for this completion report.
    """
    if any(
        not isinstance(value, str) or not value for value in (study_id, validation_id, profile_id)
    ):
        raise ValueError("Explicit study_id, validation_id and profile_id are required")
    study, validation = lab.get(service, study_id), lab.get(service, validation_id)
    _require(
        study["status"] == "succeeded" and study["job_kind"] == lab.JOB_KIND,
        "Completion report requires a completed calibration fit study",
    )
    _require(
        validation["status"] == "succeeded" and validation["job_kind"] == lab.VALIDATION_KIND,
        "Completion report requires completed independent validation",
    )
    before, after = study["result"]["summary"], validation["result"]["summary"]
    campaign = service.registry.get(lab.CAMPAIGNS, study["campaign_snapshot"]["id"])
    _require(
        study["campaign_snapshot"] == campaign == validation["campaign_snapshot"],
        "Study and validation do not share the same immutable campaign",
    )
    for run, summary in ((study, before), (validation, after)):
        _require(
            run["specification"]["campaign_id"] == campaign["id"]
            and summary.get("campaign_id") == campaign["id"]
            and summary.get("run_id") == run["id"]
            and summary.get("job_kind") == run["job_kind"],
            "Result identity differs from its campaign or job",
        )
        _require(
            summary.get("fresh_simulation") is True, "Completion requires fresh chart captures"
        )
    candidate = before["candidate_profile"]
    candidate_hash = digest(candidate)
    _require(before["candidate_sha256"] == candidate_hash, "Candidate profile hash changed")
    fit = before["fit"]
    _require(fit.get("profile") == candidate, "Fit output differs from the saved candidate")
    _require(
        fit.get("status") not in {"failed", "refused", "REFUSED"}
        and not any(
            d.get("status") in {"failed", "refused", "not_converged"}
            for d in fit.get("diagnostics", [])
        ),
        "The candidate fit did not complete",
    )
    _require(
        validation.get("parent_study_id") == study_id
        and validation.get("parent_result_sha256") == digest(study["result"]),
        "Independent validation belongs to another parent study or result",
    )
    _require(
        validation.get("candidate_sha256") == candidate_hash
        and validation.get("candidate_profile") == candidate
        and after.get("candidate_sha256") == candidate_hash
        and after.get("candidate_profile") == candidate,
        "Independent validation does not use the exact frozen candidate",
    )
    _require(
        after.get("fit", {}).get("status") == "frozen_candidate_no_refit"
        and after["fit"].get("profile") == candidate
        and not after["fit"].get("changed_parameters")
        and not after["fit"].get("diagnostics"),
        "Independent validation must not refit the candidate",
    )
    _require(
        validation.get("base_profile") == study["base_profile"] == before["baseline_profile"]
        and after.get("baseline_profile") == study["base_profile"],
        "Baseline changed between study and independent validation",
    )
    _require(
        candidate["conditions"] == study["base_profile"]["conditions"],
        "Fixed capture conditions changed during calibration",
    )
    training = _check_measurements(service, study, {"training", "development"}, campaign)
    holdout = _check_measurements(service, validation, {"final_validation"}, campaign)
    _require(any(m["role"] == "training" for m in training), "Fit has no training measurements")
    used_training = set(fit.get("training_measurement_ids", []))
    _require(
        bool(used_training)
        and used_training <= {m["id"] for m in training if m["role"] == "training"},
        "Fit training IDs are missing or include evaluation measurements",
    )
    development = set(study["development_lineage"])
    final_lineage = set().union(*(set(m["lineage_tokens"]) for m in holdout))
    _require(
        not final_lineage & development
        and not ({m["id"] for m in training} & {m["id"] for m in holdout}),
        "Final validation overlaps candidate development evidence",
    )
    _require(
        set(validation["validation_lineage"]) == final_lineage
        and set(validation["development_lineage"]) == development,
        "Independent validation lineage differs from the frozen parent",
    )
    verdict = after.get("validation", {})
    _require(
        verdict.get("independent") is True
        and verdict.get("status") in {"pass", "fail", "inconclusive"},
        "Independent validation verdict is unavailable",
    )
    _require(
        verdict.get("status") != "pass" or verdict.get("numerical_tolerance_pass") is True,
        "Validation pass contradicts its numerical tolerance status",
    )
    profile = service.registry.get(lab.PROFILES, profile_id)
    expected_profile_id = (
        "calibration-profile-" + digest({"study_id": study_id, "candidate": candidate_hash})[:32]
    )
    _require(
        profile_id == expected_profile_id
        and profile.get("source_study_id") == study_id
        and profile.get("source_result_sha256") == digest(study["result"])
        and profile.get("campaign_id") == campaign["id"]
        and profile.get("profile_sha256") == candidate_hash
        and profile.get("profile") == candidate,
        "Saved profile is not bound to this study, campaign and candidate",
    )
    activations = [
        r
        for r in service.registry.list(lab.ACTIVATIONS)
        if r.get("campaign_id") == campaign["id"] and r.get("profile_id") == profile_id
    ]
    _require(bool(activations) or profile.get("selection_status") == "saved_without_activation",
             "Saved profile has no explicit application or inactive-save record")
    inputs = [_input_evidence(m) for m in training + holdout]
    changes = [
        {
            **{k: v for k, v in row.items() if k not in {"before", "after"}},
            "before": _compact(row.get("before")),
            "after": _compact(row.get("after")),
            "status": "updated_by_fit",
        }
        for row in fit.get("changed_parameters", [])
    ]
    charts = []
    for chart in CHARTS:
        study_rows = [r for r in before.get("comparisons", []) if r.get("chart") == chart]
        validation_rows = [r for r in after.get("comparisons", []) if r.get("chart") == chart]
        diagnostics = [r for r in fit.get("diagnostics", []) if r.get("chart") == chart]
        charts.append(
            {
                "chart": chart,
                "label": CHART_NAMES[chart],
                "conversion": CHART_CONVERSION[chart],
                "mode": "validation_only" if chart == "texture" else "fit_and_compare",
                "status": "not_evaluated"
                if not study_rows and not validation_rows
                else "validation_only"
                if chart == "texture"
                else "fit_and_independent_comparison"
                if study_rows and validation_rows
                else "partial",
                "measurement_ids": [m["measurement_id"] for m in inputs if m["chart"] == chart],
                "fit_diagnostics": diagnostics,
                "parameters": [
                    r
                    for r in changes
                    if r.get("parameter", "").startswith(PARAMETER_PREFIXES[chart])
                ],
                "study_comparisons": study_rows,
                "validation_comparisons": validation_rows,
                "validation_checks": [
                    c
                    for c in verdict.get("checks", [])
                    if c.get("measurement_id") in {r["measurement_id"] for r in validation_rows}
                ],
            }
        )
    images = []
    for record in inputs:
        if record["original_image_available"]:
            images.append(
                {
                    "kind": "original_raw_preview",
                    "measurement_id": record["measurement_id"],
                    "artifact_sha256": record["provenance"]["source_sha256"],
                    "chart": record["chart"],
                    "role": record["role"],
                    "source_kind": record["source_kind"],
                    "variant": "original",
                    "preview_is_original_bytes": False,
                }
            )
    for run, summary, role in (
        (study, before, "study"),
        (validation, after, "independent_validation"),
    ):
        for image in summary.get("images", []):
            path = image.get("isp")
            _require(path in summary["files"], "Rendered image is not in its saved output manifest")
            images.append(
                {
                    "kind": "rendered_isp",
                    "study_id": run["id"],
                    "path": path,
                    "sha256": summary["files"][path],
                    "chart": image["chart"],
                    "measurement_id": image["measurement_id"],
                    "variant": image["variant"],
                    "role": role,
                    "blur_field": image.get("blur_field"),
                    "raw_path": image.get("raw"),
                    "observed_metrics": image.get("observed_metrics"),
                    "source": "fresh_simulation",
                }
            )
    covered = {c["chart"] for c in charts if c["study_comparisons"] and c["validation_comparisons"]}
    missing = [c for c in CHARTS if c not in covered]
    source_kinds = sorted({m["source_kind"] for m in inputs})
    source_kind = source_kinds[0] if len(source_kinds) == 1 else "mixed"
    outputs = {"study": _outputs(study), "validation": _outputs(validation)}
    identity = {
        "study_id": study_id,
        "validation_id": validation_id,
        "profile_id": profile_id,
        "campaign_id": campaign["id"],
        "candidate_sha256": candidate_hash,
        "study_result_sha256": outputs["study"]["result_sha256"],
        "validation_result_sha256": outputs["validation"]["result_sha256"],
        "profile_content_hash": profile["content_hash"],
        "campaign_content_hash": campaign["content_hash"],
    }
    report = {
        "schema": SCHEMA,
        **identity,
        "id": "calibration-completion-" + digest(identity)[:32],
        "title": "카메라 캘리브레이션 완료 보고서",
        "name": study.get("name", ""),
        "completion": {
            "process_status": "complete",
            "numerical_status": verdict["status"],
            "numerical_tolerance_pass": verdict.get("numerical_tolerance_pass") is True,
            "calibration_status": "partial_chart_coverage"
            if missing
            else "completed_numerical_pass"
            if verdict["status"] == "pass"
            else "completed_numerical_fail"
            if verdict["status"] == "fail"
            else "completed_inconclusive",
            "physical_validation": "not_established",
            "required_charts": list(CHARTS),
            "missing_independent_chart_coverage": missing,
            "required_chart_coverage_complete": not missing,
            "source_kind": source_kind,
            "source_kinds": source_kinds,
            "original_image_count": sum(m["original_image_available"] for m in inputs),
            "table_only_measurement_ids": [
                m["measurement_id"] for m in inputs if not m["original_image_available"]
            ],
        },
        "provenance": {
            "campaign": _record_identity(campaign),
            "camera_id": campaign.get("camera_id"),
            "source_context": campaign.get("source_context"),
            "db_bridge_id": campaign.get("db_bridge_id"),
            "db_optical_profile_id": campaign.get("db_optical_profile_id"),
            "study_created_at": study.get("created_at"),
            "validation_created_at": validation.get("created_at"),
            "study_source_code": study.get("source_code"),
            "validation_source_code": validation.get("source_code"),
            "fixed_conditions": candidate["conditions"],
            "native_shape": candidate.get("native_shape"),
            "pixel_pitch_um": candidate.get("pixel_pitch_um"),
            "cfa": candidate.get("cfa"),
            "development_lineage": sorted(development),
            "validation_lineage": sorted(final_lineage),
        },
        "inputs": inputs,
        "charts": charts,
        "parameters": changes,
        "images": images,
        "independent_validation": {
            **deepcopy(verdict),
            "parent_study_id": study_id,
            "parent_result_sha256": validation["parent_result_sha256"],
            "candidate_sha256": candidate_hash,
            "measurement_ids": [m["id"] for m in holdout],
            "refit_performed": False,
        },
        "final_profile": {
            **_record_identity(profile),
            "source_study_id": study_id,
            "profile_sha256": candidate_hash,
            "profile": candidate,
            "application_records": [
                _record_identity(r) for r in sorted(activations, key=lambda x: x["id"])
            ],
            "current_activation_state": "not_inferred",
            "selection_status": profile.get("selection_status", "explicit_application"),
            "db_design_use": profile.get("db_design_use"),
        },
        "unfitted_parameters": [
            {
                "parameter": key,
                "status": "fixed_not_identified_by_this_fit",
                "value": candidate["electronics"].get(key),
            }
            for key in ("full_well_e", "bit_depth")
        ]
        + [
            {"parameter": key, "status": "not_identified"}
            for key in (
                "physical_conversion_voltage_v_per_e",
                "separate_spectral_qe",
                "prnu_map",
                "dsnu_map",
                "unique_physical_psf",
            )
        ]
        + [{"parameter": "texture_denoiser", "status": "validation_only_not_fitted"}],
        "constraints": list(
            dict.fromkeys(
                LIMITATIONS + before.get("limitations", []) + after.get("limitations", [])
            )
        ),
        "hashes": {
            "source": {
                "study_source_code_sha256": digest(study.get("source_code")),
                "validation_source_code_sha256": digest(validation.get("source_code")),
                "baseline_profile_sha256": digest(study["base_profile"]),
            },
            "inputs": [
                {
                    "measurement_id": m["measurement_id"],
                    "source_sha256": m["provenance"]["source_sha256"],
                    "frame_hashes": m["frame_hashes"],
                    "artifacts": m["artifacts"],
                }
                for m in inputs
            ],
            "outputs": outputs,
        },
    }
    if any(
        m.get("method", {}).get("id") == "synthetic_native_rggb_chart_acquisition_v1"
        or m.get("reference", {}).get("method") == "synthetic_native_rggb_chart_acquisition_v1"
        for m in training + holdout
    ):
        report["constraints"].append(
            "이 합성 영상 실험은 같은 모델 계열을 사용하며 identity tone과 "
            "중성색 단위 응답을 고정한 조건입니다. RAW 분석의 전처리 블랙 기준값은 "
            "baseline 메타데이터 값으로 고정하며, 암전 피팅의 추정값과 구분합니다."
        )
    spatial_inputs = [m for m in inputs if m["chart"] == "mtf"]
    if spatial_inputs:
        coverage = {}
        for name, roles, comparisons in (
            ("training", {"training", "development"}, before.get("comparisons", [])),
            ("independent_validation", {"final_validation"}, after.get("comparisons", [])),
        ):
            expected = {m["measurement_id"] for m in spatial_inputs if m["role"] in roles}
            actual = {r["measurement_id"] for r in comparisons if r.get("chart") == "mtf"}
            coverage[name] = {
                "expected_measurement_ids": sorted(expected),
                "compared_measurement_ids": sorted(expected & actual),
                "missing_measurement_ids": sorted(expected - actual),
                "expected_count": len(expected),
                "compared_count": len(expected & actual),
                "complete": bool(expected) and expected <= actual,
            }
        report["spatial_coverage"] = coverage
    forward_observer = fit.get("forward_observer")
    if forward_observer is not None:
        _require(isinstance(forward_observer, dict), "Forward observer fit evidence must be an object")
        if forward_observer.get("candidate_sha256") is not None:
            _require(forward_observer["candidate_sha256"] == candidate_hash,
                     "Forward observer fit evidence belongs to a different candidate")
        report["forward_observer"] = deepcopy(forward_observer)
    image_validation = after.get("esfr_image_validation")
    if image_validation is not None:
        _require(image_validation.get("candidate_sha256") == candidate_hash,
                 "E-SFR image validation belongs to a different candidate")
        contract = image_validation.get("acquisition_contract") or {}
        if contract.get("candidate_sha256") is not None:
            _require(contract["candidate_sha256"] == candidate_hash,
                     "New acquisition contract belongs to a different frozen candidate")
        report["esfr_image_validation"] = deepcopy(image_validation)
    source_ids = sorted({m["provenance"].get("esfr_run_id") for m in inputs
                         if m["provenance"].get("esfr_run_id")})
    report["esfr_source_evidence"] = []
    for run_id in source_ids:
        if not all(c.isalnum() or c in "_.-" for c in run_id):
            raise IntegrityError("Unsafe E-SFR source run identity")
        owned = [m for m in inputs if m["provenance"].get("esfr_run_id") == run_id]
        report["esfr_source_evidence"].append({
            "run_id": run_id,
            "report_url": f"/api/v1/camera-validation/esfr/studies/{run_id}/files/report.html",
            "edge_ids": sorted({m["provenance"].get("roi_id", m["measurement_id"]) for m in owned}),
            "measurement_ids": [m["measurement_id"] for m in owned],
            "use": "input_provenance_only_not_current_candidate_isp_validation",
        })
    if source_ids:
        report["constraints"].append(
            "E-SFR 원본 보고서는 추출 곡선의 입력 근거입니다. 보정 방법은 해석식 곡선 피팅 "
            "또는 명시된 forward 영상 관측 연산자 피팅이며, 아래 새 RAW·ISP는 별도 edge probe입니다. "
            "원본 차트 픽셀 재현, 이번 후보의 원본 ISP 정합 또는 순수 렌즈 PSF 복원을 뜻하지 않습니다."
        )
    return _native(report)


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, dict)):
        if isinstance(value, dict) and value.get("representation") == "large_value_hash_reference":
            return f"원본 값 보존 · shape {value['shape_hint']} · SHA256 {value['sha256'][:16]}…"
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return encoded if len(encoded) <= 240 else encoded[:237] + "…"
    return LABELS.get(str(value), str(value))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return '<p class="muted">해당 자료가 없습니다.</p>'
    return (
        "<div class=scroll><table><thead><tr>"
        + "".join("<th>" + _escape(h) + "</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join("<td>" + _escape(_display(c)) + "</td>" for c in row) + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def _curve_svg(row: dict) -> str:
    series = [row.get(k, []) for k in ("measured", "baseline", "candidate")]
    x = row.get("axes", {}).get("x", [])
    if (
        not isinstance(x, list)
        or len(x) < 2
        or any(not isinstance(y, list) or len(y) != len(x) for y in series)
    ):
        return ""
    try:
        x = [float(v) for v in x]
        series = [[float(v) for v in y] for y in series]
    except (TypeError, ValueError):
        return ""
    if not all(math.isfinite(v) for v in x + [v for y in series for v in y]):
        return ""
    xmin, xmax = min(x), max(x)
    ymin, ymax = min(v for y in series for v in y), max(v for y in series for v in y)
    if xmax == xmin:
        return ""
    if ymax == ymin:
        ymax = ymin + 1
    width, height, left, top = 640, 225, 58, 22
    pw, ph = 554, 160
    svg = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="측정·기준·후보 곡선 비교">',
        f'<path d="M{left},{top} V{top + ph} H{left + pw}" fill="none" stroke="#9ba8b4"/>',
    ]
    for i in range(5):
        y = top + ph * i / 4
        value = ymax - (ymax - ymin) * i / 4
        svg.append(f'<text x="{left - 7}" y="{y + 4}" text-anchor="end">{value:.4g}</text>')
    stride = max(1, math.ceil(len(x) / 512))
    indexes = sorted(set([*range(0, len(x), stride), len(x) - 1]))
    for values, color in zip(series, ("#182a3b", "#bb6b14", "#087f75"), strict=True):
        points = " ".join(
            f"{left + (x[i] - xmin) / (xmax - xmin) * pw:.3f},"
            f"{top + ph - (values[i] - ymin) / (ymax - ymin) * ph:.3f}"
            for i in indexes
        )
        svg.append(f'<polyline points="{points}" stroke="{color}" fill="none" stroke-width="2"/>')
    svg.append(
        f'<text x="{left}" y="204">{xmin:.4g}</text>'
        f'<text x="{left + pw}" y="204" text-anchor="end">{xmax:.4g}</text>'
    )
    svg.append(
        '<text x="320" y="219" text-anchor="middle">'
        + _escape(row.get("axes", {}).get("unit", ""))
        + "</text></svg>"
    )
    return "".join(svg)


FORWARD_OBSERVER_METHOD = "development_native_bayer_same_observer_gaussian_v1"


def _history_report_url(value: Any) -> str | None:
    if not isinstance(value, str) or "\\" in value or any(c in value for c in "\r\n"):
        return None
    parts = value.split("/")
    if (len(parts) == 8 and parts[:4] == ["", "api", "v1", "calibration-lab"]
            and parts[4] in {"completion-reports", "studies"}
            and parts[6:] == ["files", "report.html"]
            and parts[5] and parts[5][0].isalnum()
            and all(c.isalnum() or c in "_.-" for c in parts[5])):
        return value
    return None


def _forward_provenance_html(forward: dict | None, image_validation: dict | None) -> str:
    image = image_validation or {}
    forward = forward or {}
    if not forward and not image.get("acquisition_contract") and not image.get("comparison_history"):
        return ""
    content = '<section id="forward-observer"><h3>보정 방법과 새 검증의 순서</h3>'
    if forward:
        recognized = forward.get("method") == FORWARD_OBSERVER_METHOD
        content += (
            '<p><strong>' + ("Forward 영상 관측 연산자 피팅" if recognized else "기록된 보정 방법")
            + '</strong> · ' + _escape(forward.get("method", "미기록")) + '</p>'
            + ('<p>RAW Gr SFR만 목적함수로 사용하여 native Bayer 생성·동일 SFR 측정 연산자로 '
               '조건부 Gaussian 폭을 맞춥니다. ISP는 피팅하지 않고 고정 bilinear 연산자로 별도 비교합니다. '
               '순수 렌즈 PSF 식별이나 ISO 적합성 인증이 아닙니다.</p>'
               if recognized else '<p>방법의 상세 범위는 보존된 실행 근거에서 확인합니다.</p>')
            + _table(["항목", "보존된 값"], [
                ["피팅 데이터 역할", "개발 촬영만 사용" if forward.get("fit_data_role") == "development_only"
                 else forward.get("fit_data_role", "미기록")],
                ["보정 위치", forward.get("field_count")],
                ["입력 E-SFR 실행", forward.get("source_study_id")],
                ["개발 촬영 수", len(forward.get("input_bindings", {}).get("development_capture_ids", []))],
                ["학습 측정 수", len(forward.get("input_bindings", {}).get("training_measurement_ids", []))],
                ["ROI 기하 SHA256", forward.get("input_bindings", {}).get("roi_geometry_sha256")],
            ])
        )
        metrics = forward.get("development_metrics", {})
        if metrics:
            content += '<h4>개발 촬영 오차 · 보정에 사용한 데이터</h4>' + _table(
                ["영역", "기존 해석식 후보 평균 RMSE", "Forward 후보 평균 RMSE", "Forward 후보 최대 RMSE", "비교 수"], [
                    [label, metrics.get(key, {}).get("before"), metrics.get(key, {}).get("after"),
                     metrics.get(key, {}).get("worst_after"), metrics.get(key, {}).get("count")]
                    for key, label in (("raw_sfr_rmse", "RAW Gr"), ("isp_sfr_rmse", "ISP"))
                ])
            content += ('<p class=muted>기존 해석식 후보를 native forward로 재보정한 개발 결과입니다. '
                        '원래 DB 기준 모델과의 비교는 아래 새 검증 결과에서 확인합니다. '
                        'RAW Gr 재측정 오차가 목적함수이며, 해석식 곡선 오차는 별도 진단으로 증가할 수 있습니다. '
                        'ISP는 고정 연산자의 진단이며 개발 데이터의 개선은 독립 검증 통과를 뜻하지 않습니다.</p>')
        focus_id = (image.get("comparison_history") or {}).get("focus_roi_id")
        focus = next((row for row in forward.get("fields", []) if focus_id and row.get("roi_id") == focus_id), None)
        if focus and focus.get("analytic_curve_rmse"):
            content += '<h4>집중 ROI의 개발 진단 · ' + _escape(focus_id) + '</h4>'
            content += _table(["개발 진단", "기존 해석식 후보", "Forward 후보"], [
                [label, focus.get(key, {}).get("before"), focus.get(key, {}).get("after")]
                for key, label in (("analytic_curve_rmse", "해석식 곡선 RMSE · 목적함수 아님"),
                                   ("raw_sfr_rmse", "Native RAW Gr RMSE · 피팅 목적함수"),
                                   ("isp_sfr_rmse", "고정 bilinear ISP RMSE · 별도 진단"))
            ])
    contract = image.get("acquisition_contract") or {}
    acquisition_ids = contract.get("new_validation_capture_ids", [])
    frozen = (image.get("candidate_frozen_before_new_acquisition") is True
              and image.get("validation_acquisition_status") in {"new_independent_synthetic_acquisition", "new_independent_measured_acquisition"}
              and bool(contract.get("candidate_sha256"))
              and contract.get("candidate_sha256") == image.get("candidate_sha256")
              and isinstance(acquisition_ids, list) and bool(acquisition_ids))
    if contract or image.get("validation_acquisition_status"):
        content += '<h4>후보 고정 → 새 취득 → 검증</h4>' + _table(["항목", "근거"], [
            ["후보 고정 후 새 취득", ("작업자 시각·확인서와 서버 등록 순서 확인" if image.get("validation_acquisition_status") == "new_independent_measured_acquisition" else "서버 근거로 확인") if frozen else "미확인"],
            ["검증 취득 종류", "새 독립 합성 취득" if image.get("validation_acquisition_status")
             == "new_independent_synthetic_acquisition" else "별도 세션의 새 실촬영 · 작업자 선언" if image.get("validation_acquisition_status") == "new_independent_measured_acquisition" else image.get("validation_acquisition_status")],
            ["취득 계약", contract.get("id")], ["계약 생성 시각", contract.get("created_at")],
            ["고정 후보 SHA256", contract.get("candidate_sha256")],
            ["새 검증 촬영 수", len(acquisition_ids) if isinstance(acquisition_ids, list) else None],
            ["새 검증 촬영 ID", ", ".join(str(value) for value in acquisition_ids) if isinstance(acquisition_ids, list) else None],
            ["RAW Gr 허용 RMSE", contract.get("thresholds", {}).get("raw_sfr_rmse")],
            ["ISP 허용 RMSE", contract.get("thresholds", {}).get("isp_sfr_rmse")],
        ])
        if image.get("validation_acquisition_status") == "new_independent_measured_acquisition":
            content += '<p>실촬영 분류·취득 시각·카메라 및 차트 동일성은 작업자 선언입니다. 장비 신원·취득 시각의 독립 인증, 추적 가능한 차트 MTF, ISO 적합성은 확립되지 않았습니다. 후보와 기준은 검증에 맞춰 다시 조정하지 않습니다.</p>'
        else:
            content += '<p>새 합성 취득은 물리 카메라의 새 실측을 뜻하지 않습니다. 후보와 기준은 검증에 맞춰 다시 조정하지 않습니다.</p>'
    history = image.get("comparison_history") or {}
    if history:
        roles = {"development": "개발 촬영 · 보정에 사용", "previous_holdout": "이전 검증 촬영 · 이미 확인한 이력",
                 "new_holdout": "새 독립 검증 촬영"}
        methods = {"analytic_curve": "해석식 곡선 피팅", "forward_observer": "Forward 영상 관측 연산자",
                   FORWARD_OBSERVER_METHOD: "Forward 영상 관측 연산자"}
        content += '<h4>이전 실패 위치의 보정 이력과 새 검증</h4><p>ROI: ' + _escape(history.get("focus_roi_id", "행별 확인"))
        content += ' · ' + ("기존 허용 기준 유지 확인" if history.get("thresholds_unchanged") is True else "기준 동일성 미확인") + '</p>'
        url = _history_report_url(history.get("prior_report_url"))
        if url:
            content += '<p><a href="' + _escape(url) + '">이전 실패 보고서 · 원본 이력 ↗</a></p>'
        rows = history.get("rows", [])
        content += _table(["데이터 역할", "방법", "촬영 ID / ROI", "RAW RMSE", "RAW 기준", "ISP RMSE", "ISP 기준", "판정"], [
            [roles.get(row.get("dataset_role"), row.get("dataset_role", "미기록")),
             methods.get(row.get("fit_method"), row.get("fit_method", "미기록")),
             str(row.get("capture_id", "")) + " / " + str(row.get("roi_id", history.get("focus_roi_id", ""))),
             row.get("raw_rmse"), row.get("raw_threshold"), row.get("isp_rmse"), row.get("isp_threshold"), row.get("status")]
            for row in rows
        ])
        content += '<p class=muted>이전 검증 결과는 방법 개선에 참고한 이력입니다. 새 독립 검증과 합쳐 평균 내거나 이전 실패를 지우지 않습니다.</p>'
    return content + '</section>'


def _esfr_image_verdict_html(image_validation: dict) -> str:
    """Display every failed check, while leaving the full check ledger in JSON."""
    verdict = image_validation.get("validation", {})
    checks = verdict.get("checks", [])
    status = verdict.get("status", "not_evaluated")
    status_text = {"pass": "선언한 영상 기준 충족", "fail": "영상 기준 미충족",
                   "not_evaluated": "영상 기준 미평가", "inconclusive": "영상 판정 보류"}.get(
                       status, "영상 판정: " + str(status))
    raw_only = verdict.get("scope") == "raw_native_sfr_only"
    if raw_only:
        status_text = "RAW 단독 기준 · " + status_text + " · ISP는 진단 전용"
    counts = {key: sum(row.get("status") == key for row in checks)
              for key in ("pass", "fail", "unavailable")}
    remaining = len(checks) - sum(counts.values())
    output = (
        '<div class="notice" data-image-validation-status="' + _escape(status) + '">'
        '<strong>' + _escape(status_text) + '</strong><p>'
        + (f"전체 {len(checks)}개 검사 · 통과 {counts['pass']} · 실패 {counts['fail']} · "
           f"측정 불가 {counts['unavailable']} · 미평가/기타 {remaining}"
           if checks else "세부 검사 항목이 기록되지 않았습니다.")
        + "</p><p>평균 오차 개선만으로 모든 위치가 기준을 충족한 것으로 해석하지 않습니다. "
        "기존 Calibration Lab의 곡선 피팅 판정과 별도 검사입니다.</p></div>"
    )
    metrics = image_validation.get("metrics", {})
    thresholds = verdict.get("thresholds", {})
    rows = []
    for key, label in (("raw_sfr_rmse", "RAW Gr"), ("isp_sfr_rmse", "ISP")):
        metric = metrics.get(key, {})
        selected = [row for row in checks if row.get("metric") == key]
        threshold = thresholds.get(key)
        if threshold is None:
            values = list(dict.fromkeys(row.get("threshold") for row in selected))
            threshold = values[0] if len(values) == 1 else values or None
        rows.append([label, metric.get("before"), metric.get("after"), metric.get("worst_after"),
                     threshold if threshold is not None else "미지정", metric.get("count"),
                     sum(row.get("status") == "fail" for row in selected)])
    output += _table(
        ["영역", "평균 RMSE 전", "평균 RMSE 후", "최대 RMSE 후", "위치별 허용 RMSE", "비교 수", "실패"], rows
    )
    if raw_only:
        output += '<p class="notice">제조사 ISP가 고정 bilinear 참조 연산자와 일치한다고 확인되지 않았습니다. ISP 오차와 0.05 기준값은 기록하지만 합격 판정에 사용하지 않습니다. RAW 합격을 ISP 합격으로 해석하지 않습니다.</p>'
    failed = [row for row in checks if row.get("status") == "fail"]
    if failed:
        output += (
            '<h4>기준을 넘은 모든 검사 · ' + str(len(failed)) + '건</h4>'
            + _table(["ROI", "촬영 ID", "지표", "보정 후 RMSE", "허용 RMSE", "초과량"], [
                [row.get("roi_id"), row.get("capture_id"), row.get("metric"), row.get("value"),
                 row.get("threshold"), row["value"] - row["threshold"]
                 if isinstance(row.get("value"), (int, float))
                 and isinstance(row.get("threshold"), (int, float)) else None]
                for row in failed
            ])
        )
    output += "<p class=muted>전체 검사 항목은 report.json과 RAW·ISP 상세 보고서에 보존됩니다.</p>"
    return output


def render_completion_report(
    report: dict[str, Any], *, image_resolver: ImageResolver | None = None
) -> bytes:
    """Render escaped Korean HTML with inline SVG and optional inline image bytes.

    Resolver descriptors contain exact run/path or measurement/original hash IDs.
    No URL is fetched here. Original RAW previews are display derivatives, not the
    original bytes, and are labeled accordingly. No scripts or external assets.
    """
    data = _native(report)
    if data.get("schema") != SCHEMA:
        raise ValueError("Unsupported calibration completion report schema")
    completion = data["completion"]
    numerical = {
        "pass": "수치 검증 합격",
        "fail": "수치 검증 실패",
        "inconclusive": "수치 검증 판단 보류",
    }[completion["numerical_status"]]
    source = {"synthetic": "합성 데이터", "measured": "실측 데이터", "mixed": "혼합 데이터"}.get(
        completion["source_kind"], completion["source_kind"]
    )
    sections = [
        "<header><p class=eyebrow>CAMERA CALIBRATION · COMPLETION EVIDENCE</p>"
        f"<h1>{_escape(data['title'])}</h1>",
        f"<p>{_escape(data.get('name', ''))}</p>"
        f'<p class="status {_escape(completion["numerical_status"])}">{_escape(numerical)}</p>',
        f"<p>작업 처리 완료 · {_escape(source)} · 물리 검증 미확립</p>",
        "<p class=notice>저장된 프로파일과 독립 검증 결과를 명시한 보고서입니다. "
        "합성 영상의 일치나 수치 합격을 실제 카메라의 물리 검증으로 "
        "해석하지 않습니다.</p></header>",
    ]
    image_status = data.get("esfr_image_validation", {}).get("validation", {}).get("status")
    image_scope = data.get("esfr_image_validation", {}).get("validation", {}).get("scope")
    if image_status:
        image_label = "RAW 단독 기준 · ISP 진단" if image_scope == "raw_native_sfr_only" else "RAW·ISP 영상 기준"
        image_verdict = {"pass": "충족", "fail": "미충족", "inconclusive": "판단 보류",
                         "not_evaluated": "미평가"}.get(image_status, image_status)
        sections.append('<p class="notice"><strong>' + _escape(image_label + ": " + image_verdict)
                        + '</strong> · <a href="#esfr-image-validation">전체 edge 영상 판정 확인 ↓</a><br>'
                        + '상단 수치 검증은 일반 곡선 비교의 판정입니다. 영상 오차 판정과 별도로 기록하며, '
                        + '이 E-SFR 작업의 후보 활성화는 선언한 영상 기준으로 결정합니다.</p>')
    if image_status == "fail":
        image_checks = data["esfr_image_validation"]["validation"].get("checks", [])
        failed_count = sum(row.get("status") == "fail" for row in image_checks)
        sections.append(
            '<p class="status fail"><strong>'
            + ('RAW 영상 검증 기준 미충족 · ISP 진단' if image_scope == 'raw_native_sfr_only' else 'RAW·ISP 영상 검증 기준 미충족')
            + '</strong> · '
            + str(failed_count) + ' / ' + str(len(image_checks))
            + '개 검사 실패. <a href="#esfr-image-validation">실패 위치와 오차 확인 ↓</a></p>'
        )
    if not completion["required_chart_coverage_complete"]:
        sections.append(
            "<p class=notice>전체 8개 차트가 독립 검증되지 않았습니다. 누락: "
            + _escape(", ".join(completion["missing_independent_chart_coverage"]))
            + "</p>"
        )
    sections.append(
        "<section><h2>1. 작업 식별과 고정 조건</h2>"
        + _table(
            ["항목", "값"],
            [
                ["카메라", data["provenance"].get("camera_id")],
                *[[k, data[k]] for k in ("campaign_id", "study_id", "validation_id", "profile_id")],
                ["Candidate SHA256", data["candidate_sha256"]],
                ["고정 조건", data["provenance"]["fixed_conditions"]],
                [
                    "센서 형태 / pitch / CFA",
                    [
                        data["provenance"]["native_shape"],
                        data["provenance"]["pixel_pitch_um"],
                        data["provenance"]["cfa"],
                    ],
                ],
            ],
        )
        + "</section>"
    )
    sections.append(
        "<section><h2>2. 입력 원본과 측정 근거</h2><p>원본 RAW 이미지 묶음 "
        + str(completion["original_image_count"])
        + "건. 표·메타데이터 입력을 실제 촬영 이미지로 표시하지 않습니다.</p>"
        + _table(
            ["차트", "용도", "종류", "원본", "측정법", "원본 SHA256"],
            [
                [
                    CHART_NAMES.get(m["chart"], m["chart"]),
                    m["role"],
                    m["source_kind"],
                    "RAW 이미지 원본" if m["original_image_available"] else "표 / 메타데이터",
                    m.get("quality", {}).get("method", m.get("method")),
                    m["provenance"]["source_sha256"][:16] + "…",
                ]
                for m in data["inputs"]
            ],
        )
        + "</section>"
    )
    sections.append(
        "<section><h2>3. 차트별 추정·재시뮬레이션·독립 비교</h2><p class=legend>"
        '<span>● 관측값</span><span style="color:#bb6b14">● 기준 모델</span>'
        '<span style="color:#087f75">● 후보 모델</span></p>'
    )
    coverage = data.get("spatial_coverage", {})
    if coverage:
        sections.append(
            "<section id=spatial-coverage><h3>모든 MTF 위치의 비교 범위</h3>"
            + _table(["역할", "등록", "비교 완료", "누락", "전체 포함"], [
                [_display(phase), row["expected_count"], row["compared_count"],
                 len(row["missing_measurement_ids"]), "예" if row["complete"] else "아니오"]
                for phase, row in coverage.items()
            ]) + "<p>아래에 모든 측정 ID의 곡선·오차·새 probe 영상을 포함합니다. "
            "비교 범위 충족과 수치 기준 통과는 별도 판정입니다.</p></section>"
        )
    for source_evidence in data.get("esfr_source_evidence", []):
        url = source_evidence["report_url"]
        sections.append(
            "<p class=notice>원본 E-SFR 입력 근거 · "
            + str(len(source_evidence["edge_ids"])) + '개 edge · <a href="' 
            + _escape(url) + '">' + _escape(source_evidence["run_id"]) + "</a><br>"
            "기존 보고서는 입력 출처입니다. 이번 후보의 원본 ISP 검증 결과가 아닙니다. "
            "이번 보정의 RAW·ISP 영상은 별도 edge probe로 생성했습니다.</p>"
        )
    image_validation = data.get("esfr_image_validation")
    sections.append(_forward_provenance_html(data.get("forward_observer"), image_validation))
    if image_validation:
        expected_url = (f"/api/v1/calibration-lab/studies/{data['validation_id']}"
                        "/files/esfr-images/report.html")
        _require(image_validation.get("report_url") == expected_url,
                 "E-SFR image report URL differs from the frozen validation run")
        sections.append(
            "<section id=esfr-image-validation><h3>고정 후보의 RAW·ISP 영상 비교</h3>"
            "<p>Calibration Lab에서 얻은 위치별 파라미터를 바꾸지 않고 원본 검증 촬영에 "
            "대응하는 edge 영상을 다시 계산했습니다. 밝기·기하 등 보조 조건은 개발 자료에서 "
            "고정하며 검증 자료로 재피팅하지 않습니다.</p>"
            + _esfr_image_verdict_html(image_validation)
            + _table(["항목", "값"], [
                ["위치 수", image_validation.get("field_count")],
                ["비교 수", image_validation.get("comparison_count")],
                ["방법과 범위", image_validation.get("basis")],
                ["검증 촬영", "이전 E-SFR holdout 재사용 · 신규 블라인드 취득 아님"
                 if image_validation.get("validation_acquisition_status")
                 == "reused_source_holdout_not_new_blinded_acquisition"
                 else image_validation.get("validation_acquisition_status")],
                ["고정 후보 SHA256", image_validation.get("candidate_sha256")],
            ])
            + '<p><a href="' + _escape(expected_url)
            + '">전체 위치의 RAW·ISP 영상·MTF 보고서 열기 ↗</a></p>'
            "<p>이 결과도 합성 또는 등록된 촬영 조건에 대한 비교이며, "
            "순수 렌즈 PSF 식별이나 물리적 카메라 인증을 뜻하지 않습니다.</p></section>"
        )
    input_by_id = {item["measurement_id"]: item for item in data["inputs"]}
    image_bytes = 0
    embedded_count = 0
    for chart in data["charts"]:
        sections.append(
            "<article><h3>"
            + _escape(chart["label"])
            + "</h3><p>"
            + _escape(chart["conversion"])
            + "</p><p class=muted>상태: "
            + _escape(_display(chart["status"]))
            + "</p>"
        )
        rows = [("학습/개발", r) for r in chart["study_comparisons"]] + [
            ("독립 검증", r) for r in chart["validation_comparisons"]
        ]
        sections.append(
            _table(
                ["비교", "측정 ID / 지표", "기준 RMSE", "후보 RMSE", "관측 연산"],
                [
                    [
                        phase,
                        r.get("measurement_id", "") + " / " + r.get("metric_id", ""),
                        r.get("residuals", {}).get("baseline_rmse"),
                        r.get("residuals", {}).get("candidate_rmse"),
                        r.get("operator_parity", "unknown"),
                    ]
                    for phase, r in rows
                ],
            )
        )
        for phase, row in rows:
            plot = _curve_svg(row)
            if plot:
                sections.append(
                    ("<details open><summary>" if phase == "독립 검증" else "<details><summary>")
                    + _escape(phase + " · " + row.get("measurement_id", ""))
                    + "</summary>"
                    + plot
                    + "</details>"
                )
        selected = [im for im in data.get("images", []) if im["chart"] == chart["chart"]]
        # Preserve every measurement/position, including more than 64 previews.
        # Fail explicitly on an oversized document; never silently omit edges.
        sections.append("<div class=images>")
        for descriptor in selected:
            title = (
                (
                    "원본 RAW 표시용 미리보기"
                    if descriptor["kind"] == "original_raw_preview"
                    else "새 시뮬레이션 ISP"
                )
                + " · "
                + _display(descriptor.get("role", ""))
                + " · "
                + _display(descriptor.get("variant", ""))
                + " · " + str(input_by_id.get(descriptor.get("measurement_id"), {}).get(
                    "provenance", {}).get("roi_id", descriptor.get("measurement_id", "")))
            )
            content = (
                image_resolver(deepcopy(descriptor))
                if image_resolver is not None
                else None
            )
            if content is None:
                sections.append(
                    "<figure><figcaption>"
                    + _escape(title)
                    + " · 이미지 미포함</figcaption></figure>"
                )
                continue
            pixels, mime = content
            valid = isinstance(pixels, bytes) and mime in {"image/png", "image/jpeg"}
            valid = valid and (
                (mime == "image/png" and pixels.startswith(b"\x89PNG\r\n\x1a\n"))
                or (mime == "image/jpeg" and pixels.startswith(b"\xff\xd8\xff"))
            )
            if not valid or len(pixels) > 5 * 1024**2 or image_bytes + len(pixels) > 512 * 1024**2:
                raise ValueError("Embedded previews require bounded PNG/JPEG bytes")
            if (
                descriptor["kind"] == "rendered_isp"
                and hashlib.sha256(pixels).hexdigest() != descriptor["sha256"]
            ):
                raise IntegrityError("Rendered preview differs from its saved artifact hash")
            image_bytes += len(pixels)
            embedded_count += 1
            uri = "data:" + mime + ";base64," + base64.b64encode(pixels).decode("ascii")
            sections.append(
                '<figure><img alt="'
                + _escape(title)
                + '" loading="lazy" src="'
                + uri
                + '"><figcaption>'
                + _escape(title)
                + "</figcaption></figure>"
            )
        sections.append("</div></article>")
    sections.append("</section>")
    sections.append(
        "<section><h2>4. 실제 갱신 파라미터</h2>"
        + _table(
            ["파라미터", "이전", "이후", "단위"],
            [
                [r.get("parameter"), r.get("before"), r.get("after"), r.get("unit")]
                for r in data["parameters"]
            ],
        )
        + "</section>"
    )
    validation = data["independent_validation"]
    sections.append(
        "<section><h2>5. Calibration Lab 곡선 검증</h2><p>"
        + _escape(numerical)
        + " · 최종 검증 데이터로 후보를 다시 피팅하지 않았습니다.</p>"
        + ("<p class=notice>곡선 피팅 비교의 사전 허용오차가 지정되지 않아 "
           "이 항목은 미평가입니다. 위 RAW·ISP 영상의 위치별 기준과 판정은 "
           "별도로 표시합니다.</p>"
           if validation.get("checks") and all(row.get("max_rmse") is None
                                               for row in validation["checks"]) else "")
        + _table(
            ["지표", "RMSE", "허용 RMSE", "판정"],
            [
                [r.get("metric_id"), r.get("rmse"), r.get("max_rmse"), r.get("status")]
                for r in validation.get("checks", [])
            ],
        )
        + "</section>"
    )
    sections.append(
        "<section><h2>6. 최종 저장 프로파일</h2><p>Profile "
        + _escape(data["profile_id"])
        + "</p><p>SHA256 <code>"
        + _escape(data["candidate_sha256"])
        + "</code></p><p>"
        + ("후보를 비활성 상태로 저장했습니다. 이 저장으로 기존 활성 프로파일을 바꾸지 않았습니다. "
           if data.get("final_profile", {}).get("selection_status") == "saved_without_activation"
           else "명시적 적용 기록이 보존되어 있습니다. ")
        + "이 보고서는 현재 활성 프로파일을 추정하지 않습니다.</p></section>"
    )
    sections.append(
        "<section><h2>7. 미추정 항목과 적용 한계</h2>"
        + _table(
            ["파라미터", "상태", "고정값"],
            [[r["parameter"], r["status"], r.get("value")] for r in data["unfitted_parameters"]],
        )
        + "<ul>"
        + "".join("<li>" + _escape(v) + "</li>" for v in data["constraints"])
        + "</ul></section>"
    )
    sections.append(
        "<section><h2>8. 원본·코드·산출물 해시</h2><p>전체 파일별 SHA256은 "
        "함께 제공되는 JSON 보고서에 보존됩니다. 여기에는 대표 식별값만 표시합니다.</p>"
        + _table(
            ["대상", "해시 / 개수"],
            [
                ["학습 실행 결과", data["study_result_sha256"]],
                ["독립 검증 결과", data["validation_result_sha256"]],
                ["기준 프로파일", data["hashes"]["source"]["baseline_profile_sha256"]],
                ["학습 산출물", data["hashes"]["outputs"]["study"]["file_count"]],
                ["독립 검증 산출물", data["hashes"]["outputs"]["validation"]["file_count"]],
            ],
        )
        + "<p class=muted>입력 원본의 등록 소유권·해시와 결과 summary 바인딩을 확인했습니다. "
        "전체 산출물 바이트 재해시는 보고서 생성과 별도입니다.</p></section>"
    )
    css = """
body{margin:0;
background:#eef2f5;
color:#172b3a;
font:15px/1.65 system-ui,-apple-system,sans-serif}
main{max-width:1180px;
margin:auto;
padding:36px 24px}
header,section{background:white;
border:1px solid #dce4e9;
border-radius:12px;
padding:24px;
margin-bottom:20px}
h1{font-size:30px;
margin:.3em 0}
h2{font-size:22px}
h3{font-size:18px}
.eyebrow{font-size:12px;
letter-spacing:1.8px;
color:#516776}
.status{display:inline-block;
padding:6px 12px;
border-radius:7px;
font-weight:700}
.pass{background:#daf2e9;
color:#075d43}
.fail{background:#fde5e3;
color:#9c2218}
.inconclusive{background:#fff0ce;
color:#805600}
.notice{padding:12px;
background:#f5f7fa;
border-left:4px solid #788fa1}
.muted{color:#5a6c79;
font-size:13px}
table{border-collapse:collapse;
width:100%;
font-size:13px}
th,td{padding:9px 10px;
border-bottom:1px solid #e3e9ed;
text-align:left;
vertical-align:top;
overflow-wrap:anywhere}
th{background:#f1f5f8}
.scroll{overflow-x:auto}
article{border-top:1px solid #dce4e9;
padding-top:12px;
margin-top:20px}
svg{display:block;
width:100%;
max-width:780px;
background:#fbfcfd}
svg text{font-size:10px;
fill:#4c606f}
details{margin:8px 0}
summary{cursor:pointer;
color:#235c78}
.legend{display:flex;
gap:24px;
flex-wrap:wrap}
.images{display:grid;
grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
gap:14px}
figure{margin:8px 0;
background:#f6f8fa;
padding:10px;
border-radius:6px}
img{max-width:100%;
height:auto;
display:block;
margin:auto;
image-rendering:auto}
figcaption{font-size:12px;
color:#526876;
margin-top:7px}
code{overflow-wrap:anywhere;
font-size:12px}
@media print{body{background:white}
main{padding:0}
header,section{border:0;
padding:10px}
details{display:block}
article{break-inside:avoid}
}
"""
    document = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; img-src data:; "
        "style-src 'unsafe-inline'; base-uri 'none'\"><title>"
        + _escape(data["title"])
        + "</title><style>"
        + css
        + "</style></head><body><main>"
        + "".join(sections)
        + "</main></body></html>"
    )
    return document.encode("utf-8")
