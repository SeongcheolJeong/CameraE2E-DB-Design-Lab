# ruff: noqa: E501
"""Reference Ghost Lab jobs; existing workshop storage/lifecycle and integrity."""

from __future__ import annotations

import copy
import html
import json
import time
from pathlib import Path

import numpy as np

from . import design_workshop_storage as storage
from . import reference_workshop_job as workshop
from .design_workshop_engine import write_json
from .reference_sensor_job import _json_value
from .reference_sensor_job import _load_source as _sensor_source
from .registry import IntegrityError, canonical, digest
from .virtual_lab_job import sha256

LEGACY_SCHEMA = "camera_reference_ghost_study_v1"
SCHEMA = "camera_reference_ghost_study_v2"
NEW_CHARTS = {"black", "dark_contrast"}
PROBE_HALF_MM = 256 * 0.0029 / 2
ROUTE = workshop.ROUTE + "/ghost-studies"
LIMITATIONS = [
    "Two specular reflections only; no housing/edge scattering, sensor reflection, diffraction or coherent interference.",
    "Pinned patent prescription has reviewed hypothetical clear apertures; manufactured lens coating/housing are unknown.",
    "Background uses field/pupil-averaged spectral transmission with existing geometric PSF; coating apodization is not modeled.",
    "Ghost uses ray-specific sensor angle and spectral QE; angles outside the 0–60 degree pixel table are excluded and reported.",
    "Pupil triangles and receiving-pixel quadrature are numerical approximations; refinement is reported, not physical validation.",
    "Full-field overview is optical irradiance only; RAW/ISP are actual native 256x256 local probes, not a full sensor capture.",
]


def template(version=SCHEMA):
    from .reference_sensor_job import template as sensor_template

    result = {
        "schema": version,
        "name": "Reference Ghost Lab",
        "source": sensor_template()["source"],
        "angles_deg": [0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0],
        "azimuth_deg": 45.0,
        "fields": ["center", "outer-ne"],
        "charts": ["color", "resolution"],
        "coatings": ["fresnel", "r005", "r020"],
        "irradiance_w_m2": [100.0],
        "pupil_samples": 64,
        "frames": 8,
        "seed": 42,
        "isp": {"display_white_level": 1.0, "tone_gamma": 2.2, "lsc_enabled": True},
        "reprocess_of": None,
    }

    if version == SCHEMA:
        result.update(
            probes=[],
            charts=["black", "dark_contrast"],
            angles_deg=[15.0],
            coatings=["r005"],
            irradiance_w_m2=[10.0],
        )
        result["isp"].update(lsc_enabled=False, display_white_level=0.05)
    return result


def _load_source(service, spec, verify=False):
    # New charts are newly acquired, never passed off as preserved source RAW.
    compatible = copy.deepcopy(spec)
    compatible["fields"] = list(spec["fields"] or ["center"])
    if spec.get("probes") and "center" not in compatible["fields"]:
        compatible["fields"].append("center")
    compatible["charts"] = [c for c in spec["charts"] if c not in NEW_CHARTS] or ["uniform"]
    return _sensor_source(service, compatible, verify=verify)


