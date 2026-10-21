"""Durable, source-pinned DB design studies on the existing workshop lifecycle."""

from __future__ import annotations

import copy
import html
import json
import math
import time
from pathlib import Path

from . import design_workshop_storage as storage
from . import reference_workshop_job as workshop
from .registry import IntegrityError, canonical, digest
from .virtual_lab_job import sha256

SCHEMA = "camera_db_design_study_v1"
ROUTE = "/api/v1/db-design"


def defaults():
    return {
        "schema": SCHEMA,
        "name": "DB Lens × Sensor · Reference comparison",
        "lens_keys": ["reference", "canon_p0248"],
        "sensor_keys": ["reference_imx462", "imx662"],
        "comparison_mode": "lens_sensor",
    }


def aperture_diffraction_recipe():
    return {
        **defaults(),
        "name": "Canon p0248 · F/2.75 ↔ F/4.12 · aberration + diffraction",
        "comparison_mode": "aperture_diffraction",
        "lens_keys": ["reference", "canon_p0248_open", "canon_p0248"],
        "sensor_keys": ["reference_imx462"],
    }


def _sweep_selection_policy(value):
    defaults = {"min_snr_db": 25.0, "min_relative_signal": 0.1, "max_saturation_fraction": 0.01}
    if value is None:
        return defaults
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError(
            "Aperture selection_policy accepts min_snr_db, min_relative_signal "
            "and max_saturation_fraction"
        )
    result = {**defaults, **value}
    bounds = {
        "min_snr_db": (-20.0, 80.0),
        "min_relative_signal": (0.0, 1.0),
        "max_saturation_fraction": (0.0, 1.0),
    }
    for key, (low, high) in bounds.items():
        number = result[key]
        if (
            type(number) not in (int, float)
            or not math.isfinite(number)
            or not low <= number <= high
        ):
            raise ValueError(f"selection_policy.{key} requires a finite number in [{low},{high}]")
        result[key] = float(number)
    return result


def aperture_sweep_recipe(f_numbers=None, selection_policy=None):
    from .db_design_optics import APERTURE_SWEEP_DEFAULTS, aperture_key

    values = copy.deepcopy(APERTURE_SWEEP_DEFAULTS if f_numbers is None else f_numbers)
    if not isinstance(values, list) or not 2 <= len(values) <= 8:
        raise ValueError("Aperture sweep requires 2..8 ascending unique F-numbers")
    keys = [aperture_key(value) for value in values]
    values = [float(value) for value in values]
    if any(a >= b for a, b in zip(values[:-1], values[1:], strict=True)):
        raise ValueError("Aperture sweep requires ascending unique F-numbers")
    return {
        **defaults(),
        "name": "Canon p0248 · aperture sweep · sharpness / light / SNR",
        "comparison_mode": "aperture_sweep",
        "lens_keys": ["reference", *keys],
        "sensor_keys": ["reference_imx462"],
        "f_numbers": values,
        "selection_policy": _sweep_selection_policy(selection_policy),
    }


def aperture_conditions_recipe(source_run_id=None, luminance_cd_m2=None, exposure_ms=None,
                               selection_policy=None, *, _max_pairs=24):
    """Declare a bounded grid; source optical/electrical identities remain immutable."""
    def axis(value, default, name, low, high):
        values = copy.deepcopy(default if value is None else value)
        if (not isinstance(values, list) or not 1 <= len(values) <= 8
                or any(type(v) not in (int, float) or not math.isfinite(v)
                       or not low <= v <= high for v in values)
                or any(a >= b for a, b in zip(values, values[1:]))):
            raise ValueError(f"{name} requires 1..8 ascending unique numbers in [{low},{high}]")
        return [float(v) for v in values]

    luminances = axis(luminance_cd_m2, [10, 100, 1000, 10000], "luminance_cd_m2", .1, 100000)
    exposures = axis(exposure_ms, [1, 4, 16], "exposure_ms", .1, 100)
    if len(luminances) * len(exposures) > _max_pairs:
        raise ValueError(f"Condition grid supports at most {_max_pairs} cells per explicit submission")
    if source_run_id is not None and (
        not isinstance(source_run_id, str) or len(source_run_id) != 36
        or not source_run_id.startswith("run-")
        or any(c not in "0123456789abcdef" for c in source_run_id[4:])
    ):
        raise ValueError("source_run_id requires a saved aperture sweep run ID")
    # A relative-light floor would permanently exclude narrow irises even in bright scenes.
    policy = _sweep_selection_policy(selection_policy)
    if selection_policy is None or "min_relative_signal" not in selection_policy:
        policy["min_relative_signal"] = 0.0
    return {
        "schema": SCHEMA,
        "name": "Aperture × scene luminance × exposure · conditional design map",
        "comparison_mode": "aperture_conditions",
        "source_run_id": source_run_id,
        "luminance_cd_m2": luminances,
        "exposure_ms": exposures,
        "selection_policy": policy,
    }


