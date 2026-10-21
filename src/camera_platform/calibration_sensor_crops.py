"""Global sensor-coordinate crops of a declared interpolated effective camera.

The two calibration knots are retained at their actual DB chief-image heights.
Radial covariance/DC interpolation is an explicit modeling assumption, not nine
new measurements or a fresh lens prescription solve. Scene tiles share one
global chart function. Optical and ISP halos are removed only after processing.
"""

from __future__ import annotations

import math
from copy import deepcopy

import numpy as np
from scipy.fft import next_fast_len
from scipy.signal import fftconvolve

from . import db_design_sensor_timing as timing
from .registry import digest

SCHEMA = "calibration_sensor_coordinate_crops_v1"
ISP_HALO = 8
SHAPE = (128, 128)
SCOPE = [
    ("Vendor active dimensions; assumed optical-axis mounting and active-origin RGGB phase."),
    (
        "Two original DB chief-image coordinates retained; no re"
        "assignment of the virtual outer field to a sensor corne"
        "r."
    ),
    (
        "Radially interpolated Gaussian covariance and spectral "
        "DC between two calibrated knots; rotational symmetry as"
        "sumed."
    ),
    (
        "One stationary effective PSF per crop anchor; within-cr"
        "op field gradients, distortion transport and long-range"
        " flare are not qualified."
    ),
    (
        "Global chart coordinates, optical dependency halo, nati"
        "ve CFA and ISP halo are preserved; full sensor image is"
        " not allocated."
    ),
    (
        'Numerical convergence of this decla'
        'red model, not measured multi-field'
        ' camera validation.'
    ),
]


def covariance(node):
    a = math.radians(node["theta_deg"])
    r = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    return r @ np.diag([node["sigma_x_px"] ** 2, node["sigma_y_px"] ** 2]) @ r.T


def interpolate_node(center, outer, weight, azimuth, outer_azimuth):
    if not 0 <= weight <= 1:
        raise ValueError("Sensor crop would extrapolate beyond calibrated image-height knots")
    c = covariance(center)
    if not np.allclose(c, np.eye(2) * np.trace(c) / 2, rtol=1e-8, atol=1e-12):
        raise ValueError("Radial sensor mapping requires an isotropic center knot")
    # Kernel array y increases down, while optical-image azimuth uses y up.
    a = math.radians(outer_azimuth - azimuth)
    r = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    v, axes = np.linalg.eigh((1 - weight) * c + weight * (r @ covariance(outer) @ r.T))
    return {
        **deepcopy(center),
        "sigma_x_px": float(np.sqrt(v[1])),
        "sigma_y_px": float(np.sqrt(v[0])),
        "theta_deg": float(np.degrees(np.arctan2(axes[1, 1], axes[0, 1]))),
        "source_kind": "declared_radial_covariance_interpolation",
        "field_xy": [0.0, 0.0],
    }