def specification(payload):
    if (
        not isinstance(payload, dict)
        or payload.get("schema") not in (LEGACY_SCHEMA, SCHEMA)
        or set(payload) != set(template(payload["schema"]))
    ):
        raise ValueError("Use explicit Ghost Lab template fields")
    s = copy.deepcopy(payload)
    if not isinstance(s["name"], str) or not 1 <= len(s["name"]) <= 160:
        raise ValueError("Invalid Ghost schema/name")
    from .reference_sensor_job import specification as sensor_spec
    from .reference_sensor_job import template as sensor_template

    compatible = sensor_template()
    compatible.update(
        source=s["source"],
        fields=s["fields"] or ["center"],
        charts=[c for c in s["charts"] if c not in NEW_CHARTS] or ["uniform"],
        seed=s["seed"],
        isp=copy.deepcopy(s["isp"]),
    )
    if s["schema"] == SCHEMA and isinstance(s["isp"], dict) and "display_white_level" in s["isp"]:
        white = s["isp"]["display_white_level"]
        if type(white) not in (float, int) or not np.isfinite(white) or not 0.0001 <= white <= 100:
            raise ValueError("Ghost display white must be finite in 0.0001..100")
        compatible["isp"]["display_white_level"] = 1.0
    sensor_spec(compatible)
    import re

    probes = s.get("probes", [])
    if not isinstance(probes, list) or len(probes) > 4:
        raise ValueError("Use at most four explicit native probes per study")
    ids = set(s["fields"])
    for probe in probes:
        if not isinstance(probe, dict) or set(probe) != {"id", "xy_mm"}:
            raise ValueError("Each probe requires id and sensor-plane xy_mm")
        if (
            not isinstance(probe["id"], str)
            or not re.fullmatch(r"probe-[a-z0-9-]{1,32}", probe["id"])
            or probe["id"] in ids
        ):
            raise ValueError("Probe IDs must be unique probe- identifiers")
        ids.add(probe["id"])
        xy = probe["xy_mm"]
        if (
            not isinstance(xy, list)
            or len(xy) != 2
            or any(
                type(v) not in (float, int) or not np.isfinite(v) or abs(v) > 13.66 - PROBE_HALF_MM
                for v in xy
            )
        ):
            raise ValueError("Native probe must fit inside the sensor-plane map ±13.66 mm")
    if not ids:
        raise ValueError("Select at least one saved field or native probe")
    if probes and s["isp"]["lsc_enabled"]:
        raise ValueError("Custom probes require LSC OFF: no measured full-sensor LSC is available")
    allowed_charts = {"uniform", "color", "resolution", "texture"} | (
        NEW_CHARTS if s["schema"] == SCHEMA else set()
    )
    if (
        not isinstance(s["charts"], list)
        or not s["charts"]
        or len(set(s["charts"])) != len(s["charts"])
        or any(c not in allowed_charts for c in s["charts"])
    ):
        raise ValueError("Select existing Reference chart")
    for key, lo, hi in [("angles_deg", 0, 30), ("irradiance_w_m2", 0, 10000)]:
        a = s[key]
        if (
            not isinstance(a, list)
            or not 1 <= len(a) <= 7
            or any(
                type(v) not in (float, int) or not np.isfinite(v) or not lo <= v <= hi for v in a
            )
            or len(set(a)) != len(a)
        ):
            raise ValueError("Invalid " + key)
    if (
        type(s["azimuth_deg"]) not in (int, float)
        or not np.isfinite(s["azimuth_deg"])
        or not 0 <= s["azimuth_deg"] < 360
    ):
        raise ValueError("Invalid azimuth")
    if type(s["pupil_samples"]) is not int or s["pupil_samples"] not in (32, 64, 128):
        raise ValueError("pupil_samples: 32,64,128; no automatic reduction")
    if type(s["frames"]) is not int or s["frames"] != 8:
        raise ValueError("Reference preserves eight independent captures")
    if (
        not isinstance(s["coatings"], list)
        or not s["coatings"]
        or len(set(s["coatings"])) != len(s["coatings"])
        or any(c not in ("fresnel", "r005", "r020") for c in s["coatings"])
    ):
        raise ValueError("Invalid coating profiles")
    if s["reprocess_of"] is not None:
        import re

        if not isinstance(s["reprocess_of"], str) or not re.fullmatch(
            r"run-[0-9a-f]{32}", s["reprocess_of"]
        ):
            raise ValueError("Invalid replay run")
    count = (
        len(s["angles_deg"])
        * len(s["coatings"])
        * (len(s["fields"]) + len(probes))
        * len(s["charts"])
        * len(s["irradiance_w_m2"])
    )
    if count > 300:
        raise ValueError("Split more than 300 cases into explicit jobs")
    if s["schema"] == SCHEMA:
        # JSON from browsers spells 15.0 as 15. Canonical physical coordinates
        # keep that presentation difference from retracing the same pupil rays.
        def physical_number(value):
            return float(value) if value else 0.0

        for key in ("angles_deg", "irradiance_w_m2"):
            s[key] = [physical_number(v) for v in s[key]]
        s["azimuth_deg"] = physical_number(s["azimuth_deg"])
        for probe in probes:
            probe["xy_mm"] = [physical_number(v) for v in probe["xy_mm"]]
    return s


