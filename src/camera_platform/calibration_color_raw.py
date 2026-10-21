"""Preserve native Bayer originals and measure declared Macbeth patch ROIs."""

from __future__ import annotations

import base64
import hashlib

import numpy as np

from .calibration_lab_raw import _load


def observe_raw(descriptor):
    """Return content, RGB DN means and evidence; never use an ISP to read RAW."""
    required = {"base64", "cfa", "bit_depth", "crop_origin", "rois"}
    if not isinstance(descriptor, dict) or set(descriptor) != required:
        raise ValueError("RAW original requires base64, cfa, bit_depth, crop_origin and rois")
    if descriptor["cfa"] != "RGGB" or type(descriptor["bit_depth"]) is not int:
        raise ValueError("This patch observer requires integer RGGB RAW")
    bits = descriptor["bit_depth"]
    if not 8 <= bits <= 16:
        raise ValueError("RAW bit depth must be 8..16")
    encoded = descriptor["base64"]
    if not isinstance(encoded, str) or len(encoded) > 12 * 1024**2:
        raise ValueError("Each encoded RAW original must be at most 12 MiB")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid RAW base64") from exc
    raw = _load(content)
    if raw.shape[0] != 1 or raw.min() < 0 or raw.max() > 2**bits - 1:
        raise ValueError("Patch RAW needs one exposure level and valid ADC codes")
    origin = descriptor["crop_origin"]
    if (
        not isinstance(origin, list)
        or len(origin) != 2
        or any(type(x) is not int or not 0 <= x <= 65536 for x in origin)
    ):
        raise ValueError("RAW requires native crop_origin [row,column]")
    rois = descriptor["rois"]
    if not isinstance(rois, list) or not 4 <= len(rois) <= 256:
        raise ValueError("RAW needs 4..256 explicitly declared patch ROIs")
    rgb, clipped = [], []
    h, w = raw.shape[-2:]
    occupied = np.zeros((h, w), dtype=bool)
    for roi in rois:
        if not isinstance(roi, list) or len(roi) != 4 or any(type(x) is not int for x in roi):
            raise ValueError("Each ROI is [top,left,height,width] in native pixels")
        y, x, rh, rw = roi
        if y < 0 or x < 0 or min(rh, rw) < 4 or y + rh > h or x + rw > w:
            raise ValueError("ROI is outside the native RAW raster")
        if occupied[y : y + rh, x : x + rw].any():
            raise ValueError("Patch ROIs must not overlap")
        occupied[y : y + rh, x : x + rw] = True
        rr, cc = np.indices((rh, rw))
        rr, cc = (rr + y + origin[0]) % 2, (cc + x + origin[1]) % 2
        masks = [(rr == 0) & (cc == 0), rr != cc, (rr == 1) & (cc == 1)]
        patch = raw[0, :, y : y + rh, x : x + rw]
        values = [patch[:, mask] for mask in masks]
        rgb.append([float(a.mean()) for a in values])
        clipped.append([float(np.mean((a == 0) | (a == 2**bits - 1))) for a in values])
    evidence = {
        "method": "native_rggb_patch_mean_v1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "shape": list(raw.shape),
        "frame_count": raw.shape[1],
        "patch_count": len(rois),
        "frame_sha256": [hashlib.sha256(a.tobytes()).hexdigest() for a in raw[0]],
        "cfa": "RGGB",
        "bit_depth": bits,
        "crop_origin": origin,
        "rois": rois,
        "rgb_dn": rgb,
        "clipped_fraction": clipped,
        "green_definition": "mean of both native green sites; no demosaic, WB or CCM",
    }
    return content, evidence
