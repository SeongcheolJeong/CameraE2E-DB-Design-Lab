"One frozen synthetic camera: spectral DC -> one effective PSF -> readout -> ISP."

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import numpy as np
from PIL import Image

from . import calibration_db_color_engine as color
from . import calibration_db_core_engine as core
from . import calibration_db_holdout as holdout
from . import calibration_lab_raw as observer
from . import reference_isp, virtual_lab_metrics
from .registry import digest

FIELDS = core.FIELDS
ROLES = ("observation", "replicate", "before", "after")
SCOPE = [
    (
        "Synthetic composition of separately"
        " calibrated spectral, electrical an"
        "d spatial models; not physical came"
        "ra calibration."
    ),
    ("Same frozen DB source/candidate/condition; D65 450-650 nm, gain and temperature retained."),
    (
        "Physical-parameter holdout retains "
        "two independent local virtual probe"
        "s; numerical qualification addition"
        "ally uses sensor-coordinate crops w"
        "hen geometry is bound."
    ),
    (
        "One normalized shared-RGB effective"
        " PSF per field, condition"
        "al on the original D65 Gr character"
        "ization; no spectral PSF "
        "recovery."
    ),
    (
        "Original DB spectral DC throughput "
        "and pixel area retained once; QE is"
        " an absolute replacement, not a sec"
        "ond multiplier."
    ),
    (
        "No original spatial convolution bef"
        "ore applying the effectiv"
        "e PSF; no second receiving aperture."
    ),
    ("Dark/PTC use controlled photoelectron injection, bypassing QE/PSF to isolate electronics."),
    (
        "QE check uses separately authored m"
        "onochromatic sensor-surfa"
        "ce photon doses, not Macbeth-derived QE."
    ),
    ("New DN -> electrons uses Dark/PTC DN/e. Origin spectral-scan DN/e remains provenance only."),
    (
        "CCM WB/reference electrons/exposure"
        " scale are folded into on"
        "e electron-to-output matrix; no second WB/CCM."
    ),
    (
        "Common signed camera-linear output "
        "space; previews clip/gamm"
        "a only for display, no CIE Delta E claim."
    ),
    (
        "Before ISP uses imported baseline C"
        "CM in the same output spa"
        "ce, not the original DB sRGB transform."
    ),
    (
        "No refit or candidate selection on "
        "these new observations; i"
        "mmutable parent profiles are reused."
    ),
    ("Full well, bit depth, FPN policy and other unestimated parameters remain fixed."),
]


def source_response(prepared):
    solution = {"result": prepared["optical"], "arrays": prepared["arrays"]}
    responses, gains = {}, {}
    for fid in FIELDS:
        field = next(f for f in prepared["optical"]["fields"] if f["id"] == fid)
        ri = field.get("pupil_node_index", [0.0, 0.5, 0.85].index(field["normalized_radius"]))
        response = np.asarray(
            [
                color.optics._pupil_effective(
                    solution,
                    fid,
                    wi,
                    wave,
                    lambda theta, phi, wavelength: color.sensor.absolute_angular_response(
                        prepared["pixel"], theta, phi, wavelength
                    ),
                )
                for wi, wave in enumerate(color.WAVE)
            ]
        )
        gains[fid] = (
            np.asarray(prepared["arrays"]["irradiance_per_radiance_sr"])[:, ri]
            * 10
            * (prepared["projection"]["pixel_pitch_um"] * 1e-6) ** 2
        )
        responses[fid] = response
    return responses, gains


