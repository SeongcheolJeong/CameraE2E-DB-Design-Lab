"Standard integrated profile, chart, uncertainty-floor and portable artifact report."

from __future__ import annotations

import csv
import hashlib
import io
import zipfile

import numpy as np

from . import calibration_db_core_report as base
from . import calibration_unified_engine as engine
from .registry import canonical

EXTRA = {
    "raw_patch_rgb_dn": ("Macbeth RAW RGB", "DN"),
    "isp_patch_rgb": ("Macbeth signed ISP RGB", "linear RGB"),
    "raw_scan_dn": ("Monochromatic RAW RGB", "DN"),
    "qe_estimate": ("RAW-derived absolute QE", "electron/photon"),
}


def aggregate(summary):
    result = base.aggregate(summary)
    for group, key in [
        ("macbeth_raw", "raw_patch_rgb_dn"),
        ("macbeth_isp", "isp_patch_rgb"),
        ("qe_raw", "raw_scan_dn"),
        ("qe", "qe_estimate"),
    ]:
        rows = [c["metrics"][key] for c in summary["comparisons"] if key in c["metrics"]]
        errors = {
            r: float(
                np.sqrt(
                    np.mean(
                        np.concatenate([np.asarray(m[r]["residual"]).ravel() for m in rows]) ** 2
                    )
                )
            )
            for r in ("before", "after", "replicate")
        }
        result[group] = {
            "metric": key,
            "unit": EXTRA[key][1],
            **errors,
            "reduction_percent": None
            if errors["before"] == 0
            else 100 * (1 - errors["after"] / errors["before"]),
        }
    return result


def additions(root, summary):
    esc, fmt = base.esc, base.fmt
    p = summary["camera_profile"]
    parents = p["parents"]
    parts = [
        (
            "<section><h2>하나의 카메라 프로파일</h2><p><a"
            ' href="camera-profile.json">통합 카메라 '
            '프로파일 JSON ↓</a> · <a href="before-c'
            'amera-profile.json">보정 전 프로파일</a></'
            'p><p class="identity">SHA256 '
        )
        + esc(p["profile_sha256"])
        + "</p>",
        base.table(
            ["모듈", "적용 기준"],
            [
                ["QE", ("센서 표면 절대 QE로 교체 · Gr/Gb 공유 · 원래 QE와 곱하지 않음")],
                ["Dark·PTC", ("통합 electronics의 Black·DN/e·읽기 잡음·암전류 사용")],
                ["MTF·PSF", "각 시야의 정규화 유효 PSF를 새 차트에 한 번 적용"],
                [
                    "CCM",
                    ("통합 DN/e로 DN→전자 환산 후 WB·참조 전자량·CCM을 포함한 행렬 한 번 적용"),
                ],
            ],
        ),
        (
            "<p>이 작업은 완료된 부모 보정 결과를 결합하며 새로운 검증 "
            "RAW로 재피팅하지 않습니다. 다른 카메라의 실측을 임의로 합치"
            '는 일반 기능이 아닙니다.</p><p class="identit'
            'y">QE·CCM 부모: '
        )
        + esc(parents["color_model_id"])
        + "<br>Dark·PTC·MTF·PSF 부모: "
        + esc(parents["core_model_id"])
        + "</p><p>QE 스캔 원본 DN/e: "
        + fmt(p["isp"]["origin_scan_dn_per_e"])
        + " · 이 카메라에 적용한 DN/e: "
        + fmt(p["electronics"]["dn_per_e"])
        + "</p></section>",
    ]
    for c in summary["comparisons"]:
        if c["chart"] not in ("macbeth", "qe_scan"):
            continue
        title = (
            "Macbeth · " + c["field"]
            if c["chart"] == "macbeth"
            else "파장별 광원 테스트 · 센서 표면 1,200 photons/pixel"
        )
        parts.append('<section data-chart="' + c["chart"] + '"><h2>' + esc(title) + "</h2>")
        for stage, label in [
            ("raw", "RAW · 같은 표시 범위"),
            ("isp", "ISP · 같은 camera-linear 출력 공간"),
        ]:
            parts.append("<h3>" + label + '</h3><div class="three">')
            for role, name in [
                ("before", "보정 전"),
                ("observation", "가상 실측 · 독립 취득 A"),
                ("after", "전체 파라미터 적용 후"),
            ]:
                parts.append(
                    "<figure><figcaption>"
                    + name
                    + '</figcaption><img src="'
                    + base.image(root, c["series"][role]["images"][stage])
                    + '" alt="'
                    + esc(c["id"] + " " + stage + " " + role)
                    + '"></figure>'
                )
            parts.append("</div>")
        for key, m in c["metrics"].items():
            parts.append(
                "<h3>"
                + EXTRA[key][0]
                + '</h3><p class="metric">RMSE '
                + fmt(m["before"]["rmse"])
                + " → "
                + fmt(m["after"]["rmse"])
                + " "
                + EXTRA[key][1]
                + " · 감소 "
                + fmt(m["reduction_percent"])
                + "% · 반복 A/B "
                + fmt(m["replicate"]["rmse"])
                + '</p><img src="'
                + base.image(root, c["plots"][key])
                + '" alt="'
                + esc(c["id"] + " " + key + " curve and residual")
                + '">'
            )
        if c["chart"] == "qe_scan":
            parts.append(
                '<p class="note">단색광의 알려진 센서 표면 광자량과'
                " 각 모델의 전기 응답으로 QE를 검증합니다. 렌즈 DC와 PS"
                "F를 우회하는 별도 측정입니다. 맥베스 차트에서 QE를 역추정하"
                "지 않습니다.</p>"
            )
        parts.append("</section>")
    parts.append(
        "<section><h2>CCM만 바꾼 경우 · 정확히 같은 RAW</h2>"
        + base.table(
            ["시야", "동일 RAW SHA256", "기준 CCM ISP RMSE", "보정 CCM ISP RMSE"],
            [
                [c["field_id"], c["raw_sha256"], c["before_ccm_isp_rmse"], c["after_ccm_isp_rmse"]]
                for c in summary["ccm_same_raw"]
            ],
        )
        + (
            "<p>QE·전기·PSF를 적용해 얻은 같은 정수 RAW를 재사용"
            "합니다. CCM은 RAW를 수정하지 않습니다.</p></sect"
            "ion>"
        )
    )
    return "".join(parts)


