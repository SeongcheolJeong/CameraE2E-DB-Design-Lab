"""Source-bound adaptive demosaic diagnostic on one saved Lens x Sensor RAW.

The existing pyisetcam Adaptive Laplacian kernel operates on the same
black-level-normalized mosaic as the platform's bilinear ISP. Only a saved
native 2x2 RGB RGGB CFA is accepted. No lens shading, noise or color
calibration is inferred from the target image.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import minimum_filter

from pyisetcam import ip as legacy_ip
from pyisetcam.dataset import _sha256_array
from pyisetcam.utils import linear_to_srgb

from . import isp_v2
from .retained_raw_isp_bridge import inspect_lens_sensor_isp_input

SCHEMA = "camerae2e_lens_sensor_adaptive_demosaic_v1"
METHOD = "pyisetcam.ip._adaptive_laplacian_demosaic"
_CFA = np.array([[1, 2], [2, 3]], dtype=np.int8)
_STENCIL_RADIUS = 4


def _file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evaluate_lens_sensor_adaptive_demosaic(
    source_report_path: str | Path,
    source_raw_path: str | Path,
    *,
    source_lens_sensor_job_id: str,
    sensor_db_root: str | Path,
) -> dict[str, Any]:
    """Compare two demosaicers on one retained RAW, without acquiring pixels.

    A four-pixel border and any neighborhood touching an invalid ADC code are
    excluded from comparison. This is a conservative software stencil mask,
    not a statement that the legacy kernel itself refuses those pixels.
    """
    report_path = Path(source_report_path).expanduser().resolve()
    raw_path = Path(source_raw_path).expanduser().resolve()
    checked = inspect_lens_sensor_isp_input(
        report_path,
        raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
        sensor_db_root=sensor_db_root,
        profile=isp_v2.default_profile(),
    )
    if checked["native_cfa_contract"] != "three_channel_rggb_2x2":
        raise ValueError("Adaptive Laplacian requires saved native three-channel 2x2 RGGB.")
    raw = checked["raw"]
    rows, cols = raw.shape
    if rows <= 2 * _STENCIL_RADIUS or cols <= 2 * _STENCIL_RADIUS:
        raise ValueError("Adaptive comparison needs a RAW window larger than its 4-pixel border.")
    source_report_hash = _file_sha256(report_path)
    source_raw_hash = _file_sha256(raw_path)
    kernel_source_hash = _file_sha256(legacy_ip.__file__)
    original_hash = _sha256_array(raw)
    raw.setflags(write=False)

    reference = isp_v2.process_raw(
        raw, checked["raw_format"], isp_v2.default_profile(), geometry=checked["geometry"]
    )
    reference_arrays = reference["arrays"]
    normalized = np.asarray(reference_arrays["black_subtracted_normalized_frames"][0])
    tile = np.tile(_CFA, (rows // 2, cols // 2))
    planes = np.zeros((rows, cols, 3), dtype=np.float64)
    for channel in range(3):
        site = tile == channel + 1
        planes[..., channel][site] = normalized[site]
    adaptive = np.asarray(
        legacy_ip._adaptive_laplacian_demosaic(planes, "rggb"), dtype=np.float64
    )
    if adaptive.shape != (rows, cols, 3) or not np.isfinite(adaptive).all():
        raise ValueError("Adaptive Laplacian returned invalid sensor-space RGB.")
    baseline = np.asarray(reference_arrays["demosaiced_rgb_frames"][0])
    if baseline.shape != adaptive.shape or not np.isfinite(baseline).all():
        raise ValueError("Bilinear reference returned invalid sensor-space RGB.")
    raw_valid = np.asarray(reference_arrays["raw_code_valid_mask_frames"][0], dtype=bool)
    valid = minimum_filter(
        raw_valid, size=2 * _STENCIL_RADIUS + 1, mode="constant", cval=False
    ).astype(bool)
    if not np.any(valid):
        raise ValueError("No interior RGB pixels have a valid RAW-code neighborhood.")
    difference = np.abs(adaptive - baseline)[valid]
    reference_display = np.asarray(reference_arrays["rgb"])
    adaptive_display = linear_to_srgb(np.clip(adaptive, 0.0, 1.0))

    fresh = inspect_lens_sensor_isp_input(
        report_path,
        raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
        sensor_db_root=sensor_db_root,
        profile=isp_v2.default_profile(),
    )
    if (
        _file_sha256(report_path) != source_report_hash
        or _file_sha256(raw_path) != source_raw_hash
        or _file_sha256(legacy_ip.__file__) != kernel_source_hash
        or _sha256_array(raw) != original_hash
        or fresh["source_binding"] != checked["source_binding"]
        or fresh["native_cfa"] != checked["native_cfa"]
        or fresh["readout"] != checked["readout"]
    ):
        raise ValueError("Saved Lens RAW, CFA, readout or source report changed during ISP.")

    arrays = {
        "source_raw_dn_frame": raw.copy(),
        "reference_bilinear_sensor_rgb": baseline.copy(),
        "adaptive_laplacian_sensor_rgb": adaptive.copy(),
        "reference_display_rgb": reference_display.copy(),
        "adaptive_display_rgb": adaptive_display.copy(),
        "comparison_valid_mask": valid.copy(),
    }
    source_report = json.loads(report_path.read_bytes())
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "source_binding": deepcopy(checked["source_binding"]),
        "native_cfa_contract": checked["native_cfa_contract"],
        "native_cfa": deepcopy(checked["native_cfa"]),
        "readout": deepcopy(checked["readout"]),
        "raw_format": deepcopy(checked["raw_format"]),
        "geometry": deepcopy(checked["geometry"]),
        "raw_content_sha256": original_hash,
        "raw_values_byte_equal": True,
        "method": {
            "id": METHOD,
            "pyisetcam_ip_source_sha256": kernel_source_hash,
            "input": "black_level_normalized_sparse_native_rggb_planes",
            "reference": "camera_platform.isp_v2.default_profile.bilinear_demosaic",
            "comparison_stencil_radius_pixels": _STENCIL_RADIUS,
            "boundary_comparison_policy": "exclude_border_and_invalid_raw_neighborhood",
        },
        "calibration_provenance": {
            "black_level_dn": checked["readout"]["black_level_dn"],
            "black_level_source": "saved_sensor_model_snapshot_readout",
            "readout_classification": source_report["preview"]["sensor_model"][
                "readout_classification"
            ],
            "white_balance": "fixed_identity_no_estimated_illuminant",
            "ccm": "fixed_identity_no_colorimetric_calibration",
            "spatial_lsc": "bypass_no_independent_calibration",
            "raw_denoise": "bypass_no_independent_calibration",
        },
        "before_after": {
            "domain": "black_level_normalized_sensor_rgb_valid_interior_only",
            "valid_pixels": int(np.count_nonzero(valid)),
            "mean_absolute_sensor_rgb_difference": float(np.mean(difference)),
            "changed_sample_fraction": float(np.mean(difference > 1e-12)),
            "quality_improvement_claimed": False,
        },
        "reference_isp": reference["summary"],
        "arrays": {
            key: {"shape": list(value.shape), "dtype": str(value.dtype)}
            for key, value in arrays.items()
        },
        "limitations": [
            "Adaptive Laplacian is a software demosaic diagnostic, not a calibrated ISP.",
            "The private pyisetcam kernel is pinned by source hash and may change by version.",
            "Lens radiometry and readout may be assumptions; no color calibration is inferred.",
            "Differences from bilinear are not measured quality or product ranking.",
        ],
    }
    json.dumps(summary, allow_nan=False)
    return {"summary": summary, "arrays": arrays}


def run_lens_sensor_adaptive_demosaic(
    source_report_path: str | Path,
    source_raw_path: str | Path,
    *,
    source_lens_sensor_job_id: str,
    sensor_db_root: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Persist source RAW, two RGB stages and the exact method/source record."""
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Adaptive demosaic output_dir is not empty: {destination}")
    evaluated = evaluate_lens_sensor_adaptive_demosaic(
        source_report_path,
        source_raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
        sensor_db_root=sensor_db_root,
    )
    destination.mkdir(parents=True, exist_ok=True)
    npz_path = destination / "lens-sensor-adaptive-demosaic.npz"
    np.savez_compressed(npz_path, **evaluated["arrays"])
    with np.load(npz_path, allow_pickle=False) as stored:
        if _sha256_array(stored["source_raw_dn_frame"]) != evaluated["summary"][
            "raw_content_sha256"
        ]:
            raise ValueError("Adaptive ISP artifact lost the exact source RAW.")
    report = deepcopy(evaluated["summary"])
    report["artifacts"] = {"npz_name": npz_path.name, "npz_sha256": _file_sha256(npz_path)}
    json_path = destination / "lens-sensor-adaptive-demosaic.json"
    json_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "artifacts": {"json": str(json_path), "npz": str(npz_path)},
        "artifact_sha256": {"json": _file_sha256(json_path), "npz": _file_sha256(npz_path)},
    }
