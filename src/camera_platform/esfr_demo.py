"""Independent synthetic multi-edge fixture; not a photograph of an ISO chart."""

from __future__ import annotations

import secrets
import tempfile
from copy import deepcopy
from pathlib import Path

import numpy as np
import tifffile
from scipy.special import erf

from pyisetcam.bayer_demosaic import demosaic_bayer


def fixture(seed=1234, shape=(960, 1664)):
    """Authored known Gaussian response, separate from the fitting renderer."""
    h, w = shape
    scene = np.full((h, w), 0.42, dtype=float)
    rois = []
    truth = {}
    for iy, cy in enumerate((150, 390, 630)):
        for ix, cx in enumerate((270, 830, 1390)):
            field = f"field-{iy}-{ix}"
            sigma = 1.05 + 0.40 * np.hypot(ix - 1, iy - 1)
            truth[field] = sigma
            for j, angle in enumerate((5.0, 95.0)):
                rh, rw = 128, 128
                top, left = cy - rh // 2, cx + (-80 if j == 0 else 80) - rw // 2
                yy, xx = np.indices((rh, rw), dtype=float)
                a = np.deg2rad(angle)
                # angle is tangent deviation from vertical; n=(cos(a),-sin(a)).
                distance = (xx - (rw - 1) / 2 - 0.31) * np.cos(a) - (yy - (rh - 1) / 2) * np.sin(a)
                plane = 0.16 + 0.48 * 0.5 * (1 + erf(distance / (np.sqrt(2) * sigma)))
                scene[top : top + rh, left : left + rw] = plane
                rois.append(
                    {
                        "id": field + ("-h" if j == 0 else "-v"),
                        "field_id": field,
                        "kind": "edge",
                        "box": [top, left, rh, rw],
                        "orientation_label": "near_vertical" if j == 0 else "near_horizontal",
                    }
                )
    for i in range(20):
        y, x = 890, 35 + i * 80
        scene[y : y + 40, x : x + 50] = 0.04 + 0.80 * i / 19
        rois.append(
            {
                "id": f"gray-{i:02d}",
                "field_id": f"gray-{i:02d}",
                "kind": "gray",
                "box": [y + 4, x + 4, 32, 42],
            }
        )
    # Colored sites are authored diagnostic patches, never reference ColorChecker values.
    rng = np.random.default_rng(910)
    for i in range(18):
        y, x = 800, 35 + i * 89
        rgb = rng.uniform(0.08, 0.70, 3)
        for rr in range(2):
            for cc in range(2):
                ch = ((0, 1), (1, 2))[(y + rr) % 2][(x + cc) % 2]
                scene[y + rr : y + 48 : 2, x + cc : x + 64 : 2] = rgb[ch]
        rois.append(
            {
                "id": f"color-{i:02d}",
                "field_id": f"color-{i:02d}",
                "kind": "color",
                "box": [y + 6, x + 6, 36, 52],
            }
        )
    noise = np.random.default_rng(seed).normal(0, 2.5, shape)
    raw = np.rint(np.clip(64 + scene * 4031 + noise, 0, 4095)).astype("<u2")
    linear = demosaic_bayer((raw.astype(float) - 64) / 4031, method="bilinear", cfa_pattern="RGGB")
    isp = np.rint(np.clip(linear, 0, 1) * 65535).astype(np.uint16)
    return raw, isp, rois, truth


