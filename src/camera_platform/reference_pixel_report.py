"""Small source-bound report for the Reference Pixel Lab; no capture or ISP execution."""

# HTML prose and inline CSS are deliberately kept together for the standalone export.
# ruff: noqa: E501

from __future__ import annotations

import base64
import html
from pathlib import Path
from urllib.parse import urlencode

import numpy as np


def _esc(value):
    return html.escape(str(value), quote=True)


def _num(value, precision=5):
    return "—" if value is None else f"{value:.{precision}f}"


def _table(headers, rows):
    return (
        '<div class="scroll"><table><thead><tr>'
        + "".join(f"<th>{_esc(h)}</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


def _fields(candidate):
    return {field["id"]: field for field in candidate["fields"]}


def _plot_assets(candidates, root, baseline_id):
    # Standard plotting backend keeps the exported scientific plots independent of the UI.
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    colors = ("#c27a40", "#277967", "#557daa")
    outputs = {}

    def figure(key, draw):
        fig = Figure(figsize=(8.7, 3.3), layout="constrained", facecolor="white")
        FigureCanvasAgg(fig)
        axis = fig.subplots()
        draw(axis)
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="y", alpha=0.16)
        folder = root / "plots"
        folder.mkdir(exist_ok=True)
        path = folder / (key + ".png")
        fig.savefig(path, dpi=155)
        outputs[key] = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()

    def spectral(axis):
        for color, candidate in zip(colors, candidates, strict=True):
            site = candidate["sites"].index("Gr")
            axis.plot(
                candidate["wavelength_nm"],
                np.asarray(candidate["qe"]["normal"])[:, site],
                color=color,
                lw=2,
                label=f"CFA x{candidate['cfa_thickness_scale']:g}",
            )
        axis.set(
            xlabel="Wavelength (nm)",
            ylabel="Normal QE (collected e / incident photon)",
            title="Same pixel pitch and total stack height · Gr site",
        )
        axis.set_ylim(bottom=0)
        axis.legend(frameon=False, ncol=3)

    figure("normal-qe", spectral)
    baseline = next(c for c in candidates if c["id"] == baseline_id)
    wi, si = baseline["wavelength_nm"].index(550), baseline["sites"].index("Gr")
    available = [field for field in baseline["fields"] if field["status"] == "available"]

    def angular(axis):
        for key, label, color in (
            ("normal_qe", "Normal", "#9a9a84"),
            ("chief_qe", "Chief ray", "#557daa"),
            ("pupil_qe", "Pupil integrated", "#277967"),
        ):
            axis.plot(
                range(len(available)),
                [np.asarray(f[key])[wi, si] for f in available],
                marker="o",
                color=color,
                lw=1.7,
                label=label,
            )
        axis.set_xticks(range(len(available)), [f["id"] for f in available], rotation=20)
        axis.set(ylabel="QE at 550 nm (Gr)", title="Baseline CFA x1 · same sensor-plane photons")
        axis.set_ylim(bottom=0)
        axis.legend(frameon=False, ncol=3)

    if available:
        figure("angular-response", angular)

    def budget(axis):
        selected = [(c, _fields(c).get("outer-ne")) for c in candidates]
        selected = [(c, f) for c, f in selected if f and f["status"] == "available"]
        segments = [
            ("reflection", "Reflection", "#68809b"),
            ("lens", "Top polymer absorption", "#c3bdad"),
            ("cfa", "CFA absorption", "#c28d67"),
            ("passivation", "Passivation absorption", "#a4bc94"),
            ("collected_electrons", "Collected electrons", "#2b786b"),
            ("collection_loss", "Collection loss in Si", "#a4bac5"),
            ("deep_transmission", "Deep transmission", "#e5ddcd"),
        ]
        left = np.zeros(len(selected))
        for key, label, color in segments:
            values = []
            for candidate, field in selected:
                b = field["photon_budget"]["pupil"]
                table = (
                    b["layer_absorption"][key] if key in {"lens", "cfa", "passivation"} else b[key]
                )
                values.append(
                    np.asarray(table)[
                        candidate["wavelength_nm"].index(550), candidate["sites"].index("Gr")
                    ]
                )
            axis.barh(range(len(selected)), values, left=left, color=color, label=label, height=0.6)
            left += values
        axis.set_yticks(
            range(len(selected)), [f"CFA x{c['cfa_thickness_scale']:g}" for c, _ in selected]
        )
        axis.set(
            xlim=(0, 1),
            xlabel="Fraction per incident photon",
            title="Outer NE · 550 nm Gr · saved pupil weighting",
        )
        axis.legend(
            frameon=False, ncol=3, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.2)
        )

    if any(_fields(c).get("outer-ne", {}).get("status") == "available" for c in candidates):
        figure("photon-budget", budget)
    return outputs


