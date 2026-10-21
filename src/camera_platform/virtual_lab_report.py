"""Offline, plot-first engineering report for a completed Camera Virtual Lab run."""

# Embedded HTML/SVG/CSS keep their presentation markup on readable single lines.
# ruff: noqa: E501

from __future__ import annotations

import html
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

from .virtual_lab_metrics import LINEAR_SRGB_TO_XYZ

COLORS = ["#086e86", "#f18037", "#6554bf", "#388c53", "#c74d69", "#9c771c", "#537aa9", "#945378", "#41625f"]
LABELS = {
    "center": "Center",
    "row1_col1": "Upper left", "row1_col2": "Upper middle", "row1_col3": "Upper right",
    "row2_col1": "Middle left", "row2_col2": "Center", "row2_col3": "Middle right",
    "row3_col1": "Lower left", "row3_col2": "Lower middle", "row3_col3": "Lower right",
    "upper_left": "Upper left",
    "upper_right": "Upper right",
    "lower_left": "Lower left",
    "lower_right": "Lower right",
    "top_left": "Upper left",
    "top_right": "Upper right",
    "bottom_left": "Lower left",
    "bottom_right": "Lower right",
}


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _num(value: Any, precision: int = 2) -> str:
    return (
        f"{float(value):.{precision}f}"
        if isinstance(value, (int, float)) and math.isfinite(value)
        else "—"
    )


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _pairs(x, y):
    if not isinstance(x, (list, tuple)) or not isinstance(y, (list, tuple)):
        return []
    return [
        (float(a), float(b))
        for a, b in zip(x, y, strict=False)
        if isinstance(a, (int, float))
        and isinstance(b, (int, float))
        and math.isfinite(a)
        and math.isfinite(b)
    ]


def _plot(series, *, x_label, y_label, y_min=0.0, y_max=None):
    """Inline SVG: usable without network, JavaScript, or chart dependencies."""
    lines = [(label, _pairs(x, y)) for label, x, y in series]
    lines = [(label, pts) for label, pts in lines if pts]
    if not lines:
        return '<div class="empty">이 시험의 곡선이 없습니다. 시험 선택과 결과 상태를 확인하세요.</div>'
    points = [p for _, pts in lines for p in pts]
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin = min(0.0, min(p[1] for p in points)) if y_min is None else y_min
    ymax = max(p[1] for p in points) * 1.07 if y_max is None else y_max
    if xmax <= xmin:
        xmax = xmin + 1
    if ymax <= ymin:
        ymax = ymin + 1
    w, h, left, top, pw, ph = 620, 290, 65, 20, 530, 210

    def sx(x):
        return left + pw * (x - xmin) / (xmax - xmin)

    def sy(y):
        return top + ph - ph * (y - ymin) / (ymax - ymin)

    bits = [
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{_e(y_label)} versus {_e(x_label)}">'
    ]
    for i in range(6):
        xv, yv = xmin + (xmax - xmin) * i / 5, ymin + (ymax - ymin) * i / 5
        x, y = sx(xv), sy(yv)
        bits += [
            f'<path d="M{left} {y:.2f}H{left + pw}" class="gridline"/>',
            f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" class="tick">{yv:.3g}</text>',
            f'<text x="{x:.2f}" y="{top + ph + 20}" text-anchor="middle" class="tick">{xv:.3g}</text>',
        ]
    bits += [
        f'<path d="M{left} {top}V{top + ph}H{left + pw}" class="axis"/>',
        f'<text x="{left + pw / 2}" y="{h - 15}" text-anchor="middle" class="axislabel">{_e(x_label)}</text>',
        f'<text transform="translate(17 {top + ph / 2}) rotate(-90)" text-anchor="middle" class="axislabel">{_e(y_label)}</text>',
    ]
    for i, (label, pts) in enumerate(lines):
        coords = " ".join(f"{sx(x):.2f},{sy(y):.2f}" for x, y in pts)
        bits.append(
            f'<polyline fill="none" stroke="{COLORS[i % len(COLORS)]}" stroke-width="2.4" points="{coords}"><title>{_e(label)}</title></polyline>'
        )
        if len(pts) <= 16:
            bits.extend(
                f'<circle cx="{sx(x):.2f}" cy="{sy(y):.2f}" r="3.2" fill="{COLORS[i % len(COLORS)]}"/>'
                for x, y in pts
            )
    bits.append('</svg><div class="legend">')
    for i, (label, _) in enumerate(lines):
        bits.append(f'<span><i style="background:{COLORS[i % len(COLORS)]}"></i>{_e(label)}</span>')
    bits.append("</div>")
    return "".join(bits)


def _panel(title, subtitle, content):
    return f'<article class="panel"><h3>{_e(title)}</h3><p class="muted">{_e(subtitle)}</p>{content}</article>'