def _background(
    workspace, source, capture, prescription, ci, field, chart, samples, engine, charts, probes
):
    from . import design_workshop_engine as shared
    from . import reference_workshop_engine as reference
    from . import reference_workshop_optics as optics
    from .virtual_lab_engine import _spectra

    def scene(kind, geometry, spectral):
        if kind in NEW_CHARTS:
            return charts.chart_scene(kind, geometry, spectral)
        photons, _ = shared._scene(kind, geometry, spectral)
        return photons, None

    if field["custom_probe"]:
        rate, metadata = probes.background(
            workspace, source, capture, prescription, ci, field["xy_mm"], chart, samples, scene
        )
        return (
            rate,
            np.asarray(metadata.get("transmission", np.ones(21))),
            metadata.get("chart_definition"),
            metadata,
        )
    if chart not in NEW_CHARTS:
        rate, transmission = engine.background(
            workspace, source, capture, prescription, ci, field["id"], chart, samples
        )
        return rate, transmission, None, {"method": "stored_field_optics", "field_id": field["id"]}
    solution = optics.load_solution(Path(source) / "optics")
    row = next(f for f in solution["result"]["fields"] if f["id"] == field["id"])
    photons, chart_meta = charts.chart_scene(
        chart, row["geometry"], _spectra(workspace, "D65", 1000)
    )
    if chart == "black":
        return np.zeros((256, 256)), np.ones(21), chart_meta, {"method": "zero_radiance_background"}
    transmission = engine.direct_transmission(prescription, row["object_angle_deg"], samples)[:, ci]
    response = json.loads((Path(source) / "pixel-response.json").read_text())
    rate = reference._apply(solution, row, photons * transmission, response)["mean_electron_rate"]
    return (
        rate,
        transmission,
        chart_meta,
        {"method": "stored_field_optics", "field_id": field["id"]},
    )


def fingerprint():
    names = [
        "reference_ghost.py",
        "reference_ghost_engine.py",
        "reference_ghost_job.py",
        "reference_ghost_charts.py",
        "reference_ghost_probe.py",
        "reference_workshop_optics.py",
        "reference_workshop_sensor.py",
        "reference_sensor_modes.py",
        "electrical.py",
    ]
    return {n: sha256(Path(__file__).parent / n) for n in names}


def _replay(service, spec):
    r = get(service, spec["reprocess_of"])
    if r["status"] != "succeeded":
        raise ValueError("Replay requires a complete Ghost study")
    previous = r["ghost_study"]
    if any(spec[k] != previous[k] for k in spec if k not in ("name", "isp", "reprocess_of")):
        raise ValueError("RAW replay may change only display ISP; acquisition is frozen")
    workshop.verify_files(service, r)
    return workshop._directory(service, r["id"])


def submit(service, payload):
    s = specification(payload)
    source, _ = _load_source(service, s)
    if s.get("probes") and any(c != "black" for c in s["charts"]):
        optical = json.loads((source / "optics" / "result.json").read_text())
        supported = max(float(np.linalg.norm(f["image_xy_mm"])) for f in optical["fields"])
        if any(np.linalg.norm(p["xy_mm"]) > supported for p in s["probes"]):
            raise ValueError(
                f"Background optical support ends at radius {supported:.4f} mm. Select black to evaluate ghost-only RAW outside that radius, or move the probe inward."
            )
    if s["reprocess_of"]:
        _replay(service, s)
    count = (len(s["fields"]) + len(s.get("probes", []))) * np.prod(
        [len(s[k]) for k in ("angles_deg", "coatings", "charts", "irradiance_w_m2")]
    )
    estimated = (
        int(count) * (90 if s["schema"] == SCHEMA else 60) * 1024**2
        + len(s["angles_deg"]) * 600 * 1024**2
    )
    state = storage.status(service)
    if (
        state["remaining_bytes"] < estimated
        or state["volume_free_bytes"] - estimated < storage.RESERVE_BYTES
    ):
        raise ValueError("Ghost Lab exceeds SSD budget; no resolution reduction")
    doc = {
        "name": s["name"],
        "job_kind": workshop.JOB_KIND,
        "plugin_id": s["schema"],
        "ghost_study": s,
        "source_code": fingerprint(),
        "storage_root": state["path"],
        "estimated_bytes": estimated,
        "default_worker_timeout_seconds": 14400,
    }
    r = service.registry.submit(doc, digest(doc))
    if not any(v["run_id"] == r["id"] for v in service.registry.list("reference_ghost_studies")):
        service.registry.put("reference_ghost_studies", {"run_id": r["id"]})
    return r