def bind(profile, prepared, workspace):
    """Freeze verified dimensions and all derived crop parameters at submission."""
    sensor = prepared["candidate"]["source_identities"]["sensor"]
    if (
        sensor["revision_id"] != timing.REVISION
        or sensor["content_sha256"] != timing.REVISION_SHA
        or profile["pixel_pitch_um"] != 2.9
    ):
        raise ValueError("Sensor-coordinate crops require the pinned IMX462 structural model")
    dimensions = timing.profile(workspace)
    knots = {f["id"]: f for f in prepared["optical"]["fields"]}
    zero = np.asarray(knots["center"]["image_xy_mm"], float)
    outer_xy = np.asarray(knots["outer-ne"]["image_xy_mm"], float)
    radius = float(np.linalg.norm(outer_xy))
    if not np.isfinite(radius) or radius <= 0 or not np.allclose(zero, 0, atol=1e-8):
        raise ValueError("Physical DB chief-image anchors are required for radial interpolation")
    outer_azimuth = math.degrees(math.atan2(outer_xy[1], outer_xy[0]))
    nodes = [profile["fields"][f]["blur"]["field_nodes"][0] for f in ("center", "outer-ne")]
    crops = []
    axis = dimensions["optical_axis_active_row_col"]
    labels = (("nw", "n", "ne"), ("w", "center", "e"), ("sw", "s", "se"))
    for ri, row in enumerate((64, 484, 904)):
        for ci, col in enumerate((64, 904, 1744)):
            center = [row + 63.5, col + 63.5]
            anchor = axis[:] if (ri, ci) == (1, 1) else center[:]
            xy = [(anchor[1] - axis[1]) * 0.0029, -(anchor[0] - axis[0]) * 0.0029]
            weight = float(np.linalg.norm(xy)) / radius
            node = interpolate_node(
                *nodes, weight, math.degrees(math.atan2(xy[1], xy[0])), outer_azimuth
            )
            crops.append(
                {
                    "id": "crop-" + labels[ri][ci],
                    "origin_row_col": [row, col],
                    "shape_row_col": list(SHAPE),
                    "center_active_row_col": center,
                    "optical_anchor_active_row_col": anchor,
                    "image_xy_mm": xy,
                    "interpolation_weight": weight,
                    "node": node,
                    "spectral_dc_gain": (
                        (1 - weight) * np.asarray(profile["spectral_dc_gain"]["center"])
                        + weight * np.asarray(profile["spectral_dc_gain"]["outer-ne"])
                    ).tolist(),
                    "spectral_response": (
                        (1 - weight) * np.asarray(profile["spectral_response"]["center"])
                        + weight * np.asarray(profile["spectral_response"]["outer-ne"])
                    ).tolist(),
                }
            )
    geometry = {
        "schema": SCHEMA,
        "full_sensor_shape": dimensions["active_shape_row_col"],
        "pixel_pitch_um": dimensions["pixel_pitch_um"],
        "optical_axis_active_row_col": axis,
        "coordinate_convention": (
            "native pixel centers at integer active row/col; x right, optical y up"
        ),
        "cfa": "RGGB",
        "cfa_phase_provenance": "assumed at active-array origin; all crops and halos even",
        "dimension_source": dimensions["dimension_source"],
        "sensor_revision_id": sensor["revision_id"],
        "sensor_revision_sha256": sensor["content_sha256"],
        "source_geometry_sha256": dimensions["profile_sha256"],
        "knots_image_xy_mm": {f: knots[f]["image_xy_mm"] for f in ("center", "outer-ne")},
        "interpolation": (
            "linear_covariance_and_spectral_dc_in_image_radius_with_rotational_symmetry"
        ),
        "isp_halo_native_px": ISP_HALO,
        "crops": crops,
        "scope": SCOPE,
        "physical_validation": "not_established",
        "full_frame_allocated": False,
    }
    geometry["geometry_sha256"] = digest(geometry)
    p = deepcopy(profile)
    p["sensor_crops"] = geometry
    p.pop("profile_sha256", None)
    p["profile_sha256"] = digest(p)
    return p


def validate(g):
    if g.get("schema") != SCHEMA or g.get("geometry_sha256") != digest(
        {k: v for k, v in g.items() if k != "geometry_sha256"}
    ):
        raise ValueError("Frozen sensor crop geometry hash or schema differs")
    if len(g["crops"]) != 9 or len({c["id"] for c in g["crops"]}) != 9:
        raise ValueError("Nine unique located sensor crops required")
    for c in g["crops"]:
        if c["shape_row_col"] != list(SHAPE):
            raise ValueError("Native 128x128 crop required")
        for origin, n, full in zip(c["origin_row_col"], SHAPE, g["full_sensor_shape"], strict=True):
            if type(origin) is not int or origin % 2 or origin < 0 or origin + n > full:
                raise ValueError("Even native RGGB crop must be inside active sensor")