def _optical_series(value):
    source = _mapping(value)
    if "axes" in source:
        source = {"center": source}
    series = []
    for name, record in source.items():
        axis = _mapping(_mapping(record).get("axes")).get("horizontal", {})
        if axis:
            angular = "cycles_per_degree" in axis
            series.append(
                (
                    LABELS.get(name, str(name)),
                    axis.get("cycles_per_degree" if angular else "cycles_per_mm", []),
                    axis.get("mtf", []),
                )
            )
    return series, "cycles / degree (paraxial)" if series and any(
        "cycles_per_degree" in _mapping(_mapping(v).get("axes")).get("horizontal", {})
        for v in source.values()
    ) else "cycles / mm"


def _wide_resolution(resolution):
    sites = _mapping(resolution.get("sites"))
    panels, rows = [], []
    for direction in ("radial", "tangential"):
        for stage, title in (("linear_system", "Linear"), ("processed_system", "Processed")):
            series = []
            for name, site in sites.items():
                record = _mapping(_mapping(site).get(direction))
                measured = _mapping(record.get(stage))
                frequency = _mapping(measured.get("frequency"))
                series.append((LABELS.get(name, name), frequency.get("cycles_per_pixel", []), measured.get("sfr", [])))
            panels.append(_panel(
                f"{title} {direction} SFR",
                "9개 native 위치 · 각도 환산과 ROI 척도 변화는 아래 표에서 확인",
                _plot(series, x_label="cycles / native pixel", y_label="SFR"),
            ))
        for name, site in sites.items():
            record = _mapping(_mapping(site).get(direction))
            measured = _mapping(record.get("processed_system"))
            angular = _mapping(record.get("angular_geometry"))
            qualified = angular.get("angular_frequency_qualified")
            status = "국소 근사" if qualified is True else "각도 재표본화 필요" if qualified is False else "미기록"
            rows.append(
                f"<tr><td>{_e(LABELS.get(name, name))}</td><td>{direction}</td>"
                f"<td>{_num(measured.get('mtf50_cycles_per_pixel'), 3)}</td>"
                f"<td>{_num(measured.get('mtf50_cycles_per_degree'), 3)}</td>"
                f"<td>{_num(100 * angular['roi_relative_scale_variation'], 2) if isinstance(angular.get('roi_relative_scale_variation'), (int, float)) else '—'}%</td><td>{status}</td></tr>"
            )
    panels.append('<article class="panel wide"><h3>Native directional site summary</h3><table><thead><tr><th>Site</th><th>Direction</th><th>MTF50<br>cycles/native pixel</th><th>MTF50<br>cycles/degree</th><th>ROI scale variation</th><th>Angular axis</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table><p class="muted">640×360 미리보기에서 측정하지 않습니다. 각도축은 각 ROI의 국소 구면 투영 Jacobian이며, ROI 내 척도 변화가 5%를 넘는 경우 각도축을 제공하지 않으며 각도 재표본화가 필요합니다. Native SFR은 보존합니다. MTF50가 표시 대역에서 교차하지 않으면 — 입니다.</p></article>')
    return "".join(panels)


def _resolution(value, *, field_mode="center_local"):
    resolution = _mapping(value)
    if resolution.get("schema") == "camera_virtual_lab_wide_resolution_v1":
        return _wide_resolution(resolution)
    sites = _mapping(resolution.get("sites"))
    fields = resolution.get("field_windows", [])
    full_field = field_mode == "full_field"
    if isinstance(fields, list) and fields and not full_field:
        sites = {
            row.get("id", str(i)): _mapping(
                _mapping(_mapping(row.get("metrics")).get("sites")).get("center")
            )
            for i, row in enumerate(fields)
            if isinstance(row, dict)
        }
    if not sites:
        sites = _mapping(_mapping(resolution.get("system")).get("sites"))
    result = []
    for stage, label in (
        ("linear_system", "Linear system SFR"),
        ("processed_system", "Processed system SFR"),
    ):
        series, angular = [], False
        for name, record in sites.items():
            measured = _mapping(record).get(stage, {})
            freq = _mapping(measured.get("frequency"))
            angular = "cycles_per_degree" in freq
            series.append(
                (
                    LABELS.get(name, str(name)),
                    freq.get("cycles_per_degree" if angular else "cycles_per_pixel", []),
                    measured.get("sfr", []),
                )
            )
        subtitle = (
            "설정한 관측 시야 전체의 차트 · FOV/640 근축 환산 · 국소 왜곡 보정 아님"
            if full_field
            else "별도 native 시야 창 · 동일 근축 물체각 주파수"
            if fields
            else (
                "공통 물체각 주파수 · 각 사이트의 동일 ROI"
                if angular
                else "해당 계산 격자의 주파수 · Nyquist까지"
            )
        )
        result.append(
            _panel(
                label,
                subtitle,
                _plot(
                    series,
                    x_label=("cycles / degree (paraxial)" if full_field else "cycles / degree") if angular else "cycles / pixel",
                    y_label="SFR",
                ),
            )
        )
    if sites:
        rows = "".join(
            f"<tr><td>{_e(LABELS.get(name, name))}</td><td>{_num(record.get('linear_system', {}).get('mtf50_cycles_per_pixel'), 3)}</td><td>{_num(record.get('processed_system', {}).get('mtf50_cycles_per_pixel'), 3)}</td><td>{_num(record.get('processed_system', {}).get('nyquist_response'), 3)}</td><td>{_num(100 * record.get('processed_system', {}).get('overshoot_fraction', 0), 1)}%</td></tr>"
            for name, record in sites.items()
        )
        result.append(
            f'<article class="panel wide"><h3>Site summary</h3><table><thead><tr><th>Field site</th><th>Linear MTF50<br>cycles/pixel</th><th>Processed MTF50<br>cycles/pixel</th><th>Nyquist response</th><th>Overshoot</th></tr></thead><tbody>{rows}</tbody></table><p class="muted">MTF50가 Nyquist 이전에 교차하지 않으면 — 로 표시합니다. ISP 이후 SFR은 렌즈 단독 MTF가 아닙니다.</p></article>'
        )
    if fields and not full_field:
        result.append(
            '<div class="notice wide">중심과 주변부는 위치별 PSF로 별도 계산한 작은 native 관측창입니다. 전체 차트의 주변부를 잘라 측정한 결과가 아닙니다. 메인 차트는 중심 PSF를 사용합니다.</div>'
        )
    return "".join(result)


