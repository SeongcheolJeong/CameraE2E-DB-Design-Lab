"""Standard portable chart, PSF, parameter and artifact report for core calibration."""

from __future__ import annotations

import base64
import csv
import hashlib
import html
import io
import zipfile

import numpy as np

from . import calibration_db_core_engine as engine
from .calibration_db_optics_engine import transfer_mtf

NAMES = {"dark": "Dark · 암전류·Black", "ptc": "PTC · 변환 이득·읽기 잡음", "mtf": "MTF · 유효 PSF"}
METRICS = {
    "raw_mean_dn": ("RAW 평균", "DN"),
    "raw_variance_dn2": ("RAW 시간 분산", "DN²"),
    "isp_mean_rgb": ("ISP 평균 RGB", "linear RGB"),
    "raw_sfr": ("RAW Gr SFR", "1"),
    "isp_sfr": ("ISP SFR", "1"),
}
CONTROLS = """'use strict';
document.querySelector('#chart').addEventListener('change', e => {
 document.querySelectorAll('section[data-chart]').forEach(s => {
  s.hidden=e.target.value!=='all' && s.dataset.chart!==e.target.value;
 });
});
document.querySelector('#print-report').addEventListener('click',()=>window.print());
"""


def esc(x):
    return html.escape(str(x), quote=True)


def fmt(x):
    return "—" if x is None else f"{x:.6g}" if isinstance(x, (float, int)) else esc(x)


def image(root, name):
    return "data:image/png;base64," + base64.b64encode((root / name).read_bytes()).decode()


def table(headers, rows):
    return (
        '<div class="scroll"><table><thead><tr>'
        + "".join("<th>" + esc(h) + "</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join("<td>" + fmt(v) + "</td>" for v in row) + "</tr>" for row in rows
        )
        + "</tbody></table></div>"
    )


def plot(path, x, metric, label, unit, x_unit):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(11, 3.1), constrained_layout=True)
    for role, title, color in [
        ("before", "Before", "#ab6e3d"),
        ("observation", "Synthetic observation", "#26383d"),
        ("after", "After", "#008b89"),
        ("replicate", "Independent repeat", "#9da7ac"),
    ]:
        y = np.asarray(metric["observation"] if role == "observation" else metric[role]["values"])
        if y.ndim > 1:
            y = y.mean(axis=1)
        axes[0].plot(x, y, label=title, color=color, linewidth=1.5)
        if role in ("before", "after"):
            delta = np.asarray(metric[role]["residual"])
            if delta.ndim > 1:
                delta = delta.mean(axis=1)
            axes[1].plot(x, delta, label=title, color=color)
    for ax in axes:
        ax.set_xlabel(x_unit)
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8)
    axes[0].set_ylabel(label + " (" + unit + ")")
    axes[1].set_ylabel("Signed residual (" + unit + ")")
    fig.savefig(path, dpi=130)
    plt.close(fig)