def aperture_motion_recipe(source_run_id=None, luminance_cd_m2=None, exposure_ms=None,
                           object_speed_m_s=None, object_distance_m=20.0,
                           selection_policy=None):
    """Joint iris/exposure candidates at a declared transverse target distance."""
    base = aperture_conditions_recipe(source_run_id, luminance_cd_m2, exposure_ms,
                                      selection_policy, _max_pairs=36)
    speeds = copy.deepcopy([0, 1, 4] if object_speed_m_s is None else object_speed_m_s)
    if (not isinstance(speeds, list) or not 1 <= len(speeds) <= 8
            or any(type(v) not in (int, float) or not math.isfinite(v)
                   or not 0 <= v <= 50 for v in speeds)
            or any(a >= b for a, b in zip(speeds, speeds[1:]))):
        raise ValueError("object_speed_m_s requires 1..8 ascending unique numbers in [0,50]")
    if (type(object_distance_m) not in (int, float) or not math.isfinite(object_distance_m)
            or not 1 <= object_distance_m <= 1000):
        raise ValueError("object_distance_m requires a finite number in [1,1000]")
    cells = len(base["luminance_cd_m2"]) * len(speeds)
    if cells > 24 or cells * len(base["exposure_ms"]) > 36:
        raise ValueError("Motion study supports at most 24 map cells and 36 cell/exposure pairs")
    return {
        **base,
        "name": "Aperture × exposure · luminance and transverse motion design map",
        "comparison_mode": "aperture_motion",
        "object_speed_m_s": [float(v) for v in speeds],
        "object_distance_m": float(object_distance_m),
    }


def shutter_motion_recipe(source_run_id=None, luminance_cd_m2=None, exposure_ms=None,
                          object_speed_m_s=None, object_distance_m=20.0, readout_ms=None,
                          f_numbers=None, selection_policy=None):
    """Compare row scans on a virtual native probe; zero scan is the matched GS."""
    base = aperture_motion_recipe(
        source_run_id, [100, 1000] if luminance_cd_m2 is None else luminance_cd_m2,
        exposure_ms, object_speed_m_s, object_distance_m, selection_policy)
    scans = copy.deepcopy([0, 5, 15, 30] if readout_ms is None else readout_ms)
    apertures = copy.deepcopy([4.12] if f_numbers is None else f_numbers)
    for name, values, low, high in (("readout_ms", scans, 0, 100),
                                     ("f_numbers", apertures, 2.75, 11)):
        if (not isinstance(values, list) or not 1 <= len(values) <= 8
                or any(type(v) not in (int, float) or not math.isfinite(v)
                       or not low <= v <= high for v in values)
                or any(a >= b for a, b in zip(values, values[1:]))):
            raise ValueError(f"{name} requires 1..8 ascending unique numbers in [{low},{high}]")
    if scans[0] != 0 or len(scans) < 2:
        raise ValueError("readout_ms must include 0 for matched global shutter and a positive scan")
    count = (len(base["luminance_cd_m2"]) * len(base["object_speed_m_s"])
             * len(base["exposure_ms"]) * len(scans) * len(apertures))
    if count > 216:
        raise ValueError("Shutter study supports at most 216 camera/exposure/scan evaluations")
    return {**base, "name": "Global / rolling shutter · exposure blur and row geometry",
            "comparison_mode": "shutter_motion", "readout_ms": [float(v) for v in scans],
            "f_numbers": [float(v) for v in apertures]}