def _field_response(value, config, images):
    """Fixed-scale maps and original numeric angular response, with model scope."""
    record = _mapping(value)
    if not record:
        return ""
    profile = _mapping(record.get("profile"))
    radius = profile.get("radius_fraction", [])
    cra = _mapping(record.get("cra_deg"))
    cra_range = record.get("theta_deg_range", [None, None])
    maximum = cra.get("max", cra_range[1] if len(cra_range) > 1 else None)
    angular_mode = record.get(
        "angular_mode", _mapping(config.get("angular_response")).get("mode", "off")
    )
    pupil_averaged = angular_mode == "physical_pupil"
    wide = config.get("lens_id") == "wide_parametric"
    source = record.get("cra_origin", "not_declared")
    source_label = (
        "선언한 출사동공 기하의 chief-ray 근사" if wide else
        "얇은 렌즈 chief-ray 근사"
        if source == "analytic_thin_lens_chief_ray_proxy"
        else "저장 렌즈 chief-ray 방향"
        if source == "lens_db_chief_direction"
        else str(source)
    )
    scope = (
        (
            "설정한 관측 시야 전체 · 위치별 PSF와 수신측 동공 방향 평균을 사용합니다."
            if pupil_averaged
            else "설정한 관측 시야 전체 · 위치별 PSF와 chief ray를 사용합니다."
        )
        if record.get("mode") == "full_field"
        else "중심 국소 광학 모델 · 입사각 모델의 적용 범위를 함께 확인하세요."
    )
    interpretation = {
        "physical_relative": "저장된 정면 QE에 층 모델에서 계산한 상대 각도 응답을 곱합니다. Chief ray만 적용하며 제품 실측 QE와 정합한 모델은 아닙니다.",
        "physical_pupil": "저장된 동공 방향별 층 모델 상대 응답을 수신측 위치별로 가중평균하고, 저장 정면 QE에 한 번 적용합니다. 정규화된 조건부 가중치는 렌즈 투과율을 추가하지 않습니다. 광선 도달 위치와 각도 사이의 결합까지 반영해 PSF를 다시 계산하는 방법은 아니며, 제품 실측 QE와 정합한 모델도 아닙니다.",
        "cosine_power": "선언한 R/Gr/Gb/B 채널별 cosⁿ 근사를 chief ray에 적용합니다. 수광각별 실측 응답은 아닙니다.",
        "off": "픽셀 각도 응답은 적용하지 않으며 상대 gain은 1입니다. CRA는 렌즈 모델에서 별도로 계산됩니다.",
    }.get(angular_mode, "선언된 입사각 응답 모델의 적용 범위를 확인하세요.")
    if wide and pupil_averaged:
        interpretation = interpretation.replace("저장된 동공 방향별", "선언한 합성 출사동공 원판 방향별")
    panels = [
        f'<div class="notice wide"><strong>{_e(scope)}</strong><p>CRA 진단: 최대 {_num(maximum)}° · {_e(source_label)} · 기준 파장 {_e(record.get("reference_wavelength_nm", 550))} nm</p><p>{_e(interpretation)}</p></div>'
    ]
    image_paths = _mapping(images)
    maps = _mapping(record.get("maps"))
    for key, label, legend in (
        ("cra", "Chief ray angle", "고정 표시 범위 0–60° · 550 nm chief ray"),
        (
            "angular_gain",
            "Relative angular response",
            ("동공 방향 가중평균 · " if pupil_averaged else "")
            + "고정 표시 범위 0–1.5× · 550 nm R/Gr/Gb/B 평균 · 수치 원본은 4채널 보존",
        ),
    ):
        path = _safe_image(image_paths.get(key, maps.get(key)))
        if path:
            panels.append(
                f'<figure class="imagecard"><a href="{_e(path)}"><img loading="lazy" src="{_e(path)}" alt="{_e(label)}"></a><figcaption><strong>{_e(label)}</strong><br>{_e(legend)}</figcaption></figure>'
            )
    panels.append(
        _panel(
            "CRA across the observation",
            "설정한 관측창의 중심에서 주변까지",
            _plot(
                [("Chief ray", radius, profile.get("cra_deg", []))],
                x_label="Observation radius / relative",
                y_label="CRA / degree",
            ),
        )
    )
    gains = profile.get("gain_r_gr_gb_b", [])
    series = [
        (
            channel,
            radius,
            [row[i] if isinstance(row, (list, tuple)) and len(row) > i else None for row in gains],
        )
        for i, channel in enumerate(("R", "Gr", "Gb", "B"))
    ]
    panels.append(
        _panel(
            "CFA relative angular response",
            "550 nm · 4채널 동공 방향 평균 · 렌즈 투과율과 별개"
            if pupil_averaged
            else "550 nm · 4채널 응답 · 렌즈 투과율과 별개",
            _plot(series, x_label="Observation radius / relative", y_label="Relative gain"),
        )
    )
    stats = _mapping(record.get("gain_r_gr_gb_b"))
    ranges = record.get("gain_range_r_gr_gb_b", [])
    rows = []
    for i, channel in enumerate(("R", "Gr", "Gb", "B")):
        span = ranges[i] if isinstance(ranges, list) and len(ranges) > i else [None, None]
        low = stats.get("min", [None] * 4)[i] if stats.get("min") else span[0]
        high = stats.get("max", [None] * 4)[i] if stats.get("max") else span[1]
        mean = stats.get("mean", [None] * 4)[i] if stats.get("mean") else None
        rows.append(f"<tr><td>{channel}</td><td>{_num(low, 4)}</td><td>{_num(high, 4)}</td><td>{_num(mean, 4)}</td></tr>")
    panels.append(
        '<article class="panel wide"><h3>Angular gain range</h3><table><thead><tr><th>Channel</th><th>Minimum</th><th>Maximum</th><th>Mean</th></tr></thead><tbody>'
        + "".join(rows)
        + '</tbody></table><p class="muted">색맵은 고정 표시 범위를 사용합니다. 표와 원본 배열의 수치는 표시 범위로 잘라내지 않습니다.</p></article>'
    )
    if pupil_averaged and record.get("pupil_sampling"):
        sampling_rows = "".join(
            f"<tr><td>{_e(key.replace('_', ' '))}</td><td>{_e(value)}</td></tr>"
            for key, value in _mapping(record.get("pupil_sampling")).items()
        )
        panels.append(
            f'<article class="panel wide"><h3>{"Declared exit-pupil sampling" if wide else "Stored pupil sampling"}</h3><p class="muted">{"선언한 출사동공 원판의 방향과 정규화 가중치" if wide else "보존된 방향과 유효 광선의 조건부 가중치"} · CRA 지도는 chief-ray 진단으로 유지합니다.</p><table><tbody>'
            + sampling_rows
            + "</tbody></table></article>"
        )
    provenance = record.get("angular_provenance")
    if pupil_averaged:
        provenance = {
            "angular_model": provenance,
            "pupil_sampling": record.get("pupil_sampling"),
            "pupil_table_sha256": record.get("pupil_table_sha256"),
            "pupil_table_artifact": record.get("pupil_table_artifact"),
        }
    if provenance:
        panels.append(
            '<details class="wide"><summary>입사각 응답 모델 출처</summary><pre>'
            + _e(json.dumps(provenance, ensure_ascii=False, indent=2, allow_nan=False))
            + "</pre></details>"
        )
    return "".join(panels)


