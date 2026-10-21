"""Run CameraE2E's spectral ISP on an exact retained Lens x Sensor RAW.

This is a model-derived D65 display diagnostic.  The saved sensor EQE drives
CameraE2E's Macbeth sensor-to-XYZ fit; it is not a measured CCM, AWB estimate,
or validation of the saved geometric lens PSF/radiometry.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam import ip as legacy_ip
from pyisetcam import sensor_capture as legacy_capture
from pyisetcam import sensor_estimate as legacy_estimate
from pyisetcam.dataset import _sha256_array
from pyisetcam.sensor import sensor_create
from pyisetcam.sensor_capture import configure_e2e_sensor
from pyisetcam.sensor_estimate import apply_sensor_estimate
from pyisetcam.sensor_simulation_db import sensor_simulation_db_get

from . import isp_v2
from .offline_assets import offline_asset_identity, offline_asset_store
from .registry import digest
from .retained_raw_isp_bridge import inspect_lens_sensor_isp_input

SCHEMA = "camerae2e_lens_sensor_legacy_isp_v1"
METHODS = {
    "bilinear": "bilinear",
    "laplacian": "laplacian",
    "adaptive_laplacian": "adaptive laplacian",
}
_D65_SCENES = {"uniform_d65", "slanted_edge_d65"}


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _model_source(
    checked: dict[str, Any], *, sensor_db_root: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    revision = checked["source_binding"]["saved_sensor_revision"]
    saved = sensor_simulation_db_get(
        revision["report_code"], root=sensor_db_root, run_id=revision["run_id"]
    )
    row, metadata = saved["db_entry"], saved["metadata"]
    for key in ("sensor_id", "report_code", "run_id", "result_sha256", "config_sha256"):
        if row.get(key) != revision.get(key):
            raise ValueError("Saved sensor revision changed since Lens RAW capture.")
    model = metadata.get("model_snapshot")
    if (
        not isinstance(model, dict)
        or model.get("kind") != "db_estimate_v1"
        or model.get("cfa_pattern") != "rggb_bayer"
        or model.get("filter_names") != ["red", "green", "blue"]
        or model.get("readout") != checked["readout"]
        or metadata.get("native_cfa") != checked["native_cfa"]
    ):
        raise ValueError("Saved sensor spectral/CFA/readout snapshot does not match RAW.")
    wave = np.asarray(model.get("wavelength_nm"), dtype=float)
    eqe = np.asarray(model.get("eqe"), dtype=float)
    if (
        wave.ndim != 1 or wave.size < 3 or not np.isfinite(wave).all()
        or not np.all(np.diff(wave) > 0)
        or eqe.shape != (wave.size, 3) or not np.isfinite(eqe).all()
        or np.any((eqe < 0) | (eqe > 1))
    ):
        raise ValueError("Saved sensor spectral response is invalid for CameraE2E ISP.")
    return model, row


def preflight_lens_sensor_legacy_isp(
    source_report_path: str | Path,
    source_raw_path: str | Path,
    *,
    source_lens_sensor_job_id: str,
    sensor_db_root: str | Path,
    workspace: str | Path,
    demosaic_method: str,
) -> dict[str, Any]:
    """Check exact source, model, scene, implementation and offline assets."""
    if demosaic_method not in METHODS:
        raise ValueError("Demosaic must be bilinear, laplacian or adaptive_laplacian.")
    checked = inspect_lens_sensor_isp_input(
        source_report_path, source_raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
        sensor_db_root=sensor_db_root,
        profile=isp_v2.default_profile(),
    )
    if checked["native_cfa_contract"] != "three_channel_rggb_2x2":
        raise ValueError("CameraE2E color ISP requires saved three-channel native RGGB.")
    if min(checked["raw"].shape) <= 8:
        raise ValueError("CameraE2E Laplacian ISP requires more than 8 RAW pixels per axis.")
    report = json.loads(Path(source_report_path).read_bytes())
    scene = report.get("preview", {}).get("scene", {})
    declared = report.get("specification", {})
    if (
        scene.get("kind") not in _D65_SCENES
        or scene.get("kind") != f"{declared.get('scene_kind')}_d65"
        or scene.get("kind") != checked["source_binding"]["source_scene"]
        or scene.get("luminance_cd_m2") != declared.get("luminance_cd_m2")
        or scene.get("wavelength_nm") != [450.0, 550.0, 650.0]
        or scene.get("generator") != "pyisetcam.sensor_comparison._make_scene"
    ):
        raise ValueError("CameraE2E D65 color ISP requires a saved, explicit D65 scene.")
    model, row = _model_source(checked, sensor_db_root=sensor_db_root)
    assets = offline_asset_identity(workspace=workspace, source=sensor_db_root)
    return {
        "checked": checked,
        "scene": deepcopy(scene),
        "model": model,
        "sensor_db_entry": row,
        "offline_assets": assets,
        "method_binding": {
            "demosaic_method": demosaic_method,
            "pyisetcam_ip_source_sha256": _sha256(legacy_ip.__file__),
            "pyisetcam_sensor_estimate_source_sha256": _sha256(legacy_estimate.__file__),
            "pyisetcam_sensor_capture_source_sha256": _sha256(legacy_capture.__file__),
        },
    }


def evaluate_lens_sensor_legacy_isp(
    source_report_path: str | Path,
    source_raw_path: str | Path,
    *,
    source_lens_sensor_job_id: str,
    sensor_db_root: str | Path,
    workspace: str | Path,
    demosaic_method: str,
) -> dict[str, Any]:
    """Apply public pyisetcam ``ip_compute`` without rerunning acquisition."""
    source_report_path = Path(source_report_path).expanduser().resolve()
    source_raw_path = Path(source_raw_path).expanduser().resolve()
    source_file_hashes = {
        "report": _sha256(source_report_path), "raw": _sha256(source_raw_path),
    }
    ready = preflight_lens_sensor_legacy_isp(
        source_report_path, source_raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
        sensor_db_root=sensor_db_root, workspace=workspace,
        demosaic_method=demosaic_method,
    )
    checked, model = ready["checked"], ready["model"]
    raw = np.asarray(checked["raw"], dtype=np.int32)
    raw_hash = _sha256_array(raw)
    raw.setflags(write=False)
    assets = offline_asset_store(workspace=workspace, source=sensor_db_root)
    # ip_create initializes a Display through a nested legacy default lookup.
    # Bind the pinned store for the whole calculation, including that lookup.
    with assets.bound_default(assets):
        sensor = sensor_create("bayer-rggb", pixel={"size": tuple(raw.shape)}, asset_store=assets)
        sensor = apply_sensor_estimate(sensor, model)
        sensor = configure_e2e_sensor(sensor, readout=model["readout"])
        sensor.data["raw_dn"] = raw.copy()
        processor = legacy_ip.ip_create(sensor=sensor, asset_store=assets)
        processor = legacy_ip.ip_set(
            processor, "demosaic method", METHODS[demosaic_method]
        )
        if (
            processor.fields.get("conversion_method_sensor") != "mcc optimized"
            or processor.fields.get("illuminant_correction_method") != "none"
            or processor.fields.get("internal_cs") != "xyz"
        ):
            raise ValueError("CameraE2E ISP default color pipeline changed; review the contract.")
        output = legacy_ip.ip_compute(processor, sensor, asset_store=assets)
    if output.metadata.get("sensor_input", {}).get("demosaic", {}).get("applied") != METHODS[
        demosaic_method
    ]:
        raise ValueError("CameraE2E ISP silently changed the requested demosaic method.")
    normalized = np.asarray(output.data["input"], dtype=np.float64)
    expected = (
        raw.astype(np.float64) - checked["readout"]["black_level_dn"]
    ) / (2 ** checked["readout"]["bit_depth"] - 1 - checked["readout"]["black_level_dn"])
    if not np.array_equal(normalized, expected):
        raise ValueError("CameraE2E ISP input was not normalized once from saved RAW.")
    arrays = {"source_raw_dn_frame": raw.copy(), "normalized_sensor_dn": normalized.copy()}
    for source, target in (
        ("sensorspace", "sensor_rgb"),
        ("xyz", "model_derived_xyz"),
        ("display_rgb", "display_rgb"),
        ("srgb", "srgb"),
    ):
        value = np.asarray(output.data[source], dtype=np.float64)
        if value.shape != (*raw.shape, 3) or not np.isfinite(value).all():
            raise ValueError(f"CameraE2E ISP returned invalid {source} array.")
        arrays[target] = value.copy()
    current = preflight_lens_sensor_legacy_isp(
        source_report_path, source_raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
        sensor_db_root=sensor_db_root, workspace=workspace,
        demosaic_method=demosaic_method,
    )
    if (
        _sha256(source_report_path) != source_file_hashes["report"]
        or _sha256(source_raw_path) != source_file_hashes["raw"]
        or _sha256_array(raw) != raw_hash
        or current["checked"]["source_binding"] != checked["source_binding"]
        or current["checked"]["raw_content_sha256"] != raw_hash
        or current["model"] != model
        or current["offline_assets"] != ready["offline_assets"]
        or current["method_binding"] != ready["method_binding"]
    ):
        raise ValueError(
            "Lens RAW, sensor snapshot, CameraE2E ISP or assets changed during replay."
        )
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "source_binding": deepcopy(checked["source_binding"]),
        "native_cfa_contract": checked["native_cfa_contract"],
        "native_cfa": deepcopy(checked["native_cfa"]),
        "readout": deepcopy(checked["readout"]),
        "raw_format": deepcopy(checked["raw_format"]),
        "geometry": deepcopy(checked["geometry"]),
        "scene": ready["scene"],
        "sensor_db_revision": deepcopy(ready["sensor_db_entry"]),
        "sensor_model_snapshot_sha256": digest(model),
        "offline_assets": ready["offline_assets"],
        "method_binding": ready["method_binding"],
        "raw_content_sha256": raw_hash,
        "raw_values_byte_equal": True,
        "color_contract": {
            "sensor_spectral_response": "saved_db_estimate_v1_aggregate_eqe",
            "sensor_to_xyz": "pyisetcam_D65_Macbeth_least_squares_model_fit",
            "illuminant_correction": "none_no_AWB",
            "display": "pyisetcam_lcdExample_mat_model",
            "frame_max_normalization": False,
            "negative_xyz_handling": "pyisetcam_clips_to_zero_after_sensor_matrix",
            "display_transfer_input": "clipped_to_0_to_1_before_gamma_and_srgb",
            "reference": output.metadata["sensor_input"]["fixed_reference"],
            "sensor_to_xyz_matrix": np.asarray(
                output.fields["sensor_conversion_matrix"], dtype=float
            ).tolist(),
            "xyz_to_display_matrix": np.asarray(
                output.fields["ics2display"], dtype=float
            ).tolist(),
            "measured_color_accuracy": False,
        },
        "arrays": {key: {"shape": list(value.shape), "dtype": str(value.dtype)}
                   for key, value in arrays.items()},
        "limitations": [
            "Saved geometric PSF omits diffraction; lens transmission and field illumination "
            "were assumed unity; CRA and pixel angle response were not coupled.",
            "Sensor EQE/readout are saved DB estimates or assumptions, not verified product data.",
            "D65 Macbeth/display matrices are model-derived; no measured CCM, AWB or color "
            "accuracy is established.",
            "This processes one saved linear RGGB RAW and does not implement general CFA, "
            "HDR, temporal denoise or hardware ISP timing.",
            "RGB differences and visual appearance cannot rank camera products.",
        ],
    }
    json.dumps(summary, allow_nan=False)
    return {"summary": summary, "arrays": arrays}


def run_lens_sensor_legacy_isp(
    source_report_path: str | Path,
    source_raw_path: str | Path,
    *,
    source_lens_sensor_job_id: str,
    sensor_db_root: str | Path,
    workspace: str | Path,
    demosaic_method: str,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Persist model-derived arrays with exact retained RAW readback."""
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"CameraE2E ISP output_dir is not empty: {destination}")
    evaluated = evaluate_lens_sensor_legacy_isp(
        source_report_path, source_raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
        sensor_db_root=sensor_db_root, workspace=workspace,
        demosaic_method=demosaic_method,
    )
    destination.mkdir(parents=True, exist_ok=True)
    npz_path = destination / "lens-sensor-legacy-isp.npz"
    np.savez_compressed(npz_path, **evaluated["arrays"])
    with np.load(npz_path, allow_pickle=False) as stored:
        if _sha256_array(stored["source_raw_dn_frame"]) != evaluated["summary"][
            "raw_content_sha256"
        ]:
            raise ValueError("CameraE2E ISP artifact lost the exact source RAW.")
    report = deepcopy(evaluated["summary"])
    report["artifacts"] = {"npz_name": npz_path.name, "npz_sha256": _sha256(npz_path)}
    json_path = destination / "lens-sensor-legacy-isp.json"
    json_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "artifacts": {"json": str(json_path), "npz": str(npz_path)},
        "artifact_sha256": {"json": _sha256(json_path), "npz": _sha256(npz_path)},
    }
