"""Portable evidence for direct prescription PSFs and located RAW/ISP convergence."""

from __future__ import annotations

import csv

import numpy as np
from PIL import Image

from . import calibration_db_core_report as html


def maximum(row, stage=None, metric="max_abs"):
    values = [
        c.get(metric) for c in row["comparison"]["checks"] if stage is None or c["stage"] == stage
    ]
    return max((v for v in values if v is not None), default=None)


def render(root, s):
    if not s:
        return ""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.patches import Rectangle

    g = s["geometry"]
    selected = s["selected"]
    chosen = next((c for c in s["candidates"] if selected and c["id"] == selected["id"]), None)
    reference = next(r for r in s["references"] if r["id"] == s["reference_id"])
    folder = root / "direct-convergence"
    folder.mkdir(exist_ok=True)
    fig, ax = plt.subplots(figsize=(12, 7), constrained_layout=True)
    h, w = g["full_sensor_shape"]
    ax.add_patch(Rectangle((0, 0), w, h, fill=False, edgecolor="#334155"))
    for c in g["crops"]:
        row, col = c["origin_row_col"]
        ax.add_patch(Rectangle((col, row), 128, 128, color="#008b78", alpha=0.3))
        bottom = row > 800
        ax.text(
            col + 64,
            row - 15 if bottom else row + 150,
            c["id"] + f"\n({row}, {col})",
            ha="center",
            va="bottom" if bottom else "top",
            fontsize=9,
        )
    ay, axcol = g["optical_axis_active_row_col"]
    ax.plot(axcol, ay, "x", color="black")
    ax.set(
        xlim=(-40, w + 40),
        ylim=(h + 40, -40),
        xlabel="active column / native pixel",
        ylabel="active row / native pixel",
        title="9 directly traced PSF anchors on the active sensor",
    )
    ax.set_aspect("equal")
    fig.savefig(folder / "sensor-map.png", dpi=125)
    plt.close(fig)
    parts = [
        '<section id="direct-psf" data-chart="convergence"><p>DIRECT DB LENS PSF</p>',
        "<h2>원래 9개 Crop · DB 렌즈 직접 PSF 수치 검증</h2>",
        "<h3>"
        + html.esc(
            "통과 · " + selected["id"]
            if selected and s["status"] == "passed"
            else "수렴 미확립 · 적용 차단"
        )
        + "</h3>",
        "<p>각 센서 앵커의 550nm 주광선이 지정 이미지 좌표에 도달하도록 물체각을 역산한 뒤, "
        "450–650nm의 21개 파장을 각각 직접 추적했습니다. 파장별 구면 기준 OPD·회절 PSF를 "
        "D65와 보정 QE로 가중하여 R·G·B 광학 PSF를 만듭니다"
        ". 위치 사이 PSF 보간과 Gaussian 근사는 사용하지 않"
        "습니다.</p>",
        "<p>전자 파라미터·QE·CCM은 통합 프로파일의 보정값을 적용"
        "합니다. 렌즈 PSF에는 픽셀 면적 적분을 포함하지 않으며, "
        "광학 합성 이후 native 픽셀 면적을 한 번 적분합니다. 독"
        "립적으로 식별되지 않은 전하 확산은 0 가정입니다.</p>",
        "<p><b>MTF 시험 조건:</b> 픽셀당 32,000개의 정"
        "규화 D65 광자량을 사용합니다. 위치별 절대 조도·비네팅 보정"
        " 검증은 별도입니다. "
        "기존 두 가상 벤치의 Gaussian 보정·holdout 결과는"
        " 아래 원래 항목에 보존됩니다. 새 9개 위치에서는 직접 렌즈 "
        "PSF가 적용됩니다.</p>",
        "<p>수렴 기준: MTF 절대 차이 ≤0.005, 대역 내 MT"
        "F50 상대 차이 ≤1%, 유한 기준 PSF 대비 잘린 에너지 "
        "차이 ≤0.1%. "
        "q8→q16 및 반경 64→96 pixel의 기준 검사를 먼저 "
        "통과해야 합니다. RAW Gr 대역은 0–0.25, "
        "광학·ISP G 대역은 0–0.5 cycles/native pi"
        "xel입니다. RAW MTF50이 대역 밖이면 값을 만들지 않고"
        " 표시합니다.</p>",
        "<p>수렴 기준 PSF 밖의 잔여 에너지는 미확립이며, 원래 D"
        "B pupil 검증 조건(PSF L1 ≤0.03, OPD 보간 "
        "오차 ≤0.02파장, "
        "위상 격자 변화 ≤0.25파장, 보존 에너지 0.95–1.001)을 파장별로 별도 검사합니다. "
        "센서 장착 축·RGGB 위상과 Crop 내 국소 정상 PSF는 설계 가정입니다.</p>",
        '<p><a href="direct-convergence/result.json">직접 PSF 수렴 JSON ↓</a> · '
        '<a href="direct-optics/geometry.json">고정 렌즈·센서·파장 입력 ↓</a></p>',
        '<img src="'
        + html.image(root, "direct-convergence/sensor-map.png")
        + '" alt="Nine sensor crop coordinates with direct lens PSF anchors">',
        html.table(
            ["위치", "시작 행·열", "광학 x·y (mm)", "물체각 (deg)", "주광선 위치 오차 (mm)"],
            [
                [
                    c["id"],
                    str(c["origin_row_col"]),
                    str(c["image_xy_mm"]),
                    c["object_angle_deg"],
                    c["chief_anchor_residual_mm"],
                ]
                for c in g["crops"]
            ],
        ),
        "<p>렌즈 "
        + html.esc(g["lens_key"])
        + ", 적용 조리개 F/"
        + html.esc(g["lens_source"]["pupil_condition"]["effective_f_number"])
        + " · 원본 처방 SHA "
        + html.esc(g["lens_source"]["prescription_sha256"])
        + "</p>",
        html.table(
            ["기준 확인", "비교", "판정"],
            [
                [r["axis"], r["from"] + " → " + r["to"], "통과" if r["passed"] else "미통과"]
                for r in s["reference_checks"]
            ],
        ),
    ]
    rows = []
    for c in sorted(s["candidates"], key=lambda c: c["operation_proxy"]):
        rows.append(
            [
                c["id"],
                "선택"
                if selected and selected["id"] == c["id"]
                else "통과"
                if c["passed"]
                else "미통과",
                maximum(c, "optical"),
                maximum(c, "raw"),
                maximum(c, "isp"),
                maximum(c, "mass"),
                maximum(c, metric="mtf50_relative"),
                c["operation_proxy"],
            ]
        )
    parts.append(
        html.table(
            [
                "설정",
                "판정",
                "광학 ΔMTF",
                "RAW ΔMTF",
                "ISP ΔMTF",
                "유한 기준 대비 에너지 차이",
                "대역 내 ΔMTF50 상대",
                "연산량 추정",
            ],
            rows,
        )
    )
    with (folder / "candidates.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "setting",
                "status",
                "optical_mtf_max_abs",
                "raw_mtf_max_abs",
                "isp_mtf_max_abs",
                "relative_mass_error",
                "in_band_mtf50_relative",
                "operation_proxy",
            ]
        )
        writer.writerows(rows)
    parts.append(
        '<p><a href="direct-convergence/cand'
        'idates.csv">후보 판정 CSV ↓</a> · 광학 강도'
        " Nyquist를 만족하지 않는 "
        "거친 격자는 MTF 비교 전에 거부합니다. 연산량은 PSF 투영"
        "·CDF 조회·픽셀 적분의 유한 후보 비교값입니다.</p>"
    )
    parity = s["crop_parity"]
    parts.append(
        "<p><b>확장 영역 Crop 일치: "
        + ("통과" if parity["passed"] else "미통과")
        + "</b> · "
        + str(len(parity["checks"]))
        + "개 조합. 광학 의존 영역은 센서 밖의 연속 이미지 평면까지 허용하지만, "
        "RAW·ISP halo는 실제 유효 센서 안에서 처리한 뒤 12"
        "8×128로 자릅니다. 이 국소 시험에서는 전체 프레임 배열을 생성하지 않습니다.<"
        "/p>"
    )
    if chosen:
        parts.append(
            html.table(
                ["위치", "커널 크기", "간격 (pixel)", "작업 메모리 추정 (MiB)"],
                [
                    [
                        f,
                        " × ".join(map(str, d["cost"]["kernel_shape"])),
                        d["cost"]["pitch_px"],
                        d["cost"]["working_bytes_estimate"] / 2**20,
                    ]
                    for f, d in chosen["fields"].items()
                ],
            )
        )
        for applied in s["applied_kernels"]:
            fid = applied["field"]
            with np.load(root / applied["file"], allow_pickle=False) as z:
                k = z["mass_rgb"]
            q = chosen["setting"]["samples_per_pixel"]
            r = chosen["setting"]["radius_native_px"]
            fig, axes = plt.subplots(1, 2, figsize=(11, 4), constrained_layout=True)
            values = k[..., 1] / k[..., 1].max()
            axes[0].imshow(
                np.log10(np.maximum(values, 1e-8)),
                extent=(-r - 0.5 / q, r + 0.5 / q, r + 0.5 / q, -r - 0.5 / q),
                vmin=-6,
                vmax=0,
                cmap="magma",
            )
            axes[0].set(
                xlim=(-8, 8),
                ylim=(8, -8),
                xlabel="native pixel x",
                ylabel="native pixel row",
                title=fid + " / D65-QE weighted lens PSF / log10",
            )
            for name, color in [("R", "#b85845"), ("G", "#187b65"), ("B", "#3b6da8")]:
                curve = chosen["fields"][fid]["kernel_curves"][name + "-0"]
                axes[1].plot(curve["x"], curve["y"], label=name, color=color)
            axes[1].set(
                title="optical horizontal MTF", xlabel="cycles/native pixel", ylim=(0, 1.05)
            )
            axes[1].legend()
            name = "direct-convergence/" + fid + "-psf.png"
            fig.savefig(root / name, dpi=120)
            plt.close(fig)
            parts.append(
                "<h3>"
                + html.esc(fid)
                + " · 직접 계산 PSF</h3><p>표시는 ±8 pixel 확대, 계산 커널은 ±"
                + str(r)
                + " pixel · q"
                + str(q)
                + '</p><img src="'
                + html.image(root, name)
                + '" alt="'
                + fid
                + ' direct prescription spectral PSF">'
            )
            for cid, capture in chosen["fields"][fid]["captures"].items():
                angle = cid.split("-")[1]
                fig, axes = plt.subplots(2, 3, figsize=(12, 6), constrained_layout=True)
                for col, stage in enumerate(("optical", "raw", "isp")):

                    def get(row, fid=fid, cid=cid, stage=stage, angle=angle):
                        field = row["fields"][fid]
                        return (
                            field["kernel_curves"]["G-" + angle]
                            if stage == "optical"
                            else field["captures"][cid]["curves"][stage]
                        )

                    ref = get(reference)
                    for row, label, color in [
                        (chosen, "selected " + chosen["id"], "#187b65"),
                        (reference, "reference " + reference["id"], "#333333"),
                    ]:
                        curve = get(row)
                        axes[0, col].plot(curve["x"], curve["y"], label=label, color=color)
                        if row is chosen:
                            axes[1, col].plot(
                                ref["x"],
                                np.interp(ref["x"], curve["x"], curve["y"]) - np.asarray(ref["y"]),
                                color=color,
                            )
                    axes[0, col].set(title=stage.upper() + " MTF", ylim=(-0.03, 1.1))
                    axes[0, col].legend(fontsize=7)
                    for limit in (-0.005, 0.005):
                        axes[1, col].axhline(limit, color="gray", ls=":")
                    axes[1, col].set(xlabel="cycles/native pixel", ylabel="difference to reference")
                fig.suptitle(fid + " / " + cid)
                name = f"direct-convergence/{fid}-{cid}-curves.png"
                fig.savefig(root / name, dpi=120)
                plt.close(fig)
                parts.append(
                    "<h4>"
                    + html.esc(fid + " · " + cid)
                    + '</h4><img src="'
                    + html.image(root, name)
                    + '" alt="'
                    + html.esc(fid + " " + cid + " direct PSF RAW ISP convergence")
                    + '">'
                )
                if cid.endswith("phase-00"):
                    parts.append('<div class="three">')
                    with np.load(root / capture["file"], allow_pickle=False) as z:
                        values = [
                            (z["raw"] / 4095, "RAW"),
                            (np.clip(z["linear"], 0, 1) ** (1 / 2.2), "ISP"),
                        ]
                    for value, label in values:
                        name = f"direct-convergence/{fid}-{cid}-{label}.png"
                        Image.fromarray(np.rint(np.clip(value, 0, 1) * 255).astype(np.uint8)).save(
                            root / name
                        )
                        parts.append(
                            "<figure><figcaption>"
                            + label
                            + '</figcaption><img src="'
                            + html.image(root, name)
                            + '" alt="'
                            + fid
                            + " "
                            + cid
                            + " direct "
                            + label
                            + '"></figure>'
                        )
                    parts.append("</div>")
    parts.append("<details><summary>189개 위치·파장별 직접 pupil 검증</summary>")
    parts.append(
        html.table(
            [
                "위치",
                "파장 nm",
                "pupil 점수",
                "PSF L1 최대",
                "OPD 오차 (파장)",
                "에너지",
                "입력 광선",
            ],
            [
                [
                    f["field"],
                    w["wavelength_nm"],
                    str(w["physical_qualification"]["pupil_samples"]),
                    max(w["physical_qualification"]["kernel_l1_changes"]),
                    w["physical_qualification"]["unwrapped_opd_interpolation"]["maximum_abs_waves"],
                    w["reference"]["retained_energy"],
                    w["rays_file"],
                ]
                for f in s["source"]["fields"]
                for w in f["wavelengths"]
            ],
        )
    )
    parts.append(
        '</details><p class="note">DB 특허 렌즈·'
        "재질 모델에 대한 수치 검증이며 제품 렌즈 또는 실측 카메라 검"
        "증은 미확립입니다. "
        "MTF 일치만으로 실제 카메라의 고유 PSF를 식별한 결과로 해석할 수 없습니다.</p></section>"
    )
    return "".join(parts)