def execute(service, job):
    from . import reference_ghost as core
    from . import reference_ghost_engine as engine
    from . import reference_workshop_optics as optics
    from .reference_sensor_modes import _transfer, process_electrons
    from .reference_workshop_sensor import lsc_gain_map

    s = specification(job["ghost_study"])
    if job["source_code"] != fingerprint() or job["storage_root"] != str(storage.root(service)):
        raise IntegrityError("Ghost code/storage changed after submission")
    start = time.monotonic()
    directory = workshop._directory(service, job["id"])
    with storage.reserve(service, job["estimated_bytes"]) as (project, check):
        directory.mkdir(parents=True, exist_ok=True)

        def progress(message):
            if service.registry.get_run(job["id"])["status"] != "running":
                raise RuntimeError("Ghost study cancelled")
            write_json(directory / "progress.json", {"stage": message})

        source, capture = _load_source(service, s, verify=True)
        previous = _replay(service, s) if s["reprocess_of"] else None
        calibration = json.loads((source / "calibration.json").read_text())
        cell = json.loads((source / "pixel-cell.json").read_text())
        _, _, raw_white_dn = _transfer(cell["electrical_model"], 1.0)
        response = json.loads((source / "pixel-response.json").read_text())
        solution = optics.load_solution(source / "optics")
        prescription = engine.load_prescription(service.workspace)
        fields = [
            {"id": f["id"], "xy_mm": f["image_xy_mm"], "custom_probe": False}
            for f in solution["result"]["fields"]
            if f["id"] in s["fields"]
        ]
        fields.extend(
            {
                **p,
                "custom_probe": True,
                "coordinate_scope": "independent_virtual_native_ROI_RGGB_2.9um",
                "background_scope": "interpolated_stored_radial_optics; no extrapolation",
            }
            for p in s.get("probes", [])
        )
        from . import reference_ghost_charts as charts
        from . import reference_ghost_probe as probes

        write_json(directory / "specification.json", s)
        write_json(directory / "prescription.json", prescription)
        write_json(directory / "calibration.json", calibration)
        write_json(directory / "pixel-cell.json", cell)
        power, _ = engine.spectral_power(service.workspace)
        background = {}
        atlas = []
        cases = []
        new_frames = 0
        for angle in s["angles_deg"]:
            progress(f"Optical cache · {angle:g}°")
            maps = engine.map_angle(
                prescription,
                angle,
                s["azimuth_deg"],
                s["pupil_samples"],
                response,
                power,
                fields,
                project / "reference-ghost-cache",
                progress,
            )
            meta = json.loads((maps / "maps.json").read_text())
            atid = f"angle-{len(atlas):02d}"
            target = directory / atid
            target.mkdir(exist_ok=True)
            overview = np.load(maps / "overview.npy")
            overview.astype("<f4").tofile(target / "overview.bin")
            write_json(target / "maps.json", meta)
            best = sorted(
                enumerate(meta["paths"]),
                key=lambda x: -sum(x[1]["energy_m2"]) if x[1]["pair"] else float("inf"),
            )[:12]
            paths = []
            for bi, row in best:
                with np.load(maps / row["map"]) as saved_layer:
                    data = saved_layer["layer"]
                name = f"path-{bi:03d}.bin"
                data.astype("<f4").tofile(target / name)
                paths.append({**row, "binary": atid + "/" + name})
            atlas.append(
                {
                    "id": atid,
                    "angle_deg": angle,
                    "binary": atid + "/overview.bin",
                    "shape": [2, 512, 512, 3],
                    "bounds_mm": meta["overview_bounds_mm"],
                    "paths": paths,
                    "diagnostics": meta["diagnostics"],
                    "unsupported_angle_weight_diagnostic": meta[
                        "unsupported_angle_weight_diagnostic"
                    ],
                }
            )
            with np.load(maps / "rates.npz") as data:
                rates = {f["id"]: data[f["id"]] for f in fields}
            for coating in s["coatings"]:
                ci = core.COATINGS.index(coating)
                for field in fields:
                    fid = field["id"]
                    f = capture["fields"]["center" if field["custom_probe"] else fid]
                    gain = (
                        lsc_gain_map(calibration["lsc"], fid, shape=(256, 256))
                        if s["isp"]["lsc_enabled"]
                        else None
                    )
                    for chart in s["charts"]:
                        key = (coating, fid, chart)
                        if key not in background:
                            progress(
                                f"Background spectral transmission · {coating} · {fid} · {chart}"
                            )
                            background[key] = _background(
                                service.workspace,
                                source,
                                capture,
                                prescription,
                                ci,
                                field,
                                chart,
                                s["pupil_samples"],
                                engine,
                                charts,
                                probes,
                            )
                        bg, transmission, chart_meta, bg_meta = background[key]
                        for strength in s["irradiance_w_m2"]:
                            caseid = f"case-{len(cases):03d}"
                            out = directory / "cases" / caseid
                            out.mkdir(parents=True, exist_ok=True)
                            progress(
                                f"{caseid} · {angle:g}° · {coating} · {fid} · {chart} · {strength:g} W/m²"
                            )
                            components = rates[fid][:, :, :, ci, :]
                            if s["schema"] == SCHEMA:
                                # Raster maps use ascending physical Y; native image rows
                                # increase downwards, as in the preserved background PSF.
                                components = components[:, ::-1]
                            mosaic = np.zeros((2, 256, 256))
                            for site, (r, c) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
                                mosaic[:, r::2, c::2] = components[:, r::2, c::2, site]
                            np.savez_compressed(
                                out / "mean-rates.npz",
                                background=bg,
                                direct=mosaic[0] * strength,
                                ghost=mosaic[1] * strength,
                                transmission=transmission,
                            )
                            result_modes = []
                            base = None
                            raw_hashes = {}
                            source_off = None
                            modes = [
                                ("direct_only", bg + mosaic[0] * strength),
                                ("with_ghost", bg + mosaic.sum(axis=0) * strength),
                            ]
                            if s["schema"] == SCHEMA:
                                modes.insert(0, ("source_off", bg))
                            for mode, rate in modes:
                                if previous:
                                    old = previous / "cases" / caseid / (mode + ".npz")
                                    with np.load(old) as data:
                                        a = {k: data[k] for k in data.files}
                                    p = [
                                        process_electrons(
                                            e,
                                            calibration["color"]["matrix"],
                                            lsc_gain=gain,
                                            display_white=s["isp"]["display_white_level"],
                                            gamma=s["isp"]["tone_gamma"],
                                            valid=ok,
                                        )
                                        for e, ok in zip(
                                            a["electron_frames"], a["raw_valid_frames"], strict=True
                                        )
                                    ]
                                    a.update(
                                        linear_frames=np.array([v["linear"] for v in p]),
                                        display_frames=np.array([v["display"] for v in p]),
                                        valid_frames=np.array([v["valid"] for v in p]),
                                    )
                                else:
                                    a = engine.acquire(
                                        rate,
                                        f["readout"],
                                        cell["electrical_model"],
                                        calibration,
                                        gain,
                                        s["seed"]
                                        + [row["id"] for row in fields].index(fid) * 10000
                                        + s["charts"].index(chart) * 100,
                                        white=s["isp"]["display_white_level"],
                                        gamma=s["isp"]["tone_gamma"],
                                    )
                                    new_frames += 8
                                np.savez_compressed(
                                    out / (mode + ".npz"),
                                    **{k: v for k, v in a.items() if k != "display_frames"},
                                )
                                engine.png(out / (mode + ".png"), a["display_frames"][0])
                                engine.png(
                                    out / (mode + "-raw.png"), a["raw_frames"][0] / raw_white_dn
                                )
                                engine.png(out / (mode + "-valid.png"), a["valid_frames"][0])
                                engine.png(
                                    out / (mode + "-raw-valid.png"), a["raw_valid_frames"][0]
                                )
                                if mode == "source_off":
                                    source_off = a
                                if mode == "direct_only":
                                    base = a
                                measured = engine.measure(
                                    a, base, chart=chart if chart not in NEW_CHARTS else "uniform"
                                )
                                chart_metrics = (
                                    charts.measure_chart(a, source_off, chart_meta)
                                    if chart_meta
                                    else None
                                )
                                raw_hashes[mode] = (
                                    __import__("hashlib")
                                    .sha256(a["raw_frames"].tobytes())
                                    .hexdigest()
                                )
                                result_modes.append(
                                    {
                                        "id": mode,
                                        "image": f"cases/{caseid}/{mode}.png",
                                        "raw_image": f"cases/{caseid}/{mode}-raw.png",
                                        "raw_valid": f"cases/{caseid}/{mode}-raw-valid.png",
                                        "raw_preview": {
                                            "black_dn": 0,
                                            "white_dn": raw_white_dn,
                                            "gamma": 1,
                                            "normalization": "fixed_ADC_range_includes_pedestal",
                                        },
                                        "valid": f"cases/{caseid}/{mode}-valid.png",
                                        "arrays": f"cases/{caseid}/{mode}.npz",
                                        "metrics": _json_value(measured),
                                        "chart_metrics": _json_value(chart_metrics),
                                    }
                                )
                            case = {
                                "id": caseid,
                                "angle_deg": angle,
                                "coating": coating,
                                "field_id": fid,
                                "chart": chart,
                                "probe_xy_mm": field["xy_mm"],
                                "native_row_axis": "decreasing_sensor_y"
                                if s["schema"] == SCHEMA
                                else "legacy_map_rows",
                                "native_pixel_pitch_um": 2.9,
                                "native_cfa_origin": "independent_virtual_ROI_RGGB",
                                "custom_probe": field["custom_probe"],
                                "chart_definition": chart_meta,
                                "background_model": bg_meta,
                                "irradiance_w_m2": strength,
                                "modes": result_modes,
                                "raw_hashes": raw_hashes,
                                "ghost_mean_e": float(np.mean(mosaic[1]) * strength * 0.004),
                                "ghost_peak_e": float(np.max(mosaic[1]) * strength * 0.004),
                                "background_transmission_min_max": [
                                    float(transmission.min()),
                                    float(transmission.max()),
                                ],
                                "sampling_convergence": "not_established",
                            }
                            write_json(out / "case.json", case)
                            cases.append(case)
            check()
        refinement = None
        if previous is None:
            angle = min(s["angles_deg"], key=lambda a: abs(a - 15))
            progress(f"Refinement at {angle:g}°: {s['pupil_samples']} -> {s['pupil_samples'] * 2}")
            coarse = engine.map_angle(
                prescription,
                angle,
                s["azimuth_deg"],
                s["pupil_samples"],
                response,
                power,
                fields,
                project / "reference-ghost-cache",
                progress,
            )
            fine = engine.map_angle(
                prescription,
                angle,
                s["azimuth_deg"],
                s["pupil_samples"] * 2,
                response,
                power,
                fields,
                project / "reference-ghost-cache",
                progress,
            )
            checks = []
            with np.load(coarse / "rates.npz") as a, np.load(fine / "rates.npz") as b:
                for f in fields:
                    for ci, coating in enumerate(core.COATINGS):
                        lo = a[f["id"]][1, :, :, ci, :]
                        hi = b[f["id"]][1, :, :, ci, :]
                        relative_l1 = float(
                            np.mean(np.abs(hi - lo)) / max(float(np.mean(np.abs(hi))), 1e-12)
                        )
                        checks.append(
                            {
                                "field": f["id"],
                                "coating": coating,
                                "relative_l1_ghost_rate": relative_l1,
                                "within_demo_5_percent": relative_l1 <= 0.05,
                            }
                        )
            refinement = {
                "angle_deg": angle,
                "pupil_samples": [s["pupil_samples"], s["pupil_samples"] * 2],
                "checks": checks,
                "status": "within_demo_tolerance"
                if all(c["within_demo_5_percent"] for c in checks)
                else "inconclusive",
                "scope": "one representative angle, not all-angle qualification",
            }
            write_json(directory / "refinement.json", refinement)
        else:
            prior_result = json.loads((previous / "result.json").read_text())
            refinement = prior_result.get("refinement")
        _load_source(service, s, verify=True)
        result = {
            "schema": s["schema"],
            "name": s["name"],
            "specification": s,
            "source": s["source"],
            "atlas": atlas,
            "fields": fields,
            "cases": cases,
            "limitations": LIMITATIONS,
            "source_code": job["source_code"],
            "new_readout_frames": new_frames,
            "reprocess_of": s["reprocess_of"],
            "elapsed_seconds": time.monotonic() - start,
            "physical_validation": "not_established",
            "refinement": refinement,
        }
        write_json(directory / "result.json", result)
        report(directory, result)
        files = {
            str(p.relative_to(directory)): sha256(p)
            for p in directory.rglob("*")
            if p.is_file()
            and not p.name.startswith("._")
            and p.name not in ("manifest.json", "progress.json")
        }
        write_json(
            directory / "manifest.json",
            {"schema": "camera_reference_workshop_files_v1", "files": files},
        )
        summary = {
            **result,
            "manifest_sha256": sha256(directory / "manifest.json"),
            "package_bytes": sum((directory / p).stat().st_size for p in files),
            "files_base": f"{ROUTE}/{job['id']}/files/",
            "report_url": f"{ROUTE}/{job['id']}/files/report.html",
            "bundle_url": f"{ROUTE}/{job['id']}/bundle.zip",
        }
        artifact = service.registry.store_blob(
            canonical(summary).encode(), "reference-ghost-study.json", "application/json"
        )
        return {"summary": summary, "artifacts": [artifact]}