def compose(
    prepared,
    color_model,
    core_model,
    core_result,
    color_inputs,
    target_camera_id="synthetic-db-unified",
    workspace=None,
):
    if set(core_model["modules"]) != {"electronics", "optics"}:
        raise ValueError("Unified camera requires both electrical and optical parent calibration")
    if not core_result["validation"]["numerical_tolerance_pass"]:
        raise ValueError("Core parent did not pass its declared numerical validation")
    baselines, _ = core.profiles(prepared)
    if digest(baselines) != digest(core_result["baseline_profiles"]):
        raise ValueError("Core baseline no longer matches the same DB projection")
    candidates = deepcopy(core_model["profiles"])
    if candidates["center"]["electronics"] != candidates["outer-ne"]["electronics"]:
        raise ValueError("Unified v1 requires one shared calibrated readout")
    if core_result["protocol"].get("generator") != "db_local_core_v1":
        raise ValueError("No known authored core reference generator")
    color_record = color_model["calibration_record_snapshot"]
    applied = color.binding(prepared, color_record)
    from .calibration_db_color import _same_application

    _same_application(color_model["resolved"], applied)
    task = color_inputs
    if holdout.plan(task, 72519)["status"] != "planned":
        raise ValueError("No known authored spectral reference generator")
    true = holdout.authored_reference(color.WAVE)
    response, gains = source_response(prepared)
    # Gr/Gb equality is an explicit v1 contract for replacement profiles.
    q = np.asarray(applied["applied_response"]["qe_rggb"])
    t = np.asarray(true["qe_rgb"])[:, [0, 1, 1, 2]]
    truth_fields = core.authored_truth(baselines, ["electronics", "optics"])
    matrix = np.diag(
        np.asarray(true["white_balance"]) * true["exposure_scale"] / true["reference_white_e"]
    ) @ np.asarray(true["matrix"])
    common = dict(
        schema="camera_unified_calibration_profile_v1",
        camera_id=target_camera_id,
        parent_calibration_metadata={
            "qe": color_model["resolved"]["qe_profile"],
            "ccm": color_model["resolved"]["ccm_profile"],
        },
        source_kind="synthetic",
        physical_validation="not_established",
        source=prepared["source_identity"],
        conditions={
            "gain": prepared["spec"]["gain"],
            "temperature_c": 25.0,
            "exposure_ms": prepared["spec"]["exposure_ms"],
            "luminance_cd_m2": prepared["spec"]["luminance_cd_m2"],
            "illuminant": "D65",
            "wavelength_nm": color.WAVE.tolist(),
        },
        cfa="RGGB",
        pixel_pitch_um=prepared["projection"]["pixel_pitch_um"],
        scope=SCOPE,
        parents={
            "color_model_id": color_model["id"],
            "color_profile_sha256": color_model["profile_sha256"],
            "core_model_id": core_model["id"],
            "core_profile_sha256": core_model["profile_sha256"],
        },
        spectral_dc_gain={k: v.tolist() for k, v in gains.items()},
        isp_tuning=deepcopy(prepared["spec"]["isp"]),
    )
    common["isp_tuning"].update(
        wb=[1.0, 1.0, 1.0], ccm=np.eye(3).tolist(), color_calibration_id=None
    )
    bundle = {}
    for role, fields, qe, transform in (
        (
            "before",
            baselines,
            response,
            applied["isp_adapter"][("baseline_electron_to_output_matrix")],
        ),
        (
            "after",
            candidates,
            {fid: q for fid in FIELDS},
            applied["isp_adapter"]["electron_to_output_matrix"],
        ),
        ("truth", truth_fields, {fid: t for fid in FIELDS}, matrix),
    ):
        p = deepcopy(common)
        p.update(
            electronics=deepcopy(fields["center"]["electronics"]),
            fields=deepcopy(fields),
            spectral_response={fid: np.asarray(qe[fid]).tolist() for fid in FIELDS},
            isp={
                "electron_to_output_matrix": np.asarray(transform).tolist(),
                "matrix_convention": "row_vector",
                "target_space": "camera_linear_rgb_pre_clipping",
                "normalization": "fixed_reference_electrons",
                "origin_scan_dn_per_e": color_model["resolved"]["provenance"][
                    "origin_scan_dn_per_e"
                ],
                "dn_to_electrons_parameter": "electronics.dn_per_e",
                "wb_ccm_application_count": 1,
            },
        )
        origin = applied["isp_adapter"]
        p["isp"].update(
            reference_white_e=origin["reference_white_e"],
            white_balance=origin["white_balance"],
            exposure_scale=origin["exposure_scale"],
            matrix=origin["baseline_matrix"] if role == "before" else origin["matrix"],
            post_matrix_white_balance=[1.0, 1.0, 1.0],
            post_matrix_ccm=np.eye(3).tolist(),
        )
        if role == "truth":
            p["isp"].update(
                reference_white_e=true["reference_white_e"],
                white_balance=true["white_balance"],
                exposure_scale=true["exposure_scale"],
                matrix=true["matrix"],
            )
        p["profile_sha256"] = digest(p)
        bundle[role] = p
    if workspace is not None:
        from .calibration_direct_optics import bind

        bundle["after"] = bind(bundle["after"], prepared, workspace)
        from . import calibration_anchor_refinement as refinement
        from . import calibration_anchor_source as anchor_source

        after = bundle["after"]
        after["psf_anchor_geometry"] = refinement.bind(
            anchor_source.bind(after["sensor_crops"]), after["sensor_crops"]
        )
        after.pop("profile_sha256", None)
        after["profile_sha256"] = digest(after)
    return bundle


