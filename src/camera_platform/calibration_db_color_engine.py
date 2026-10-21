"""Fresh DB optical Macbeth capture with explicit absolute QE and ISP CCM replacement.

This is a synthetic derived-model transfer. Its four paths separate the original
DB output space from a fair comparison in the imported camera-linear target space.
No saved electron rate is reused and no new independent observation is invented.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import numpy as np
from PIL import Image

from . import calibration_ccm as ccm
from . import calibration_db_engine as electrical_bridge
from . import calibration_spectral as spectral_calibration
from . import design_workshop_engine as charts
from . import reference_isp
from . import reference_workshop_optics as optics
from . import reference_workshop_sensor as sensor
from .calibration_db_optics_engine import prepare_optical_source
from .registry import canonical
from .virtual_lab_engine import _spectra

FIELDS = ("center", "outer-ne")
VARIANTS = ("db_baseline", "qe_only_db_isp", "qe_ccm_baseline", "qe_ccm_fitted")
WAVE = np.arange(450.0, 651.0, 10.0)
SPECTRAL_FILE = "derived/db-color-spectra.json"
LIMITATIONS = [
    "Synthetic profile transfer creates a new model; "
    "it does not calibrate the source product camera.",
    "The retained DB optical operator uses only 450–650 nm; no full 380–780 nm or NIR claim.",
    "Imported absolute CFA-inclusive QE replaces the original pixel response once.",
    "Imported QE has no angular scan: "
    "angle independence and identical Gr/Gb are explicit assumptions.",
    "Lens transport, sampled optical PSF and receiving pixel aperture are retained exactly once.",
    "Native 256x256 local field probes are not full-sensor images "
    "or a matched physical camera FOV.",
    "DB readout remains frozen; scan DN/e is calibration provenance, not a readout override.",
    "Imported CCM replaces the DB electron-to-color transform, rather than stacking after it.",
    "Camera-linear target is signed pre-clipping ISP output, not standard sRGB or CIE colorimetry.",
    "Preview clipping/gamma is fixed display-only; "
    "signed linear arrays and patch metrics are preserved.",
    "Comparisons measure change only. "
    "No matched new observation or independent DB validation exists.",
]


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value):
    return canonical(value).encode()


def _matrix(value, name):
    result = np.asarray(value, dtype=float)
    if result.shape != (3, 3) or not np.isfinite(result).all():
        raise ValueError(name + " requires a finite 3x3 matrix")
    return result


def prepare(
    workspace, source_root, summary, candidate_id, condition_id=None, expected_manifest_sha256=None
):
    """Verify immutable DB transport, and freeze exact generated Macbeth spectra.

    ``frozen_files`` are original manifest-bound bytes; ``derived_files`` are
    explicitly generated spectra. A reconstructed tree must reproduce the exact
    frozen spectral bytes, so changing workspace assets cannot alter a queued run.
    """
    p = prepare_optical_source(
        source_root, summary, candidate_id, condition_id, expected_manifest_sha256
    )
    spec = deepcopy(p["candidate"]["result"]["configuration"])
    reference_isp.validate_profile(spec["isp"])
    if (
        spec.get("frames") != 8
        or spec.get("chart_suite") != "classic_v1"
        or spec["isp"]["lsc_enabled"]
    ):
        raise ValueError("DB color rerun requires eight-repeat classic_v1, fixed LSC-off source")
    if p["cell"].get("pixel_pitch_um") != 2.9:
        raise ValueError("Retained DB transport requires the original 2.9 um receiver")
    model = electrical_bridge._source_model(p["cell"])
    if not np.array_equal(p["pixel"].get("wavelength_nm"), WAVE):
        raise ValueError("DB color rerun supports the exact 21-band 450–650 nm source")
    field_map = {f["id"]: f for f in p["optical"]["fields"]}
    if not set(FIELDS) <= set(p["dc"]):
        raise ValueError("Both center and outer-ne preserved fields are required")
    calibration = None
    for fid in FIELDS:
        field, capture = field_map[fid], p["dc"][fid]["capture"]
        geometry = field["geometry"]
        phase = geometry.get("cfa_origin", {})
        if (
            field.get("status") != "succeeded"
            or geometry.get("native_shape_row_col") != [256, 256]
            or phase.get("pattern") != "RGGB"
            or phase.get("local_row", 0) != 0
            or phase.get("local_col", 0) != 0
            or geometry != capture["geometry"]
            or capture["configuration"] != spec
        ):
            raise ValueError(
                "DB field geometry/configuration differs from its frozen native capture"
            )
        color = capture["calibration"]
        _matrix(color["matrix"], "Frozen DB electron-to-color transform")
        if calibration is not None and calibration != color:
            raise ValueError("Fields do not share the same frozen DB color calibration")
        calibration = deepcopy(color)
    if type(spec.get("seed")) is not int or not 0 <= spec["seed"] <= 2**31 - 65536:
        raise ValueError("DB source seed is outside deterministic capture bounds")
    electrical_bridge.electrical.validate_capture_conditions(
        model, spec["exposure_ms"] / 1000, spec["gain"], 25
    )
    spectral = _spectra(workspace, "D65", spec["luminance_cd_m2"])
    spectral_document = {
        "schema": "camera_db_color_spectra_v1",
        "wavelength_nm": WAVE.tolist(),
        "source_radiance_unit": "photons/s/m2/sr/nm",
        "illuminant": "D65",
        "luminance_cd_m2": spec["luminance_cd_m2"],
        "data": {
            key: value.tolist() if isinstance(value, np.ndarray) else value
            for key, value in spectral.items()
        },
    }
    encoded = _json_bytes(spectral_document)
    stored = Path(source_root) / SPECTRAL_FILE
    if stored.exists() and (stored.is_symlink() or stored.read_bytes() != encoded):
        raise ValueError("Frozen DB chart spectra differ from current workspace assets")
    p.update(
        spec=spec,
        electrical_model=model,
        calibration=calibration,
        spectral=spectral,
        derived_files={SPECTRAL_FILE: encoded},
        spectral_sha256=_sha(encoded),
    )
    p["projection"] = projection(p)
    return p


def projection(prepared):
    """Project source units without inventing full-sensor or camera identity mappings."""
    result = electrical_bridge.baseline_projection(prepared)
    result.update(
        scope="frozen_db_readout_for_spectral_recompute",
        source_identity=deepcopy(prepared["source_identity"]),
        exposure_ms=prepared["spec"]["exposure_ms"],
        luminance_cd_m2=prepared["spec"]["luminance_cd_m2"],
        wavelength_nm=WAVE.tolist(),
        unmapped=[
            "Physical full-sensor dimensions and source-camera identity are not inferred.",
            "Imported angular sensitivity and separate Gr/Gb response are not identified.",
        ],
    )
    return result


def binding(prepared, color_record):
    """Return engine compatibility and exact applied operators; do not activate a model."""
    if (
        color_record.get("schema") != "camera_spectral_color_record_v1"
        or color_record.get("status") != "pass"
        or color_record.get("source_kind") != "synthetic"
    ):
        raise ValueError(
            "DB color v1 requires a passed synthetic color task and explicit derived-model binding"
        )
    qe, wave, values = spectral_calibration._profile(color_record["qe"]["profile"])
    profile = ccm._profile(color_record["ccm"]["profile"])
    validation = color_record["ccm"]["validation"]
    if (
        validation.get("status") != "pass"
        or validation.get("profile_sha256") != profile["profile_sha256"]
        or validation.get("refit_performed") is not False
        or color_record["raw_validation"]["candidate"].get("status") != "pass"
    ):
        raise ValueError("Color task must preserve passed frozen QE/CCM validation")
    if qe["source_kind"] != "synthetic" or profile["source_kind"] != "synthetic":
        raise ValueError(
            "Synthetic-model transfer cannot silently import measured-product identity"
        )
    if (
        profile["target_space"] != "camera_linear_rgb"
        or profile["normalization"] != "fixed_reference_electrons"
    ):
        raise ValueError("DB color v1 requires fixed_reference_electrons camera-linear CCM")
    if profile["conditions"] != qe["conditions"]:
        raise ValueError("Imported QE and CCM originate from different sensor conditions")
    source_conditions = projection(prepared)
    if 2 ** source_conditions["electronics"]["bit_depth"] - 1 != qe["adc_white_dn"]:
        raise ValueError("Imported scan and DB ADC ranges differ")
    if any(
        source_conditions["conditions"][key] != qe["conditions"][key]
        for key in ("gain", "temperature_c")
    ):
        raise ValueError("Imported scan and DB gain/temperature conditions differ")
    if wave[0] > WAVE[0] or wave[-1] < WAVE[-1]:
        raise ValueError("Imported absolute QE does not cover the complete DB spectral band")
    normalization = profile["normalization_metadata"]
    white = normalization.get("reference_white_e")
    if type(white) not in (int, float) or not np.isfinite(white) or not 0 < white <= 1e12:
        raise ValueError("Imported CCM requires an explicit positive fixed reference_white_e")
    white = float(white)
    if normalization.get("formula") != "(native_mean_dn-black_level_dn)/dn_per_e/reference_white_e":
        raise ValueError("Unsupported imported camera normalization formula")
    if normalization.get("dn_per_e") != qe["dn_per_e"]:
        raise ValueError("Imported CCM normalization differs from its scan DN/e")
    response = np.column_stack([np.interp(WAVE, wave, values[:, c]) for c in range(3)])
    factor = np.diag(np.asarray(profile["white_balance"]) * profile["exposure_scale"] / white)
    transform = factor @ _matrix(profile["matrix"], "Imported CCM")
    baseline = factor @ _matrix(profile["baseline_matrix"], "Imported baseline CCM")
    return {
        "schema": "camera_db_color_engine_binding_v1",
        "compatible": True,
        "binding_mode": "synthetic_model_transfer",
        "color_task_id": color_record["id"],
        "color_task_content_hash": color_record.get("content_hash"),
        "source_identity": deepcopy(prepared["source_identity"]),
        "qe_profile_sha256": qe["profile_sha256"],
        "ccm_profile_sha256": profile["profile_sha256"],
        "source_camera_id": qe["conditions"]["camera_id"],
        "source_conditions": deepcopy(qe["conditions"]),
        "applied_response": {
            "wavelength_nm": WAVE.tolist(),
            "qe_rgb": response.tolist(),
            "qe_rggb": response[:, [0, 1, 1, 2]].tolist(),
            "sites": ["R", "Gr", "Gb", "B"],
            "reference_plane": "sensor_surface",
            "included_effects": qe["included_effects"],
            "angular_policy": "angle_independent",
            "green_policy": "shared_green",
            "application": "absolute_replacement",
            "unit": "collected_electrons_per_incident_photon",
        },
        "isp_adapter": {
            "reference_white_e": white,
            "white_balance": profile["white_balance"],
            "exposure_scale": profile["exposure_scale"],
            "matrix": profile["matrix"],
            "baseline_matrix": profile["baseline_matrix"],
            "electron_to_output_matrix": transform.tolist(),
            "baseline_electron_to_output_matrix": baseline.tolist(),
            "matrix_convention": "row_vector",
            "target_space": "camera_linear_rgb",
            "delta_e_status": "not_applicable_camera_output_reproduction",
            "application": "demosaiced_electrons @ electron_to_output_matrix",
            "post_matrix_white_balance": [1.0, 1.0, 1.0],
            "post_matrix_ccm": np.eye(3).tolist(),
        },
        "db_readout": electrical_bridge._readout(
            prepared["electrical_model"], prepared["spec"]["gain"]
        ),
        "physical_validation": "not_established",
        "independent_db_validation": "not_established",
        "limitations": list(LIMITATIONS),
    }


def _save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def _save_image(path, pixels):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.rint(np.clip(pixels, 0, 1) * 255).astype(np.uint8)).save(path)


def _patches(values):
    """Use the inner half of each known native 4x6 Macbeth patch, no autodetection."""
    h, w = values.shape[:2]
    yy, xx = np.indices((h, w))
    pattern = charts._patterns("color", (h, w))
    output = []
    for index in range(24):
        r, c = divmod(index, 6)
        mask = (
            (pattern == index)
            & (yy >= (r + 0.25) * h / 4)
            & (yy < (r + 0.75) * h / 4)
            & (xx >= (c + 0.25) * w / 6)
            & (xx < (c + 0.75) * w / 6)
        )
        output.append(values[mask].mean(axis=0).tolist())
    return output


def _raw_metrics(stack, white):
    mean = stack.astype(float).mean(axis=0)
    yy, xx = np.indices(mean.shape)
    masks = [(yy % 2 == 0) & (xx % 2 == 0), yy % 2 != xx % 2, (yy % 2 == 1) & (xx % 2 == 1)]
    pattern = charts._patterns("color", mean.shape)
    rows = []
    h, w = mean.shape
    for index in range(24):
        r, c = divmod(index, 6)
        patch = (
            (pattern == index)
            & (yy >= (r + 0.25) * h / 4)
            & (yy < (r + 0.75) * h / 4)
            & (xx >= (c + 0.25) * w / 6)
            & (xx < (c + 0.75) * w / 6)
        )
        rows.append([float(mean[patch & mask].mean()) for mask in masks])
    return {
        "patch_rgb_dn": rows,
        "temporal_noise_dn": float(np.sqrt(np.mean(np.var(stack.astype(float), axis=0, ddof=1)))),
        "clipped_fraction": float(np.mean((stack == 0) | (stack == white))),
        "observer": "inner_half_native_R_Gmean_B_patch_means_v1",
    }


def _change(field_id, kind, before, after, unit):
    a, b = np.asarray(before), np.asarray(after)
    difference = b - a
    return {
        "field_id": field_id,
        "kind": kind,
        "unit": unit,
        "baseline": a.tolist(),
        "candidate": b.tolist(),
        "difference": difference.tolist(),
        "rmse_change": float(np.sqrt(np.mean(difference**2))),
        "max_abs_change": float(np.max(np.abs(difference))),
        "comparison_type": "change_only_no_reference_observation",
    }


def execute(prepared, color_record, directory, progress=None):
    """Recompute optics/QE, capture paired RAW, and apply four explicit ISP operators."""
    applied = binding(prepared, color_record)
    root = Path(directory)
    if root.exists() and any(p.name != "progress.json" for p in root.iterdir()):
        raise ValueError("DB color output directory must be new or empty")
    root.mkdir(parents=True, exist_ok=True)
    spec, model = prepared["spec"], prepared["electrical_model"]
    readout = applied["db_readout"]
    solution = {"result": prepared["optical"], "arrays": prepared["arrays"]}
    field_map = {f["id"]: f for f in prepared["optical"]["fields"]}
    imported = np.asarray(applied["applied_response"]["qe_rggb"])
    output_fields, images, comparisons = [], [], []
    _save_json(root / "binding.json", applied)
    _save_json(root / "source-configuration.json", spec)
    _save_json(root / "electrical-model.json", model)
    _save_json(root / "source-color-calibration.json", prepared["calibration"])
    _save_json(root / "qe-profile.json", color_record["qe"]["profile"])
    _save_json(root / "ccm-profile.json", color_record["ccm"]["profile"])
    for name, content in prepared["derived_files"].items():
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    for fi, fid in enumerate(FIELDS):
        field = field_map[fid]
        photons, _ = charts._scene("color", field["geometry"], prepared["spectral"])
        raw_paths, captures = {}, {}
        for qi, key in enumerate(("db_baseline", "qe_only_db_isp")):
            if progress:
                progress(
                    {
                        "stage": f"{fid} · {key} · fresh optical/QE transport",
                        "fraction": (fi * 6 + qi) / 12,
                    }
                )
            if key == "db_baseline":
                transport = optics.apply_field(
                    solution,
                    fid,
                    photons,
                    angular_response=lambda theta, phi, wave: sensor.absolute_angular_response(
                        prepared["pixel"], theta, phi, wave
                    ),
                )
            else:
                transport = optics.apply_field(solution, fid, photons, pixel_qe=imported)
            rate = transport["mean_electron_rate"]
            if rate.shape != (256, 256) or not np.isfinite(rate).all() or np.any(rate < 0):
                raise ValueError("Fresh DB transport returned invalid native electron rates")
            folder = root / fid / key
            folder.mkdir(parents=True)
            np.save(folder / "mean-electron-rate.npy", rate, allow_pickle=False)
            np.save(
                folder / "planes-electron-rate.npy",
                transport["planes_electron_rate"],
                allow_pickle=False,
            )
            _save_json(
                folder / "transport.json",
                {
                    **transport["metadata"],
                    "saved_rates_reused": False,
                    "qe_application": "source_angular"
                    if qi == 0
                    else "absolute_angle_independent_replacement",
                },
            )
            frames, records = [], []
            for repeat in range(8):
                if progress:
                    progress(
                        {
                            "stage": f"{fid} · {key} · RAW {repeat + 1}/8",
                            "fraction": (fi * 6 + qi + (repeat + 1) / 8) / 12,
                        }
                    )
                seed = spec["seed"] + fi * 32 + repeat
                capture = charts._capture(rate, model, spec, seed)
                raw = capture["arrays"]["raw_dn"]
                path = folder / f"raw-{repeat:02d}.npy"
                np.save(path, raw, allow_pickle=False)
                records.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "sha256": _sha(path.read_bytes()),
                        "seed": seed,
                        "saturation_fractions": capture["summary"]["saturation_fractions"],
                    }
                )
                frames.append(raw)
                if repeat == 0:
                    _save_json(folder / "capture.json", capture["summary"])
            raw_paths[key] = records
            captures[key] = np.stack(frames)
        variants = []
        for vi, variant in enumerate(VARIANTS):
            if progress:
                progress(
                    {
                        "stage": f"{fid} · {variant} · fixed native ISP",
                        "fraction": (fi * 6 + 2 + vi) / 12,
                    }
                )
            base = variant == "db_baseline"
            raw_key = "db_baseline" if base else "qe_only_db_isp"
            stack, records = captures[raw_key], raw_paths[raw_key]
            tuning = deepcopy(spec["isp"])
            matrix = np.asarray(prepared["calibration"]["matrix"])
            output_space = "db_bench_linear_srgb"
            if variant.startswith("qe_ccm"):
                matrix = np.asarray(
                    applied["isp_adapter"][
                        "baseline_electron_to_output_matrix"
                        if variant == "qe_ccm_baseline"
                        else "electron_to_output_matrix"
                    ]
                )
                tuning["wb"], tuning["ccm"] = [1.0] * 3, np.eye(3).tolist()
                tuning["color_calibration_id"] = None
                output_space = "imported_camera_linear_rgb_pre_clipping"
            linear, processed, display = reference_isp.process(stack[0], readout, matrix, tuning)
            folder = root / fid / variant
            folder.mkdir(parents=True, exist_ok=True)
            np.save(folder / "linear.npy", linear, allow_pickle=False)
            np.save(folder / "processed.npy", processed, allow_pickle=False)
            _save_image(folder / "display.png", display)
            white = 2 ** readout["bit_depth"] - 1
            _save_image(
                folder / "raw-preview.png",
                (stack[0].astype(float) - readout["black_level_dn"])
                / (white - readout["black_level_dn"]),
            )
            metadata = {
                "matrix": matrix.tolist(),
                "matrix_convention": "electron_row_vector",
                "tuning": tuning,
                "output_space": output_space,
                "refit_performed": False,
            }
            _save_json(folder / "applied-isp.json", metadata)
            paths = {
                key: (folder / name).relative_to(root).as_posix()
                for key, name in (
                    ("raw_preview", "raw-preview.png"),
                    ("isp", "display.png"),
                    ("linear", "linear.npy"),
                    ("processed", "processed.npy"),
                )
            }
            record = {
                "variant": variant,
                "output_space": output_space,
                "raw": {"frames": records, **_raw_metrics(stack, white)},
                "images": paths,
                "patch_linear_rgb": _patches(linear),
                "applied_isp": metadata,
            }
            variants.append(record)
            images.append(
                {
                    "variant": variant,
                    "field_id": fid,
                    "chart": "color",
                    "raw": records[0]["path"],
                    **paths,
                    "output_space": output_space,
                }
            )
        by_name = {v["variant"]: v for v in variants}
        for kind, first, last, key, unit in (
            ("qe_raw_change", "db_baseline", "qe_only_db_isp", "raw", "DN"),
            (
                "db_isp_change",
                "db_baseline",
                "qe_only_db_isp",
                "patch_linear_rgb",
                "DB_bench_linear_sRGB",
            ),
            (
                "ccm_output_change",
                "qe_ccm_baseline",
                "qe_ccm_fitted",
                "patch_linear_rgb",
                "camera_linear_RGB",
            ),
        ):
            a, b = by_name[first][key], by_name[last][key]
            if key == "raw":
                a, b = a["patch_rgb_dn"], b["patch_rgb_dn"]
            comparisons.append(_change(fid, kind, a, b, unit))
        same = all(
            by_name[v]["raw"]["frames"] == by_name["qe_only_db_isp"]["raw"]["frames"]
            for v in ("qe_ccm_baseline", "qe_ccm_fitted")
        )
        if not same:
            raise ValueError("ISP CCM changed source RAW identity")
        output_fields.append(
            {
                "field_id": fid,
                "geometry": field["geometry"],
                "variants": variants,
                "raw_identity": {"qe_variants_identical": same},
            }
        )
    result = {
        "schema": "camera_db_color_replay_v1",
        "status": "succeeded",
        "source_identity": prepared["source_identity"],
        "binding": applied,
        "projection": prepared["projection"],
        "configuration": spec,
        "fields": output_fields,
        "images": images,
        "comparisons": comparisons,
        "spectral_sha256": prepared["spectral_sha256"],
        "fresh_optical_transport": True,
        "fresh_raw_frames": 32,
        "saved_electron_rates_reused": False,
        "physical_validation": "not_established",
        "independent_db_validation": "not_established",
        "limitations": list(LIMITATIONS),
    }
    _save_json(root / "numerical-summary.json", result)
    result["files"] = {
        p.relative_to(root).as_posix(): _sha(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "progress.json"
    }
    if progress:
        progress(
            {
                "stage": "DB color rerun complete; independent observation unavailable",
                "fraction": 1.0,
            }
        )
    return result