def sensor_shutter_recipe(source_run_id=None, luminance_cd_m2=None, exposure_ms=None,
                          object_speed_m_s=None, object_distance_m=20.0, line_time_us=None,
                          f_numbers=None, selection_policy=None, operating_mode_id=None,
                          fps=None, located_source_run_id=None):
    """Locate native probes on the source-backed sensor; line times remain declared."""
    base = shutter_motion_recipe(source_run_id, luminance_cd_m2, exposure_ms,
                                 object_speed_m_s, object_distance_m, [0, 1],
                                 f_numbers, selection_policy)
    operating = None
    if operating_mode_id is not None:
        from . import db_design_sensor_modes as modes
        operating = modes.mode(operating_mode_id, fps)
        expected_lines = [0., operating['line_time_us']]
        if line_time_us is not None and line_time_us != expected_lines:
            raise ValueError('Operating mode fixes line_time_us; arbitrary line-time override is not allowed')
        line_time_us = expected_lines
        modes.schedule(operating, base['exposure_ms'])
    elif fps is not None:
        raise ValueError('FPS constraints require operating_mode_id')
    lines = copy.deepcopy([0, 10, 20] if line_time_us is None else line_time_us)
    if (not isinstance(lines, list) or not 2 <= len(lines) <= 8
            or any(type(v) not in (int, float) or not math.isfinite(v)
                   or not (v == 0 or .1 <= v <= 100) for v in lines)
            or lines[0] != 0 or any(a >= b for a, b in zip(lines, lines[1:]))):
        raise ValueError("line_time_us requires GS 0 then 1..7 ascending positive values in [0.1,100]")
    count = (len(base["luminance_cd_m2"]) * len(base["object_speed_m_s"])
             * len(base["exposure_ms"]) * len(lines) * len(base["f_numbers"]))
    if count > 216:
        raise ValueError("Sensor shutter study supports at most 216 camera/exposure/line-time evaluations")
    base.pop("readout_ms")
    recipe = {**base, "name": "Sensor rows × line time · located ROI global / rolling shutter",
              "comparison_mode": "sensor_shutter", "line_time_us": [float(v) for v in lines]}
    if operating is not None:
        recipe.update(operating_mode_id=operating_mode_id, fps=operating['requested_fps'])
    if located_source_run_id is not None:
        import re
        if not isinstance(located_source_run_id, str) or not re.fullmatch(r'run-[0-9a-f]{32}', located_source_run_id):
            raise ValueError('Located source requires a preserved run ID')
        recipe['located_source_run_id'] = located_source_run_id
    return recipe


def specification(payload):
    from .db_design_optics import SOURCES
    from .db_design_sensor import _PINS

    if isinstance(payload, dict) and payload.get("comparison_mode") in {
        "aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"
    }:
        sensor_shutter = payload["comparison_mode"] == "sensor_shutter"
        shutter = payload["comparison_mode"] == "shutter_motion"
        motion = payload["comparison_mode"] in {"aperture_motion", "shutter_motion", "sensor_shutter"}
        recipe_fn = (sensor_shutter_recipe if sensor_shutter else shutter_motion_recipe if shutter
                     else aperture_motion_recipe if motion else aperture_conditions_recipe)
        optional = {'operating_mode_id', 'fps', 'located_source_run_id'} if sensor_shutter else set()
        if set(payload) - (set(recipe_fn()) | optional):
            raise ValueError("Condition studies accept only declared source, axes and selection policy")
        arguments = {key: payload.get(key) for key in (
            "source_run_id", "luminance_cd_m2", "exposure_ms", "selection_policy"
        )}
        if motion:
            arguments.update(object_speed_m_s=payload.get("object_speed_m_s"),
                             object_distance_m=payload.get("object_distance_m", 20.0))
        if shutter:
            arguments.update(readout_ms=payload.get("readout_ms"), f_numbers=payload.get("f_numbers"))
        if sensor_shutter:
            arguments.update(line_time_us=payload.get("line_time_us"), f_numbers=payload.get("f_numbers"))
            arguments.update({key: payload.get(key) for key in optional})
        recipe = recipe_fn(**arguments)
        if recipe["source_run_id"] is None:
            raise ValueError("Condition study requires a saved aperture sweep source_run_id")
        recipe["name"] = payload.get("name", recipe["name"])
        if (payload.get("schema", SCHEMA) != SCHEMA or not isinstance(recipe["name"], str)
                or not 1 <= len(recipe["name"]) <= 160):
            raise ValueError("Select the DB design schema and a 1..160 character name")
        return recipe
    if not isinstance(payload, dict) or set(payload) - (
        set(defaults()) | {"f_numbers", "selection_policy"}
    ):
        raise ValueError(
            "Study accepts DB design fields and aperture sweep f_numbers/selection_policy"
        )
    spec = {**defaults(), **copy.deepcopy(payload)}
    if (
        spec["schema"] != SCHEMA
        or not isinstance(spec["name"], str)
        or not 1 <= len(spec["name"]) <= 160
    ):
        raise ValueError("Select the DB design schema and a 1..160 character name")
    if spec["comparison_mode"] == "aperture_sweep":
        recipe = aperture_sweep_recipe(spec.get("f_numbers"), spec.get("selection_policy"))
        if any(
            key in payload and payload[key] != recipe[key] for key in ("lens_keys", "sensor_keys")
        ):
            raise ValueError("Aperture sweep lens_keys must match f_numbers and the pinned IMX462")
        recipe["name"] = spec["name"] if "name" in payload else recipe["name"]
        return recipe
    if "f_numbers" in spec or "selection_policy" in spec:
        raise ValueError("f_numbers and selection_policy require aperture_sweep comparison_mode")
    if spec["comparison_mode"] == "aperture_diffraction":
        recipe = aperture_diffraction_recipe()
        if any(spec[k] != recipe[k] for k in ("lens_keys", "sensor_keys")):
            raise ValueError(
                "Aperture diffraction requires the pinned reference/open/stopped trio and IMX462"
            )
        return spec
    if spec["comparison_mode"] != "lens_sensor":
        raise ValueError(
            "Select lens_sensor, aperture_diffraction or aperture_sweep comparison_mode"
        )
    for field, choices, baseline in (
        ("lens_keys", SOURCES, "reference"),
        ("sensor_keys", _PINS, "reference_imx462"),
    ):
        keys = spec[field]
        if (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= 2
            or any(not isinstance(k, str) or k not in choices for k in keys)
            or len(set(keys)) != len(keys)
            or keys[0] != baseline
            or (field == "lens_keys" and "canon_p0248_open" in keys)
        ):
            raise ValueError(
                field + " requires reference first and at most one supported alternative"
            )
    return spec