def publish(root, summary):
    import sys

    import psutil

    memory = psutil.Process().memory_info()
    peak = user = system = None
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        peak = int(usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024))
        user, system = usage.ru_utime, usage.ru_stime
    except ImportError:
        pass
    summary["execution_resource_observation"] = dict(
        rss_bytes=memory.rss,
        peak_rss_bytes=peak,
        user_cpu_seconds=user,
        system_cpu_seconds=system,
        scope=(
            "whole worker process through report start; "
            "not per-renderer allocation or final lifetime peak"
        ),
    )
    if (root / "report.html").exists() or (root / "evidence.zip").exists():
        raise ValueError("Unified reports are immutable")
    summary["aggregate"] = aggregate(summary)
    for c in summary["comparisons"]:
        c["plots"] = {}
        for key, m in c["metrics"].items():
            name = "plots/" + c["id"] + "-" + key + ".png"
            (root / "plots").mkdir(exist_ok=True)
            label, unit = EXTRA.get(key, base.METRICS.get(key, ("", "")))
            english = {
                "raw_mean_dn": "RAW mean",
                "raw_variance_dn2": "RAW temporal variance",
                "isp_mean_rgb": "ISP mean RGB",
            }.get(key, label)
            base.plot(
                root / name,
                m.get("x", c["series"]["observation"]["x"]),
                m,
                english,
                unit,
                c["series"]["observation"]["x_unit"],
            )
            c["plots"][key] = name
    from . import calibration_psf_convergence as convergence
    from . import calibration_psf_convergence_report as numerical_report

    truth = __import__("json").loads((root / "truth-camera-profile.json").read_text())

    def kernel_provider(role, fid, node):
        profile = {
            "before": summary["baseline_camera_profile"],
            "after": summary["camera_profile"],
            "observation": truth,
        }[role]
        if "psf_numerics" in profile:
            setting = profile["psf_numerics"]["setting"]
            return convergence.kernel(node, setting), 1 / setting["samples_per_pixel"]
        _, kernel = engine.core.lab._effective_blur(np.zeros((8, 8, 3)), node)
        return kernel, 1.0 if node.get("family") == "sampled_native_kernel_v1" else 0.25

    summary["psf"] = base.psf_evidence(root, summary, kernel_provider)
    numerical_html = numerical_report.render(root, summary["psf_convergence"])
    from .calibration_direct_report import render as direct_report

    numerical_html = direct_report(root, summary.get("direct_psf_convergence")) + numerical_html
    from .calibration_spatial_psf_report import render as spatial_report

    numerical_html = spatial_report(root, summary.get("spatial_psf")) + numerical_html
    from .calibration_anchor_report import render as anchor_report

    numerical_html = anchor_report(root, summary.get("anchor_density")) + numerical_html
    from .calibration_adaptive_report import render as adaptive_report

    numerical_html = (
        adaptive_report(
            root,
            summary.get("adaptive_anchors"),
            summary.get("anchor_density"),
            summary["camera_profile"],
            summary["execution_resource_observation"],
        )
        + numerical_html
    )
    direct_result = summary.get("direct_psf_convergence")
    if direct_result and direct_result["selected"]:
        selected = direct_result["selected"]["setting"]
        for crop in direct_result["geometry"]["crops"]:
            parameters = {
                "image_x_mm": crop["image_xy_mm"][0],
                "image_y_mm": crop["image_xy_mm"][1],
                "object_angle_deg": crop["object_angle_deg"],
                "samples_per_pixel": selected["samples_per_pixel"],
                "support_radius_native_px": selected["radius_native_px"],
            }
            for name, value in parameters.items():
                summary["parameter_recovery"].append(
                    {
                        "parameter": "direct_psf." + crop["id"] + "." + name,
                        "before": None,
                        "after": value,
                        "truth": None,
                        "absolute_error": None,
                        "source": "DB_model_derived_numeric_setting_not_measurement_fit",
                    }
                )

    anchors = summary.get("anchor_density")
    if anchors:
        settings = {
            "selected_anchor_count": (summary.get("adaptive_anchors") or anchors)[
                "selected_anchor_count"
            ],
            "samples_per_pixel": 8,
            "support_radius_native_px": 64,
            "optical_halo_native_px": 65,
            "isp_halo_native_px": 8,
            "tile_side_native_px": 128,
            "kernel_cache_bytes": 4 * 1025**2 * 3 * 4,
        }
        for name, value in settings.items():
            summary["parameter_recovery"].append(
                dict(
                    parameter="boundary_spatial_psf." + name,
                    before=None,
                    after=value,
                    truth=None,
                    absolute_error=None,
                    source="DB_model_derived_numeric_setting_not_measurement_fit",
                )
            )

    for fid in engine.FIELDS:
        for wi, wave in enumerate(engine.color.WAVE):
            for ci, ch in enumerate(("R", "Gr", "Gb", "B")):
                b, a, t = (
                    np.asarray(p["spectral_response"][fid])[wi, ci]
                    for p in (summary["baseline_camera_profile"], summary["camera_profile"], truth)
                )
                summary["parameter_recovery"].append(
                    dict(
                        parameter=f"{fid}.QE.{ch}.{wave:g}nm",
                        before=float(b),
                        after=float(a),
                        truth=float(t),
                        absolute_error=float(abs(a - t)),
                        source="known_synthetic_truth_only",
                    )
                )
    for r in range(3):
        for c in range(3):
            b, a, t = (
                p["isp"]["electron_to_output_matrix"][r][c]
                for p in (summary["baseline_camera_profile"], summary["camera_profile"], truth)
            )
            summary["parameter_recovery"].append(
                dict(
                    parameter=f"ISP.electron_to_output_matrix.{r}.{c}",
                    before=b,
                    after=a,
                    truth=t,
                    absolute_error=abs(a - t),
                    source="known_synthetic_truth_only",
                )
            )
    core = summary.copy()
    core["comparisons"] = [
        c for c in summary["comparisons"] if c["chart"] in ("dark", "ptc", "mtf")
    ]
    page = (
        base.render(root, core)
        .decode()
        .replace("Dark·PTC / MTF·PSF", "QE·CCM·Dark·PTC·MTF·PSF")
        .replace("AUTOMATIC CORE CALIBRATION", "UNIFIED CAMERA CALIBRATION")
        .replace(
            "고정 DB ISP · 프레임별 처리 후 시간 평균",
            "각 모델의 통합 ISP · 프레임별 처리 후 시간 평균",
        )
    )
    page = page.replace(
        "</select></label><button",
        (
            '<option value="convergence">PSF 수렴</option><option value="macbeth">Macbeth</op'
            'tion><option value="qe_sc'
            'an">QE 단색광</option></select></label><button'
        ),
        1,
    )
    page = page.replace('href="candidate-profiles.json"', 'href="camera-profile.json"')
    marker = "</header>"
    index = page.index(marker) + len(marker)
    page = page[:index] + numerical_html + additions(root, summary) + page[index:]
    (root / "camera-profile.json").write_text(canonical(summary["camera_profile"]))
    (root / "report-controls.js").write_text(base.CONTROLS)
    (root / "report.html").write_text(page)
    stream = io.StringIO()
    writer = csv.DictWriter(
        stream, fieldnames=["parameter", "before", "after", "truth", "absolute_error", "source"]
    )
    writer.writeheader()
    writer.writerows(summary["parameter_recovery"])
    (root / "parameters.csv").write_text(stream.getvalue())
    engine.core.save(root / "report.json", summary)
    files = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "progress.json"
    }
    engine.core.save(
        root / "manifest.json", {"schema": "camera_unified_artifacts_v1", "files": files}
    )
    with zipfile.ZipFile(root / "evidence.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in [*files, "manifest.json"]:
            z.write(root / name, name)
    with zipfile.ZipFile(root / "evidence.zip") as z:
        if z.testzip() is not None or any(
            hashlib.sha256(z.read(n)).hexdigest() != h for n, h in files.items()
        ):
            raise ValueError("Unified ZIP hash verification failed")
