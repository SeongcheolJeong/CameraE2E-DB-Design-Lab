"""Real prescription → absolute pixel QE → native RAW → fixed ISP workshop loop."""

from __future__ import annotations

import html
import json
import shutil
import time
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter

from . import design_workshop_optics as optics
from . import design_workshop_sensor as pixel
from . import virtual_lab_metrics as metrics
from .electrical import capture_electrons
from .registry import digest
from .virtual_lab_engine import XYZ_TO_RGB, _png, _process, _spectra
from .virtual_lab_job import sha256

WAVE = np.arange(450.0, 651.0, 10.0)


def write_json(path, data):
    Path(path).write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            default=lambda v: v.tolist() if isinstance(v, np.ndarray) else v.item(),
        )
    )


def _notify(callback, text):
    if callback:
        callback({"stage": text})


def _optics_directory(spec, cache):
    return Path(cache) / "optics" / digest({"specification": spec, "backend": optics._backend()})


def _solution(spec, cache, callback):
    directory = _optics_directory(spec, cache)
    optics.solve(spec, directory, on_progress=callback)
    return optics.load_solution(directory)


def _bounds(geometry):
    bounds = geometry["crop_bounds_row_col"]
    if len(bounds) == 2:
        return [bounds[0][0], bounds[0][1], bounds[1][0], bounds[1][1]]
    return bounds


