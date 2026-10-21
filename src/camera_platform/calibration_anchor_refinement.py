"""Center-refined boundary mesh and a fresh disjoint direct optical validation set."""

from __future__ import annotations

import math
import shutil
from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy.optimize import brentq

from . import calibration_anchor_source as source
from . import calibration_direct_optics as optics
from . import prescription_psf
from . import reference_exit_pupil as pupil
from .registry import digest


def bind(base, g):
    from rayoptics.raytr import trace
    from rayoptics.raytr.opticalspec import Field

    source.validate(base, g)
    request = g["requests"][3]
    model = prescription_psf._model(request, 1)

    def chief(angle):
        f = Field(y=angle, fov=model.optical_spec["fov"])
        pupil.aim_at_stop(model, f, 550.0)
        ray, _, _ = trace.trace_base(
            model,
            [0.0, 0.0],
            f,
            550.0,
            apply_vignetting=False,
            check_apertures=True,
            pt_inside_fuzz=0.0,
        )
        prescription_psf._validate_physical_ray(
            ray, model, request["prescription"]["surfaces"], "refined sensor chief"
        )
        return np.asarray(ray[-1][0][:2], float)

    h, w = g["full_sensor_shape"]
    axis = g["optical_axis_active_row_col"]
    new = []
    for r in range(4):
        for c in range(4):
            y = (h - 1) * (r + (0.25 if r < 2 else 0.75)) / 4
            x = (w - 1) * (c + (0.25 if c < 2 else 0.75)) / 4
            xy = [(x - axis[1]) * 0.0029, -(y - axis[0]) * 0.0029]
            radius = float(np.linalg.norm(xy))
            angle = brentq(lambda a: np.linalg.norm(chief(a)) - radius, 0.0, 12.0, xtol=1e-10)
            point = chief(angle)
            error = float(abs(np.linalg.norm(point) - radius))
            if error > 1e-7:
                raise ValueError("Refinement check chief misses actual sensor position")
            new.append(
                dict(
                    id=f"check-{r}-{c}",
                    role="validation",
                    optical_anchor_active_row_col=[y, x],
                    image_xy_mm=xy,
                    object_angle_deg=float(angle),
                    meridional_anchor_xy_mm=point.tolist(),
                    azimuth_deg=math.degrees(math.atan2(xy[1], xy[0])),
                    chief_anchor_residual_mm=error,
                )
            )
    extra = deepcopy(base)
    extra["fields"] = new
    extra["parent_geometry_sha256"] = base["sha256"]
    extra["binder_sha256"] = optics.sha(__file__)
    extra.pop("sha256", None)
    extra["sha256"] = digest(extra)
    fields = deepcopy(base["fields"])
    for c in fields:
        if c["role"] == "validation":
            c["role"] = "refinement_anchor"
    result = dict(
        schema="direct_boundary_center_refinement_v1",
        base=base,
        extra=extra,
        fields=fields + new,
        full_sensor_shape=g["full_sensor_shape"],
        grids=[3, 5, "refined"],
        independent_validation_fields=16,
        binder_sha256=optics.sha(__file__),
    )
    result["sha256"] = digest(result)
    return result


def validate(geometry, g):
    if geometry.get("sha256") != digest({k: v for k, v in geometry.items() if k != "sha256"}):
        raise ValueError("Refined anchor geometry changed")
    if geometry["binder_sha256"] != optics.sha(__file__):
        raise ValueError("Refinement geometry backend changed")
    source.validate(geometry["base"], g)
    source.validate(geometry["extra"], g)
    expected = deepcopy(geometry["base"]["fields"])
    for c in expected:
        if c["role"] == "validation":
            c["role"] = "refinement_anchor"
    if geometry["fields"] != expected + geometry["extra"]["fields"]:
        raise ValueError("Refinement source field lineage changed")
    a = {tuple(c["optical_anchor_active_row_col"]) for c in expected}
    v = {tuple(c["optical_anchor_active_row_col"]) for c in geometry["extra"]["fields"]}
    if len(a) != 41 or len(v) != 16 or a & v:
        raise ValueError("Final direct validation must be disjoint from all anchors")


def retain(profile, geometry, workspace, root, progress=None):
    validate(geometry, profile["sensor_crops"])
    first = source.retain(profile, geometry["base"], workspace, root, progress)
    source.prepare_cache(profile, geometry["extra"], workspace)
    base = source.cache_root(workspace, geometry["extra"])
    dest = Path(root) / "anchor-optics"
    fields = list(first["fields"])
    for c in geometry["extra"]["fields"]:
        row = source.field(profile["sensor_crops"], geometry["extra"], c["id"], workspace, progress)
        source._verify(base / c["id"], row)
        shutil.copytree(base / c["id"], dest / c["id"], dirs_exist_ok=True)
        fields.append(row)
    result = dict(
        schema=geometry["schema"],
        geometry=geometry,
        fields=fields,
        status="passed",
        numerical_profile_sha256=first["numerical_profile_sha256"],
    )
    optics.save(dest / "result.json", result)
    return result
