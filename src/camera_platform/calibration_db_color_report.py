"""Render immutable DB color-model replay evidence without upgrading its validation scope."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from collections.abc import Callable
from typing import Any

VARIANTS = {
    "db_baseline": "원본 DB 모델 · 기존 ISP",
    "qe_only_db_isp": "새 QE · 기존 DB ISP",
    "qe_ccm_baseline": "새 QE · 보정 전 CCM",
    "qe_ccm_fitted": "새 QE · 추정한 CCM",
}
COMPARISONS = {
    "qe_raw_change": "QE 적용에 따른 RAW 변화",
    "db_isp_change": "동일 DB ISP에서 QE 변경 영향",
    "ccm_output_change": "같은 RAW · 같은 출력 공간의 CCM 변화",
}


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(text) > 2400:
            return (
                f"전체 값은 결과 JSON에 보존 · {len(text.encode())} bytes · "
                f"SHA256 {hashlib.sha256(text.encode()).hexdigest()}"
            )
        return text
    return str(value)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return '<p class="muted">제공된 수치가 없습니다.</p>'
    return (
        '<div class="scroll"><table><thead><tr>'
        + "".join("<th>" + _escape(value) + "</th>" for value in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join("<td>" + _escape(_display(value)) + "</td>" for value in row) + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def _detail(title: str, value: Any) -> str:
    return (
        "<details><summary>"
        + _escape(title)
        + "</summary><pre>"
        + _escape(_display(value))
        + "</pre></details>"
    )


def _image_source(summary: dict, path: Any, resolver: Callable[[str], bytes] | None) -> str | None:
    if not isinstance(path, str) or not path.split("/"):
        return None
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) for part in path.split("/")):
        return None
    expected = summary.get("files", {}).get(path)
    if isinstance(expected, dict):
        expected = expected.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        return None
    if not path.lower().endswith(".png"):
        return None
    if resolver is None:
        return path
    content = resolver(path)
    if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("Report image differs from its preserved manifest: " + path)
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Report image is not PNG: " + path)
    return "data:image/png;base64," + base64.b64encode(content).decode("ascii")


def _image_pair(summary: dict, field: dict, keys: tuple[str, str], resolver) -> str:
    by_key = {item.get("variant"): item for item in field.get("variants", [])}
    figures = []
    for key in keys:
        item = by_key.get(key, {})
        body = "<figure><h4>" + _escape(VARIANTS[key]) + "</h4>"
        for stage, label in (("raw_preview", "RAW 미리보기"), ("isp", "ISP 출력")):
            source = _image_source(summary, item.get("images", {}).get(stage), resolver)
            body += "<p class='muted'>" + label + "</p>"
            if source:
                body += (
                    '<img src="'
                    + _escape(source)
                    + '" alt="'
                    + _escape(str(field.get("field_id")) + " · " + VARIANTS[key] + " · " + label)
                    + '">'
                )
            else:
                body += '<p class="missing">검증된 이미지 파일이 제공되지 않았습니다.</p>'
        body += "<figcaption>출력 공간: " + _escape(item.get("output_space", "미기록"))
        figures.append(body + "</figcaption></figure>")
    return '<div class="pair">' + "".join(figures) + "</div>"


def _field_section(summary: dict, field: dict, resolver) -> str:
    identity = field.get("field_id")
    label = {"center": "중심", "outer-ne": "주변 우상"}.get(identity, identity)
    parts = ["<section><h2>" + _escape(label) + " · Macbeth RAW / ISP</h2>"]
    parts.append(
        "<h3>1. QE 적용 전·후 · 같은 DB ISP</h3>"
        "<p>원본 모델과 새 QE의 영향을 기존 DB ISP로 비교합니다.</p>"
        + _image_pair(summary, field, ("db_baseline", "qe_only_db_isp"), resolver)
        + "<h3>2. CCM 적용 전·후 · 같은 RAW와 출력 공간</h3>"
        "<p>QE와 RAW를 고정하고 보정 전 행렬과 추정 행렬을 비교합니다. "
        "camera_linear_rgb 미리보기는 색도 기준 sRGB 표현이 아닙니다. "
        "목표 공간이 다른 위 DB ISP와의 색 정확도 순위는 계산하지 않습니다.</p>"
        + _image_pair(summary, field, ("qe_ccm_baseline", "qe_ccm_fitted"), resolver)
    )
    parts.append(
        "<h3>RAW 취득과 출력 공간</h3>"
        + _table(
            ["변형", "출력 공간", "프레임 수", "시간 잡음 (DN)", "잘림 비율"],
            [
                [
                    VARIANTS.get(item.get("variant"), item.get("variant")),
                    item.get("output_space"),
                    len(item.get("raw", {}).get("frames", [])),
                    item.get("raw", {}).get("temporal_noise_dn"),
                    item.get("raw", {}).get("clipped_fraction"),
                ]
                for item in field.get("variants", [])
            ],
        )
        + '<p class="scope">새 QE의 세 변형에서 RAW 원본 동일성: '
        + (
            "저장 원본 동일 확인"
            if field.get("raw_identity", {}).get("qe_variants_identical") is True
            else "확인 근거 없음"
        )
        + ". CCM은 RAW를 바꾸지 않습니다.</p>"
    )
    comparisons = [
        item for item in summary.get("comparisons", []) if item.get("field_id") == identity
    ]
    parts.append(
        "<h3>24패치의 변화량 · 정답 대비 오차 아님</h3>"
        + _table(
            ["비교", "단위", "변화량 RMSE", "최대 절대 변화"],
            [
                [
                    COMPARISONS.get(item.get("kind"), item.get("kind")),
                    item.get("unit"),
                    item.get("rmse_change"),
                    item.get("max_abs_change"),
                ]
                for item in comparisons
            ],
        )
        + "<p>독립 실측 정답이 없는 모델 간 변화입니다. RMSE가 작다는 사실은 "
        "실측 정확도가 높다는 뜻이 아니며, 개선율·ΔE·검증 통과를 산출하지 않습니다.</p>"
    )
    for item in comparisons:
        baseline, candidate, difference = (
            item.get(key, []) for key in ("baseline", "candidate", "difference")
        )
        parts.append(
            "<details><summary>"
            + _escape(COMPARISONS.get(item.get("kind"), item.get("kind")))
            + " · 패치별 값</summary>"
            + _table(
                ["패치", "기준 R/G/B", "후보 R/G/B", "후보 − 기준"],
                [
                    [
                        index + 1,
                        baseline[index] if index < len(baseline) else None,
                        candidate[index] if index < len(candidate) else None,
                        difference[index] if index < len(difference) else None,
                    ]
                    for index in range(max(len(baseline), len(candidate), len(difference)))
                ],
            )
            + _detail(
                "비교 정의",
                {"unit": item.get("unit"), "comparison_type": item.get("comparison_type")},
            )
            + "</details>"
        )
    parts.append(
        _detail(
            "시야 기하와 원본 RAW 프레임",
            {
                "geometry": field.get("geometry"),
                "raw": {
                    item.get("variant", "unknown"): item.get("raw")
                    for item in field.get("variants", [])
                },
            },
        )
    )
    return "".join(parts) + "</section>"


def render_db_color_report(
    summary: dict[str, Any], *, image_resolver: Callable[[str], bytes] | None = None
) -> bytes:
    """Render a replay; a resolver embeds only manifest-verified PNG image bytes."""
    if not isinstance(summary, dict) or not summary.get("run_id") or not summary.get("model_id"):
        raise ValueError("Preserved run_id and model_id are required for a DB color report")
    data = json.loads(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    source = data.get("source_context", data.get("source_identity", {}))
    source_identity = {
        key: source.get(key)
        for key in ("run_id", "candidate_id", "condition_id", "manifest_sha256")
    }
    parts = [
        "<header><p class='eyebrow'>CAMERAE2E · DB COLOR MODEL REPLAY</p>"
        "<h1>QE · CCM 적용 모델<br>Macbeth RAW · ISP 재실행 보고서</h1>"
        "<p class='badge'>합성 모델 이전</p><p class='badge'>물리 검증 미확립</p>"
        "<p class='scope'>이 보고서는 갱신된 DB 모델의 새 계산을 기록합니다. "
        "원래 보정 데이터에서 얻은 검증 결과를 DB 조건의 실측 통과로 이전하지 않습니다. "
        "DB 조건 독립 실측 정합: 미확립.</p>",
        _table(
            ["근거", "고정된 식별자"],
            [
                ["재실행 ID", data["run_id"]],
                ["보고서 생성 시각", data.get("report_generated_at")],
                ["사용 모델 ID", data["model_id"]],
                ["색 응답 보정 ID", data.get("color_calibration_id")],
                ["적용 프로파일 SHA256", data.get("applied_profile_sha256")],
                ["원본 DB 선택", source_identity],
                ["합성 이전 가정", data.get("binding")],
                ["취득 설정", data.get("configuration")],
            ],
        ),
        "</header>",
    ]
    h = data.get("holdout", {})
    if h.get("status") == "synthetic_holdout_completed":
        if (
            h.get("source", {}).get("model_id") != data["model_id"]
            or h.get("source", {}).get("replay_id") != data["run_id"]
        ):
            raise ValueError("Holdout report model/run binding differs")
        if h.get("report_path") != "holdout/report.html" or not data.get("files", {}).get(
            h["report_path"]
        ):
            raise ValueError("Holdout report is outside the preserved manifest")
        parts.append(
            "<section><h2>가상 실측 · 보정 전후 자동 비교</h2>"
            "<p>새 독립 취득과 보정 전후 결과의 24패치 오차입니다. "
            "합성 검증이며 물리 검증은 미확립입니다.</p>"
            + _table(
                ["지표", "보정 전 RMSE", "보정 후 RMSE", "감소 (%)", "독립 반복 차이"],
                [
                    [
                        name,
                        h["aggregate"][key]["before"]["rmse"],
                        h["aggregate"][key]["after"]["rmse"],
                        h["aggregate"][key]["reduction_percent"],
                        h["aggregate"][key]["replicate"]["rmse"],
                    ]
                    for key, name in (("raw", "RAW · DN"), ("isp", "ISP · 선형 RGB"))
                ],
            )
            + '<p><a href="holdout/report.html">가상 실측·보정 전후 차트와 오차 보고서 ↗</a> · '
            '<a href="holdout/evidence.zip">가상 실측 근거 ZIP ↓</a></p></section>'
        )
    if not data.get("fields"):
        parts.append("<section><h2>시야별 결과</h2><p>제공된 계산 결과가 없습니다.</p></section>")
    parts.extend(_field_section(data, field, image_resolver) for field in data.get("fields", []))
    limits = [
        "QE는 센서면의 절대 분광 응답이며, 입사각 독립·공유 Green 응답이라는 "
        "명시적 이전 가정을 사용합니다.",
        "기존 DB 광학 모델은 고정합니다. QE 적용 후 새 광전자와 RAW를 계산하며 "
        "기존 RAW 밝기 보정과 구분합니다.",
        "기존 보정의 RAW·CCM 검증은 원래 카메라·광량·노출 조건의 근거입니다. "
        "DB 조건의 새로운 실측 검증은 미확립입니다.",
        "CCM 적용 전·후는 같은 RAW와 같은 출력 공간으로 비교합니다. "
        "CCM으로 RAW 오차를 없애지 않습니다.",
        "파라미터 변화량 표의 RMSE는 두 계산의 변화량이며 정답 대비 오차가 아닙니다. "
        "이 재실행은 물리 카메라 검증이나 제품 합격을 증명하지 않습니다.",
    ]
    limits.extend(item for item in data.get("limitations", []) if isinstance(item, str))
    parts.append(
        "<section><h2>적용 범위와 보존 근거</h2><ul>"
        + "".join("<li>" + _escape(item) + "</li>" for item in dict.fromkeys(limits))
        + "</ul>"
        + _detail("원본 DB 선택 전체 근거", source)
        + _detail("정확히 적용한 QE·CCM", data.get("application"))
        + _detail("센서·ISP 변환 계약", data.get("projection"))
        + _detail("원본 및 파일 SHA256", data.get("files"))
        + "<p>모델·프로파일·RAW·선형 결과·미리보기·파일 해시는 "
        "함께 제공되는 JSON과 근거 ZIP에 보존됩니다.</p>"
        + (
            ""
            if image_resolver
            else "<p>이 HTML의 영상은 근거 ZIP 안의 상대경로 파일을 사용합니다. "
            "파일 구조를 유지하세요.</p>"
        )
        + "</section>"
    )
    css = """body{margin:0;
background:#f4f6f1;
color:#193b3e;
font:14px/1.65 system-ui,sans-serif}

