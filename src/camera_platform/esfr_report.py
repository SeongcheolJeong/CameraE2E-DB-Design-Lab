"""Portable measured/simulated E-SFR comparison reports with explicit scope."""

from __future__ import annotations

# Inline HTML and CSS templates retain readable markup rather than artificial text breaks.
# ruff: noqa: E501
import base64
import csv
import html
import json
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from PIL import Image, ImageDraw


def _evidence_filename(summary, key):
    """Only the declared standalone/supplementary evidence layouts are linkable."""
    supported = {
        "request_file": ("request.json", "../request.json"),
        "measurement_details_file": ("measurement-details.json", "full-result.json.gz"),
    }
    allowed = supported[key]
    value = summary.get(key, allowed[0])
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("Unsupported E-SFR report evidence filename")
    return value


def _measurement_download(summary, label):
    name = _evidence_filename(summary, "measurement_details_file")
    encoding = "JSON.GZ · 무손실 압축" if name.endswith(".gz") else "JSON"
    return '<a href="./' + _e(name) + '" download>' + _e(label) + " (" + encoding + ")</a>"


def overviews(captures, rois, directory):
    from .esfr_io import RawFrame, read_isp

    result = []
    seen = set()
    for capture, raw_path, isp_path in captures:
        role = capture["metadata"]["role"]
        if role in seen:
            continue
        seen.add(role)
        with RawFrame(raw_path, capture["metadata"]) as raw:
            codes = raw.read().astype(np.float32)
        meta = capture["metadata"]["raw_format"]
        black = float(np.min(meta["black_level_dn"]))
        white = float(np.max(meta["white_level_dn"]))
        image = np.uint8(np.clip((codes - black) / (white - black), 0, 1) ** (1 / 2.2) * 255)
        rgb, info = read_isp(isp_path, capture["metadata"]["isp"])
        views = {
            "raw": Image.fromarray(image).convert("RGB"),
            "isp": Image.fromarray(
                np.uint8(np.clip(rgb, 0, 1) ** (1 / 2.2 if info["linear"] else 1) * 255)
            ),
        }
        paths = {}
        for domain, im in views.items():
            width, height = im.size
            im.thumbnail((1500, 900))
            sx, sy = im.width / width, im.height / height
            draw = ImageDraw.Draw(im)
            if (
                domain == "raw"
                or capture["metadata"]["isp"]["coordinate_mapping"] == "same_native_raster"
            ):
                for roi in rois:
                    y, x, h, w = roi["box"]
                    color = {
                        "edge": "#54e1b8",
                        "gray": "#ffcf63",
                        "color": "#ff7fae",
                        "wedge": "#8cafff",
                    }[roi["kind"]]
                    draw.rectangle(
                        (int(x * sx), int(y * sy), int((x + w) * sx), int((y + h) * sy)),
                        outline=color,
                        width=2,
                    )
                    draw.text((int(x * sx), max(0, int(y * sy) - 12)), roi["id"], fill=color)
            name = f"overview-{role}-{domain}.png"
            im.save(Path(directory) / name)
            paths[domain] = name
        result.append({"role": role, "capture_id": capture["id"], "images": paths})
    return result


def _e(value):
    return html.escape(str(value))


def _n(value):
    return f"{value:.6g}" if isinstance(value, (int, float)) else "—"


def _image(path, caption):
    path = Path(path)
    mime = {".png": "image/png", ".svg": "image/svg+xml"}.get(path.suffix.lower())
    if not path.is_file() or not mime:
        return ""
    uri = "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode()
    return f'<figure><img src="{uri}" alt="{_e(caption)}"><figcaption>{_e(caption)}</figcaption></figure>'


def _details(label, value):
    return (
        "<details><summary>"
        + _e(label)
        + "</summary><pre>"
        + _e(json.dumps(value, ensure_ascii=False, indent=2))
        + "</pre></details>"
    )