def _swatch(rgb):
    values = np.clip(np.asarray(rgb, dtype=float), 0, 1)
    if values.shape != (3,) or not np.isfinite(values).all():
        return "#e7ebed"
    values = np.where(values <= 0.0031308, 12.92 * values, 1.055 * values ** (1 / 2.4) - 0.055)
    return "#" + "".join(f"{int(round(v * 255)):02x}" for v in values)


def _color(value):
    record = _mapping(value)
    patches = record.get("patches", [])
    if not patches:
        return _panel(
            "Color fidelity",
            "24개 고정 분광 기준 패치",
            '<div class="empty">색 시험이 실행되지 않았습니다.</div>',
        )
    cells = []
    inverse = np.linalg.inv(LINEAR_SRGB_TO_XYZ)
    for patch in patches:
        reference = np.asarray(patch.get("reference_xyz", [0, 0, 0])) @ inverse.T
        observed = patch.get("measured_linear_rgb", [0, 0, 0])
        cells.append(
            f'<div class="patch"><div class="patchcolors"><i style="background:{_swatch(reference)}" title="Reference"></i><i style="background:{_swatch(observed)}" title="Camera"></i></div><strong>{_e(patch.get("patch", ""))}</strong><span>ΔE {_num(patch.get("delta_e00"))}</span></div>'
        )
    return _panel(
        "Color fidelity · ΔE00",
        f"평균 {_num(record.get('delta_e00_mean'))} · 최대 {_num(record.get('delta_e00_max'))} · 중성 패치 xy 오차 {_num(record.get('neutral_xy_error_mean'), 4)}",
        '<div class="patchgrid">'
        + "".join(cells)
        + '</div><p class="muted">각 패치의 왼쪽은 기준, 오른쪽은 카메라입니다. 표시 색은 sRGB gamut으로 제한되지만 ΔE는 선형 XYZ에서 계산합니다. 시험 차트로 WB·CCM을 재적합하지 않습니다.</p>',
    )


