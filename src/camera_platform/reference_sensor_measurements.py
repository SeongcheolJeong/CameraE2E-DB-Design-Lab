"""Comparable measurements and portable reports for Reference sensor modes.

Electrons are aggregate readout values; ``contribution_count`` converts them to
the equivalent signal per contributing native pixel. RGB must already use the
common electron-rate / native-area ISP convention. Temporal noise never treats
chart texture or the spatial fixed-pattern realization as repeated noise.
"""

# The standalone report keeps HTML prose and inline styles together.
# ruff: noqa: E501

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

import numpy as np

from . import virtual_lab_metrics as shared

SCHEMA = "camera_reference_sensor_measurements_v1"
NATIVE_BORDER = 16


def _flags(value, shape, name):
    if value is None:
        return None
    flags = np.asarray(value)
    if flags.shape != shape or not np.isin(flags, [0, 1]).all():
        raise ValueError(f"{name} must be a boolean array with the electron frame shape")
    return flags.astype(bool)


def _sfr(image, common, factor, chart, horizontal_fov_deg):
    h, w = image.shape
    native_shape = (h * factor, w * factor)
    if chart == "resolution":
        rois = {
            key: [int(v // factor) for v in roi]
            for key, roi in shared.five_site_rois(native_shape).items()
        }
    else:
        border = NATIVE_BORDER // factor
        rois = {"center": [border, h - border, border, w - border]}
    sites = {}
    for key, (r0, r1, c0, c1) in rois.items():
        roi = [r0, r1, c0, c1]
        if not common[r0:r1, c0:c1].all():
            sites[key] = {
                "status": "unavailable",
                "reason": "edge_outside_valid_support",
                "roi": roi,
            }
            continue
        curve = shared.slanted_edge_metrics(image[r0:r1, c0:c1])
        if curve["status"] != "ok":
            sites[key] = {**curve, "roi": roi}
            continue
        frequency = np.asarray(curve["frequency"]["cycles_per_pixel"]) / factor
        mtf50 = curve["mtf50_cycles_per_pixel"]
        site = {
            "status": "ok",
            "method": curve["method"],
            "roi": roi,
            "frequency_cycles_per_native_pixel": frequency.tolist(),
            "mtf": curve["sfr"],
            "mtf50_cycles_per_native_pixel": None if mtf50 is None else mtf50 / factor,
            "mtf50_reason": curve["mtf50_reason"],
            "nyquist_cycles_per_native_pixel": 0.5 / factor,
            "overshoot_fraction": curve["overshoot_fraction"],
            "undershoot_fraction": curve["undershoot_fraction"],
        }
        if horizontal_fov_deg is not None:
            # Only a declared uniform local angular raster. Do not infer a full
            # camera field from a small off-axis probe's array dimensions.
            degrees_per_native_pixel = horizontal_fov_deg / native_shape[1]
            site["frequency_cycles_per_degree"] = (frequency / degrees_per_native_pixel).tolist()
            site["angular_scope"] = "declared local probe horizontal FOV; uniform angular raster"
        sites[key] = site
    center = sites["center"]
    return {
        **center,
        "sites": sites,
        "interpretation": "Processed system SFR, not lens-only MTF; no upsampled image measured.",
        "repeat_aggregation": "mean of independently processed linear frames",
    }


def measure(
    electron_frames,
    linear_frames,
    valid_frames,
    *,
    contribution_count=1,
    native_pitch_factor=1,
    chart="uniform",
    electron_valid_frames=None,
    well_saturated_frames=None,
    adc_saturated_frames=None,
    horizontal_fov_deg=None,
):
    """Measure one mode using a common physical border and per-native-pixel units.

    Saturation uses the entire interior, including invalid/saturated samples.
    ``valid_frames`` belongs to the reconstructed regular RGB grid; optional
    ``electron_valid_frames`` belongs to the physical CFA-site samples. These
    masks must stay separate for binning's irregular site centroids. Each metric
    uses its own all-repeat finite support. Missing metrics are not zero/pass.
    """
    electrons = np.asarray(electron_frames, dtype=float)
    linear = np.asarray(linear_frames, dtype=float)
    if electrons.ndim != 3 or not electrons.size:
        raise ValueError("electron_frames must be nonempty T,H,W")
    if linear.shape != (*electrons.shape, 3):
        raise ValueError("linear_frames must have shape T,H,W,3 matching electrons")
    valid = _flags(valid_frames, electrons.shape, "valid_frames")
    if valid is None:
        raise ValueError("valid_frames is required")
    electron_valid = _flags(electron_valid_frames, electrons.shape, "electron_valid_frames")
    if electron_valid is None:
        electron_valid = valid
    if native_pitch_factor not in (1, 2) or isinstance(native_pitch_factor, bool):
        raise ValueError("native_pitch_factor must be 1 or 2")
    factor = int(native_pitch_factor)
    count = float(contribution_count)
    if not np.isfinite(count) or count <= 0:
        raise ValueError("contribution_count must be positive and finite")
    if horizontal_fov_deg is not None:
        horizontal_fov_deg = float(horizontal_fov_deg)
        if not np.isfinite(horizontal_fov_deg) or not 0 < horizontal_fov_deg < 180:
            raise ValueError("horizontal_fov_deg must describe a finite local probe FOV")
    well = _flags(well_saturated_frames, electrons.shape, "well_saturated_frames")
    adc = _flags(adc_saturated_frames, electrons.shape, "adc_saturated_frames")
    frames, h, w = electrons.shape
    border = NATIVE_BORDER // factor
    interior = np.zeros((h, w), dtype=bool)
    if min(h, w) > 2 * border:
        interior[border:-border, border:-border] = True
    common = interior & np.all(valid & np.isfinite(linear).all(axis=-1), axis=0)
    electron_common = interior & np.all(electron_valid & np.isfinite(electrons), axis=0)
    n = int(common.sum())
    electron_n = int(electron_common.sum())
    interior_n = int(interior.sum())
    reason = None if n and electron_n else "incomplete_electron_or_linear_support"
    if not n and not electron_n:
        reason = "no_common_valid_interior"
    result = {
        "schema": SCHEMA,
        "status": "ok" if n and electron_n else "partial" if n or electron_n else "unavailable",
        "reason": reason,
        "repeat_count": frames,
        "contribution_count": count,
        "native_pitch_factor": factor,
        "native_equivalent_shape": [h * factor, w * factor],
        "native_border_pixels": NATIVE_BORDER,
        "measurement_roi": [border, h - border, border, w - border] if interior_n else None,
        "valid_pixels": n,
        "linear_valid_pixels": n,
        "electron_valid_pixels": electron_n,
        "interior_pixels": interior_n,
        "valid_fraction": n / interior_n if interior_n else None,
        "electron_valid_fraction": electron_n / interior_n if interior_n else None,
        "support_policy": "CFA site electrons and reconstructed RGB retain separate masks",
        "electron_unit": "electrons per contributing native pixel",
        "linear_unit": "common linear RGB; fixed electron-rate and native-area convention",
        "noise_method": "sqrt(mean(per-pixel temporal variance, ddof=1)); spatial texture excluded",
        "nyquist_cycles_per_native_pixel": 0.5 / factor,
        "mean_electrons": None,
        "temporal_noise_e": None,
        "snr": None,
        "snr_db": None,
        "linear_y_temporal_noise": None,
        "linear_rgb_temporal_noise": None,
        "local_contrast": None,
        "local_contrast_definition": "P95-P5 divided by abs(P95)+abs(P5), temporal-mean linear Y",
        "well_saturation_fraction": float(well[:, interior].mean())
        if well is not None and interior_n
        else None,
        "adc_saturation_fraction": float(adc[:, interior].mean())
        if adc is not None and interior_n
        else None,
        "saturation_denominator": "all repeats and interior pixels, including invalid samples",
        "temporal_noise_reason": "no_common_valid_electron_interior"
        if not electron_n
        else "at_least_two_repeats_required"
        if frames < 2
        else None,
        "linear_noise_reason": "no_common_valid_linear_interior"
        if not n
        else "at_least_two_repeats_required"
        if frames < 2
        else None,
    }
    if electron_n:
        samples = electrons[:, electron_common] / count
        signal = float(np.mean(samples))
        result["mean_electrons"] = signal
        if frames >= 2:
            noise = float(np.sqrt(np.mean(np.var(samples, axis=0, ddof=1))))
            result["temporal_noise_e"] = noise
            if noise > 0 and signal > 0:
                result["snr"] = signal / noise
                result["snr_db"] = float(20 * np.log10(signal / noise))
            else:
                result["snr_reason"] = "nonpositive_signal_or_zero_noise"
    if n:
        rgb = linear[:, common]
        luma = rgb @ shared.LUMA
        low, high = np.percentile(np.mean(luma, axis=0), [5, 95])
        denominator = abs(low) + abs(high)
        result["local_contrast"] = float((high - low) / denominator) if denominator > 0 else None
        if frames >= 2:
            result["linear_y_temporal_noise"] = float(
                np.sqrt(np.mean(np.var(luma, axis=0, ddof=1)))
            )
            result["linear_rgb_temporal_noise"] = np.sqrt(
                np.mean(np.var(rgb, axis=0, ddof=1), axis=0)
            ).tolist()
    if interior_n and chart in {"resolution", "edge", "slanted_edge"}:
        with np.errstate(invalid="ignore"):
            image = np.mean(linear, axis=0) @ shared.LUMA
        result["sfr"] = _sfr(image, common, factor, chart, horizontal_fov_deg)
    return result


def _esc(value):
    return html.escape(str(value), quote=True)


def _number(value):
    return "—" if value is None else f"{value:.5g}"


def _image(root, name):
    if not name or not isinstance(name, str):
        return None
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        return None
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(path.suffix)
    if mime is None or not path.is_file():
        return None
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _plots(cases, root):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figures = []
    fields = sorted({case.get("field_id", "unknown") for case in cases})
    directory = root / "plots"
    directory.mkdir(exist_ok=True)
    for field in fields:
        available = [case for case in cases if case.get("field_id", "unknown") == field]
        uniform = [case for case in available if case.get("chart") == "uniform"]
        for metric, label in (
            ("mean_electrons", "Mean signal (e / native pixel)"),
            ("temporal_noise_e", "Temporal RMS (e / native pixel)"),
            ("snr", "Signal / temporal RMS"),
            ("adc_saturation_fraction", "ADC saturation fraction"),
        ):
            series = {}
            for case in uniform:
                for mode in case.get("modes", []):
                    value = mode.get("metrics", {}).get(metric)
                    scale = case.get("light_scale")
                    if value is not None and scale is not None and scale > 0:
                        series.setdefault(mode["id"], []).append((scale, value))
            if not series:
                continue
            fig = Figure(figsize=(7.2, 3.4), layout="constrained")
            FigureCanvasAgg(fig)
            axis = fig.subplots()
            for mode_id, points in series.items():
                points.sort()
                axis.plot(*np.asarray(points).T, marker="o", label=mode_id)
            axis.set(
                xscale="log",
                xlabel="Declared light multiplier",
                ylabel=label,
                title=f"{field} · uniform chart",
            )
            axis.grid(alpha=0.18)
            axis.legend(fontsize=8)
            filename = f"curve-{len(figures):02d}.png"
            fig.savefig(directory / filename, dpi=140)
            figures.append((label, _image(root, "plots/" + filename)))
        for case in available:
            tone_modes = [mode for mode in case.get("modes", []) if mode.get("tone_curve")]
            for metric, label in (
                ("mean_electrons", "Measured response (e / native pixel)"),
                ("temporal_noise_e", "Temporal RMS (e / native pixel)"),
                ("well_saturation_fraction", "Well / path saturation fraction"),
                ("adc_saturation_fraction", "ADC saturation fraction"),
                ("valid_fraction", "Valid output fraction"),
            ):
                series = []
                for mode in tone_modes:
                    points = [
                        (row.get("input_mean_electrons"), row.get(metric))
                        for row in mode["tone_curve"]
                    ]
                    points = [
                        (x, y) for x, y in points if x is not None and y is not None and x > 0
                    ]
                    if points:
                        series.append((mode.get("label", mode["id"]), sorted(points)))
                if not series:
                    continue
                fig = Figure(figsize=(7.2, 3.4), layout="constrained")
                FigureCanvasAgg(fig)
                axis = fig.subplots()
                for label_mode, points in series:
                    axis.plot(*np.asarray(points).T, marker="o", markersize=3, label=label_mode)
                axis.set(
                    xscale="log",
                    xlabel="Mean input photoelectrons / native pixel",
                    ylabel=label,
                    title=f"{field} · dynamic-range steps · {case.get('light_scale', 1):g}× light",
                )
                axis.grid(alpha=0.18)
                axis.legend(fontsize=8)
                filename = f"curve-{len(figures):02d}.png"
                fig.savefig(directory / filename, dpi=140)
                figures.append((label, _image(root, "plots/" + filename)))
            curves = [
                (mode, mode.get("sfr") or mode.get("metrics", {}).get("sfr"))
                for mode in case.get("modes", [])
            ]
            curves = [
                (mode, curve) for mode, curve in curves if curve and curve.get("status") == "ok"
            ]
            if not curves:
                continue
            fig = Figure(figsize=(7.2, 3.4), layout="constrained")
            FigureCanvasAgg(fig)
            axis = fig.subplots()
            for mode, curve in curves:
                (line,) = axis.plot(
                    curve["frequency_cycles_per_native_pixel"],
                    curve["mtf"],
                    label=mode.get("label", mode["id"]),
                )
                axis.axvline(
                    curve["nyquist_cycles_per_native_pixel"],
                    alpha=0.3,
                    ls=":",
                    color=line.get_color(),
                )
            axis.set(
                xlabel="Cycles per native pixel",
                ylabel="Processed SFR",
                title=f"{field} · {case.get('light_scale', 1):g}× light · center edge",
            )
            axis.grid(alpha=0.18)
            axis.legend(fontsize=8)
            filename = f"curve-{len(figures):02d}.png"
            fig.savefig(directory / filename, dpi=140)
            figures.append(
                (
                    "Processed SFR, including each raster's Nyquist",
                    _image(root, "plots/" + filename),
                )
            )
    return figures


def _readout_plots(cases, root):
    """Compact circuit diagnostics from retained repeated-RAW measurements."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figures = []
    directory = root / "plots"
    directory.mkdir(exist_ok=True)
    for field in dict.fromkeys(c["field_id"] for c in cases):
        available = [c for c in cases if c["field_id"] == field]
        for kind in ("ptc", "column", "nps"):
            fig = Figure(figsize=(7.2, 3.4), layout="constrained")
            FigureCanvasAgg(fig)
            axis = fig.subplots()
            plotted = False
            chosen = next((c for c in available if c["chart"] == "dark"), available[0])
            if kind == "ptc":
                grouped = {}
                for case in available:
                    for mode in case["modes"]:
                        points = mode.get("readout_diagnostics", {}).get("ptc", {}).get("points", [])
                        grouped.setdefault(mode["id"], []).extend(
                            (p["mean_electrons"], p["temporal_variance_e2"])
                            for p in points if p["site"] == "Gr" and p["status"] == "ok"
                            and p["mean_electrons"] > 0
                        )
                for label, points in grouped.items():
                    if points:
                        axis.plot(*np.asarray(sorted(points)).T, "o-", label=label, markersize=3)
                        plotted = True
                axis.set(xscale="log", yscale="log", xlabel="Gr mean electrons", ylabel="Temporal variance (e²)",
                         title=f"{field} · simulated photon-transfer points")
            else:
                for mode in chosen["modes"]:
                    diag = mode.get("readout_diagnostics", {})
                    if kind == "column":
                        values = diag.get("column_temporal_rms_e", [])
                        points = [(i, v) for i, v in enumerate(values) if v is not None]
                    else:
                        spectrum = diag.get("noise_power_spectrum", {})
                        points = [(x, y) for x, y in zip(spectrum.get("frequency_cycles_per_pixel", []),
                                  spectrum.get("radial_power", []), strict=True) if y is not None and y > 0]
                    if points:
                        axis.plot(*np.asarray(points).T, label=mode["id"])
                        plotted = True
                axis.set(title=f"{field} · {chosen['chart']} · {chosen['light_scale']:g}×")
                if kind == "column":
                    axis.set(xlabel="Native column", ylabel="Temporal RMS of column mean (e)")
                else:
                    axis.set(xlabel="Cycles per native pixel (Gr grid)", ylabel="Temporal NPS density", yscale="log")
            if plotted:
                axis.grid(alpha=0.18)
                axis.legend(fontsize=8)
                filename = f"readout-{field}-{kind}.png"
                fig.savefig(directory / filename, dpi=140)
                figures.append((f"{field} · {kind}", _image(root, "plots/" + filename)))
    return figures


def write_report(result, cases, output_dir):
    """Create standalone HTML with embedded previews, curves and source metadata."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    cases = list(cases)
    parts = []
    headers = (
        "Mode",
        "Raster",
        "Mean e/native",
        "Temporal e/native",
        "SNR",
        "Y noise",
        "Well sat.",
        "ADC sat.",
        "Digital rail",
        "Contrast",
    )
    for index, case in enumerate(cases):
        title = f"{case.get('field_id', 'unknown')} · {case.get('chart', 'unknown')} · {case.get('light_scale', 1):g}×"
        rows, previews = [], []
        for mode in case.get("modes", []):
            metric = mode.get("metrics", {})
            values = [mode.get("label", mode.get("id", "unknown")), str(mode.get("shape", "—"))]
            values += [
                _number(metric.get(key))
                for key in (
                    "mean_electrons",
                    "temporal_noise_e",
                    "snr",
                    "linear_y_temporal_noise",
                    "well_saturation_fraction",
                    "adc_saturation_fraction",
                    "digital_saturation_fraction",
                    "local_contrast",
                )
            ]
            rows.append("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in values) + "</tr>")
            image = _image(root, mode.get("files", {}).get("display"))
            if image:
                previews.append(
                    f'<figure><img src="{image}" alt="{_esc(values[0])}"><figcaption>{_esc(values[0])}</figcaption></figure>'
                )
        parts.append(
            f'<details id="case-{index}" {"open" if index == 0 else ""}><summary>{_esc(title)}</summary>'
            + '<div class="previews">'
            + "".join(previews)
            + '</div><div class="scroll"><table><thead><tr>'
            + "".join(f"<th>{_esc(h)}</th>" for h in headers)
            + "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
            + ("<details><summary>Readout settings, modeled noise budget and temporal diagnostics</summary><pre>"
               + _esc(json.dumps([
                   {"mode": m["id"], "diagnostics": m.get("readout_diagnostics"),
                    "metadata": m.get("metadata")}
                   for m in case["modes"]
               ], indent=2, ensure_ascii=False)) + "</pre></details>"
               if any("readout_diagnostics" in m for m in case["modes"]) else "")
            + "<details><summary>Case assumptions and source references</summary><pre>"
            + _esc(
                json.dumps(
                    {k: case.get(k) for k in ("assumptions", "source_refs")},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            + "</pre></details></details>"
        )
    plots = "".join(
        f'<figure><img src="{image}" alt="{_esc(title)}"><figcaption>{_esc(title)}</figcaption></figure>'
        for title, image in (_plots(cases, root) + (_readout_plots(cases, root)
            if any("readout_diagnostics" in m for c in cases for m in c["modes"]) else []))
        if image
    )
    source = {
        key: result[key]
        for key in ("recipe", "source", "assumptions", "specification", "limitations")
        if key in result
    }
    document = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reference Sensor Mode Lab</title><style>
body{font:15px/1.55 system-ui,sans-serif;background:#f4f2ec;color:#253b39;margin:0 auto;padding:32px;max-width:1320px}
h1{font-size:30px}p{max-width:1000px}.note{border-left:4px solid #bc864e;padding:12px;background:#fff8ea}
details{background:white;border:1px solid #deded1;border-radius:10px;margin:16px 0;padding:16px}summary{cursor:pointer;font-weight:650}
.previews,.plots{display:flex;flex-wrap:wrap;gap:12px}figure{margin:12px 0;flex:1 1 240px}figure img{display:block;max-width:100%;height:auto}
.previews img{width:256px;image-rendering:pixelated}.plots figure{flex-basis:480px}figcaption{font-size:13px;color:#56645f}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:8px;text-align:left;border-bottom:1px solid #e0e1d7;white-space:nowrap}
.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}a{color:#246b60}
</style><h1>Reference Sensor Mode Lab</h1>
<p>Same source, lens and pixel response. Readout modes are explicit research models, not claims about a commercial sensor mode.</p>
<p class="note">Binned electron signal and noise are normalized per contributing native pixel. Linear RGB uses a common fixed ISP scale. Noise is measured across independent captures, not across spatial texture. Saturation includes invalid pixels. Preview enlargement does not restore acquired detail. SFR includes sensor sampling and ISP; it is not optical MTF.</p>"""
    document += (
        "<nav>"
        + " · ".join(f'<a href="#case-{i}">{_esc(c.get("id", i))}</a>' for i, c in enumerate(cases))
        + "</nav>"
    )
    document += (
        '<h2>Response and noise curves</h2><div class="plots">'
        + plots
        + "</div><h2>Native mode comparisons</h2>"
        + "".join(parts)
    )
    document += (
        "<h2>Source and model assumptions</h2><pre>"
        + _esc(json.dumps(source, indent=2, ensure_ascii=False))
        + "</pre></html>"
    )
    path = root / "report.html"
    path.write_text(document, encoding="utf-8")
    return path