def tasks():
    return [
        *core.specs(["electronics", "optics"]),
        *[
            {"id": fid + "-macbeth", "chart": "macbeth", "field": fid, "orientation": 0}
            for fid in FIELDS
        ],
        {"id": "qe-scan", "chart": "qe_scan", "field": "center", "orientation": 0},
    ]


def seed_for(seed, role, task, level, frame):
    return (
        seed
        + ROLES.index(role) * 100000
        + [t["id"] for t in tasks()].index(task["id"]) * 1000
        + level * 32
        + frame
    )


def doses(profile, prepared, task):
    chart, fid = task["chart"], task["field"]
    exposure = prepared["spec"]["exposure_ms"] / 1000
    if chart == "dark":
        x = np.asarray([0.001, 0.01, 0.04, 0.1, 0.3, 0.8, 2.5]) * 1.113
        return x, [np.zeros((64, 64)) for _ in x], x, 32, "s"
    if chart == "ptc":
        x = np.asarray([8.0, 24.0, 60.0, 140.0, 300.0, 600.0, 1000.0]) * 1.113
        return (
            x,
            [np.full((64, 64), v) for v in x],
            [exposure] * len(x),
            32,
            "authored photoelectrons/pixel",
        )
    qe = np.asarray(profile["spectral_response"][fid])
    gain = np.asarray(profile["spectral_dc_gain"][fid])
    if chart == "qe_scan":
        # Sensor-surface photons; bypass lens and spatial response for monochromator validation.
        planes = [np.broadcast_to(1200 * row, (64, 64, 4)) for row in qe]
        return color.WAVE, [remosaic(p) for p in planes], [exposure] * len(planes), 16, "nm"
    if chart in ("mtf", "macbeth") and "psf_numerics" in profile:
        from .calibration_psf_convergence import image_charge

        return (
            np.array([0.0]),
            [image_charge(profile, prepared, task)],
            [exposure],
            16,
            ("Macbeth patch" if chart == "macbeth" else "cycles/native pixel"),
        )
    response = gain[:, None] * qe * exposure
    # Separate Gr/Gb are retained for baseline DC; shared effective PSF for all sites.
    rgb_response = response[:, [0, 1, 3]].copy()
    rgb_response[:, 1] = response[:, 1:3].mean(axis=1)
    if chart == "macbeth":
        patterns = color.charts._patterns("color", (512, 512))
        patch = np.asarray(prepared["spectral"]["patches"]) @ rgb_response
        scene = patch[patterns]
    else:
        scene, _ = core.lab._scene(
            profile["fields"][fid],
            {"chart": "mtf", "observations": {"orientation_deg": task["orientation"]}},
            (128, 128),
        )
        scene = scene * (np.asarray(prepared["spectral"]["white"]) @ rgb_response)
    planes, _ = core.lab._effective_blur(scene, profile["fields"][fid]["blur"]["field_nodes"][0])
    return (
        np.array([0.0]),
        [remosaic(planes)],
        [exposure],
        16,
        "Macbeth patch" if chart == "macbeth" else "cycles/native pixel",
    )


def remosaic(planes):
    rr, cc = np.indices(planes.shape[:2])
    site = (np.array([[0, 1], [2, 3]]) if planes.shape[-1] == 4 else np.array([[0, 1], [1, 2]]))[
        rr % 2, cc % 2
    ]
    return np.take_along_axis(planes, site[..., None], axis=2)[..., 0]


def channel_means(raw):
    return np.asarray(
        [
            [
                raw[..., ::2, ::2].mean(),
                np.mean([raw[..., ::2, 1::2].mean(), raw[..., 1::2, ::2].mean()]),
                raw[..., 1::2, 1::2].mean(),
            ]
        ]
    )