main{max-width:1160px;
margin:auto;
padding:30px}
header,section{background:white;
border:1px solid #dbe4dc;
border-radius:12px;
padding:25px;
margin-bottom:20px}

h1{font-size:32px;
line-height:1.3}
h2{font-size:22px}
h3{margin-top:24px}
h4{font-size:14px;
margin:0 0 12px}
.eyebrow{letter-spacing:2px;
font-size:10px;
color:#087e81}

.scope{border-left:3px solid #966525;
padding:12px;
background:#fcf7ef;
color:#6d552f}
.badge{display:inline-block;
border:1px solid #dbe4dc;
border-radius:30px;
padding:4px 10px;
font-size:11px;
margin-right:8px}
.muted,figcaption{font-size:11px;
color:#60716f}

.pair{display:grid;
grid-template-columns:1fr 1fr;
gap:16px}
figure{margin:0;
border:1px solid #dbe4dc;
padding:14px;
border-radius:8px}
img{display:block;
width:100%;
max-width:400px;
margin:auto}
figcaption{padding-top:10px}

.scroll{overflow:auto}
table{border-collapse:collapse;
width:100%;
font-size:11px}
th,td{text-align:left;
vertical-align:top;
border-bottom:1px solid #dbe4dc;
padding:9px;
overflow-wrap:anywhere}
th{background:#f4f7f2}
td{font-variant-numeric:tabular-nums}

details{margin:15px 0}
summary{cursor:pointer;
font-size:12px;
color:#60716f}
pre{white-space:pre-wrap;
overflow-wrap:anywhere;
font:11px/1.7 monospace;
background:#f5f7f3;
padding:12px}
.missing{padding:24px;
border:1px dashed #dbe4dc}

@media(max-width:650px){main{padding:12px}
.pair{grid-template-columns:1fr}
header,section{padding:16px}
h1{font-size:26px}
}
@media print{body{background:white}
main{padding:0}
section{break-inside:avoid}
.pair{grid-template-columns:1fr 1fr}
details{display:block}
}
"""
    document = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>DB QE · CCM Macbeth 재실행 보고서</title><style>"
        + css
        + "</style></head><body><main>"
        + "".join(parts)
        + "</main></body></html>"
    )
    return document.encode("utf-8")