def _rows(value):
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    value = _mapping(value)
    for key in (
        "photon_transfer_points",
        "sweep",
        "levels",
        "points",
        "curve",
        "conditions",
        "rows",
    ):
        if isinstance(value.get(key), list):
            return _rows(value[key])
    return []


def _noise(value):
    record = _mapping(value)
    rows = _rows(value)
    raw_rows, isp_rows = [], []
    for row in rows:
        raw = _mapping(row.get("raw", row.get("sensor", row)))
        isp = _mapping(row.get("processed", row.get("isp", row.get("after_isp", {}))))
        raw_rows.append(raw)
        if isp:
            isp_rows.append(isp)
    if not raw_rows and "temporal_variance" in record:
        raw_rows = [record]
        if record.get("after_isp"):
            isp_rows = [record["after_isp"]]
    mean = [row.get("mean_signal") for row in raw_rows]
    variance = [row.get("temporal_variance") for row in raw_rows]
    snr = [row.get("snr_db") for row in raw_rows]
    unit = record.get("unit", raw_rows[0].get("unit", "DN") if raw_rows else "DN")
    channel = record.get("channel", "Sensor")
    panels = [
        _panel(
            "Mean–variance / temporal noise",
            "독립 반복 취득의 픽셀별 시간 분산",
            _plot(
                [(channel, mean, variance)],
                x_label=f"Mean signal / {unit}",
                y_label=f"Temporal variance / {unit}²",
            ),
        ),
        _panel(
            "Measured signal-to-noise",
            "장면 질감과 고정 패턴을 시간 잡음으로 계산하지 않음",
            _plot(
                [(channel, mean, snr)],
                x_label=f"Mean signal / {unit}",
                y_label="SNR / dB",
                y_min=None,
            ),
        ),
    ]
    if raw_rows:
        trows = "".join(
            f"<tr><td>{_num(row.get('mean_signal'))}</td><td>{_num(row.get('temporal_std'), 3)}</td><td>{_num(row.get('snr_db'))}</td><td>{_num(row.get('fixed_pattern_and_shading_std_estimate'), 3)}</td><td>{_e(row.get('repeat_count', '—'))}</td></tr>"
            for row in raw_rows
        )
        panels.append(
            f'<article class="panel wide"><h3>Noise measurements</h3><table><thead><tr><th>Mean signal</th><th>Temporal σ</th><th>SNR dB</th><th>Spatial residual σ</th><th>Repeats</th></tr></thead><tbody>{trows}</tbody></table><p class="muted">Spatial residual에는 고정 패턴·shading이 포함되며 PRNU/DSNU를 독립 분리한 값은 아닙니다.</p></article>'
        )
    if isp_rows:
        ispunit = isp_rows[0].get("unit", "output units")
        panels.append(
            _panel(
                "ISP temporal noise",
                "동일 RAW 반복을 각각 ISP 처리한 후 측정",
                _plot(
                    [
                        (
                            "Processed",
                            [r.get("mean_signal") for r in isp_rows],
                            [r.get("temporal_std") for r in isp_rows],
                        )
                    ],
                    x_label=f"Mean signal / {ispunit}",
                    y_label="Temporal σ",
                ),
            )
        )
    predicted = _mapping(record.get("model_snr_curve"))
    if predicted:
        panels.append(
            _panel(
                "Readout model prediction",
                "별도 해석적 기대값 · 위의 반복 취득 측정과 구분",
                _plot(
                    [
                        (
                            "Predicted",
                            predicted.get("mean_electrons", []),
                            predicted.get("snr_db", []),
                        )
                    ],
                    x_label="Mean photoelectrons",
                    y_label="Predicted SNR / dB",
                    y_min=None,
                ),
            )
        )
    return "".join(panels)