def report(directory, result):
    rows = "".join(
        f"<tr><td>{html.escape(c['id'])}</td><td>{c['angle_deg']}° / {html.escape(c['coating'])}</td><td>{html.escape(c['field_id'])} / {html.escape(c['chart'])}</td><td>{c['ghost_mean_e']:.3g}</td><td>{''.join('<figure style="display:inline-block;margin:3px"><figcaption>' + html.escape(m['id']) + '</figcaption><img width="160" src="' + m['image'] + '"></figure>' for m in c['modes'])}</td></tr>"
        for c in result["cases"]
    )
    (directory / "report.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Reference Ghost Lab</title><style>body{font:15px system-ui;max-width:1100px;margin:40px auto;background:#f1f6f3}td{padding:12px;border-bottom:1px solid #ccd}img{image-rendering:pixelated}li{margin:8px}</style><h1>Reference Ghost Lab</h1><p>직접광 → 2회 반사 → 센서 입사각·QE → RAW → 고정 ISP</p><ul>'
        + "".join("<li>" + html.escape(x) + "</li>" for x in result["limitations"])
        + "</ul><p>오른쪽 처리 영상의 포화·무효 픽셀은 동봉 valid mask로 확인하십시오. 전체 시야 지도는 광학 진단입니다.</p><table><tr><th>Case</th><th>광원 / 반사율</th><th>시야 / 차트</th><th>Ghost mean e</th><th>광원 OFF / 직접광 / 고스트 포함 (v1: 2모드)</th></tr>"
        + rows
        + "</table>"
    )


def get(service, identity):
    r = workshop.get(service, identity)
    if r.get("plugin_id") not in (LEGACY_SCHEMA, SCHEMA):
        raise ValueError("Not a Ghost study")
    return r


def install_routes(app, service):
    from urllib.parse import quote

    from fastapi.responses import RedirectResponse

    @app.get(ROUTE)
    def listing():
        rows = [get(service, r["run_id"]) for r in service.registry.list("reference_ghost_studies")]
        return {
            "template": template(),
            "studies": [{k: r[k] for k in ("id", "name", "status")} for r in rows],
        }

    @app.post(ROUTE, status_code=202)
    def create(payload: dict):
        return submit(service, payload)

    @app.get(ROUTE + "/{identity}")
    def detail(identity: str):
        return get(service, identity)

    @app.get(ROUTE + "/{identity}/files/{relative:path}")
    def file(identity: str, relative: str):
        get(service, identity)
        return RedirectResponse(f"{workshop.ROUTE}/runs/{identity}/files/{quote(relative)}")

    @app.get(ROUTE + "/{identity}/bundle.zip")
    def bundle(identity: str):
        get(service, identity)
        return RedirectResponse(f"{workshop.ROUTE}/runs/{identity}/bundle")
