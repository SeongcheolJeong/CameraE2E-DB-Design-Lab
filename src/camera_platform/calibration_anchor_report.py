"""Portable evidence for direct sensor boundaries and independent density probes."""

from __future__ import annotations

import csv

import numpy as np
from PIL import Image

from . import calibration_anchor_density as density
from . import calibration_db_core_report as html


def render(root, s):
    if not s:
        return ""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    folder = root / "anchor-density"
    with (folder / "anchors.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "id",
                "role",
                "active_row",
                "active_col",
                "image_x_mm",
                "image_y_mm",
                "object_angle_deg",
                "chief_residual_mm",
            ],
        )
        writer.writeheader()
        for c in s["geometry"]["fields"]:
            writer.writerow(
                dict(
                    id=c["id"],
                    role=c["role"],
                    active_row=c["optical_anchor_active_row_col"][0],
                    active_col=c["optical_anchor_active_row_col"][1],
                    image_x_mm=c.get("image_xy_mm", [None, None])[0],
                    image_y_mm=c.get("image_xy_mm", [None, None])[1],
                    object_angle_deg=c.get("object_angle_deg"),
                    chief_residual_mm=c.get("chief_anchor_residual_mm"),
                )
            )
    parts = [
        (
            "<style>.anchor-pair{display:grid;gr"
            "id-template-columns:repeat(2,minmax"
            "(0,1fr));gap:12px}@media(max-width:"
            "760px){.anchor-pair{grid-template-c"
            'olumns:1fr}}</style><section id="an'
            'chor-density" data-chart="convergen'
            'ce"><h2>센서 끝 직접 PSF · 앵커 밀도 수렴</h2>'
        ),
        "<p><b>"
        + ("통과" if s["status"] == "passed" else "미수렴 · 자동 적용 차단")
        + "</b> · 선택 "
        + str(s["selected_anchor_count"])
        + "개 앵커</p>",
        (
            "<p>센서 모서리·가장자리의 실제 native 좌표를 DB 렌즈"
            "의 chief ray에 역으로 연결하고, 위치마다 450–650"
            " nm의 21개 파장을 직접 추적했습니다. 모든 활성 픽셀 중심"
            "이 직접 앵커의 볼록 영역 안에 있으므로 경계 PSF 유지·외삽"
            "을 사용하지 않습니다. 수신 픽셀 밖 광학 입력 차트 확장과 I"
            "SP 경계 반사는 별도 처리입니다.</p>"
        ),
        (
            "<p>후보 9·25·41점과 분리된 새 16개 위치의 직접 PSF가 "
            "공간 보간 오차 기준입니다. 9·25점 후보는 앵커에 사용하지 "
            "않은 기존 중심 16개도 추가로 검사합니다. 각 검증점에서 PS"
            "F L1·광학 M"
            "TF·동일 전자 모델의 RAW·ISP SFR을 검사합니다. 해당"
            " 점의 PSF를 내부 테스트 패치로 평행이동해 커널 자체를 평가"
            "하며, 센서 경계 Crop을 촬영했다고 해석하지 않습니다.</p"
            ">"
        ),
        (
            "<p>전체 센서는 동일한 25패널 차트를 두 격자로 각각 재시뮬"
            "레이션합니다. 아래 25↔41점 MTF 차이는 밀도 민감도입니"
            "다. 41점 전체 프레임이 연속 공간의 직접 계산 정답이라는 뜻"
            "은 아닙니다. 최소 비용 선택은 독립 16점의 유한 검증 범위에"
            " 한정됩니다.</p>"
        ),
        (
            "<p>광학 q8·반경 64 pixel은 새 직접 계산점마다 q1"
            "6·반경 96 pixel과 비교했습니다. 수신 면적 적분 1회,"
            " 동일 보정 QE·Dark·PTC·CCM, 무잡음 32000 p"
            "hoton/pixel 조건입니다. 절대 비네팅·점광원 총 에너지"
            "·실물 카메라 검증은 미확립입니다.</p>"
        ),
    ]
    parts.append(
        '<p><a href="anchor-density/anchors.csv">직접 위치·광학 좌표·주광선 연결 CSV ↓</a></p>'
    )
    parts.append(
        "<p>41점 설정은 5×5 각 셀 중심의 직접 PSF 16개를 "
        "실행 앵커로 사용합니다. 41점 후보에서는 이 중심들을 검증에서 제외하고, "
        "다른 위치의 새 16개 직접 PSF로 모든 후보를 판정합니다. "
        "9·25점 후보는 기존 셀 중심 16개도 계속 검사해 기존 실패가 빠지지 않게 합니다.</p>"
    )
    parts.append(
        "<p>최소 비용은 기준을 통과한 후보 중 실행 앵커 수가 가장 "
        "적은 설정을 뜻합니다. 계산 시간은 별도로 기록하며 전체 실행 "
        "시간의 수학적 최적해를 뜻하지 않습니다.</p>"
    )
    rows = []
    for c in s["candidates"]:
        cs = [x for x in c["checks"]]
        stages = {
            stage: max(
                v["max_abs"] for x in cs for v in x["comparison"]["checks"] if v["stage"] == stage
            )
            for stage in ("optical", "raw", "isp")
        }
        rows.append(
            [
                (
                    "5×5+16 중심"
                    if c["grid_size"] == "refined"
                    else str(c["grid_size"]) + "×" + str(c["grid_size"])
                ),
                c["anchor_count"],
                max(max(x["psf_l1_rgb"]) for x in cs),
                stages["optical"],
                stages["raw"],
                stages["isp"],
                "통과" if c["passed"] else "미수렴",
            ]
        )
    parts.append(
        html.table(
            [
                "격자",
                "앵커 수",
                "최대 PSF L1",
                "광학 MTF 최대 오차",
                "RAW SFR 최대 오차",
                "ISP SFR 최대 오차",
                "독립점·전체 센서 판정",
            ],
            rows,
        )
    )
    parts.append(
        "<p>사전 기준: PSF L1 ≤ 0.03, MTF 곡선 최대 "
        "차이 ≤ 0.005, 대역 안 MTF50 상대 차이 ≤ 1%. "
        "Bayer Gr RAW는 0–0.25 cy/native pixe"
        "l, ISP·광학은 0–0.5입니다. MTF50가 양쪽 모두 대"
        "역 밖이면 대역 밖으로 기록하고 곡선 기준을 적용합니다.</p>"
    )
    probe_rows = []
    for candidate in s["candidates"]:
        for check in candidate["checks"]:
            values = check["comparison"]["checks"]
            probe_rows.append(
                dict(
                    grid=str(candidate["grid_size"]),
                    field=check.get("field"),
                    row=check.get("position", [None, None])[0],
                    col=check.get("position", [None, None])[1],
                    psf_l1_r=check["psf_l1_rgb"][0],
                    psf_l1_g=check["psf_l1_rgb"][1],
                    psf_l1_b=check["psf_l1_rgb"][2],
                    optical_mtf_max=max(v["max_abs"] for v in values if v["stage"] == "optical"),
                    raw_mtf_max=max(v["max_abs"] for v in values if v["stage"] == "raw"),
                    isp_mtf_max=max(v["max_abs"] for v in values if v["stage"] == "isp"),
                    passed=check.get("passed"),
                )
            )
    with (folder / "independent-probes.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(probe_rows[0]))
        writer.writeheader()
        writer.writerows(probe_rows)
    parts.append(
        '<p><a href="anchor-density/independ'
        'ent-probes.csv">모든 후보·독립 검증점 PSF·MT'
        "F 오차 CSV ↓</a></p>"
    )
    if s["selected_grid_size"] is not None:
        parts.append(
            "<details><summary>선택 후보 · 새 독립 16점 오차</summary>"
            + html.table(
                [
                    "검증점",
                    "active 행·열",
                    "R PSF L1",
                    "G PSF L1",
                    "B PSF L1",
                    "광학 ΔMTF",
                    "RAW ΔMTF",
                    "ISP ΔMTF",
                ],
                [
                    [
                        v["field"],
                        str([v["row"], v["col"]]),
                        v["psf_l1_r"],
                        v["psf_l1_g"],
                        v["psf_l1_b"],
                        v["optical_mtf_max"],
                        v["raw_mtf_max"],
                        v["isp_mtf_max"],
                    ]
                    for v in probe_rows
                    if v["grid"] == str(s["selected_grid_size"])
                ],
            )
            + "</details>"
        )
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), constrained_layout=True)
    for ax, n in zip(axes, (5, "refined"), strict=True):
        anchors, tris = density.topology(s["geometry"], n)
        for t in tris:
            xy = np.array([anchors[k] for k in t + [t[0]]])
            ax.plot(xy[:, 1], xy[:, 0], color="#148779", lw=0.8)
        xy = np.array(list(anchors.values()))
        ax.scatter(xy[:, 1], xy[:, 0], label="direct candidate anchors")
        vv = np.array(
            [
                c["optical_anchor_active_row_col"]
                for c in s["geometry"]["fields"]
                if c["role"] == "validation"
            ]
        )
        ax.scatter(
            vv[:, 1], vv[:, 0], marker="x", color="#d36122", label="independent direct probes"
        )
        h, w = s["geometry"]["full_sensor_shape"]
        ax.set(
            xlim=(-20, w + 20),
            ylim=(h + 20, -20),
            xlabel="active column",
            ylabel="active row",
            title=f"{len(anchors)} anchors: full native sensor hull",
        )
        ax.legend(fontsize=8)
    fig.savefig(folder / "anchor-map.png", dpi=125)
    plt.close(fig)
    parts.append(
        '<img src="'
        + html.image(root, "anchor-density/anchor-map.png")
        + '" alt="Direct boundary anchors and separate density validation points">'
    )
    p = s["partition_check"]
    parts.append(
        html.table(
            ["구현 검증", "결과"],
            [
                ["전체 센서", str(s["geometry"]["full_sensor_shape"])],
                ["직접 위치 × 파장", str(len(s["geometry"]["fields"])) + " × 21"],
                ["커널 LRU 한도 (MiB)", density.KERNEL_BYTES / 1024**2],
                ["FFT LRU 한도 (MiB)", density.spatial.CACHE_BYTES / 1024**2],
                [
                    "실제 커널 LRU 최대 (MiB)",
                    max(c["kernel_cache_peak_bytes"] for c in s["captures"]) / 1024**2,
                ],
                [
                    "광학 배열 작업량 상한 추정 (MiB)",
                    max(c["maximum_array_working_bytes_estimate"] for c in s["captures"]) / 1024**2,
                ],
                [
                    "직접 PSF 계산 시간 합 (분, 병렬 wall time 아님)",
                    sum(c["elapsed_s"] for c in s["source"]["fields"]) / 60,
                ],
                [
                    "전체 센서 밀도 비교 계산 시간 합 (분)",
                    sum(c["elapsed_s"] for c in s["captures"]) / 60,
                ],
                ["타일 128↔96 비교 픽셀", None if p is None else p["active_pixels_compared"]],
                ["타일 128↔96 RAW 최대 차이 (DN)", None if p is None else p["raw_max_abs_dn"]],
                ["타일 128↔96 ISP 최대 차이", None if p is None else p["isp_max_abs"]],
                [
                    "독립 Crop·경계 반사 일치",
                    str(sum(x["passed"] for x in s["independent_window_checks"]))
                    + "/"
                    + str(len(s["independent_window_checks"])),
                ],
            ],
        )
    )
    parts.append(
        "<p>캐시 한도는 배열 비용이며 전체 프로세스 RSS가 아닙니다"
        ". 타일은 광학 halo 65·ISP halo 8 pixel을 "
        "사용합니다. 분석 ROI는 128×128 짝수 원점이며 상·좌 "
        "경계에 닿습니다. 홀수 크기 센서의 마지막 행·열까지 포함한 모"
        "든 픽셀은 타일 재분할 검사로 확인합니다.</p>"
    )
    reused = [
        c
        for c in s["captures"]
        if c.get("native_frame_cache", {}).get("status") == "verified_reuse"
    ]
    if reused:
        sources = sorted({c["native_frame_cache"]["origin"]["run_id"] for c in reused})
        parts.append(
            "<p><b>동일 수치 계약의 프레임 캐시 재사용: "
            + str(len(reused))
            + "개.</b> 입력·PSF·수치 코드·파일 해시를 검증했습니다. 원본 실행 "
            + html.esc(", ".join(sources))
            + (
                "의 계산 시간을 표시하며 복원 시간은 JSON에 별도 기록합니다"
                ". 이 실행의 소스 지문·독립 MTF/Crop·타일 검증·새 보"
                "정 holdout·DB CAS는 다시 확인합니다. 캐시 원본의 "
                "실행 상태는 JSON 이력에 보존됩니다.</p>"
            )
        )
    # All direct selected-grid PSFs in common native-pixel extent, display only.
    n = s["selected_grid_size"] or 5
    anchors, _ = density.topology(s["geometry"], n)
    bank = density.KernelBank(root, s["kernels"])
    columns = 5
    row_count = (len(anchors) + columns - 1) // columns
    fig, axes = plt.subplots(
        row_count, columns, figsize=(12, row_count * 2), constrained_layout=True
    )
    for ax in axes.flat:
        ax.set_visible(False)
    for ax, (fid, position) in zip(axes.flat, anchors.items(), strict=False):
        ax.set_visible(True)
        k = bank[fid][..., 1]
        v = k[480:545, 480:545]
        ax.imshow(
            np.log10(np.maximum(v / v.max(), 1e-6)),
            extent=(-4, 4, 4, -4),
            vmin=-6,
            vmax=0,
            cmap="magma",
        )
        ax.set_title(f"{fid} {position}", fontsize=7)
        ax.tick_params(labelsize=6)
    fig.suptitle("Direct green optical PSFs: display +/-4 native pixels, log10 peak scale")
    fig.savefig(folder / "direct-psfs.png", dpi=125)
    plt.close(fig)
    parts.append(
        (
            "<h3>선택 격자의 직접 PSF</h3><p>표시만 중심 ±4 "
            "pixel·로그 스케일입니다. 실제 계산 커널은 ±64 pixe"
            "l이며 픽셀 적분 전 광학 PSF입니다.</p><img src="
            '"'
        )
        + html.image(root, "anchor-density/direct-psfs.png")
        + '" alt="Direct optical PSFs including corners and edges">'
    )
    # Whole-sensor RAW/ISP: all densities, orientations and phases.
    for f in s["captures"]:
        raw = np.load(root / f["raw"], mmap_mode="r")
        lin = np.load(root / f["linear"], mmap_mode="r")
        Image.fromarray(np.uint8(np.clip(raw.astype(float) / 4095, 0, 1) * 255)).resize(
            (968, 548)
        ).save(folder / (f["id"] + "-raw.png"))
        Image.fromarray(np.uint8(np.clip(np.maximum(lin, 0), 0, 1) ** (1 / 2.2) * 255)).resize(
            (968, 548)
        ).save(folder / (f["id"] + "-isp.png"))
        parts.append(
            "<details><summary>"
            + html.esc(f["id"])
            + ' · 전체 센서 RAW·ISP</summary><p>동일 표시 범위, 원본 <a href="'
            + f["raw"]
            + '">RAW NPY</a> · <a href="'
            + f["linear"]
            + '">linear RGB NPY</a></p><div class="anchor-pair"><img src="'
            + html.image(root, "anchor-density/" + f["id"] + "-raw.png")
            + '" alt="'
            + f["id"]
            + ' RAW"><img src="'
            + html.image(root, "anchor-density/" + f["id"] + "-isp.png")
            + '" alt="'
            + f["id"]
            + ' ISP"></div></details>'
        )
    csvrows = []
    for c in s["full_sensor_density_comparisons"]:
        csvrows.append(
            dict(
                field=c["field"],
                candidate_grid_size=c["candidate_grid_size"],
                reference_grid_size=c["reference_grid_size"],
                chart=c["chart"],
                origin_row=c["origin_row_col"][0],
                origin_col=c["origin_row_col"][1],
                raw_dn_max=c["raw_max_abs_dn"],
                isp_max=c["isp_max_abs"],
                raw_mtf_max=c["mtf"]["raw"]["max_abs"],
                isp_mtf_max=c["mtf"]["isp"]["max_abs"],
                raw_mtf50_relative=c["mtf"]["raw"].get("mtf50_relative"),
                isp_mtf50_relative=c["mtf"]["isp"].get("mtf50_relative"),
            )
        )
    with (folder / "full-sensor-mtf.csv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(csvrows[0]))
        writer.writeheader()
        writer.writerows(csvrows)
    parts.append(
        "<h3>전체 센서에서 추출한 25개 위치의 MTF</h3><p>"
        "파랑: 25점 / 초록: 41점. 각 곡선은 전체 RAW·ISP"
        " 프레임의 native 좌표 Crop에서 측정했습니다. 두 위상"
        ' 최소 100개 비교의 수치는 <a href="anchor-den'
        'sity/full-sensor-mtf.csv">CSV</a>와 '
        '<a href="anchor-density/result.json'
        '">JSON</a>으로 제공합니다.</p>'
    )
    for angle in (0, 90):
        for stage in ("raw", "isp"):
            fig, axes = plt.subplots(5, 5, figsize=(15, 12), constrained_layout=True)
            rows = [
                c
                for c in s["full_sensor_density_comparisons"]
                if c["chart"] == f"edge-{angle}-phase-00" and c["candidate_grid_size"] == 5
            ]
            for ax, c in zip(axes.flat, rows, strict=True):
                for k, color, label in [
                    ("curves", "#4476b5", "25 anchors"),
                    ("reference_curves", "#09836e", "41 anchors"),
                ]:
                    v = c[k][stage]
                    ax.plot(v["x"], v["y"], color=color, label=label, lw=1)
                ax.set(
                    title=c["field"] + " / " + format(c["mtf"][stage]["max_abs"], ".4g"),
                    ylim=(0, 1.15),
                )
                ax.tick_params(labelsize=7)
            axes.flat[0].legend(fontsize=7)
            fig.suptitle(
                f"{stage.upper()} SFR, {angle} deg; title suffix = maximum curve difference"
            )
            name = f"{stage}-{angle}-all-rois.png"
            fig.savefig(folder / name, dpi=110)
            plt.close(fig)
            parts.append(
                '<img src="'
                + html.image(root, "anchor-density/" + name)
                + '" alt="'
                + stage
                + " all 25 ROI MTF "
                + str(angle)
                + ' degrees">'
            )
    parts.append(
        "<p>PSF 수치 샘플링·공간 앵커 밀도·타일 구현 일치·물리 "
        "카메라 일치는 서로 다른 검증입니다. 이번 결과는 DB 렌즈 기"
        "반 합성 조건의 수치 검증입니다. 출력 좌표별 정규화 PSF는 "
        "상수 응답을 보존하며 절대 조사량이나 왜곡 운반을 식별하지 않습"
        "니다.</p></section>"
    )
    return "".join(parts)
