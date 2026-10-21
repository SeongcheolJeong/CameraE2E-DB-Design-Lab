"""Deterministic error-driven anchor placement; held-out direct fields never steer it."""

from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy.spatial import Delaunay

from . import calibration_anchor_density as density
from . import calibration_direct_convergence as direct
from . import calibration_direct_optics as optics
from . import calibration_spatial_psf as spatial
from .registry import digest

POLICY = "camera_error_driven_anchor_placement_v1"


def topology(geometry, ids):
    """Pin real native coordinates and canonical triangles; never admit holdouts."""
    allowed = {
        c["id"]: c["optical_anchor_active_row_col"]
        for c in geometry["fields"]
        if c["role"] != "validation"
    }
    ids = sorted(ids)
    initial = set(density.topology(geometry, 3)[0])
    if len(ids) != len(set(ids)) or not initial.issubset(ids) or not set(ids).issubset(allowed):
        raise ValueError(
            "Adaptive anchors must preserve seed coverage and exclude validation fields"
        )
    anchors = {fid: allowed[fid] for fid in ids}
    triangles = sorted(
        [
            sorted(ids[i] for i in row)
            for row in Delaunay(np.array(list(anchors.values()))).simplices
        ]
    )
    return anchors, triangles


def application(profile, geometry, ids, kernels):
    a = density.application(profile, geometry, 5, kernels)
    anchors, triangles = topology(geometry, ids)
    a.update(
        grid_size="adaptive",
        anchors=anchors,
        triangles=triangles,
        kernels=[k for k in kernels if k["field"] in anchors],
        adaptive_policy=POLICY,
        adaptive_renderer_sha256=optics.sha(__file__),
        selected_anchor_ids=sorted(ids),
    )
    return a


def probe(profile, a, bank, field, references, memo, *, force_mtf=False):
    fid = field["id"]
    position = field["optical_anchor_active_row_col"]
    # A topology change away from this receiver cannot alter this local probe.
    point = np.array(position)
    for tri in a["triangles"]:
        p = np.array([a["anchors"][v] for v in tri])
        uv = np.linalg.solve(np.stack([p[0] - p[2], p[1] - p[2]], axis=1), point - p[2])
        if np.r_[uv, 1 - uv.sum()].min() >= -1e-10:
            break
    else:
        raise ValueError("Adaptive probe lies outside sensor coverage")
    key = (fid, tuple(tri), force_mtf)
    if key not in memo:
        kernel = density.mixture(a, bank, *position)
        ref = bank[fid]
        ref = ref / ref.sum(axis=(0, 1), dtype=float)
        l1 = np.abs(kernel - ref).sum(axis=(0, 1)).tolist()
        # Reject known PSF failures before expensive RAW/ISP captures. They can never pass
        # the conjunction; untested MTF is explicitly absent, never recorded as passed.
        if not force_mtf and max(l1) > density.LIMITS["psf_l1"]:
            memo[key] = dict(
                field=fid,
                position=position,
                psf_l1_rgb=l1,
                score=max(l1) / density.LIMITS["psf_l1"],
                passed=False,
                comparison=dict(passed=False, checks=[], reason="psf_l1_failed_mtf_not_evaluated"),
            )
            return deepcopy(memo[key])
        if fid not in references:
            references[fid] = density.stationary(profile, ref, field)
        measured = density.stationary(profile, kernel, field)
        comparison = direct.compare({"fields": {fid: measured}}, {"fields": {fid: references[fid]}})
        scores = [max(l1) / density.LIMITS["psf_l1"]]
        for c in comparison["checks"]:
            scores.append(c["max_abs"] / density.LIMITS["mtf_max_abs"])
            if c.get("mtf50_relative") is not None:
                scores.append(c["mtf50_relative"] / density.LIMITS["mtf50_relative"])
        # compare() is the authority even when a curve has no in-band MTF50.
        score = max(scores)
        passed = max(l1) <= density.LIMITS["psf_l1"] and comparison["passed"]
        if not passed:
            score = max(score, 1.000000000001)
        memo[key] = dict(
            field=fid,
            position=position,
            psf_l1_rgb=l1,
            score=score,
            passed=passed,
            comparison=comparison,
            measured=measured,
            reference=references[fid],
        )
    return deepcopy(memo[key])


def select(geometry, evaluate, make_application):
    """Worst normalized failing score first; every remaining selection probe is retained."""
    ids = list(density.topology(geometry, 3)[0])
    pool = [c for c in geometry["fields"] if c["role"] != "validation"]
    history = []
    while True:
        a = make_application(ids)
        checks = [evaluate(a, f) for f in pool if f["id"] not in ids]
        failed = [c for c in checks if not c["passed"]]
        worst = max(failed, key=lambda c: (c["score"], c["field"])) if failed else None
        history.append(
            dict(
                anchor_count=len(ids),
                selected_ids=sorted(ids),
                added_field=None if worst is None else worst["field"],
                checks=[
                    {k: v for k, v in c.items() if k not in ("measured", "reference")}
                    for c in checks
                ],
                passed=not failed,
            )
        )
        if worst is None:
            return a, history
        ids.append(worst["field"])