def expand(profile):
    p = deepcopy(profile)
    g = p.get("sensor_crops")
    if not g:
        return p
    if g.get("psf_source") == "direct_prescription_reference_sphere_scalar_diffraction":
        return p
    validate(g)
    for c in g["crops"]:
        fid = c["id"]
        f = deepcopy(p["fields"]["center"])
        f["blur"]["field_nodes"] = [deepcopy(c["node"])]
        p["fields"][fid] = f
        p["spectral_response"][fid] = c["spectral_response"]
        p["spectral_dc_gain"][fid] = c["spectral_dc_gain"]
    return p


def crop_for(profile, fid):
    return next(c for c in profile["sensor_crops"]["crops"] if c["id"] == fid)


def cost(node, s, isp=ISP_HALO):
    from . import calibration_psf_convergence as conv

    result = conv.cost(node, s)
    *_, q, radius = conv.parameters(node, s)
    optical = 2 * math.ceil(math.ceil(radius / q) / 2)
    source = [(n + 2 * isp + 2 * optical) * q for n in SHAPE]
    fft = [next_fast_len(n + 2 * radius, real=True) for n in source]
    area = math.prod(fft)
    result.update(
        fft_shape=fft,
        source_tile_samples_row_col=source,
        # Achromatic edge factorization uses one convolution, then RGB scaling.
        fft_work_proxy=float(area * math.log2(area)),
        fft_channels=1,
        working_bytes_estimate=int(
            (
                math.prod(source) * 3
                + area * 8
                + (2 * radius + 1) ** 2
                + math.prod([n + 2 * isp for n in SHAPE]) * 24
            )
            * 8
        ),
    )
    return result