def psf_evidence(root, summary, kernel_provider=None):
    import json

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    truth = json.loads((root / "authored-truth.json").read_text())
    fields = []
    for fid in engine.FIELDS:
        records = {}
        for role, profile in [
            ("before", summary["baseline_profiles"][fid]),
            ("observation", truth[fid]),
            ("after", summary["candidate_profiles"][fid]),
        ]:
            node = profile["blur"]["field_nodes"][0]
            if kernel_provider is None:
                _, kernel = engine.lab._effective_blur(np.zeros((8, 8, 3)), node)
                spacing = 1.0 if node.get("family") == "sampled_native_kernel_v1" else 0.25
            else:
                kernel, spacing = kernel_provider(role, fid, node)
            folder = root / "psf" / fid
            folder.mkdir(parents=True, exist_ok=True)
            np.save(folder / (role + ".npy"), kernel, allow_pickle=False)
            coordinates = (np.arange(kernel.shape[0]) - kernel.shape[0] // 2) * spacing
            yy, xx = np.meshgrid(coordinates, coordinates, indexing="ij")
            cx, cy = float(np.sum(kernel * xx)), float(np.sum(kernel * yy))
            sx, sy = (
                float(np.sqrt(np.sum(kernel * (xx - cx) ** 2))),
                float(np.sqrt(np.sum(kernel * (yy - cy) ** 2))),
            )
            fig, ax = plt.subplots(figsize=(3.4, 3.1), constrained_layout=True)
            extent = [coordinates[0] - spacing / 2, coordinates[-1] + spacing / 2] * 2
            im = ax.imshow(
                np.log10(np.maximum(kernel / spacing**2, 1e-7)),
                extent=extent,
                vmin=-7,
                vmax=0,
                cmap="magma",
                origin="lower",
            )
            ax.set(
                xlabel="native pixel",
                ylabel="native pixel",
                xlim=(-8, 8),
                ylim=(-8, 8),
                title=f"{role} · pitch {spacing:g} px",
            )
            fig.colorbar(im, ax=ax, label="log10 density / pixel²")
            name = (folder / (role + ".png")).relative_to(root).as_posix()
            fig.savefig(root / name, dpi=120)
            plt.close(fig)
            frequency = np.linspace(0, 0.5, 101)
            mtf = transfer_mtf(kernel, frequency, [0, 90], spacing=spacing)
            records[role] = {
                "kernel": str((folder / (role + ".npy")).relative_to(root)),
                "image": name,
                "pitch_px": spacing,
                "sum": float(kernel.sum()),
                "centroid_px": [cx, cy],
                "sigma_second_moment_px": [sx, sy],
                "frequency_cy_px": frequency.tolist(),
                "kernel_mtf_xy": mtf.tolist(),
                "family": node.get("family", "centered_elliptical_gaussian_v1"),
            }
        curves = {role: np.asarray(r["kernel_mtf_xy"]).T for role, r in records.items()}
        # Truth is known only in this authored experiment, never a physical PSF verdict.
        curves["replicate"] = curves["observation"]
        metric = engine.residuals(curves)
        metric["scope"] = "Known synthetic kernel OTF; separate from observed RAW/ISP SFR"
        name = f"psf/{fid}/kernel-mtf.png"
        plot(root / name, frequency, metric, "Applied kernel MTF", "1", "cycles/native pixel")
        fields.append(
            {"field_id": fid, "series": records, "kernel_mtf_error": metric, "plot": name}
        )
    return fields


def aggregate(summary):
    result = {}
    for group, key in [
        ("dark", "raw_mean_dn"),
        ("ptc", "raw_variance_dn2"),
        ("mtf", "raw_sfr"),
        ("isp_mtf", "isp_sfr"),
    ]:
        rows = [
            c["metrics"][key]
            for c in summary["comparisons"]
            if key in c["metrics"] and (group in ("mtf", "isp_mtf") or c["chart"] == group)
        ]
        if not rows:
            continue
        errors = {
            role: float(
                np.sqrt(
                    np.mean(
                        np.concatenate([np.asarray(m[role]["residual"]).ravel() for m in rows]) ** 2
                    )
                )
            )
            for role in ("before", "after", "replicate")
        }
        errors["reduction_percent"] = (
            None if errors["before"] == 0 else 100 * (1 - errors["after"] / errors["before"])
        )
        result[group] = {"metric": key, "unit": METRICS[key][1], **errors}
    return result


def render(root, s):
    parts = [
        '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewp'
        'ort" content="width=device-width,initial-scale=1"><title>Dark·PTC / MT'
        "F·PSF 자동 보정 보고서</title><style>body{margin:0;background:#eff4f0;color:#"
        "183d3e;font:14px/1.6 system-ui}main{max-width:1220px;margin:auto;paddi"
        "ng:26px}header,section{background:white;border:1px solid #dae4db;borde"
        "r-radius:12px;padding:24px;margin-bottom:22px}h1{font-size:30px}h2{fon"
        "t-size:23px}h3{margin-top:28px}.three{display:grid;grid-template-colum"
        "ns:repeat(3,1fr);gap:14px}figure{margin:0;border:1px solid #e0e6df;pad"
        "ding:10px;border-radius:8px}figcaption{font-weight:650;margin-bottom:8"
        "px}img{display:block;width:100%}.note{padding:12px;background:#faf4e8;"
        "border-left:3px solid #ac7c43}.metric{padding:10px;background:#edf6f3}"
        ".scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-v"
        "ariant-numeric:tabular-nums}th,td{padding:9px;text-align:left;border-b"
        "ottom:1px solid #dce6df}pre{white-space:pre-wrap;overflow-wrap:anywher"
        "e;font-size:11px}a{color:#007d7f}select,button{padding:9px;margin:6px}"
        ".identity{overflow-wrap:anywhere;font:11px monospace}@media(max-width:"
        "760px){.three{grid-template-columns:1fr}main{padding:10px}section,head"
        "er{padding:15px}}@media print{header,section{break-before:page}header{"
        "break-before:auto}.toolbar{display:none}}</style><main>"
    ]
    parts += [
        "<header><p>CAMERAE2E · AUTOMATIC CORE CALIBRATION</p><h1>가상 실측으로 확인한<b"
        "r>Dark·PTC / MTF·PSF 보정 전후</h1"
        '><p class="note">새 가상 실측은 원래 정'
        "의한 합성 카메라에"
        "서 생성했습니다. 학습 RAW로 추정한 후보를 고정한 "
        "뒤 별도 취득 A/B로 평가하며 재피팅하지 않습니다. "
        "실제 카메라 검증은"
        " 미확립입니다.</p>",
        table(
            ["차트 지표", "보정 전 RMSE", "보정 후 RMSE", "감소 (%)", "독립 반복 A/B", "단위"],
            [
                [key, a["before"], a["after"], a["reduction_percent"], a["replicate"], a["unit"]]
                for key, a in s["aggregate"].items()
            ],
        ),
        '<p class="identity">'
        + esc(s["run_id"])
        + "<br>"
        + esc(s["model_id"])
        + "</p><p>DB 연결: "
        + esc(s["source_context"]["run_id"])
        + " / "
        + esc(s["source_context"]["candidate_id"])
        + " / "
        + esc(s["source_context"].get("condition_id"))
        + "</p><p>모델 적용 상태: "
        + esc(s["activation"]["status"])
        + '</p><p class="toolbar"><label>차트 <select id="chart"><option value="all'
        '">모두</option><option value="dark">Dark</option><option value="ptc">PTC'
        '</option><option value="mtf">MTF·PSF</option></select></label><button '
        'id="print-report">인쇄 / PDF 저장</button><a href="report.json">수치 JSON</a'
        '> · <a href="candidate-profiles.json">적용 프로파일</a> · <a href="parameter'
        's.csv">파라미터 CSV</a> · <a href="evidence.zip">RAW·ISP·근거 ZIP</a></p></h'
        "eader>",
    ]
    for c in s["comparisons"]:
        parts.append(
            '<section data-chart="'
            + c["chart"]
            + '"><h2>'
            + NAMES[c["chart"]]
            + " · "
            + esc(c["field"])
            + ((" · " + str(c["orientation"]) + "°") if c["chart"] == "mtf" else "")
            + "</h2>"
        )
        parts.append(
            "<p>중심/주변은 원본 DB 렌즈 시야 ID입니다. 각"
            " 위치는 별도 국소 가상 센서로 계산하며 실제 센서 전"
            "체 좌표를 식별하지 않습니다.</p>"
        )
        for stage, label in [
            ("raw", "RAW · 동일 표시 범위"),
            ("isp", "고정 DB ISP · 프레임별 처리 후 시간 평균"),
        ]:
            parts.append("<h3>" + label + '</h3><div class="three">')
            for role, name in [
                ("before", "보정 전"),
                ("observation", "가상 실측 · 독립 취득 A"),
                ("after", "보정 후"),
            ]:
                parts.append(
                    "<figure><figcaption>"
                    + name
                    + '</figcaption><img src="'
                    + image(root, c["series"][role]["images"][stage])
                    + '" alt="'
                    + esc(c["id"] + " " + stage + " " + role)
                    + '"></figure>'
                )
            parts.append("</div>")
        for key, m in c["metrics"].items():
            name, unit = METRICS[key]
            parts.append(
                "<h3>"
                + name
                + '</h3><p class="metric">RMSE '
                + fmt(m["before"]["rmse"])
                + " → "
                + fmt(m["after"]["rmse"])
                + " "
                + unit
                + " · 감소 "
                + fmt(m["reduction_percent"])
                + "% · 반복 A/B "
                + fmt(m["replicate"]["rmse"])
                + '</p><img src="'
                + image(root, c["plots"][key])
                + '" alt="'
                + esc(c["id"] + " " + key + " curve and residual")
                + '">'
            )
        if c["chart"] == "mtf":
            parts.append(
                '<p class="note">RAW SFR은 Gr 서브'
                "격자의 실제 관측값입니다. RAW·ISP 비교는 공통 "
                "관측 대역 0–0."
                "25 cycles/native pixel 이내이며, 아래 PSF 커널 OTF와 구분합니다.</p>"
            )
        parts.append("</section>")
    if "optics" in s["modules"]:
        for f in s["psf"]:
            parts.append(
                '<section data-chart="mtf"><h2>유효 PSF · '
                + esc(f["field_id"])
                + '</h2><p class="note">원래 DB의 sa'
                "mpled PSF와 가정한 합성 기준, 추정 Gauss"
                "ian PSF입니다"
                ". MTF만으로 고유한 물리 PSF를 복원한 결과가 아"
                "닙니다. 커널은 정규화 질량을 보존하며 그림은 각 격자"
                " pitch로 나눈"
                ' 밀도의 같은 log 구간으로 표시합니다.</p><div class="three">'
            )
            for role, title in [
                ("before", "보정 전 · DB sampled"),
                ("observation", "알려진 합성 기준"),
                ("after", "추정한 유효 PSF"),
            ]:
                parts.append(
                    "<figure><figcaption>"
                    + title
                    + '</figcaption><img src="'
                    + image(root, f["series"][role]["image"])
                    + '" alt="'
                    + esc(f["field_id"] + " PSF " + role)
                    + '"></figure>'
                )
            parts.append(
                "</div>"
                + table(
                    ["커널", "pitch(px)", "질량 합", "중심 x/y(px)", "2차 모멘트 σx/y(px)"],
                    [
                        [
                            role,
                            v["pitch_px"],
                            v["sum"],
                            str(v["centroid_px"]),
                            str(v["sigma_second_moment_px"]),
                        ]
                        for role, v in f["series"].items()
                    ],
                )
                + '<h3>적용한 PSF에서 재계산한 커널 MTF</h3><img src="'
                + image(root, f["plot"])
                + '" alt="'
                + esc(f["field_id"] + " kernel MTF")
                + '"></section>'
            )
    parts.append(
        "<section><h2>파라미터 회복 · 합성 정답을 "
        "아는 경우에만</h2><p>아래 truth는 생성기·평"
        "가기 전용입니다. "
        "피팅 입력에는 포함되지 않습니다. 실측 데이터에서는 이"
        " 표 대신 독립 차트 오차를 사용합니다. baselin"
        "e PSF는 Gau"
        "ssian 근사값으로 바꾸지 않고 원본 sampled 커널을 적용했습니다.</p>"
        + table(
            ["파라미터", "보정 전", "보정 후", "합성 truth", "절대 오차"],
            [
                [r["parameter"], r["before"], r["after"], r["truth"], r["absolute_error"]]
                for r in s["parameter_recovery"]
            ],
        )
        + "<h3>범위와 고정 조건</h3><ul>"
        + "".join("<li>" + esc(x) + "</li>" for x in s["scope"])
        + "</ul><details><summary>고정된 프로토콜·출처</summary><pre>"
        + esc(engine.canonical(s["protocol"]))
        + '</pre></details></section></main><script src="report-controls.js"></script></html>'
    )
    return "".join(parts).encode()


def publish(root, summary):
    if (root / "report.html").exists() or (root / "evidence.zip").exists():
        raise ValueError("Core reports are immutable; publish into a fresh workflow directory")
    (root / "report-controls.js").write_text(CONTROLS)
    summary["aggregate"] = aggregate(summary)
    for c in summary["comparisons"]:
        c["plots"] = {}
        for key, m in c["metrics"].items():
            folder = root / "plots"
            folder.mkdir(exist_ok=True)
            name = "plots/" + c["id"] + "-" + key + ".png"
            x = m.get("x", c["series"]["observation"]["x"])
            plot(
                root / name,
                x,
                m,
                {
                    "raw_mean_dn": "RAW mean",
                    "raw_variance_dn2": "RAW temporal variance",
                    "isp_mean_rgb": "ISP mean RGB",
                    "raw_sfr": "RAW Gr SFR",
                    "isp_sfr": "ISP SFR",
                }[key],
                METRICS[key][1],
                "cycles/native pixel"
                if c["chart"] == "mtf"
                else c["series"]["observation"]["x_unit"],
            )
            c["plots"][key] = name
    summary["psf"] = psf_evidence(root, summary) if "optics" in summary["modules"] else []
    stream = io.StringIO()
    writer = csv.DictWriter(
        stream, fieldnames=["parameter", "before", "after", "truth", "absolute_error", "source"]
    )
    writer.writeheader()
    writer.writerows(summary["parameter_recovery"])
    (root / "parameters.csv").write_text(stream.getvalue())
    (root / "report.html").write_bytes(render(root, summary))
    engine.save(root / "report.json", summary)
    files = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "progress.json"
    }
    engine.save(root / "manifest.json", {"schema": "camera_db_core_artifacts_v1", "files": files})
    with zipfile.ZipFile(root / "evidence.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in [*files, "manifest.json"]:
            z.write(root / name, name)
    with zipfile.ZipFile(root / "evidence.zip") as z:
        if z.testzip() is not None or any(
            hashlib.sha256(z.read(name)).hexdigest() != h for name, h in files.items()
        ):
            raise ValueError("Core evidence ZIP failed checksum verification")
