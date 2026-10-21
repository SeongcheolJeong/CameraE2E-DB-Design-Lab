"""Calibrated charge preview with fixed color settings from a v1 ISP profile.

The executed graph is declared here; the profile supplies WB, CCM and tone only.
It is not a replay of the profile's linear-DN black-level graph or an advanced
ISP. Physical signed intermediates and invalid masks survive until preview.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from . import charge_voltage_response, electrical, isp, nonlinear_readout

IMPLEMENTATION = "camera_platform_calibrated_charge_preview_v1"
PREVIEW = {
    "method": "calibrated_charge_fixed_color_v1",
    "normalization": "full_well_e",
    "isp_profile_role": "fixed_color_settings_only",
}
GRAPH = [
    "nonlinear_raw_interval_decode", "subtract_calibrated_dark_e",
    "normalize_full_well_e", "bilinear_native_cfa_conservative_validity",
    "fixed_white_balance", "fixed_ccm", "tone_preview",
]


def _input(raw, calibration, geometry):
    if (type(raw) is not np.ndarray or raw.dtype.kind not in "iu"
            or raw.dtype.itemsize > 4 or raw.ndim != 2):
        raise ValueError("Nonlinear preview RAW requires one integer HxW array, at most 32 bits")
    if (any(side < 2 or side > isp.MAX_SIDE or side % 2 for side in raw.shape)
            or raw.size > electrical.MAX_PIXELS):
        raise ValueError(
            "Nonlinear preview requires even sides in [2,4096] and at most 1000000 pixels"
        )
    if (type(geometry) is not dict
            or set(geometry) != {"kind", "full_sensor_shape", "crop_origin"}
            or geometry["kind"] != "native_sensor"):
        raise ValueError("Nonlinear preview requires exact native_sensor geometry")
    cal = nonlinear_readout.validate_calibration(calibration, raw.shape)
    full, origin = electrical._geometry({key: geometry[key] for key in
                                         ("full_sensor_shape", "crop_origin")}, raw.shape)
    native = {"full_sensor_shape": full, "crop_origin": origin}
    if native != cal["acquisition"]["geometry"]:
        raise ValueError("Preview geometry must match the fixed calibration acquisition geometry")
    pattern = np.roll(np.array([[1, 2], [2, 3]]), (-origin[0] % 2, -origin[1] % 2), axis=(0, 1))
    return cal, {"kind": "native_sensor", **native}, pattern


def _conservative_validity(raw_valid):
    padded = np.pad(raw_valid, 1, mode="reflect")
    rows, cols = raw_valid.shape
    valid = np.ones(raw_valid.shape, dtype=bool)
    for dy in range(3):
        for dx in range(3):
            valid &= padded[dy:dy + rows, dx:dx + cols]
    return valid


def _implementation():
    import pyisetcam.ip
    import pyisetcam.utils

    return {
        "id": IMPLEMENTATION,
        "precision": "IEEE754_float64",
        "numpy_version": np.__version__,
        "demosaic": "pyisetcam.ip._ie_bilinear",
        "srgb_transfer": "pyisetcam.utils.linear_to_srgb",
        "source_sha256": {
            name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for name, path in {
                "camera_platform/nonlinear_isp.py": __file__,
                "camera_platform/nonlinear_readout.py": nonlinear_readout.__file__,
                "camera_platform/charge_voltage_response.py": charge_voltage_response.__file__,
                "camera_platform/isp.py": isp.__file__,
                "pyisetcam/ip.py": pyisetcam.ip.__file__,
                "pyisetcam/utils.py": pyisetcam.utils.__file__,
            }.items()
        },
    }


def process_raw(raw, calibration, profile, *, geometry):
    """Preview one immutable RAW frame using its fixed nonlinear calibration.

    The legacy-compatible black_subtracted_normalized_frames key contains
    (decoded total_charge_e - dark_e) / full_well_e, not a DN black subtraction.
    raw_valid_mask_frames describes that mosaic. linear_valid_mask_frames is
    stricter: all reflected 3x3 RAW neighbors must be valid for every RGB channel.
    Invalid linear RGB remains NaN; only final display RGB becomes black. Every
    array is frame-first except rgb, which is a detached copy of frame zero.
    """
    from pyisetcam.ip import _ie_bilinear
    from pyisetcam.utils import linear_to_srgb

    if type(profile) is not dict or profile.get("schema") != isp.SCHEMA:
        raise ValueError(
            "Nonlinear preview supports only v1 fixed color settings; ISP v2 is unsupported"
        )
    selected = isp.validate_profile(profile)
    cal, geom, pattern = _input(raw, calibration, geometry)
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    decoded = nonlinear_readout.decode_raw(raw, cal)
    raw_valid = decoded["arrays"]["valid_mask"].copy()
    normalized = ((decoded["arrays"]["total_charge_e"] - cal["dark_e"])
                  / cal["model"]["full_well_e"])
    normalized[~raw_valid] = np.nan
    valid = _conservative_validity(raw_valid)
    rows, cols = raw.shape
    sites = np.tile(pattern, (rows // 2, cols // 2))
    planes = np.zeros((rows, cols, 3), dtype=np.float64)
    for channel in range(3):
        samples = (sites == channel + 1) & raw_valid
        planes[..., channel][samples] = normalized[samples]
    demosaiced = _ie_bilinear(planes, pattern)
    if demosaiced.shape != (rows, cols, 3):
        raise ValueError("Nonlinear preview demosaic produced an unexpected shape")
    demosaiced[~valid] = np.nan
    balanced = demosaiced * np.asarray(selected["white_balance"]["gains_rgb"])
    corrected = balanced @ np.asarray(selected["ccm"]["matrix_rgb"]).T
    for value in (demosaiced, balanced, corrected):
        if not np.isfinite(value[valid]).all():
            raise ValueError("Nonlinear preview produced a nonfinite valid linear intermediate")
    clipped = np.clip(np.where(valid[..., None], corrected, 0.), 0., 1.)
    output = linear_to_srgb(clipped) if selected["tone"]["curve"] == "srgb" else clipped
    if not np.isfinite(output).all():
        raise ValueError("Nonlinear preview produced a nonfinite display output")
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        raise RuntimeError("RAW input changed during nonlinear preview")
    arrays = {
        "black_subtracted_normalized_frames": normalized[None, ...],
        "demosaiced_rgb_frames": demosaiced[None, ...],
        "white_balanced_rgb_frames": balanced[None, ...],
        "ccm_rgb_frames": corrected[None, ...],
        "rgb_frames": output[None, ...],
        "rgb": output.copy(),
        "raw_valid_mask_frames": raw_valid[None, ...],
        "linear_valid_mask_frames": valid[None, ...],
    }
    return {"arrays": arrays, "summary": {
        "schema_version": "camera_nonlinear_preview_result_v1",
        "implementation": _implementation(), "preview": dict(PREVIEW),
        "executed_graph": list(GRAPH),
        "color_settings_profile": selected,
        "color_settings_profile_sha256": isp._digest(selected),
        "color_settings_profile_fields_used": ["white_balance", "ccm", "tone"],
        "geometry": geom, "local_cfa_rgb_indices": (pattern - 1).tolist(),
        "calibration_schema": cal["schema"], "model_sha256": cal["model_sha256"],
        "acquisition_sha256": cal["acquisition_sha256"], "raw_decode": decoded["summary"],
        "input_shape": list(raw.shape), "input_dtype": raw.dtype.str,
        "input_array_bytes_sha256": before,
        "input_hash_scope": "C-order array data bytes; original container bytes retained by caller",
        "frames": 1, "output_size": [rows, cols],
        "array_units": {key: "boolean" if value.dtype.kind == "b" else "1"
                        for key, value in arrays.items()},
        "normalization": "(decoded total_charge_e - calibrated dark_e) / full_well_e",
        "black_subtracted_normalized_frames_meaning": "calibrated signed photocharge/full_well_e",
        "frame_max_normalization": False,
        "linear_negative_policy": "preserved through calibrated mosaic, demosaic, fixed WB and CCM",
        "invalid_policy": "NaN physical intermediates; black display preview",
        "rgb_validity": "all reflected 3x3 RAW neighbors valid; conservative for all channels",
        "tone_input_clipping": [0., 1.],
        "frame_diagnostics": [{
            "frame_index": 0, "raw_valid_pixels": int(raw_valid.sum()),
            "linear_valid_pixels": int(valid.sum()), "preview_invalid_pixels": int((~valid).sum()),
            "negative_calibrated_mosaic_samples": int(np.count_nonzero(normalized < 0)),
            "tone_low_clipped_channel_samples": int(np.count_nonzero(corrected < 0)),
            "tone_high_clipped_channel_samples": int(np.count_nonzero(corrected > 1)),
        }],
        "rgb_meaning": "fixed matrix-transformed sensor RGB; colorimetric accuracy not established",
        "physical_validation": "not_established", "uncertainty": {"kind": "unknown"},
        "limitations": [
            "The v1 profile supplies fixed WB/CCM/tone only; its full DN graph is not executed.",
            "No ISP v2, denoising, lens shading, sharpening, automatic controls or HDR merge.",
            "Bilinear reflects crop boundaries; full-frame boundary equivalence is not claimed.",
            "Invalid RGB support is conservatively expanded to the reflected 3x3 neighborhood.",
            "Signed estimated photocharge is preserved, without claiming negative electrons.",
            "sRGB is only a transfer curve; physical color accuracy and uncertainty are unknown.",
        ],
    }}
