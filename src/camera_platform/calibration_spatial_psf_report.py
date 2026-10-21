"""Portable full-sensor/tile/spatial-model evidence for the unified camera report."""

from __future__ import annotations

import csv

import numpy as np
from PIL import Image

from . import calibration_db_core_report as html
from . import calibration_spatial_psf as spatial


def render(root, s):
    if not s:
        return ""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    folder = root / "spatial-psf"
    a = s["application"]
    captures = s["captures"]
    height, width = captures[0]["shape_row_col"]
    field_ids = list(a["anchors"])
    palette = plt.get_cmap("tab10")
    # Display-only sampled map; the actual operator evaluates exact native coordinates tile by tile.
    yy, xx = np.mgrid[0:height:8, 0:width:8]
    weights = spatial.weights(a, (0, height, 0, width), stride=8)
    dominant = np.argmax(
        np.stack([weights[k] if k in weights else np.zeros(yy.shape) for k in field_ids]), axis=0
    )
    del weights
    fig, ax = plt.subplots(figsize=(12, 6), constrained_layout=True)
    ax.imshow(
        dominant,
        cmap="tab10",
        vmin=0,
        vmax=9,
        extent=(-0.5, width - 0.5, height - 0.5, -0.5),
        alpha=0.22,
        interpolation="nearest",
    )
    for tri in a["triangles"]:
        p = np.array([a["anchors"][k] for k in tri + [tri[0]]])
        ax.plot(p[:, 1], p[:, 0], color="#008b78", lw=1)
    for i, (fid, (y, x)) in enumerate(a["anchors"].items()):
        ax.plot(x, y, "o", color=palette(i))
        ax.text(x, y + 32, fid, ha="center", va="top", fontsize=9)
    ax.set(
        xlim=(-0.5, width - 0.5),
        ylim=(height - 0.5, -0.5),
        xlabel="active native column",
        ylabel="active native row",
        title="9 direct PSFs: receiver-coordinate triangle basis and clamped outer region",
    )
    fig.savefig(folder / "spatial-map.png", dpi=125)
    plt.close(fig)
    parts = [
        (
            "<style>.spatial-pair{display:grid;g"
            "rid-template-columns:repeat(2,minma"
            "x(0,1fr));gap:12px}@media(max-width"
            ":760px){.spatial-pair{grid-template"
            "-columns:1fr}}</style>"
        ),
        (
            '<section id="spatial-psf" data-char'
            't="convergence"><p>FULL SENSOR · SP'
            "ATIALLY VARYING OPTICAL PSF</p>"
        ),
        "<h2>전체 센서 공간 가변 PSF · 타일·halo 검증</h2>",
        "<h3>" + html.esc("통과" if s["status"] == "passed" else "미확립 · 적용 차단") + "</h3>",
        (
            "<p>9개 위치의 직접 계산 광학 PSF를 비음수 삼각형 가중치"
            "로 연결합니다. 각 native 수신 픽셀 중심의 가중치를 픽셀"
            " 면적 적분 동안 고정합니다. PSF를 위치마다 저장하지 않으며"
            ", 타일마다 필요한 앵커만 합성합니다.</p>"
        ),
        (
            "<p><b>두 비교는 의미가 다릅니다.</b> 같은 공간 가변 "
            "모델의 전체 센서·독립 Crop·타일 크기 변경 비교는 구현 일"
            "치 검사입니다. 기존 고정 PSF Crop과의 차이는 Crop "
            "내부에 새로 적용된 공간 변화량이며, 0으로 강제하지 않습니다."
            " 아래 그래프와 표에 따로 표시합니다.</p>"
        ),
        (
            "<p>앵커 사각형 바깥은 가장 가까운 경계 위치의 PSF를 유지"
            "합니다. 이는 외삽 가정이며 미추적 모서리 광학 검증이 아닙니다"
            ". 출력 위치별 PSF 정규화는 상수 응답을 보존하지만 점광원 "
            "총 에너지·절대 비네팅·왜곡 운반을 검증하지 않습니다.</p>"
        ),
        (
            "<p>광학 합성 → 수신 픽셀 면적 1회 적분 → 보정 전자 모"
            "델·RGGB RAW → 보정 CCM·ISP 순서입니다. q8, "
            "광학 반경 64 pixel, 광학 입력 halo 65 pixel"
            ", ISP halo 8 pixel. 센서 밖 광학 입력은 동일한"
            " 연속 차트로 확장하고 RAW·ISP 센서 경계는 RGGB 위상"
            "을 보존하는 반사를 사용합니다.</p>"
        ),
        (
            '<p><a href="spatial-psf/result.json'
            '">전체 센서 검증 JSON ↓</a> · <a href="sp'
            'atial-psf/crop-comparisons.csv">36개'
            " Crop 비교 CSV ↓</a></p>"
        ),
        '<img src="'
        + html.image(root, "spatial-psf/spatial-map.png")
        + '" alt="Full sensor spatial PSF anchor triangles and clamped boundaries">',
    ]
    resource = s["resource"]
    partition = s["partition_check"]
    parts.append(
        html.table(
            ["항목", "결과"],
            [
                ["전체 native 센서", f"{height} × {width}"],
                ["타일", str(a["tile_shape_row_col"])],
                [
                    "타일 광학 배열 작업량 상한 추정 (MiB)",
                    resource["maximum_array_working_bytes_estimate"] / 1024**2,
                ],
                [
                    "전 센서 q8 RGB float64 단일 버퍼 (MiB)",
                    resource["naive_full_supersampled_rgb_single_buffer_bytes"] / 1024**2,
                ],
                ["단일 대형 버퍼 / 타일 작업량 추정 비율", resource["naive_to_tiled_array_ratio"]],
                ["FFT 캐시 한도 (MiB)", a["fft_cache_max_bytes"] / 1024**2],
                ["128↔96 타일 전체 픽셀 수", partition["active_pixels_compared"]],
                ["128↔96 RAW 최대 차이 (DN)", partition["raw_max_abs_dn"]],
                ["128↔96 ISP 최대 차이", partition["isp_max_abs"]],
                [
                    "독립 공간 가변 Crop 비교",
                    f"{sum(v['passed'] for v in s['independent_window_checks'])}/36",
                ],
                [
                    "고정 앵커 FFT ↔ 기존 CDF Crop 재현",
                    f"{sum(v['passed'] for v in s['stationary_renderer_checks'])}/36",
                ],
            ],
        )
    )
    parts.append(
        "<p>메모리 값은 광학 배열과 제한된 캐시의 보수적 추정입니다."
        " Python·라이브러리·운영체제 파일 캐시를 포함한 프로세스 "
        "RSS 측정값이 아닙니다. q8 RGB 전체 배열은 실제 할당하"
        "지 않았습니다. RAW·linear RGB는 native 해상도"
        "의 디스크 배열로 저장합니다.</p>"
    )
    for cap in captures:
        raw = np.load(root / cap["raw"], mmap_mode="r", allow_pickle=False)
        linear = np.load(root / cap["linear"], mmap_mode="r", allow_pickle=False)
        # Display scaling only; original RAW and signed linear arrays are downloadable.
        rr = np.asarray(np.clip(raw.astype(float) / 4095, 0, 1) * 255, dtype=np.uint8)
        rgb = np.asarray(np.clip(np.maximum(linear, 0), 0, 1) ** (1 / 2.2) * 255, dtype=np.uint8)
        Image.fromarray(rr).resize((968, 548)).save(folder / (cap["id"] + "-raw.png"))
        Image.fromarray(rgb).resize((968, 548)).save(folder / (cap["id"] + "-isp.png"))
        parts += [
            "<h3>" + html.esc(cap["id"]) + " · 전체 센서 RAW·ISP</h3>",
            "<p>계산 시간 "
            + html.esc(round(cap["elapsed_s"], 2))
            + " s · ADC 포화 "
            + html.esc(cap["clipped_adc_pixels"])
            + ' pixel · native 결과 <a href="'
            + html.esc(cap["raw"])
            + '">RAW NPY ↓</a> / <a href="'
            + html.esc(cap["linear"])
            + '">linear RGB NPY ↓</a></p>',
            '<div class="spatial-pair"><img src="'
            + html.image(root, "spatial-psf/" + cap["id"] + "-raw.png")
            + '" alt="'
            + cap["id"]
            + ' full sensor tiled spatial RAW">',
            '<img src="'
            + html.image(root, "spatial-psf/" + cap["id"] + "-isp.png")
            + '" alt="'
            + cap["id"]
            + ' full sensor tiled spatial ISP"></div>',
        ]
    rows = []
    for c in s["comparisons"]:
        window = next(
            v
            for v in s["independent_window_checks"]
            if v["field"] == c["field"] and v["chart"] == c["chart"]
        )
        stationary = next(
            v
            for v in s["stationary_renderer_checks"]
            if v["field"] == c["field"] and v["chart"] == c["chart"]
        )
        rows.append(
            {
                "field": c["field"],
                "chart": c["chart"],
                "spatial_vs_fixed_raw_mtf_max_abs": c["mtf"]["raw"]["max_abs"],
                "spatial_vs_fixed_isp_mtf_max_abs": c["mtf"]["isp"]["max_abs"],
                "spatial_vs_fixed_raw_mtf50_relative": c["mtf"]["raw"].get("mtf50_relative"),
                "spatial_vs_fixed_isp_mtf50_relative": c["mtf"]["isp"].get("mtf50_relative"),
                "full_vs_window_raw_max_dn": window["raw_max_abs_dn"],
                "full_vs_window_isp_max_abs": window["isp_max_abs"],
                "fixed_fft_vs_prior_raw_max_dn": stationary["raw_max_abs_dn"],
                "fixed_fft_vs_prior_raw_mtf_max_abs": stationary["mtf"]["raw"]["max_abs"],
                "fixed_fft_vs_prior_isp_mtf_max_abs": stationary["mtf"]["isp"]["max_abs"],
            }
        )
        if not c["chart"].endswith("phase-00"):
            continue
        fig, axes = plt.subplots(2, 2, figsize=(12, 7), constrained_layout=True)
        for col, stage in enumerate(("raw", "isp")):
            ax = axes[0, col]
            v = c["curves"][stage]
            old = c["reference_curves"][stage]
            ref = window["reference_curves"][stage]
            ax.plot(old["x"], old["y"], color="#6b7280", label="prior stationary Crop")
            ax.plot(v["x"], v["y"], color="#008b78", label="full sensor spatial PSF")
            ax.plot(ref["x"], ref["y"], "--", color="#e09a25", label="independent spatial window")
            ax.set(title=stage.upper() + " MTF", ylim=(0, 1.15), xlabel="cycles/native pixel")
            ax.legend(fontsize=8)
            diff = np.asarray(v["y"]) - np.interp(v["x"], old["x"], old["y"])
            tile = np.asarray(v["y"]) - np.interp(v["x"], ref["x"], ref["y"])
            axes[1, col].plot(v["x"], diff, label="spatial - stationary", color="#008b78")
            axes[1, col].plot(v["x"], tile, label="full - spatial window", color="#e09a25")
            axes[1, col].axhline(0, color="#6b7280", lw=0.5)
            axes[1, col].set(xlabel="cycles/native pixel", ylabel="MTF difference")
            axes[1, col].legend(fontsize=8)
        fig.suptitle(c["field"] + " / " + c["chart"])
        path = "spatial-psf/" + c["field"] + "-" + c["chart"] + "-mtf.png"
        fig.savefig(root / path, dpi=125)
        plt.close(fig)
        parts += [
            "<h3>" + html.esc(c["field"] + " · " + c["chart"]) + "</h3>",
            '<img src="'
            + html.image(root, path)
            + '" alt="'
            + html.esc(c["field"] + " " + c["chart"] + " spatial versus stationary RAW ISP MTF")
            + '">',
        ]
    with (folder / "crop-comparisons.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    parts += [
        "<h3>36개 Crop의 모델 변화량과 구현 일치도</h3>",
        html.table(
            [
                "위치",
                "차트",
                "공간 변화 RAW MTF",
                "공간 변화 ISP MTF",
                "전체↔독립 RAW (DN)",
                "전체↔독립 ISP",
                "고정 FFT↔기존 RAW MTF",
            ],
            [
                [
                    r[k]
                    for k in (
                        "field",
                        "chart",
                        "spatial_vs_fixed_raw_mtf_max_abs",
                        "spatial_vs_fixed_isp_mtf_max_abs",
                        "full_vs_window_raw_max_dn",
                        "full_vs_window_isp_max_abs",
                        "fixed_fft_vs_prior_raw_mtf_max_abs",
                    )
                ]
                for r in rows
            ],
        ),
        (
            "<p>MTF50이 측정 대역 밖인 경우 숫자를 만들지 않고 JS"
            "ON·CSV에서 미확립으로 남깁니다. 공간 변화량에는 기존 Cr"
            "op과의 합격/불합격을 강제하지 않습니다. 타일·독립 Crop "
            "일치도와 고정 PSF 렌더러 재현 기준을 통과해야 활성화됩니다."
            "</p>"
        ),
        "<details><summary>공간 가변 모델의 가정·검증 범위</summary><ul>"
        + "".join("<li>" + html.esc(v) + "</li>" for v in s["scope"])
        + "</ul></details></section>",
    ]
    return "".join(parts)
