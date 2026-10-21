"""Standalone report separating spectral QE, pre-CCM RAW and ISP color evidence.

This renderer consumes a preserved record only. It never fits, changes a profile,
loads an external image, or upgrades numerical agreement to physical validation.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
from copy import deepcopy
from typing import Any

LABELS = {
    "pass": "수치 기준 충족",
    "fail": "수치 기준 미충족",
    "inconclusive": "판정 보류",
    "not_evaluated": "미평가",
    "unsupported": "지원 범위 밖",
    "not_established": "물리 검증 미확립",
    "synthetic": "합성 입력",
    "measured": "실측 입력",
    "mixed": "혼합 입력",
    "absolute_qe": "절대 QE",
    "relative_spectral_response": "상대 분광 응답",
    "none": "없음",
    "per_channel_max": "채널별 최댓값 정규화",
}
METRICS = {
    "rmse": "RMSE (선언한 선형 색 공간)",
    "channel_rmse": "채널별 RMSE",
    "max_abs_error": "최대 절대 오차",
    "bias": "채널별 평균 편차",
    "delta_e00_mean": "평균 ΔE00",
    "delta_e00_max": "최대 ΔE00",
    "delta_e00_status": "ΔE00 제공 상태",
    "overall_rmse_dn": "전체 RAW RMSE (DN)",
    "max_abs_dn": "최대 RAW 절대 오차 (DN)",
    "ratio_max_abs": "R/G · B/G 비율의 최대 절대 오차",
    "ratio_unavailable_patches": "채널 비율 미평가 패치",
}
LIMITATIONS = [
    "QE는 파장별 교정광량과 센서 신호에서 구합니다. "
    "색상 차트의 CCM으로 파장별 QE를 식별하지 않습니다.",
    "RAW는 CCM 이전의 센서 신호입니다. CCM으로 출력 색상을 보정해도 RAW 오차는 없어지지 않습니다.",
    "상대 QE에는 절대 광량 응답 크기가 없으므로 절대 광전자 수나 RAW DN을 예측할 수 없습니다.",
    "합성 예제는 계산 경로의 구현 검증이며 실제 카메라 실측 검증이 아닙니다.",
    "수치 허용오차 충족과 물리 검증 완료는 별개입니다. "
    "보고서는 물리 검증 상태를 승격하지 않습니다.",
]


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _label(value: Any) -> str:
    return LABELS.get(str(value), str(value)) if value is not None else "미기록"


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(text) > 1400:
            checksum = hashlib.sha256(text.encode()).hexdigest()
            return (
                f"상세 값은 결과 JSON에 보존 · {len(text.encode())} bytes · "
                f"표시값 SHA256 {checksum}"
            )
        return text
    return _label(value)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return '<p class="muted">해당 수치가 제공되지 않았습니다.</p>'
    return (
        '<div class="scroll"><table><thead><tr>'
        + "".join("<th>" + _escape(h) + "</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join("<td>" + _escape(_display(v)) + "</td>" for v in row) + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def _status(value: Any) -> str:
    style = value if value in {"pass", "fail", "inconclusive"} else "neutral"
    return f'<span class="status {style}">{_escape(_label(value))}</span>'


def _finite(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _spectral_plot(profile: dict) -> str:
    wave = profile.get("wavelength_nm", [])
    values = profile.get("qe", profile.get("relative_spectral_response", []))
    channels = profile.get("channels", ["R", "G", "B"])
    if (
        not isinstance(wave, list)
        or not 2 <= len(wave) <= 4096
        or not all(_finite(v) for v in wave)
        or not isinstance(values, list)
        or len(values) != len(wave)
        or not isinstance(channels, list)
        or len(channels) != 3
        or any(not isinstance(row, list) or len(row) != 3 for row in values)
        or not all(_finite(v) for row in values for v in row)
    ):
        return '<p class="muted">유효한 파장별 R/G/B 곡선이 제공되지 않았습니다.</p>'
    xmin, xmax = min(wave), max(wave)
    ymin, ymax = (
        min(0.0, *(v for row in values for v in row)),
        max(v for row in values for v in row),
    )
    if xmax <= xmin:
        return '<p class="muted">파장 범위가 없어 곡선을 그리지 않았습니다.</p>'
    span = ymax - ymin if ymax > ymin else 1.0
    indices = sorted(set([*range(0, len(wave), max(1, math.ceil(len(wave) / 512))), len(wave) - 1]))
    ylabel = (
        "상대 응답"
        if profile.get("response_kind") == "relative_spectral_response"
        else "QE (e⁻ / photon)"
    )
    result = [
        '<svg viewBox="0 0 720 280" role="img" aria-label="파장별 R/G/B 응답">',
        '<path d="M64 25 V225 H695" fill="none" stroke="#9cafaa"/>',
        '<text x="65" y="16">' + _escape(ylabel) + "</text>",
    ]
    for step in range(5):
        y = 25 + step * 50
        result.append(
            f'<text x="56" y="{y + 4}" text-anchor="end">{ymax - step * span / 4:.3g}</text>'
        )
    colors = ("#bf5141", "#248760", "#396eb5")
    for channel, color in enumerate(colors):
        points = " ".join(
            f"{64 + (wave[i] - xmin) / (xmax - xmin) * 631:.3f},"
            f"{225 - (values[i][channel] - ymin) / span * 200:.3f}"
            for i in indices
        )
        result.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.3"/>'
        )
    result.extend(
        [
            f'<text x="64" y="246">{xmin:g}</text>',
            f'<text x="695" y="246" text-anchor="end">{xmax:g}</text>',
            '<text x="380" y="271" text-anchor="middle">파장 (nm)</text></svg>',
            '<p class="legend">'
            + "".join(
                f'<span style="color:{color}">━ {_escape(channel)}</span>'
                for channel, color in zip(channels, colors, strict=True)
            )
            + "</p>",
        ]
    )
    return "".join(result)


def _metrics(before: dict | None, after: dict | None, *, raw: bool = False) -> str:
    before, after = before or {}, after or {}
    keys = [key for key in METRICS if key in before or key in after]
    rows = [[METRICS[key], before.get(key), after.get(key)] for key in keys]
    if raw:
        rows = [
            row
            for row, key in zip(rows, keys, strict=True)
            if key not in {"rmse", "delta_e00_mean", "delta_e00_max", "delta_e00_status"}
        ]
    return _table(["지표", "기준", "후보"], rows)


def _raw_residual_plot(candidate: dict) -> str:
    values = candidate.get("residual_dn", [])
    if (
        not isinstance(values, list)
        or not 2 <= len(values) <= 4096
        or any(not isinstance(row, list) or len(row) != 3 for row in values)
        or not all(_finite(v) for row in values for v in row)
    ):
        return ""
    lo = min(0.0, *(v for row in values for v in row))
    hi = max(0.0, *(v for row in values for v in row))
    span = hi - lo if hi > lo else 1.0
    output = [
        '<svg viewBox="0 0 720 250" role="img" aria-label="패치별 후보 RAW 잔차">',
        '<path d="M64 25 V200 H695" fill="none" stroke="#9cafaa"/>',
        '<text x="64" y="16">후보 예측 − 측정 (DN)</text>',
    ]
    for step in range(5):
        output.append(
            f'<text x="56" y="{29 + step * 43.75}" text-anchor="end">'
            f"{hi - step * span / 4:.3g}</text>"
        )
    for channel, color in enumerate(("#bf5141", "#248760", "#396eb5")):
        points = " ".join(
            f"{64 + i / (len(values) - 1) * 631:.3f},{200 - (row[channel] - lo) / span * 175:.3f}"
            for i, row in enumerate(values)
        )
        output.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>'
        )
    output.extend(
        [
            '<text x="64" y="222">1</text>',
            f'<text x="695" y="222" text-anchor="end">{len(values)}</text>',
            '<text x="380" y="242" text-anchor="middle">'
            "패치 번호 · R 빨강 / G 초록 / B 파랑</text></svg>",
        ]
    )
    return "".join(output)


def _qe_section(record: dict) -> str:
    qe = record.get("qe", {})
    profile = qe.get("profile", {})
    if not profile:
        return "<section><h2>1. QE · 파장별 센서 응답</h2><p>QE 프로파일이 없습니다.</p></section>"
    relative = profile.get("response_kind") == "relative_spectral_response"
    note = (
        "상대 분광 응답입니다. 정규화된 곡선에는 절대 감도가 없으며 "
        "절대 RAW 예측에 사용할 수 없습니다."
        if relative
        else "파장별 교정광량을 센서면에 입사하고 채널별 전자 신호와 비교해 얻은 절대 QE입니다."
    )
    channels = profile.get("channels", ["R", "G", "B"])
    values = profile.get("qe", profile.get("relative_spectral_response", []))
    rows = [
        [wave, *(values[i] if i < len(values) and isinstance(values[i], list) else [])]
        for i, wave in enumerate(profile.get("wavelength_nm", []))
    ]
    return (
        '<section><p class="eyebrow">01 / SPECTRAL SENSOR RESPONSE</p>'
        "<h2>QE · 파장별 센서 응답</h2>"
        + _status(profile.get("response_kind"))
        + '<p class="notice">'
        + _escape(note)
        + "</p>"
        + _spectral_plot(profile)
        + _table(["파장 (nm)", *channels], rows)
        + _table(
            ["조건", "보존값"],
            [
                ["광량 기준면", profile.get("reference_plane")],
                ["측정 조건", profile.get("conditions")],
                ["응답 정규화", profile.get("normalization")],
                ["광량 교정 근거", profile.get("radiometry")],
                ["포함 효과", profile.get("included_effects")],
                ["QE Profile SHA256", profile.get("profile_sha256")],
            ],
        )
        + "</section>"
    )


def _raw_section(record: dict) -> str:
    raw = record.get("raw_validation", {})
    candidate = raw.get("candidate", raw if raw.get("status") else {})
    baseline = raw.get("baseline") or {}
    sections = [
        '<section><p class="eyebrow">02 / BEFORE ISP CCM</p><h2>RAW · 센서 모델 검증</h2>',
        _status(candidate.get("status")),
        '<p class="notice">RAW는 CCM 이전의 센서 신호입니다. '
        "뒤 단계의 CCM으로 RAW 오차가 사라지지 않습니다.</p>",
    ]
    if candidate.get("reason"):
        sections.append('<p class="notice">' + _escape(candidate["reason"]) + "</p>")
    metrics, previous = candidate.get("metrics", {}), baseline.get("metrics", {})
    channels = candidate.get("channels", [])

    def at(value, index):
        return value[index] if isinstance(value, list) and index < len(value) else None

    sections.append(
        _table(
            ["채널", "기준 RMSE (DN)", "후보 RMSE (DN)", "후보 평균 편차 (DN)"],
            [
                [
                    name,
                    at(previous.get("channel_rmse_dn"), i),
                    at(metrics.get("channel_rmse_dn"), i),
                    at(metrics.get("channel_bias_dn"), i),
                ]
                for i, name in enumerate(channels)
            ],
        )
    )
    sections.append(_metrics(previous, metrics, raw=True))
    sections.append(_raw_residual_plot(candidate))
    sections.append(
        _table(
            ["패치", "측정 RAW (DN)", "기준 예측 (DN)", "후보 예측 (DN)", "후보 잔차 (DN)"],
            [
                [
                    patch,
                    at(candidate.get("measured_raw_rgb_dn"), i),
                    at(baseline.get("predicted_raw_rgb_dn"), i),
                    at(candidate.get("predicted_raw_rgb_dn"), i),
                    at(candidate.get("residual_dn"), i),
                ]
                for i, patch in enumerate(candidate.get("patch_ids", []))
            ],
        )
    )
    sections.append(
        _table(
            ["검증 기준", "관측값", "허용값", "판정"],
            [
                [check.get("id"), check.get("value"), check.get("limit"), check.get("status")]
                for check in candidate.get("checks", [])
            ],
        )
    )
    sections.append(
        _table(
            ["검증 동작", "보존값"],
            [
                ["추가 정규화", candidate.get("normalization")],
                ["QE 고정 여부", candidate.get("qe_unchanged")],
                ["사용 QE SHA256", candidate.get("qe_profile_sha256_before")],
                ["검증 입력 SHA256", candidate.get("payload_sha256")],
            ],
        )
    )
    return "".join(sections) + "</section>"


def _ccm_section(record: dict) -> str:
    ccm = record.get("ccm", {})
    profile = ccm.get("profile", {})
    validation = ccm.get("validation", {})
    comparison = validation.get("comparison", {})
    sections = [
        '<section><p class="eyebrow">03 / ISP COLOR TRANSFORM</p><h2>CCM · 출력 색상 보정</h2>',
        _status(validation.get("status", ccm.get("status"))),
        "<p>QE와 RAW 모델을 고정한 뒤 별도 색상 차트로 ISP 색 행렬을 보정합니다. "
        "학습과 독립 검증을 구분합니다.</p>",
    ]
    if not profile:
        sections.append(
            '<p class="notice">'
            + _escape(
                ccm.get("reason")
                or validation.get("reason")
                or "CCM 프로파일이 제공되지 않았습니다."
            )
            + "</p></section>"
        )
        return "".join(sections)
    if profile.get("target_space") == "camera_linear_rgb":
        sections.append(
            '<p class="notice">camera_linear_rgb는 색도 기준 공간이 아니므로 '
            "ΔE를 계산하지 않습니다. 아래 RMSE는 선언한 선형 RGB 공간의 오차입니다.</p>"
        )
    training = ccm.get("training", ccm.get("comparison", {}))
    if "comparison" in training:
        training = training["comparison"]
    sections.append(
        "<h3>학습 차트</h3>"
        + _metrics(
            training.get("metrics", {}).get("baseline"),
            training.get("metrics", {}).get("candidate"),
        )
    )
    sections.append(
        "<h3>독립 검증 차트</h3>"
        + _metrics(
            comparison.get("metrics", {}).get("baseline"),
            comparison.get("metrics", {}).get("candidate"),
        )
    )
    targets = comparison.get("target_rgb", [])
    sections.append(
        _table(
            ["패치", "기준 출력", "후보 출력", "목표 출력"],
            [
                [
                    i + 1,
                    comparison.get("baseline", [])[i]
                    if i < len(comparison.get("baseline", []))
                    else None,
                    comparison.get("candidate", [])[i]
                    if i < len(comparison.get("candidate", []))
                    else None,
                    row,
                ]
                for i, row in enumerate(targets)
            ],
        )
    )
    outputs = ["X", "Y", "Z"] if profile.get("target_space") == "xyz" else ["R", "G", "B"]
    for key, title in (("baseline_matrix", "기준 CCM"), ("matrix", "새 CCM")):
        sections.append(
            "<h3>"
            + title
            + "</h3>"
            + _table(
                ["입력 → 출력", *outputs],
                [[["R", "G", "B"][i], *row] for i, row in enumerate(profile.get(key, [])[:3])],
            )
        )
    sections.append(
        _table(
            ["적용 조건", "보존값"],
            [
                ["행렬 적용식", profile.get("application")],
                ["목표 공간", profile.get("target_space")],
                ["고정 white balance", profile.get("white_balance")],
                ["고정 노출 배율", profile.get("exposure_scale")],
                ["독립 검증 재피팅", validation.get("refit_performed")],
                ["CCM Profile SHA256", profile.get("profile_sha256")],
            ],
        )
    )
    sections.append(
        _table(
            ["독립 검증 기준", "관측값", "허용값", "판정"],
            [
                [check.get("metric"), check.get("value"), check.get("limit"), check.get("status")]
                for check in validation.get("checks", [])
            ],
        )
    )
    return "".join(sections) + "</section>"


def render_color_report(record: dict[str, Any]) -> bytes:
    """Render finite preserved evidence as Korean HTML without remote dependencies."""
    if not isinstance(record, dict) or not record.get("id"):
        raise ValueError("A preserved color-calibration record with an ID is required")
    data = json.loads(json.dumps(deepcopy(record), ensure_ascii=False, allow_nan=False))
    evidence = data.get("evidence", {})
    inputs = evidence.get("inputs", {})
    sections = [
        '<header><p class="eyebrow">CAMERAE2E · SPECTRAL / RAW / ISP EVIDENCE</p>',
        "<h1>QE · RAW · CCM 분리 보정 보고서</h1>",
        "<p>" + _escape(data.get("name", data["id"])) + "</p>",
        _status(data.get("source_kind")),
        _status("not_established"),
        '<p class="notice">세 단계의 판정을 각각 확인합니다. ISP 색 보정의 성공을 '
        "RAW 모델이나 물리 카메라 검증 성공으로 해석하지 않습니다.</p>",
    ]
    if data.get("source_kind") == "synthetic":
        sections.append(
            '<p class="notice">합성 입력으로 계산 경로를 확인한 결과입니다. '
            "실제 카메라 실측 검증이 아닙니다.</p>"
        )
    sections.append(
        _table(
            ["작업 식별", "보존값"],
            [
                ["결과 ID", data["id"]],
                ["연결 캠페인", data.get("campaign_id")],
                ["생성 시각", data.get("created_at")],
                ["입력 SHA256", data.get("input_sha256")],
            ],
        )
        + "</header>"
    )
    sections.extend([_qe_section(data), _raw_section(data), _ccm_section(data)])
    limitations = list(LIMITATIONS)
    for container in [
        data,
        data.get("qe", {}).get("profile", {}),
        data.get("raw_validation", {}).get("candidate", {}),
        data.get("ccm", {}),
    ]:
        limitations.extend(
            item for item in container.get("limitations", []) if isinstance(item, str)
        )
    sections.append(
        "<section><h2>4. 적용 범위와 미평가 항목</h2><ul>"
        + "".join("<li>" + _escape(item) + "</li>" for item in dict.fromkeys(limitations))
        + "</ul></section>"
    )
    provenance_rows = []
    for stage in ("qe_scan", "raw_validation", "ccm_training", "ccm_validation"):
        source = inputs.get(stage) or {}
        provenance_rows.append(
            [stage, source.get("source_kind"), source.get("provenance"), source.get("conditions")]
        )
    sections.append(
        "<section><h2>5. 원본·출처·재현 근거</h2>"
        + _table(
            ["원본 RAW 역할", "패치 수", "프레임 수", "CFA / bit", "원본 SHA256"],
            [
                [
                    role,
                    original.get("patch_count"),
                    original.get("frame_count"),
                    [original.get("cfa"), original.get("bit_depth")],
                    original.get("sha256"),
                ]
                for role, original in evidence.get("raw_originals", {}).items()
            ],
        )
        + _table(["입력 단계", "종류", "취득 근거", "조건"], provenance_rows)
        + _table(
            ["근거", "보존값"],
            [
                ["원본 입력 SHA256", data.get("input_sha256")],
                ["원본 RAW 관측 근거", evidence.get("raw_originals")],
                ["계산 환경", evidence.get("source_environment")],
            ],
        )
        + "<p>전체 원본 입력·프로파일·수치 결과·파일 해시는 "
        "함께 제공되는 JSON과 근거 패키지에 보존됩니다.</p></section>"
    )
    css = """