def create_legacy(service, payload):
    from .esfr_lab import import_capture, submit, template

    seed = secrets.randbelow(2**30)
    token = f"{seed:08x}"
    meta = template()["capture_metadata"]
    meta.update(source_kind="synthetic", camera_id="esfr-synthetic-" + token, pixel_pitch_um=2.9)
    meta["conditions"] = {
        "exposure_s": 0.01,
        "gain": 1.0,
        "light_id": "synthetic-D65",
        "focus_setting": "fixed-authored",
        "distance_m": 2.0,
        "mode_id": "linear-bayer",
        "temperature_c": 25.0,
        "aperture_f_number": 4.12,
        "focus_distance_m": 2.0,
    }
    meta["raw_format"].update(width=1664, height=960, stride_bytes=3328)
    meta["geometry"] = {"full_sensor_shape": [960, 1664], "crop_origin": [0, 0]}
    meta["isp"]["transfer"] = "linear"
    ids = {"development": [], "validation": []}
    with tempfile.TemporaryDirectory(prefix="camera-esfr-demo-") as temp:
        folder = Path(temp)
        for i in range(10):
            raw, isp, rois, truth = fixture(seed + i)
            p, q = folder / "capture.raw", folder / "capture.tiff"
            raw.tofile(p)
            tifffile.imwrite(q, isp, photometric="rgb")
            m = deepcopy(meta)
            role = "development" if i < 5 else "validation"
            m.update(
                role=role,
                session_id=role + "-" + token,
                acquisition_id=f"acq-{token}-{i:02d}",
                synthetic_generator={
                    "method": "independent_erf_gaussian_fixture_v1",
                    "seed": seed + i,
                    "chart_is_authored_fixture": True,
                },
            )
            c = import_capture(service, m, p, q)
            ids[role].append(c["id"])
    chart = template()["chart_profile"]
    chart.update(
        id="synthetic-multiedge-fixture",
        layout="authored_not_imagetest_chart",
        chart_geometry=None,
        reference_status="authored_synthetic_only",
        rois=rois,
        note="분석 흐름 검증용 합성 다중 edge·패치. 보유 E-SFR 차트의 실촬영이 아닙니다.",
    )
    job = submit(
        service,
        {
            "name": "E-SFR 합성 개발/독립 검증 · 9개 위치",
            "development_ids": ids["development"],
            "validation_ids": ids["validation"],
            "rois": rois,
            "chart_profile": chart,
            "source": payload.get("source"),
            "baseline_sigma_native_px": 2.4,
            "fit": True,
            "thresholds": {"raw_sfr_rmse": 0.03, "isp_sfr_rmse": 0.05},
        },
    )
    return {
        **job,
        "demo_capture_ids": ids,
        "chart_profile": chart,
        "authored_truth": truth,
        "demo_notice": "합성 소프트웨어 검증이며 실카메라 검증이 아닙니다.",
    }


def chart_capture(scene, seed, sigma=1.4):
    """Full-scene convolution and native RGGB sampling, independent of edge fitter.

    scene contains pixel-area averaged nominal linear RGB. Gaussian convolution
    is applied to the entire chart (including wedges and junctions), never to
    isolated ROI replacements. The reference ISP is fixed normalized bilinear.
    """
    from scipy.ndimage import gaussian_filter

    from .esfr_engine import bilinear_isp

    rgb = np.asarray(scene, dtype=np.float32)
    if rgb.ndim != 3 or rgb.shape[2] != 3 or not np.isfinite(rgb).all():
        raise ValueError("Chart scene must be finite linear RGB")
    if not np.isfinite(sigma) or not 0.15 <= sigma <= 8:
        raise ValueError("Synthetic blur sigma outside bounded domain")
    blur = gaussian_filter(rgb, sigma=(sigma, sigma, 0), mode="reflect", truncate=5)
    mosaic = np.empty(rgb.shape[:2], dtype=np.float32)
    for r, c, ch in ((0, 0, 0), (0, 1, 1), (1, 0, 1), (1, 1, 2)):
        mosaic[r::2, c::2] = blur[r::2, c::2, ch]
    rng = np.random.default_rng(seed)
    raw = np.rint(np.clip(64 + mosaic * 4031 + rng.normal(0, 1.5, mosaic.shape), 0, 4095)).astype(
        "<u2"
    )
    levels = {ch: {"black_dn": 64.0, "white_dn": 4095.0} for ch in ("R", "Gr", "Gb", "B")}
    isp = np.rint(np.clip(bilinear_isp(raw, "RGGB", (0, 0), levels), 0, 1) * 65535).astype(
        np.uint16
    )
    return raw, isp