def _table(headers, rows):
    return (
        '<div class="scroll"><table><thead><tr>'
        + "".join("<th>" + _e(x) + "</th>" for x in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join("<td>" + _e(x) + "</td>" for x in row) + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


def _official_source(value):
    """Only link the cited publisher pages; render all other metadata as text."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.scheme == "https" and any(
            host == domain or host.endswith("." + domain) for domain in ("imatest.com", "iso.org")
        ):
            return value
    except ValueError:
        pass
    return None


def _chart_section(summary, directory):
    """Embed the exact blueprint shipped with this run, not today's server template."""
    request_file = _evidence_filename(summary, "request_file")
    request_href = request_file if request_file.startswith("../") else "./" + request_file
    profile = summary.get("chart_profile") or {}
    if not isinstance(profile, dict):
        profile = {}
    geometry = summary.get("chart_geometry") or profile.get("chart_geometry") or {}
    if not isinstance(geometry, dict):
        geometry = {}
    if not geometry and not (directory / "chart.svg").is_file():
        return ""
    if not geometry:
        geometry = profile
    rows = []
    counts = geometry.get("counts", geometry.get("feature_counts", {}))
    if not isinstance(counts, dict):
        counts = {}
    for keys, label, expected in (
        (("stars", "four_cycle_stars", "slanted_stars"), "4-cycle slanted stars", 13),
        (("side_squares", "squares", "focus_squares"), "측면 focus squares", 2),
        (("gray_patches", "gray", "grayscale_patches"), "계조 패치", 20),
        (("color_patches", "color"), "색 패치", 18),
        (
            ("wedge_groups", "wedges", "visible_wedge_groups", "logarithmic_wedges"),
            "그림의 logarithmic wedge groups",
            "제조사 12 pairs와 별도 단위",
        ),
        (("fiducials", "registration_fiducials"), "정합 fiducials", 4),
    ):
        value = next((counts[key] for key in keys if key in counts), None)
        rows.append([label, expected, "미기록" if value is None else value])
    parts = [
        '<section class="card" id="chart-blueprint"><h2>차트 정의 · 형상 · 근거</h2>',
        "<p><b>"
        + _e(
            geometry.get(
                "name",
                geometry.get("title", geometry.get("id", profile.get("id", "Extended E-SFR 2023"))),
            )
        )
        + "</b></p>",
        '<p>이 실행에 고정된 차트 정의와 원본 참고 이미지를 보존합니다. 참고 이미지의 RGB를 인쇄물 반사율·차트 MTF·실측 RAW로 사용하지 않습니다. 합성 edge의 선형 명암비는 공칭 4:1이며 실제 차트와 조명의 값은 별도 측정 대상입니다.</p><div class="grid two">',
        _image(directory / "reference.png", "보유 차트의 참고 이미지 · 배치 확인용"),
        _image(directory / "chart.svg", "해석적으로 생성한 차트 · 실행 시 고정된 형상"),
        "</div>",
    ]
    overlay = _image(
        directory / "chart-rois.svg",
        "차트 좌표의 ROI 설계 · 분석은 취득 영상의 원본 센서 좌표에서 수행",
    )
    if overlay:
        parts.append(
            "<details><summary>ROI 배치와 단일 edge 영역</summary>" + overlay + "</details>"
        )
    parts.append(_table(["패턴", "Extended 2023 참고 구성", "실행에 기록된 수"], rows))
    parts.append(
        "<p>Wedge는 그림에 배치된 그룹 수로 셉니다. 제조사의 12 pairs 표기와 개별 그룹의 대응은 확정하지 않았으며, 같은 단위의 개수로 취급하지 않습니다.</p>"
    )
    parts.append(
        '<p class="note"><b>차트 버전과 분석기 버전</b><br>2023 차트 형상을 사용합니다. SFR은 native straight-edge engineering estimator이며 ISO 알고리즘 적합성은 미확립입니다. 형상 재현·합성 응답 검증은 보유 인쇄물의 품질 교정이나 실제 카메라 검증을 대신하지 않습니다.</p>'
    )
    sources = geometry.get("sources", [])
    if isinstance(sources, list):
        links = []
        for item in sources:
            if isinstance(item, str):
                url, label = _official_source(item), item
            elif isinstance(item, dict):
                url = _official_source(item.get("url", item.get("href")))
                label = item.get(
                    "title", item.get("label", item.get("name", item.get("url", "참고 문서")))
                )
            else:
                continue
            if url:
                links.append(
                    '<a href="' + _e(url) + '" target="_blank" rel="noopener">' + _e(label) + "</a>"
                )
        if links:
            parts.append("<p>근거 문서: " + " · ".join(links) + "</p>")
    parts.append(
        _details("차트 형상·출처·구현 범위 기록", geometry)
        + '<p><a href="' + _e(request_href)
        + '" download>실행 입력과 차트 프로파일 (JSON)</a></p></section>'
    )
    return "".join(parts)


def _position_maps(summary, directory, plt):
    """Show observed native-coordinate points only; never interpolate a field."""
    observation = next(
        (item for item in summary.get("observations", []) if item.get("role") == "validation"), None
    )
    if observation is None:
        return ""
    geometry = summary.get("candidate_profile", {}).get("geometry", {})
    shape, origin = geometry.get("full_sensor_shape"), geometry.get("crop_origin", [0, 0])
    if not isinstance(shape, (list, tuple)) or len(shape) != 2:
        return ""
    rois = [item for item in observation.get("rois", []) if item.get("kind") == "edge"]
    if not rois:
        return ""
    comparisons = {
        item["roi_id"]: item
        for item in summary.get("comparisons", [])
        if item.get("capture_id") == observation.get("capture_id")
    }
    fig, axes = plt.subplots(2, 2, figsize=(11, 7), layout="constrained")
    for ax, (domain, channel, quantity) in zip(
        axes.flat,
        (
            ("raw", "Gr", "mtf50"),
            ("isp", "Y", "mtf50"),
            ("raw", "Gr", "after_rmse"),
            ("isp", "Y", "after_rmse"),
        ),
        strict=True,
    ):
        valid, missing = [], []
        for roi in rois:
            row, col, height, width = roi["box"]
            x, y = origin[1] + col + (width - 1) / 2, origin[0] + row + (height - 1) / 2
            if quantity == "mtf50":
                measurement = roi.get(domain, {}).get(channel, {})
                value = measurement.get("mtf50") if measurement.get("status") == "ok" else None
            else:
                value = comparisons.get(roi["id"], {}).get(domain, {}).get(quantity)
            if isinstance(value, (int, float)) and np.isfinite(value):
                valid.append((x, y, value))
            else:
                missing.append((x, y))
            ax.annotate(
                roi["id"],
                (x, y),
                xytext=(3, 5),
                textcoords="offset points",
                fontsize=6,
                color="#334155",
            )
        if valid:
            points = np.asarray(valid)
            if quantity == "mtf50":
                maximum = 0.25 if domain == "raw" else 0.5
            else:
                threshold = (
                    summary.get("validation", {})
                    .get("thresholds", {})
                    .get(f"{domain}_sfr_rmse", 0.0)
                )
                maximum = max(float(threshold or 0.0), float(np.max(points[:, 2])), 1e-6)
            artist = ax.scatter(
                points[:, 0],
                points[:, 1],
                c=points[:, 2],
                s=65,
                cmap="viridis",
                vmin=0,
                vmax=maximum,
                edgecolors="#152337",
                linewidths=0.5,
            )
            fig.colorbar(
                artist,
                ax=ax,
                label="cycles / native pixel" if quantity == "mtf50" else "SFR RMSE",
                shrink=0.85,
            )
        if missing:
            points = np.asarray(missing)
            ax.scatter(
                points[:, 0], points[:, 1], marker="x", c="#9ca3af", s=45, label="unavailable"
            )
            ax.legend(fontsize=7)
        title = (
            f"{domain.upper()} {channel} measured MTF50"
            if quantity == "mtf50"
            else f"{domain.upper()} after-fit error"
        )
        if domain == "isp" and not observation.get("isp_info", {}).get("linear", False):
            title += " (code values)"
        ax.set(
            title=title,
            xlabel="native sensor x (pixel)",
            ylabel="native sensor y (pixel)",
            xlim=(-0.5, shape[1] - 0.5),
            ylim=(shape[0] - 0.5, -0.5),
        )
        ax.set_aspect("equal", adjustable="box")
        ax.grid(alpha=0.15)
    name = "native-position-metrics.png"
    fig.savefig(directory / name, dpi=140)
    plt.close(fig)
    return (
        '<section class="card"><h2>센서 좌표별 측정과 잔차</h2><p>첫 검증 촬영 '
        + _e(observation["capture_id"])
        + "의 ROI 중심점만 표시합니다. 측정하지 않은 영역을 보간하지 않습니다. "
        "각 점은 해당 edge 방향의 응답이며, 방향을 평균한 렌즈 MTF가 아닙니다. "
        "회색 ×는 측정 불가 항목입니다. RAW Gr의 분석 대역은 0–0.25 cycles/native pixel입니다.</p>"
        + _image(
            directory / name,
            "네 패널의 색 눈금은 각각 독립적입니다. 입력 MTF50과 보정 후 곡선 오차를 구분해 읽습니다.",
        )
        + "</section>"
    )


def _patch_tables(summary):
    patches = summary.get("patch_summary", summary.get("patches", []))
    patch_list = patches if isinstance(patches, list) else []
    first = next((item for item in patch_list if item.get("role") == "validation"), None)
    parts = [
        '<section class="card"><h2>패치·반복성·취득 품질</h2><p>색 패치는 채널 평균과 상대 변화만 평가합니다. 실제 기준값과 광원 조건이 확인되기 전에는 ΔE·절대 색 정확도·CCM 보정을 출력하지 않습니다. 회색 패치의 공간 표준편차와 반복 촬영의 시간 분산은 별개입니다.</p>'
    ]
    if first:
        parts.append(
            "<p>첫 검증 촬영 "
            + _e(first.get("capture_id"))
            + " · RAW는 black 차감 후 DN, ISP는 입력 전달함수에 따른 정규화 값입니다.</p>"
        )
        rows = []
        for roi in first.get("rois", []):
            raw, isp = roi.get("raw", {}), roi.get("isp", {})
            rows.append(
                [
                    roi.get("id"),
                    roi.get("kind"),
                    " / ".join(
                        _n(raw.get(channel, {}).get("mean")) for channel in ("R", "Gr", "Gb", "B")
                    ),
                    _n(raw.get("Gr", {}).get("spatial_std")),
                    " / ".join(_n(isp.get(channel, {}).get("mean")) for channel in ("R", "G", "B")),
                ]
            )
        parts.append(
            _table(
                [
                    "ROI",
                    "종류",
                    "RAW 평균 R / Gr / Gb / B (DN)",
                    "RAW Gr 공간 σ (DN)",
                    "ISP 평균 R / G / B",
                ],
                rows,
            )
        )
    else:
        parts.append('<p class="muted">검증 촬영의 패치 측정값이 없습니다.</p>')
    parts.append(
        "<p>" + _measurement_download(summary, "모든 촬영·채널의 패치·측정 관측값과 품질 사유")
        + " · 상세 원자료는 evidence ZIP에도 포함됩니다. 이 보고서의 그림은 HTML에 내장되어 있습니다.</p>"
    )
    parts.append(
        '</section><section class="card"><h2>반복 촬영 시간 노이즈·SNR</h2><p>표는 RAW Gr 채널만 요약합니다. 동일 픽셀의 반복 촬영 시간 분산에는 조명 변화도 포함되며, Dark·PTC 파라미터 추정값이 아닙니다.</p>'
    )
    temporal = summary.get("temporal_patch_noise", [])
    rows = [
        [
            item.get("role"),
            item.get("roi_id"),
            item.get("frame_count"),
            _n(item.get("mean_signal_dn")),
            _n(item.get("temporal_variance_dn2")),
            _n(item.get("temporal_snr_db")),
            item.get("status"),
        ]
        for item in temporal
        if item.get("channel") == "Gr"
    ]
    if rows:
        parts.append(
            _table(
                [
                    "역할",
                    "ROI",
                    "프레임 수",
                    "평균 신호 (DN)",
                    "시간 분산 (DN²)",
                    "시간 SNR (dB)",
                    "상태",
                ],
                rows,
            )
        )
    else:
        parts.append('<p class="muted">반복 촬영 시간 노이즈 측정값이 없습니다.</p>')
    parts.append(_details("모든 채널의 반복성 측정 상세", temporal))
    parts.append("</section>")
    return "".join(parts)


def publish(summary, directory):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    directory = Path(directory)
    source = summary.get("source_context") or {}
    metrics = summary.get("metrics", {})
    comparisons = summary.get("comparisons", [])
    profile = summary.get("candidate_profile", {})
    measured = summary.get("source_kind") == "measured"
    validation = summary.get("validation", {})
    raw_only = validation.get("scope") == "raw_native_sfr_only"
    title = "실촬영 E-SFR 분석" if measured else "합성 E-SFR 기능 검증"
    css = """body{margin:0;background:#f1f4f8;color:#152337;font:15px/1.65 system-ui,sans-serif}main{max-width:1240px;margin:auto;padding:40px 24px}header{border-top:6px solid #2563eb;padding:24px 0}h1{font-size:34px;margin:.3em 0}h2{font-size:23px;margin-top:0}h3{font-size:17px}.eyebrow{letter-spacing:.12em;color:#436589;font-weight:700;font-size:12px}.card{background:white;padding:26px;border:1px solid #dce4ee;border-radius:12px;margin:20px 0}.note{background:#fff5dc;padding:16px;border-left:4px solid #d8901c}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.two{grid-template-columns:repeat(2,minmax(0,1fr))}table{width:100%;border-collapse:collapse;font-size:13px}th,td{border-bottom:1px solid #e4eaf2;padding:9px;text-align:left;vertical-align:top}th{background:#f5f8fc}figure{margin:8px 0}img{max-width:100%;height:auto;border-radius:6px}figcaption{color:#576a80;font-size:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f5f8fc;padding:12px}.muted{color:#597086}.tag{display:inline-block;background:#e8f0ff;color:#164aa0;padding:3px 10px;border-radius:20px}a{color:#2359ad}.number{font-size:24px;font-weight:700}.scroll{overflow:auto}@media(max-width:760px){.grid{grid-template-columns:1fr}main{padding:14px}h1{font-size:27px}}details{margin:12px 0}summary{cursor:pointer;color:#2359ad;font-weight:600}@media print{body{background:white}.card{break-inside:avoid}details:not([open])>*:not(summary){display:none}}"""
    parts = [
        f'<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)} · CameraE2E</title><style>{css}</style><main>',
        f'<header><div class="eyebrow">CAMERAE2E · CAMERA VALIDATION LAB</div><h1>{_e(title)}</h1><p>Extended E-SFR ISO 2023 · 원본 RAW / ISP · 유효 공간응답 보정</p><span class="tag">{_e(summary.get("run_id"))}</span></header>',
        '<div class="note"><b>결과의 범위</b><br>차트 지원과 ISO 알고리즘 인증은 별개입니다. 차트 MTF·색 기준값 미확인 상태에서는 차트와 카메라가 합쳐진 조건부 응답을 측정합니다. QE·Dark·PTC·순수 렌즈 PSF 및 절대 색 정확도를 식별한 결과가 아닙니다.</div>',
    ]
    if not measured:
        parts.append(
            '<p class="note">이 보고서는 독립적으로 생성한 합성 RAW·ISP 입력의 소프트웨어 검증입니다. 보유 차트의 실촬영이나 물리 카메라 검증이 아닙니다.</p>'
        )
    if summary.get("replay_of_applied_profile") and not summary.get("acquisition_contract"):
        parts.append(
            '<p class="note">이 실행은 적용된 고정 프로파일의 동작 확인입니다. 같은 저장 촬영을 다시 사용했으며 새로운 독립 실측 검증이 아닙니다.</p>'
        )
    parts.append(_chart_section(summary, directory))
    parts.append(
        '<section class="card"><h2>원본 차트와 측정 위치</h2><p>표시용 축소 영상입니다. 분석은 원본 해상도의 RAW·ISP에서 수행합니다.</p>'
    )
    for item in summary.get("overviews", []):
        parts.append(
            "<h3>"
            + _e(item["role"])
            + " · "
            + _e(item["capture_id"])
            + '</h3><div class="grid two">'
        )
        for domain in ("raw", "isp"):
            parts.append(
                _image(directory / item["images"][domain], domain.upper() + " · ROI overlay")
            )
        parts.append("</div>")
    parts.append("</section>")
    parts.append(
        '<section class="card"><h2>독립 검증 촬영의 보정 전후 오차</h2><div class="grid two">'
    )
    for key, label in [("raw_sfr_rmse", "RAW Gr SFR"), ("isp_sfr_rmse", "선형 ISP Y SFR")]:
        if raw_only and key == "isp_sfr_rmse":
            label += " · 진단 전용"
        m = metrics.get(key, {})
        b, a = m.get("before"), m.get("after")
        reduction = (
            (100 * (1 - a / b))
            if isinstance(a, (float, int)) and isinstance(b, (float, int)) and b > 0
            else None
        )
        parts.append(
            f'<div><h3>{label} 곡선 RMSE</h3><div class="number">{_n(b)} → {_n(a)}</div><p>개선율 {_n(reduction)}% · 유효 비교 {_e(m.get("count", "—"))}</p></div>'
        )
    parts.append(
        '</div><p class="muted">개발 자료의 피팅 오차와 분리한 값입니다. 평균 개선이 개별 주변부 실패를 대신하지 않습니다.</p>'
        + '<p data-acceptance-scope="' + _e(validation.get("scope", "unspecified")) + '">'
        + ("RAW Gr 단독 기준 판정" if raw_only else "검증 판정") + ': <b>'
        + _e(validation.get("status", "inconclusive"))
        + "</b></p>"
        + ('<p class="note"><b>ISP는 진단 전용이며 합격 판정에 포함되지 않습니다.</b><br>'
           '제조사 ISP와 고정 bilinear 참조 연산자의 일치가 확인되지 않았습니다. '
           'ISP 오차와 기준값 ' + _n(validation.get("thresholds", {}).get("isp_sfr_rmse"))
           + '는 참고용으로 보존합니다. RAW 기준 충족을 ISP 기준 충족 또는 카메라 인증으로 해석하지 않습니다.</p>'
           if raw_only else "")
        + _details("개별 검증 기준과 판정 상세", validation)
        + "</section>"
    )
    parts.append(
        '<section class="card"><h2>고정된 후보 프로파일</h2><p>개발 촬영에서 추정한 후보를 고정한 후 검증 촬영을 평가합니다. 기준 Gaussian은 명시된 비교 초기값이며, DB 렌즈에서 직접 계산했다고 표시하지 않습니다.</p><div class="scroll"><table><tr><th>위치</th><th>σx / σy / θ</th><th>피팅 세부</th></tr>'
    )
    for field, p in profile.get("fields", {}).items():
        parts.append(
            f"<tr><td>{_e(field)}</td><td>{_n(p.get('sigma_x_px'))} / {_n(p.get('sigma_y_px'))} / {_n(p.get('theta_deg'))}</td><td><details><summary>매개변수</summary><pre>{_e(json.dumps(p, ensure_ascii=False, indent=2))}</pre></details></td></tr>"
        )
    parts.append(
        "</table></div>"
        + _details("피팅 과정과 초기값 상세", summary.get("fit", {}))
        + "</section>"
    )
    parts.append(_position_maps(summary, directory, plt))
    csv_rows = []
    for c in comparisons:
        csv_rows.append(
            {k: c.get(k) for k in ("capture_id", "role", "roi_id", "field_id")}
            | {
                f"{domain}_{stage}_rmse": c.get(domain, {}).get(stage + "_rmse")
                for domain in ("raw", "isp")
                for stage in ("before", "after")
            }
        )
    if csv_rows:
        with (directory / "roi-errors.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0]))
            w.writeheader()
            w.writerows(csv_rows)
    parts.append(
        '<p><a href="./roi-errors.csv" download>모든 ROI의 보정 전후 오차 (CSV)</a> · '
        + _measurement_download(summary, "전체 측정 상세") + "</p>"
    )
    shown = set()
    parts.append(
        '<section class="card"><h2>실측 · 보정 전 · 보정 후 RAW·ISP</h2><p>같은 ROI 위치·관측 연산자에서 비교합니다. 재시뮬레이션은 개발 자료에서 고정한 edge 위치·방향·명암과 유효 PSF를 사용한 국소 edge 재현입니다. 보유 차트 전체의 정확한 장면 복원과 구분합니다.</p>'
    )
    for index, c in enumerate(comparisons):
        if c.get("role") != "validation" or c.get("roi_id") in shown:
            continue
        images = c.get("images", {})
        if not images:
            continue
        shown.add(c["roi_id"])
        parts.append(f"<h3>{_e(c['field_id'])} · {_e(c['roi_id'])}</h3>")
        for domain, label in [("raw", "RAW"), ("isp", "ISP")]:
            parts.append(f'<p><b>{label}</b></p><div class="grid">')
            for stage, name in [
                ("measured", "실측 입력" if measured else "가상 실측"),
                ("before", "보정 전"),
                ("after", "보정 후"),
            ]:
                namepath = images.get(stage, {}).get(domain)
                parts.append(_image(directory / namepath, name) if namepath else "<p>영상 없음</p>")
            parts.append("</div>")
        curves = c.get("curves", {})
        fig, axes = plt.subplots(1, 2, figsize=(10, 3), layout="constrained")
        for ax, domain in zip(axes, ("raw", "isp"), strict=True):
            for stage, color in [
                ("measured", "#182c45"),
                ("before", "#dc8b27"),
                ("after", "#228b75"),
            ]:
                curve = curves.get(domain, {}).get(stage) or {}
                x = curve.get("frequency_cy_native_px", curve.get("frequency", []))
                y = curve.get("sfr", [])
                if len(x) == len(y) and len(x) > 1:
                    ax.plot(x, y, label=stage, color=color)
            ax.set(
                title=domain.upper() + " SFR",
                xlabel="cycles / native pixel",
                ylabel="SFR",
                ylim=(0, 1.3),
            )
            ax.grid(alpha=0.2)
            if ax.lines:
                ax.legend(fontsize=8)
        name = f"curves-{index:03d}.png"
        fig.savefig(directory / name, dpi=130)
        plt.close(fig)
        parts.append(_image(directory / name, "검은색: 입력 · 주황색: 보정 전 · 녹색: 보정 후"))
    parts.append("</section>")
    parts.append(
        '<section class="card"><h2>모든 ROI 오차와 실패 기록</h2><div class="scroll"><table><tr><th>촬영/역할</th><th>위치/ROI</th><th>RAW 전 → 후</th><th>ISP 전 → 후'
        + (' · 진단 전용' if raw_only else '') + '</th><th>상태</th></tr>'
    )
    for c in comparisons:
        r, i = c.get("raw", {}), c.get("isp", {})
        parts.append(
            f"<tr><td>{_e(c.get('capture_id'))}<br>{_e(c.get('role'))}</td><td>{_e(c.get('field_id'))}<br>{_e(c.get('roi_id'))}</td><td>{_n(r.get('before_rmse'))} → {_n(r.get('after_rmse'))}</td><td>{_n(i.get('before_rmse'))} → {_n(i.get('after_rmse'))}</td><td>{_e(r.get('status'))} / {_e(i.get('status'))}</td></tr>"
        )
    parts.append("</table></div></section>")
    parts.append(_patch_tables(summary))
    parts.append(
        '<section class="card"><h2>DB 연결 · 출처 · 계산 비용</h2><p>DB 연결은 선택한 설계 조건과 이 측정 프로파일의 계보를 보존합니다. 실카메라와 DB 렌즈의 대응은 별도 확인 대상이며, E-SFR 전용 적용은 원본 렌즈 처방·QE·전기 파라미터를 변경하지 않습니다.</p>'
    )
    parts.append(
        "<pre>"
        + _e(
            json.dumps(
                {
                    k: source.get(k)
                    for k in (
                        "run_id",
                        "candidate_id",
                        "condition_id",
                        "manifest_sha256",
                        "candidate_sha256",
                    )
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        + "</pre>"
    )
    parts.append(
        f"<p>분석 시간 (보고서 생성 제외): {_n(summary.get('elapsed_seconds'))} s · 분석 ROI: {_e(summary.get('roi_count', '—'))}</p>"
    )
    parts.append(
        "<pre>"
        + _e(json.dumps(summary.get("limitations", []), ensure_ascii=False, indent=2))
        + "</pre></section>"
    )
    parts.append(
        '<footer class="muted">CameraE2E · 원본·조건·분석기·후보·검증 계보를 함께 보존합니다. 판정 범위 밖 항목은 미확립으로 남깁니다.</footer></main></html>'
    )
    (directory / "report.html").write_text("".join(parts), encoding="utf-8")