def global_chart(g, origin, shape, q, orientation, phase):
    """Evaluate one global 3x3 edge-chart layout at arbitrary source tile indices."""
    rows = np.arange(shape[0] * q, dtype=float) / q + origin[0] - (q // 2) / q
    cols = np.arange(shape[1] * q, dtype=float) / q + origin[1] - (q // 2) / q
    anchors = np.asarray([c["center_active_row_col"] for c in g["crops"]])
    ys, xs = np.unique(anchors[:, 0]), np.unique(anchors[:, 1])
    # Global cell identity is independent of requested crop or tile origin.
    yi = np.argmin(abs(rows[:, None] - ys), axis=1)
    xi = np.argmin(abs(cols[:, None] - xs), axis=1)
    y, x = (rows - ys[yi])[:, None], (cols - xs[xi])[None, :]
    a = math.radians(orientation)
    across, along = x * math.cos(a) + y * math.sin(a), y * math.cos(a) - x * math.sin(a)
    return 0.1 + 0.7 * (across > 0.07 * along + phase)


def capture(profile, prepared, fid, s, orientation, phase, extra=0):
    """Render optical halo, capture RAW/ISP halo, then crop at global native indices."""
    from . import calibration_db_core_engine as core
    from . import calibration_psf_convergence as conv
    from . import reference_isp
    from .calibration_unified_engine import remosaic

    g, c = profile["sensor_crops"], crop_for(profile, fid)
    node = c["node"]
    q = s["samples_per_pixel"]
    isp = ISP_HALO + extra
    if type(extra) is not int or extra < 0 or extra % 2 or extra > 16:
        raise ValueError("Expanded canvas halo must be an even 0..16 native pixels")
    k = conv.kernel(node, s)
    radius = k.shape[0] // 2
    optical = 2 * math.ceil(math.ceil(radius / q) / 2)
    halo = isp + optical
    origin = [v - halo for v in c["origin_row_col"]]
    shape = [v + 2 * halo for v in SHAPE]
    if any(
        o < 0 or o + n > a for o, n, a in zip(origin, shape, g["full_sensor_shape"], strict=True)
    ):
        raise ValueError("Sensor crop lacks optical/ISP dependency halo inside active array")
    estimate = cost(node, s, isp)
    if estimate["working_bytes_estimate"] > conv.MAX_WORK_BYTES:
        raise ValueError("Sensor crop exceeds bounded working memory")
    mono = global_chart(g, origin, shape, q, orientation, phase)
    blurred = fftconvolve(mono, k, mode="same")
    native = blurred[
        optical * q + q // 2 : (shape[0] - optical) * q : q,
        optical * q + q // 2 : (shape[1] - optical) * q : q,
    ]
    response = np.asarray(c["spectral_dc_gain"])[:, None] * np.asarray(c["spectral_response"])
    rgb = response[:, [0, 1, 3]].copy()
    rgb[:, 1] = response[:, 1:3].mean(axis=1)
    white = np.asarray(prepared["spectral"]["white"]) @ rgb * prepared["spec"]["exposure_ms"] / 1000
    charge = remosaic(np.maximum(0, native[..., None] * white))
    local = profile["fields"][fid]
    raw = core.lab._capture_electrical(
        local,
        core.lab._electrical_model(local),
        {},
        charge,
        prepared["spec"]["exposure_ms"] / 1000,
        0,
        noise_enabled=False,
    )["arrays"]["raw_dn"].astype(np.uint16)
    linear = reference_isp.process(
        raw,
        {**profile["electronics"], "gain": prepared["spec"]["gain"]},
        profile["isp"]["electron_to_output_matrix"],
        profile["isp_tuning"],
    )[0]
    raw_crop, isp_crop = raw[isp:-isp, isp:-isp].copy(), linear[isp:-isp, isp:-isp].copy()
    meta = core.metadata(
        local,
        {"id": fid, "chart": "mtf", "field": fid, "orientation": orientation},
        "observation",
        0,
        prepared["spec"]["exposure_ms"] / 1000,
    )
    meta["geometry"] = {
        "full_sensor_shape": g["full_sensor_shape"],
        "crop_origin": c["origin_row_col"],
    }
    meta["analysis"].pop("field_xy", None)
    evidence = {
        "geometry_sha256": g["geometry_sha256"],
        "crop": c,
        "source_tile_origin_row_col": origin,
        "source_tile_shape_row_col": shape,
        "optical_halo_native_px": optical,
        "isp_halo_native_px": isp,
        "raw_halo_origin_row_col": [v - isp for v in c["origin_row_col"]],
        "raw_halo_shape_row_col": list(raw.shape),
        "native_crop_after_isp": True,
        "local_stationary_psf": True,
        "full_frame_allocated": False,
    }
    return raw_crop, isp_crop, meta, evidence, raw


def parity(profile, prepared, s, root):
    """Expanded-canvas crop parity tests tile origin, halo and CFA alignment."""
    from . import calibration_db_core_engine as core

    p = expand(profile)
    rows = []
    for c in p["sensor_crops"]["crops"]:
        for angle in (0, 90):
            for phase in (0.0, 0.25):
                a = capture(p, prepared, c["id"], s, angle, phase)
                b = capture(p, prepared, c["id"], s, angle, phase, extra=16)
                raw_error = int(np.max(np.abs(a[0].astype(int) - b[0].astype(int))))
                isp_error = float(np.max(np.abs(a[1] - b[1])))
                rows.append(
                    {
                        "field": c["id"],
                        "orientation": angle,
                        "phase": phase,
                        "raw_max_abs_dn": raw_error,
                        "isp_max_abs": isp_error,
                        "passed": raw_error == 0 and isp_error <= 1e-12,
                        "base_geometry": a[3],
                        "expanded_geometry": b[3],
                    }
                )
    result = {
        "method": "same_global_chart_and_local_psf_on_32_pixel_larger_canvas_then_crop",
        "scope": "located tile/halo/CFA/ISP equivalence, not full-frame varying-PSF equivalence",
        "passed": all(r["passed"] for r in rows),
        "checks": rows,
    }
    core.save(root / "convergence" / "crop-parity.json", result)
    return result