body{margin:0;
background:#f2f5f1;
color:#183b3f;
font:14px/1.65 system-ui,-apple-system,sans-serif}
main{max-width:1150px;
margin:auto;
padding:34px 24px}
header,section{background:#fff;
border:1px solid #dbe4dc;
border-radius:12px;
padding:24px;
margin-bottom:20px}
h1{font-size:29px;
letter-spacing:-1px}
h2{font-size:22px}
h3{font-size:16px;
margin-top:24px}
.eyebrow{font-size:10px;
letter-spacing:1.8px;
color:#087e81;
font-weight:700}
.notice{padding:12px 15px;
border-left:3px solid #a6752c;
background:#f9f5ec;
color:#624e2b}
.status{display:inline-block;
padding:4px 9px;
border-radius:16px;
font-size:11px;
margin-right:7px;
background:#edf4f0}
.pass{color:#086848;
background:#dff4e9}
.fail{color:#a33e30;
background:#fdebe5}
.inconclusive{color:#825c20;
background:#fff1d5}
.muted{color:#637572;
font-size:12px}
.scroll{overflow-x:auto}
table{width:100%;
border-collapse:collapse;
font-size:12px}
th,td{padding:9px;
border-bottom:1px solid #e0e7e1;
text-align:left;
vertical-align:top;
overflow-wrap:anywhere;
font-variant-numeric:tabular-nums}
th{background:#f3f7f2;
color:#526c66}
svg{width:100%;
max-width:850px;
display:block;
background:#f8fbf7;
margin:14px 0;
border-radius:8px}
svg text{font:11px sans-serif;
fill:#5c716c}
.legend{display:flex;
gap:24px;
font-size:12px}
@media print{body{background:white}
main{padding:0}
header,section{border:0;
padding:10px}
h2,h3{break-after:avoid}
tr{break-inside:avoid}
svg{max-height:260px}
}
@media(max-width:600px){main{padding:15px}
header,section{padding:16px}
h1{font-size:24px}
}
"""
    document = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; base-uri 'none'\">"
        "<title>QE · RAW · CCM 분리 보정 보고서</title><style>"
        + css
        + "</style></head><body><main>"
        + "".join(sections)
        + "</main></body></html>"
    )
    return document.encode("utf-8")