def fingerprint():
    result = workshop.fingerprint()
    for path in sorted(Path(__file__).parent.glob("db_design_*.py")):
        result[path.name] = sha256(path)
    result["reference_exit_pupil.py"] = sha256(Path(__file__).parent / "reference_exit_pupil.py")
    result["sensor_simulation_db.py"] = sha256(
        Path(__file__).parents[1] / "pyisetcam/sensor_simulation_db.py"
    )
    return result


def _rows(rows):
    return [
        {
            **r,
            "classification": r.get("classification", r.get("origin", "unclassified")),
            "source": r.get(
                "source", r.get("note", r.get("source_sha256", "pinned source manifest"))
            ),
            "unit": r.get("unit", ""),
        }
        for r in rows
    ]


def snapshot(workspace, spec):
    from . import db_design_optics as lenses
    from . import db_design_sensor as sensors

    selected = {"lenses": {}, "sensors": {}}
    for family, choices, keys in (
        ("lenses", lenses.candidates(workspace, spec["lens_keys"]), spec["lens_keys"]),
        ("sensors", sensors.candidates(workspace), spec["sensor_keys"]),
    ):
        for row in choices:
            if row["key"] not in keys:
                continue
            if not row["execution_ready"]:
                raise ValueError(row.get("reason") or "Selected DB adapter is unavailable")
            item = copy.deepcopy(row)
            item["parameter_sources"] = _rows(item.get("parameter_sources", []))
            if family == "sensors":
                item["cell_sha256"] = sensors.build_cell(workspace, row["key"])["cell_sha256"]
            selected[family][row["key"]] = item
    if any(
        len(selected[f]) != len(spec[k])
        for f, k in (("lenses", "lens_keys"), ("sensors", "sensor_keys"))
    ):
        raise ValueError("A selected pinned DB candidate is unavailable")
    return selected


def _sensor_timing_profile(workspace):
    from .db_design_sensor_timing import profile
    return profile(workspace)


def options(service):
    from . import db_design_optics as lenses
    from . import db_design_sensor as sensors
    from . import db_design_sensor_modes as modes

    return {
        "schema": SCHEMA,
        "defaults": defaults(),
        "recipes": {
            "aperture_diffraction": aperture_diffraction_recipe(),
            "aperture_sweep": aperture_sweep_recipe(),
            "aperture_conditions": aperture_conditions_recipe(),
            "aperture_motion": aperture_motion_recipe(),
            "shutter_motion": shutter_motion_recipe(),
            "sensor_shutter": sensor_shutter_recipe(),
        },
        "sensor_timing_profile": _sensor_timing_profile(service.workspace),
        "sensor_operating_modes": modes.catalog(service.workspace),
        "lenses": lenses.candidates(service.workspace),
        "sensors": sensors.candidates(service.workspace),
        "recent_runs": [
            {
                "id": r["id"],
                "name": r["name"],
                "status": r["status"],
                "comparison_mode": r.get("specification", {}).get("comparison_mode"),
                "operating_mode_id": r.get("specification", {}).get("operating_mode_id"),
                "fps": r.get("specification", {}).get("fps"),
                "timing_configuration": (r.get("specification")
                                         if r.get("specification", {}).get("operating_mode_id") else None),
                "result_url": "/db-design.html?run=" + r["id"],
            }
            for r in service.registry.list_runs()
            if r.get("db_design_study")
        ],
        "fixed_conditions": {
            "native_shape": [256, 256],
            "pixel_pitch_um": 2.9,
            "fields": ["center", "outer-ne"],
            "frames": 8,
            "exposure_ms": 4,
            "gain": 1,
            "luminance_cd_m2": 1000,
            "illuminant": "D65",
            "lsc": "off_for_all",
            "color": "common_baseline_spectral_CCM",
            "fpn": "off_for_all_saved_FPN_metadata_retained",
        },
        "physical_validation": "not_established",
    }