def _patterns(kind, shape):
    from pyisetcam.scene import img_deadleaves

    rows, cols = shape
    yy, xx = np.indices(shape)
    pattern = np.full(shape, 0.18)
    if kind == "color":
        return np.minimum(3, yy * 4 // rows) * 6 + np.minimum(5, xx * 6 // cols)
    if kind == "texture":
        leaves = img_deadleaves([rows, cols], 3, {"seed": 901})
        return 0.18 + 0.08 * (leaves - np.mean(leaves)) / max(float(np.ptp(leaves)), 1e-9)
    if kind == "uniform":
        return pattern
    if kind == "field_color":
        # Identical six-patch sequence at three field positions; field and color are separable.
        local = np.minimum(5, ((xx * 3 / cols) % 1) * 6).astype(int)
        return np.array([18, 12, 13, 14, 15, 19])[local]
    if kind == "composite":
        pattern = 0.12 + 0.18 * (yy < rows * 0.38)
        pattern += 0.06 * (np.sin(xx / 2) * np.sin(yy / 3)) * (yy > rows * 0.7)
        pattern[:, cols // 2 - 1 : cols // 2 + 1] = 0.7
        return pattern
    for r0, r1, c0, c1 in metrics.five_site_rois(shape).values():
        rr, cc = np.indices((r1 - r0, c1 - c0))
        pattern[r0:r1, c0:c1] = np.where(
            cc - (c1 - c0) / 2 - (rr - (r1 - r0) / 2) / 12 > 0, 0.75, 0.08
        )
    return pattern


def _scene(kind, geometry, spectral):
    shape = geometry["native_shape_row_col"]
    pattern = _patterns(kind, shape)
    photons = (
        spectral["patches"][pattern]
        if kind in {"color", "field_color"}
        else pattern[..., None] * spectral["white"]
    )
    r0, r1, c0, c1 = _bounds(geometry)
    h, w = geometry["input_shape_row_col"]
    # Explicit analytic chart continuation, not convolution zero padding.
    return np.pad(photons, ((r0, h - r1), (c0, w - c1), (0, 0)), mode="edge"), pattern


def _apply(solution, photons, response):
    def angular(theta, phi, wave):
        return pixel.absolute_angular_response(response, theta, phi, wave)

    return optics.apply_spectral_radiance(
        solution,
        photons,
        pixel_qe=np.asarray(response["normal_qe"]),
        wavelength_nm=WAVE,
        angular_response=angular,
    )


def _calibration(workspace, cache, callback):
    from .design_workshop_job import fingerprint

    baseline_cell = pixel.build_cell(workspace, cfa_thickness_scale=1)
    key = digest(
        {
            "cell": baseline_cell,
            "source": fingerprint(),
            "optical_backend": optics._backend(),
            "kind": "baseline-spectral-color-v1",
        }
    )
    path = Path(cache) / ("calibration-" + key + ".json")
    if path.exists():
        cached = json.loads(path.read_text())
        if cached.get("sha256") != digest(cached.get("calibration")):
            raise ValueError("Cached color calibration changed; refusing reuse.")
        return cached["calibration"]
    response = pixel.build_response(baseline_cell, wavelength_nm=WAVE)
    solution = _solution(optics.default_specification(pixel_pitch_um=3.45), cache, callback)
    spectral = _spectra(workspace, "D65", 1000)
    shape = solution["result"]["geometry"]["input_shape_row_col"]
    photons = np.broadcast_to(spectral["white"], (*shape, len(WAVE))).copy()
    applied = _apply(solution, photons, response)
    # Use the actual admitted pupil directions and their wavelength-dependent
    # weights, exactly as capture does. Normal-incidence QE would be a different
    # camera even at the image center, where the pupil contains oblique rays.
    site_response = np.asarray(applied["center_electron_rate_per_band"]) * 0.004
    response_per_band = site_response[:, [0, 1, 3]].copy()
    response_per_band[:, 1] = (site_response[:, 1] + site_response[:, 2]) / 2
    desired = spectral["xyz_per_photon"] * spectral["white"][:, None] / 1000
    white_response, white_target = response_per_band.sum(axis=0), desired.sum(axis=0)
    # White-constrained spectral fit at one frozen development condition.
    scale = max(float(np.max(response_per_band)), 1e-30)
    x = response_per_band / scale
    wc = white_response / scale
    lhs = np.block([[x.T @ x, wc[:, None]], [wc[None, :], np.zeros((1, 1))]])
    rhs = np.vstack([x.T @ desired, white_target])
    matrix = np.linalg.solve(lhs, rhs)[:3] / scale @ XYZ_TO_RGB.T
    result = {
        "matrix": matrix.tolist(),
        "baseline_cell_sha256": baseline_cell["cell_sha256"],
        "baseline_lens": optics.default_specification(pixel_pitch_um=3.45),
        "method": "fixed_baseline_D65_spectral_fit_with_white_constraint",
        "reference_exposure_ms": 4,
        "reference_luminance_cd_m2": 1000,
        "spectral_response_basis": "actual_center_pupil_averaged_electrons_per_band",
        "spectral_reference": spectral["asset"],
        "chromatic_adaptation": spectral["chromatic_adaptation"],
        "per_candidate_fit": False,
        "physical_calibration": False,
    }
    write_json(path, {"calibration": result, "sha256": digest(result)})
    return result


def _rate(workspace, solution, response, spec, kind, cache, callback):
    from .design_workshop_job import fingerprint

    key = digest(
        {
            "lens": spec["lens"],
            "pixel": spec["pixel"],
            "kind": kind,
            "luminance": spec["luminance_cd_m2"],
            "response": response,
            "source": fingerprint(),
            "optical_revision": solution["result"]["revision"],
        }
    )
    path = Path(cache) / "rates" / key
    metadata_path, array_path = path / "metadata.json", path / "rate.npy"
    if metadata_path.exists() and array_path.exists():
        saved = json.loads(metadata_path.read_text())
        if saved["sha256"] != sha256(array_path):
            raise ValueError("Cached mean response changed; refusing reuse.")
        return np.load(array_path), saved["metadata"], True
    _notify(callback, f"PSF + absolute pixel QE → {kind} electron rate")
    spectral = _spectra(workspace, "D65", spec["luminance_cd_m2"])
    photons, _ = _scene(kind, solution["result"]["geometry"], spectral)
    applied = _apply(solution, photons, response)
    path.mkdir(parents=True, exist_ok=True)
    np.save(array_path, applied["mean_electron_rate"])
    write_json(metadata_path, {"sha256": sha256(array_path), "metadata": applied["metadata"]})
    return applied["mean_electron_rate"], applied["metadata"], False


def _capture(rate, model, spec, seed, *, noise_enabled=True):
    return capture_electrons(
        np.asarray(rate * spec["exposure_ms"] / 1000, dtype=float),
        spec["exposure_ms"] / 1000,
        model,
        analog_gain=spec["gain"],
        temperature_c=25,
        geometry={"full_sensor_shape": list(rate.shape), "crop_origin": [0, 0]},
        noise_enabled=noise_enabled,
        seed=seed,
    )


def _reference_image(kind, shape, spectral):
    """Ideal source display in the same fixed luminance/color reference scale."""
    pattern = _patterns(kind, shape)
    if kind in {"color", "field_color"}:
        xyz = np.asarray(spectral["reference_xyz"])[pattern]
    else:
        xyz = pattern[..., None] * np.asarray(spectral["white_xyz"])
    return xyz @ XYZ_TO_RGB.T


def run(workspace, output_dir, spec, cache, on_progress=None):
    start = time.monotonic()
    root, cache = Path(output_dir), Path(cache)
    root.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    solution = _solution(spec["lens"], cache, on_progress)
    cell = pixel.build_cell(workspace, cfa_thickness_scale=spec["pixel"]["cfa_thickness_scale"])
    response = pixel.build_response(cell, wavelength_nm=WAVE)
    calibration = _calibration(workspace, cache, on_progress)
    spectral = _spectra(workspace, "D65", spec["luminance_cd_m2"])
    write_json(root / "pixel-cell.json", cell)
    write_json(root / "pixel-response.json", response)
    write_json(root / "optical-response.json", solution["result"])
    # Optical diagnostics are copied with their source identity, not re-rendered approximations.
    optical_directory = _optics_directory(spec["lens"], cache)
    if optical_directory.exists():
        shutil.copytree(optical_directory, root / "optics", dirs_exist_ok=True)
    captures = {}
    kinds = list(
        dict.fromkeys([spec["scene"], "resolution", "color", "uniform", "texture", "field_color"])
    )
    for kind in kinds:
        rate, metadata, reused = _rate(
            workspace, solution, response, spec, kind, cache, on_progress
        )
        _notify(on_progress, f"Independent readouts + fixed ISP: {kind}")
        folder = root / kind
        folder.mkdir(exist_ok=True)
        reference = _reference_image(kind, rate.shape, spectral)
        np.save(folder / "source-reference-linear.npy", reference.astype(np.float32))
        _png(folder / "source-reference.png", reference, gamma=True)
        np.save(folder / "mean-electron-rate.npy", rate)
        records = []
        for repeat in range(spec["frames"]):
            captured = _capture(rate, cell["electrical_model"], spec, spec["seed"] + repeat)
            name = f"raw-{repeat:02d}.npy"
            np.save(folder / name, captured["arrays"]["raw_dn"])
            records.append(
                {"file": name, "sha256": sha256(folder / name), "seed": spec["seed"] + repeat}
            )
            if repeat == 0:
                write_json(folder / "capture.json", captured["summary"])
        captures[kind] = {
            "raw": records,
            "rate_sha256": sha256(folder / "mean-electron-rate.npy"),
            "source_reference_sha256": sha256(folder / "source-reference-linear.npy"),
            "response_cache_reused": reused,
            "optical_pixel_contract": metadata,
        }
    info = {
        "schema": "camera_design_workshop_capture_v1",
        "configuration": spec,
        "geometry": solution["result"]["geometry"],
        "captures": captures,
        "readout": {**cell["readout"], "gain": spec["gain"]},
        "calibration": calibration,
        "scene_reference": {
            "reference_xyz": spectral["reference_xyz"].tolist(),
            "white_xyz": spectral["white_xyz"].tolist(),
            "asset": spectral["asset"],
            "chromatic_adaptation": spectral["chromatic_adaptation"],
            "display_reference_luminance_cd_m2": 1000,
            "source_workspace": str(Path(workspace).resolve()),
        },
        "cell_sha256": cell["cell_sha256"],
        "physical_validation": "not_established",
    }
    write_json(root / "capture.json", info)
    result = _render(root, info, spec, solution["result"], response, cell)
    result["performance"] = {
        "elapsed_seconds": time.monotonic() - start,
        "wall_clock_is_vehicle_latency": False,
    }
    return result


def _render(root, capture, spec, optical_result, response, cell, *, process=None,
            observer=None, texture_reference_process=None):
    process = process or _process
    matrix = np.asarray(capture["calibration"]["matrix"])
    display_white = float(spec["isp"].get("display_white_level", 1))

    def display_image(processed):
        return np.clip(processed / display_white, 0, 1) ** (1 / spec["isp"]["tone_gamma"])

    # Fixed exposure display reference; exposure changes remain visibly brighter/darker.
    images, tests, rendered = {}, {}, {}
    noise = {}
    for kind, data in capture["captures"].items():
        folder = root / kind
        if sha256(folder / "mean-electron-rate.npy") != data["rate_sha256"]:
            raise ValueError("Preserved mean response changed before ISP replay.")
        reference_path = folder / "source-reference-linear.npy"
        if sha256(reference_path) != data["source_reference_sha256"]:
            raise ValueError("Preserved source reference changed before ISP replay.")
        _png(folder / "source-reference.png", np.load(reference_path) / display_white, gamma=True)
        displays, linears, linear = [], [], None
        raw_frames = []
        for record in data["raw"]:
            path = folder / record["file"]
            if sha256(path) != record["sha256"]:
                raise ValueError("Preserved RAW changed before ISP replay.")
            raw = np.load(path)
            raw_frames.append(raw)
            lin, proc, _ = process(raw, capture["readout"], matrix, spec["isp"])
            if observer is not None:
                observer.frame(kind, len(raw_frames) - 1, folder, raw, lin, proc)
            displays.append(proc)
            linears.append(lin)
            if linear is None:
                linear = lin
                _png(folder / "display.png", display_image(proc))
                _png(folder / "linear.png", lin / display_white, gamma=True)
                black = capture["readout"]["black_level_dn"]
                white = 2 ** capture["readout"]["bit_depth"] - 1
                _png(folder / "raw.png", (raw.astype(float) - black) / (white - black))
                np.save(folder / "linear.npy", lin.astype(np.float32))
                np.save(folder / "processed.npy", proc.astype(np.float32))
        images[kind] = {
            "display": f"{kind}/display.png",
            "linear": f"{kind}/linear.png",
            "reference": f"{kind}/source-reference.png",
            "raw_preview": f"{kind}/raw.png",
            "raw": f"{kind}/raw-00.npy",
            "mean": f"{kind}/mean-electron-rate.npy",
        }
        mean_linear, mean_processed = np.mean(linears, axis=0), np.mean(displays, axis=0)
        rendered[kind] = (mean_linear, mean_processed)
        if observer is not None:
            observer.chart(kind, folder, raw_frames, linears, displays, images[kind])
        if kind in {"uniform", "texture"}:
            from .reference_isp import readout_dn_per_e

            raw_green = np.asarray(raw_frames)[:, 0::2, 1::2]
            readout = capture["readout"]
            dn_per_e = readout_dn_per_e(readout)
            dark_dn = readout["dark_current_e_per_s"] * spec["exposure_ms"] / 1000 * dn_per_e
            noise[kind] = {
                "raw_green": metrics.temporal_noise_metrics(
                    raw_green, black_level=readout["black_level_dn"] + dark_dn
                ),
                "processed": metrics.temporal_noise_metrics(
                    np.asarray(displays), unit="linear_srgb"
                ),
            }
            noise[kind]["raw_green"]["signal_definition"] = (
                "measured temporal mean minus black and declared mean dark DN"
            )
            noise[kind]["raw_green"]["dark_signal_subtracted_dn"] = dark_dn
        if kind == "resolution":
            tests["resolution"] = metrics.resolution_metrics(
                mean_linear, mean_processed, pixel_pitch_um=spec["lens"]["pixel_pitch_um"]
            )
            tests["resolution"]["repeat_aggregation"] = (
                "mean of independently acquired, individually processed frames"
            )
        if kind == "color":
            h, w = linear.shape[:2]
            rois = [
                [
                    int((r + 0.25) * h / 4),
                    int((r + 0.75) * h / 4),
                    int((c + 0.25) * w / 6),
                    int((c + 0.75) * w / 6),
                ]
                for r in range(4)
                for c in range(6)
            ]
            reference = capture["scene_reference"]
            tests["color"] = metrics.color_metrics(
                mean_processed,
                reference["reference_xyz"],
                patch_rois=rois,
                white_xyz=reference["white_xyz"],
                reference_provenance=reference["asset"],
            )
            tests["color"]["repeat_aggregation"] = (
                "mean of independently acquired, individually processed frames"
            )
            tests["color"]["white_reference_policy"] = (
                "declared source illuminant and luminance; no output fitting or display scaling"
            )
        if kind == "texture":
            rate_path = folder / "mean-electron-rate.npy"
            if sha256(rate_path) != data["rate_sha256"]:
                raise ValueError("Preserved mean response changed before texture diagnosis")
            deterministic = _capture(
                np.load(rate_path), cell["electrical_model"], spec, 0, noise_enabled=False
            )
            clean_linear, clean_processed, _ = process(
                deterministic["arrays"]["raw_dn"], capture["readout"], matrix, spec["isp"]
            )
            _png(folder / "deterministic-display.png", display_image(clean_processed))
            images[kind]["deterministic_display"] = f"{kind}/deterministic-display.png"
            before, after = clean_linear @ metrics.LUMA, clean_processed @ metrics.LUMA
            if texture_reference_process is not None:
                _, fixed, _ = texture_reference_process(
                    deterministic["arrays"]["raw_dn"], capture["readout"], matrix, spec["isp"]
                )
                before = fixed @ metrics.LUMA
            hi0 = before - gaussian_filter(before, 1)
            hi1 = after - gaussian_filter(after, 1)
            tests["texture"] = {
                "linear_highpass_rms": float(np.sqrt(np.mean(hi0**2))),
                "processed_highpass_rms": float(np.sqrt(np.mean(hi1**2))),
                "contrast_ratio_to_linear": float(np.std(hi1) / max(np.std(hi0), 1e-15)),
                "meaning": (
                    "Deterministic readout dead-leaves highpass contrast; temporal noise "
                    "reported separately; not lens MTF"
                ),
                "noise_enabled": False,
                "quantization_and_clipping_retained": True,
            }
            if texture_reference_process is not None:
                tests["texture"]["reference_policy"] = "fixed_same_source_bypass_simulation_reference"
                if observer is not None:
                    observer.texture(before, after, tests["texture"])
    tests["noise"] = noise
    mean = np.load(root / "uniform" / "mean-electron-rate.npy") * spec["exposure_ms"] / 1000
    captures = json.loads((root / "uniform" / "capture.json").read_text())
    tests["signal"] = {
        "mean_electrons": float(mean.mean()),
        "saturation": captures["saturation_fractions"],
    }
    tests["signal"]["mean_electrons_definition"] = (
        "pre-noise, pre-clipping expected photoelectrons averaged over native CFA sites"
    )
    tests["signal"]["mean_electrons_by_site"] = {
        name: float(mean[row::2, col::2].mean())
        for name, row, col in [("R", 0, 0), ("Gr", 0, 1), ("Gb", 1, 0), ("B", 1, 1)]
    }
    if "field_color" in rendered:
        field = rendered["field_color"][1]
        h, w = field.shape[:2]
        tests["field_color"] = {
            "identical_patches": ["neutral", "blue", "green", "red", "yellow", "gray"],
            "positions": [
                {
                    "field": label,
                    "patch_linear_rgb": [
                        field[
                            h // 3 : 2 * h // 3,
                            int((j + k / 6 + 0.02) * w / 3) : int((j + (k + 1) / 6 - 0.02) * w / 3),
                        ]
                        .mean(axis=(0, 1))
                        .tolist()
                        for k in range(6)
                    ],
                }
                for j, label in enumerate(["left", "center", "right"])
            ],
            "interpretation": "Same six patch spectra repeated across sensor; fixed ISP",
        }
    result = {
        "schema": "camera_design_workshop_result_v1",
        "configuration": spec,
        "geometry": capture["geometry"],
        "images": images,
        "image_sets": images,
        "tests": tests,
        "optics": optical_result,
        "pixel": {"cell": cell, "response": response},
        "calibration": capture["calibration"],
        "capture_files": capture["captures"],
        "raw_preserved": True,
        "raw_preview": {
            "transfer": "linear black-subtracted DN; no frame normalization or gamma",
            "black_dn": capture["readout"]["black_level_dn"],
            "white_dn": 2 ** capture["readout"]["bit_depth"] - 1,
            "native_cfa": "RGGB",
        },
        "physical_validation": "not_established",
        "limitations": [
            "Authored prescription within its supported narrow field.",
            "Native sampled source and pixel-integrated PSF; no second pixel-aperture filtering.",
            "Absolute incoherent layer QE; no full 3D OCL/DTI or charge transport.",
            "Fixed assumed readout and baseline spectral color transform; "
            "no per-frame normalization.",
        ],
    }
    result["metrics"] = {
        "mean_electrons": tests["signal"]["mean_electrons"],
        "delta_e00": tests["color"]["delta_e00_mean"],
        "temporal_noise_dn": noise["uniform"]["raw_green"]["temporal_std"],
        "texture_contrast_ratio": tests["texture"]["contrast_ratio_to_linear"],
    }
    write_json(root / "render.json", result)
    return result


def replay(source, output_dir, spec, on_progress=None):
    start = time.monotonic()
    preserved = json.loads((Path(source) / "capture.json").read_text())
    if any(
        spec.get(key) != value
        for key, value in preserved["configuration"].items()
        if key not in {"name", "isp"}
    ):
        raise ValueError("Same-RAW replay may change only ISP and name")
    root = Path(output_dir)
    shutil.copytree(
        source,
        root,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "manifest.json", "progress.json", "result.json", "report.html"
        ),
    )
    _notify(on_progress, "Same preserved RAW → changed ISP; acquisition skipped")
    capture = json.loads((root / "capture.json").read_text())
    result = _render(
        root,
        capture,
        spec,
        json.loads((root / "optical-response.json").read_text()),
        json.loads((root / "pixel-response.json").read_text()),
        json.loads((root / "pixel-cell.json").read_text()),
    )
    result["performance"] = {
        "elapsed_seconds": time.monotonic() - start,
        "acquisition_reused": True,
    }
    return result


def report(result, root):
    candidates = result.get("candidates", [result])
    sections = []
    for index, item in enumerate(candidates):
        prefix = item["id"] + "/" if "candidates" in result else ""
        cards = []
        for kind, paths in item.get("images", {}).items():
            for stage, label in (("reference", "ideal source"), ("display", "native capture")):
                if stage in paths:
                    source = html.escape(prefix + paths[stage], quote=True)
                    caption = html.escape(kind + " · " + label)
                    cards.append(
                        f'<figure><img src="{source}"><figcaption>{caption}</figcaption></figure>'
                    )
        scores = html.escape(json.dumps(item.get("metrics", {}), indent=2))
        settings = html.escape(json.dumps(item.get("configuration", {}), indent=2))
        limits = html.escape("; ".join(item.get("limitations", [])))
        sections.append(
            f'<h2>Candidate {index + 1}</h2><div class="images">{"".join(cards)}</div>'
            f"<pre>{scores}</pre><details><summary>Exact configuration and limits</summary>"
            f"<pre>{settings}</pre><p>{limits}</p></details>"
        )
    page = (
        '<!doctype html><html lang="ko"><meta charset="utf-8">'
        "<title>Camera Design Workshop</title><style>"
        "body{font:16px system-ui;background:#101820;color:#e8eef2;max-width:1300px;"
        "margin:auto;padding:32px}.images{display:flex;flex-wrap:wrap}"
        "figure{margin:8px}img{max-width:380px;image-rendering:auto}"
        "pre{white-space:pre-wrap}details{background:#21303d;padding:16px}</style>"
        "<h1>Lens · Pixel · ISP 설계 워크숍</h1>"
        "<p>실제 처방 계산 → 절대 픽셀 QE → 보존된 RAW → 공통 ISP. 연구용 모델 기반 진단.</p>"
        + "".join(sections)
        + "</html>"
    )
    (Path(root) / "report.html").write_text(page)