def write_report(result, directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    candidates = sorted(result["candidate_details"], key=lambda c: c["cfa_thickness_scale"])
    plots = _plot_assets(candidates, root, result["baseline_candidate_id"])
    structure_rows, angular_rows, color_rows, source_rows, closure_rows = [], [], [], [], []
    for candidate in candidates:
        section = candidate["structure"]["cross_section"]
        layers = {layer["name"]: layer for layer in section["layers"]}
        scale = f"×{candidate['cfa_thickness_scale']:g}"
        structure_rows.append(
            [
                scale,
                section["pixel_pitch_um"],
                layers["cfa"]["thickness_um"],
                layers["passivation"]["thickness_um"],
                section["top_stack_height_um"],
                layers["active_silicon"]["thickness_um"],
            ]
        )
        wi, si = candidate["wavelength_nm"].index(550), candidate["sites"].index("Gr")
        for field in candidate["fields"]:
            if field["id"] not in {"center", "outer-ne"}:
                continue
            if field["status"] != "available":
                angular_rows.append(
                    [scale, field["id"], "사용 불가", field.get("reason"), "—", "—"]
                )
            else:
                angular_rows.append(
                    [
                        scale,
                        field["id"],
                        _num(field["chief_theta_deg"][wi], 2),
                        *[
                            _num(np.asarray(field[k])[wi, si])
                            for k in ("normal_qe", "chief_qe", "pupil_qe")
                        ],
                    ]
                )
            color = field["color_comparison"]
            for role, title in (("frozen", "고정 spectral"), ("munsell", "Munsell64 재피팅")):
                arm = color.get(role, {})
                eligible = arm.get("metric_support", {}).get("color", {}).get("eligible")
                color_rows.append(
                    [
                        scale,
                        field["id"],
                        title,
                        _num(arm.get("delta_e00_mean"), 4),
                        _num(arm.get("color_noise_luma"), 6),
                        "평가 가능" if eligible else "측정 지지 부족 / 미제공",
                    ]
                )
        refs = candidate["source_refs"]
        source_rows.append(
            [
                candidate["id"],
                refs["acquisition"]["run_id"],
                refs["acquisition"]["candidate_id"],
                refs["color"]["run_id"],
            ]
        )
        closure_rows.append(
            [
                scale,
                f"{candidate['photon_budget']['optical_closure_max_abs']:.2e}",
                f"{candidate['photon_budget']['collection_closure_max_abs']:.2e}",
            ]
        )
    selected = candidates[0]["id"]
    url = "/reference-workshop.html?" + urlencode(
        {"pixel_study": root.name, "pixel_candidate": selected, "field": "outer-ne"}
    )

    def image(key, alternative):
        return (
            f'<img src="{plots[key]}" alt="{_esc(alternative)}">'
            if key in plots
            else (
                '<p class="note">보존 응답의 지원 범위가 부족하여 이 그래프는 제공되지 않습니다.</p>'
            )
        )

    # The report remains standalone even after download; source links need the local platform server.
    document = f'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_esc(result["name"])}</title>
<style>
body{{font:15px/1.65 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif;color:#1c392f;background:#f3f6f0;margin:0}}main{{max-width:1100px;padding:38px 24px;margin:auto}}h1{{font-size:38px;line-height:1.2;letter-spacing:-1px}}h2{{font-size:23px}}h3{{font-size:16px}}a{{color:#086b5a}}.eyebrow{{font-size:11px;letter-spacing:2px;color:#50836d}}.lead{{font-size:18px;color:#577064}}section{{background:white;padding:24px;border:1px solid #dae5d8;border-radius:12px;margin:18px 0}}.links{{display:flex;gap:18px;flex-wrap:wrap}}.note{{font-size:12px;color:#687d6e}}img{{display:block;width:100%;height:auto;margin:18px 0}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}}td,th{{padding:10px 8px;border-bottom:1px solid #e3eae0;text-align:left;white-space:nowrap}}th{{color:#5a7b66;background:#f7f9f4}}.badges{{color:#2b6b51;font-size:13px}}details{{margin-top:16px}}footer{{font-size:12px;color:#6a7a6c;margin-top:24px}}@media print{{body{{background:white}}section{{break-inside:avoid}}}}@media(max-width:640px){{main{{padding:18px 12px}}h1{{font-size:29px}}section{{padding:16px}}}}
</style><main><div class="eyebrow">CAMERAE2E · REFERENCE PIXEL DESIGN LAB</div>
<h1>픽셀 구조의 변경을<br>광자 예산·각도 응답·색 잡음으로 연결합니다.</h1>
<p class="lead">고정된 특허 렌즈 아래에서 CFA 두께 세 후보의 보존된 계산 결과를 비교합니다. 구조 근거와 모델 가정, 수집 전자와 ISP 색 보정의 역할을 분리합니다.</p>
<p class="badges">2.9 μm pitch · 총 높이 고정 · CFA ×0.9 / 1.0 / 1.1 · 원본 RAW 참조 · 신규 취득 0</p>
<div class="links"><a href="{_esc(url)}">Pixel Design Lab 열기 ↗</a><a href="#photon">광자 예산</a><a href="#angle">입사각 결합</a><a href="#color">색 보정과 잡음</a></div>
<section id="photon"><div class="eyebrow">01 · STRUCTURE TO COLLECTED ELECTRONS</div><h2>CFA를 얇게 하면 무엇이 통과하고, 무엇을 잃는가?</h2>
{_table(["CFA 배율", "Pitch (μm)", "CFA (μm)", "Passivation (μm)", "상부 총 높이 (μm)", "Active Si (μm)"], structure_rows)}
{image("normal-qe", "세 CFA 후보의 Gr 정상 입사 분광 QE")}{image("photon-budget", "주변 NE 550nm Gr의 동공 적분 광자 예산")}
<p class="note">이 예산은 보존된 평면층 모델의 흡수·반사·투과·수집 분해입니다. Silicon 흡수를 수집 전자와 수집 손실로 나누었으며 이들을 이중 합산하지 않습니다. 마이크로렌즈 형상·DTI·공간 crosstalk의 새 해석은 아닙니다.</p></section>
<section id="angle"><div class="eyebrow">02 · OPTICS TO ANGULAR RESPONSE</div><h2>CRA 하나와 동공 전체 방향은 같은 입력이 아닙니다.</h2>
{image("angular-response", "기준 CFA의 정상 입사, chief ray 및 동공 적분 QE")}
{_table(["CFA", "시야", "Chief 각도 (°)", "Normal QE", "Chief QE", "Pupil QE"], angular_rows)}
<p class="note">표는 550 nm Gr의 비교입니다. 저장된 센서면 도달 광자의 조건부 방향 가중치를 사용하며 cosine·면적·렌즈 throughput을 다시 곱하지 않습니다. Normal/chief/pupil 값은 각도 응답 진단이며 다른 세 장의 RAW를 새로 촬영한 결과가 아닙니다. 현재 층 모델은 방위각에 독립적입니다.</p></section>
<section id="color"><div class="eyebrow">03 · PIXEL TO ISP COLOR / NOISE</div><h2>색차를 줄인 보정이 잡음까지 줄여 주는가?</h2>
{_table(["CFA", "시야", "색 행렬", "취득 차트 ΔE00", "색 차트 시간 잡음 Y", "측정 지지"], color_rows)}
<p class="note">같은 CFA의 두 행은 동일 RAW를 사용합니다. 다른 CFA 사이의 RAW는 픽셀 응답 변화로 달라집니다. 색 행렬 외에는 LSC·demosaic·표시 white·gamma를 고정하고 NR/sharpen을 끕니다. 시간 잡음은 반복 RAW의 시간 분산이며 공간 질감을 잡음으로 계산하지 않습니다.</p></section>
<section><h2>보존한 출처와 지원 범위</h2>{_table(["후보", "취득 run", "취득 candidate", "색 보정 run"], source_rows)}
<details><summary>광학·수집 예산의 수치 닫힘</summary>{_table(["CFA", "최대 광학 닫힘 오차", "최대 Si 수집 닫힘 오차"], closure_rows)}</details>
<p>자동으로 최적 센서를 선언하지 않습니다. 기준 CFA ×1.0과 원하는 후보·시야·파장·색 보정 방식을 선택해 비교를 저장할 수 있습니다. 원본 RAW는 기존 외부 저장소에 있고 이 패키지는 진단 수치와 source 참조를 보존합니다.</p>
<p class="note">보고서 구조 근거: DEF-2011-804. 재료 n,k·수집효율·readout·색별 CFA 배치는 모델 가정입니다. 기하 ray PSF는 회절을 포함하지 않으며, 9개 로컬 탐침은 전체 제품 센서 프레임이 아닙니다. 외부 전달 권한은 기존 원자료의 이용 조건을 따릅니다.</p></section>
<footer>원본 보존: {_esc(result.get("raw_preserved"))} · RAW 복사: {_esc(result.get("raw_copied"))} · 신규 취득: {_esc(result.get("new_acquisitions"))} · 실측 검증: 미확립</footer></main></html>'''
    (root / "report.html").write_text(document)
    return root / "report.html"
