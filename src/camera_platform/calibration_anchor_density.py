"""Whole-active-sensor direct anchor coverage and spatial-density qualification."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import numpy as np

from . import calibration_anchor_refinement as refinement
from . import calibration_anchor_source as source
from . import calibration_direct_convergence as direct
from . import calibration_direct_optics as optics
from . import calibration_spatial_psf as spatial
from . import reference_isp
from .registry import digest

POLICY = "camera_full_sensor_boundary_anchor_density_v1"
KERNEL_BYTES = 4 * (1025**2 * 3 * 4)
LIMITS = dict(psf_l1=0.03, mtf_max_abs=0.005, mtf50_relative=0.01)


def topology(geometry, n):
    if n == "refined":
        positions = {c["id"]: c["optical_anchor_active_row_col"] for c in geometry["fields"]}
        names = [c["id"] for c in geometry["fields"] if c["role"] != "validation"]
        anchors = {fid: positions[fid] for fid in names}
        triangles = []
        for r in range(4):
            for c in range(4):
                ring = [f"a-{r}-{c}", f"a-{r}-{c + 1}", f"a-{r + 1}-{c + 1}", f"a-{r + 1}-{c}"]
                triangles.extend([[ring[i], ring[(i + 1) % 4], f"v-{r}-{c}"] for i in range(4)])
        return anchors, triangles
    if n not in (3, 5):
        raise ValueError("Qualified grids are 3x3, 5x5 and 5x5+16 directly traced centers")
    step = 4 // (n - 1)
    ids = [[f"a-{r}-{c}" for c in range(0, 5, step)] for r in range(0, 5, step)]
    positions = {c["id"]: c["optical_anchor_active_row_col"] for c in geometry["fields"]}
    anchors = {fid: positions[fid] for row in ids for fid in row}
    triangles = []
    for r in range(n - 1):
        for c in range(n - 1):
            nw, ne, sw, se = ids[r][c], ids[r][c + 1], ids[r + 1][c], ids[r + 1][c + 1]
            triangles.extend([[nw, ne, se], [nw, se, sw]])
    return anchors, triangles


def application(profile, geometry, n, kernels):
    refinement.validate(geometry, profile["sensor_crops"])
    anchors, triangles = topology(geometry, n)
    return dict(
        schema=POLICY,
        renderer_sha256=optics.sha(__file__),
        shared_renderer_sha256=optics.sha(spatial.__file__),
        numpy=np.__version__,
        scipy=spatial.scipy.__version__,
        grid_size=n,
        setting=source.SETTING,
        anchors=anchors,
        triangles=triangles,
        outer_boundary="direct_anchors_cover_every_active_native_pixel_center",
        outside_active_receiver="refuse; optical scene input may extend beyond active sensor",
        weights="nonnegative_barycentric_at_native_receiver_centers",
        optical_halo_native_px=65,
        isp_halo_native_px=8,
        tile_shape_row_col=[128, 128],
        fft_cache_max_bytes=spatial.CACHE_BYTES,
        kernel_cache_max_bytes=KERNEL_BYTES,
        geometry_sha256=geometry["sha256"],
        kernels=[k for k in kernels if k["field"] in anchors],
        photon_dose=32000.0,
        pixel_aperture_application_count=1,
        physical_validation="not_established",
    )


class KernelBank(Mapping):
    """At most four native optical kernels, regardless of total anchor count."""

    def __init__(self, root, rows, budget=KERNEL_BYTES):
        self.root = Path(root).resolve()
        self.rows = {r["field"]: r for r in rows}
        if len(self.rows) != len(rows):
            raise ValueError("Duplicate anchor kernel")
        self.budget = budget
        self.data = OrderedDict()
        self.bytes = 0
        self.peak = 0

    def __len__(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __contains__(self, fid):
        return fid in self.rows

    def __getitem__(self, fid):
        if fid in self.data:
            self.data.move_to_end(fid)
            return self.data[fid]
        row = self.rows[fid]
        rel = Path(row["file"])
        p = (self.root / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or not p.is_relative_to(self.root):
            raise ValueError("Anchor kernel escapes immutable result")
        if optics.sha(p) != row["sha256"]:
            raise ValueError("Anchor kernel bytes changed")
        expected_bytes = 1025**2 * 3 * 4
        if expected_bytes > self.budget:
            raise ValueError("One anchor exceeds kernel cache")
        while self.bytes + expected_bytes > self.budget:
            _, v = self.data.popitem(last=False)
            self.bytes -= v.nbytes
            del v
        with np.load(p, allow_pickle=False) as z:
            k = z["mass_rgb"]
        if (
            k.shape != (1025, 1025, 3)
            or k.dtype != np.float32
            or not np.isfinite(k).all()
            or np.any(k < 0)
            or np.any(k.sum(axis=(0, 1), dtype=float) <= 0)
        ):
            raise ValueError("Invalid direct anchor optical mass")
        if k.nbytes > self.budget:
            raise ValueError("One anchor exceeds kernel cache")
        while self.bytes + k.nbytes > self.budget:
            _, v = self.data.popitem(last=False)
            self.bytes -= v.nbytes
        self.data[fid] = k
        self.bytes += k.nbytes
        self.peak = max(self.peak, self.bytes)
        return k


def weights(a, bounds, stride=1):
    y0, y1, x0, x1 = bounds
    points = np.array(list(a["anchors"].values()))
    if (
        y0 < points[:, 0].min()
        or x0 < points[:, 1].min()
        or y1 - 1 > points[:, 0].max()
        or x1 - 1 > points[:, 1].max()
    ):
        raise ValueError("Receiving position outside directly covered active sensor")
    # Existing barycentric evaluator's clip is identity over this full-sensor hull.
    return spatial.weights(a, bounds, stride)


def estimate(a, tile=128):
    cost = spatial.estimate(a, tile)
    per_kernel = (
        (2 * a["setting"]["samples_per_pixel"] * a["setting"]["radius_native_px"] + 1) ** 2 * 3 * 4
    )
    prior_reserve = cost.pop("kernel_bank_bytes")
    resident = 4 * per_kernel
    extra_weights = max(0, len(a["anchors"]) - 9) * tile * tile * 8
    cost["maximum_array_working_bytes_estimate"] += max(0, resident + extra_weights - prior_reserve)
    if cost["maximum_array_working_bytes_estimate"] > spatial.MAX_ARRAY_BYTES:
        raise ValueError("Direct boundary renderer exceeds bounded optical array budget")
    cost.update(
        resident_kernel_cache_max_bytes=resident,
        all_kernels_loaded_bytes_counterfactual=len(a["anchors"]) * per_kernel,
        native_anchor_weights_max_bytes=len(a["anchors"]) * tile * tile * 8,
        estimate_scope=(
            "conservative shared FFT/native allo"
            "wance plus bounded four-kernel cach"
            "e and anchor weights; not process R"
            "SS"
        ),
    )
    return cost


def _capture_native(profile, root, a, bank, scene, out, tile=128, progress=None):
    root = Path(root)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    h, w = profile["sensor_crops"]["full_sensor_shape"]
    cost = estimate(a, tile)
    photons = np.array(profile["sensor_crops"]["white_e_rgb"]) * 4
    planes = np.lib.format.open_memmap(
        out / "work-electrons.npy", mode="w+", dtype=float, shape=(h, w, 3)
    )
    cache = spatial.FFTCache(bank)
    started = time.perf_counter()
    count = 0
    for ch in range(3):
        for b in spatial.tiles((0, h, 0, w), tile):
            y, z, x, v = b
            weights(a, b)  # Refuse extrapolation before shared renderer.
            planes[y:z, x:v, ch] = spatial.convolve_tile(a, bank, cache, scene, b, ch) * photons[ch]
            count += 1
            if progress and x == 0:
                progress(
                    f"경계 앵커 전체 센서 · {len(a['anchors'])}점 · 채널 {ch + 1}/3 · 행 {y}/{h}"
                )
    planes.flush()
    raw = np.lib.format.open_memmap(out / "raw.npy", mode="w+", dtype=np.uint16, shape=(h, w))
    for b in spatial.tiles((0, h, 0, w), tile):
        y, z, x, v = b
        raw[y:z, x:v] = spatial.electrons_to_raw(profile, planes[y:z, x:v], (y, x))
    raw.flush()
    del planes
    (out / "work-electrons.npy").unlink()
    linear = np.lib.format.open_memmap(out / "linear.npy", mode="w+", dtype=float, shape=(h, w, 3))
    for b in spatial.tiles((0, h, 0, w), tile):
        y, z, x, v = b
        linear[y:z, x:v] = spatial.isp_window(profile, raw, b)
    linear.flush()
    clipped = int(np.count_nonzero(raw >= 2 ** profile["electronics"]["bit_depth"] - 1))
    if clipped:
        raise ValueError("Anchor-density MTF chart clips ADC")
    return dict(
        raw=str((out / "raw.npy").relative_to(root)),
        linear=str((out / "linear.npy").relative_to(root)),
        raw_sha256=optics.sha(out / "raw.npy"),
        linear_sha256=optics.sha(out / "linear.npy"),
        shape_row_col=[h, w],
        elapsed_s=time.perf_counter() - started,
        tile_size=tile,
        optical_channel_tiles=count,
        clipped_adc_pixels=clipped,
        fft_cache_peak_bytes=cache.peak,
        kernel_cache_peak_bytes=bank.peak,
        **cost,
    )


def capture(profile, root, a, bank, scene, out, tile=128, progress=None, *, cache_workspace=None):
    from .calibration_frame_cache import capture as cached_capture

    return cached_capture(
        _capture_native,
        profile,
        root,
        a,
        bank,
        scene,
        out,
        tile,
        progress,
        workspace=cache_workspace,
    )


def mixture(a, bank, row, col):
    # Validation anchors can lie between native centers; never truncate coordinates.
    b = None
    for tri in a["triangles"]:
        p = np.array([a["anchors"][fid] for fid in tri])
        uv = np.linalg.solve(
            np.stack([p[0] - p[2], p[1] - p[2]], axis=1), np.array([row, col]) - p[2]
        )
        bary = np.r_[uv, 1 - uv.sum()]
        if bary.min() >= -1e-10:
            b = {fid: float(max(0, w)) for fid, w in zip(tri, bary, strict=True) if w > 0}
            total = sum(b.values())
            b = {fid: w / total for fid, w in b.items()}
            break
    if b is None:
        raise ValueError("Independent probe outside covered sensor")
    k = np.zeros((1025, 1025, 3), float)
    for fid, w in b.items():
        v = bank[fid]
        k += w * v / v.sum(axis=(0, 1), dtype=float)
    return k


def stationary(profile, k, c):
    """Independent direct-probe MTF: translated interior patch, never boundary halo fiction."""
    g = profile["sensor_crops"]
    test = deepcopy(g["crops"][4])
    test["id"] = c["id"]
    rows = {}
    for angle in (0, 90):
        cdf, offset = direct.projection_cdf(k, angle)
        for phase in direct.PHASES:
            raw, linear, _, meta, _ = direct.capture(
                profile, g, test, source.SETTING, angle, phase, cdf, offset
            )
            curves, _ = direct.curves(raw, linear, meta, angle)
            rows[f"edge-{angle}-phase-{int(phase * 100):02d}"] = dict(curves=curves)
    return dict(
        status="measured",
        energy_rgb=[1.0, 1.0, 1.0],
        kernel_curves=direct.optical_curves(k, 8),
        captures=rows,
    )


def local_gate(profile, a, bank, geometry, folder, root, progress):
    checks = []
    for c in geometry["fields"]:
        if c["id"] in a["anchors"] or c["role"] not in {"validation", "refinement_anchor"}:
            continue
        if progress:
            progress(f"독립 직접 PSF 공간 밀도 · {len(a['anchors'])}점 · {c['id']}")
        row, col = c["optical_anchor_active_row_col"]
        k = mixture(a, bank, row, col)
        ref = bank[c["id"]]
        ref = ref / ref.sum(axis=(0, 1), dtype=float)
        l1 = np.abs(k - ref).sum(axis=(0, 1)).tolist()
        measured = stationary(profile, k, c)
        truth = stationary(profile, ref, c)
        comparison = direct.compare({"fields": {c["id"]: measured}}, {"fields": {c["id"]: truth}})
        checks.append(
            dict(
                field=c["id"],
                position=[row, col],
                psf_l1_rgb=l1,
                passed=max(l1) <= LIMITS["psf_l1"] and comparison["passed"],
                comparison=comparison,
                measured=measured,
                reference=truth,
            )
        )
    return dict(
        grid_size=a["grid_size"],
        anchor_count=len(a["anchors"]),
        passed=all(r["passed"] for r in checks),
        checks=checks,
    )


class BoundaryAtlas:
    """25 chart panels; outer 128x128 analysis ROIs touch native top/left boundaries."""

    def __init__(self, g, angle, phase):
        self.angle = angle
        self.phase = phase
        h, w = g["full_sensor_shape"]
        self.origins_y = [int(round(v / 2)) * 2 for v in np.linspace(0, h - 129, 5)]
        self.origins_x = [int(round(v / 2)) * 2 for v in np.linspace(0, w - 129, 5)]
        self.rows = [v + 63.5 for v in self.origins_y]
        self.cols = [v + 63.5 for v in self.origins_x]
        self.crops = [
            dict(
                id=f"roi-{r}-{c}",
                origin_row_col=[y, x],
                shape_row_col=[128, 128],
                center_active_row_col=[y + 63.5, x + 63.5],
            )
            for r, y in enumerate(self.origins_y)
            for c, x in enumerate(self.origins_x)
        ]

    __call__ = spatial.EdgeAtlas.__call__

    def specification(self):
        return dict(
            type="full_sensor_25_panel_native_edge_atlas_v1",
            orientation_deg=self.angle,
            phase_native_px=self.phase,
            panel_center_rows=self.rows,
            panel_center_cols=self.cols,
            slope_rational=[7, 100],
            outside_active_sensor="same_analytic_extended_chart",
            roi_scope=(
                "128 square even-origin analysis win"
                "dows; last active row/col in whole-"
                "frame parity only"
            ),
        )


def window_checks(profile, root, a, bank, scene, full, progress=None):
    """Independent optical windows and globally phased real-sensor boundary halos."""
    rows = []
    r0 = np.load(root / full["raw"], mmap_mode="r")
    l0 = np.load(root / full["linear"], mmap_mode="r")
    h, w = profile["sensor_crops"]["full_sensor_shape"]
    for c in scene.crops:
        y, x = c["origin_row_col"]
        if progress:
            progress("독립 경계 Crop 일치 · " + full["chart"] + " · " + c["id"])
        yy = spatial.reflected_indices(y - 8, y + 136, h)
        xx = spatial.reflected_indices(x - 8, x + 136, w)
        by = (
            int(yy.min()) // 2 * 2,
            min(h, int(yy.max()) + 1),
            int(xx.min()) // 2 * 2,
            min(w, int(xx.max()) + 1),
        )
        planes, _ = spatial.optical_window(a, bank, scene, by, tile=96)
        planes *= np.array(profile["sensor_crops"]["white_e_rgb"]) * 4
        localraw = spatial.electrons_to_raw(profile, planes, (by[0], by[2]))
        halo = localraw[(yy - by[0])[:, None], (xx - by[2])[None, :]]
        li = reference_isp.process(
            halo,
            {**profile["electronics"], "gain": profile["conditions"]["gain"]},
            profile["isp"]["electron_to_output_matrix"],
            profile["isp_tuning"],
        )[0][8:-8, 8:-8]
        rd = int(
            np.max(abs(r0[y : y + 128, x : x + 128].astype(int) - halo[8:-8, 8:-8].astype(int)))
        )
        ld = float(np.max(abs(l0[y : y + 128, x : x + 128] - li)))
        rows.append(
            dict(
                field=c["id"],
                chart=full["chart"],
                grid_size=full["grid_size"],
                raw_max_abs_dn=rd,
                isp_max_abs=ld,
                passed=rd == 0 and ld <= 1e-12,
            )
        )
    return rows


def numerical_cache_workspace(profile, geometry, workspace):
    """Separate RAW/ISP qualification caches when optics match but readout/CCM differ.

    The original geometry-only cache remains usable only after exact input
    readback. Otherwise a deterministic numerical-input namespace avoids stale
    RAW/ISP evidence without changing physical optical geometry identities.
    """
    workspace = Path(workspace)
    inputs = {k: profile[k] for k in ("fields", "electronics", "conditions", "isp", "isp_tuning")}
    context = digest(inputs)
    matching = True
    for g in (geometry["base"], geometry["extra"]):
        path = source.cache_root(workspace, g) / "numerical-profile.json"
        if not path.exists() or json.loads(path.read_text()) != inputs:
            matching = False
            break
    return (
        workspace if matching else workspace / ".cache" / "calibration-numerical-contexts" / context
    ), context


def qualify(profile, root, workspace, progress=None):
    root = Path(root)
    folder = root / "anchor-density"
    folder.mkdir(exist_ok=False)
    geometry = profile["psf_anchor_geometry"]
    cache_workspace, numeric_context = numerical_cache_workspace(profile, geometry, workspace)
    physical = refinement.retain(profile, geometry, cache_workspace, root, progress)
    kernels = [
        dict(
            field=r["field"]["id"],
            file="anchor-optics/" + r["field"]["id"] + "/kernel.npz",
            sha256=r["kernel_sha256"],
            setting=source.SETTING,
            pixel_aperture_included=False,
        )
        for r in physical["fields"]
    ]
    bank = KernelBank(root, kernels)
    applications = {n: application(profile, geometry, n, kernels) for n in (3, 5, "refined")}
    candidates = [
        local_gate(profile, applications[n], bank, geometry, folder, root, progress)
        for n in (3, 5, "refined")
    ]
    full_grids = ([3] if candidates[0]["passed"] else []) + [5, "refined"]
    captures = []
    comparisons = []
    parity = []
    tile_check = None
    # Compare both spatial densities with the exact same four complete sensor charts.
    for angle in (0, 90):
        for phase in direct.PHASES:
            cid = f"edge-{angle}-phase-{int(phase * 100):02d}"
            scene = BoundaryAtlas(profile["sensor_crops"], angle, phase)
            frames = {}
            for n in full_grids:
                f = capture(
                    profile,
                    root,
                    applications[n],
                    bank,
                    scene,
                    folder / f"grid-{n}-{cid}",
                    progress=progress,
                    cache_workspace=workspace,
                )
                f.update(id=f"grid-{n}-{cid}", grid_size=n, chart=cid, scene=scene.specification())
                captures.append(f)
                frames[n] = (
                    np.load(root / f["raw"], mmap_mode="r"),
                    np.load(root / f["linear"], mmap_mode="r"),
                )
            for c in scene.crops:
                y, x = c["origin_row_col"]
                meta = spatial.metadata(profile, c, angle)
                raw, lin = frames[5]
                refraw, reflin = frames["refined"]
                change = spatial.metrics(
                    raw[y : y + 128, x : x + 128],
                    lin[y : y + 128, x : x + 128],
                    meta,
                    angle,
                    refraw[y : y + 128, x : x + 128],
                    reflin[y : y + 128, x : x + 128],
                )
                comparisons.append(
                    dict(
                        field=c["id"],
                        chart=cid,
                        candidate_grid_size=5,
                        reference_grid_size="refined",
                        origin_row_col=[y, x],
                        **change,
                    )
                )
                if 3 in frames:
                    raw3, lin3 = frames[3]
                    v = spatial.metrics(
                        raw3[y : y + 128, x : x + 128],
                        lin3[y : y + 128, x : x + 128],
                        meta,
                        angle,
                        refraw[y : y + 128, x : x + 128],
                        reflin[y : y + 128, x : x + 128],
                    )
                    comparisons.append(
                        dict(
                            field=c["id"],
                            chart=cid,
                            candidate_grid_size=3,
                            reference_grid_size="refined",
                            origin_row_col=[y, x],
                            **v,
                        )
                    )
    # A coarse candidate must also agree with the denser whole-sensor chart.
    # The 5x5 reference still needs the sixteen independent direct-probe gates.
    for candidate in candidates:
        candidate["independent_probe_pass"] = candidate["passed"]
        relevant = [
            v
            for c in comparisons
            if c["candidate_grid_size"] == candidate["grid_size"]
            for v in c["mtf"].values()
        ]
        candidate["whole_sensor_reference_pass"] = (
            all(v["passed"] for v in relevant) if relevant else None
        )
        if candidate["grid_size"] != "refined":
            candidate["passed"] &= candidate["whole_sensor_reference_pass"] is True
    valid = [v for v in candidates if v["passed"]]
    selected = min(valid, key=lambda v: v["anchor_count"])["grid_size"] if valid else None
    if selected is not None:
        selected_captures = [c for c in captures if c["grid_size"] == selected]
        for f in selected_captures:
            angle = f["scene"]["orientation_deg"]
            phase = f["scene"]["phase_native_px"]
            scene = BoundaryAtlas(profile["sensor_crops"], angle, phase)
            parity.extend(
                window_checks(profile, root, applications[selected], bank, scene, f, progress)
            )
        f0 = selected_captures[0]
        scene = BoundaryAtlas(profile["sensor_crops"], 0, 0.0)
        alternate = capture(
            profile,
            root,
            applications[selected],
            bank,
            scene,
            folder / "partition-96",
            tile=96,
            progress=progress,
            cache_workspace=workspace,
        )
        r0 = np.load(root / f0["raw"], mmap_mode="r")
        l0 = np.load(root / f0["linear"], mmap_mode="r")
        rr = np.load(root / alternate["raw"], mmap_mode="r")
        ll = np.load(root / alternate["linear"], mmap_mode="r")
        rd = int(np.max(abs(r0.astype(int) - rr.astype(int))))
        ld = float(np.max(abs(l0 - ll)))
        tile_check = dict(
            active_pixels_compared=int(rr.size),
            raw_max_abs_dn=rd,
            isp_max_abs=ld,
            passed=rd == 0 and ld <= 1e-12,
            alternate=alternate,
        )
    passed = selected is not None and tile_check["passed"] and all(v["passed"] for v in parity)
    result = dict(
        schema=POLICY,
        status="passed" if passed else "not_converged",
        geometry=geometry,
        source=physical,
        numerical_cache_context_sha256=numeric_context,
        kernels=kernels,
        candidates=candidates,
        selected_grid_size=selected,
        selected_anchor_count=None if selected is None else len(applications[selected]["anchors"]),
        application=None if selected is None else applications[selected],
        limits=LIMITS,
        captures=captures,
        full_sensor_density_comparisons=comparisons,
        independent_window_checks=parity,
        partition_check=tile_check,
        selection_scope=(
            "minimum of 9/25/41 anchors passing "
            "sixteen fresh direct probes and ful"
            "l chart comparison; finite validati"
            "on domain"
        ),
        reference_scope=(
            "full-sensor 25 vs 41 sensitivity; 41-anchor model is not continuous direct truth"
        ),
        physical_validation="not_established",
    )
    optics.save(folder / "result.json", result)
    return result


def apply(profile, result):
    p = deepcopy(profile)
    p["boundary_spatial_psf"] = {
        **deepcopy(result["application"] or {}),
        "validation_status": result["status"],
    }
    p.pop("profile_sha256", None)
    p["profile_sha256"] = digest(p)
    return p


def capture_applied(profile, root, scene, out, tile=128, progress=None):
    if profile.get("profile_sha256") != digest(
        {k: v for k, v in profile.items() if k != "profile_sha256"}
    ):
        raise ValueError("Applied boundary profile hash changed")
    a = deepcopy(profile["boundary_spatial_psf"])
    if a.pop("validation_status", None) != "passed":
        raise ValueError("Boundary density has not passed")
    if a.get("grid_size") == "adaptive":
        from .calibration_adaptive_anchors import application as adaptive_application
        expected = adaptive_application(
            profile, profile["psf_anchor_geometry"], a["selected_anchor_ids"], a["kernels"]
        )
    else:
        expected = application(
            profile, profile["psf_anchor_geometry"], a["grid_size"], a["kernels"]
        )
    if a != expected:
        raise ValueError("Applied boundary renderer/source changed")
    return capture(profile, root, a, KernelBank(root, a["kernels"]), scene, out, tile, progress)