def _tone(value):
    record = _mapping(value)
    response = record.get("response", [])
    if not isinstance(response, list):
        response = []
    x = [r.get("input_level") for r in response]
    panels = [
        _panel(
            "Exposure response",
            "평균 광전자와 readout을 고정 시험 광량에서 비교",
            _plot(
                [("Electrons", x, [r.get("mean_electrons") for r in response])],
                x_label="Input level / relative",
                y_label="Mean electrons",
            ),
        ),
        _panel(
            "Saturation",
            "Well 용량과 ADC clipping을 별도로 표시",
            _plot(
                [
                    ("Well", x, [r.get("well_saturation_fraction") for r in response]),
                    ("ADC", x, [r.get("adc_saturation_fraction") for r in response]),
                ],
                x_label="Input level / relative",
                y_label="Saturated fraction",
                y_max=1,
            ),
        ),
    ]
    return "".join(panels)


def _safe_image(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return None
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
    ):
        return None
    return value


def _images(images):
    cards = []
    for name, value in _mapping(images).items():
        path = _safe_image(value)
        if path:
            cards.append(
                f'<figure class="imagecard"><a href="{_e(path)}"><img loading="lazy" src="{_e(path)}" alt="{_e(name)}"></a><figcaption>{_e(name)}</figcaption></figure>'
            )
    return "".join(cards)


def _wide_context(result, config, geometry):
    if config.get("lens_id") != "wide_parametric":
        return ""
    values = [
        ("Projection", geometry.get("projection")),
        ("HFOV / VFOV / DFOV", " / ".join(_num(geometry.get(key), 2) + "°" for key in ("horizontal_fov_deg", "vertical_fov_deg", "diagonal_fov_deg"))),
        ("Focal length", _num(geometry.get("focal_length_mm"), 4) + " mm"),
        ("Pixel pitch", _num(geometry.get("pixel_pitch_um"), 3) + " µm"),
        ("Native sensor region", " × ".join(str(v) for v in reversed(geometry.get("native_shape", [])))),
        ("Preview", "640 × 360 · preview only"),
    ]
    facts = "".join(f"<div><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>" for label, value in values)
    links = []
    image_sets = _mapping(result.get("image_sets"))
    for kind, images in image_sets.items():
        path = _safe_image(_mapping(images).get("native_isp"))
        if path:
            links.append(f'<li><a href="{_e(path)}" download>{_e(kind)} · native ISP PNG</a></li>')
    if not links:
        path = _safe_image(_mapping(result.get("images")).get("native_isp"))
        if path:
            links.append(f'<li><a href="{_e(path)}" download>Native ISP PNG</a></li>')
    sampling = _mapping(result.get("tests")).get("sampling", _mapping(result.get("provenance")).get("wide_sampling"))
    sampling_html = ('<details><summary>실제 샘플링 · refinement 결과</summary><pre>' + _e(json.dumps(sampling, ensure_ascii=False, indent=2, allow_nan=False)) + '</pre></details>') if sampling else ""
    return f'<section id="wide-angle"><h2>Wide-angle design · native 2 MP</h2><dl>{facts}</dl><div class="notice">선언한 투영 · 회절 · 방사/접선 blur · 출사동공 근사입니다. 기존 DB 렌즈 처방의 시야 확장이나 제품 고유 mode가 아닙니다. 센서 pitch를 유지하는 1920×1080 가상 수광 영역을 계산하며, 아래 영상은 640×360 미리보기입니다.</div><h3>Full native ISP downloads</h3><ul>{"".join(links) or "<li>이 결과에는 native ISP 링크가 없습니다.</li>"}</ul>{sampling_html}</section>'


