"""Portable fixed41/adaptive comparison, including placement and validation boundaries."""

from __future__ import annotations

import csv

import numpy as np
from PIL import Image

from . import calibration_db_core_report as html


def maxima(rows):
    return [max(max(v["psf_l1_rgb"]) for v in rows)] + [
        max(c["max_abs"] for v in rows for c in v["comparison"]["checks"] if c["stage"] == stage)
        for stage in ("optical", "raw", "isp")
    ]


def render(root, s, baseline, profile=None, resources=None):
    if not s:
        return ""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    folder = root / "adaptive-anchors"

    def table(headers, rows):
        return html.table(headers, rows)

    def reason(step):
        if step["added_field"] is None:
            return "선택용 미사용 점 기준 통과"
        c = next(v for v in step["checks"] if v["field"] == step["added_field"])
        if not c["comparison"]["checks"]:
            return "PSF L1 실패 · MTF 미평가"
        failed = [v for v in c["comparison"]["checks"] if not v["passed"]]
        return (
            ", ".join(
                sorted(
                    {
                        v["stage"]
                        + (" MTF50" if (v.get("mtf50_relative") or 0) > 0.01 else " MTF curve")
                        for v in failed
                    }
                )
            )
            or "PSF L1"
        )

    def image(name, alt):
        return (
            '<figure><img src="'
            + html.image(root, "adaptive-anchors/" + name)
            + '" alt="'
            + html.esc(alt)
            + '"></figure>'
        )

    a = s["adaptive_application"]
    b = baseline["application"]
    cost = s["cost"]
    parts = [
        (
            '<section id="adaptive-anchors" data-char'
            't="convergence"><h2>오차 기반 적응형 PSF 앵커 · 고'
            "정 41점 비교</h2>"
        ),
        "<p><b>"
        + ("통과" if s["status"] == "passed" else "미수렴 · 고정 41점 유지")
        + "</b> · 적용 "
        + str(s["selected_anchor_count"])
        + "점 · "
        + s["selected_mode"]
        + "</p>",
        (
            "<p>센서 끝을 덮는 9점에서 시작해, 선택용 직접 PSF 41점 중 오"
            "차/허용치 비율이 가장 큰 실패 위치 하나만 추가합니다. PSF L1이 "
            "실패한 점은 MTF 미평가로 명시하며, 통과할 수 있는 점에는 광학·RA"
            "W·ISP MTF 기준을 모두 적용합니다. 같은 전자·QE·CCM, q8"
            "·반경 64 pixel, D65·무잡음 조건입니다. 배치를 고정한 후 다"
            "른 16개 직접 계산점으로 검증합니다. 검증점으로 배치를 수정하지 않습니"
            "다. 유한 후보 집합의 탐욕적 배치이며 전역 최소 비용을 보장하지 않습니"
            "다.</p>"
        ),
        (
            "<p>허용치: PSF L1 ≤ 0.03, 광학/RAW/ISP MTF 최대"
            " 오차 ≤ 0.005, 대역 안 MTF50 상대 차이 ≤ 1%. RAW "
            "Gr은 0–0.25, 광학·ISP G는 0–0.5 cy/native pi"
            "xel입니다. 동일한 별도 16점의 오차를 아래에서 비교합니다.</p>"
        ),
        table(
            ["배치", "앵커 수", "PSF L1 최대", "광학 ΔMTF 최대", "RAW ΔMTF 최대", "ISP ΔMTF 최대"],
            [
                ["고정 41", 41, *maxima(s["baseline_heldout_checks"])],
                ["적응형", len(a["anchors"]), *maxima(s["heldout_checks"])],
            ],
        ),
        table(
            ["비용 항목", "고정 41", "적응형"],
            [
                ["실행 앵커 수", 41, len(a["anchors"])],
                ["삼각형 수", len(b["triangles"]), len(a["triangles"])],
                [
                    "실행 커널 합계 (비압축 MiB)",
                    cost["baseline_kernel_bytes"] / 2**20,
                    cost["runtime_kernel_bytes"] / 2**20,
                ],
                ["커널 LRU 한도 (MiB)", 48.09379577636719, 48.09379577636719],
                ["FFT LRU 한도 (MiB)", 96, 96],
                [
                    "광학 배열 상한 추정 (MiB)",
                    cost["baseline"]["maximum_array_working_bytes_estimate"] / 2**20,
                    cost["adaptive"]["maximum_array_working_bytes_estimate"] / 2**20,
                ],
                [
                    "4개 전체 프레임 원래 계산 합계 (초)",
                    cost["baseline_original_render_seconds"],
                    cost["adaptive_original_render_seconds"],
                ],
                ["추가 적응 배치 탐색 (초)", 0, s["selection_seconds"]],
                ["직접 PSF 사전 계산 풀 (공유)", 57, 57],
            ],
        ),
        (
            ("<p><b>이번 조건은 비용 절감이 없어 검증된 고정 41점을 적용합니다.</b></p>")
            if s["selected_mode"] == "fixed_41_no_cost_reduction"
            else ""
        )
        + (
            "<p>계산 시간은 각 프레임의 최초 계산 시간을 합한 값이며 병렬 wal"
            "l time이나 동일 부하 성능 벤치마크가 아닙니다. 캐시 복원 시간은 "
            "원래 계산 시간과 구분해 각 프레임 메타데이터에 보존합니다. 배열 상한은"
            " 프로세스 RSS가 아닙니다. 선택 탐색은 추가 비용이며, 이번 검증은 "
            "이미 직접 계산한 57점 풀을 공유하므로 사전 계산 자체를 줄였다는 주장"
            "이 아닙니다.</p>"
        ),
    ]
    if resources:
        parts.append(
            table(
                ["작업 프로세스 관측 · 보고서 생성 시작 시점", "값"],
                [
                    ["현재 RSS (MiB)", resources["rss_bytes"] / 2**20],
                    [
                        "그 시점까지 최고 RSS (MiB)",
                        None
                        if resources["peak_rss_bytes"] is None
                        else resources["peak_rss_bytes"] / 2**20,
                    ],
                    ["사용자 CPU 합계 (초)", resources["user_cpu_seconds"]],
                    ["시스템 CPU 합계 (초)", resources["system_cpu_seconds"]],
                ],
            )
        )
        parts.append(
            "<p>이 관측값은 통합 작업 프로세스 전체의 누적 값입니다. "
            "고정/적응형 렌더러 각각의 RSS로 분해하지 않으며, "
            "보고서 생성 이후의 최고 메모리까지 측정한 값도 아닙니다.</p>"
        )
    (fig, axs) = plt.subplots(1, 2, figsize=(13, 5), constrained_layout=True)
    for ax, app, title in zip(
        axs, [b, a], ["Fixed 41", f"Adaptive {len(a['anchors'])}"], strict=True
    ):
        for tri in app["triangles"]:
            pts = np.array([app["anchors"][v] for v in tri + [tri[0]]])
            ax.plot(pts[:, 1], pts[:, 0], color="#8c9aa8", lw=0.5)
        pts = np.array(list(app["anchors"].values()))
        ax.scatter(pts[:, 1], pts[:, 0], s=18, label="Runtime anchors")
        held = np.array([v["position"] for v in s["heldout_checks"]])
        ax.scatter(held[:, 1], held[:, 0], s=30, marker="x", label="Held-out direct probes")
        ax.invert_yaxis()
        ax.set_aspect("equal")
        ax.set_title(title)
        ax.set_xlabel("Native sensor column")
        ax.set_ylabel("Native sensor row")
        ax.legend(fontsize=8)
    fig.savefig(folder / "anchor-map.png", dpi=130)
    plt.close(fig)
    parts.append(image("anchor-map.png", "Fixed 41 and adaptive native sensor anchor placement"))
    csvrows = []
    for mode, checks in [
        ("fixed41", s["baseline_heldout_checks"]),
        ("adaptive", s["heldout_checks"]),
    ]:
        for v in checks:
            vals = maxima([v])
            csvrows.append(
                dict(
                    mode=mode,
                    field=v["field"],
                    row=v["position"][0],
                    col=v["position"][1],
                    psf_l1=vals[0],
                    optical_mtf=vals[1],
                    raw_mtf=vals[2],
                    isp_mtf=vals[3],
                    passed=v["passed"],
                )
            )
    with (folder / "heldout-errors.csv").open("w", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=list(csvrows[0]))
        w.writeheader()
        w.writerows(csvrows)
    parts.append(
        '<p><a href="adaptive-anchors/heldout-errors.csv">별도 16점 · PSF·MTF 오차 CSV ↓</a></p>'
    )
    parts.append(
        "<details><summary>오차가 큰 점의 순차 추가 이력</summary>"
        + table(
            [
                "현재 앵커 수",
                "다음 추가 위치",
                "최대 허용치 비율",
                "추가 이유",
                "선택용 남은 점 통과",
            ],
            [
                [
                    v["anchor_count"],
                    v["added_field"] or "종료",
                    max([c["score"] for c in v["checks"]], default=0),
                    reason(v),
                    v["passed"],
                ]
                for v in s["history"]
            ],
        )
        + "</details>"
    )
    worst = max(s["heldout_checks"], key=lambda v: max(v["psf_l1_rgb"]))
    reference_probe = next(v for v in s["baseline_heldout_checks"] if v["field"] == worst["field"])
    if "measured" in worst:
        fig, axes = plt.subplots(1, 3, figsize=(13, 4), constrained_layout=True)
        for ax, stage in zip(axes, ("optical", "raw", "isp"), strict=True):
            for label, measured in (
                ("Direct reference", worst["reference"]),
                ("Fixed 41", reference_probe["measured"]),
                ("Adaptive", worst["measured"]),
            ):
                curve = (
                    measured["kernel_curves"]["G-0"]
                    if stage == "optical"
                    else measured["captures"]["edge-0-phase-00"]["curves"][stage]
                )
                ax.plot(curve["x"], curve["y"], label=label)
            ax.set_title(stage.upper() + " · " + worst["field"])
            ax.set_xlabel("Cycles / native pixel")
            ax.set_ylabel("MTF / SFR")
            ax.legend(fontsize=8)
        fig.savefig(folder / "worst-heldout-curves.png", dpi=130)
        plt.close(fig)
        parts.append(
            image("worst-heldout-curves.png", "Worst held-out PSF point optical RAW ISP curves")
        )
        parts.append(
            "<p>예시 곡선은 PSF L1이 가장 큰 별도 검증점의 G 광학·RAW Gr·ISP G "
            "0도/위상 0 조건입니다. 표와 판정에는 모든 방향·위상·광학 RGB를 포함합니다.</p>"
        )
    if baseline.get("kernels"):
        from . import calibration_anchor_density as density

        bank = density.KernelBank(root, baseline["kernels"])
        direct_kernel = bank[worst["field"]]
        direct_kernel = direct_kernel / direct_kernel.sum(axis=(0, 1), dtype=float)
        kernels = [
            direct_kernel,
            density.mixture(b, bank, *worst["position"]),
            density.mixture(a, bank, *worst["position"]),
        ]
        fig, axes = plt.subplots(1, 3, figsize=(12, 4), constrained_layout=True)
        peak = max(k[..., 1].max() for k in kernels)
        for ax, label, k in zip(
            axes, ("Direct reference", "Fixed 41", "Adaptive"), kernels, strict=True
        ):
            ax.imshow(
                np.log10(np.maximum(k[448:577, 448:577, 1] / peak, 1e-6)),
                origin="lower",
                extent=[-8, 8, -8, 8],
                vmin=-6,
                vmax=0,
            )
            ax.set_title(label + " · Green PSF")
            ax.set_xlabel("Native pixel offset")
        fig.savefig(folder / "worst-heldout-psf.png", dpi=130)
        plt.close(fig)
        parts.append(
            image(
                "worst-heldout-psf.png",
                "Direct fixed41 adaptive Green PSF common log scale central 16 pixels",
            )
        )
        parts.append(
            "<p>PSF 이미지는 중앙 ±8 native pixel을 공통 log 강도로 표시합니다. "
            "PSF L1은 전체 ±64 pixel 커널에서 계산했습니다.</p>"
        )
    full = s["full_sensor_comparisons"]
    parts.append(
        "<h3>전체 센서 25개 Crop × 방향·위상 4조건</h3>"
        + table(
            ["항목", "최대 차이 · 판정"],
            [
                [stage + " MTF", max(c["mtf"][stage]["max_abs"] for c in full)]
                for stage in ("raw", "isp")
            ]
            + [
                ["25 ROI RAW 영상 최대 차이 (DN)", max(c.get("raw_max_abs_dn", 0) for c in full)],
                ["25 ROI ISP 영상 최대 차이", max(c.get("isp_max_abs", 0) for c in full)],
                [
                    "독립 Crop RAW/ISP 일치",
                    str(sum(c["passed"] for c in s["independent_window_checks"])) + "/100",
                ],
                ["128↔96 타일 RAW 최대차 (DN)", s["partition_check"]["raw_max_abs_dn"]],
                ["128↔96 타일 ISP 최대차", s["partition_check"]["isp_max_abs"]],
            ],
        )
    )
    fullrows = [
        dict(
            field=c["field"],
            chart=c["chart"],
            raw_mtf_max=c["mtf"]["raw"]["max_abs"],
            isp_mtf_max=c["mtf"]["isp"]["max_abs"],
            raw_image_max_dn=c.get("raw_max_abs_dn"),
            isp_image_max_abs=c.get("isp_max_abs"),
            raw_passed=c["mtf"]["raw"]["passed"],
            isp_passed=c["mtf"]["isp"]["passed"],
        )
        for c in full
    ]
    with (folder / "full-sensor-mtf.csv").open("w", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=list(fullrows[0]))
        w.writeheader()
        w.writerows(fullrows)
    parts.append(
        '<p><a href="adaptive-anchors/full-sensor-mtf.csv">전체 센서 100개 MTF 비교 CSV ↓</a></p>'
    )
    for stage in ("raw", "isp"):
        (fig, ax) = plt.subplots(figsize=(11, 4), constrained_layout=True)
        for chart in dict.fromkeys(c["chart"] for c in full):
            vals = [c for c in full if c["chart"] == chart]
            ax.plot(range(len(vals)), [c["mtf"][stage]["max_abs"] for c in vals], ".-", label=chart)
        ax.axhline(0.005, color="red", ls="--", label="Limit")
        ax.set_xlabel("Native ROI index (5 x 5)")
        ax.set_ylabel("Max absolute MTF difference")
        ax.set_title("Adaptive vs fixed41 · " + stage.upper())
        ax.legend(fontsize=8)
        fig.savefig(folder / (stage + "-mtf-error.png"), dpi=130)
        plt.close(fig)
        parts.append(image(stage + "-mtf-error.png", stage + " full sensor MTF comparison"))
    for mode, f in [
        ("fixed41", next(v for v in baseline["captures"] if v["grid_size"] == "refined")),
        ("adaptive", s["captures"][0]),
    ]:
        for stage in ("raw", "linear"):
            im = np.load(root / f[stage], mmap_mode="r")
            im = np.asarray(im, dtype=float)
            im = (
                im / (2 ** (profile["electronics"]["bit_depth"] if profile else 12) - 1)
                if stage == "raw"
                else im
            )
            im = np.uint8(np.clip(im, 0, 1) ** (1 / 2.2) * 255)
            pic = Image.fromarray(im)
            pic.thumbnail((1000, 600))
            pic.save(folder / f"{mode}-{stage}.png")
        parts.append(
            "<h4>"
            + mode
            + ' · 전체 센서 RAW / ISP 표시용 미리보기</h4><div class="anchor-pair">'
            + image(mode + "-raw.png", mode + " RAW display preview")
            + image(mode + "-linear.png", mode + " ISP display preview")
            + "</div>"
        )
    parts.append(
        "<p>전체 프레임 차이는 고정 41점에 대한 공간 밀도 민감도이며 연속 "
        "공간의 직접 정답과의 오차가 아닙니다. PSF는 수신 위치별 정규화 보간"
        "입니다. 절대 비네팅·총 광량·실물 카메라 검증은 미확립입니다.</p><"
        "/section>"
    )
    return "".join(parts)
