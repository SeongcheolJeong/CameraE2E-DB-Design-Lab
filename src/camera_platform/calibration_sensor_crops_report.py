"""Located sensor crop evidence in the portable unified report."""

from . import calibration_db_core_report as base


def render(root, summary):
    g = summary.get("sensor_crops")
    if not g:
        return ""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.patches import Rectangle

    fig, ax = plt.subplots(figsize=(12, 7), constrained_layout=True)
    h, w = g["full_sensor_shape"]
    ax.add_patch(Rectangle((0, 0), w, h, fill=False, edgecolor="#334155", lw=2))
    for c in g["crops"]:
        r, col = c["origin_row_col"]
        rh, cw = c["shape_row_col"]
        ax.add_patch(Rectangle((col, r), cw, rh, color="#0f8c79", alpha=0.3))
        ax.text(
            col + cw / 2,
            r + rh + 28,
            c["id"] + f"\n({r}, {col})",
            ha="center",
            va="top",
            fontsize=9,
        )
        y, x = c["optical_anchor_active_row_col"]
        ax.plot(x, y, "+", color="#9c4530")
    y, x = g["optical_axis_active_row_col"]
    ax.plot(x, y, "x", color="black", label="assumed optical axis")
    ax.set(
        xlim=(-40, w + 40),
        ylim=(h + 100, -40),
        xlabel="active column (native pixels)",
        ylabel="active row (native pixels)",
        title="9 sensor-coordinate crops / 128 x 128 native pixels",
    )
    ax.set_aspect("equal")
    ax.legend(loc="center right")
    name = "convergence/sensor-crop-map.png"
    fig.savefig(root / name, dpi=130)
    plt.close(fig)
    parity = summary.get("crop_parity")
    rows = []
    for c in g["crops"]:
        rows.append(
            [
                c["id"],
                str(c["origin_row_col"]),
                str(c["optical_anchor_active_row_col"]),
                ", ".join(f"{x:.4f}" for x in c["image_xy_mm"]),
                c["interpolation_weight"],
                c["node"]["sigma_x_px"],
                c["node"]["sigma_y_px"],
            ]
        )
    return (
        '<section id="sensor-crops" data-cha'
        'rt="convergence"><h2>센서 좌표에 연결된 9개 '
        "Crop</h2>"
        "<p>유효 센서 1,097 × 1,937 pixel · pitch 2.9 µm · 각 Crop 128 × 128 pixel. "
        "행은 아래로, 열은 오른쪽으로 증가합니다. 광학 축 [548, "
        "968]과 RGGB 위상은 설계 가정입니다.</p>"
        "<p>공통 전역 차트에서 광학 halo를 포함해 계산하고, 8 "
        "pixel ISP halo까지 처리한 뒤 지정 좌표로 자릅니다."
        " "
        "RAW 재분석도 전체 센서 크기와 실제 crop_origin을 "
        "사용합니다. 전체 프레임 큐브를 만들지는 않습니다.</p>"
        '<img src="'
        + base.image(root, name)
        + '" alt="Nine crops at actual native sensor row and column coordinates">'
        + base.table(
            [
                "Crop",
                "시작 행·열",
                "광학 앵커 행·열",
                "광학 x·y (mm)",
                "외곽 보간 가중치",
                "σx (pixel)",
                "σy (pixel)",
            ],
            rows,
        )
        + "<p><b>시야 모델의 한계:</b> 보정된 원래 PSF 앵커는 DB chief-image 좌표 "
        + base.esc(str(g["knots_image_xy_mm"]))
        + "에 그대로 둡니다. 센서 안의 9개 PSF는 이 두 앵커의 "
        "공분산을 이미지 반경으로 보간하고 회전 대칭을 가정한 값입니다."
        " 외곽 가상 앵커를 센서 모서리로 "
        "옮긴 것이 아니며, 새 위치의 실측값·렌즈 재추적 결과가 아닙니"
        "다. 각 Crop 내부에서는 PSF를 고정합니다.</p>"
        + (
            "<p><b>확장 캔버스 일치: "
            + ("통과" if parity["passed"] else "미통과")
            + "</b> · "
            + str(len(parity["checks"]))
            + "개 위치·방향·위상 조합. 더 큰 캔버스에서 처리 후 동일 좌표를 자른 결과와 "
            "RAW 최대 차이 "
            + str(max(r["raw_max_abs_dn"] for r in parity["checks"]))
            + " DN, ISP 최대 차이 "
            + str(max(r["isp_max_abs"] for r in parity["checks"]))
            + '. <a href="convergence/crop-parity.json">전체 일치 검사 JSON ↓</a></p>'
            if parity
            else ""
        )
        + "<p>아래의 “기존·선택·기준”은 동일 보정 파라미터에 대한 수치 설정 비교입니다. "
        "물리 파라미터 보정 전후 비교는 통합 보고서의 기존 차트 항목에"
        " 별도로 유지됩니다.</p></section>"
    )