def condition_source(service, spec):
    record = workshop.get(service, spec["source_run_id"])
    if record["status"] != "succeeded" or not record.get("db_design_study"):
        raise ValueError("Condition source must be a completed DB aperture sweep")
    summary = record["result"]["summary"]
    if (summary.get("comparison_mode") != "aperture_sweep"
            or not summary.get("aperture_sweep") or not summary.get("raw_preserved")):
        raise ValueError("Condition source requires preserved aperture sweep responses")
    pinned = {
        "source_run_id": record["id"],
        "source_manifest_sha256": summary["manifest_sha256"],
        "source_summary_sha256": digest(summary),
        "source_code": record["source_code"],
        "f_numbers": summary["configuration"]["f_numbers"],
        "source_snapshot": summary.get("source_snapshot"),
    }
    if spec["comparison_mode"] in {"shutter_motion", "sensor_shutter"}:
        available = summary["configuration"]["f_numbers"]
        if any(value not in available for value in spec["f_numbers"]):
            raise ValueError("Requested shutter F-numbers must be present in the preserved source sweep")
        pinned["selected_f_numbers"] = list(spec["f_numbers"])
    if spec["comparison_mode"] == "sensor_shutter":
        pinned["sensor_timing_profile"] = _sensor_timing_profile(service.workspace)
        if spec.get('operating_mode_id'):
            from . import db_design_sensor_modes as modes
            pinned['sensor_operating_modes'] = modes.catalog(service.workspace)
            pinned['operating_mode'] = modes.mode(spec['operating_mode_id'], spec.get('fps'))
        if spec.get('located_source_run_id'):
            cached = workshop.get(service, spec['located_source_run_id'])
            prior = cached.get('result', {}).get('summary', {})
            if (cached['status'] != 'succeeded' or prior.get('comparison_mode') != 'sensor_shutter'
                    or prior.get('sensor_timing_profile') != pinned['sensor_timing_profile']
                    or prior.get('configuration', {}).get('source_run_id') != spec['source_run_id']
                    or any(f not in prior['configuration']['f_numbers'] for f in spec['f_numbers'])):
                raise ValueError('Located optical source must match the preserved parent, ROI profile and apertures')
            pinned['located_source'] = {'run_id':cached['id'], 'manifest_sha256':prior['manifest_sha256'],
                'summary_sha256':digest(prior)}
    return workshop._directory(service, record["id"]), summary, pinned


def _snapshot(service, spec):
    if spec["comparison_mode"] in {"aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"}:
        return condition_source(service, spec)[2]
    return snapshot(service.workspace, spec)


def submit(service, payload):
    spec = specification(payload)
    pinned = _snapshot(service, spec)
    evaluations = (len(pinned.get("selected_f_numbers", pinned["f_numbers"])) * len(spec["luminance_cd_m2"])
                   * len(spec["exposure_ms"]) * len(spec.get("object_speed_m_s", [0]))
                   * len(spec.get("line_time_us", spec.get("readout_ms", [0])))
                   if spec["comparison_mode"] in {"aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"}
                   else None)
    if spec["comparison_mode"] in {"aperture_motion", "shutter_motion", "sensor_shutter"} and evaluations > 216:
        raise ValueError("Motion study supports at most 216 camera/exposure evaluations")
    estimated = (
        512 * 1024**2 * evaluations
        if evaluations is not None
        else 2 * 1024**3 * len(spec["lens_keys"]) * len(spec["sensor_keys"])
    )
    if spec["comparison_mode"] == "sensor_shutter":
        estimated += 2 * 1024**3 * len(spec["f_numbers"])  # New located PSFs/rate-plane source.
    state = storage.status(service)
    if (
        state["remaining_bytes"] < estimated
        or state["volume_free_bytes"] - estimated < storage.RESERVE_BYTES
    ):
        raise ValueError("SSD budget insufficient; no sampling reduction applied")
    document = {
        "job_kind": workshop.JOB_KIND,
        "plugin_id": SCHEMA,
        "name": spec["name"],
        "db_design_study": {"schema": SCHEMA, "snapshot": pinned},
        "specification": spec,
        "source_code": fingerprint(),
        "storage_root": state["path"],
        "estimated_bytes": estimated,
        "default_worker_timeout_seconds": 14400
        if spec["comparison_mode"] in {"aperture_sweep", "aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"}
        else 3600,
        "submission_ns": time.time_ns(),
    }
    return service.registry.submit(document, digest(document))