def qualify(profile, root, baseline, workspace, progress=None):
    root = Path(root)
    folder = root / "adaptive-anchors"
    folder.mkdir(exist_ok=False)
    geometry = baseline["geometry"]
    kernels = baseline["kernels"]
    bank = density.KernelBank(root, kernels)
    references, memo = {}, {}
    started = time.monotonic()

    def make(ids):
        return application(profile, geometry, ids, kernels)

    def evaluate(a, field):
        if progress:
            progress(f"적응형 배치 · {len(a['anchors'])}점 · {field['id']}")
        return probe(profile, a, bank, field, references, memo)

    a, history = select(geometry, evaluate, make)
    selection_seconds = time.monotonic() - started
    # Freeze placement BEFORE opening disjoint held-out responses.
    frozen = digest(a)
    holdout = [
        probe(profile, a, bank, f, references, memo, force_mtf=True)
        for f in geometry["fields"]
        if f["role"] == "validation"
    ]
    assert frozen == digest(a)
    baseline_holdout = next(c for c in baseline["candidates"] if c["grid_size"] == "refined")[
        "checks"
    ]
    captures, comparisons, parity = [], [], []
    for old in baseline["captures"]:
        if old["grid_size"] != "refined":
            continue
        angle, phase = old["scene"]["orientation_deg"], old["scene"]["phase_native_px"]
        scene = density.BoundaryAtlas(profile["sensor_crops"], angle, phase)
        f = density.capture(
            profile,
            root,
            a,
            bank,
            scene,
            folder / old["chart"],
            progress=progress,
            cache_workspace=workspace,
        )
        f.update(
            id="adaptive-" + old["chart"],
            grid_size="adaptive",
            chart=old["chart"],
            scene=scene.specification(),
        )
        captures.append(f)
        raw, lin = (np.load(root / f[k], mmap_mode="r") for k in ("raw", "linear"))
        rr, ll = (np.load(root / old[k], mmap_mode="r") for k in ("raw", "linear"))
        for crop in scene.crops:
            y, x = crop["origin_row_col"]
            metrics = spatial.metrics(
                raw[y : y + 128, x : x + 128],
                lin[y : y + 128, x : x + 128],
                spatial.metadata(profile, crop, angle),
                angle,
                rr[y : y + 128, x : x + 128],
                ll[y : y + 128, x : x + 128],
            )
            comparisons.append(
                dict(field=crop["id"], chart=old["chart"], origin_row_col=[y, x], **metrics)
            )
        parity.extend(density.window_checks(profile, root, a, bank, scene, f, progress))
    alternate = density.capture(
        profile,
        root,
        a,
        bank,
        density.BoundaryAtlas(profile["sensor_crops"], 0, 0.0),
        folder / "partition-96",
        tile=96,
        progress=progress,
        cache_workspace=workspace,
    )
    rd = int(
        np.max(
            abs(
                np.load(root / captures[0]["raw"]).astype(int)
                - np.load(root / alternate["raw"]).astype(int)
            )
        )
    )
    ld = float(
        np.max(abs(np.load(root / captures[0]["linear"]) - np.load(root / alternate["linear"])))
    )
    partition = dict(
        raw_max_abs_dn=rd,
        isp_max_abs=ld,
        passed=rd == 0 and ld <= 1e-12,
        active_pixels_compared=int(np.prod(profile["sensor_crops"]["full_sensor_shape"])),
        alternate=alternate,
    )
    passed = (
        all(c["passed"] for c in holdout)
        and all(v["passed"] for c in comparisons for v in c["mtf"].values())
        and all(c["passed"] for c in parity)
        and partition["passed"]
    )
    use_adaptive = passed and len(a["anchors"]) < 41
    selected = a if use_adaptive else baseline["application"]
    result = dict(
        schema=POLICY,
        status="passed" if passed else "not_converged",
        selected_mode=(
            "adaptive"
            if use_adaptive
            else "fixed_41_no_cost_reduction"
            if passed
            else "fixed_41_fallback"
        ),
        application=selected,
        adaptive_application=a,
        geometry=geometry,
        placement_sha256=frozen,
        history=history,
        heldout_checks=holdout,
        baseline_heldout_checks=baseline_holdout,
        limits=density.LIMITS,
        adaptive_anchor_count=len(a["anchors"]),
        selected_anchor_count=len(selected["anchors"]),
        baseline_anchor_count=41,
        captures=captures,
        full_sensor_comparisons=comparisons,
        independent_window_checks=parity,
        partition_check=partition,
        selection_seconds=selection_seconds,
        unique_probe_evaluations=len(memo),
        cost=dict(
            adaptive=density.estimate(a),
            baseline=density.estimate(baseline["application"]),
            adaptive_original_render_seconds=sum(f["elapsed_s"] for f in captures),
            baseline_original_render_seconds=sum(
                f["elapsed_s"] for f in baseline["captures"] if f["grid_size"] == "refined"
            ),
            runtime_kernel_bytes=len(a["anchors"]) * 1025**2 * 3 * 4,
            baseline_kernel_bytes=41 * 1025**2 * 3 * 4,
            source_pool_direct_fields=57,
            selection_pool_fields=41,
            heldout_fields=16,
        ),
        gate_policy=(
            "PSF L1 failure short-circuits MTF; full optical RAW ISP checks "
            "required for every passing remaining selection point and all heldouts"
        ),
        selection_scope="finite 41-point direct pool; greedy insertion, no global optimality claim",
        validation_scope=(
            "16 disjoint direct points never used for placement; "
            "full sensor compared to fixed41, not continuous truth"
        ),
        physical_validation="not_established",
    )
    optics.save(folder / "result.json", result)
    return result


def apply(profile, result):
    # A failed adaptive trial falls back to the already qualified immutable 41-point baseline.
    p = density.apply(profile, dict(application=result["application"], status="passed"))
    p["adaptive_anchor_selection"] = dict(
        policy=POLICY,
        mode=result["selected_mode"],
        candidate_anchor_count=result["adaptive_anchor_count"],
        applied_anchor_count=result["selected_anchor_count"],
        placement_sha256=result["placement_sha256"],
        trial_status=result["status"],
        independent_validation_fields=16,
    )
    p.pop("profile_sha256", None)
    p["profile_sha256"] = digest(p)
    return p
