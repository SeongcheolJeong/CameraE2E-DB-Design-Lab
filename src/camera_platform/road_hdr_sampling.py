"""Representative source-grid refinement at unchanged FOV, PSF and pixel pitch."""

import json
from pathlib import Path

import numpy as np

from .road_design_compute import compute_mean, to_common_grid
from .road_design_inputs import write_json
from .road_hdr_inputs import bound_specification
from .road_hdr_study import manifest, verify_manifest


def qualify(workspace, package, on_progress=lambda x: None):
    package = Path(package)
    spec, binding = bound_specification(package)
    root = package / "sampling"
    if (root / "manifest.json").exists():
        verify_manifest(root, "manifest.json")
        result = json.loads((root / "result.json").read_text())
        if result["specification_sha256"] != binding["sha256"]:
            raise ValueError("Sampling belongs to another specification.")
        return result
    event = next(e for e in spec["events"] if e["split"] == "selection" and e["risk"])
    anchor = next(
        a
        for a in event["anchors"]
        if any(p["instance_id"] == event["target_instance_id"] for p in a["persons"])
    )
    box = next(
        p["bbox"] for p in anchor["persons"] if p["instance_id"] == event["target_instance_id"]
    )
    frame = next(f for f in event["frames"] if f["token"] == anchor["frame_token"])
    x0, y0, x1, y1 = spec["observation"]["tangent_bounds_xyxy"]
    bx0, by0, bx1, by1 = box
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    hw, hh = max(32, (bx1 - bx0) * 0.75), max(32, (by1 - by0) * 0.75)
    roi = {
        "id": "preselected-object-and-background",
        "common_shape_row_col": [128, 128],
        "tangent_bounds_xyxy": [
            x0 + max(0, cx - hw) / 639 * (x1 - x0),
            y0 + max(0, cy - hh) / 359 * (y1 - y0),
            x0 + min(639, cx + hw) / 639 * (x1 - x0),
            y0 + min(359, cy + hh) / 359 * (y1 - y0),
        ],
    }
    root.mkdir(parents=True, exist_ok=True)
    outputs = {}
    checks = []
    for factor in (1, 2, 4):
        directory = root / f"grid-{factor}"
        on_progress(f"representative source-grid refinement x{factor}")
        mean, report, _, _, coordinates = compute_mean(
            Path(workspace), spec, frame, roi, spec["candidates"][0], directory, refinement=factor
        )
        common = to_common_grid(mean, coordinates, report["native_roi_bounds"])
        gradient = float(np.mean(np.hypot(*np.gradient(common))))
        outputs[factor] = (mean, gradient, report)
        if factor > 1:
            a, ga, ar = outputs[factor // 2]
            b, gb, br = outputs[factor]
            delta = float(np.mean(abs(a - b)) / max(1e-15, np.mean(b)))
            contrast = abs(ga - gb) / max(1e-15, gb)
            check = {
                "coarse": factor // 2,
                "fine": factor,
                "mean_relative_delta": delta,
                "gradient_relative_delta": contrast,
                "qualified": bool(delta <= 0.01 and contrast <= 0.01),
                "coarse_convolution_pitch_um": ar["convolution_pitch_um"],
                "fine_convolution_pitch_um": br["convolution_pitch_um"],
            }
            checks.append(check)
            if check["qualified"]:
                break
    selected = next((c["coarse"] for c in checks if c["qualified"]), None)
    result = {
        "specification_sha256": binding["sha256"],
        "qualified": selected is not None,
        "selected_source_refinement": selected,
        "checks": checks,
        "roi": roi,
        "frame_token": frame["token"],
        "stored_PSF_refined": False,
        "physical_validation": False,
        "scope": (
            "representative source-grid signal/gradient; fixed stored "
            "geometric PSF; not detector or optical bandwidth certification"
        ),
    }
    write_json(root / "result.json", result)
    manifest(root, "manifest.json")
    return result
