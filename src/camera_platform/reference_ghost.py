"""Two-reflection geometrical ghost bundles for the pinned, centered lens.

No housing scatter, sensor reflection, coherent interference or manufactured
coating prediction. Optical power is never normalized per ghost. Piecewise
linear pupil triangles carry projected entrance area to the receiving plane.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.spatial import Delaunay

COATINGS = ("fresnel", "r005", "r020")


def sag_gradient(surface, x, y):
    """Vector form of optics_surfaces.sag/radial_derivative, same coefficients."""
    r2 = x * x + y * y
    c = 0.0 if surface["radius_mm"] is None else 1.0 / surface["radius_mm"]
    a = surface.get("asphere", {})
    k = a.get("conic_constant", 0.0)
    q = 1 - (1 + k) * c * c * r2
    root = np.sqrt(np.maximum(q, 1e-30))
    sag = c * r2 / (1 + root)
    slope = c / root
    for order, coefficient in zip(a.get("orders", []), a.get("coefficients", []), strict=True):
        sag = sag + coefficient * r2 ** (order / 2)
        slope = slope + order * coefficient * r2 ** (order / 2 - 1)
    return sag, slope * x, slope * y, q > 0


def intersect(surface, vertex, positions, directions):
    """Bounded forward Newton intersection; failures remain numerical failures."""
    p, d = np.asarray(positions), np.asarray(directions)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        t = (vertex - p[:, 2]) / d[:, 2]
        t = np.where(np.isfinite(t), t, 0.0)
        domain = np.ones(len(t), bool)
        for _ in range(24):
            hit = p + t[:, None] * d
            z, gx, gy, good = sag_gradient(surface, hit[:, 0], hit[:, 1])
            f = hit[:, 2] - vertex - z
            derivative = d[:, 2] - gx * d[:, 0] - gy * d[:, 1]
            domain &= good & np.isfinite(f) & (np.abs(derivative) > 1e-10)
            step = np.where(domain, f / derivative, 0.0)
            t -= step
            if np.all((np.abs(step) < 1e-10) | ~domain):
                break
        hit = p + t[:, None] * d
        z, gx, gy, good = sag_gradient(surface, hit[:, 0], hit[:, 1])
        converged = domain & good & np.isfinite(hit).all(axis=1) & (t >= -1e-8)
        converged &= np.abs(hit[:, 2] - vertex - z) < 1e-7
        inside = np.hypot(hit[:, 0], hit[:, 1]) <= surface["semi_diameter_mm"]
        normal = np.stack((-gx, -gy, np.ones(len(t))), axis=1)
        normal /= np.linalg.norm(normal, axis=1)[:, None]
    return hit, normal, converged, inside


def interaction(direction, normal, n_from, n_to, reflect=False):
    """Unpolarized Fresnel power and vector Snell/reflection."""
    n = normal.copy()
    dot = np.sum(direction * n, axis=1)
    n[dot > 0] *= -1
    ci = np.clip(-np.sum(direction * n, axis=1), 0, 1)
    eta = n_from / n_to
    st2 = eta * eta * (1 - ci * ci)
    ct = np.sqrt(np.maximum(0, 1 - st2))
    rs = ((n_from * ci - n_to * ct) / np.maximum(n_from * ci + n_to * ct, 1e-30)) ** 2
    rp = ((n_to * ci - n_from * ct) / np.maximum(n_to * ci + n_from * ct, 1e-30)) ** 2
    fresnel = np.where(st2 > 1, 1.0, 0.5 * (rs + rp))
    out = (
        direction + 2 * ci[:, None] * n
        if reflect
        else eta[:, None] * direction + (eta * ci - ct)[:, None] * n
    )
    out /= np.maximum(np.linalg.norm(out, axis=1)[:, None], 1e-30)
    return out, fresnel, st2 > 1


def pupil_mesh(surface, samples):
    """Square cells plus circular rim, deterministic entrance-surface mesh."""
    radius = surface["semi_diameter_mm"]
    axis = np.linspace(-radius, radius, samples)
    xx, yy = np.meshgrid(axis, axis)
    keep = xx * xx + yy * yy < (radius * (1 - 1e-8)) ** 2
    phi = np.linspace(0, 2 * np.pi, samples * 4, endpoint=False)
    xy = np.concatenate((np.c_[xx[keep], yy[keep]], radius * np.c_[np.cos(phi), np.sin(phi)]))
    tri = Delaunay(xy).simplices
    z = sag_gradient(surface, xy[:, 0], xy[:, 1])[0]
    return np.c_[xy, z], tri


def trace_bundles(prescription, theta_deg, samples=32, callback=None):
    """Yield direct then all lens-surface-pair bundles, all wavelengths together.

    valid flags exclude blocked/TIR/numerical rays; reasons are reported separately.
    Origin samples share their IDs across wavelength and reflection paths.
    """
    surfaces = prescription["surfaces"]
    vertices = np.r_[0.0, np.cumsum([s["thickness_mm"] for s in surfaces])]
    waves = np.asarray(prescription["wavelength_nm"])
    points, triangles = pupil_mesh(surfaces[0], samples)
    npupil, nw = len(points), len(waves)
    theta = math.radians(theta_deg)
    direction = np.array([math.sin(theta), 0.0, math.cos(theta)])
    initial_area = (
        np.abs(
            np.einsum(
                "ij,j->i",
                np.cross(
                    points[triangles[:, 1]] - points[triangles[:, 0]],
                    points[triangles[:, 2]] - points[triangles[:, 0]],
                ),
                direction,
            )
        )
        * 0.5
        * 1e-6
    )
    wi = np.repeat(np.arange(nw), npupil)
    media = np.array([[1.0] * nw] + [s["refractive_index_after"] for s in surfaces])
    start = (
        np.tile(points, (nw, 1)),
        np.tile(direction, (nw * npupil, 1)),
        np.ones(nw * npupil, bool),
        np.ones((nw * npupil, 3)),
    )
    diagnostics = {
        "numerical_failure": 0,
        "aperture_blocked": 0,
        "total_internal_reflection": 0,
        "behind_sensor": 0,
    }

    def step(state, i, forward, reflect=False, first=False):
        pos, dirs, valid, weights = state
        outp, outd, outv, outw = pos.copy(), dirs.copy(), valid.copy(), weights.copy()
        ids = np.flatnonzero(valid)
        if not len(ids):
            return outp, outd, outv, outw
        if first:
            hit = pos[ids]
            _, gx, gy, _ = sag_gradient(surfaces[i], hit[:, 0], hit[:, 1])
            normal = np.c_[-gx, -gy, np.ones(len(ids))]
            normal /= np.linalg.norm(normal, axis=1)[:, None]
            good = inside = np.ones(len(ids), bool)
        else:
            hit, normal, good, inside = intersect(surfaces[i], vertices[i], pos[ids], dirs[ids])
        diagnostics["numerical_failure"] += int((~good).sum())
        diagnostics["aperture_blocked"] += int((good & ~inside).sum())
        active = good & inside
        outv[ids[~active]] = False
        ids2 = ids[active]
        if not len(ids2):
            return outp, outd, outv, outw
        n1 = media[i if forward else i + 1, wi[ids2]]
        n2 = media[i + 1 if forward else i, wi[ids2]]
        refr, r, tir = interaction(dirs[ids2], normal[active], n1, n2, reflect)
        refl = np.c_[r, np.full(len(r), 0.005), np.full(len(r), 0.02)]
        refl[tir] = 1.0
        if surfaces[i]["is_stop"]:
            refl[:] = 0
        if not reflect:
            diagnostics["total_internal_reflection"] += int(tir.sum())
            outv[ids2[tir]] = False
        outp[ids2] = hit[active]
        outd[ids2] = refr
        outw[ids2] *= refl if reflect else 1 - refl
        return outp, outd, outv, outw

    def finish(state, pair):
        p, d, v, w = state
        t = (vertices[-1] - p[:, 2]) / np.where(np.abs(d[:, 2]) > 1e-12, d[:, 2], np.nan)
        good = v & np.isfinite(t) & (t >= 0) & (d[:, 2] > 0)
        diagnostics["behind_sensor"] += int((v & ~good).sum())
        p = p + np.where(good, t, 0)[:, None] * d
        w[~good] = 0
        return {
            "pair": pair,
            "positions": p.reshape(nw, npupil, 3),
            "directions": d.reshape(nw, npupil, 3),
            "valid": good.reshape(nw, npupil),
            "weights": w.reshape(nw, npupil, 3),
            "triangles": triangles,
            "entrance_area_m2": initial_area,
            "diagnostics": diagnostics.copy(),
            "vertices_mm": vertices,
        }

    # Prefix before each interface, enabling all first reflections without retracing entrance.
    prefix = []
    state = start
    for j in range(len(surfaces)):
        prefix.append(state)
        state = step(state, j, True, first=(j == 0))
    yield finish(state, None)
    for j in range(1, len(surfaces)):
        if surfaces[j]["is_stop"]:
            continue
        if callback:
            callback(f"Ghost first reflection {j + 1}/{len(surfaces)} at {theta_deg:g}°")
        back = step(prefix[j], j, True, reflect=True)
        for i in range(j - 1, -1, -1):
            if not surfaces[i]["is_stop"]:
                branch = step(back, i, False, reflect=True)
                for k in range(i + 1, len(surfaces)):
                    if not branch[2].any():
                        break
                    branch = step(branch, k, True)
                yield finish(branch, [j, i])
            back = step(back, i, False)
            if not back[2].any():
                break


def raster_triangles(xy, triangles, valid, values, entrance_area, bounds, shape):
    """Conservative finite bundle density with 2x2 receiver quadrature.

    values[N,C] is the per-ray throughput (or QE-weighted throughput).
    Triangle power = entrance triangle area * mean vertex value. Folded bundles
    add incoherently. No interpolation across clipped pupil triangle vertices.
    Maps are density per receiving m². Sub-cell triangles use centroid deposits
    only for overview; quantitative callers can report under-resolved energy.
    """
    h, w = shape
    x0, x1, y0, y1 = bounds
    dx, dy = (x1 - x0) / w, (y1 - y0) / h
    out = np.zeros((h, w, values.shape[1]), float)
    active = triangles[np.all(valid[triangles], axis=1)]
    areas = entrance_area[np.all(valid[triangles], axis=1)]
    p = xy[active]
    lo = p.min(axis=1)
    hi = p.max(axis=1)
    keep = (hi[:, 0] >= x0) & (lo[:, 0] <= x1) & (hi[:, 1] >= y0) & (lo[:, 1] <= y1)
    under = 0.0
    admitted = 0.0
    for ids, pts, area in zip(active[keep], p[keep], areas[keep], strict=True):
        a, b, c = pts
        det = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        power = area * values[ids].mean(axis=0)
        if not np.isfinite(det) or abs(det) < 1e-15:
            continue
        projected_area = abs(det) * 0.5
        admitted += float(np.sum(power))
        if projected_area < dx * dy * 0.25:
            center = pts.mean(axis=0)
            ix, iy = int(np.floor((center[0] - x0) / dx)), int(np.floor((center[1] - y0) / dy))
            if 0 <= ix < w and 0 <= iy < h:
                out[iy, ix] += power / (dx * dy * 1e-6)
                under += float(np.sum(power))
            continue
        ix0 = max(0, int(np.floor((pts[:, 0].min() - x0) / dx)))
        ix1 = min(w, int(np.ceil((pts[:, 0].max() - x0) / dx)))
        iy0 = max(0, int(np.floor((pts[:, 1].min() - y0) / dy)))
        iy1 = min(h, int(np.ceil((pts[:, 1].max() - y0) / dy)))
        if ix1 <= ix0 or iy1 <= iy0:
            continue
        yy, xx = np.mgrid[iy0:iy1, ix0:ix1]
        coverage = np.zeros(xx.shape)
        for sy in (0.25, 0.75):
            for sx in (0.25, 0.75):
                qx = x0 + (xx + sx) * dx - a[0]
                qy = y0 + (yy + sy) * dy - a[1]
                u = (qx * (c[1] - a[1]) - qy * (c[0] - a[0])) / det
                v = ((b[0] - a[0]) * qy - (b[1] - a[1]) * qx) / det
                coverage += (u >= 0) & (v >= 0) & (u + v <= 1)
        out[iy0:iy1, ix0:ix1] += coverage[..., None] * 0.25 * power / (projected_area * 1e-6)
    return out, {"subcell_deposited_power": under, "intersecting_triangle_power": admitted}


def representative_path(prescription, theta_deg, samples, pair, pupil_index, wavelength_index=10):
    """One real admitted ray, for path attribution only (not bundle extent)."""
    surfaces = prescription["surfaces"]
    vertices = np.r_[0.0, np.cumsum([s["thickness_mm"] for s in surfaces])]
    points, _ = pupil_mesh(surfaces[0], samples)
    p = points[pupil_index : pupil_index + 1].copy()
    theta = math.radians(theta_deg)
    d = np.array([[math.sin(theta), 0.0, math.cos(theta)]])
    rows = [[float(p[0, 2] - 5 * d[0, 2]), float(p[0, 0] - 5 * d[0, 0])]]
    media = [1.0] + [s["refractive_index_after"][wavelength_index] for s in surfaces]
    n = len(surfaces)
    if pair is None:
        sequence = [(i, True, False) for i in range(n)]
    else:
        j, i = pair
        sequence = (
            [(k, True, False) for k in range(j)]
            + [(j, True, True)]
            + [(k, False, False) for k in range(j - 1, i, -1)]
            + [(i, False, True)]
            + [(k, True, False) for k in range(i + 1, n)]
        )
    for index, (i, forward, reflect) in enumerate(sequence):
        if index == 0:
            _, gx, gy, _ = sag_gradient(surfaces[0], p[:, 0], p[:, 1])
            normal = np.c_[-gx, -gy, [1.0]]
            normal /= np.linalg.norm(normal, axis=1)[:, None]
        else:
            p, normal, ok, inside = intersect(surfaces[i], vertices[i], p, d)
            if not (ok & inside).all():
                return None
        rows.append([float(p[0, 2]), float(p[0, 0])])
        d, _, _ = interaction(
            d,
            normal,
            np.array([media[i if forward else i + 1]]),
            np.array([media[i + 1 if forward else i]]),
            reflect,
        )
    t = (vertices[-1] - p[0, 2]) / d[0, 2]
    hit = p[0] + t * d[0]
    rows.append([float(hit[2]), float(hit[0])])
    return rows
