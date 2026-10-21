"""Portable numerical convergence evidence, included in the unified report ZIP."""

from __future__ import annotations

import csv
import io

import numpy as np
from PIL import Image

from . import calibration_db_core_report as base


def maximum(row, metric, stage=None):
    values = [
        c.get(metric) for c in row["comparison"]["checks"] if stage is None or c["stage"] == stage
    ]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def render(root, s):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    from .calibration_sensor_crops_report import render as crop_report

    chosen = s["selected"]
    candidates = s["candidates"]
    selected = next((c for c in candidates if chosen and c["id"] == chosen["id"]), None)
    reference = next(r for r in s["references"] if r["id"] == s["reference_id"])
    original = next(c for c in candidates if c["id"] == "q4-s5")
    table_rows = []
    csv_rows = []
    for c in sorted(candidates, key=lambda c: c["fft_work_proxy"]):
        ratio = c["fft_work_proxy"] / original["fft_work_proxy"]
        row = [
            c["id"],
            "선택" if chosen and c["id"] == chosen["id"] else "통과" if c["passed"] else "미통과",
            maximum(c, "max_abs", "kernel"),
            maximum(c, "max_abs", "raw"),
            maximum(c, "max_abs", "isp"),
            maximum(c, "mtf50_relative"),
            max(f["cost"]["tail_energy_bound"] for f in c["fields"].values()),
            ratio,
        ]
        table_rows.append(row)
        csv_rows.append(
            dict(
                zip(
                    (
                        "setting",
                        "status",
                        "kernel_mtf_max_abs",
                        "raw_mtf_max_abs",
                        "isp_mtf_max_abs",
                        "mtf50_relative_max",
                        "tail_energy_bound",
                        "fft_work_relative_to_q4_s5",
                    ),
                    row,
                    strict=True,
                )
            )
        )
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(csv_rows[0]))
    writer.writeheader()
    writer.writerows(csv_rows)
    (root / "convergence" / "candidates.csv").write_text(stream.getvalue())
    title = "통과 · " + chosen["id"] + " 자동 선택" if chosen else "수렴 미확립 · 자동 적용 차단"
    parts = [
        crop_report(root, s),
        (
            '<section id="psf-convergence" data-chart="convergence">'
            "<p>NUMERICAL PSF QUALIFICATION</p><h2>PSF 샘플링·범위 수렴</h2"
            "><h3>"
        )
        + base.esc(title)
        + "</h3>",
        (
            "<p>저장된 시야별 128×128 ROI/Crop, 0°/90° 에지, 0/0.25 pixel 위"
            "치를 비교했습니다. 고정된 보정 파라미터로 잡음 없는 정수 RAW와 "
            "동일 ISP를 생성합니다. 이후"
            " 별도 잡음 seed의 전체 카메라 취득에 선택 설정을 적용합니다.</p>"
        ),
        (
            "<p>기준: MTF 절대 차이 ≤0.005, MTF50 상대 차이 ≤1%, 잘린 PSF 에너지 상한"
            " ≤0.1%. RAW Gr는 0–0.25, ISP G·커널은 0–0.5 cycles/native p"
            "ixel입니다. MTF50이 대역 안에서 검출되지 않으면 미통과입니다.</p>"
        ),
        (
            "<p>비교 기준은 q16·±8σ입니다. q8→q16 및 ±6σ→"
            "±8σ가 각각 먼저 통과해야 합니다."
            " 선택은 후보 16개 중 모든 검증 시야의 추정 FFT 계산량 합이 최소인 "
            "설정입니다. 센서 Crop 연결 작업은 9개 Crop과 기존 2"
            "개 가상 ROI를 모두 포함합니다. 실제 실행 시간은 "
            "선택 기준이 아니며 메모리 값은 예산 추정치입니다.</p>"
        ),
        (
            '<p><a href="convergence/result.json">수렴 전체 결과 JSON ↓</a'
            '> · <a href="convergence/candidates.csv">후보 판정 CSV ↓</a'
            "></p>"
        ),
        base.table(
            ["기준 확인", "비교", "판정"],
            [
                [r["axis"], r["from"] + " → " + r["to"], "통과" if r["passed"] else "미통과"]
                for r in s["reference_checks"]
            ],
        ),
        base.table(
            [
                "설정 q·σ",
                "판정",
                "커널 ΔMTF 최대",
                "RAW ΔMTF 최대",
                "ISP ΔMTF 최대",
                "ΔMTF50 상대",
                "꼬리 에너지 상한",
                "계산량 / 기존 q4·5σ",
            ],
            table_rows,
        ),
    ]
    if selected:
        parts.append(
            base.table(
                [
                    "시야",
                    "선택 PSF 크기",
                    "샘플 간격 (pixel)",
                    "작업 메모리 추정 (MiB)",
                    "기존 대비 계산량",
                ],
                [
                    [
                        fid,
                        " × ".join(map(str, f["cost"]["kernel_shape"])),
                        f["cost"]["pitch_px"],
                        f["cost"]["working_bytes_estimate"] / 1024**2,
                        f["cost"]["fft_work_proxy"]
                        / original["fields"][fid]["cost"]["fft_work_proxy"],
                    ]
                    for fid, f in selected["fields"].items()
                ],
            )
        )
        plots = []
        for fid in selected["fields"]:
            for cid in selected["fields"][fid]["captures"]:
                angle = cid.split("-")[1]
                fig, axes = plt.subplots(2, 3, figsize=(12, 6), constrained_layout=True)
                for col, stage in enumerate(("kernel", "raw", "isp")):

                    def curve(record, fid=fid, angle=angle, stage=stage, cid=cid):
                        field = record["fields"][fid]
                        return (
                            field["kernel_curves"][angle]
                            if stage == "kernel"
                            else field["captures"][cid]["curves"][stage]
                        )

                    ref = curve(reference)
                    for row, label, color in [
                        (original, "previous q4 s5", "#b96b37"),
                        (selected, "selected " + selected["id"], "#187b65"),
                        (reference, "reference q16 s8", "#333333"),
                    ]:
                        c = curve(row)
                        axes[0, col].plot(c["x"], c["y"], label=label, color=color)
                        if row is not reference:
                            axes[1, col].plot(
                                ref["x"],
                                np.interp(ref["x"], c["x"], c["y"]) - np.asarray(ref["y"]),
                                label=label,
                                color=color,
                            )
                    axes[0, col].set(
                        title=stage.upper() + " MTF",
                        ylabel="normalized response",
                        ylim=(-0.03, 1.1),
                    )
                    axes[1, col].axhline(0.005, color="gray", ls=":")
                    axes[1, col].axhline(-0.005, color="gray", ls=":")
                    axes[1, col].set(xlabel="cycles/native pixel", ylabel="difference to reference")
                    axes[0, col].legend(fontsize=7)
                    axes[0, col].grid(alpha=0.2)
                    axes[1, col].grid(alpha=0.2)
                fig.suptitle(fid + " / " + cid)
                name = "convergence/" + fid + "-" + cid + "-curves.png"
                fig.savefig(root / name, dpi=120)
                plt.close(fig)
                plots.append(name)
                parts.append(
                    "<h3>"
                    + base.esc(fid + " · " + cid)
                    + '</h3><img src="'
                    + base.image(root, name)
                    + '" alt="'
                    + base.esc(fid + " " + cid + " convergence MTF and residual")
                    + '">'
                )
                if cid.endswith("phase-00"):
                    for stage in ("raw", "isp"):
                        parts.append(
                            "<h4>" + stage.upper() + ' · 동일 표시 범위</h4><div class="three">'
                        )
                        for row, label in [
                            (original, "기존 q4·5σ"),
                            (selected, "자동 선택"),
                            (reference, "수렴 기준"),
                        ]:
                            with np.load(
                                root / row["fields"][fid]["captures"][cid]["file"],
                                allow_pickle=False,
                            ) as z:
                                values = (
                                    z["raw"] / 1024
                                    if stage == "raw"
                                    else np.clip(z["linear"], 0, 1) ** (1 / 2.2)
                                )
                            name = f"convergence/{fid}-{cid}-{row['id']}-{stage}.png"
                            Image.fromarray(
                                np.rint(np.clip(values, 0, 1) * 255).astype(np.uint8)
                            ).save(root / name)
                            parts.append(
                                "<figure><figcaption>"
                                + label
                                + '</figcaption><img src="'
                                + base.image(root, name)
                                + '" alt="'
                                + base.esc(fid + " " + cid + " " + stage + " " + row["id"])
                                + '"></figure>'
                            )
                        parts.append("</div>")
        s["plots"] = plots
    parts.append(
        '<p class="note">검증 범위: 이미 보정된 Gaussian 유효 PSF의 수치 근사입니다'
        ". 원본 DB의 픽셀 적분 PSF는 그대로 유지하며, 그 원본의 더 미세한 PSF나 실제 카메라 수"
        "차를 복원한 검증은 아닙니다. 좌표가 연결된 Crop의 보간 가"
        "정은 별도로 표시하며 장거리 flare 검증은 포함하지 않"
        "습니다.</p></section>"
    )
    return "".join(parts)
