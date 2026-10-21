"""Manifest-bound DB optical transport to a sampled Calibration Lab blur prior.

This is model compression, not measured PSF identification. Spectral PSFs are
combined before their Fourier magnitude, using the exact legacy Reference
pupil/QE weighting. The sampled kernel retains phase and non-Gaussian shape.
Gaussian compression remains a diagnostic, never a silent substitute.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

from . import calibration_lab_engine as lab
from . import reference_workshop_optics as optics
from . import reference_workshop_sensor as sensor
from .db_design_conditions import VerifiedSource
from .offline_assets import offline_asset_identity, offline_asset_store
from .registry import digest
from .virtual_lab_engine import _spectra

SCHEMA = "camera_calibration_db_optics_conversion_v1"
WAVE = np.arange(450.0, 651.0, 10.0)
FREQUENCY = np.linspace(0.0, 0.5, 129)
ANGLES = np.arange(0.0, 180.0, 15.0)
SITES = ("R", "Gr", "Gb", "B")
FIT_LIMITS = {"rmse": 0.05, "max_abs": 0.15, "renderer_rmse": 0.005}
LIMITATIONS = [
    "Model-derived initialization only; no measured optical or camera validation.",
    "D65 450-650 nm, green Gr response; one shared RGB blur is a green-conditioned proxy.",
    "Pixel receiving aperture is already integrated; no second aperture, QE or blur is applied.",
    "Charge diffusion and spatial crosstalk are absent from the source; zero is an explicit "
    "assumption.",
    "Native sampled Gr PSF retains source centroid, phase and shape; the discarded Gaussian "
    "diagnostic loses these features.",
    "No subpixel response is reconstructed: the sampled kernel transfers native cell-center "
    "inputs, exactly as the source model.",
    "Only explicitly mapped local field nodes apply; no interpolation or unmeasured-field "
    "extrapolation.",
    "Lens field coordinates and image heights do not establish a physical full-sensor mapping.",
    "Electrical noise, nonlinear RAW/ISP operations, demosaic and RAW SFR observer are outside "
    "this transfer MTF.",
    "MTF agreement is evaluated over 0-0.5 cycles/native pixel, not an independent "
    "measured-camera acceptance.",
    "DB replay remains electrical-only with original optical rates frozen; reverse optical "
    "rerun is unsupported.",
]


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _pin(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _freeze(reader, prefix, files, bindings):
    bindings[prefix] = reader.manifest_sha256
    for name in list(reader.used):
        files[prefix + name] = reader.read(name)
    data = (reader.root / "manifest.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != reader.manifest_sha256:
        raise ValueError("Optical source manifest changed during preparation")
    files[prefix + "manifest.json"] = data


def _select(reader, summary, candidate_id, condition_id):
    actual = reader.json("result.json")
    if summary is not None and (
        not isinstance(summary, dict) or any(summary.get(k) != v for k, v in actual.items())
    ):
        raise ValueError("Optical source summary differs from its pinned result")
    if (
        actual.get("schema") != "camera_db_design_study_result_v1"
        or actual.get("status") != "succeeded"
    ):
        raise ValueError("Optical conversion requires a completed DB Design study")
    if actual.get("comparison_mode") == "aperture_conditions":
        if not isinstance(condition_id, str):
            raise ValueError("Condition-grid optics requires an explicit condition_id")
        matches = [
            c for c in actual["aperture_conditions"]["conditions"] if c.get("id") == condition_id
        ]
        if len(matches) != 1:
            raise ValueError("Unknown source optical condition")
        condition = matches[0]
        if reader.json(f"conditions/{condition_id}/result.json") != condition:
            raise ValueError("Condition differs from its preserved source")
        candidates, prefix = (
            condition["candidates"],
            f"conditions/{condition_id}/candidates/{candidate_id}/",
        )
    else:
        if condition_id is not None:
            raise ValueError("condition_id is forbidden for a non-condition source")
        candidates, prefix = actual.get("candidates", []), f"candidates/{candidate_id}/"
    selected = [c for c in candidates if c.get("id") == candidate_id]
    if len(selected) != 1:
        raise ValueError("Unknown or ambiguous optical source candidate")
    candidate = selected[0]
    if candidate.get("status") != "succeeded" or candidate.get("prefix") != prefix:
        raise ValueError("Optical candidate must be completed with its exact source prefix")
    if reader.json(prefix + "result.json") != candidate:
        raise ValueError("Optical candidate differs from its preserved result")
    if reader.json(prefix + "sources.json") != candidate["source_identities"]:
        raise ValueError("Optical candidate source identities changed")
    return actual, candidate, prefix


def prepare_optical_source(
    root, summary, candidate_id, condition_id=None, expected_manifest_sha256=None
):
    """Load only checked optical/QE/DC evidence, including an explicitly pinned ancestor.

    ``manifest_bindings`` partitions original ``frozen_files``. Generated spectral
    evidence is deliberately kept in ``derived_files`` by :func:`convert_source`.
    """
    if summary is None and not _pin(expected_manifest_sha256):
        raise ValueError("Omitted summary requires an optical source manifest SHA256 pin")
    if (
        not isinstance(candidate_id, str)
        or re.fullmatch(r"candidate-[0-9]{2}", candidate_id) is None
    ):
        raise ValueError("Optical source requires a candidate-NN identity")
    reader = VerifiedSource(root, expected_manifest_sha256)
    actual, candidate, prefix = _select(reader, summary, candidate_id, condition_id)
    optical = reader.json(prefix + "optical-response.json")
    pixel = reader.json(prefix + "pixel-response.json")
    cell = reader.json(prefix + "pixel-cell.json")
    if candidate["result"].get("optical_revision") != optical.get("revision"):
        raise ValueError("Candidate optical revision differs from its numerical response")
    if (
        pixel.get("cell_sha256") != cell.get("cell_sha256")
        or cell.get("cell_sha256")
        != hashlib.sha256(
            json.dumps(
                {k: v for k, v in cell.items() if k != "cell_sha256"},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
    ):
        raise ValueError("Pixel response is not bound to the selected cell")
    frozen, bindings, ancestors = {}, {}, []
    numerical_reader, numerical_prefix = reader, prefix
    arrays_name = optical.get("arrays_file")
    if arrays_name != "optics.npz":
        raise ValueError("Only the preserved legacy optics.npz transport is supported")
    array_relative = prefix + "optics/" + arrays_name
    if array_relative not in reader.files:
        source = candidate.get("source_capture", {})
        run_id, pin = source.get("run_id"), source.get("manifest_sha256")
        if (
            not isinstance(run_id, str)
            or not re.fullmatch(r"run-[0-9a-f]{32}", run_id)
            or not _pin(pin)
        ):
            raise ValueError(
                "Numerical optics absent and no exact source_capture ancestor is pinned"
            )
        # Frozen service trees retain their ancestor under this namespace. The
        # first conversion may resolve only the exact pinned sibling run.
        ancestor_root = reader.root / "ancestors" / run_id
        if not ancestor_root.exists():
            ancestor_root = reader.root.parent / run_id
        numerical_reader = VerifiedSource(ancestor_root, pin)
        ancestor, ancestor_candidate, numerical_prefix = _select(
            numerical_reader, None, candidate_id, None
        )
        if ancestor.get("id") != run_id:
            raise ValueError("Optical ancestor run identity differs from source_capture")
        for name, expected in (
            ("optical-response", optical),
            ("pixel-response", pixel),
            ("pixel-cell", cell),
        ):
            if numerical_reader.json(numerical_prefix + name + ".json") != expected:
                raise ValueError("Condition optics/QE differs from the pinned numerical ancestor")
        if ancestor_candidate["result"].get("optical_revision") != optical["revision"]:
            raise ValueError("Ancestor optical revision differs")
        array_relative = numerical_prefix + "optics/" + arrays_name
        ancestors.append({"run_id": run_id, "manifest_sha256": pin, "candidate_id": candidate_id})
    data = numerical_reader.read(array_relative)
    if hashlib.sha256(data).hexdigest() != optical.get("arrays_sha256"):
        raise ValueError("Optical NPZ hash differs from the response declaration")
    with np.load(io.BytesIO(data), allow_pickle=False) as archive:
        arrays = {key: archive[key] for key in archive.files}
    for array in arrays.values():
        if array.dtype.kind not in "iuf" or not np.isfinite(array).all():
            raise ValueError("Optical NPZ must contain finite numeric arrays only")
        array.setflags(write=False)
    dc = {}
    for field in optical.get("fields", []):
        if field.get("status") != "succeeded":
            continue
        fid = field["id"]
        name = prefix + f"fields/{fid}/capture.json"
        if name not in reader.files:
            continue
        capture = reader.json(name)
        rate_name = prefix + f"fields/{fid}/uniform/planes-electron-rate.npy"
        expected = capture.get("captures", {}).get("uniform", {}).get("planes_sha256")
        if reader.files.get(rate_name) != expected or not _pin(expected):
            raise ValueError("Preserved uniform planes must be manifest/capture bound")
        planes = reader.array(rate_name)
        if planes.shape != (*field["geometry"]["native_shape_row_col"], 4) or np.any(planes < 0):
            raise ValueError("Optical source uniform planes have incompatible geometry")
        dc[fid] = {"capture": capture, "uniform_planes": planes}
    if not dc or "center" not in dc:
        raise ValueError("Optical initialization requires a preserved center DC reference")
    _freeze(reader, "", frozen, bindings)
    if numerical_reader is not reader:
        _freeze(numerical_reader, "ancestors/" + ancestors[0]["run_id"] + "/", frozen, bindings)
    return {
        "optical": optical,
        "pixel": pixel,
        "cell": cell,
        "arrays": arrays,
        "dc": dc,
        "candidate": candidate,
        "frozen_files": frozen,
        "manifest_bindings": bindings,
        "source_identity": {
            "run_id": actual.get("id"),
            "candidate_id": candidate_id,
            "condition_id": condition_id,
            "manifest_sha256": reader.manifest_sha256,
            "candidate_sha256": digest(candidate),
            "optical_revision": optical["revision"],
            "optics_npz_sha256": optical["arrays_sha256"],
            "pixel_response_sha256": digest(pixel),
            "ancestors": ancestors,
        },
    }


def _validate_transport(payload, profile, *, allow_geometric=False):
    optical, pixel, arrays = payload["optical"], payload["pixel"], payload["arrays"]
    spec = optical.get("specification", {})
    if (
        optical.get("schema") != "camera_reference_workshop_optics_v1"
        or spec.get("optical_profile") not in ({"exit_pupil_diffraction", "geometric_retrace"} if allow_geometric else {"exit_pupil_diffraction"})
    ):
        raise ValueError("Only the explicit legacy exit-pupil diffraction response is supported")
    if spec.get("wavelength_nm") != WAVE.tolist() or pixel.get("wavelength_nm") != WAVE.tolist():
        raise ValueError("Conversion requires exact 21-band 450-650 nm optical and QE axes")
    if pixel.get("schema") != "camera_design_workshop_absolute_qe_v1" or pixel.get("sites") != list(
        SITES
    ):
        raise ValueError("Conversion requires the explicit absolute four-site QE response")
    if pixel.get("qe_units") != "electrons per sensor-plane incident photon":
        raise ValueError("Relative or ambiguous QE units cannot initialize camera blur")
    if profile["pixel_pitch_um"] != spec.get("pixel_pitch_um") or profile[
        "pixel_pitch_um"
    ] != payload["cell"].get("pixel_pitch_um"):
        raise ValueError(
            "Profile pitch must equal the native optical receiver pitch; rescaling is unsupported"
        )
    if (
        profile["pixel_pitch_um"] != 2.9
        or profile["cfa"] != "RGGB"
        or payload["cell"].get("cfa_pattern") != "RGGB"
    ):
        raise ValueError("Legacy optical transport supports its native 2.9 um RGGB receiver only")
    psf = arrays.get("psf")
    if psf is None or psf.ndim != 4 or psf.shape[:2] != (len(WAVE), len(optical["fields"])):
        raise ValueError("Optical PSF wavelength/field axes differ from metadata")
    if (
        psf.shape[2] != psf.shape[3]
        or psf.shape[2] % 2 != 1
        or psf.shape[2] > 257
        or np.any(psf < 0)
    ):
        raise ValueError("Optical kernels require finite nonnegative odd square support <=257")
    for key in ("conditional_weights", "directions_xyz", "irradiance_per_radiance_sr"):
        if key not in arrays:
            raise ValueError("Missing preserved pupil transport: " + key)
    for field in optical["fields"]:
        if field["id"] not in payload["dc"]:
            continue
        g = field.get("geometry", {})
        if (
            g.get("receiver_sampling") != "pixel_integrated_optical_impulse"
            or g.get("source_sampling") != "native_cell_center_with_optical_halo"
        ):
            raise ValueError(
                "Unrecognized sampling ownership; cannot prevent double pixel aperture"
            )
        if g.get("pixel_pitch_um") != 2.9 or g.get("cfa_origin", {}).get("pattern") != "RGGB":
            raise ValueError("Field sampling differs from the native receiver")
    return psf


def transfer_mtf(kernel, frequency=FREQUENCY, angles=ANGLES, *, spacing=1.0):
    """Magnitude of the complex 2-D kernel transform at declared native axes."""
    kernel = np.asarray(kernel, float)
    frequency, angles = np.asarray(frequency, float), np.asarray(angles, float)
    if kernel.ndim != 2 or not np.isfinite(kernel).all() or np.any(kernel < 0) or kernel.sum() <= 0:
        raise ValueError("Transfer kernel must be finite, nonnegative and nonzero")
    y = (np.arange(kernel.shape[0]) - kernel.shape[0] // 2) * spacing
    x = (np.arange(kernel.shape[1]) - kernel.shape[1] // 2) * spacing
    normalized = kernel / kernel.sum()
    result = []
    for angle in np.radians(angles):
        ex = np.exp(-2j * np.pi * frequency[:, None] * x * np.cos(angle))
        ey = np.exp(-2j * np.pi * frequency[:, None] * y * np.sin(angle))
        result.append(np.abs(np.sum((ey @ normalized) * ex, axis=1)))
    return np.asarray(result)


def gaussian_mtf(parameters, frequency=FREQUENCY, angles=ANGLES):
    angle = np.radians(np.asarray(angles)[:, None] - parameters["theta_deg"])
    variance = (
        parameters["sigma_x_px"] ** 2 * np.cos(angle) ** 2
        + parameters["sigma_y_px"] ** 2 * np.sin(angle) ** 2
    )
    return np.exp(-2 * np.pi**2 * variance * np.asarray(frequency)[None, :] ** 2)


def fit_gaussian(kernel):
    """Fit magnitude only; the original centroid is reported, never inferred away."""
    kernel = np.asarray(kernel, float)
    kernel = kernel / kernel.sum()
    yy, xx = np.indices(kernel.shape, dtype=float)
    xx -= kernel.shape[1] // 2
    yy -= kernel.shape[0] // 2
    centroid = [float(np.sum(xx * kernel)), float(np.sum(yy * kernel))]
    x, y = xx - centroid[0], yy - centroid[1]
    covariance = np.array(
        [
            [np.sum(kernel * x * x), np.sum(kernel * x * y)],
            [np.sum(kernel * x * y), np.sum(kernel * y * y)],
        ]
    )
    values, vectors = np.linalg.eigh(covariance)
    theta = np.degrees(np.arctan2(vectors[1, -1], vectors[0, -1]))
    observed = transfer_mtf(kernel)
    initial = [
        np.clip(np.sqrt(max(values[1], 0)), 0.031, 7.99),
        np.clip(np.sqrt(max(values[0], 0)), 0.031, 7.99),
        ((theta + 90) % 180) - 90,
    ]

    def residual(p):
        return (
            gaussian_mtf(dict(zip(("sigma_x_px", "sigma_y_px", "theta_deg"), p, strict=True)))
            - observed
        ).ravel()

    fitted = least_squares(
        residual,
        initial,
        bounds=([0.03, 0.03, -180], [8, 8, 180]),
        max_nfev=250,
        ftol=1e-10,
        xtol=1e-10,
        gtol=1e-10,
    )
    sx, sy, theta = map(float, fitted.x)
    if sy > sx:
        sx, sy, theta = sy, sx, theta + 90
    theta = (theta + 90) % 180 - 90
    parameters = {"sigma_x_px": sx, "sigma_y_px": sy, "theta_deg": theta}
    regenerated = gaussian_mtf(parameters)
    # Use the actual Calibration Lab renderer's returned quarter-pixel kernel,
    # not a second independently authored Gaussian implementation.
    _, rendered_kernel = lab._effective_blur(np.zeros((8, 8, 3)), parameters)
    rendered = transfer_mtf(rendered_kernel, spacing=0.25)
    error = regenerated - observed
    return {
        "parameters": parameters,
        "centroid_px": centroid,
        "source_mtf": observed,
        "reconstructed_mtf": regenerated,
        "renderer_mtf": rendered,
        "residuals": {
            "rmse": float(np.sqrt(np.mean(error**2))),
            "max_abs": float(np.max(np.abs(error))),
            "renderer_rmse": float(np.sqrt(np.mean((rendered - regenerated) ** 2))),
            "renderer_source_rmse": float(np.sqrt(np.mean((rendered - observed) ** 2))),
        },
        "optimizer": {
            "success": bool(fitted.success),
            "evaluations": int(fitted.nfev),
            "bound_active": bool(sx >= 7.999 or sy <= 0.030001),
        },
    }


def _spectral_evidence(payload):
    workspace = Path(__file__).resolve().parents[2]
    spectral = _spectra(workspace, "D65", 1000)
    identity = offline_asset_identity(workspace=workspace)
    store = offline_asset_store(workspace=workspace)
    derived = {
        "spectral-assets/" + name: store.resolve(name).read_bytes()
        for name in identity["required_files_sha256"]
    }
    evidence = {
        "method": "reconstructed_fixed_offline_reference_D65_operator_v1",
        "wavelength_nm": WAVE.tolist(),
        "photon_radiance_per_m2_sr_s_nm_at_1000_cd_m2": spectral["white"].tolist(),
        "bin_width_nm": [10.0] * len(WAVE),
        "offline_assets": identity,
        "original_source_spectrum_retained": False,
        "qualification": (
            "Reconstructed spectrum; checked against preserved source XYZ and "
            "four-site uniform photocharge DC."
        ),
    }
    derived["spectral-evidence.json"] = _json_bytes(evidence)
    return spectral, evidence, derived


def _mapping(field_mapping, available, optical_fields, field_mapping_mode):
    if field_mapping_mode not in (None, "reference_model"):
        raise ValueError("field_mapping_mode must be reference_model or omitted")
    if field_mapping_mode is not None and field_mapping is not None:
        raise ValueError("field_mapping_mode and explicit field_mapping are mutually exclusive")
    if field_mapping_mode == "reference_model":
        result, seen = {}, set()
        for field in optical_fields:
            if field["id"] not in available or field.get("status") != "succeeded":
                continue
            xy = np.asarray([field.get("x"), field.get("y")], float)
            if xy.shape != (2,) or not np.isfinite(xy).all() or np.max(np.abs(xy)) > 1:
                raise ValueError("Source reference field coordinates must be finite in [-1,1]")
            if field["id"] in result or tuple(xy) in seen:
                raise ValueError("Retained reference fields require unique IDs and coordinates")
            result[field["id"]] = xy.tolist()
            seen.add(tuple(xy))
        if not 1 <= len(result) <= 25:
            raise ValueError("Reference mapping requires 1..25 retained successful DC fields")
        return (
            result,
            "automatic_retained_reference_model_fields",
            "reference_lens_object_direction",
        )
    if field_mapping is None:
        return {"center": [0.0, 0.0]}, "center_only_local_reference", "sensor_normalized"
    if not isinstance(field_mapping, list) or not 1 <= len(field_mapping) <= min(
        25, len(available)
    ):
        raise ValueError("Explicit optical field_mapping requires 1..25 available source nodes")
    result, seen = {}, set()
    for row in field_mapping:
        if (
            not isinstance(row, dict)
            or set(row) != {"source_field_id", "field_xy"}
            or row["source_field_id"] not in available
        ):
            raise ValueError("Field mapping requires exact source_field_id and target field_xy")
        xy = np.asarray(row["field_xy"], float)
        if xy.shape != (2,) or not np.isfinite(xy).all() or np.max(np.abs(xy)) > 1:
            raise ValueError(
                "Target field_xy must be a finite normalized sensor coordinate in [-1,1]"
            )
        if row["source_field_id"] in result or tuple(xy) in seen:
            raise ValueError("Optical source and target field mappings must be unique")
        result[row["source_field_id"]] = xy.tolist()
        seen.add(tuple(xy))
    return result, "explicit_user_source_to_target_mapping", "sensor_normalized"


def project_optical_profile(payload, profile, *, field_mapping=None, field_mapping_mode=None, allow_geometric=False):
    """Retain exact sampled source transport with explicit local applicability."""
    profile = lab.validate_profile(profile)
    psf = _validate_transport(payload, profile, allow_geometric=allow_geometric)
    spectral, spectral_evidence, derived = _spectral_evidence(payload)
    optical, pixel, arrays = payload["optical"], payload["pixel"], payload["arrays"]
    mapping, mapping_kind, field_domain = _mapping(
        field_mapping, payload["dc"], payload["optical"]["fields"], field_mapping_mode
    )
    mapping_mode = field_mapping_mode or ("explicit_sensor" if field_mapping else "center_only")
    solution = {"result": optical, "arrays": arrays}
    fields, nodes, rejected = [], [], []
    kernels = {}

    def angular(theta, phi, wave):
        return sensor.absolute_angular_response(pixel, theta, phi, wave)

    for fi, field in enumerate(optical["fields"]):
        fid = field["id"]
        if fid not in payload["dc"]:
            continue
        qe = optics.effective_pupil_response(solution, fid, angular)
        ri = field.get("pupil_node_index", [0.0, 0.5, 0.85].index(field["normalized_radius"]))
        irradiance = arrays["irradiance_per_radiance_sr"][:, ri]
        weights = spectral["white"][:, None] * irradiance[:, None] * qe * 10.0 * (2.9e-6) ** 2
        if (
            weights.shape != (21, 4)
            or not np.isfinite(weights).all()
            or np.any(weights < 0)
            or np.any(weights.sum(axis=0) <= 0)
        ):
            raise ValueError("Invalid or unsupported spectral pupil/QE weights")
        sums = psf[:, fi].sum(axis=(1, 2))
        if not np.allclose(sums, 1, rtol=1e-8, atol=1e-10):
            raise ValueError("Optical PSF energy normalization differs from the source transport")
        combined = np.einsum("ws,wyx->syx", weights, psf[:, fi])
        combined /= combined.sum(axis=(1, 2))[:, None, None]
        # Source scene is a fixed18% neutral; condition reruns scale its incident
        # luminance. The independent retained DC catches changed QE/weighting.
        preserved = payload["dc"][fid]
        capture = preserved["capture"]
        luminance = capture["configuration"]["luminance_cd_m2"]
        predicted_dc = weights.sum(axis=0) * 0.18 * luminance / 1000
        measured_dc = np.mean(preserved["uniform_planes"], axis=(0, 1))
        dc_error = float(
            np.max(np.abs(predicted_dc - measured_dc) / np.maximum(measured_dc, 1e-30))
        )
        original_xyz = np.asarray(capture.get("scene_reference", {}).get("reference_xyz", []))
        reference_scale = capture.get("scene_reference", {}).get(
            "condition_reference_scale", luminance / 1000
        )
        expected_xyz = spectral["reference_xyz"] * reference_scale
        if original_xyz.shape != expected_xyz.shape or not np.allclose(
            original_xyz, expected_xyz, rtol=1e-9, atol=1e-10
        ):
            raise ValueError("Reconstructed D65 source reference differs from preserved chart XYZ")
        if dc_error > 1e-7:
            raise ValueError("Reconstructed spectral/QE weights fail preserved four-site DC parity")
        fitted = fit_gaussian(combined[1])
        node = {
            "family": "sampled_native_kernel_v1",
            "kernel": combined[1].tolist(),
            "kernel_pitch_px": 1.0,
            "sampling": "native_cell_center_pixel_integrated",
            **fitted["parameters"],
            "included_effects": ["lens_blur", "pixel_aperture"],
            "charge_diffusion_assumption": "zero_not_identified",
            "source_kind": "model_derived",
            "source_field_id": fid,
            "field_coordinate_domain": field_domain,
        }
        # Serialize/reload the exact profile representation, then ask the real
        # runtime for its applied kernel. These are the qualification metrics;
        # Gaussian compression below is retained only as a discarded diagnostic.
        stored = json.loads(_json_bytes(node))
        reconstructed = transfer_mtf(stored["kernel"])
        _, applied_kernel = lab._effective_blur(np.zeros((8, 8, 3)), stored)
        renderer = transfer_mtf(applied_kernel)
        exact_error = reconstructed - fitted["source_mtf"]
        exact_residuals = {
            "rmse": float(np.sqrt(np.mean(exact_error**2))),
            "max_abs": float(np.max(np.abs(exact_error))),
            "renderer_rmse": float(np.sqrt(np.mean((renderer - reconstructed) ** 2))),
            "renderer_source_rmse": float(np.sqrt(np.mean((renderer - fitted["source_mtf"]) ** 2))),
        }
        exact_passed = max(exact_residuals.values()) <= 1e-12
        channels = {
            site + "_rmse": float(
                np.sqrt(np.mean((transfer_mtf(combined[i]) - reconstructed) ** 2))
            )
            for i, site in enumerate(SITES)
        }
        warnings = []
        fit_passed = all(fitted["residuals"][key] <= limit for key, limit in FIT_LIMITS.items())
        if not fit_passed:
            warnings.append(
                "Gaussian compression rejected by diagnostic limits; original sampled PSF is "
                "retained instead."
            )
        if max(channels.values()) > 0.05:
            warnings.append(
                "Shared green-conditioned PSF does not reproduce all CFA-channel spatial responses."
            )
        if fid in mapping and not exact_passed:
            rejected.append(
                fid + ": sampled kernel roundtrip or runtime transfer differs from the source"
            )
        if fid in mapping:
            nodes.append({"field_xy": mapping[fid], **node})
        gaussian_curves = [
            {
                "orientation_deg": float(a),
                "frequency_cy_px": FREQUENCY.tolist(),
                "source_mtf": fitted["source_mtf"][i].tolist(),
                "reconstructed_mtf": fitted["reconstructed_mtf"][i].tolist(),
                "renderer_mtf": fitted["renderer_mtf"][i].tolist(),
            }
            for i, a in enumerate(ANGLES)
        ]
        fields.append(
            {
                "source_field_id": fid,
                "source_field_xy": [field.get("x"), field.get("y")],
                "source_image_xy_mm": field.get("image_xy_mm"),
                "source_normalized_radius": field.get("normalized_radius"),
                "source_field_domain": (
                    "reference_lens_object_direction_not_physical_sensor_coordinates"
                ),
                "target_field_xy": mapping.get(fid),
                "target_field_coordinate_domain": field_domain if fid in mapping else None,
                "applied": fid in mapping,
                "parameters": fitted["parameters"],
                "centroid_px": fitted["centroid_px"],
                "curves": [
                    {
                        "orientation_deg": float(a),
                        "frequency_cy_px": FREQUENCY.tolist(),
                        "source_mtf": fitted["source_mtf"][i].tolist(),
                        "reconstructed_mtf": reconstructed[i].tolist(),
                        "renderer_mtf": renderer[i].tolist(),
                    }
                    for i, a in enumerate(ANGLES)
                ],
                "residuals": exact_residuals,
                "channel_mismatch": channels,
                "warnings": warnings,
                "kernel_metadata": {
                    "shape": list(combined[1].shape),
                    "sum": float(combined[1].sum()),
                    "kernel_pitch_px": 1.0,
                    "sampling": "native_cell_center_pixel_integrated",
                    "phase": "retained_sampled_psf",
                },
                "gaussian_approximation": {
                    "parameters": fitted["parameters"],
                    "accepted": fit_passed,
                    "residuals": fitted["residuals"],
                    "curves": gaussian_curves,
                    "used_for_profile": False,
                    "acceptance_limits": FIT_LIMITS,
                },
                "spectral_weights_normalized_per_site": (weights / weights.sum(axis=0)).tolist(),
                "dc_parity": {
                    "predicted_e_per_s": predicted_dc.tolist(),
                    "preserved_e_per_s": measured_dc.tolist(),
                    "max_relative_error": dc_error,
                },
                "optimizer": fitted["optimizer"],
                "fit_accepted": exact_passed,
            }
        )
        kernels[fid + "_rgba_sites"] = combined
    if not nodes:
        raise ValueError("No source optical field maps to the requested profile")
    source = payload["candidate"]["source_identities"].get("lens", {}).get("source", {})
    applied = source.get("pupil_condition", {}).get(
        "effective_f_number", source.get("paraxial", {}).get("effective_f_number")
    )
    if not isinstance(applied, (int, float)) or not np.isfinite(applied) or applied <= 0:
        raise ValueError("Source does not establish an applied aperture condition")
    conditions = {"light_id": "D65", "aperture_f_number": float(applied)}
    blur = {
        "application": "effective_system_replacement",
        "family": "field_effective_psf_v1",
        "field_nodes": nodes,
        "included_effects": ["lens_blur", "pixel_aperture"],
        "charge_diffusion_assumption": "zero_not_identified",
        "spectral_scope": "D65_450_650nm_Gr_shared_RGB_proxy",
        "field_application": "explicit_nodes_only",
        "phase": "retained_sampled_psf",
        "unique_physical_psf": False,
        "source_kind": "model_derived",
        "initialization_only": True,
        "field_mapping_kind": mapping_kind,
        "field_mapping_mode": mapping_mode,
        "field_coordinate_domain": field_domain,
        "source_identity": deepcopy(payload["source_identity"]),
    }
    report = {
        "schema": SCHEMA,
        "source_optical_profile": optical["specification"]["optical_profile"],
        "status": "rejected" if rejected else "qualified",
        "can_apply": not rejected,
        "qualification": "sampled_kernel_exact_transfer",
        "physical_validation": "not_established",
        "scope": (
            "Local green-conditioned broadband source-model sampled PSF initialization; "
            "exact transfer within the preserved native discrete model"
        ),
        "source_identity": deepcopy(payload["source_identity"]),
        "included_effects": blur["included_effects"],
        "charge_diffusion_assumption": blur["charge_diffusion_assumption"],
        "spectral_condition": {
            "illuminant": "D65",
            "wavelength_nm": WAVE.tolist(),
            "channel": "Gr",
            **spectral_evidence,
        },
        "field_mapping_kind": mapping_kind,
        "field_mapping_mode": mapping_mode,
        "field_coordinate_domain": field_domain,
        "applicable_fields": [
            {"source_field_id": fid, "field_xy": xy, "field_coordinate_domain": field_domain}
            for fid, xy in mapping.items()
        ],
        "field_mapping_scope": (
            "Source lens object-direction coordinates only; not physical sensor positions"
            if field_domain == "reference_lens_object_direction"
            else "Declared sensor-normalized positions; physical correspondence not validated"
        ),
        "fields": fields,
        "limitations": LIMITATIONS + (["Geometric-retrace source: diffraction is absent; no diffraction or subpixel physics is invented by this replay."] if optical["specification"]["optical_profile"] == "geometric_retrace" else []),
        "rejection_reasons": rejected,
        "frequency_unit": "cycles/native_pixel",
        "orientation_convention": (
            "frequency direction; x=columns,y=rows,clockwise in image coordinates"
        ),
        "fit_frequency_band_cy_px": [0.0, 0.5],
        "fit_acceptance_limits": {"roundtrip_and_renderer_max_error": 1e-12},
        "gates": [
            {"id": "source_manifest_and_ancestor_binding", "status": "passed"},
            {"id": "spectral_qe_dc_parity", "status": "passed", "limit": 1e-7},
            {
                "id": "mapped_field_sampled_kernel_transfer",
                "status": "failed" if rejected else "passed",
                "reason": "; ".join(rejected)
                if rejected
                else "All mapped nodes meet declared numerical approximation limits",
                "limits": {"max_transfer_error": 1e-12},
                "frequency_band_cy_px": [0.0, 0.5],
            },
        ],
        "profile_geometry_preserved": {
            "native_shape": profile["native_shape"],
            "pixel_pitch_um": profile["pixel_pitch_um"],
            "cfa": profile["cfa"],
            "focus_distance_m": profile["conditions"].get("focus_distance_m"),
        },
        "reverse_optical_replay_supported": False,
    }
    stream = io.BytesIO()
    np.savez_compressed(
        stream, **kernels, wavelength_nm=WAVE, frequency_cy_px=FREQUENCY, orientation_deg=ANGLES
    )
    derived["broadband-kernels.npz"] = stream.getvalue()
    return {
        "profile_patch": {"blur": blur, "conditions": conditions},
        "conditions_mapping": [
            {
                "parameter": "conditions." + key,
                "before": profile["conditions"].get(key),
                "after": value,
                "classification": "source_model_condition",
                "unit": "f-number" if key == "aperture_f_number" else "illuminant_id",
            }
            for key, value in conditions.items()
        ],
        "report": report,
        "can_apply": not rejected,
        "status": report["status"],
        "derived_files": derived,
    }


def convert_source(
    root,
    summary,
    candidate_id,
    profile,
    condition_id=None,
    expected_manifest_sha256=None,
    *,
    field_mapping=None,
    field_mapping_mode=None,
):
    payload = prepare_optical_source(
        root, summary, candidate_id, condition_id, expected_manifest_sha256
    )
    converted = project_optical_profile(
        payload, profile, field_mapping=field_mapping, field_mapping_mode=field_mapping_mode
    )
    return {
        **converted,
        "source_identity": payload["source_identity"],
        "frozen_files": payload["frozen_files"],
        "manifest_bindings": payload["manifest_bindings"],
        "source_files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in payload["frozen_files"].items()
        },
    }