def render(result: dict, directory: Path | str) -> None:
    """Write report.html; packaged image paths remain relative for offline ZIP use."""
    directory = Path(directory)
    config, tests = (
        _mapping(result.get("configuration")),
        _mapping(result.get("tests", result.get("metrics"))),
    )
    resolution, color, noise, tone = (
        tests.get(key, {}) for key in ("resolution", "color", "noise", "tone")
    )
    field_mode = _mapping(config.get("field_model")).get("mode", "center_local")
    field_response = _field_response(tests.get("field_response"), config, result.get("images"))
    field_section = (
        f'<section id="field-response"><h2>Field response · CRA &amp; pixel coupling</h2><div class="grid">{field_response}</div></section>'
        if field_response
        else ""
    )
    field_link = '<a href="#field-response">Field &amp; CRA</a>' if field_response else ""
    pipeline_images = {
        key: value
        for key, value in _mapping(result.get("images")).items()
        if key != "native_isp" and not (field_response and key in {"cra", "angular_gain", "field_cra", "field_angular_gain"})
    }
    geometry = _mapping(
        result.get(
            "geometry",
            result.get("sampling", _mapping(result.get("provenance")).get("geometry", {})),
        )
    )
    wide_section = _wide_context(result, config, geometry)
    optical = _mapping(resolution).get("optical", tests.get("optical", {}))
    if not optical and isinstance(_mapping(resolution).get("field_windows"), list):
        optical = {
            row.get("id", str(i)): _mapping(row.get("metrics")).get("optical", {})
            for i, row in enumerate(resolution["field_windows"])
            if isinstance(row, dict)
        }
    optical_series, optical_unit = _optical_series(optical)
    optical_panel = (
        _panel(
            "Optical PSF → MTF",
            "550 nm 중심 PSF의 광학 전달 특성 · 수평 방향" if field_mode == "full_field" else "공급 PSF의 광학 전달 특성 · 수평 방향",
            _plot(optical_series, x_label=optical_unit, y_label="MTF"),
        )
        if optical_series
        else ""
    )
    badges = [
        config.get("lens_id", "Lens"),
        config.get("sensor_id", "Sensor"),
        config.get("illuminant", "Illuminant"),
        f"{_num(config.get('exposure_ms'))} ms",
        f"gain {_num(config.get('gain'), 1)}",
        _mapping(config.get("isp")).get("profile", "ISP"),
    ]
    header = "".join(f"<span>{_e(v)}</span>" for v in badges)
    limitations = "".join(f"<li>{_e(v)}</li>" for v in result.get("limitations", []))
    geometry_html = "".join(
        f"<div><dt>{_e(k.replace('_', ' '))}</dt><dd>{_e(v)}</dd></div>"
        for k, v in geometry.items()
        if not isinstance(v, dict)
    )
    center = _mapping(_mapping(_mapping(resolution).get("sites")).get("center", _mapping(_mapping(resolution).get("sites")).get("row2_col2")))
    kpis = [
        (
            "Center linear MTF50",
            _num(_mapping(center.get("linear_system")).get("mtf50_cycles_per_pixel"), 3),
            "cycles / pixel",
        ),
        ("Color ΔE00", _num(_mapping(color).get("delta_e00_mean")), "24-patch mean"),
        ("Temporal samples", str(config.get("frames", "—")), "independent captures"),
        ("Native observation", "1920 × 1080", "640 × 360 preview") if config.get("lens_id") == "wide_parametric" else ("Rendered observation", "640 × 360", "native calculation described below"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><span>{_e(label)}</span><strong>{_e(value)}</strong><small>{_e(unit)}</small></div>'
        for label, value, unit in kpis
    )
    details = _e(
        json.dumps(
            {"configuration": config, "geometry": geometry, "model": result.get("model", {})},
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )
    document = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{_e(config.get("name", "Camera Virtual Lab"))} · Camera Virtual Lab</title>
<style>
:root{{--ink:#162f40;--muted:#647b88;--line:#dce6eb;--accent:#086e86}}*{{box-sizing:border-box}}body{{margin:0;background:#eef3f5;color:var(--ink);font:15px/1.55 system-ui,-apple-system,sans-serif}}main{{max-width:1360px;margin:auto;padding:44px 28px 64px}}header{{margin-bottom:28px}}.eyebrow{{color:var(--accent);font-weight:750;letter-spacing:.13em;font-size:12px}}h1{{font-size:clamp(28px,4vw,46px);line-height:1.15;letter-spacing:-.035em;margin:12px 0}}h2{{font-size:26px;letter-spacing:-.025em;margin:0 0 5px}}h3{{font-size:17px;margin:0}}p{{margin:8px 0}}.muted,.sectionnote{{color:var(--muted);font-size:13px}}.badges{{display:flex;flex-wrap:wrap;gap:8px;margin-top:19px}}.badges span{{border:1px solid #c6d7df;border-radius:99px;background:#fff;padding:6px 12px;font-size:12px}}nav{{display:flex;gap:20px;flex-wrap:wrap;border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:14px 0;margin:25px 0}}a{{color:var(--accent);text-decoration:none}}a:hover{{text-decoration:underline}}.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.kpi{{background:#fff;border-radius:14px;padding:18px 20px;border-top:3px solid var(--accent)}}.kpi span,.kpi small{{display:block;color:var(--muted);font-size:12px}}.kpi strong{{display:block;font-size:29px;letter-spacing:-.03em;margin:3px 0}}section{{margin-top:38px;scroll-margin-top:20px}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:16px}}.panel,.imagecard{{background:#fff;border:1px solid var(--line);border-radius:15px;padding:20px;min-width:0;margin:0}}.wide{{grid-column:1/-1}}svg{{display:block;width:100%;height:auto}}.gridline{{stroke:#e5edf1;stroke-width:1}}.axis{{stroke:#9bb0bb;fill:none}}.tick{{font:11px system-ui;fill:#647b88}}.axislabel{{font:12px system-ui;fill:#324f61}}.legend{{display:flex;gap:13px;flex-wrap:wrap;font-size:11px;justify-content:center}}.legend i{{display:inline-block;width:15px;height:3px;vertical-align:middle;margin-right:5px}}.empty{{min-height:180px;display:grid;place-content:center;color:var(--muted);text-align:center;font-size:13px;border:1px dashed var(--line);border-radius:10px}}table{{width:100%;border-collapse:collapse;margin-top:14px;font-size:12px}}th,td{{padding:10px;text-align:left;border-bottom:1px solid var(--line)}}th{{color:var(--muted);font-weight:600}}.patchgrid{{display:grid;grid-template-columns:repeat(6,1fr);gap:9px;margin-top:20px}}.patch{{border:1px solid var(--line);border-radius:7px;overflow:hidden;text-align:center;padding-bottom:5px}}.patchcolors{{display:flex;height:51px}}.patchcolors i{{flex:1}}.patch strong{{display:block;font-size:11px;margin-top:4px}}.patch span{{font-size:10px;color:var(--muted)}}.imagecard{{padding:10px}}.imagecard img{{width:100%;display:block;background:#111;aspect-ratio:16/9;object-fit:contain;border-radius:8px}}figcaption{{padding:10px 8px 2px;font-size:12px;color:var(--muted)}}.notice{{background:#e3eff3;border-left:3px solid var(--accent);padding:13px 17px;border-radius:0 10px 10px 0;margin-top:17px;color:#365665;font-size:13px}}dl{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:18px 0}}dt{{font-size:11px;color:var(--muted)}}dd{{margin:4px 0;font-size:13px;overflow-wrap:anywhere}}details{{margin-top:15px;background:#fff;border-radius:12px;padding:16px 20px;border:1px solid var(--line)}}summary{{cursor:pointer;font-weight:650}}pre{{font:12px/1.55 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}}footer{{margin-top:35px;color:var(--muted);font-size:12px}}@media(max-width:760px){{main{{padding:25px 16px}}.grid{{grid-template-columns:1fr}}.kpis{{grid-template-columns:repeat(2,1fr)}}dl{{grid-template-columns:1fr 1fr}}.panel{{padding:15px}}.patchgrid{{gap:5px}}}}@media print{{body{{background:white}}main{{padding:10px}}nav{{display:none}}.panel{{break-inside:avoid}}section{{break-before:auto}}}}
</style></head><body><main>
<header><div class="eyebrow">CAMERA VIRTUAL LAB / DESIGN REVIEW</div><h1>{_e(config.get("name", "Camera Virtual Lab"))}</h1><p>렌즈 · 센서 · ISP가 만드는 영상과 Lab 응답을 한 구성에서 확인합니다.</p><div class="badges">{header}</div></header>
<div class="kpis">{kpi_html}</div><nav><a href="#images">Pipeline</a><a href="#resolution">Resolution</a>{field_link}<a href="#color">Color</a><a href="#noise">Noise</a><a href="#tone">Tone</a><a href="#configuration">Configuration</a></nav>
{wide_section}
<section id="images"><h2>Pipeline images</h2><p class="sectionnote">저장된 실제 계산 단계 · 이미지를 선택하면 원본 크기로 열립니다.</p><div class="grid">{_images(pipeline_images)}</div></section>
<section id="resolution"><h2>Resolution</h2><p class="sectionnote">Optical MTF와 선형·ISP 이후 시스템 SFR을 구분해 해석합니다.</p><div class="grid">{optical_panel}{_resolution(resolution, field_mode=field_mode)}</div></section>
{field_section}
<section id="color"><h2>Color</h2><div class="grid"><div class="wide">{_color(color)}</div></div></section>
<section id="noise"><h2>Noise</h2><p class="sectionnote">평균–분산 · SNR · 공간 비균일도</p><div class="grid">{_noise(noise)}</div></section>
<section id="tone"><h2>Tone & saturation</h2><div class="grid">{_tone(tone)}</div></section>
<section id="configuration"><h2>Calculation context</h2><dl>{geometry_html}</dl><div class="notice">모델 기반 가상 Lab 결과입니다. 실제 카메라의 실측 일치나 규격 인증을 의미하지 않습니다. 표시 영상의 크기와 native 계산 격자는 별도로 확인하세요.</div><details open><summary>모델 가정과 적용 범위</summary><ul>{limitations}</ul></details><details><summary>고정 구성과 계산 좌표</summary><pre>{details}</pre></details></section>
<footer><a href="result.json">전체 수치 결과</a> · <a href="manifest.json">원본 파일 목록</a><p>Camera Virtual Lab · Offline report · No network dependencies</p></footer>
</main></body></html>"""
    (directory / "report.html").write_text(document, encoding="utf-8")