def create(service, payload):
    """Create the Extended2023 layout demo with independent repeated captures."""
    if payload.get("fixture") == "legacy_multiedge":
        return create_legacy(service, payload)
    from .esfr_chart import render_chart
    from .esfr_lab import chart_template, import_capture, submit, template

    seed = secrets.randbelow(2**30)
    token = f"{seed:08x}"
    shape = (1440, 2560)
    scene, rois, manifest = render_chart(shape=shape, supersample=4)
    package = chart_template()
    chart = template()["chart_profile"]
    chart.update(
        id="extended-esfr-2023-reconstructed-v1",
        rois=rois,
        chart_geometry=package,
        render_manifest=manifest,
        reference_status="public_design_targets_not_measured_print",
        note="공식 공개 자료로 재구성한 합성 차트. 인쇄 원본과 실카메라 교정값은 별도입니다.",
    )
    meta = template()["capture_metadata"]
    meta.update(
        source_kind="synthetic", camera_id="esfr-chart-synthetic-" + token, pixel_pitch_um=2.9
    )
    meta["conditions"] = {
        "exposure_s": 0.01,
        "gain": 1.0,
        "light_id": "nominal-chart-linear-rgb",
        "focus_setting": "fixed-synthetic-gaussian",
        "distance_m": 2.0,
        "mode_id": "linear-bayer",
        "temperature_c": 25.0,
        "aperture_f_number": 4.12,
        "focus_distance_m": 2.0,
    }
    meta["raw_format"].update(width=shape[1], height=shape[0], stride_bytes=shape[1] * 2)
    meta["geometry"] = {"full_sensor_shape": list(shape), "crop_origin": [0, 0]}
    meta["isp"]["transfer"] = "linear"
    ids = {"development": [], "validation": []}
    sigma = 1.4
    with tempfile.TemporaryDirectory(prefix="camera-esfr-chart-") as temp:
        folder = Path(temp)
        for i in range(6):
            raw, isp = chart_capture(scene, seed + i, sigma)
            p, q = folder / "capture.raw", folder / "capture.tiff"
            raw.tofile(p)
            tifffile.imwrite(q, isp, photometric="rgb")
            m = deepcopy(meta)
            role = "development" if i < 3 else "validation"
            m.update(
                role=role,
                session_id=role + "-" + token,
                acquisition_id=f"acq-{token}-{i:02d}",
                synthetic_generator={
                    "method": "full_chart_linear_rgb_gaussian_rggb_v1",
                    "seed": seed + i,
                    "discrete_gaussian_sigma_native_px": sigma,
                    "pixel_area_supersample": 4,
                    "chart_package_sha256": package["asset_sha256"]["manifest.json"],
                    "physical_chart_reference": "not_measured",
                    "truth_scope": "discrete Gaussian after chart raster area integration",
                },
            )
            c = import_capture(service, m, p, q)
            ids[role].append(c["id"])
    job = submit(
        service,
        {
            "name": "Extended E-SFR 2023 · 전체 차트 · 개발 3 / 독립 검증 3",
            "development_ids": ids["development"],
            "validation_ids": ids["validation"],
            "rois": rois,
            "chart_profile": chart,
            "source": payload.get("source"),
            "baseline_sigma_native_px": 2.4,
            "fit": True,
            "thresholds": {"raw_sfr_rmse": 0.03, "isp_sfr_rmse": 0.05},
        },
    )
    return {
        **job,
        "demo_capture_ids": ids,
        "chart_profile": chart,
        "authored_truth": {r["field_id"]: sigma for r in rois if r["kind"] == "edge"},
        "truth_scope": ("discrete Gaussian after chart raster area integration; "
                        "fitted effective response may differ"),
        "demo_notice": "합성 차트 소프트웨어 검증이며 인쇄 차트 및 실카메라 검증이 아닙니다.",
    }