def execute(service, job):
    from . import db_design_engine as engine

    if job["source_code"] != fingerprint():
        raise IntegrityError("DB study implementation changed after submission")
    spec, pinned = job["specification"], job["db_design_study"]["snapshot"]
    if digest(_snapshot(service, spec)) != digest(pinned):
        raise IntegrityError("DB study sources changed after submission")
    if job["storage_root"] != str(storage.root(service)):
        raise IntegrityError("DB study storage changed")
    with storage.reserve(service, job["estimated_bytes"]) as (_, budget):
        directory = workshop._directory(service, job["id"])
        directory.mkdir(parents=True, exist_ok=True)

        def progress(message):
            if service.registry.get_run(job["id"])["status"] != "running":
                raise RuntimeError("DB design study cancelled")
            engine.shared.write_json(directory / "progress.json", {"stage": str(message)})

        def cancel_check():
            if service.registry.get_run(job["id"])["status"] != "running":
                raise RuntimeError("DB design study cancelled")

        if spec["comparison_mode"] in {"aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"}:
            if spec["comparison_mode"] == "sensor_shutter":
                from . import db_design_sensor_shutter as executor
            elif spec["comparison_mode"] == "shutter_motion":
                from . import db_design_shutter as executor
            elif spec["comparison_mode"] == "aperture_motion":
                from . import db_design_motion as executor
            else:
                from . import db_design_conditions as executor

            source_root, source_summary, source_pinned = condition_source(service, spec)
            execution_options = ({"sensor_timing_profile": source_pinned["sensor_timing_profile"]}
                                 if spec["comparison_mode"] == "sensor_shutter" else {})
            if spec.get('located_source_run_id'):
                execution_options['located_source_root'] = workshop._directory(service, spec['located_source_run_id'])
                execution_options['located_source_manifest_sha256'] = source_pinned['located_source']['manifest_sha256']
            result = executor.run(
                service.workspace, directory, spec, source_root, source_summary,
                progress, check=budget, cancel_check=cancel_check,
                **execution_options,
            )
        else:
            result = engine.run(service.workspace, directory, spec, pinned, progress, check=budget)
        progress("Verifying original DB snapshots and native artifacts")
        if job["source_code"] != fingerprint() or digest(
            _snapshot(service, spec)
        ) != digest(pinned):
            raise IntegrityError("DB source or implementation changed during execution")
        result.update(id=job["id"], status="succeeded", source_snapshot=pinned)
        engine.shared.write_json(directory / "result.json", result)
        # A portable report retains the exact source/assumption tables and local images.
        blocks = [
            "<!doctype html><meta charset='utf-8'><title>DB Design Lab</title>",
            "<style>body{font:16px system-ui;max-width:1200px;margin:32px auto;"
            "background:#f7f8fa}img{width:256px}pre{white-space:pre-wrap}"
            "article{background:white;padding:20px;margin:20px 0}</style>",
            "<h1>DB Lens × Sensor — native RAW / ISP</h1>",
            "<p>Same native sensor-plane charts, fixed Reference CCM. Different lens FOVs; "
            "no product ranking. Physical validation: not established.</p>",
        ]
        if result.get('operating_mode'):
            blocks.append('<article><h2>Operating mode · timing evidence and exposure limits</h2><pre>'
                          + html.escape(json.dumps({'operating_mode':result['operating_mode'],
                              'exposure_schedule':result['exposure_schedule']},ensure_ascii=False,indent=2))
                          + '</pre><p>Blocked exposure requests generate no RAW. Both GS and RS use the same '
                            'quantized actual exposure. Stored structural readout retained.</p></article>')
        if "aperture_sweep" in result and "aperture_conditions" not in result:
            sweep = result["aperture_sweep"]
            selection = sweep["selection"]
            blocks.extend(
                [
                    "<article><h2>Aperture sweep — fixed exposure and common ISP</h2>",
                    "<p>Selection: "
                    + html.escape(
                        str(selection.get("recommended_candidate_id") or "no feasible candidate")
                    )
                    + "</p>",
                    "<pre>" + html.escape(json.dumps(selection["policy"], indent=2)) + "</pre>",
                    "<table><thead><tr><th>F-number</th><th>Field</th><th>SFR50 (cyc/px)</th>"
                    "<th>Photoelectrons</th><th>Relative signal</th><th>Temporal SNR (dB)</th>"
                    "<th>High saturation upper bound (well / ADC / output)</th>"
                    "<th>Decision / reasons</th></tr></thead><tbody>",
                ]
            )
            decisions = {row["candidate_id"]: row for row in selection["decisions"]}
            for row in sweep["rows"]:
                decision = decisions[row["candidate_id"]]
                for field in row["fields"]:
                    cells = [
                        row.get("f_number"),
                        field.get("field_id"),
                        field.get("processed_sfr50_cpp"),
                        field.get("mean_signal_e"),
                        field.get("relative_signal"),
                        field.get("snr_db"),
                        field.get("saturation_fraction"),
                        "eligible" if decision["eligible"] else "; ".join(decision["reasons"]),
                    ]
                    blocks.append(
                        "<tr>"
                        + "".join(
                            "<td>"
                            + html.escape(
                                f"{v:.5g}"
                                if isinstance(v, (int, float))
                                else str(v if v is not None else "unavailable")
                            )
                            + "</td>"
                            for v in cells
                        )
                        + "</tr>"
                    )
            blocks.extend(["</tbody></table><p>", html.escape(sweep["scope"]), "</p></article>"])
        if "aperture_conditions" in result:
            grid = result["aperture_conditions"]
            sensor_shutter = spec["comparison_mode"] == "sensor_shutter"
            shutter = spec["comparison_mode"] in {"shutter_motion", "sensor_shutter"}
            motion = spec["comparison_mode"] in {"aperture_motion", "shutter_motion", "sensor_shutter"}
            blocks.extend([
                "<article><h2>" + ("Global / rolling shutter — exposure blur and row geometry" if shutter else
                                    "Scene luminance × motion — joint aperture/exposure selection"
                                    if motion else "Scene luminance × exposure — conditional aperture selection")
                + "</h2>",
                ("<p>Sensor dimensions are source-backed. Line time, scan order, active-to-total "
                 "offset and CFA origin are declared assumptions, not a verified product mode. "
                 "ROI positions and optical responses are regenerated inside the active array. "
                 "All fields use a shared active-array-center clock; field timing retains the ROI "
                 "offset as well as local row shear. Whole-array scan and local ROI scan are separate. "
                 "Selection is separate within each line time; SFR is not geometric fidelity.</p>" if sensor_shutter else
                 "<p>Readout ms is the first-to-last exposure-start scan of the virtual 256-row probe, "
                 "not a product full-frame readout or transport latency. Row time = scan / 255. "
                 "GS and RS share the center-row exposure midpoint. Per-row pre-mosaic response "
                 "integration precedes fresh RAW readout. Matched GS uses the same F-number and exposure. "
                 "Selection is separate within each scan; SFR alone does not rank shutter designs. "
                 "Noise-free edge geometry and native skew are separate from processed SFR. "
                 "Fixed CCM/display; no rolling-shutter correction, 3D or occlusion.</p>" if shutter else
                 "<p>Fixed camera, transverse horizontal target motion at a declared distance. "
                 "Pinhole image-speed conversion and native linear response interpolation; "
                 "common supported ROI excludes unavailable surroundings. No rolling shutter or occlusion. "
                 "Each exposure integrates noise-free RGGB planes before fresh RAW readout. "
                 "Fixed CCM/display. D65 luminance in cd/m², not lux.</p>" if motion else
                "<p>D65 scene luminance in cd/m², not incident illuminance in lux. Static input; "
                "longer exposures do not model motion blur. Identical fixed CCM and display scale. "
                "Every condition has fresh noisy integer RAW; optical rates alone are reused.</p>"),
                "<pre>" + html.escape(json.dumps(spec["selection_policy"], indent=2)) + "</pre>",
            ])
            if sensor_shutter:
                blocks.append("<details><summary>Sensor timing sources and assumptions</summary><pre>"
                              + html.escape(json.dumps(result.get("sensor_timing_profile"), ensure_ascii=False, indent=2))
                              + "</pre></details>")
            for condition in grid["conditions"]:
                sweep = condition["aperture_sweep"]
                choice = sweep["selection"].get("recommended_candidate_id")
                decisions = {r["candidate_id"]: r for r in sweep["selection"]["decisions"]}
                blocks.append("<h3>" + html.escape(
                    f"{condition['luminance_cd_m2']:g} cd/m² · "
                    + (f"{condition['object_speed_m_s']:g} m/s at {spec['object_distance_m']:g} m"
                       if motion else f"{condition['exposure_ms']:g} ms")
                ) + "</h3><p>" + ("GS reference selection: " if shutter else "Selection: ")
                  + html.escape(choice or "no feasible candidate") + "</p>")
                if shutter:
                    blocks.append("<details><summary>Separate selections within each scan</summary><pre>"
                                  + html.escape(json.dumps(sweep["selection"].get("selections_by_readout_ms", {}),
                                                          ensure_ascii=False, indent=2)) + "</pre></details>")
                blocks.append("<table><tr><th>F-number</th>"
                              + ("<th>Exposure ms</th><th>Blur px</th>" if motion else "")
                              + ("<th>Shutter</th><th>Line time µs</th><th>Modeled total scan ms</th>"
                                 "<th>Local ROI scan ms</th><th>Local ROI skew px</th>" if sensor_shutter else
                                 "<th>Shutter</th><th>First-to-last scan ms</th><th>Full-probe skew px</th>" if shutter else "")
                              + "<th>Worst SFR50</th>"
                              "<th>Worst SNR dB</th><th>High saturation upper bound</th>"
                              "<th>Decision / reasons</th></tr>")
                for row in sweep["rows"]:
                    decision = decisions[row["candidate_id"]]
                    values = [row["requested_f_number"]]
                    if motion:
                        values += [row.get("exposure_ms"), row.get("blur_length_px")]
                    if sensor_shutter:
                        values += [row.get("shutter_mode"), row.get("line_time_us"), row.get("readout_ms"),
                                   row.get("local_roi_scan_ms"), row.get("top_bottom_skew_px")]
                    elif shutter:
                        values += [row.get("shutter_mode"), row.get("readout_ms"), row.get("top_bottom_skew_px")]
                    values += [row.get("worst_field_sfr50_cpp"),
                              row.get("worst_field_snr_db"), row.get("max_saturation_fraction"),
                              "eligible" if decision["eligible"] else "; ".join(decision["reasons"])]
                    blocks.append("<tr>" + "".join("<td>" + html.escape(
                        f"{v:.5g}" if isinstance(v, (int, float)) else str(v)
                    ) + "</td>" for v in values) + "</tr>")
                blocks.append("</table><details><summary>Native ISP comparisons</summary>")
                for candidate in condition["candidates"]:
                    blocks.append("<p>" + html.escape(candidate["label"]) + "</p>")
                    for field in candidate["result"]["fields"]:
                        path = candidate["prefix"] + field["images"]["resolution"]["display"]
                        blocks.append("<img alt='" + html.escape(field["id"]) + "' src='"
                                      + html.escape(path, quote=True) + "'>")
                blocks.append("</details>")
            blocks.append("</article>")
        for candidate in result["candidates"]:
            blocks.append("<article><h2>" + html.escape(candidate["label"]) + "</h2>")
            for field in candidate["result"]["fields"]:
                blocks.append("<h3>" + html.escape(field["label"]) + "</h3>")
                for chart in engine.CHARTS:
                    path = candidate["prefix"] + field["images"][chart]["display"]
                    blocks.append("<img alt='" + chart + "' src='" + path + "'>")
            blocks.append(
                "<details><summary>Sources / assumptions</summary><pre>"
                + html.escape(
                    json.dumps(candidate["parameter_sources"], ensure_ascii=False, indent=2)
                )
                + "</pre></details></article>"
            )
        (directory / "report.html").write_text("".join(blocks))
        files = {
            str(p.relative_to(directory)): sha256(p)
            for p in sorted(directory.rglob("*"))
            if p.is_file()
            and p.name not in {"manifest.json", "progress.json"}
            and not p.name.startswith("._")
        }
        engine.shared.write_json(
            directory / "manifest.json",
            {"schema": "camera_reference_workshop_files_v1", "files": files},
        )
        base = f"{workshop.ROUTE}/runs/{job['id']}"
        summary = {
            **result,
            "files_base": base + "/files/",
            "report_url": base + "/files/report.html",
            "manifest_url": base + "/files/manifest.json",
            "bundle_url": base + "/bundle",
            "manifest_sha256": sha256(directory / "manifest.json"),
            "result_url": "/db-design.html?run=" + job["id"],
            "package_bytes": sum((directory / name).stat().st_size for name in files),
        }
        if "aperture_sweep" in result and "aperture_conditions" not in result:
            sweep = result["aperture_sweep"]
            chosen = (
                sweep["selection"].get("recommended_candidate_id") or sweep["baseline_candidate_id"]
            )
            summary["result_url"] += (
                "&candidate-a=" + sweep["baseline_candidate_id"] + "&candidate-b=" + chosen
            )
        if "aperture_conditions" in result:
            grid = result["aperture_conditions"]
            condition = next((c for c in grid["conditions"]
                              if c["id"] == grid.get("default_condition_id")), grid["conditions"][0])
            sweep = condition["aperture_sweep"]
            chosen = sweep["selection"].get("recommended_candidate_id") or sweep["baseline_candidate_id"]
            baseline = condition.get("default_candidate_a_id", sweep["baseline_candidate_id"])
            chosen = condition.get("default_candidate_b_id", chosen)
            summary["result_url"] += ("&condition=" + condition["id"] + "&candidate-a="
                                      + baseline + "&candidate-b=" + chosen)
        artifact = service.registry.store_blob(
            canonical(summary).encode(), "db-design-study.json", "application/json"
        )
        return {"summary": summary, "artifacts": [artifact]}


def install_routes(app, service):
    @app.get(ROUTE + "/options", tags=["db_design"])
    def get_options():
        return options(service)

    @app.post(ROUTE + "/studies", status_code=202, tags=["db_design"])
    def create(payload: dict):
        return submit(service, payload)