def acquire(profile, prepared, task, role, seed, root):
    folder = root / "acquisitions" / task["id"] / role
    folder.mkdir(parents=True, exist_ok=False)
    x, charges, exposures, repeats, xunit = doses(profile, prepared, task)
    fid, chart = task["field"], task["chart"]
    p = profile["fields"][fid]
    e = profile["electronics"]
    readout = {
        k: e[k]
        for k in (
            "full_well_e",
            "read_noise_e",
            "dark_current_e_per_s",
            "bit_depth",
            "black_level_dn",
            "dn_per_e",
        )
    }
    readout["gain"] = prepared["spec"]["gain"]
    model = core.lab._electrical_model(p)
    raw = []
    linears = []
    seeds = []
    for li, (charge, t) in enumerate(zip(charges, exposures, strict=True)):
        frames = []
        linear = []
        for ri in range(repeats):
            stream = seed_for(seed, role, task, li, ri)
            seeds.append(stream)
            frame = (
                holdout.reference_raw(charge / t, readout, t, stream)
                if role in ("observation", "replicate")
                else core.lab._capture_electrical(p, model, {}, charge, t, stream)["arrays"][
                    "raw_dn"
                ]
            )
            frame = np.asarray(frame, dtype=np.uint16)
            frames.append(frame)
            linear.append(
                reference_isp.process(
                    frame,
                    readout,
                    profile["isp"]["electron_to_output_matrix"],
                    profile["isp_tuning"],
                )[0]
            )
        raw.append(frames)
        linears.append(np.mean(linear, axis=0))
    raw = np.asarray(raw, dtype=np.uint16)
    linears = np.asarray(linears)
    means = raw.astype(float).mean(axis=1)
    np.save(folder / "raw.npy", raw, allow_pickle=False)
    np.save(folder / "linear.npy", linears, allow_pickle=False)
    meta = core.metadata(p, task, "observation", seed, prepared["spec"]["exposure_ms"] / 1000)
    meta.update(
        id=role + "-" + task["id"],
        session_id=f"unified-{seed}-{role}",
        acquisition_id=f"unified-{seed}-{role}-{task['id']}",
    )
    if chart == "dark":
        meta["analysis"]["exposure_s"] = x.tolist()
    if chart in ("dark", "ptc", "mtf"):
        observations, quality, _ = observer.analyze_npy(meta, (folder / "raw.npy").read_bytes())
        if quality["not_fit_reasons"]:
            raise ValueError(
                "Unified RAW observer rejected "
                + task["id"]
                + ": "
                + str(quality["not_fit_reasons"])
            )
        metrics = (
            {
                "raw_mean_dn": observations["mean_dn"],
                "raw_variance_dn2": observations["variance_dn2"],
                "isp_mean_rgb": [v[4:-4, 4:-4].mean(axis=(0, 1)).tolist() for v in linears],
            }
            if chart != "mtf"
            else {}
        )
        if chart == "mtf":
            linear = linears[-1][..., 1]
            linear = linear.T if task["orientation"] == 90 else linear
            sfr = virtual_lab_metrics.slanted_edge_metrics(linear)
            if sfr["status"] != "ok":
                raise ValueError("Unified ISP SFR unavailable")
            metrics = {
                "raw_sfr": {"x": observations["frequency_cy_px"], "y": observations["mtf"]},
                "isp_sfr": {"x": sfr["frequency"]["cycles_per_pixel"], "y": sfr["sfr"]},
            }
    elif chart == "macbeth":
        observations = {}
        quality = {}
        metrics = {
            "raw_patch_rgb_dn": color._raw_metrics(raw[0], 4095)["patch_rgb_dn"],
            "isp_patch_rgb": color._patches(linears[0]),
        }
        x = np.arange(1, 25)
    else:
        observations = {}
        quality = {}
        dn = np.concatenate([channel_means(level) for level in raw], axis=0)
        qe = (
            (
                dn
                - readout["black_level_dn"]
                - np.asarray(exposures)[:, None]
                * readout["dark_current_e_per_s"]
                * readout["dn_per_e"]
            )
            / readout["dn_per_e"]
            / 1200
        )
        metrics = {"raw_scan_dn": dn.tolist(), "qe_estimate": qe.tolist()}
    measurement = {
        **meta,
        "observations": observations,
        "quality": quality,
        "source_kind": "synthetic",
        "readout": readout,
        "profile_sha256": profile["profile_sha256"],
    }
    core.save(folder / "measurement.json", measurement)
    # All roles share this fixed RAW display scale; no per-image stretch.
    span = 128.0 if chart == "dark" else 1024.0
    Image.fromarray(np.rint(np.clip(means[-1] / span, 0, 1) * 255).astype(np.uint8)).save(
        folder / "raw.png"
    )
    Image.fromarray(np.rint(np.clip(linears[-1], 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)).save(
        folder / "isp.png"
    )
    return {
        "measurement": measurement,
        "metrics": metrics,
        "raw": str((folder / "raw.npy").relative_to(root)),
        "linear": str((folder / "linear.npy").relative_to(root)),
        "raw_sha256": core.checksum((folder / "raw.npy").read_bytes()),
        "seeds": seeds,
        "frames": len(seeds),
        "profile_sha256": profile["profile_sha256"],
        "images": {k: str((folder / (k + ".png")).relative_to(root)) for k in ("raw", "isp")},
        "x": x.tolist(),
        "x_unit": xunit,
        "raw_display_range_dn": [0.0, span],
    }


def execute(prepared, profiles, output, seed, excluded_seeds, progress=None):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    specs = tasks()
    streams = [
        seed_for(seed, r, t, level, n)
        for r in ROLES
        for t in specs
        for level in range(
            21 if t["chart"] == "qe_scan" else 7 if t["chart"] in ("dark", "ptc") else 1
        )
        for n in range(32 if t["chart"] in ("dark", "ptc") else 16)
    ]
    if len(streams) != len(set(streams)) or set(streams) & set(excluded_seeds):
        raise ValueError("Unified acquisitions overlap parent data or each other")
    from . import calibration_psf_convergence as convergence

    # Physical parameters remain frozen. Numerical selection precedes all holdout data.
    profiles = deepcopy(profiles)
    direct_result = None
    spatial_result = None
    anchor_result = None
    adaptive_result = None
    if profiles["after"].get("sensor_crops", {}).get("psf_source") == (
        "direct_prescription_reference_sphere_scalar_diffraction"
    ):
        from . import calibration_direct_convergence as direct_convergence
        from . import calibration_direct_optics as direct_optics

        g = profiles["after"]["sensor_crops"]
        source = direct_optics.retain(g, prepared["direct_workspace"], root, progress)
        direct_result = direct_convergence.qualify(profiles["after"], source, root, progress)
        profiles["after"] = direct_convergence.applied_profile(profiles["after"], direct_result)
    numerical = convergence.qualify(profiles["after"], prepared, root, progress)
    if numerical["selected"] is not None:
        profiles["after"] = convergence.with_numerics(
            profiles["after"], numerical["selected"]["setting"]
        )
    if direct_result is not None and direct_result["status"] == "passed":
        if "psf_anchor_geometry" in profiles["after"]:
            from . import calibration_anchor_density as anchors

            anchor_result = anchors.qualify(
                profiles["after"], root, prepared["direct_workspace"], progress
            )
            profiles["after"] = anchors.apply(profiles["after"], anchor_result)
            if anchor_result["status"] == "passed":
                from . import calibration_adaptive_anchors as adaptive

                adaptive_result = adaptive.qualify(
                    profiles["after"], root, anchor_result, prepared["direct_workspace"], progress
                )
                profiles["after"] = adaptive.apply(profiles["after"], adaptive_result)
        else:
            from . import calibration_spatial_psf as spatial

            spatial_result = spatial.qualify(profiles["after"], direct_result, root, progress)
            profiles["after"] = spatial.apply(profiles["after"], spatial_result)
    # Independent synthetic observations use the finer, wider reference renderer.
    profiles["truth"] = convergence.with_numerics(profiles["truth"], convergence.setting(16, 8))
    candidate = digest(profiles["after"])
    thresholds = {
        "dark.raw_mean_dn": 0.5,
        "ptc.raw_mean_dn": 2.0,
        "ptc.raw_variance_dn2": 5.0,
        "mtf.raw_sfr": 0.08,
        "mtf.isp_sfr": 0.08,
        "macbeth.raw_patch_rgb_dn": 3.0,
        "macbeth.isp_patch_rgb": 0.003,
        "qe_scan.raw_scan_dn": 3.0,
        "qe_scan.qe_estimate": 0.015,
    }
    protocol = {
        "schema": "camera_unified_holdout_protocol_v1",
        "seed": seed,
        "roles": list(ROLES),
        "configuration": prepared["spec"],
        "thresholds": thresholds,
        "candidate_frozen_before_holdout": True,
        "psf_convergence_policy": convergence.POLICY,
        "numerical_selection_before_holdout": True,
        "holdout_refit": False,
        "independent_streams": len(streams),
        "parent_streams_excluded": sorted(excluded_seeds),
        "scope": SCOPE,
    }
    core.save(root / "protocol.json", protocol)
    for role, p in profiles.items():
        core.save(root / (role + "-camera-profile.json"), p)
    # Standard PSF evidence renderer consumes these two-field local profiles.
    core.save(root / "authored-truth.json", profiles["truth"]["fields"])
    core.save(root / "candidate-profiles.json", profiles["after"]["fields"])
    comparisons = []
    checks = []
    for task in specs:
        series = {}
        for role in ROLES:
            if progress:
                progress("통합 카메라 새 취득 · " + task["id"] + " · " + role)
            series[role] = acquire(
                profiles["truth" if role in ("observation", "replicate") else role],
                prepared,
                task,
                role,
                seed,
                root,
            )
        metrics = {
            key: (
                core.sfr_comparison(series, key)
                if task["chart"] == "mtf"
                else core.compare(series, key)
            )
            for key in series["observation"]["metrics"]
        }
        for key, m in metrics.items():
            name = task["chart"] + "." + key
            if name in thresholds:
                checks.append(
                    {
                        "chart": task["id"],
                        "metric": key,
                        "after_rmse": m["after"]["rmse"],
                        "threshold": thresholds[name],
                        "passed": m["after"]["rmse"] <= thresholds[name],
                    }
                )
        comparisons.append({**task, "series": series, "metrics": metrics})
    if digest(profiles["after"]) != candidate:
        raise ValueError("Unified candidate mutated during holdout")
    # Isolate CCM on exactly the same integer RAW, readout and demosaic.
    ccm = []
    for c in comparisons:
        if c["chart"] != "macbeth":
            continue
        data = np.load(root / c["series"]["after"]["raw"], allow_pickle=False)[0]
        p = profiles["after"]
        readout = c["series"]["after"]["measurement"]["readout"]
        linear = np.mean(
            [
                reference_isp.process(
                    raw,
                    readout,
                    profiles["before"]["isp"]["electron_to_output_matrix"],
                    p["isp_tuning"],
                )[0]
                for raw in data
            ],
            axis=0,
        )
        name = "acquisitions/" + c["id"] + "/after/isp-before-ccm.npy"
        np.save(root / name, linear, allow_pickle=False)
        error = holdout.errors(
            color._patches(linear), c["series"]["observation"]["metrics"]["isp_patch_rgb"]
        )
        ccm.append(
            {
                "field_id": c["field"],
                "raw": c["series"]["after"]["raw"],
                "raw_sha256": c["series"]["after"]["raw_sha256"],
                "before_ccm_linear": name,
                "same_raw": True,
                "before_ccm_isp_rmse": error["rmse"],
                "after_ccm_isp_rmse": c["metrics"]["isp_patch_rgb"]["after"]["rmse"],
            }
        )
    return {
        "schema": "camera_unified_calibration_result_v1",
        "status": "synthetic_holdout_completed",
        "protocol": protocol,
        "scope": SCOPE,
        "modules": ["electronics", "optics", "spectral", "isp"],
        "comparisons": comparisons,
        "baseline_profiles": profiles["before"]["fields"],
        "candidate_profiles": profiles["after"]["fields"],
        "baseline_camera_profile": profiles["before"],
        "camera_profile": profiles["after"],
        "profile_sha256": profiles["after"]["profile_sha256"],
        "candidate_sha256": digest(profiles["after"]["fields"]),
        "parameter_recovery": core.recover(
            profiles["before"]["fields"], profiles["after"]["fields"], profiles["truth"]["fields"]
        ),
        "ccm_same_raw": ccm,
        "psf_convergence": numerical,
        "direct_psf_convergence": direct_result,
        "spatial_psf": spatial_result,
        "anchor_density": anchor_result,
        "adaptive_anchors": adaptive_result,
        "raw_frames": len(streams),
        "refit_performed": False,
        "validation": {
            "numerical_tolerance_pass": numerical["status"] == "passed"
            and (direct_result is None or direct_result["status"] == "passed")
            and (
                direct_result is None
                or anchor_result is not None
                and anchor_result["status"] == "passed"
                or spatial_result is not None
                and spatial_result["status"] == "passed"
            )
            and all(c["passed"] for c in checks),
            "adaptive_anchor_trial_pass": None
            if adaptive_result is None
            else adaptive_result["status"] == "passed",
            "anchor_density_pass": None
            if anchor_result is None
            else anchor_result["status"] == "passed",
            "spatial_psf_pass": None
            if spatial_result is None
            else spatial_result["status"] == "passed",
            "psf_convergence_pass": numerical["status"] == "passed",
            "direct_psf_convergence_pass": None
            if direct_result is None
            else direct_result["status"] == "passed",
            "checks": checks,
            "source": ("predeclared_synthetic_demo_thresholds"),
            "physical_validation": "not_established",
        },
        "physical_validation": "not_established",
    }
